"""D2 회원가입 보호 테스트 묶음(src/evaluation/protected_tests)과 결과 어댑터(check_adapters.py)를 시험한다.

확인하는 것:
  - 묶음 대응표(suite.json)가 실제 테스트 메서드·기준표 검사 ID·정적 판정 기준과 정확히 맞는가?
  - dev가 보호 파일 설정(UnitTestScope)과 보호 QA 케이스(QACaseBinding)를 받아 주는가?
  - unittest 보고서·정적 판정 결과가 검사 결과로 옮겨질 때 근거 없는 PASS가 생기지 않는가?
  - 독립 평가 보고서까지 이어지는가?
  - (실행) 최소 회원가입 서비스와 결함 변형 14개를 dev unittest 실행기로 돌렸을 때 결함마다
    해당 검사만 FAIL이 되고, 연결이 안 되면 모두 UNVERIFIED가 되는가?
실행 시험은 sqlalchemy·argon2-cffi가 있어야 돈다(없으면 건너뜀):
  uv run --with sqlalchemy --with argon2-cffi python -m pytest tests/evaluation/test_protected_signup.py
"""
import ast
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import UUID

import pytest

from evaluation import protected_tests as pt
from evaluation.aggregation import Status
from evaluation.check_adapters import check_results_from_assessment, check_results_from_unit_report
from evaluation.independent import build_evaluation_report
from evaluation.manifest import ExecutionManifest
from orchestrator.domain.scenario_registry import SCN_001_ID, RequirementValidator, get_scenario

