"""Independent review regressions. 57eba00에서 재현한 오판의 회귀 검사와 정상 동작 대조.
Run with repo importable and pytest, sqlalchemy, argon2-cffi available.
Only trusted synthetic service code is executed locally. Docker calls are simulated.
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
from evaluation.summary import _flow
from evaluation.trace_checks import check_traceability


@pytest.mark.skipif(not RUNTIME, reason="합성 서비스 실행 의존성 필요")
def test_startup_failure_must_not_pass_all_protected_checks(monkeypatch):
    original = signup_service.build
    def broken_startup(variant):
        files = original(variant)
        files['main.py'] += '\n@app.on_event("startup")\nasync def startup():\n    raise RuntimeError("synthetic startup failure")\n'
        return files
    monkeypatch.setattr(signup_service, 'build', broken_startup)
    result = run_protected('good')
    assert result['report']['passed'] == 0
    assert result['report']['skipped'] == 8
    assert all(row['details'] == 'UNVERIFIED:SERVICE_SETUP_FAILED' for row in result['report']['tests'])


def test_passlib_category_below_minimum_must_not_pass():
    code = '''from passlib.context import CryptContext
pwd_context = CryptContext(schemes=["argon2"], admin__argon2__memory_cost=1024)
def hash_password(password: str) -> str:
    return pwd_context.hash(password, category="admin")
'''
    requirement = json.loads(evaluate(make_request(dict(GOOD, **{'security.py': code})))['evidence_json'])['requirements']['REQ-005']
    assert requirement['outcome'] != 'PASS'


def test_killed_docker_process_must_not_produce_pass():
    class KilledDocker(DockerRunner):
        def _call(self, args, timeout):
            if args[0] == 'image':
                return subprocess.CompletedProcess(args, 0, json.dumps([{'Os': 'linux', 'Id': IMAGE}]), '')
            rows = [{'testId': test_id, 'outcome': 'PASS'} for test_id in pt.evaluation_inputs(CONTRACT)['unit_cases']]
            payload = {'format': 'unittest-v1', 'total': len(rows), 'passed': len(rows), 'failed': 0, 'skipped': 0, 'tests': rows}
            return subprocess.CompletedProcess(args, 137, json.dumps(payload)+'\n', '')
    archive = snapshot()
    result = final(archive, manifest(archive), KilledDocker(IMAGE))
    assert result['runner']['exitCode'] == 137
    assert result['report']['evaluationVerdict'] != 'PASS'


def test_foreign_run_events_must_not_pass():
    bundle = copy.deepcopy(RUNS[QA_FIX])
    bundle['run']['runId'] = '00000000-0000-4000-8000-000000000099'
    assert check_traceability(bundle, CONTRACT).status != 'PASS'


def test_missing_final_validation_events_must_not_pass():
    bundle = copy.deepcopy(RUNS[QA_FIX])
    bundle['events'] = [event for event in bundle['events'] if not (
        event.get('eventType') == 'VALIDATION_FINISHED' and event.get('codeVersion') == bundle['run']['codeVersion'])]
    bundle['eventsTotal'] = len(bundle['events'])
    assert check_traceability(bundle, CONTRACT).status != 'PASS'


def test_missing_security_proof_must_not_be_shown_as_no_problem():
    bundle = RUNS['initial_build_failure_is_fixed_then_missing_security_proof_stays_unverified']
    assert bundle['run']['verdict'] == 'HUMAN_REVIEW'
    text = '\n'.join(_flow(bundle, {}))
    assert '| v2 | PASS | 문제 없음 | 문제 없음 |' not in text


@pytest.mark.skipif(not RUNTIME, reason="합성 서비스 실행 의존성 필요")
def test_startup_initialization_is_available_to_requests(monkeypatch):
    original = signup_service.build
    def with_startup(variant):
        files = original(variant)
        files['main.py'] += '\n@app.on_event("startup")\nasync def startup():\n    app.state.ready = True\n'
        files['routers/auth.py'] = files['routers/auth.py'].replace(
            '    user = User(', '    from main import app\n    assert app.state.ready\n    user = User(')
        return files
    monkeypatch.setattr(signup_service, 'build', with_startup)
    result = run_protected('good')
    assert result['report']['passed'] == 8


@pytest.mark.parametrize('call', [
    'pwd_context.hash(password, category="admin")',
    'pwd_context.hash(password, **{"category": "admin"})',
    'pwd_context.hash(password)',
])
def test_category_policies_cannot_be_inferred_from_defaults(call):
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
    from evaluation.final_evaluation import RunnerOutcome, _checked_outcome
    report = {'format': 'unittest-v1', 'total': 1, 'passed': int(outcome == 'PASS'),
              'failed': int(outcome == 'FAIL'), 'skipped': int(outcome == 'SKIP'),
              'tests': [{'testId': 'm.C.test_a', 'outcome': outcome}]}
    result = _checked_outcome(RunnerOutcome(report, exit_code, error, IMAGE, 'docker'))
    assert result.error == expected_error
    assert result.report == (None if expected_error else report)


def test_final_evaluation_rechecks_custom_runner_error():
    from evaluation.final_evaluation import RunnerOutcome
    class FailedRunner:
        kind = 'docker'
        def run(self, *args):
            rows = [{'testId': key, 'outcome': 'PASS'} for key in pt.evaluation_inputs(CONTRACT)['unit_cases']]
            return RunnerOutcome({'format': 'unittest-v1', 'total': len(rows), 'passed': len(rows),
                                  'failed': 0, 'skipped': 0, 'tests': rows}, 0, 'TIMEOUT', IMAGE, self.kind)
    archive = snapshot()
    result = final(archive, manifest(archive), FailedRunner())
    assert result['report']['evaluationVerdict'] == 'UNVERIFIED'
    assert all(check['status'] == 'ERROR' for check in result['report']['checks'] if check['errorCode'] == 'TIMEOUT')


@pytest.mark.parametrize('section', ['events', 'issues'])
@pytest.mark.parametrize('missing', [True, False])
def test_trace_rows_need_matching_run_identity(section, missing):
    bundle = copy.deepcopy(RUNS[QA_FIX])
    if missing:
        bundle[section][0].pop('runId')
    else:
        bundle[section][0]['runId'] = '00000000-0000-4000-8000-000000000099'
    result = check_traceability(bundle, CONTRACT)
    assert result.status == 'UNVERIFIED' and result.reasons == ('TRACE_RUN_MISMATCH',)


@pytest.mark.parametrize('change', ['run', 'version', 'trace'])
def test_final_evaluation_rejects_foreign_metadata_before_execution(change):
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
