"""Validate and render supplementary observations without changing nine-check scoring.

An absent or malformed receipt is not evidence of non-execution. Reconstruct
the trusted plan and show UNVERIFIED. Never synthesize PASS from a parent PASS.
"""
from collections import Counter
from copy import deepcopy
import ast
import copy
import hashlib
import json

from orchestrator.domain.validation_feedback import validate_row_diagnostic, validate_status_origin
from . import protected_tests
from .protected_tests.tests.signup.signup_details import matches, plan, safe_actual

FUNCTION_KEYS = dict(zip(
    ('valid_signup', 'invalid_email', 'duplicate_email', 'password_length',
     'concurrent_signup', 'response_matches_result', 'stored_hash',
     'unique_and_canonical', 'not_exposed'),
    ('QA-SIGNUP-001', 'QA-SIGNUP-002', 'QA-SIGNUP-003', 'QA-SIGNUP-004',
     'QA-SIGNUP-005', 'QA-SIGNUP-006', 'SEC-SIGNUP-002', 'SEC-SIGNUP-003', 'SEC-SIGNUP-005')))
def plans_from_source(source):
    """Read only the trusted exam's literals, never import generated source."""
    tree = ast.parse(source)
    constants = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                 if isinstance(node, ast.Assign) and len(node.targets) == 1
                 and isinstance(node.targets[0], ast.Name)}

    class Literals(ast.NodeTransformer):
        def visit_Name(self, node):
            if node.id not in constants:
                raise ValueError('DETAIL_PLAN_NOT_LITERAL')
            return ast.Constant(constants[node.id])

    result = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or not node.name.startswith('test_'):
            continue
        calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)
                 and isinstance(item.func, ast.Attribute) and item.func.attr in FUNCTION_KEYS]
        if len(calls) != 1 or len(calls[0].args) != 2:
            raise ValueError('DETAIL_PLAN_CALL_INVALID')
        call = calls[0]
        key = FUNCTION_KEYS[call.func.attr]
        data = ast.literal_eval(Literals().visit(copy.deepcopy(call.args[1])))
        result[key] = plan(key, data)
    return result


LABELS = {'http_status': '응답 상태', 'user_delta': '사용자 수 증가량', 'account_count': '해당 이메일 계정 수', 'rows_unchanged': '기존 사용자 정보 불변', 'success_count': '성공 응답 수', 'response_statuses': '모든 응답이 2xx 또는 4xx', 'response_matches_db': '응답 성공 여부와 계정 생성 일치', 'no_plaintext': '저장값이 문자열이며 비밀번호 원문 미포함', 'argon2id': '모든 저장값의 Argon2id 형식', 'm': 'Argon2 메모리 m', 't': 'Argon2 반복 t', 'p': 'Argon2 병렬도 p', 'distinct_salts': '서로 다른 salt 수', 'correct_password': '가입 비밀번호 검증 성공', 'wrong_password': '다른 비밀번호 검증 성공', 'canonical': '저장 이메일이 정규화 정책과 일치', 'unique': 'DB 직접 중복 삽입 거부', 'injected': 'DB 오류 주입 확인', 'password_exposed': '비밀번호 노출 여부', 'hash_exposed': '해시 또는 해시 부분 노출 여부', 'ready': '앱·DB 관찰 준비', 'response_status': '받은 응답의 상태 코드', 'password_in_crash': '처리하지 못한 예외 traceback의 비밀번호 노출', 'hash_in_crash': '처리하지 못한 예외 traceback의 해시 노출'}

STATUSES = ('PASS', 'FAIL', 'NOT_RUN', 'UNVERIFIED')
REASONS = {None, 'EXPECTATION_MISMATCH', 'NOT_REACHED', 'PRIOR_ASSERTION_FAILED',
           'PRECONDITION_OR_OBSERVATION_UNAVAILABLE', 'CHECK_INTERRUPTED',
           'OBSERVATION_INTERRUPTED', 'OBSERVATION_UNAVAILABLE', 'RESPONSE_NOT_CAPTURED',
           'HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED', 'STORED_USER_REQUIRED', 'UNHANDLED_APP_EXCEPTION'}
MAX_BYTES = 1024 * 1024


def expected_plans(exam, suite=None):
    suite = suite or protected_tests.load_suite()
    result = {}
    for path in suite['scopes'][exam]['files']:
        if path.rsplit('/', 1)[-1] in {'exam_final.py', 'exam_pilot.py', 'practice_qa.py', 'practice_security.py'}:
            for key, rows in plans_from_source((protected_tests.ROOT / path).read_text()).items():
                if key in result:
                    raise ValueError('DETAIL_PLAN_DUPLICATE')
                result[key] = rows
    if set(result) != {check['key'] for check in suite['checks']}:
        raise ValueError('DETAIL_PLAN_INCOMPLETE')
    return result


