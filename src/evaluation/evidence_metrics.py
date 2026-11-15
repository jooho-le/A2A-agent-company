"""기록으로 증명할 수 있는 TIME-05 및 과정 계수. 누락 기록은 0으로 바꾸지 않는다."""
from datetime import datetime

from .reporting import validate_contract


def _time(value):
    if not isinstance(value, str):
        raise ValueError('TIMESTAMP_MISSING')
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.utcoffset() is None:
        raise ValueError('TIMESTAMP_NOT_AWARE')
    return moment


def _unique(rows, key):
    found = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('RECORD_INVALID')
        identity = row.get(key)
        if not isinstance(identity, str) or not identity:
            raise ValueError('RECORD_ID_MISSING')
        if identity in found and found[identity] != row:
            raise ValueError('DUPLICATE_RECORD_CHANGED')
        found[identity] = row
    return list(found.values())


def first_passing_snapshot(bundle, reports):
    """Run 종료 후 버전별 채점으로 최초 통과 버전의 고정 시각을 구한다. 채점 실행 시간은 포함하지 않는다."""
    run = (bundle or {}).get('run') or {}
    result = {'runId': run.get('runId'), 'status': 'UNVERIFIED', 'reason': None,
              'firstPassingVersion': None, 'candidateVersion': None, 'firstPassingSnapshotMs': None}
    try:
        final = run.get('codeVersion')
        if type(final) is not int or not 1 <= final <= 4:
            raise ValueError('NO_SNAPSHOT')
        if run.get('status') not in ('FINISHED', 'ABORTED', 'HUMAN_REVIEW', 'WAITING_INPUT'):
            raise ValueError('RUN_NOT_CLOSED')
        by_version = {}
        for report in reports:
            try:
                validate_contract(report, 'independent_evaluation')
            except Exception:
                raise ValueError('REPORT_INVALID') from None
            manifest = report['expectedManifest']
            version = manifest['codeVersion']
            if report['sourceRunId'] != run['runId']:
                raise ValueError('REPORT_RUN_MISMATCH')
            if version in by_version:
                raise ValueError('DUPLICATE_VERSION_REPORT')
            by_version[version] = report
        if set(by_version) != set(range(1, final + 1)):
            raise ValueError('SNAPSHOT_HISTORY_INCOMPLETE')
        reference = by_version[final]
        for report in reports:
            if (report['suite'] != reference['suite'] or report['architecture'] != reference['architecture']
                    or any(report['expectedManifest'][key] != reference['expectedManifest'][key]
                           for key in ('repositoryId', 'containerImageDigest', 'dependencyLockHash'))):
                raise ValueError('REPORT_CONDITIONS_DIFFER')
            if not report['manifestCheck']['metadataMatches'] or report['manifestCheck']['errorCode']:
                raise ValueError('SNAPSHOT_IDENTITY_UNVERIFIED')
        passed = [v for v in sorted(by_version) if by_version[v]['evaluationVerdict'] == 'PASS']
        if not passed:
            unknown = any(r['evaluationVerdict'] == 'UNVERIFIED' and not r['confirmedFailureIds'] for r in reports)
            return {**result, 'status': 'UNVERIFIED' if unknown else 'PASS',
                    'reason': 'PASS_EXISTENCE_UNVERIFIED' if unknown else 'NO_PASSING_SNAPSHOT'}
        candidate = passed[0]
        result['candidateVersion'] = candidate
        if any(by_version[v]['evaluationVerdict'] == 'UNVERIFIED' and not by_version[v]['confirmedFailureIds']
               for v in range(1, candidate)):
            raise ValueError('EARLIER_PASS_UNVERIFIED')
        events = (bundle or {}).get('events')
        if not isinstance(events, list) or bundle.get('eventsTotal') != len(events) or bundle.get('snapshotConsistent') is False:
            raise ValueError('EVENTS_INCOMPLETE')
        events = _unique(events, 'eventId')
        if any(e.get('runId') != run['runId'] for e in events):
            raise ValueError('EVENT_RUN_MISMATCH')
        starts = [e for e in events if e.get('eventType') == 'RUN_STARTED']
        if len(starts) != 1:
            raise ValueError('RUN_START_NOT_UNIQUE')
        start = _time(starts[0].get('occurredAt'))
        boundaries = [_time(e.get('occurredAt')) for e in events
                      if e.get('eventType') in ('RUN_FINISHED', 'RUN_ABORTED')
                      or e.get('workflowState') in ('HUMAN_REVIEW', 'WAITING_INPUT')]
        if not boundaries:
            raise ValueError('AUTOMATIC_SEGMENT_END_MISSING')
        times = []
        for version in range(1, candidate + 1):
            frozen = [e for e in events if e.get('eventType') == 'SNAPSHOT_FROZEN' and e.get('codeVersion') == version]
            if len(frozen) != 1:
                raise ValueError('SNAPSHOT_TIME_NOT_UNIQUE')
            event, manifest = frozen[0], by_version[version]['expectedManifest']
            if (event.get('snapshotSha256') != manifest['snapshotSha256']
                    or manifest['projectArtifactId'] not in (event.get('outputArtifactIds') or [])):
                raise ValueError('SNAPSHOT_EVENT_IDENTITY_MISMATCH')
            times.append(_time(event.get('occurredAt')))
        if times != sorted(times) or any(moment < start for moment in times):
            raise ValueError('EVENT_TIME_REVERSED')
        if times[-1] > min(boundaries):
            raise ValueError('SNAPSHOT_OUTSIDE_AUTOMATIC_SEGMENT')
        return {**result, 'status': 'PASS', 'firstPassingVersion': candidate,
                'firstPassingSnapshotMs': int((times[-1] - start).total_seconds() * 1000)}
    except (ValueError, KeyError, TypeError) as error:
        return {**result, 'reason': str(error) if isinstance(error, ValueError) else 'METRIC_INPUT_INVALID'}


