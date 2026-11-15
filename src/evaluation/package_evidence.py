"""TIME-02 intake contract. Export time is never a package completion timestamp."""
from .evidence_metrics import _time, _unique


def package_assessment(export):
    result = {'status': 'UNVERIFIED', 'reason': 'PACKAGE_COMPLETION_EVENT_NOT_RECORDED', 'packageMs': None}
    evidence = export.get('packageCompletion')
    if evidence is None:
        return result
    try:
        if not isinstance(evidence, dict):
            raise ValueError('PACKAGE_EVIDENCE_INVALID')
        bundle, handoff = export['bundle'], export['handoff']
        run, files = bundle['run'], handoff['files']
        production = handoff.get('packageProduction')
        if production is not None:
            if (not isinstance(production, dict) or production.get('runId') != run['runId']
                    or production.get('status') not in ('NOT_CONFIGURED', 'PENDING', 'COMPLETED', 'NOT_COMPLETED', 'ERROR')):
                raise ValueError('PACKAGE_PRODUCTION_STATUS_INVALID')
            if production['status'] not in ('NOT_CONFIGURED', 'COMPLETED'):
                raise ValueError('PACKAGE_PRODUCTION_' + production['status'])
            if production['status'] == 'COMPLETED' and (
                    production.get('eventId') != evidence.get('eventId')
                    or production.get('completedAt') != evidence.get('completedAt')):
                raise ValueError('PACKAGE_PRODUCTION_STATUS_MISMATCH')
        if (type(evidence.get('schemaVersion')) is not int or evidence['schemaVersion'] != 1
                or type(evidence.get('codeVersion')) is not int
                or evidence.get('runId') != run['runId'] or evidence.get('codeVersion') != run['codeVersion']):
            raise ValueError('PACKAGE_IDENTITY_MISMATCH')
        if export.get('packageFilesVerified') is not True:
            raise ValueError('PACKAGE_FILES_NOT_VERIFIED')
        version = run['codeVersion']
        report_path = evidence['finalReportPath']
        entries = evidence['files']
        required = {f'v{version}/source.tar', f'v{version}/manifest.json', report_path}
        if (not isinstance(report_path, str) or not report_path
                or len(required) != 3 or not isinstance(entries, dict) or not required <= entries.keys()
                or any(files.get(name) != detail for name, detail in entries.items())):
            raise ValueError('PACKAGE_FILES_MISMATCH')
        if not all(isinstance(detail, dict) and type(detail.get('sizeBytes')) is int and detail['sizeBytes'] > 0 for detail in entries.values()):
            raise ValueError('PACKAGE_FILES_EMPTY')
        # The source bytes and manifest were checked by load_handoff; bind completion to that same snapshot.
        manifest = export['packageManifest']
        from orchestrator.domain.snapshot_handoff import ExecutionManifest, CodeSnapshotArtifact
        ExecutionManifest.model_validate(manifest)
        sources = [item for item in export['trace'].get('artifacts', []) if item.get('artifactType') == 'SOURCE'
                   and item.get('artifactId') == manifest['projectArtifactId']]
        if (len(sources) != 1 or sources[0].get('runId') != run['runId']
                or sources[0].get('snapshotSha256') != evidence['snapshotSha256']
                or manifest['codeVersion'] != version
                or manifest['snapshotSha256'] != evidence['snapshotSha256']):
            raise ValueError('PACKAGE_SNAPSHOT_MISMATCH')
        if (CodeSnapshotArtifact.model_validate(sources[0]).execution_manifest().model_dump(mode='json', by_alias=True) != manifest
                or files[f'v{version}/source.tar']['sha256'] != manifest['snapshotSha256']):
            raise ValueError('PACKAGE_SNAPSHOT_MISMATCH')
        final_report = export['packageFinalReport']
        if (not isinstance(final_report, dict) or final_report.get('artifactType') != 'FINAL_REPORT' or final_report.get('runId') != run['runId'] or final_report.get('codeVersion') != version
                or final_report.get('snapshotSha256') != evidence['snapshotSha256']
                or final_report.get('verdict') != run['verdict']):
            raise ValueError('PACKAGE_REPORT_MISMATCH')
        if bundle.get('snapshotConsistent') is not True or bundle.get('eventsTotal') != len(bundle['events']):
            raise ValueError('PACKAGE_EVENTS_INCOMPLETE')
        events = _unique(bundle['events'], 'eventId')
        if any(row.get('runId') != run['runId'] for row in events):
            raise ValueError('PACKAGE_EVENT_RUN_MISMATCH')
        starts = [row for row in events if row.get('eventType') == 'RUN_STARTED']
        end_kind = 'RUN_ABORTED' if run['status'] == 'ABORTED' else 'RUN_FINISHED'
        ends = [row for row in events if row.get('eventType') in ('RUN_FINISHED', 'RUN_ABORTED')]
        completions = [row for row in events if row.get('eventType') == 'RESULT_PACKAGE_COMPLETED']
        if len(starts) != 1 or len(ends) != 1 or len(completions) != 1 or run['status'] not in ('FINISHED', 'ABORTED'):
            raise ValueError('PACKAGE_COMPLETION_EVENT_NOT_UNIQUE')
        if ends[0]['eventType'] != end_kind:
            raise ValueError('PACKAGE_COMPLETION_EVENT_MISMATCH')
        completed = completions[0]
        if (completed['eventId'] != evidence['eventId'] or completed.get('actor') != 'Orchestrator'
                or completed.get('codeVersion') != version or completed.get('snapshotSha256') != evidence['snapshotSha256']
                or _time(completed['occurredAt']) != _time(evidence['completedAt'])):
            raise ValueError('PACKAGE_COMPLETION_EVENT_MISMATCH')
        start, end, finish = _time(starts[0]['occurredAt']), _time(ends[0]['occurredAt']), _time(completed['occurredAt'])
        if not start <= end <= finish:
            raise ValueError('PACKAGE_EVENT_TIME_REVERSED')
        if evidence.get('producer') != 'HOST_AUTOMATIC' or any(
                row.get('workflowState') in ('HUMAN_REVIEW', 'WAITING_INPUT')
                or row.get('eventType') == 'WORKFLOW_RESUMED' for row in events):
            raise ValueError('PACKAGE_OUTSIDE_AUTOMATIC_SEGMENT')
        return {'status': 'PASS', 'reason': None, 'packageMs': int((finish - start).total_seconds() * 1000),
                'eventId': completed['eventId'], 'completedAt': evidence['completedAt'],
                'scope': 'HASH_CHECKED_HOST_DECLARATION'}
    except (KeyError, TypeError, ValueError) as error:
        reason = str(error) if type(error) is ValueError and str(error).startswith('PACKAGE_') else 'PACKAGE_EVIDENCE_INVALID'
        return {**result, 'reason': reason}