from tests.evaluation import signup_service

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = get_scenario(SCN_001_ID).planner_contract()
SUITE = pt.load_suite()
CHECKS = {check['key']: check for check in SUITE['checks']}


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자면 같은 값이다."""
    return str(UUID(int=number, version=4))


def defined_tests(scope):
    """보호 파일을 읽기만 해서(import 없이) unittest 테스트 ID 목록을 만든다."""
    ids = set()
    for path, content in pt.protected_files(scope).items():
        module = Path(path).stem
        for node in ast.parse(content).body:
            if isinstance(node, ast.ClassDef):
                ids |= {f'{module}.{node.name}.{item.name}' for item in node.body
                        if isinstance(item, ast.FunctionDef) and item.name.startswith('test')}
    return ids


def matches(pattern, name):
    """파일 이름이 범위의 패턴(예: protected_signup_*.py)에 맞는지 본다. dev 실행기와 같은 fnmatch 규칙이다."""
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


# ---------------------------------------------------------------- 대응표


def test_suite_lists_exactly_the_checks_of_the_criteria_table():
    """묶음의 검사는 기준표(scn001_criteria.md)의 QA 6개·SEC 5개와 정확히 같고,
    정적 검사의 요구사항 연결은 Security 정적 판정 기준(scn001_security_criteria.json)과 같다."""
    assert set(CHECKS) == {f'QA-SIGNUP-00{n}' for n in range(1, 7)} | {f'SEC-SIGNUP-00{n}' for n in range(1, 6)}
    criteria = json.loads((ROOT / 'src/evaluation/security/scn001_security_criteria.json').read_text(encoding='utf-8'))
    static = {check['key']: check['requirementKey'] for check in SUITE['checks'] if check['method'] == 'static'}
    assert static == {label: requirement for requirement, label in criteria['checks'].items()}


@pytest.mark.parametrize('scope', pt.SCOPES)
def test_every_protected_test_is_bound_and_every_bound_test_exists(scope):
    """dev QA는 대응표에 없는 테스트가 보고서에 있으면 보고서 전체를 거부한다. 빠짐없이 1:1이어야 한다."""
    pattern = SUITE['scopes'][scope]['pattern']
    discovered = {test for test in defined_tests(scope) if matches(pattern, test.split('.', 1)[0] + '.py')}
    bound = {check['testId'] for check in SUITE['checks'] if scope in check['scopes'] and check['method'] == 'unittest'}
    assert discovered == bound


def test_in_loop_scope_holds_only_qa_requirements():
    """순환 안 보호 케이스는 dev가 QA 담당으로 정한 요구사항만 쓰고, 모두 run_unit_tests 도구의
    protected-signup 선택자에 묶인다."""
    qa_ids = {str(value) for value in get_scenario(SCN_001_ID).requirement_ids_for(RequirementValidator.QA)}
    cases = pt.owned_qa_cases(CONTRACT)['cases']
    assert {case['requirementId'] for case in cases} == qa_ids
    assert all(case['selector'] == 'protected-signup' and case['toolName'] == 'run_unit_tests' for case in cases)


def test_dev_accepts_protected_files_and_cases():
    """dev 코드가 우리 설정을 그대로 받아 준다: 보호 파일 설정(UnitTestScope)과 보호 QA 케이스(QACaseBinding).
    범위별 설정 크기도 Host CLI 설정 한도 안에 여유 있게 들어간다."""
    from agents.roles.qa_contract import QACaseBinding
    from mcp_tools.tools.unit_config import UnitTestScope
    for scope in pt.SCOPES:
        config = pt.unit_test_scope(scope, suite_ref='artifact://protected-suite/scn-001/signup')
        UnitTestScope(**config)
        # Host CLI 설정 전체가 64KiB 이하여야 한다. 순환 안 범위는 여유를 크게 남긴다.
        assert len(json.dumps(config).encode('utf-8')) < 32 * 1024
    for case in pt.owned_qa_cases(CONTRACT)['cases']:
        QACaseBinding(tool_name=case['toolName'], selector=case['selector'], test_id=case['testId'],
                      requirement_id=UUID(case['requirementId']), title=case['title'],
                      expected_result=case['expectedResult'])


def test_protected_files_parse_as_python_310():
    """팀이 선언한 최소 버전(3.10) 문법으로 읽혀야 한다."""
    for content in pt.protected_files('final').values():
        ast.parse(content, feature_version=(3, 10))


def test_suite_fingerprint_follows_file_bytes(monkeypatch):
    """지문(suite SHA-256)은 같은 내용이면 같고, 범위가 다르거나 파일에 한 글자(줄바꿈)만 더해져도 달라진다."""
    first = pt.suite_sha256('final')
    assert first == pt.suite_sha256('final') and first != pt.suite_sha256('in-loop')
    original = pt.protected_files
    monkeypatch.setattr(pt, 'protected_files', lambda scope, suite=None: {
        path: content + ('\n' if path.endswith('signup_link.py') else '') for path, content in original(scope).items()})
    assert pt.suite_sha256('final') != first


@pytest.mark.parametrize('mutate, message', [
    (lambda s: s['checks'].append(dict(s['checks'][0])), 'Duplicate'),
    (lambda s: s['checks'][6].update(scopes=['in-loop', 'final']), 'Static'),
    (lambda s: s['checks'][4].update(scopes=['in-loop', 'final']), 'not part of its scope'),
    (lambda s: s['scopes']['final'].update(selector='protected-signup'), 'distinct'),
    (lambda s: s['scopes']['final']['files'].append('tests/signup/../secret.py'), 'directly'),
])
def test_broken_suite_is_rejected(tmp_path, mutate, message):
    """잘못된 suite.json(검사 중복, 정적 검사를 순환 안에 넣음, 범위 밖 테스트 모듈, 같은 선택자,
    폴더 밖 파일)은 읽을 때 ValueError로 거부된다."""
    suite = json.loads(pt.SUITE_FILE.read_text(encoding='utf-8'))
    mutate(suite)
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps(suite), encoding='utf-8')
    with pytest.raises(ValueError, match=message):
        pt.load_suite(path)


# ---------------------------------------------------------------- 결과 어댑터

CASES = {'m.C.test_a': uid(1), 'm.C.test_b': uid(2), 'm.C.test_c': uid(3)}


def report(*rows):
    """unittest-v1 보고서를 흉내 낸다. 줄은 (testId, outcome) 또는 (testId, outcome, details)이고 건수는 자동으로 맞춘다."""
    tests = [dict(zip(('testId', 'outcome', 'details'), row)) if len(row) == 3
             else {'testId': row[0], 'outcome': row[1]} for row in rows]
    count = {outcome: sum(t['outcome'] == outcome for t in tests) for outcome in ('PASS', 'FAIL', 'SKIP')}
    return {'format': 'unittest-v1', 'total': len(tests), 'passed': count['PASS'],
            'failed': count['FAIL'], 'skipped': count['SKIP'], 'tests': tests}


def by_id(results):
    """검사 결과 목록을 {검사 번호: 결과} 사전으로 바꾼다."""
    return {result.test_id: result for result in results}


def test_unit_report_outcomes_map_to_check_statuses():
    """PASS → PASS(증거 번호 붙음), FAIL → FAIL(실패 설명 유지),
    SKIP "UNVERIFIED:코드" → ERROR(그 코드, 증거 번호 없음)로 옮겨진다."""
    results = by_id(check_results_from_unit_report(
        report(('m.C.test_a', 'PASS'), ('m.C.test_b', 'FAIL', 'AssertionError: 7자 거부 안 됨'),
               ('m.C.test_c', 'SKIP', 'UNVERIFIED:APP_IMPORT_FAILED')),
        CASES, evidence_id=uid(9)))
    assert results[uid(1)].status == Status.PASS and results[uid(1)].evidence_ids == (uid(9),)
    assert results[uid(2)].status == Status.FAIL and '7자' in results[uid(2)].actual_result
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'APP_IMPORT_FAILED'
    assert results[uid(3)].evidence_ids == ()


def test_plain_skip_and_missing_case_are_unverified():
    """사유 없는 SKIP은 RUNNER_REPORTED_SKIP, 보고서에 없는 계획 검사는 PLANNED_CASE_NOT_REPORTED로 미검증이다."""
    results = by_id(check_results_from_unit_report(
        report(('m.C.test_a', 'SKIP', 'not supported'), ('m.C.test_b', 'PASS')), CASES, evidence_id=uid(9)))
    assert results[uid(1)].error_code == 'RUNNER_REPORTED_SKIP'
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'PLANNED_CASE_NOT_REPORTED'


@pytest.mark.parametrize('bad, code', [
    (None, 'TEST_RUNNER_ERROR'),
    ({'error': 'TEST_RUNNER_ERROR'}, 'UNIT_REPORT_INVALID'),
    (dict(report(('m.C.test_a', 'PASS')), passed=0), 'UNIT_REPORT_INVALID'),
    (report(('m.C.test_a', 'PASS'), ('m.C.test_a', 'PASS')), 'UNIT_REPORT_INVALID'),
    (report(('m.C.test_a', 'PASS'), ('setUpClass (m.C)', 'FAIL', 'boom')), 'UNIT_REPORT_UNBOUND_CASE'),
])
def test_untrusted_report_never_yields_pass(bad, code):
    """보고서가 없거나 형식이 틀리거나 대응표 밖의 줄이 섞이면, 통과한 줄이 있어도 모두 미검증이다."""
    results = check_results_from_unit_report(bad, CASES, evidence_id=uid(9))
    assert {result.status for result in results} == {Status.ERROR}
    assert {result.error_code for result in results} == {code}


def test_assessment_outcomes_map_to_check_statuses():
    """정적 판정의 PASS·FAIL·UNVERIFIED가 검사 상태로 옮겨지고, 모르는 outcome은 ASSESSMENT_INVALID 미검증이다."""
    requirements = {
        'REQ-003': {'checkId': 'SEC-SIGNUP-003', 'outcome': 'FAIL', 'basis': 'VIOLATION_FOUND'},
        'REQ-005': {'checkId': 'SEC-SIGNUP-001', 'outcome': 'PASS', 'basis': 'APPROVED_HASH_STORAGE'},
        'REQ-006': {'checkId': 'SEC-SIGNUP-004', 'outcome': 'UNVERIFIED', 'basis': 'ANALYSIS_TOO_COMPLEX'},
    }
    cases = {'SEC-SIGNUP-001': uid(1), 'SEC-SIGNUP-003': uid(2), 'SEC-SIGNUP-004': uid(3)}
    results = by_id(check_results_from_assessment(requirements, cases, evidence_id=uid(9)))
    assert results[uid(1)].status == Status.PASS and results[uid(1)].evidence_ids == (uid(9),)
    assert results[uid(2)].status == Status.FAIL
    assert results[uid(3)].status == Status.ERROR and results[uid(3)].error_code == 'ANALYSIS_TOO_COMPLEX'
    weird = {'REQ-005': {'checkId': 'SEC-SIGNUP-001', 'outcome': 'MAYBE', 'basis': 'X'}}
    assert check_results_from_assessment(weird, cases, evidence_id=uid(9))[0].error_code == 'ASSESSMENT_INVALID'


def evaluate(unit_outcome='PASS', static_outcome='PASS', static=True):
    """보호 테스트 결과(모두 unit_outcome)와 정적 판정 결과(모두 static_outcome)로 독립 평가 보고서를 만든다.
    static=False면 정적 판정을 넣지 않는다. Manifest는 기대값과 관측값을 같게 둔다."""
    inputs = pt.evaluation_inputs(CONTRACT)
    unit = report(*[(test_id, unit_outcome) + (('UNVERIFIED:APP_IMPORT_FAILED',) if unit_outcome == 'SKIP' else ())
                    for test_id in inputs['unit_cases']])
    requirements = {key: {'checkId': key, 'outcome': static_outcome, 'basis': 'BASIS'} for key in inputs['static_cases']}
    results = pt.final_check_results(inputs, unit_report=unit, unit_evidence_id=uid(50),
                                     assessment_requirements=requirements if static else None,
                                     static_evidence_id=uid(51))
    manifest = ExecutionManifest('repo', 2, uid(1), 'sha1', 'a' * 40, 'b' * 40, 'c' * 64,
                                 'sha256:' + 'd' * 64, 'sha256:' + 'e' * 64)
    return build_evaluation_report(
        evaluation_id=uid(60), source_run_id=uid(61), architecture='MULTI_AGENT',
        suite_id=inputs['suite_id'], suite_version=inputs['suite_version'], suite_sha256=inputs['suite_sha256'],
        created_at='2026-10-28T12:00:00+09:00', required_requirement_ids=inputs['required_requirement_ids'],
        expected=manifest, observed=manifest, plan=inputs['plan'], results=results, bindings=inputs['bindings'])


def test_final_inputs_build_an_independent_report():
    """최종 평가 입력으로 검사 11개·요구사항 7개 보고서가 만들어지고, 하나라도 FAIL이면 FAIL,
    미검증(SKIP·정적 UNVERIFIED·정적 판정 누락)이 있으면 UNVERIFIED가 된다."""
    passed = evaluate()
    assert passed['evaluationVerdict'] == 'PASS' and len(passed['checks']) == 11
    assert len(passed['requirements']) == 7  # REQ-008은 Orchestrator 검증이라 제외
    assert evaluate(unit_outcome='FAIL')['evaluationVerdict'] == 'FAIL'
    assert evaluate(unit_outcome='SKIP')['evaluationVerdict'] == 'UNVERIFIED'
    assert evaluate(static_outcome='UNVERIFIED')['evaluationVerdict'] == 'UNVERIFIED'
    assert evaluate(static=False)['evaluationVerdict'] == 'UNVERIFIED'  # 정적 판정 누락 → 미검증


# ---------------------------------------------------------------- 실행(최소 서비스와 결함 변형)

RUNTIME = all(importlib.util.find_spec(name) for name in ('fastapi', 'httpx', 'sqlalchemy', 'argon2'))
RUNNER = ROOT / 'src/mcp_tools/tools/unit_runner.py'
_RUN = '''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("trusted_unit_runner", sys.argv[1])
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
report, code = runner.run_tests(sys.argv[2], sys.argv[3], sys.argv[4])
sys.__stdout__.write(json.dumps({"report": report, "exit": code}))
'''

# 변형마다 FAIL이 되어야 하는 검사. 나머지 검사는 모두 PASS여야 한다(None이면 모두 미검증).
EXPECTED_FAILURES = {
    'good': set(),
    'plaintext_storage': {'SEC-SIGNUP-002'},
    'weak_argon2_memory': {'SEC-SIGNUP-002'},
    'fixed_salt': {'SEC-SIGNUP-002'},
    'seven_char_password': {'QA-SIGNUP-004', 'QA-SIGNUP-006'},
    'duplicates_allowed': {'QA-SIGNUP-003', 'QA-SIGNUP-005', 'QA-SIGNUP-006'},
    'case_sensitive_email': {'QA-SIGNUP-003'},
    # 공백 이메일을 정리하지 않고 거부해도 중복 계정은 생기지 않는다(정적 SEC-SIGNUP-003이 따로 FAIL을 낸다).
    'padded_email_rejected': set(),
    'invalid_email_accepted': {'QA-SIGNUP-002', 'QA-SIGNUP-006'},
    'validation_input_echoed': {'SEC-SIGNUP-005'},
    'password_logged': {'SEC-SIGNUP-005'},
    'hash_in_response': {'SEC-SIGNUP-005'},
    # 처리하지 못한 DB 오류: 중복이 500으로 응답되고, traceback의 SQL 파라미터로 해시가 로그에 남는다.
    'unhandled_duplicate': {'QA-SIGNUP-006', 'SEC-SIGNUP-005'},
    'password_printed': {'SEC-SIGNUP-005'},
    'isolated_logger': {'SEC-SIGNUP-005'},
    # 모든 가입이 422가 되는 서비스: 정상 가입 실패가 FAIL이고, 거부 검사·전제 조건이 필요한 검사는 미검증이다.
    'different_request_field': {'QA-SIGNUP-001', 'QA-SIGNUP-004', 'QA-SIGNUP-006'},
    'app_missing': None,
    'wrong_signup_path': None,
}
# 모두 미검증이 되는 변형의 근거 코드
UNVERIFIED_CODES = {'app_missing': {'APP_IMPORT_FAILED'}, 'wrong_signup_path': {'SIGNUP_ROUTE_NOT_FOUND'}}
# FAIL도 PASS도 아닌(미검증) 검사가 있는 변형
EXPECTED_UNVERIFIED = {'different_request_field': {'QA-SIGNUP-002', 'QA-SIGNUP-003', 'QA-SIGNUP-005',
                                                   'SEC-SIGNUP-002', 'SEC-SIGNUP-005'}}


def run_protected(variant):
    """합성 회원가입 서비스 변형(variant)을 임시 폴더에 쓰고, 최종 범위 보호 테스트를 dev unittest 실행기로 돌린다.
    돌려주는 값: {"report": unittest-v1 보고서, "exit": 종료 코드}. 믿을 수 있는 합성 코드만 이 방식으로 실행한다."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for path, content in signup_service.build(variant).items():
            (root / 'snapshot' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'snapshot' / path).write_text(content, encoding='utf-8')
        for path, content in pt.protected_files('final').items():
            (root / 'inputs' / path).parent.mkdir(parents=True, exist_ok=True)
            (root / 'inputs' / path).write_text(content, encoding='utf-8')
        # 변형마다 새 파이썬 프로세스에서 돌린다(앱 모듈 main·models가 프로세스에 남지 않게).
        completed = subprocess.run(
            [sys.executable, '-I', '-c', _RUN, str(RUNNER), str(root / 'inputs/tests/signup'),
             str(root / 'snapshot'), SUITE['scopes']['final']['pattern']],
            capture_output=True, text=True, timeout=600, cwd=root)
        return json.loads(completed.stdout)


