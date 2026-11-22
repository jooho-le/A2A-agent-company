"""Policy exclusions, counted per generated test and separately from product ACC.

Only linked Host reports are read. Final grades and exception locations never
classify a conflict. Optional reproduction reviews are explicit operator input,
bound to the Run, Snapshot and test; their evidence hashes are retained for audit.
This module never executes a reproduction or certifies the supplied review.
"""
import hashlib
import html
import json
import re

EVENT = 'VALIDATION_CONFLICT_POLICY_EXCLUDED'
TEST_FIXED = 'FAILURE_GONE_AFTER_TEST_SPEC_FIX'
PRODUCT_LINKED = 'SAME_FAILURE_LINKED_TO_REQUIREMENT'
CLASSES = ('PRODUCT_DEFECT_CONFIRMED', 'TEST_DEFECT_CONFIRMED', 'UNDETERMINED')
COUNT_FIELDS = ('productDefectConfirmed', 'testDefectConfirmed', 'undetermined')
SECURITY_CODES = {'MEASURED_SECURITY_TESTS', 'SECURITY_TESTS_UNVERIFIED', 'SECURITY_TESTS_NOT_RUN'}
NOTE = ('충돌은 Run·Snapshot·역할·요구사항·제외한 생성 시험별로 센다. 재현 검토가 없으면 미확정이다. '
        '최종 시험의 PASS/FAIL이나 오류 위치만으로 원인을 확정하지 않는다. '
        '집계 범위는 기록된 정책 제외 사건이며, 계측 도입 전 Run의 누락 여부를 증명하지 않는다.')


