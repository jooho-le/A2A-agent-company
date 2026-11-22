"""Bounded, expectation-free observations for development-loop feedback.

These supplement the parent verdict. They never replace it or infer values
from a PASS/FAIL. Test input, response bodies and exception text are excluded.
"""
import json
import re
from orchestrator.core.security import redact_text
from orchestrator.domain.snapshot_handoff import _validate_artifact_uri


MAX_OBSERVATIONS = 256
REASONS = frozenset({
    'EXPECTATION_MISMATCH', 'NOT_REACHED', 'PRIOR_ASSERTION_FAILED',
    'PRECONDITION_OR_OBSERVATION_UNAVAILABLE', 'CHECK_INTERRUPTED',
    'OBSERVATION_INTERRUPTED', 'OBSERVATION_UNAVAILABLE', 'RESPONSE_NOT_CAPTURED',
    'HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED',
})
_ID = re.compile(r'[A-Za-z0-9_-]{1,128}\Z')
LABELS = {
    'http_status': '응답 상태', 'user_delta': '사용자 수 증가량',
    'account_count': '해당 이메일 계정 수', 'rows_unchanged': '기존 사용자 정보 불변',
    'success_count': '성공 응답 수', 'response_statuses': '동시 요청 응답 상태',
    'response_matches_db': '응답 성공 여부와 계정 생성 일치',
    'no_plaintext': '저장값의 비밀번호 원문 미포함', 'argon2id': 'Argon2id 형식',
    'm': 'Argon2 메모리 m', 't': 'Argon2 반복 t', 'p': 'Argon2 병렬도 p',
    'distinct_salts': '서로 다른 salt 수', 'correct_password': '가입 비밀번호 검증 성공',
    'wrong_password': '다른 비밀번호 검증 성공', 'canonical': '이메일 정규화 저장',
    'unique': 'DB 직접 중복 삽입 거부', 'injected': 'DB 오류 주입 확인',
    'password_exposed': '비밀번호 노출 여부', 'hash_exposed': '해시 노출 여부',
    'ready': '앱·DB 관찰 준비',
}


def observation_json(rows):
    """Validate the closed wire schema before storing any observation."""
    if type(rows) is not list or not 1 <= len(rows) <= MAX_OBSERVATIONS:
        raise ValueError('CHECK_OBSERVATIONS_INVALID')
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) != {'caseId', 'itemId', 'kind', 'actual', 'status', 'reason'}:
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        for key in ('caseId', 'itemId'):
            if type(row[key]) is not str or not _ID.fullmatch(row[key]) or redact_text(row[key]) != row[key]:
                raise ValueError('CHECK_OBSERVATIONS_INVALID')
        identity = row['caseId'], row['itemId']
        if identity in seen or row['kind'] not in ('precondition', 'assertion'):
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        seen.add(identity)
        value, status, reason = row['actual'], row['status'], row['reason']
        if reason in ('HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED') and status != 'NOT_RUN':
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        if status in ('PASS', 'FAIL'):
            safe = (type(value) is bool or type(value) is int and abs(value) <= 10 ** 12
                    or type(value) is list and len(value) <= 100
                    and all(type(v) is int and 0 <= v <= 999 for v in value))
            if not safe or reason != (None if status == 'PASS' else 'EXPECTATION_MISMATCH'):
                raise ValueError('CHECK_OBSERVATIONS_INVALID')
        elif status in ('NOT_RUN', 'UNVERIFIED'):
            if value is not None or type(reason) is not str or reason not in REASONS or reason == 'EXPECTATION_MISMATCH':
                raise ValueError('CHECK_OBSERVATIONS_INVALID')
        else:
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
    return json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def case_feedback(case):
    """Use already authenticated UnitTestCase receipts; no test re-execution."""
    encoded = getattr(case, 'observations_json', None)
    if encoded is None:
        return {'observationStatus': 'UNVERIFIED', 'observations': [],
                'reason': getattr(case, 'observation_error', None) or 'DETAIL_RECORD_MISSING'}
    rows = json.loads(encoded)
    observation_json(rows)
    failed = any(r['kind'] == 'assertion' and r['status'] == 'FAIL' for r in rows)
    issue = None
    if (failed and case.outcome != 'FAIL') or (case.outcome == 'FAIL' and not failed):
        issue = 'PARENT_DETAIL_MISMATCH'
    elif case.outcome == 'PASS' and any(r['status'] != 'PASS' for r in rows):
        issue = 'DETAIL_OBSERVATIONS_INCOMPLETE'
    return {'observationStatus': 'UNVERIFIED' if issue else 'COMPLETE',
            'observations': [{**row, 'label': LABELS.get(row['itemId'], row['itemId'])} for row in rows],
            'reason': issue}