def _validated(rows, plan):
    if not isinstance(rows, list) or len(rows) != len(plan):
        raise ValueError('DETAIL_ROWS_INCOMPLETE')
    result = []
    for row, expected in zip(rows, plan):
        if (not isinstance(row, dict) or set(row) - {'handlerEntry', 'diagnostic', 'statusOrigin'} != set(expected) | {'status', 'actual', 'reason'}
                or json.dumps({name: row.get(name) for name in expected}, sort_keys=True, allow_nan=False)
                != json.dumps(expected, sort_keys=True, allow_nan=False)
                or row['status'] not in STATUSES or row['reason'] not in REASONS):
            raise ValueError('DETAIL_ROW_INVALID')
        status, actual, reason = row['status'], row['actual'], row['reason']
        if reason in {'HASH_FORMAT_REQUIRED', 'HASH_VALUES_REQUIRED', 'STORED_USER_REQUIRED'} and status != 'NOT_RUN':
            raise ValueError('DETAIL_REASON_INVALID')
        if status in ('PASS', 'FAIL'):
            if not safe_actual(actual) or matches(expected['expected'], actual) != (status == 'PASS'):
                raise ValueError('DETAIL_VERDICT_MISMATCH')
            if reason != (None if status == 'PASS' else 'EXPECTATION_MISMATCH'):
                raise ValueError('DETAIL_REASON_INVALID')
        elif actual is not None or reason in (None, 'EXPECTATION_MISMATCH'):
            raise ValueError('DETAIL_UNOBSERVED_VALUE')
        # Copy trusted definitions; only validated scalar observations cross.
        detail = {**deepcopy(expected), 'actual': deepcopy(actual), 'status': status, 'reason': reason}
        if 'statusOrigin' in row:
            validate_status_origin(row)
            detail['statusOrigin'] = row['statusOrigin']
        if 'diagnostic' in row:
            validate_row_diagnostic(row)
            detail['diagnostic'] = deepcopy(row['diagnostic'])
        if 'handlerEntry' in row:
            if (expected['kind'] != 'precondition' or expected['itemId'] != 'response_status'
                    or row['handlerEntry'] not in ('ENTERED', 'NOT_ENTERED', 'UNKNOWN')):
                raise ValueError('DETAIL_ROW_INVALID')
            detail['handlerEntry'] = row['handlerEntry']
        result.append(detail)
    return result