def _text(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError('CONFLICT_ID_INVALID')
    return value


def _digest(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('CONFLICT_HASH_INVALID')
    return value


def _index(rows, key):
    found = {}
    if not isinstance(rows, list):
        raise ValueError('CONFLICT_RECORDS_MISSING')
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('CONFLICT_RECORD_INVALID')
        identity = _text(row.get(key))
        if identity in found and found[identity] != row:
            raise ValueError('CONFLICT_RECORD_CONTRADICTORY')
        found[identity] = row
    return found


def _tests(report, requirement):
    role = 'QA' if report['artifactType'] == 'QA_REPORT' else 'SECURITY'
    if role == 'QA':
        rows = report['tests']
    else:
        rows = []
        for result in report['requirementResults']:
            if result['requirementId'] != requirement or result['outcome'] == 'PASS':
                continue
            details = json.loads(result.get('details') or '{}')
            if not isinstance(details, dict):
                raise ValueError('CONFLICT_TESTS_INVALID')
            if details.get('code') not in SECURITY_CODES:
                # Semantic findings do not belong to the generated-test exclusion policy.
                continue
            tests = details['tests']
            if not isinstance(tests, list) or not tests:
                raise ValueError('CONFLICT_TESTS_MISSING')
            rows.extend({**test, 'requirementId': requirement} for test in tests)
    if not isinstance(rows, list):
        raise ValueError('CONFLICT_TESTS_MISSING')
    for test in rows:
        if test['requirementId'] != requirement:
            continue
        if test['outcome'] not in ('PASS', 'FAIL', 'UNVERIFIED'):
            raise ValueError('CONFLICT_OUTCOME_INVALID')
        if test['outcome'] != 'PASS':
            yield role, _text(test['testId']), test['outcome']


def _apply_review(rows, review, run_id):
    if review is None:
        return
    if (not isinstance(review, dict) or set(review) != {'schemaVersion', 'runId', 'reviews'}
            or type(review['schemaVersion']) is not int or review['schemaVersion'] != 1
            or review['runId'] != run_id or not isinstance(review['reviews'], list)):
        raise ValueError('CONFLICT_REVIEW_INVALID')
    by_id, seen = {row['conflictId']: row for row in rows}, set()
    fields = {'conflictId', 'snapshotSha256', 'result', 'evidenceSha256'}
    for item in review['reviews']:
        if not isinstance(item, dict) or set(item) != fields:
            raise ValueError('CONFLICT_REVIEW_INVALID')
        identity = item['conflictId']
        if not isinstance(identity, str) or identity not in by_id or identity in seen:
            raise ValueError('CONFLICT_REVIEW_BINDING_INVALID')
        seen.add(identity)
        row = by_id[identity]
        evidence = item['evidenceSha256']
        if (item['snapshotSha256'] != row['snapshotSha256']
                or item['result'] not in (TEST_FIXED, PRODUCT_LINKED)
                or not isinstance(evidence, list) or not 1 <= len(evidence) <= 32):
            raise ValueError('CONFLICT_REVIEW_EVIDENCE_INVALID')
        hashes = [_digest(value) for value in evidence]
        if len(set(hashes)) != len(hashes):
            raise ValueError('CONFLICT_REVIEW_EVIDENCE_INVALID')
        row.update(classification='TEST_DEFECT_CONFIRMED' if item['result'] == TEST_FIXED else 'PRODUCT_DEFECT_CONFIRMED',
                   classificationBasis='SUPPLIED_REPRODUCTION_REVIEW', reproductionResult=item['result'],
                   evidenceSha256=hashes)


def conflict_analysis(bundle, trace, *, review=None):
    """Incomplete evidence produces unknown totals, never a fabricated zero.

An operator review names the SHA-256 conflictId emitted by this function, whose
identity includes runId/codeVersion/snapshotSha256/role/requirementId/testId.
Review evidence must be independently checked by the operator. Only its binding
and shape are validated here; no hidden final-exam results are consumed.
"""
    run_id = (bundle.get('run') or {}).get('runId')
    issues, found = [], {}
    try:
        _text(run_id)
        events = bundle['events']
        if (not isinstance(events, list) or type(bundle.get('eventsTotal')) is not int
                or bundle['eventsTotal'] != len(events) or bundle.get('snapshotConsistent') is not True
                or trace.get('runId') != run_id):
            raise ValueError('CONFLICT_TRACE_INCOMPLETE')
        indexed = _index(events, 'eventId')
        exclusions = [event for event in indexed.values() if event.get('eventType') == EVENT]
        artifacts = _index(trace.get('artifacts'), 'artifactId') if exclusions else {}
        for event in exclusions:
            try:
                if (event.get('runId') != run_id or event.get('actor') != 'Orchestrator'
                        or type(event.get('codeVersion')) is not int or event['codeVersion'] < 1):
                    raise ValueError('CONFLICT_EVENT_BINDING_INVALID')
                snapshot = _digest(event.get('snapshotSha256'))
                requirements, ids = event['requirementIds'], event['outputArtifactIds']
                if (not isinstance(requirements, list) or not requirements or not isinstance(ids, list)
                        or len(ids) != 2 or len(set(ids)) != 2):
                    raise ValueError('CONFLICT_EVENT_BINDING_INVALID')
                linked = [artifacts[identity] for identity in ids]
                if {report.get('artifactType') for report in linked} != {'QA_REPORT', 'SECURITY_REPORT'}:
                    raise ValueError('CONFLICT_REPORTS_MISSING')
                for report in linked:
                    manifest = report.get('executionManifest')
                    if (report.get('runId') != run_id or report.get('codeVersion') != event['codeVersion']
                            or not isinstance(manifest, dict) or manifest.get('snapshotSha256') != snapshot):
                        raise ValueError('CONFLICT_REPORT_BINDING_INVALID')
                pending = []
                for requirement in requirements:
                    _text(requirement)
                    matched = [(report, test) for report in linked for test in _tests(report, requirement)]
                    if not matched:
                        raise ValueError('CONFLICT_TESTS_MISSING')
                    for report, (role, test_id, outcome) in matched:
                        identity = dict(runId=run_id, codeVersion=event['codeVersion'], snapshotSha256=snapshot,
                                        role=role, requirementId=requirement, testId=test_id)
                        conflict_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
                        pending.append((conflict_id, identity, outcome, report['artifactId']))
                for conflict_id, identity, outcome, artifact_id in pending:
                    row = found.setdefault(conflict_id, {**identity, 'conflictId': conflict_id,
                        'outcome': outcome, 'eventIds': [], 'artifactIds': [], 'classification': 'UNDETERMINED',
                        'classificationBasis': 'NO_REPRODUCTION_REVIEW', 'evidenceSha256': []})
                    if row['outcome'] != outcome:
                        raise ValueError('CONFLICT_OUTCOME_CONTRADICTORY')
                    for key, value in (('eventIds', event['eventId']), ('artifactIds', artifact_id)):
                        if value not in row[key]:
                            row[key].append(value)
            except (KeyError, TypeError, ValueError):
                issues.append('CONFLICT_EVENT_OR_REPORT_INCOMPLETE')
    except (KeyError, TypeError, ValueError):
        issues.append('CONFLICT_TRACE_INCOMPLETE')
    rows = sorted(found.values(), key=lambda row: (row['codeVersion'], row['role'], row['requirementId'], row['testId']))
    _apply_review(rows, review, run_id)
    counts = {name: sum(row['classification'] == kind for row in rows) for name, kind in zip(COUNT_FIELDS, CLASSES)}
    return {'schemaVersion': 1, 'kind': 'VALIDATION_CONFLICT_ANALYSIS', 'runId': run_id,
            'status': 'UNVERIFIED' if issues else 'MEASURED', 'reason': issues[0] if issues else None,
            'coverage': 'RECORDED_POLICY_EXCLUSIONS', 'excluded': None if issues else len(rows),
            **{name: None if issues else value for name, value in counts.items()},
            'observedExcluded': len(rows), 'conflicts': rows, 'note': NOTE}


def summary(data):
    return {key: data[key] for key in ('status', 'reason', 'excluded', *COUNT_FIELDS)}


def render(data):
    def cell(value):
        return html.escape(str(value)).replace('|', '&#124;').replace('\n', ' ')
    def count(value):
        return '미확인' if value is None else str(value)
    lines = ['# 충돌 분석(ACC와 별도)', '', f'- 집계 상태: {data["status"]}',
             f'- 정책으로 제외한 충돌: {count(data["excluded"])}',
             f'- 제품 결함으로 확인: {count(data["productDefectConfirmed"])}',
             f'- 시험 결함으로 확인: {count(data["testDefectConfirmed"])}',
             f'- 미확정: {count(data["undetermined"])}', '', data['note'], '',
             '확정 분류는 별도로 제공한 재현 검토에 따른다. 이 집계기는 재현을 실행하거나 근거 파일을 인증하지 않는다.', '',
             '| 버전 | 역할 | 요구사항 | 제외한 시험 | 원래 결과 | 분류 | 충돌 ID |',
             '|---|---|---|---|---|---|---|']
    if data['reason']:
        lines.insert(3, f'- 사유: {data["reason"]} (확인된 항목 {data["observedExcluded"]}개; 전체 수 미확인)')
    for row in data['conflicts']:
        labels = {'PRODUCT_DEFECT_CONFIRMED': '제품 결함 확인',
                  'TEST_DEFECT_CONFIRMED': '시험 결함 확인', 'UNDETERMINED': '미확정'}
        shown = {**row, 'classification': labels[row['classification']]}
        lines.append('| ' + ' | '.join(cell(shown[key]) for key in
            ('codeVersion', 'role', 'requirementId', 'testId', 'outcome', 'classification', 'conflictId')) + ' |')
    return '\n'.join(lines) + '\n'
