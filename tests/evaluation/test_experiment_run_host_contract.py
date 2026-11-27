"""측정 실행기 ↔ 실제 dev Host API 계약(dev 83·85·86번). 가짜 Host가 아니라 dev의 실제 앱·SQLite를 쓴다.

모델·Docker·네트워크는 쓰지 않는다(에이전트 전송은 아무 일도 하지 않는 대역). 확인하는 것:
실행기가 보내는 토큰·중복 방지 키를 dev가 받아 주는지, 실행기가 기록하는 요청 지문이 dev 접수 기록의 지문과 같은지,
응답을 잃은 제출의 복구가 실제 접수 조회로 같은 Run을 잇거나 같은 키로만 다시 보내는지.
"""
from uuid import uuid4

from pydantic import SecretStr
import pytest
from starlette.testclient import TestClient

from evaluation import experiment_run as er
from orchestrator.core.config import Settings
from orchestrator.domain.run_configuration import RunConfiguration
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app

TOKEN = 'evaluation-host-contract-fixture-token-not-a-real-key'
PLAN = {'experimentId': str(uuid4()), 'scenarioKey': 'SCN-001', 'warmupState': 'FRESH_WORKSPACE_COLD_START',
        'loopPolicy': 'FINAL_ONLY', 'requestText': '이메일과 비밀번호로 회원가입 기능을 만들어줘.'}
ROW = {'order': 1, 'architecture': 'MULTI_AGENT', 'repetition': 1}
ENTRY = {'api': 'http://host'}


class _Dispatcher:
    def __init__(self):
        self.run_ids = []

    async def dispatch_planner(self, run_id):
        self.run_ids.append(run_id)


@pytest.fixture
def host(tmp_path, monkeypatch):
    repository = SQLiteWorkflowRepository(tmp_path / 'host.sqlite3')
    approved = RunConfiguration.model_validate({
        'startingCommitHash': 'a' * 40, 'startingSnapshotSha256': 'b' * 64,
        'model': {'provider': 'openai', 'modelId': 'gpt-4.1-mini-2025-04-14', 'temperature': 0},
        'limits': {'runtimeBudgetMs': 60000}, 'scannerProfileRef': 'artifact://scanner/approved',
        'environment': {'containerImageDigest': 'sha256:' + 'c' * 64, 'dependencyLockHash': 'sha256:' + 'd' * 64,
                        'hardwareProfile': 'fixture-local'}})
    app = create_app(repository, dispatcher=_Dispatcher(), submission_validator=lambda configuration: None,
                     settings=Settings(_env_file=None, log_level='CRITICAL', database_path=str(tmp_path / 'host.sqlite3'),
                                       workspace_root=str(tmp_path / 'workspaces')))
    app.state.host_api_auth_required = True
    app.state.host_api_token = SecretStr(TOKEN)
    app.state.require_idempotency_key = True
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: approved)
    monkeypatch.setenv(er.HOST_TOKEN_ENV, TOKEN)

    def factory(api, token=True):
        # 실행기의 host_client와 같은 헤더(환경 변수의 토큰)로 실제 앱에 붙는다.
        headers = dict(er.host_client(api).headers) if token else {}
        return TestClient(app, base_url=api, headers={k: v for k, v in headers.items() if k.lower() == 'authorization'})

    def runs():
        with repository._connection() as connection:
            return connection.execute('SELECT count(*) FROM workflow_runs').fetchone()[0]
    return factory, runs, approved


def test_real_host_accepts_the_runner_token_and_key_and_replays_the_same_run(host):
    factory, runs, approved = host
    body, key = er.submission(PLAN, approved, 1), str(uuid4())
    with factory(ENTRY['api']) as client:
        first = er.submit(client, body, idempotency_key=key)
        again = er.submit(client, body, idempotency_key=key)  # dev는 같은 키·같은 본문을 200으로 재생한다
        assert first == again and runs() == 1
        lookup = client.get(f'/api/v1/submissions/{key}')
        assert lookup.status_code == 200
        # 실행기가 원장에 남기는 지문이 dev 접수 기록의 지문과 같아야 복구 때 대조할 수 있다.
        assert lookup.json()['requestSha256'] == er._host_request_hash(body)
        assert lookup.json()['runId'] == first and lookup.json()['status'] == 'CREATED'
        # 상태 조회도 같은 토큰으로 실제 경로·응답 칸을 읽는다(대역 Host라 Run은 진행하지 않으므로 대기 시간 초과).
        ticks = iter(range(0, 1000, 10))
        assert er.wait(client, first, timeout_seconds=30, clock=lambda: next(ticks), sleep=lambda _: None) == 'TIMEOUT'
        changed = {**body, 'requestText': '다른 요청'}
        with pytest.raises(er.ExperimentRunError, match='RUN_SUBMISSION_REJECTED_409'):
            er.submit(client, changed, idempotency_key=key)
        with pytest.raises(er.ExperimentRunError, match='RUN_SUBMISSION_REJECTED_422'):
            er.submit(client, body)  # 키 없는 생성은 실제 Host가 거부한다
    with factory(ENTRY['api'], token=False) as client:
        with pytest.raises(er.ExperimentRunError, match='RUN_SUBMISSION_REJECTED_401'):
            er.submit(client, body, idempotency_key=str(uuid4()))
    assert runs() == 1


@pytest.mark.parametrize('accepted', [True, False])
def test_reconciliation_against_the_real_lookup_never_creates_a_second_run(host, tmp_path, accepted):
    factory, runs, approved = host
    body, key = er.submission(PLAN, approved, ROW['order']), str(uuid4())
    state = {'idempotencyKey': key, 'hostRequestSha256': er._host_request_hash(body),
             'submissionSha256': er._submission_hash(body)}
    original = None
    if accepted:  # 서버는 접수했고 응답만 잃은 경우
        with factory(ENTRY['api']) as client:
            original = er.submit(client, body, idempotency_key=key)
    run_id = er._reconcile_submission(PLAN, ROW, state, ENTRY, tmp_path, factory)
    assert runs() == 1 and (original is None or run_id == original)
    # 다시 복구해도 같은 Run이다.
    assert er._reconcile_submission(PLAN, ROW, state, ENTRY, tmp_path, factory) == run_id and runs() == 1


def test_reconciliation_refuses_a_receipt_for_a_different_request(host, tmp_path):
    factory, runs, approved = host
    body, key = er.submission(PLAN, approved, ROW['order']), str(uuid4())
    with factory(ENTRY['api']) as client:
        er.submit(client, {**body, 'requestText': '다른 요청'}, idempotency_key=key)
    state = {'idempotencyKey': key, 'hostRequestSha256': er._host_request_hash(body),
             'submissionSha256': er._submission_hash(body)}
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_RECONCILIATION_CONFLICT'):
        er._reconcile_submission(PLAN, ROW, state, ENTRY, tmp_path, factory)
    assert runs() == 1
