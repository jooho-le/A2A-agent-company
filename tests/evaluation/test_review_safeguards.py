"""외부 점검(2026-10-28)에서 재현한 오판의 회귀 검사와, 고친 뒤에도 정상 동작은 그대로인지 대조한다.

확인하는 것:
  - 서비스 시작(startup)이 실패하면 보호 검사가 PASS가 되지 않고, 시작 때 준비한 값은 요청에서 쓸 수 있는가?
  - passlib CryptContext의 category별 설정(기준 미만 메모리 등)을 기본 설정으로 추정해 PASS를 내지 않는가?
  - 실행기 종료 코드와 보고서가 어긋나거나(강제 종료 137 등) 실행기 오류가 있으면 보고서를 쓰지 않는가?
  - 다른 Run의 사건·Issue가 섞이거나 최종 코드 버전의 검증 사건이 없으면 추적 검사가 PASS가 되지 않는가?
  - Security 증거가 없는 버전을 요약 흐름 표에서 "문제 없음"으로 적지 않는가?
  - 대상 Run 번호·코드 버전·Trace 문서가 서로 어긋나면 실행 전에 멈추는가?
  - (2026-10-29 점검) 대문자·공백 이메일 재가입이 500이어도 최종 PASS가 나오지 않는가?(F1) 최종 채점기 결과를 다른 Run 자료와
    섞어 요약하지 않는가?(F4) 알려 준 시험 비밀값이 최종 채점기의 노출 검사까지 전달되고 결과에는 값이 남지 않는가?
실행 시험은 sqlalchemy·argon2-cffi가 있어야 돈다(없으면 건너뜀). 믿을 수 있는 합성 서비스 코드만 로컬에서 실행하고,
Docker 호출은 흉내 낸다.
"""
import copy
import json
import subprocess

import pytest
from tests.evaluation.test_trace_checks import RUNS, QA_FIX, CONTRACT
from tests.evaluation.test_final_evaluation import snapshot, manifest, final, IMAGE
from tests.evaluation.test_protected_signup import run_protected, RUNTIME
from tests.evaluation import signup_service
from tests.evaluation.signup_examples import GOOD
from tests.evaluation.test_security_assess import make_request
from evaluation import protected_tests as pt
from evaluation.final_evaluation import DockerRunner
from evaluation.security.assess import evaluate
from evaluation.summary import _flow, render_summary
from evaluation.trace_checks import check_traceability


@pytest.mark.skipif(not RUNTIME, reason="합성 서비스 실행 의존성 필요")
def test_startup_failure_must_not_pass_all_protected_checks(monkeypatch):
    """서비스 시작(startup)이 실패하면 보호 테스트 9개가 모두 SERVICE_SETUP_FAILED 미검증(SKIP)이다.
    (시작 단계를 건너뛰고 요청만 보내 PASS가 되지 않게 한다.)"""
    original = signup_service.build
    def broken_startup(variant):
        """정상 서비스에 시작할 때 예외를 내는 startup 처리기를 붙인다."""
        files = original(variant)
        files['main.py'] += '\n@app.on_event("startup")\nasync def startup():\n    raise RuntimeError("synthetic startup failure")\n'
        return files
    monkeypatch.setattr(signup_service, 'build', broken_startup)
    result = run_protected('good')
    assert result['report']['passed'] == 0
    assert result['report']['skipped'] == 9
    assert all(row['details'] == 'UNVERIFIED:SERVICE_SETUP_FAILED' for row in result['report']['tests'])


def test_passlib_category_below_minimum_must_not_pass():
    """CryptContext의 admin category 설정이 기준 미만 메모리인데 category="admin"으로 해시하면 PASS가 아니다."""
    code = '''from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["argon2"], admin__argon2__memory_cost=1024)
def hash_password(password: str) -> str:
    return pwd_context.hash(password, category="admin")
'''
    requirement = json.loads(evaluate(make_request(dict(GOOD, **{'security.py': code})))['evidence_json'])['requirements']['REQ-005']
    assert requirement['outcome'] != 'PASS'


