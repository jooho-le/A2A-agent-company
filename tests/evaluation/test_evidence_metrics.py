import copy

import pytest

from evaluation.aggregation import Status
from evaluation.evidence_metrics import first_passing_snapshot, process_metrics, summarize_process
from tests.evaluation.test_outcomes import report_with
from tests.evaluation.test_run_metrics import uid


def timing_case():
    reports = [copy.deepcopy(report_with({20: Status.FAIL}, run_id=uid(1))),
               copy.deepcopy(report_with({}, run_id=uid(1), code_version=2))]
    events = [{'eventId': uid(500), 'runId': uid(1), 'eventType': 'RUN_STARTED', 'occurredAt': '2026-11-13T00:00:00Z'}]
    for version, report in enumerate(reports, 1):
        manifest = report['expectedManifest']
        events.append({'eventId': uid(500 + version), 'runId': uid(1), 'eventType': 'SNAPSHOT_FROZEN',
                       'occurredAt': f'2026-11-13T00:00:0{version}Z', 'codeVersion': version,
                       'snapshotSha256': manifest['snapshotSha256'], 'outputArtifactIds': [manifest['projectArtifactId']]})
    events.insert(1, {'eventId': uid(510), 'runId': uid(1), 'eventType': 'RUN_FINISHED',
                      'occurredAt': '2026-11-13T00:00:03Z'})
    return {'run': {'runId': uid(1), 'codeVersion': 2, 'status': 'FINISHED'}, 'events': events, 'eventsTotal': 4}, reports


def test_first_pass_uses_snapshot_time_and_keeps_duplicate_delivery_once():
    bundle, reports = timing_case()
    bundle['events'].append(copy.deepcopy(bundle['events'][-1]))
    bundle['eventsTotal'] += 1
    result = first_passing_snapshot(bundle, reports)
    assert result['status'] == 'PASS' and result['firstPassingVersion'] == 2
    assert result['firstPassingSnapshotMs'] == 2000


@pytest.mark.parametrize('damage,reason', [
    ('history', 'SNAPSHOT_HISTORY_INCOMPLETE'), ('hash', 'SNAPSHOT_EVENT_IDENTITY_MISMATCH'),
    ('artifact', 'SNAPSHOT_EVENT_IDENTITY_MISMATCH'), ('time', 'EVENT_TIME_REVERSED'),
    ('missingTime', 'TIMESTAMP_MISSING'), ('count', 'EVENTS_INCOMPLETE'), ('run', 'REPORT_RUN_MISMATCH'),
    ('condition', 'REPORT_CONDITIONS_DIFFER'), ('duplicateVersion', 'DUPLICATE_VERSION_REPORT'),
    ('changedEvent', 'DUPLICATE_RECORD_CHANGED'), ('missingVersion', 'SNAPSHOT_TIME_NOT_UNIQUE'),
])
def test_missing_or_wrong_timing_evidence_never_becomes_zero(damage, reason):
    bundle, reports = timing_case()
    if damage == 'history': reports.pop(0)
    if damage == 'hash': bundle['events'][-1]['snapshotSha256'] = 'f' * 64
    if damage == 'artifact': bundle['events'][-1]['outputArtifactIds'] = [uid(999)]
    if damage == 'time': bundle['events'][-1]['occurredAt'] = '2026-11-12T23:59:59Z'
    if damage == 'missingTime': bundle['events'][-1].pop('occurredAt')
    if damage == 'count': bundle['eventsTotal'] += 1
    if damage == 'run': reports[0]['sourceRunId'] = uid(9)
    if damage == 'condition': reports[0]['suite']['sha256'] = '1' * 64
    if damage == 'duplicateVersion': reports.append(reports[0])
    if damage == 'changedEvent':
        bundle['events'].append({**bundle['events'][-1], 'occurredAt': '2026-11-13T00:00:03Z'})
        bundle['eventsTotal'] += 1
    if damage == 'missingVersion': bundle['events'][-1].pop('codeVersion')
    result = first_passing_snapshot(bundle, reports)
    assert result['status'] == 'UNVERIFIED' and result['reason'] == reason
    assert result['firstPassingSnapshotMs'] is None


def test_unknown_earlier_version_cannot_establish_first_pass():
    bundle, reports = timing_case()
    reports[0] = report_with({20: Status.ERROR}, run_id=uid(1))
    result = first_passing_snapshot(bundle, reports)
    assert result['candidateVersion'] == 2 and result['reason'] == 'EARLIER_PASS_UNVERIFIED'
    assert result['firstPassingVersion'] is None


def test_no_pass_and_unknown_pass_are_different():
    bundle, reports = timing_case()
    reports[1] = report_with({20: Status.FAIL}, run_id=uid(1), code_version=2)
    assert first_passing_snapshot(bundle, reports)['reason'] == 'NO_PASSING_SNAPSHOT'
    reports[1] = report_with({20: Status.ERROR}, run_id=uid(1), code_version=2)
    assert first_passing_snapshot(bundle, reports)['reason'] == 'PASS_EXISTENCE_UNVERIFIED'


