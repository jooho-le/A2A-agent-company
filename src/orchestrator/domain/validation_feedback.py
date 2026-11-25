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
    'HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED', 'STORED_USER_REQUIRED',
    # 보호 시험 0.7.3: 앱이 처리하지 못한 요청 예외로 응답이 없음(노출 경로 안내).
    'UNHANDLED_APP_EXCEPTION',
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
    'response_status': '받은 응답의 상태 코드',
    'password_in_crash': '처리하지 못한 예외 traceback의 비밀번호 노출',
    'hash_in_crash': '처리하지 못한 예외 traceback의 해시 노출',
}


OUTPUT_CHANNELS = frozenset({'SUCCESS_RESPONSE', 'REJECTION_RESPONSE', 'STORAGE_ERROR_RESPONSE',
                             'STORAGE_ERROR_REQUEST_LOGS', 'REQUEST_LOGS', 'OTHER_RESPONSE'})


def validated_signup_checks(value):
    if type(value) is not dict or not value or not set(value) <= {
            'concurrentCanonicalSignup', 'canonicalEmailUniqueKey', 'storedHashParameters'}:
        raise ValueError('SIGNUP_OBSERVATIONS_INVALID')
    for key, facts in value.items():
        if key != 'storedHashParameters':
            if type(facts) is not bool:
                raise ValueError('SIGNUP_OBSERVATIONS_INVALID')
            continue
        if (type(facts) is not dict or set(facts) - {'distinctSalts'} != {'argon2id', 'verified', 'memoryKiB', 'iterations', 'parallelism'}
                or ('distinctSalts' in facts and type(facts['distinctSalts']) is not bool)
                or any(type(facts[k]) is not bool for k in ('argon2id', 'verified'))
                or any(type(facts[k]) is not int or not 0 <= facts[k] <= 2**32 - 1
                       for k in ('memoryKiB', 'iterations', 'parallelism'))):
            raise ValueError('SIGNUP_OBSERVATIONS_INVALID')
    return value


def validated_output_checks(marks):
    if (type(marks) is not dict or not marks or marks.keys() - OUTPUT_CHANNELS
            or any(v not in ('CHECKED', 'EXPOSED', 'INCOMPLETE') for v in marks.values())):
        raise ValueError('INVALID_OUTPUT_OBSERVATIONS')
    return dict(marks)


_BODY_PATH = re.compile(r'BODY(?:\.(?:detail|input|body|email|password|password_hash|message|\*)|\[(?:[0-9]|[12][0-9]|3[01])\]){0,8}\Z')
_EXCEPTION_TYPES = frozenset(['OTHER', *('sqlalchemy.exc.' + n for n in (
    'OperationalError','IntegrityError','DBAPIError','DatabaseError','StatementError','DataError','SQLAlchemyError','ProgrammingError')),
    *('sqlite3.' + n for n in ('OperationalError','IntegrityError','DatabaseError')),
    *('builtins.' + n for n in ('RuntimeError','ValueError','TypeError','Exception','KeyError')),
    'fastapi.exceptions.RequestValidationError','fastapi.exceptions.HTTPException','starlette.exceptions.HTTPException'])


def observed_argument_type(cause):
    if (type(cause) is dict and set(cause) == {'code','actualType'}
            and cause['code'] == 'ARGUMENT_TYPE_MISMATCH'
            and cause['actualType'] in ('str','bytes','bool','int','float','list','tuple','dict','set','NoneType')):
        return cause['actualType']
    return None