def test_killed_docker_process_must_not_produce_pass():
    """컨테이너가 강제 종료(137)되면 출력에 전부 PASS인 보고서가 있어도 최종 판정이 PASS가 아니다."""
    class KilledDocker(DockerRunner):
        """시험 대역: 이미지 확인은 성공하고, 실행은 전부 PASS 보고서를 출력한 뒤 종료 코드 137로 끝난다."""
        def _call(self, args, timeout):
            if args[0] == 'image':
                return subprocess.CompletedProcess(args, 0, json.dumps([{'Os': 'linux', 'Id': IMAGE}]), '')
            rows = [{'testId': test_id, 'outcome': 'PASS'} for test_id in pt.evaluation_inputs(CONTRACT, 'final')['unit_cases']]
            payload = {'format': 'unittest-v1', 'total': len(rows), 'passed': len(rows), 'failed': 0, 'skipped': 0, 'tests': rows}
            return subprocess.CompletedProcess(args, 137, json.dumps(payload)+'\n', '')
    archive = snapshot()
    result = final(archive, manifest(archive), KilledDocker(IMAGE))
    assert result['runner']['exitCode'] == 137
    assert result['report']['evaluationVerdict'] != 'PASS'


def test_foreign_run_events_must_not_pass():
    """Run 번호가 사건·Issue의 Run 번호와 다르면(다른 Run 기록) 추적 검사가 PASS가 아니다."""
    bundle = copy.deepcopy(RUNS[QA_FIX])
    bundle['run']['runId'] = '00000000-0000-4000-8000-000000000099'
    assert check_traceability(bundle, CONTRACT).status != 'PASS'


def test_missing_final_validation_events_must_not_pass():
    """최종 코드 버전의 검증 종료 사건이 없으면 추적 검사가 PASS가 아니다(최종 요구사항 검증을 확인할 수 없음)."""
    bundle = copy.deepcopy(RUNS[QA_FIX])
    bundle['events'] = [event for event in bundle['events'] if not (
        event.get('eventType') == 'VALIDATION_FINISHED' and event.get('codeVersion') == bundle['run']['codeVersion'])]
    bundle['eventsTotal'] = len(bundle['events'])
    assert check_traceability(bundle, CONTRACT).status != 'PASS'


def test_missing_security_proof_must_not_be_shown_as_no_problem():
    """Security 증거가 없어 HUMAN_REVIEW로 끝난 Run의 흐름 표에서, 검증 칸을 "문제 없음"으로 적지 않는다."""
    bundle = RUNS['initial_build_failure_is_fixed_then_missing_security_proof_stays_unverified']
    assert bundle['run']['verdict'] == 'HUMAN_REVIEW'
    text = '\n'.join(_flow(bundle, {}))
    assert '| v2 | PASS | 문제 없음 | 문제 없음 |' not in text


@pytest.mark.skipif(not RUNTIME, reason="합성 서비스 실행 의존성 필요")
def test_startup_initialization_is_available_to_requests(monkeypatch):
    """보호 테스트가 실제 서버처럼 startup을 실행해서, 시작 때 준비한 값을 가입 요청이 쓸 수 있다(정상 서비스는 9개 PASS)."""
    original = signup_service.build
    def with_startup(variant):
        """정상 서비스에 startup 처리기를 붙이고, 가입 처리에서 그 처리기가 준비한 값을 확인하게 바꾼다."""
        files = original(variant)
        files['main.py'] += '\n@app.on_event("startup")\nasync def startup():\n    app.state.ready = True\n'
        files['routers/auth.py'] = files['routers/auth.py'].replace(
            '    user = User(', '    from main import app\n    assert app.state.ready\n    user = User(')
        return files
    monkeypatch.setattr(signup_service, 'build', with_startup)
    result = run_protected('good')
    assert result['report']['passed'] == 9


@pytest.mark.parametrize('call', [
    'pwd_context.hash(password, category="admin")',
    'pwd_context.hash(password, **{"category": "admin"})',
    'pwd_context.hash(password)',
])
def test_category_policies_cannot_be_inferred_from_defaults(call):
    """category별 설정이 있는 CryptContext는 어떤 호출 방식이든(category 지정·**kwargs·생략)
    어떤 설정으로 해시하는지 정적으로 확정할 수 없으므로 UNVERIFIED다."""
    code = ('from passlib.context import CryptContext\n'
            'pwd_context = CryptContext(schemes=["argon2"], admin__argon2__memory_cost=1024)\n'
            'def hash_password(password: str) -> str:\n    return ' + call + '\n')
    requirement = json.loads(evaluate(make_request(dict(GOOD, **{'security.py': code})))['evidence_json'])['requirements']['REQ-005']
    assert requirement['outcome'] == 'UNVERIFIED'