@pytest.mark.skipif(not RUNTIME, reason='fastapi·httpx·sqlalchemy·argon2-cffi가 있어야 실행 시험을 돈다')
@pytest.mark.parametrize('variant', sorted(EXPECTED_FAILURES))
def test_protected_suite_catches_each_defect(variant):
    """변형마다 기대한 검사만 FAIL이고 나머지는 PASS다(EXPECTED_FAILURES). 연결이 안 되는 변형은 모두
    미검증이고 근거 코드가 맞다. 실패 설명에 해시 형식 글자가 남지 않는다."""
    output = run_protected(variant)
    inputs = pt.evaluation_inputs(CONTRACT)
    results = by_id(check_results_from_unit_report(output['report'], inputs['unit_cases'], evidence_id=uid(70)))
    names = {check['checkId']: check['key'] for check in SUITE['checks']}
    statuses = {names[check_id]: result.status for check_id, result in results.items()}
    expected = EXPECTED_FAILURES[variant]
    if expected is None:
        assert set(statuses.values()) == {Status.ERROR}
        assert {result.error_code for result in results.values()} == UNVERIFIED_CODES[variant]
        return
    assert {key for key, status in statuses.items() if status == Status.FAIL} == expected
    assert {key for key, status in statuses.items() if status == Status.ERROR} == EXPECTED_UNVERIFIED.get(variant, set())
    for result in results.values():  # 실패 설명에 해시 형식 글자가 남지 않는다.
        assert '$argon2' not in (result.actual_result or '')