def validate_diagnostic(data):
    """Only closed structural coordinates, never error text, user keys, payloads or SQL."""
    if type(data) is not dict:
        raise ValueError('INVALID_REQUEST_DIAGNOSTIC')
    if set(data) == {'locations'}:
        locations = data['locations']
        if (type(locations) is list and 1 <= len(locations) <= 4
                and all(type(p) is str and len(p) <= 140 and (p == 'HEADERS' or _BODY_PATH.fullmatch(p)) for p in locations)
                and len(set(locations)) == len(locations)):
            return
    elif (set(data) - {'appFrame','observedCause'} == {'escapedToClient','responseStarted','exceptionType'}
          and data['escapedToClient'] is True and type(data['responseStarted']) is bool
          and type(data['exceptionType']) is str and data['exceptionType'] in _EXCEPTION_TYPES):
        if 'observedCause' in data:
            cause = data['observedCause']
            if ('appFrame' not in data or data['exceptionType'] != 'builtins.TypeError'
                    or observed_argument_type(cause) is None):
                raise ValueError('INVALID_REQUEST_DIAGNOSTIC')
        if 'appFrame' not in data:
            return
        frame = data['appFrame']
        if (type(frame) is dict and set(frame) == {'sourceSha256','line'}
                and type(frame['sourceSha256']) is str and re.fullmatch('[0-9a-f]{64}', frame['sourceSha256'])
                and type(frame['line']) is int and 1 <= frame['line'] <= 1_000_000):
            return
    raise ValueError('INVALID_REQUEST_DIAGNOSTIC')


def server_status_evidence(row):
    """Project origin on a scored 5xx; historical numeric observations cannot distinguish the source."""
    code = row.get('actual')
    if (row.get('itemId') != 'http_status' or row.get('status') not in ('PASS', 'FAIL')
            or type(code) not in (int, float) or not 500 <= code <= 599 or int(code) != code):
        return {}
    origin = row.get('statusOrigin')
    if origin == 'HTTP_RESPONSE' or (origin == 'ESCAPED_EXCEPTION' and code == 500):
        return {'statusOrigin': origin}
    return {'statusOrigin': 'UNKNOWN'}


def validate_status_origin(row):
    origin = row['statusOrigin']
    code = row['actual']
    if (row['itemId'] != 'http_status' or row['kind'] not in ('assertion', 'precondition')
            or row['status'] not in ('PASS', 'FAIL') or type(code) is not int or not 500 <= code <= 599
            or origin not in ('HTTP_RESPONSE', 'ESCAPED_EXCEPTION')
            or (origin == 'ESCAPED_EXCEPTION' and code != 500)):
        raise ValueError('CHECK_STATUS_ORIGIN_INVALID')


def validate_row_diagnostic(row):
    data = row['diagnostic']
    validate_diagnostic(data)
    if 'locations' in data:
        valid = (row['kind'] == 'assertion' and row['itemId'] in ('password_exposed','hash_exposed')
                 and row['status'] == 'FAIL' and row['actual'] is True)
    else:
        valid = (row['kind'] == 'precondition' and row['itemId'] == 'response_status'
                 and row['status'] == 'UNVERIFIED' and row['reason'] == 'UNHANDLED_APP_EXCEPTION')
    if not valid:
        raise ValueError('REQUEST_DIAGNOSTIC_OUTCOME_MISMATCH')


def validated_output_diagnostics(value, marks):
    if type(value) is not dict or not value or value.keys() - marks.keys():
        raise ValueError('INVALID_OUTPUT_DIAGNOSTICS')
    for channel, items in value.items():
        if channel.endswith('LOGS') or type(items) is not list or not 1 <= len(items) <= 4:
            raise ValueError('INVALID_OUTPUT_DIAGNOSTICS')
        for data in items:
            validate_diagnostic(data)
            if marks[channel] == 'CHECKED' or ('locations' in data and marks[channel] != 'EXPOSED'):
                raise ValueError('REQUEST_DIAGNOSTIC_OUTCOME_MISMATCH')
    return value


def observation_json(rows):
    """Validate the closed wire schema before storing any observation."""
    if type(rows) is not list or not 1 <= len(rows) <= MAX_OBSERVATIONS:
        raise ValueError('CHECK_OBSERVATIONS_INVALID')
    seen = set()
    for row in rows:
        if type(row) is not dict or set(row) - {'handlerEntry', 'diagnostic', 'statusOrigin'} != {'caseId', 'itemId', 'kind', 'actual', 'status', 'reason'}:
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        if 'handlerEntry' in row and (row['kind'] != 'precondition' or row['itemId'] != 'response_status'
                or row['handlerEntry'] not in ('ENTERED', 'NOT_ENTERED', 'UNKNOWN')):
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        if 'statusOrigin' in row:
            validate_status_origin(row)
        if 'diagnostic' in row:
            validate_row_diagnostic(row)
        for key in ('caseId', 'itemId'):
            if type(row[key]) is not str or not _ID.fullmatch(row[key]) or redact_text(row[key]) != row[key]:
                raise ValueError('CHECK_OBSERVATIONS_INVALID')
        identity = row['caseId'], row['itemId']
        if identity in seen or row['kind'] not in ('precondition', 'assertion'):
            raise ValueError('CHECK_OBSERVATIONS_INVALID')
        seen.add(identity)
        value, status, reason = row['actual'], row['status'], row['reason']
        if reason in ('HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED', 'STORED_USER_REQUIRED') and status != 'NOT_RUN':
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