@pytest.mark.parametrize('exit_code, outcome, error, expected_error', [
    (0, 'PASS', None, None),
    (1, 'FAIL', None, None),
    (0, 'SKIP', None, None),
    (1, 'PASS', None, 'RUNNER_EXIT_REPORT_MISMATCH'),
    (0, 'FAIL', None, 'RUNNER_EXIT_REPORT_MISMATCH'),
    (2, 'PASS', None, 'RUNNER_EXIT_INVALID'),
    (137, 'PASS', None, 'RUNNER_EXIT_INVALID'),
    (-9, 'PASS', None, 'RUNNER_EXIT_INVALID'),
    (None, 'PASS', None, 'RUNNER_EXIT_INVALID'),
    (False, 'PASS', None, 'RUNNER_EXIT_INVALID'),
    (0, 'PASS', 'TIMEOUT', 'TIMEOUT'),
])
def test_runner_exit_and_report_agreement(exit_code, outcome, error, expected_error):
    """실행기 종료 코드와 보고서가 맞을 때만 보고서를 쓴다: 0은 FAIL 없음, 1은 FAIL 있음.
    그 밖의 종료 코드(2·137·음수·None·False)나 실행기 오류가 있으면 보고서를 버리고 오류 코드를 남긴다."""
    from evaluation.final_evaluation import RunnerOutcome, _checked_outcome
    report = {'format': 'unittest-v1', 'total': 1, 'passed': int(outcome == 'PASS'),
              'failed': int(outcome == 'FAIL'), 'skipped': int(outcome == 'SKIP'),
              'tests': [{'testId': 'm.C.test_a', 'outcome': outcome}]}
    result = _checked_outcome(RunnerOutcome(report, exit_code, error, IMAGE, 'docker'))
    assert result.error == expected_error
    assert result.report == (None if expected_error else report)


def test_final_evaluation_rechecks_custom_runner_error():
    """직접 만든 실행기가 오류(TIMEOUT)와 전부 PASS 보고서를 함께 돌려줘도 최종 채점기가 다시 확인해 UNVERIFIED로 둔다."""
    from evaluation.final_evaluation import RunnerOutcome
    class FailedRunner:
        """시험 대역: 전부 PASS인 보고서와 TIMEOUT 오류를 함께 돌려주는 실행기"""
        kind = 'docker'
        def run(self, *args, **kwargs):
            rows = [{'testId': key, 'outcome': 'PASS'} for key in pt.evaluation_inputs(CONTRACT, 'final')['unit_cases']]
            return RunnerOutcome({'format': 'unittest-v1', 'total': len(rows), 'passed': len(rows),
                                  'failed': 0, 'skipped': 0, 'tests': rows}, 0, 'TIMEOUT', IMAGE, self.kind)
    archive = snapshot()
    result = final(archive, manifest(archive), FailedRunner())
    assert result['report']['evaluationVerdict'] == 'UNVERIFIED'
    assert all(check['status'] == 'ERROR' for check in result['report']['checks'] if check['errorCode'] == 'TIMEOUT')


@pytest.mark.parametrize('section', ['events', 'issues'])
@pytest.mark.parametrize('missing', [True, False])
def test_trace_rows_need_matching_run_identity(section, missing):
    """사건·Issue 줄에 Run 번호가 없거나 다른 Run 번호가 있으면 추적 검사는 TRACE_RUN_MISMATCH 미검증이다."""
    bundle = copy.deepcopy(RUNS[QA_FIX])
    if missing:
        bundle[section][0].pop('runId')
    else:
        bundle[section][0]['runId'] = '00000000-0000-4000-8000-000000000099'
    result = check_traceability(bundle, CONTRACT)
    assert result.status == 'UNVERIFIED' and result.reasons == ('TRACE_RUN_MISMATCH',)


@pytest.mark.parametrize('change', ['run', 'version', 'trace'])
def test_final_evaluation_rejects_foreign_metadata_before_execution(change):
    """대상 Run 번호·코드 버전·Trace 문서의 Run 번호가 서로 어긋나면 실행기를 부르기 전에 ValueError로 멈춘다."""
    from tests.evaluation.test_final_evaluation import RecordingRunner, uid
    archive, runner = snapshot(), RecordingRunner()
    bundle = copy.deepcopy(RUNS[QA_FIX])
    kwargs = {'source_run_id': bundle['run']['runId'], 'run_bundle': bundle}
    if change == 'run':
        kwargs['source_run_id'] = uid(2)
    elif change == 'version':
        bundle['run']['codeVersion'] += 1
    else:
        kwargs['trace_documents'] = {'runId': uid(2)}
    with pytest.raises(ValueError):
        final(archive, manifest(archive), runner, **kwargs)
    assert runner.calls == 0