def build_details(report, payload, *, exam, runner_error=None):
    suite = protected_tests.load_suite()
    plans = expected_plans(exam, suite)
    parent = {row['title'].split(' ', 1)[0]: row for row in report['checks']}
    problem = None
    manifest = report.get('manifestCheck') or {}
    if manifest.get('errorCode') or not manifest.get('metadataMatches'):
        problem = 'DETAIL_MANIFEST_UNVERIFIED'
    elif report['suite'] != {'id': suite['suiteId'], 'version': suite['suiteVersion'],
                             'sha256': protected_tests.suite_sha256(exam, suite)}:
        problem = 'DETAIL_SUITE_MISMATCH'
    elif runner_error:
        problem = 'DETAIL_RUNNER_UNVERIFIED'
    elif payload is None:
        problem = 'DETAIL_RECORD_MISSING'
    else:
        try:
            if (len(json.dumps(payload, allow_nan=False).encode()) > MAX_BYTES
                    or not isinstance(payload, dict) or set(payload) != {'schemaVersion', 'checks'}
                    or type(payload['schemaVersion']) is not int or payload['schemaVersion'] != 1
                    or not isinstance(payload['checks'], dict)
                    or set(payload['checks']) != set(plans)):
                raise ValueError('DETAIL_RECORD_INVALID')
        except (TypeError, ValueError, OverflowError, RecursionError):
            problem = 'DETAIL_RECORD_INVALID'
    groups = []
    for key, plan in plans.items():
        error = problem
        rows = None
        if error is None:
            try:
                rows = _validated(payload['checks'][key], plan)
            except (TypeError, ValueError, KeyError, RecursionError, OverflowError):
                error = 'DETAIL_RECORD_INVALID'
        if rows is None:
            rows = [{**deepcopy(row), 'actual': None, 'status': 'UNVERIFIED', 'reason': error} for row in plan]
        for row in rows:
            row['label'] = LABELS[row['itemId']]
        source = parent[key]
        failed = [row for row in rows if row['kind'] == 'assertion' and row['status'] == 'FAIL']
        # Preserve useful validated observations, but prominently flag conflicts.
        if error is None:
            if source['status'] == 'PASS' and failed or source['status'] == 'FAIL' and not failed:
                error = 'PARENT_DETAIL_MISMATCH'
            elif source['status'] == 'PASS' and any(row['status'] != 'PASS' for row in rows):
                error = 'DETAIL_OBSERVATIONS_INCOMPLETE'
        counts = dict(Counter(row['status'] for row in rows))
        groups.append({'checkId': key, 'title': source['title'], 'parentStatus': source['status'],
                       'evidenceIds': source.get('evidenceIds', []), 'recordIssue': error,
                       'counts': {status: counts.get(status, 0) for status in STATUSES}, 'items': rows})
    data = {'schemaVersion': 1, 'kind': 'PROTECTED_CHECK_DETAILS', 'sourceRunId': report['sourceRunId'],
            'evaluationId': report['evaluationId'], 'architecture': report['architecture'],
            'exam': exam, 'suite': deepcopy(report['suite']), 'createdAt': report['createdAt'],
            'expectedManifest': deepcopy(report['expectedManifest']), 'manifestCheck': deepcopy(manifest),
            'recordStatus': 'COMPLETE' if all(not row['recordIssue'] for row in groups) else 'UNVERIFIED',
            'checks': groups,
            'scopeNotes': ['공식 판정은 기존 9개 검사 결과를 사용하며 세부 항목 수로 점수를 바꾸지 않습니다.',
                           '비밀번호·해시·이메일·응답 본문·로그 원문은 저장하지 않습니다.',
                           '노출 검사는 수집된 응답과 앱 시작~종료 로그·출력에 한정됩니다. 별도 파일 등 미수집 경로는 포함하지 않습니다.']}
    data['sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False,
                                             separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    return data


def _cell(value):
    if value is None:
        return '미관찰'
    if type(value) is bool:
        return '참' if value else '거짓'
    text = str(value)
    # All text originates in trusted metadata except validated source identity.
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('|', '\\|').replace('\n', ' ')


def _expected(row):
    op, value = row['expected']['operator'], row['expected']['value']
    if op == 'range':
        return f'{value[0]}–{value[1]}'
    if op == 'gte':
        return f'{value} 이상'
    if op == 'http_statuses':
        return f'{value}건, 모두 2xx 또는 4xx'
    return _cell(value)


def _case_label(case):
    names = {
        'setup': '앱·DB 준비', 'precondition': '정상 가입 사전조건',
        'valid_email_1': '점·하위 도메인 포함 이메일', 'valid_email_2': '2글자 최상위 도메인 이메일',
        'invalid_email_1': '빈 이메일', 'invalid_email_2': '공백만 있는 이메일',
        'invalid_email_3': '@ 없는 이메일', 'invalid_email_4': '로컬 부분 없는 이메일',
        'invalid_email_5': '도메인 없는 이메일', 'invalid_email_6': '@ 두 개인 이메일',
        'invalid_email_7': '중간 공백 포함 이메일', 'final': '모든 요청 이후',
        'success': '정상 가입 응답', 'invalid_email': '잘못된 이메일 응답',
        'short_password': '짧은 비밀번호 응답', 'duplicate': '중복 가입 응답',
        'db_error': 'DB 오류 요청', 'application_logs': '앱 시작~종료 로그·출력',
        'storage': '저장값·계정 간 비교'}
    return names.get(case, case)


def render_details(data):
    lines = ['# QA·Security 세부 결과', '',
             f'- 실행: {_cell(data["sourceRunId"])} · 구조: {_cell(data["architecture"])}',
             f'- 채점 ID: {_cell(data["evaluationId"])} · 시험: {_cell(data["exam"])}',
             f'- 코드 Snapshot: {_cell(data["expectedManifest"].get("snapshotSha256"))}',
             f'- 시험지: {_cell(data["suite"]["version"])} · {_cell(data["suite"]["sha256"])}',
             f'- 상세 기록 상태: {_cell(data["recordStatus"])}', '',
             '세부 상태: PASS / FAIL / 미실행(NOT_RUN) / 미검증(UNVERIFIED). 준비 조건은 제품 검증 항목과 구분합니다.', '']
    lines += ['- ' + note for note in data['scopeNotes']]
    lines += ['', '## 검사별 목차', '']
    for group in data['checks']:
        anchor = group['checkId'].lower()
        lines.append(f'- [{group["checkId"]}](#{anchor}) · 상위 판정 {group["parentStatus"]}')
    lines += ['', '## 실패·미실행·미검증 항목', '']
    issues = []
    for group in data['checks']:
        if group['recordIssue']:
            issues.append(f'- {group["checkId"]}: 기록 문제 {_cell(group["recordIssue"])}')
        for row in group['items']:
            if row['status'] != 'PASS':
                issues.append(f'- {group["checkId"]} / {row["caseId"]} / {row["itemId"]}: '
                              f'{row["status"]} ({_cell(row["reason"])})')
    lines += issues or ['- 없음']
    for group in data['checks']:
        lines += ['', f'## {group["checkId"]}', '',
                  f'{_cell(group["title"])} · 상위 판정 **{group["parentStatus"]}**', '',
                  '| 사례 | 구분 | 세부 항목 | 기대값 | 실제값 | 결과 | 사유 |',
                  '|---|---|---|---|---|---|---|']
        for row in group['items']:
            status = {'NOT_RUN': '미실행', 'UNVERIFIED': '미검증'}.get(row['status'], row['status'])
            values = (_case_label(row['caseId']), '준비 조건' if row['kind'] == 'precondition' else '제품 검사',
                      row['label'], _expected(row), row['actual'], status, row['reason'] or '—')
            lines.append('| ' + ' | '.join(_cell(value) for value in values) + ' |')
    return '\n'.join(lines) + '\n'


def write_details(folder, output, write):
    """Use the caller's existing transactional/symlink-safe result writer."""
    data = output.get('checkDetails')
    if data is None:
        return
    write(folder / 'check_details.json', data, False)
    write(folder / 'check_details.md', render_details(data), False)
