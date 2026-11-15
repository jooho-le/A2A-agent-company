"""Host-owned final reports, durably published with their completion event.

This is a producer, not an export-time inference. It reads registered immutable
artifacts only; generated code is never executed. Final report bytes and their
completion receipt commit in the same database transaction as the Trace event.
"""
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import json
import logging
import os
from uuid import UUID

from agents.platform.export_readonly import ReadOnlyWorkflowRepository
from agents.platform.snapshot_export import export_snapshots
from orchestrator.domain.models import utc_now
from orchestrator.domain.trace import TraceEvent
from orchestrator.infrastructure.sqlite_workflows import _insert_event


_KINDS = ('REQUIREMENT', 'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT')
_LOG = logging.getLogger(__name__)


def _json(value):
    # Same canonical export bytes as evaluation_export; manifest hashes must agree.
    return (json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2) + '\n').encode('utf-8')


class PackageProductionError(ValueError):
    pass


class HostPackageService:
    def __init__(self, repository, *, clock=utc_now):
        self.repository, self.clock = repository, clock
        with repository._transaction() as connection:
            connection.execute('''CREATE TABLE IF NOT EXISTS host_package_enrollments (
                run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id), payload_json TEXT NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS host_final_reports (
                run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id), report BLOB NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS host_result_packages (
                run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
                event_id TEXT NOT NULL UNIQUE REFERENCES trace_events(event_id),
                completion BLOB NOT NULL)''')
            connection.execute('''CREATE TABLE IF NOT EXISTS host_package_attempts (
                run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
                status TEXT NOT NULL, reason TEXT)''')
            for table in ('host_package_enrollments', 'host_final_reports', 'host_result_packages'):
                for operation in ('UPDATE', 'DELETE'):
                    connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()}
                        BEFORE {operation} ON {table}
                        BEGIN SELECT RAISE(ABORT, 'immutable result package'); END''')
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_no_replace
                    BEFORE INSERT ON {table}
                    WHEN EXISTS(SELECT 1 FROM {table} WHERE run_id=NEW.run_id)
                    BEGIN SELECT RAISE(ABORT, 'immutable result package'); END''')

    def enroll(self, run_id):
        """Pin producer participation before execution; never enroll historical results."""
        run_id = UUID(str(run_id))
        with self.repository._transaction() as connection:
            view = ReadOnlyWorkflowRepository(self.repository.database_path)
            view._pinned = connection
            run, configuration = view.get_run(run_id), view.get_run_configuration(run_id)
            if run is None or configuration is None:
                raise PackageProductionError('PACKAGE_RUN_NOT_FOUND')
            binding = {'schemaVersion': 1, 'runId': str(run_id), 'producer': 'HOST_AUTOMATIC',
                       'configurationSha256': sha256(_json(configuration.to_artifact_json())).hexdigest()}
            previous = connection.execute('SELECT payload_json FROM host_package_enrollments WHERE run_id=?',
                                          (str(run_id),)).fetchone()
            if previous:
                recorded = json.loads(previous[0])
                if {k: v for k, v in recorded.items() if k != 'enrolledAt'} != binding:
                    raise PackageProductionError('PACKAGE_ENROLLMENT_CHANGED')
                return recorded
            if (run.status.value not in ('RECEIVED', 'PLANNING') or run.code_version is not None or run.fix_attempt != 0
                    or connection.execute("SELECT 1 FROM trace_events WHERE run_id=? AND event_type IN "
                                          "('A2A_MESSAGE_SENT','LLM_MODEL_CALLED','WORKFLOW_RESUMED') LIMIT 1",
                                          (str(run_id),)).fetchone()
                    or connection.execute('SELECT 1 FROM project_artifacts WHERE run_id=? LIMIT 1',
                                          (str(run_id),)).fetchone()):
                raise PackageProductionError('PACKAGE_ENROLLMENT_TOO_LATE')
            binding['enrolledAt'] = utc_now().isoformat()
            connection.execute('INSERT INTO host_package_enrollments VALUES(?,?)',
                               (str(run_id), json.dumps(binding, ensure_ascii=False, sort_keys=True)))
            return binding

    @staticmethod
    def _status(connection, run_id, status, reason=None):
        connection.execute('''INSERT INTO host_package_attempts(run_id,status,reason) VALUES(?,?,?)
            ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,reason=excluded.reason''',
            (str(run_id), status, reason))
        return {'status': status, 'reason': reason}

    def complete(self, run_id):
        """Idempotent Host completion. Failed writes roll back report and event together."""
        run_id = UUID(str(run_id))
        with self.repository._transaction() as connection:
            if connection.execute('SELECT 1 FROM host_result_packages WHERE run_id=?', (str(run_id),)).fetchone():
                return {'status': 'COMPLETED', 'reason': None}
            view = ReadOnlyWorkflowRepository(self.repository.database_path)
            view._pinned = connection
            run = view.get_run(run_id)
            if run is None:
                raise PackageProductionError('PACKAGE_RUN_NOT_FOUND')
            enrollment = connection.execute('SELECT payload_json FROM host_package_enrollments WHERE run_id=?',
                                            (str(run_id),)).fetchone()
            if enrollment is None:
                return {'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_PRODUCER_NOT_ENROLLED'}
            configuration = view.get_run_configuration(run_id)
            if (configuration is None or json.loads(enrollment[0]).get('configurationSha256')
                    != sha256(_json(configuration.to_artifact_json())).hexdigest()):
                raise PackageProductionError('PACKAGE_ENROLLMENT_CHANGED')
            if run.status.value not in ('FINISHED', 'ABORTED'):
                return self._status(connection, run_id, 'NOT_COMPLETED', 'PACKAGE_RUN_NOT_TERMINAL')
            try:
                report, files, events = self._prepare(view, run)
            except PackageProductionError as error:
                return self._status(connection, run_id, 'NOT_COMPLETED', str(error))
            # All required Source bytes already exist in artifact_contents and
            # were checked above. Serialize the actual final report before the
            # completion clock is sampled, never borrow RUN_FINISHED/export time.
            report_bytes = _json(report)
            if len(report_bytes) > 16 * 1024 * 1024:
                return self._status(connection, run_id, 'NOT_COMPLETED', 'PACKAGE_REPORT_TOO_LARGE')
            files['final-report.json'] = report_bytes
            # Report storage precedes the completion sample. An error after
            # this insert rolls it back with the event; there is no partial
            # visible package and no export-time substitute for the timestamp.
            connection.execute('INSERT INTO host_final_reports(run_id,report) VALUES(?,?)',
                               (str(run_id), report_bytes))
            completed_at = self.clock()
            if (not isinstance(completed_at, datetime) or completed_at.utcoffset() is None
                    or completed_at < max(event.occurred_at for event in events)):
                raise PackageProductionError('PACKAGE_CLOCK_INVALID')
            event = TraceEvent(run_id=run_id, event_type='RESULT_PACKAGE_COMPLETED',
                actor='Orchestrator', attempt=run.fix_attempt, code_version=run.code_version,
                snapshot_sha256=report['snapshotSha256'], occurred_at=completed_at,
                workflow_state=run.status)
            evidence = {'schemaVersion': 1, 'producer': 'HOST_AUTOMATIC',
                'eventId': str(event.event_id), 'runId': str(run_id), 'codeVersion': run.code_version,
                'snapshotSha256': report['snapshotSha256'], 'completedAt': event.to_trace_json()['occurredAt'],
                'finalReportPath': 'final-report.json',
                'files': {name: {'sha256': sha256(data).hexdigest(), 'sizeBytes': len(data)}
                          for name, data in files.items()}}
            _insert_event(connection, event)
            connection.execute('INSERT INTO host_result_packages(run_id,event_id,completion) VALUES(?,?,?)',
                               (str(run_id), str(event.event_id), _json(evidence)))
            return self._status(connection, run_id, 'COMPLETED')

    @staticmethod
    def _prepare(view, run):
        with view._connection() as connection:
            events = [TraceEvent.model_validate_json(row[0]) for row in connection.execute(
                'SELECT payload_json FROM trace_events WHERE run_id=? ORDER BY occurred_at,event_id', (str(run.run_id),))]
        if any(event.workflow_state is not None and event.workflow_state.value in ('HUMAN_REVIEW', 'WAITING_INPUT')
               or event.event_type == 'WORKFLOW_RESUMED' for event in events):
            raise PackageProductionError('PACKAGE_OUTSIDE_AUTOMATIC_SEGMENT')
        starts = [event for event in events if event.event_type == 'RUN_STARTED']
        ends = [event for event in events if event.event_type in ('RUN_FINISHED', 'RUN_ABORTED')]
        if (len(starts) != 1 or len(ends) != 1 or starts[0].occurred_at > ends[0].occurred_at
                or ends[0].event_type != ('RUN_ABORTED' if run.status.value == 'ABORTED' else 'RUN_FINISHED')
                or any(event.event_type == 'RESULT_PACKAGE_COMPLETED' for event in events)):
            raise PackageProductionError('PACKAGE_LIFECYCLE_INVALID')
        artifacts = view.list_project_artifacts(run.run_id)
        chosen = {}
        for kind in _KINDS:
            candidates = [item for item in artifacts if item.artifact_type == kind
                          and (kind == 'REQUIREMENT' or item.code_version == run.code_version)]
            if not candidates:
                raise PackageProductionError('PACKAGE_MISSING_' + kind)
            chosen[kind] = max(candidates, key=lambda item: (item.artifact_version, item.created_at))
        try:
            snapshot, = export_snapshots(view, run.run_id)
        except Exception:
            raise PackageProductionError('PACKAGE_SNAPSHOT_UNVERIFIED') from None
        manifest = snapshot.manifest
        for kind in ('BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'):
            if chosen[kind].execution_manifest.model_dump(mode='json', by_alias=True) != manifest:
                raise PackageProductionError('PACKAGE_REPORT_SNAPSHOT_MISMATCH')
        configuration = view.get_run_configuration(run.run_id)
        if configuration is None:
            raise PackageProductionError('PACKAGE_CONFIGURATION_MISSING')
        report = {'schemaVersion': 1, 'artifactType': 'FINAL_REPORT', 'producer': 'HOST_AUTOMATIC',
            'runId': str(run.run_id), 'scenarioId': str(run.scenario_id), 'codeVersion': run.code_version,
            'snapshotSha256': manifest['snapshotSha256'], 'status': run.status.value,
            'verdict': None if run.verdict is None else run.verdict.value,
            'summary': 'Host workflow result; independent product evaluation is recorded separately.',
            'configurationArtifactId': str(configuration.artifact_id),
            'artifacts': {kind: item.model_dump(mode='json', by_alias=True) for kind, item in chosen.items()},
            'issues': [item.model_dump(mode='json', by_alias=True) for item in view.list_issue_records(run.run_id)]}
        files = {f'v{run.code_version}/source.tar': snapshot.archive,
                 f'v{run.code_version}/manifest.json': _json(manifest)}
        return report, files, events

    async def after_dispatch(self, run_id):
        from agents.platform.composition import _host_call
        try:
            return await _host_call(self.complete, run_id)
        except Exception:
            # Preserve the workflow's original outcome. No completion event is
            # committed after a failed transaction; restart retries the producer.
            _LOG.warning('HOST_PACKAGE_WRITE_FAILED')
            try:
                with self.repository._transaction() as connection:
                    if not connection.execute('SELECT 1 FROM host_result_packages WHERE run_id=?', (str(run_id),)).fetchone():
                        self._status(connection, run_id, 'NOT_COMPLETED', 'PACKAGE_WRITE_FAILED')
            except Exception:
                pass  # The database itself may be unavailable; never invent a completion.
            return {'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_WRITE_FAILED'}

    def recover(self):
        """Retry interrupted publication on Host startup; never change a completed receipt."""
        with self.repository._connection() as connection:
            rows = connection.execute('''SELECT r.run_id,l.token,l.owner_pid FROM workflow_runs r
                JOIN host_package_enrollments e ON r.run_id=e.run_id
                LEFT JOIN host_result_packages p ON r.run_id=p.run_id
                LEFT JOIN control_leases l ON r.run_id=l.run_id
                WHERE r.status IN ('FINISHED','ABORTED') AND p.run_id IS NULL''').fetchall()
        recovered = []
        for run_id, token, owner in rows:
            if token is not None:
                if owner is None:
                    continue
                try:
                    os.kill(owner, 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    continue
                else:
                    continue
            try:
                recovered.append(self.complete(UUID(run_id)))
            except Exception:
                _LOG.warning('HOST_PACKAGE_RECOVERY_FAILED')
        return recovered


def install_package_service(platform, repository):
    """Bind the producer to actual dispatch completion and Host restart recovery."""
    from agents.platform.composition import _host_call
    service = HostPackageService(repository)
    platform.dispatcher.set_after_dispatch(service.after_dispatch)
    app = platform.orchestrator_app
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            await _host_call(lambda _: service.recover(), None)
            yield

    app.router.lifespan_context = lifespan
    app.state.package_service = service
    return service
