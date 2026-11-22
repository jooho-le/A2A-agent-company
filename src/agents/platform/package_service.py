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
from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS, MAX_CONSECUTIVE_SAME_ISSUE_REPEATS
from orchestrator.domain.repair_progress import final_rechecks
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
            connection.execute('''CREATE TABLE IF NOT EXISTS host_boundary_reports (
                run_id TEXT NOT NULL, boundary_key TEXT NOT NULL, report BLOB NOT NULL,
                PRIMARY KEY(run_id,boundary_key))''')
            for operation in ('UPDATE', 'DELETE'):
                connection.execute(f'''CREATE TRIGGER IF NOT EXISTS boundary_no_{operation.lower()}
                    BEFORE {operation} ON host_boundary_reports
                    BEGIN SELECT RAISE(ABORT, 'immutable boundary report'); END''')
            connection.execute('''CREATE TRIGGER IF NOT EXISTS boundary_no_replace
                BEFORE INSERT ON host_boundary_reports
                WHEN EXISTS(SELECT 1 FROM host_boundary_reports
                    WHERE run_id=NEW.run_id AND boundary_key=NEW.boundary_key)
                BEGIN SELECT RAISE(ABORT, 'immutable boundary report'); END''')
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
                if run.status.value in ('HUMAN_REVIEW', 'WAITING_INPUT'):
                    self._boundary(connection, view, run)
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
    def _boundary(connection, view, run):
        from agents.platform.export_readonly import ReadOnlyTelemetryStore
        key = sha256(_json({'runId': str(run.run_id), 'status': run.status.value,
                           'updatedAt': run.updated_at.isoformat()})).hexdigest()
        if connection.execute('SELECT 1 FROM host_boundary_reports WHERE run_id=? AND boundary_key=?',
                              (str(run.run_id), key)).fetchone():
            return
        artifacts = view.list_project_artifacts(run.run_id)
        sources = [a for a in artifacts if a.artifact_type == 'SOURCE']
        retained = max(sources, key=lambda a: (a.code_version, a.artifact_version), default=None)
        retained_version = None if retained is None else retained.code_version
        current = [a for a in artifacts if a.artifact_type == 'REQUIREMENT'
                   or retained_version is not None and getattr(a, 'code_version', None) == retained_version]
        events = [json.loads(row[0]) for row in connection.execute(
            'SELECT payload_json FROM trace_events WHERE run_id=? ORDER BY occurred_at,event_id', (str(run.run_id),))]
        try:
            usage = ReadOnlyTelemetryStore(view).usage_summary(run.run_id)
        except Exception:
            usage = {'usageComplete': False, 'totalTokens': None, 'costUsd': None}
        report = {'schemaVersion': 1, 'kind': 'AUTOMATIC_BOUNDARY_REPORT', 'runId': str(run.run_id),
                  'boundaryKey': key, 'status': run.status.value, 'completed': False,
                  'verdict': None if run.verdict is None else run.verdict.value,
                  'codeVersion': run.code_version, 'fixAttempt': run.fix_attempt,
                  'retainedCodeVersion': retained_version,
                  'retainedSnapshotSha256': None if retained is None else retained.snapshot_sha256,
                  'resumeState': None if run.resume_state is None else run.resume_state.value,
                  'observedAt': run.updated_at.isoformat(), 'usage': usage,
                  'steps': [s.model_dump(mode='json', by_alias=True) for s in view.list_steps(run.run_id)],
                  'artifacts': [a.model_dump(mode='json', by_alias=True) for a in current],
                  'missingArtifacts': [k for k in _KINDS if not any(a.artifact_type == k for a in current)],
                  'events': events}
        data = _json(report)
        if len(data) > 16 * 1024 * 1024:
            raise PackageProductionError('BOUNDARY_REPORT_TOO_LARGE')
        connection.execute('INSERT INTO host_boundary_reports VALUES(?,?,?)',
                           (str(run.run_id), key, data))

    def boundary_reports(self, run_id):
        with self.repository._connection() as connection:
            return [json.loads(row[0]) for row in connection.execute(
                'SELECT report FROM host_boundary_reports WHERE run_id=? ORDER BY rowid', (str(run_id),))]

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
        missing_validation = []
        # Host-proven closes that keep the last candidate (docs/live-api/05 8·14절): when its build
        # failed, the validation reports were never produced and are declared, not invented.
        closing_stops = ('DEVELOPMENT_NO_PROGRESS_EXHAUSTED', 'DEVELOPMENT_CORRECTIONS_EXHAUSTED',
                         'DEVELOPMENT_REJECTION_EXHAUSTED',
                         'SAME_ISSUE_REPEAT_LIMIT_REACHED')
        no_progress = any(e.event_type in closing_stops for e in events)
        qa_stops = [e for e in events if e.event_type == 'QA_DRAFT_CORRECTIONS_EXHAUSTED'
                    and e.code_version == run.code_version]
        security_stops = [e for e in events if e.event_type == 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'
                          and e.code_version == run.code_version]
        draft_stops = {'QA_REPORT': qa_stops, 'SECURITY_REPORT': security_stops}
        missing_events = {
            kind: next((e for e in reversed(events) if e.event_type == role + '_MODEL_VALIDATION_STOP'
                        and e.code_version == run.code_version), None)
            for kind, role in (('QA_REPORT', 'QA'), ('SECURITY_REPORT', 'SECURITY'))
        }
        # docs/live-api/05 22절 일반 종료 규칙: a model-behavior stop closed with the last measured candidate.
        from orchestrator.infrastructure.automatic_stop import EVENT as AUTOMATIC_STOP, stop_record
        with view._connection() as connection:
            automatic = stop_record(connection, run.run_id)
        if automatic is not None and (automatic['retainedCodeVersion'] != run.code_version or run.verdict is None
                                      or automatic['verdict'] != run.verdict.value
                                      or not any(e.event_type == AUTOMATIC_STOP and str(e.event_id) == automatic['eventId']
                                                 for e in events)):
            raise PackageProductionError('PACKAGE_AUTOMATIC_STOP_UNVERIFIED')
        for kind in _KINDS:
            candidates = [item for item in artifacts if item.artifact_type == kind
                          and (kind == 'REQUIREMENT' or item.code_version == run.code_version)]
            if not candidates:
                build = chosen.get('BUILD_REPORT')
                stops = draft_stops.get(kind, [])
                missing_event = stops[0] if len(stops) == 1 else missing_events.get(kind)
                if (missing_event is not None and run.verdict is not None
                        and run.verdict.value in ('FAIL', 'UNVERIFIED') and build is not None and build.passed
                        and missing_event.snapshot_sha256 == build.execution_manifest.snapshot_sha256):
                    missing_validation.append(kind)
                    continue
                if (automatic is not None and kind in ('QA_REPORT', 'SECURITY_REPORT') and build is not None
                        and run.verdict.value in ('FAIL', 'UNVERIFIED')
                        and (build.passed or (build.execution_outcome.value == 'FAIL'
                                              and build.failure_kind == 'PRODUCT'))):
                    missing_validation.append(kind)  # declared below, never invented
                    continue
                if (no_progress and run.verdict is not None and run.verdict.value == 'FAIL'
                        and kind in ('QA_REPORT', 'SECURITY_REPORT') and build is not None
                        and build.execution_outcome.value == 'FAIL' and build.failure_kind == 'PRODUCT'):
                    missing_validation.append(kind)
                    continue
                raise PackageProductionError('PACKAGE_MISSING_' + kind)
            chosen[kind] = max(candidates, key=lambda item: (item.artifact_version, item.created_at))
        try:
            snapshot, = export_snapshots(view, run.run_id)
        except Exception:
            raise PackageProductionError('PACKAGE_SNAPSHOT_UNVERIFIED') from None
        manifest = snapshot.manifest
        for kind in ('BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'):
            if kind in missing_validation:
                continue
            if chosen[kind].execution_manifest.model_dump(mode='json', by_alias=True) != manifest:
                raise PackageProductionError('PACKAGE_REPORT_SNAPSHOT_MISMATCH')
        configuration = view.get_run_configuration(run.run_id)
        if configuration is None:
            raise PackageProductionError('PACKAGE_CONFIGURATION_MISSING')
        issues = view.list_issue_records(run.run_id)
        report = {'schemaVersion': 1, 'artifactType': 'FINAL_REPORT', 'producer': 'HOST_AUTOMATIC',
            'runId': str(run.run_id), 'scenarioId': str(run.scenario_id), 'codeVersion': run.code_version,
            'snapshotSha256': manifest['snapshotSha256'], 'status': run.status.value,
            'verdict': None if run.verdict is None else run.verdict.value,
            'summary': 'Host workflow result; independent product evaluation is recorded separately.',
            'configurationArtifactId': str(configuration.artifact_id),
            'artifacts': {kind: item.model_dump(mode='json', by_alias=True) for kind, item in chosen.items()},
            'issues': [item.model_dump(mode='json', by_alias=True) for item in issues]}
        for event_type, key in (('GENERATED_RECHECK_UNRESOLVED', 'unresolvedGeneratedRechecks'),
                                ('PROTECTED_RECHECK_UNRESOLVED', 'unresolvedProtectedRechecks')):
            outstanding = [event for event in events if event.event_type == event_type
                           and event.code_version == run.code_version and event.snapshot_sha256 == manifest['snapshotSha256']]
            if outstanding:
                report[key] = [{'issueId': str(event.issue_id), 'eventId': str(event.event_id)}
                               for event in outstanding]
        if missing_validation:
            build = chosen['BUILD_REPORT']

            def declared(kind):
                if draft_stops.get(kind):
                    return {'status': 'UNVERIFIED', 'reason': draft_stops[kind][0].event_type}
                if missing_events.get(kind) is not None:
                    return {'status': 'UNVERIFIED', 'reason': missing_events[kind].event_type}
                if automatic is not None and build.passed:
                    # The validator stopped (or its report was not registered) after the build passed.
                    return {'status': 'UNVERIFIED', 'reason': AUTOMATIC_STOP}
                return {'status': 'NOT_RUN', 'reason': 'BUILD_FAILED'}
            report['unexecutedValidation'] = {kind: declared(kind) for kind in missing_validation}
        # QA alone keeps Codex's original shape; a Security stop uses the same shape with its own
        # produced flag, and when both stopped every entry is listed in validationStops.
        entries = []
        for kind, flag in (('QA_REPORT', 'qaReportProduced'), ('SECURITY_REPORT', 'securityReportProduced')):
            stop = draft_stops[kind][0] if draft_stops[kind] else missing_events.get(kind)
            if stop:
                reason = next((e.event_type.removeprefix('VALIDATOR_STOP_') for e in events
                               if e.workflow_step_id == stop.workflow_step_id and e.event_type.startswith('VALIDATOR_STOP_')), None)
                entries.append({'reason': stop.event_type, **({'stopCode': reason} if reason else {}),
                    'eventId': str(stop.event_id), 'workflowStepId': str(stop.workflow_step_id),
                    'retainedCodeVersion': run.code_version, 'retainedSnapshotSha256': manifest['snapshotSha256'],
                    'requirementIds': [str(i) for i in stop.requirement_ids],
                    'status': 'UNVERIFIED', flag: False,
                    'failureTraceEventIds': [str(e.event_id) for e in events
                        if e.workflow_step_id == stop.workflow_step_id and e.event_type == 'LLM_RECOVERY_DECIDED'],
                    'testExecution': 'See preserved Host tool receipts; a missing report does not mean no tests ran.'})
        if entries:
            report['validationStop'] = entries[0]
        if len(entries) > 1:
            report['validationStops'] = entries
        stops = [event for event in events if event.event_type in closing_stops]
        if stops:
            report['developmentStop'] = {
                'reason': stops[-1].event_type, 'eventId': str(stops[-1].event_id),
                'workflowStepId': None if stops[-1].workflow_step_id is None else str(stops[-1].workflow_step_id),
                'retainedCodeVersion': run.code_version,
                'retainedSnapshotSha256': manifest['snapshotSha256'],
                'newCandidatePublished': False,
            }
        if automatic is not None:
            report['automaticStop'] = {**automatic, 'reason': AUTOMATIC_STOP,
                'retainedSnapshotSha256': manifest['snapshotSha256'], 'newCandidatePublished': False,
                'note': 'Stopped for a model reason; the verdict comes only from Host-measured results.'}
        if run.fix_attempt:
            # Cite the decision that actually closed this run. Reaching a counter alone
            # does not establish the cause, especially when validation or a tool stopped.
            reasons = {*closing_stops, AUTOMATIC_STOP, 'VALIDATION_DECISION_FINISHED',
                       'BUILD_FAILED_FIX_LIMIT_REACHED', 'BUILD_UNVERIFIED',
                       'QA_DRAFT_CORRECTIONS_EXHAUSTED', 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'}
            decision = next((event for event in reversed(events)
                             if event.code_version == run.code_version and event.workflow_state == run.status
                             and event.event_type in reasons), ends[0])
            final_reports = {str(chosen[kind].artifact_id) for kind in ('QA_REPORT', 'SECURITY_REPORT', 'BUILD_REPORT')
                             if kind in chosen}
            protected_rows = {}
            review = chosen['CHANGE_REPORT'].ready_review
            if review and any(check.prior_protected_receipt_id for check in review.rechecks):
                from orchestrator.infrastructure.protected_recheck import current_protected_observation
                requested = {check.issue_id for check in review.rechecks if check.prior_protected_receipt_id}
                with view._connection() as connection:
                    for issue in issues:
                        if issue.issue_id in requested and issue.protected_receipt is not None:
                            protected_rows[issue.issue_id] = current_protected_observation(connection, issue, chosen['SOURCE'])
            from mcp_tools.tools.generated_recheck import measured_row
            with view._connection() as connection:
                rechecks = final_rechecks(review, chosen['SOURCE'], artifacts, chosen, configuration,
                    issues=issues, protected_rows=protected_rows,
                    generated_lookup=lambda issue, source, row: measured_row(connection, issue, source, row))
            report['repairOutcome'] = {
                'codeFixesUsed': run.fix_attempt, 'maxCodeFixAttempts': MAX_CODE_FIX_ATTEMPTS,
                'sameIssueRepeatLimit': MAX_CONSECUTIVE_SAME_ISSUE_REPEATS,
                'decisionEvent': decision.event_type, 'decisionEventId': str(decision.event_id),
                'triggerIssueId': str(decision.issue_id) if decision.issue_id else None,
                'currentIssueIds': [issue['issueId'] for issue in report['issues']
                                    if issue['sourceArtifactId'] == manifest['projectArtifactId']
                                    and (issue.get('reportArtifactId') in final_reports or issue.get('protectedReceipt'))],
                'rechecks': rechecks,
            }
        # Reasons are model proposals, beside (never promoted into) Host routing facts.
        with view._connection() as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runtime_events'").fetchone():
                rows = connection.execute("SELECT t.event_id,r.payload_json FROM agent_runtime_events r JOIN trace_events t "
                    "ON r.trace_event_id=t.event_id AND r.run_id=t.run_id "
                    "WHERE r.run_id=? AND t.event_type='LLM_RECOVERY_DECIDED' ORDER BY r.sequence",
                    (str(run.run_id),)).fetchall()
                from agents.llm.recovery import FailureRoute
                rejections = []
                stop = report.get('developmentStop', {})
                for event_id, raw in rows:
                    item = json.loads(raw)
                    recovery = item.get('detail', {}).get('recovery', {})
                    if (stop.get('reason') == 'DEVELOPMENT_CORRECTIONS_EXHAUSTED'
                            and recovery.get('code') in ('READY_RUNTIME_REPAIR_REQUIRED', 'READY_LIBRARY_TYPE_MISMATCH')
                            and recovery.get('action') == 'STOP'
                            and item['binding'].get('workflowStepId') == stop['workflowStepId']
                            and item['binding'].get('snapshotSha256') == stop['retainedSnapshotSha256']):
                        route = FailureRoute.model_validate(recovery)
                        stop.update(correctionCode=route.code, recoveryEventId=event_id)
                        if route.code == 'READY_RUNTIME_REPAIR_REQUIRED':
                            stop['unresolvedPaths'] = list(route.unlinked_repair_paths)
                    if recovery.get('developmentRejection') is not None:
                        route = FailureRoute.model_validate(recovery)
                        rejections.append({'eventId': item['binding']['eventId'],
                            'workflowStepId': item['binding']['workflowStepId'], **route.public()})
                if rejections:
                    report['developmentRejections'] = rejections
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
                WHERE r.status IN ('FINISHED','ABORTED','HUMAN_REVIEW','WAITING_INPUT') AND p.run_id IS NULL''').fetchall()
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