def measured_test_version(receipt):
    """Return a content version from the Host-verified test inputs, when available."""
    scope = getattr(receipt, 'scope', None)
    ref = getattr(scope, 'protected_suite_ref', None)
    matched = re.search(r'/practice-([0-9a-f]{64})$', ref) if isinstance(ref, str) else None
    if matched:
        return {'testVersion': 'practice:' + matched[1]}
    inputs = getattr(receipt, 'inputs', None)
    digest = inputs.get('inputsSha256') if hasattr(inputs, 'get') else None
    if isinstance(digest, str) and re.fullmatch('[0-9a-f]{64}', digest):
        return {'testVersion': 'inputs:' + digest}
    return {}


def case_feedback(case):
    """Use already authenticated UnitTestCase receipts; no test re-execution."""
    channels = {'outputChecks': dict(case.output_checks)} if getattr(case, 'output_checks', ()) else {}
    if getattr(case, 'signup_checks_json', None):
        channels['signupChecks'] = validated_signup_checks(json.loads(case.signup_checks_json))
    if getattr(case, 'output_diagnostics_json', None):
        channels['outputDiagnostics'] = validated_output_diagnostics(json.loads(case.output_diagnostics_json), channels['outputChecks'])
    encoded = getattr(case, 'observations_json', None)
    if encoded is None:
        return {**channels, 'observationStatus': 'UNVERIFIED', 'observations': [],
                'reason': getattr(case, 'observation_error', None) or 'DETAIL_RECORD_MISSING'}
    rows = json.loads(encoded)
    observation_json(rows)
    failed = any(r['kind'] == 'assertion' and r['status'] == 'FAIL' for r in rows)
    issue = None
    if (failed and case.outcome != 'FAIL') or (case.outcome == 'FAIL' and not failed):
        issue = 'PARENT_DETAIL_MISMATCH'
    elif case.outcome == 'PASS' and any(r['status'] != 'PASS' for r in rows):
        issue = 'DETAIL_OBSERVATIONS_INCOMPLETE'
    return {**channels, 'observationStatus': 'UNVERIFIED' if issue else 'COMPLETE',
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
    channels = {'outputChecks': validated_output_checks(data['outputChecks'])} if 'outputChecks' in data else {}
    if data.get('code') in {'BROWSER_ACTION_TIMEOUT', 'BROWSER_ASSERTION_FAILED', 'BROWSER_ACTION_FAILED'}:
        index, action = data.get('stepIndex'), data.get('action')
        if (type(index) is int and 1 <= index <= 1000 and action in {
                'goto', 'fill', 'click', 'assert_text', 'assert_visible', 'assert_url'}):
            channels['browserFailure'] = {'code': data['code'], 'stepIndex': index, 'action': action}
    if 'signupChecks' in data:
        channels['signupChecks'] = validated_signup_checks(data['signupChecks'])
    if 'outputDiagnostics' in data:
        channels['outputDiagnostics'] = validated_output_diagnostics(data['outputDiagnostics'], channels.get('outputChecks', {}))
    version = data.get('testVersion')
    identity = {'testVersion': version} if isinstance(version, str) and re.fullmatch(
        r'(?:practice|inputs):[0-9a-f]{64}', version) else {}
    rows = data.get('observations')
    if type(rows) is not list or not rows:
        return {**identity, **channels, 'observationStatus': 'UNVERIFIED', 'observations': [],
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
    return {**identity, **channels, 'observationStatus': status, 'reason': reason,
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
                    'MEASURED_SECURITY_TESTS', 'SECURITY_TESTS_UNVERIFIED', 'SECURITY_TESTS_NOT_RUN') else []
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