def test_snapshot_after_human_boundary_is_not_automatic_first_pass():
    bundle, reports = timing_case()
    bundle['events'].append({'eventId': uid(520), 'runId': uid(1), 'eventType': 'WORKFLOW_STATE_CHANGED',
                             'workflowState': 'HUMAN_REVIEW', 'occurredAt': '2026-11-13T00:00:01Z'})
    bundle['eventsTotal'] += 1
    assert first_passing_snapshot(bundle, reports)['reason'] == 'SNAPSHOT_OUTSIDE_AUTOMATIC_SEGMENT'
    bundle['run']['status'] = 'FIXING'
    assert first_passing_snapshot(bundle, reports)['reason'] == 'RUN_NOT_CLOSED'


def process_case():
    rows = [
        {'kind': 'REPAIR', 'route': 'REPLAN', 'operationId': 'p', 'roundId': 'r1', 'action': 'STARTED'},
        {'kind': 'REPAIR', 'route': 'REPLAN', 'operationId': 'p', 'roundId': 'r1', 'action': 'COMPLETED'},
        {'kind': 'REPAIR', 'route': 'IMPLEMENTATION', 'operationId': 'd', 'roundId': 'r1', 'action': 'STARTED'},
        {'kind': 'RETRY', 'channel': 'A2A', 'operationId': 'send1', 'attempt': 1},
        {'kind': 'RETRY', 'channel': 'LLM', 'operationId': 'call1', 'attempt': 1},
        {'kind': 'GATE_REJECTION', 'stage': 'QA_INPUT', 'operationId': 'qa1', 'rejectionId': 'reject1'},
        {'kind': 'CONFLICT', 'qaReportId': 'qa', 'securityReportId': 'sec', 'reasons': ['QA_PASS_SECURITY_BLOCK']},
        {'kind': 'CONFLICT', 'qaReportId': 'qa', 'securityReportId': 'sec', 'reasons': ['JUDGEMENT_CONTRADICTION']},
    ]
    return {'schemaVersion': 1, 'runId': uid(1), 'completeKinds': ['REPAIR', 'RETRY', 'GATE_REJECTION', 'CONFLICT'],
            'events': [{'eventId': uid(600 + n), 'runId': uid(1), **row} for n, row in enumerate(rows)]}


def test_process_counts_cycles_attempts_completions_and_conflicts_without_duplicates():
    evidence = process_case()
    evidence['events'] += [copy.deepcopy(evidence['events'][3]), {**evidence['events'][3], 'eventId': uid(800)}]
    result = process_metrics(uid(1), evidence)
    assert result['repairs']['roundCount'] == 1
    assert result['repairs']['routes']['IMPLEMENTATION'] == {'attempted': 1, 'completed': 0}
    assert result['repairs']['routes']['REPLAN'] == {'attempted': 1, 'completed': 1}
    assert result['retries']['channels'] == {'TOOL': 0, 'A2A': 1, 'LLM': 1}
    assert result['gateRejections']['count'] == 1
    assert result['conflicts']['count'] == result['conflicts']['contradictions'] == 1


def test_absent_incomplete_and_declared_empty_records_are_distinct():
    assert process_metrics(uid(1))['conflicts']['count'] is None
    evidence = process_case()
    evidence['completeKinds'] = []
    assert process_metrics(uid(1), evidence)['retries']['channels']['LLM'] is None
    evidence['events'] = []
    evidence['completeKinds'] = ['CONFLICT']
    assert process_metrics(uid(1), evidence)['conflicts']['count'] == 0


@pytest.mark.parametrize('damage', ['run', 'firstAttempt', 'changedEvent', 'sameReports', 'route'])
def test_bad_process_records_are_rejected(damage):
    evidence = process_case()
    if damage == 'run': evidence['runId'] = uid(2)
    if damage == 'firstAttempt': evidence['events'][3]['attempt'] = 0
    if damage == 'changedEvent': evidence['events'].append({**evidence['events'][3], 'attempt': 2})
    if damage == 'sameReports': evidence['events'][6]['securityReportId'] = 'qa'
    if damage == 'route': evidence['events'][2]['operationId'] = 'p'
    with pytest.raises(ValueError): process_metrics(uid(1), evidence)


def test_orphan_completion_and_missing_run_do_not_produce_confirmed_totals():
    evidence = process_case()
    evidence['events'].pop(0)
    result = process_metrics(uid(1), evidence)
    assert result['repairs']['roundCount'] is None and result['repairs']['reason'] == 'REPAIR_START_MISSING'
    summary = summarize_process([result], 2)
    assert summary['repairRounds']['total'] is None
    assert summary['LLMRetries'] == {'total': None, 'knownTotal': 1, 'measuredRuns': 1, 'unmeasuredRuns': 1}


def test_retry_history_gap_and_changed_rejection_identity_are_not_counted_as_complete():
    evidence = process_case()
    evidence['events'][3]['attempt'] = 3
    result = process_metrics(uid(1), evidence)
    assert result['retries']['reason'] == 'RETRY_HISTORY_INCOMPLETE'
    assert result['retries']['channels']['A2A'] is None
    evidence = process_case()
    evidence['events'].append({**evidence['events'][5], 'eventId': uid(900), 'stage': 'OTHER_STAGE'})
    with pytest.raises(ValueError, match='IDENTITY_CHANGED'): process_metrics(uid(1), evidence)