PROCESS_KINDS = ('REPAIR', 'RETRY', 'GATE_REJECTION', 'CONFLICT')
ROUTES = ('IMPLEMENTATION', 'REPLAN', 'TEST_CHANGE')
CHANNELS = ('TOOL', 'A2A', 'LLM')
CONFLICT_REASONS = ('QA_PASS_SECURITY_BLOCK', 'VERSION_MISMATCH', 'JUDGEMENT_CONTRADICTION')


def process_metrics(run_id, evidence=None):
    """추가 계측 계약 v1. completeKinds는 수집자가 전체를 제공했다고 명시한 종류다(서명 인증이 아님).

    Host 원본에서 증명 가능한 항목은 변환기가 공급한다. 없는 종류는 별도 추가 기록이 필요하다.
    최초 시도는 RETRY가 아니며 attempt=1부터 첫 재시도다. 재계획과 후속 구현은 같은 roundId를 쓴다.
    """
    result = {'schemaVersion': 1, 'runId': run_id, 'coverage': 'DECLARED_RECORDS',
              'repairs': {'status': 'UNVERIFIED', 'reason': 'PROCESS_RECORDS_MISSING', 'roundCount': None, 'routes': {}},
              'retries': {'status': 'UNVERIFIED', 'reason': 'PROCESS_RECORDS_MISSING', 'channels': {}},
              'gateRejections': {'status': 'UNVERIFIED', 'reason': 'PROCESS_RECORDS_MISSING', 'count': None, 'stages': {}},
              'conflicts': {'status': 'UNVERIFIED', 'reason': 'PROCESS_RECORDS_MISSING', 'count': None, 'contradictions': None}}
    if evidence is None:
        return result
    if (not isinstance(evidence, dict) or type(evidence.get('schemaVersion')) is not int or evidence['schemaVersion'] != 1 or evidence.get('runId') != run_id
            or not isinstance(evidence.get('completeKinds'), list) or not isinstance(evidence.get('events'), list)
            or any(k not in PROCESS_KINDS for k in evidence['completeKinds'])):
        raise ValueError('PROCESS_EVIDENCE_INVALID')
    rows = _unique(evidence['events'], 'eventId')
    for row in rows:
        if row.get('runId') != run_id or row.get('kind') not in PROCESS_KINDS:
            raise ValueError('PROCESS_RECORD_INVALID')
    complete = set(evidence['completeKinds'])
    if any(not isinstance(evidence.get(key, []), list) or any(not isinstance(v, str) for v in evidence.get(key, []))
           for key in ('completeRoutes', 'completeChannels')):
        raise ValueError('PROCESS_COVERAGE_INVALID')
    routes_complete = set(ROUTES) if 'REPAIR' in complete else set(evidence.get('completeRoutes', []))
    channels_complete = set(CHANNELS) if 'RETRY' in complete else set(evidence.get('completeChannels', []))
    if not routes_complete <= set(ROUTES) or not channels_complete <= set(CHANNELS):
        raise ValueError('PROCESS_COVERAGE_INVALID')
    result['coverage'] = evidence.get('source', 'DECLARED_RECORDS')
    result['limitations'] = evidence.get('limitations', [])

    def label(kind):
        return {'status': 'PASS' if kind in complete else 'UNVERIFIED',
                'reason': None if kind in complete else 'PROCESS_RECORDS_INCOMPLETE'}

    def required(row, key):
        value = row.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError('PROCESS_FIELD_MISSING_' + key)
        return value

    repairs, retries, gates, conflicts = {}, set(), {}, {}
    for row in rows:
        kind = row['kind']
        if kind == 'REPAIR':
            route, action = row.get('route'), row.get('action')
            if route not in ROUTES or action not in ('STARTED', 'COMPLETED'):
                raise ValueError('REPAIR_RECORD_INVALID')
            key = required(row, 'operationId')
            identity = (route, required(row, 'roundId'))
            previous = repairs.setdefault(key, {'identity': identity, 'actions': set()})
            if previous['identity'] != identity:
                raise ValueError('REPAIR_IDENTITY_CHANGED')
            previous['actions'].add(action)
        elif kind == 'RETRY':
            if row.get('channel') not in CHANNELS or type(row.get('attempt')) is not int or row['attempt'] < 1:
                raise ValueError('RETRY_RECORD_INVALID')
            retries.add((row['channel'], required(row, 'operationId'), row['attempt']))
        elif kind == 'GATE_REJECTION':
            key = required(row, 'rejectionId')
            identity = (required(row, 'stage'), required(row, 'operationId'))
            if key in gates and gates[key] != identity:
                raise ValueError('GATE_REJECTION_IDENTITY_CHANGED')
            gates[key] = identity
        else:
            reasons = row.get('reasons')
            if not isinstance(reasons, list) or not reasons or any(r not in CONFLICT_REASONS for r in reasons):
                raise ValueError('CONFLICT_RECORD_INVALID')
            pair = (required(row, 'qaReportId'), required(row, 'securityReportId'))
            if pair[0] == pair[1]:
                raise ValueError('CONFLICT_REPORT_PAIR_INVALID')
            conflicts.setdefault(pair, set()).update(reasons)
    orphan_routes = {item['identity'][0] for item in repairs.values()
                     if 'COMPLETED' in item['actions'] and 'STARTED' not in item['actions']}
    orphan = bool(orphan_routes)
    repair_known = routes_complete == set(ROUTES) and not orphan
    result['repairs'] = {**label('REPAIR'), 'roundCount': len({r['identity'][1] for r in repairs.values()
                        if 'STARTED' in r['actions']}) if repair_known else None,
                        'routes': {route: {name: sum(r['identity'][0] == route and action in r['actions']
                        for r in repairs.values()) if route in routes_complete and route not in orphan_routes else None
                        for name, action in (('attempted', 'STARTED'), ('completed', 'COMPLETED'))} for route in ROUTES}}
    if orphan:
        result['repairs'].update(status='UNVERIFIED', reason='REPAIR_START_MISSING')
    result['repairs']['routeCoverage'] = {route: route in routes_complete and route not in orphan_routes for route in ROUTES}
    retry_attempts = {}
    for channel, operation, attempt in retries:
        retry_attempts.setdefault((channel, operation), set()).add(attempt)
    gap_channels = {key[0] for key, attempts in retry_attempts.items() if min(attempts) != 1 or max(attempts) != len(attempts)}
    gaps = bool(gap_channels)
    result['retries'] = {**label('RETRY'), 'channels': {c: sum(r[0] == c for r in retries) if c in channels_complete and c not in gap_channels else None
                                                    for c in CHANNELS}}
    if gaps:
        result['retries'].update(status='UNVERIFIED', reason='RETRY_HISTORY_INCOMPLETE')
    result['retries']['channelCoverage'] = {c: c in channels_complete and c not in gap_channels for c in CHANNELS}
    result['gateRejections'] = {**label('GATE_REJECTION'), 'count': len(gates) if 'GATE_REJECTION' in complete else None,
                               'stages': {stage: sum(value[0] == stage for value in gates.values())
                                          for stage in sorted({value[0] for value in gates.values()})} if 'GATE_REJECTION' in complete else {}}
    result['conflicts'] = {**label('CONFLICT'), 'count': len(conflicts) if 'CONFLICT' in complete else None,
                          'contradictions': sum('JUDGEMENT_CONTRADICTION' in reasons for reasons in conflicts.values())
                          if 'CONFLICT' in complete and evidence.get('contradictionsComplete', True) is True else None}
    return result


def summarize_process(records, planned):
    """계획된 모든 Run이 해당 종류를 완전히 제공했을 때만 확정 합계를 낸다."""
    result = {}
    selectors = {
        'repairRounds': ('repairs', lambda r: r['roundCount']),
        'gateRejections': ('gateRejections', lambda r: r['count']),
        'conflicts': ('conflicts', lambda r: r['count']),
        'contradictions': ('conflicts', lambda r: r['contradictions']),
        **{route + action: ('repairs', lambda r, route=route, action=action: r['routes'][route][action])
           for route in ROUTES for action in ('attempted', 'completed')},
        **{channel + 'Retries': ('retries', lambda r, channel=channel: r['channels'][channel]) for channel in CHANNELS},
    }
    for name, (section, select) in selectors.items():
        values = []
        for record in records:
            part = (record or {}).get(section) or {}
            try:
                value = select(part)
            except (KeyError, TypeError):
                continue
            if type(value) is int:
                values.append(value)
        complete = len(values) == planned and planned > 0
        result[name] = {'total': sum(values) if complete else None, 'knownTotal': sum(values),
                        'measuredRuns': len(values), 'unmeasuredRuns': planned - len(values)}
    return result