def _detail(value):
    try:
        data = json.loads(value)
        return data if type(data) is dict else {}
    except (TypeError, ValueError, RecursionError):
        return {}


def _feedback(data):
    """Read only our closed projection from a validation Artifact's details."""
    rows = data.get('observations')
    if type(rows) is not list or not rows:
        return {'observationStatus': 'UNVERIFIED', 'observations': [],
                'reason': 'DETAIL_RECORD_INVALID' if data.get('reason') == 'DETAIL_RECORD_INVALID' else 'DETAIL_RECORD_MISSING'}
    # Labels are presentation metadata; reconstruct them from safe item IDs.
    if any(type(row) is not dict for row in rows):
        raise ValueError('CHECK_OBSERVATIONS_INVALID')
    plain = [{k: v for k, v in row.items() if k != 'label'} for row in rows]
    observation_json(plain)
    status, reason = data.get('observationStatus'), data.get('reason')
    if status not in ('COMPLETE', 'UNVERIFIED') or reason not in (
            None, 'PARENT_DETAIL_MISMATCH', 'DETAIL_OBSERVATIONS_INCOMPLETE'):
        raise ValueError('CHECK_OBSERVATIONS_INVALID')
    if (status == 'COMPLETE') != (reason is None):
        raise ValueError('CHECK_OBSERVATIONS_INVALID')
    return {'observationStatus': status, 'reason': reason,
            'observations': [{**r, 'label': LABELS.get(r['itemId'], r['itemId'])} for r in plain]}


def _evidence_refs(data, evidence):
    refs = data.get('evidenceRefs', [] if evidence is None else [evidence.evidence_ref])
    if type(refs) is not list or len(refs) > 16:
        raise ValueError('CHECK_FEEDBACK_EVIDENCE_INVALID')
    if any(type(ref) is not str or len(ref) > 4096 or redact_text(ref) != ref for ref in refs):
        raise ValueError('CHECK_FEEDBACK_EVIDENCE_INVALID')
    return [_validate_artifact_uri(ref) for ref in refs]


def validation_feedback(artifacts):
    """All current QA/Security checks, including PASS siblings of failures."""
    result = []
    sources = [a for a in artifacts if a.artifact_type == 'SOURCE']
    for artifact in artifacts:
        if artifact.artifact_type not in ('QA_REPORT', 'SECURITY_REPORT'):
            continue
        if sources and (len(sources) != 1 or artifact.run_id != sources[0].run_id
                        or artifact.code_version != sources[0].code_version
                        or artifact.execution_manifest != sources[0].execution_manifest()):
            raise ValueError('CHECK_FEEDBACK_SOURCE_MISMATCH')
        base = {'role': artifact.created_by.value, 'reportArtifactId': str(artifact.artifact_id),
                'runId': str(artifact.run_id), 'codeVersion': artifact.code_version,
                'executionManifest': artifact.execution_manifest.model_dump(mode='json', by_alias=True)}
        if sources:
            base['sourceArtifactId'] = str(sources[0].artifact_id)
        if artifact.artifact_type == 'QA_REPORT':
            for test in artifact.tests:
                data = _detail(test.details)
                result.append({**base, 'requirementId': str(test.requirement_id), 'testId': test.test_id,
                    'outcome': test.outcome.value, 'title': test.title,
                    'evidenceRefs': _evidence_refs(data, test.tool_evidence), **_feedback(data)})
        else:
            for requirement in artifact.requirement_results:
                data = _detail(requirement.details)
                tests = data.get('tests', []) if data.get('code') in (
                    'MEASURED_SECURITY_TESTS', 'SECURITY_TESTS_UNVERIFIED') else []
                if type(tests) is not list or len(tests) > 1000:
                    raise ValueError('CHECK_FEEDBACK_TESTS_INVALID')
                if not tests:
                    tests = [{'testId': 'security-requirement:' + str(requirement.requirement_id),
                              'outcome': requirement.outcome.value}]
                for test in tests:
                    if (type(test) is not dict or type(test.get('testId')) is not str
                            or not test['testId'] or len(test['testId']) > 1024
                            or test.get('outcome') not in ('PASS', 'FAIL', 'UNVERIFIED')):
                        raise ValueError('CHECK_FEEDBACK_TESTS_INVALID')
                    result.append({**base, 'requirementId': str(requirement.requirement_id),
                        'testId': test['testId'], 'outcome': test['outcome'],
                        'evidenceRefs': _evidence_refs(test, requirement.tool_evidence),
                        **_feedback(test)})
    return result


def without_expectations(value):
    """Model-facing copy only; retain the original immutable audit records."""
    if type(value) is dict:
        return {key: without_expectations(item) for key, item in value.items()
                if key not in ('expectedResult', 'expected_result')}
    if type(value) is list:
        return [without_expectations(item) for item in value]
    return value