# ---------------------------------------------------------------- 외부 점검(2026-10-29)

@pytest.mark.skipif(not RUNTIME, reason="합성 서비스 실행 의존성 필요")
def test_server_error_on_email_variants_is_not_a_final_pass():
    """F1: 대문자·앞뒤 공백 이메일에서만 500이 나는 서비스는 QA-SIGNUP-003 FAIL, 최종 판정 FAIL이다.
    (중복 계정은 생기지 않지만 기준표 4절의 "중복은 4xx 거부"를 어긴다. 같은 원인으로 공백만 있는 이메일 거부(QA-002),
    거부 뒤 상태(QA-006), 정규화 저장(SEC-003)도 FAIL이다.)"""
    from tests.evaluation.test_final_evaluation import LOCK, VerifiedImageRunner, canonical_tar, statuses
    files = signup_service.build('variant_email_server_error')
    archive = canonical_tar({**{name: text.encode('utf-8') for name, text in files.items()}, 'requirements.lock': LOCK})
    output = final(archive, manifest(archive), VerifiedImageRunner())
    result = statuses(output)
    assert result['QA-SIGNUP-003'] == 'FAIL' and output['report']['evaluationVerdict'] == 'FAIL'
    assert {key for key, status in result.items() if status != 'PASS'} == {
        'QA-SIGNUP-002', 'QA-SIGNUP-003', 'QA-SIGNUP-006', 'SEC-SIGNUP-003'}


def test_final_output_carries_its_sources_into_the_summary():
    """F4: 최종 채점기 결과의 trace·runner에는 출처(Run 번호·평가 번호)가 붙어 그대로 요약할 수 있고,
    다른 Run의 흐름 자료를 함께 넣으면 요약을 만들지 않는다."""
    from tests.evaluation.test_final_evaluation import RecordingRunner
    archive = snapshot()
    bundle = copy.deepcopy(RUNS[QA_FIX])
    output = final(archive, manifest(archive), RecordingRunner(), source_run_id=bundle['run']['runId'], run_bundle=bundle)
    assert output['trace']['runId'] == bundle['run']['runId']
    assert output['runner']['evaluationId'] == output['report']['evaluationId']
    text = render_summary(output['report'], scenario_contract=CONTRACT, run_bundle=bundle, trace=output['trace'],
                          runner=output['runner'])
    assert '추적 가능성(REQ-008)' in text
    other = copy.deepcopy(RUNS['repeated_issue_stops_after_two_unsolved_fix_cycles'])
    with pytest.raises(ValueError):
        render_summary(output['report'], scenario_contract=CONTRACT, run_bundle=other)


def test_known_secret_reaches_final_exposure_check_without_its_value():
    """알려 준 시험 비밀값을 최종 채점기에 넘기면 Run 기록의 자유 글에서도 찾아 FAIL이고, 결과 어디에도 값이 남지 않는다.
    노출 검사 결과에는 검사 범위(읽은 기록·보지 못한 출력·알려 준 값 개수)가 붙는다."""
    from tests.evaluation.test_final_evaluation import RecordingRunner, uid
    from tests.evaluation.test_trace_checks import documents
    secret = 'Audit' + 'SyntheticValue2948'
    trace = documents(events=[{'runId': uid(2), 'message': 'received input: ' + secret}], eventsTotal=1)
    trace['runId'] = uid(2)
    archive = snapshot()
    output = final(archive, manifest(archive), RecordingRunner(), trace_documents=trace, known_values=[secret])
    exposure = output['trace']['exposure']
    assert exposure['status'] == 'FAIL' and 'KNOWN_SECRET_VALUE' in exposure['reasons']
    assert exposure['coverage']['knownValues'] == 1 and 'A2A_MESSAGE_BODY' in exposure['coverage']['notCollectedOutputs']
    assert secret not in json.dumps(output, ensure_ascii=False, default=str)
    without = final(archive, manifest(archive), RecordingRunner(), trace_documents=trace)
    assert without['trace']['exposure']['status'] == 'PASS'  # 값을 모르면 자유 글 속 비밀번호는 찾지 못한다(한계)