# ---------------------------------------------------------------- 팀장 전달용 설정 내보내기


def test_export_writes_dev_ready_files(tmp_path):
    """export가 만든 세 파일을 dev 코드가 받아 주고, suite 참조·지문이 서로 맞는다.
    이미 있는 파일은 --force 없이 덮어쓰지 않고, 명령줄(main)의 --suite-ref가 반영된다."""
    from agents.roles.qa_contract import QACaseBinding
    from evaluation.protected_tests.__main__ import export, main
    from mcp_tools.tools.unit_config import UnitTestScope
    written = export(tmp_path, contract=CONTRACT)
    cases = json.loads(written['owned-qa-cases.local.json'].read_text(encoding='utf-8'))
    scope = json.loads(written['unit-protected-scope.json'].read_text(encoding='utf-8'))
    handoff = json.loads(written['handoff.json'].read_text(encoding='utf-8'))
    assert cases == pt.owned_qa_cases(CONTRACT) and len(cases['cases']) == 5
    UnitTestScope(**scope)
    for case in cases['cases']:
        QACaseBinding(tool_name=case['toolName'], selector=case['selector'], test_id=case['testId'],
                      requirement_id=UUID(case['requirementId']), title=case['title'], expected_result=case['expectedResult'])
    assert scope['protected_suite_ref'] == handoff['protectedSuiteRef'] and handoff['finalSuiteSha256'] == pt.suite_sha256('final')
    with pytest.raises(FileExistsError):
        export(tmp_path, contract=CONTRACT)  # 이미 있는 파일은 --force 없이 덮어쓰지 않는다.
    assert main(['export', '--out', str(tmp_path), '--force', '--suite-ref', 'artifact://protected-tests/x']) == 0
    assert json.loads(written['unit-protected-scope.json'].read_text(encoding='utf-8'))['protected_suite_ref'] == \
        'artifact://protected-tests/x'
