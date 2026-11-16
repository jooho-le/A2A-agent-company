"""측정 Run 실행 도구(evaluation.experiment_run)를 시험한다. 실제 Host·모델 대신 dev 설정 검사와 가짜 HTTP를 쓴다.

확인하는 것:
  - 실행 순서: 반복마다 구조 순서를 번갈아 매기고(11-A), 무작위 순서는 시드를 남겨 다시 만들 수 있다.
  - 제출 본문: dev Host 승인 설정에 experimentId·executionOrder·warmupState만 더한다. 승인 설정의 보호 테스트 참조가
    계획 버전(Ver1·Ver2)과 다르면 제출하지 않는다.
  - 대기: 끝·멈춤 상태에서 멈추고, 멈춘 Run에 재개 요청을 보내지 않는다. 시간이 지나면 TIMEOUT(취소하지 않음).
  - 인계: runtime 설정의 DB 경로로 dev evaluation_export와 같은 저장소 인자를 만들고 runs.jsonl에 결과를 남긴다.
"""
import json
import hashlib
import shutil
import sys
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from evaluation import experiment_run as er

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT = str(uuid4())


def _plan(**changes):
    plan = {'experimentId': EXPERIMENT, 'scenarioKey': 'SCN-001', 'loopPolicy': 'FINAL_ONLY',
            'executionOrder': 'ALTERNATING', 'warmupState': 'COLD_FRESH_WORKSPACE',
            'plannedRuns': {'SINGLE_AGENT': 5, 'MULTI_AGENT': 5},
            'hostConfigurations': {name: {'api': f'http://127.0.0.1:{port}', 'runtime': f'{name}-runtime.json',
                                          'tools': 'tools.json', 'source': 'source.json'}
                                   for name, port in (('SINGLE_AGENT', 8010), ('MULTI_AGENT', 8000))}}
    plan.update(changes)
    return plan


def _approved(protected_ref=None, budget=3_600_000, architecture='MULTI_AGENT'):
    from orchestrator.domain.run_configuration import RunConfiguration
    return RunConfiguration.model_validate({
        'architecture': architecture, 'startingCommitHash': 'a' * 40, 'startingSnapshotSha256': 'b' * 64,
        'model': {'provider': 'openai', 'modelId': 'gpt-4.1-mini-2025-04-14', 'temperature': 0},
        'limits': {'runtimeBudgetMs': budget},
        'environment': {'containerImageDigest': 'sha256:' + 'c' * 64, 'dependencyLockHash': 'sha256:' + 'd' * 64,
                        'hardwareProfile': 'fixture', 'networkPolicy': 'DENY'},
        'scannerProfileRef': 'approved-bandit', 'protectedTestSuiteRef': protected_ref})


# ---------------------------------------------------------------- 실행 순서

def test_alternating_order_flips_structure_each_repetition():
    planned = er.schedule(_plan())
    names = [row['architecture'][0] for row in planned['runs']]
    assert names == list('SMMSSMMSSM') and planned['seed'] is None
    assert [row['order'] for row in planned['runs']] == list(range(1, 11))
    assert all(row['loopPolicy'] == 'FINAL_ONLY' for row in planned['runs'])
    assert [row['repetition'] for row in planned['runs'] if row['architecture'] == 'MULTI_AGENT'] == [1, 2, 3, 4, 5]


def test_unequal_counts_keep_alternation_until_one_structure_ends():
    planned = er.schedule(_plan(plannedRuns={'SINGLE_AGENT': 2, 'MULTI_AGENT': 3}))
    assert [row['architecture'][0] for row in planned['runs']] == list('SMMSM')


def test_randomized_order_records_a_reproducible_seed():
    first = er.schedule(_plan(executionOrder='RANDOMIZED_RECORDED'))
    again = er.schedule(_plan(executionOrder='RANDOMIZED_RECORDED'))
    assert first == again and first['seed'] is not None
    key = lambda row: (row['architecture'], row['repetition'])  # noqa: E731
    assert sorted(map(key, first['runs'])) == sorted(map(key, er.schedule(_plan())['runs']))


@pytest.mark.parametrize('changes, code', [
    ({'plannedRuns': {'SINGLE_AGENT': 0, 'MULTI_AGENT': 5}}, 'PLANNED_RUN_COUNTS_REQUIRED'),
    ({'loopPolicy': None}, 'LOOP_POLICY_REQUIRED'),
    ({'experimentId': 'not-a-uuid'}, 'EXPERIMENT_ID_REQUIRED'),
    ({'executionOrder': 'MANUAL'}, 'EXECUTION_ORDER_REQUIRED'),
])
def test_schedule_rejects_incomplete_plans(changes, code):
    with pytest.raises(er.ExperimentRunError, match=code):
        er.schedule(_plan(**changes))


@pytest.mark.parametrize('contents', ['[]', 'null', '42', '"invalid"', '{broken'])
def test_cli_rejects_non_object_or_broken_plan_without_traceback(tmp_path, capsys, contents):
    path = tmp_path / 'plan.json'
    path.write_text(contents)
    assert er.main(['schedule', '--plan', str(path)]) == 1
    assert capsys.readouterr().err.strip() in ('PLAN_INVALID', 'EXPERIMENT_INPUT_INVALID')


def test_cli_missing_plan_is_a_controlled_input_error(tmp_path, capsys):
    assert er.main(['schedule', '--plan', str(tmp_path / 'missing.json')]) == 1
    assert capsys.readouterr().err.strip() == 'EXPERIMENT_INPUT_INVALID'


@pytest.mark.parametrize('key', ['hostConfigurations', 'model', 'limits', 'source'])
def test_malformed_plan_sections_are_rejected_before_submission(tmp_path, monkeypatch, key):
    plan = _matching_plan(**{key: ['invalid']})
    _runtime_files(tmp_path)
    monkeypatch.setattr(er, 'approved_configuration', lambda *args: _approved(architecture='SINGLE_AGENT'))
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='PLAN_INVALID|HOST_CONFIGURATION_REQUIRED'):
        er.run_one(plan, er.schedule(plan)['runs'][0], tmp_path / 'execution', tmp_path, client_factory=host.client)
    assert host.requests == []
    assert not (tmp_path / 'execution' / er.LEDGER).exists()


@pytest.mark.parametrize('interval', [-1, 0, float('nan'), float('inf')])
def test_invalid_poll_interval_cannot_start_a_run(tmp_path, interval):
    plan = _matching_plan()
    host = _Host(['PLANNING'])
    with pytest.raises(er.ExperimentRunError, match='POLL_INTERVAL_INVALID'):
        er.run_one(plan, er.schedule(plan)['runs'][0], tmp_path / 'execution', tmp_path,
                   client_factory=host.client, poll_seconds=interval)
    assert host.requests == [] and not (tmp_path / 'execution').exists()


# ---------------------------------------------------------------- 제출 본문

def test_submission_adds_only_the_experiment_declaration():
    approved = _approved()
    body = er.submission(_plan(), approved, order=3)
    configuration = body['configuration']
    assert (configuration['experimentId'], configuration['executionOrder'], configuration['warmupState']) == (
        EXPERIMENT, 3, 'COLD_FRESH_WORKSPACE')
    expected = approved.model_dump(mode='json', by_alias=True)
    assert {key: value for key, value in configuration.items()
            if key not in ('experimentId', 'executionOrder', 'warmupState')} == {
        key: value for key, value in expected.items() if key not in ('experimentId', 'executionOrder', 'warmupState')}
    assert body['requestText'] == er.DEFAULT_REQUEST_TEXT
    assert er.submission(_plan(requestText='같은 요청문'), approved, order=1)['requestText'] == '같은 요청문'


def test_submission_refuses_an_incomplete_baseline_or_missing_warmup():
    with pytest.raises(er.ExperimentRunError, match='EXPERIMENT_BASELINE_INCOMPLETE'):
        er.submission(_plan(), _approved(budget=None), order=1)
    with pytest.raises(er.ExperimentRunError, match='WARMUP_STATE_REQUIRED'):
        er.submission(_plan(warmupState=''), _approved(), order=1)


@pytest.mark.parametrize('policy, reference, ok', [
    ('IN_LOOP_AND_FINAL', 'artifact://protected', True), ('IN_LOOP_AND_FINAL', None, False),
    ('FINAL_ONLY', None, True), ('FINAL_ONLY', 'artifact://protected', False),
])
def test_loop_policy_must_match_the_host_protected_reference(monkeypatch, tmp_path, policy, reference, ok):
    from types import SimpleNamespace
    import agents.platform.host as host
    monkeypatch.setattr(host, 'inspect_host_configuration',
                        lambda **_: SimpleNamespace(approved_configuration=_approved(protected_ref=reference)))
    plan = _plan(loopPolicy=policy)
    if ok:
        assert er.approved_configuration(plan, 'MULTI_AGENT', tmp_path).protected_test_suite_ref == reference
    else:
        with pytest.raises(er.ExperimentRunError, match='LOOP_POLICY_MISMATCH'):
            er.approved_configuration(plan, 'MULTI_AGENT', tmp_path)


# ---------------------------------------------------------------- 대기·재개 금지

class _Host:
    """가짜 Host API(dev 83·85번 계약). 상태 목록을 차례로 돌려주고 받은 요청을 모두 기록한다.

    Run 생성에는 UUIDv4 Idempotency-Key가 있어야 하고, 같은 키·같은 본문이면 처음 Run을 200으로 다시 돌려준다.
    """

    def __init__(self, statuses):
        self.statuses, self.requests, self.run_id = list(statuses), [], str(uuid4())
        self.keys, self.headers = {}, []

    def handler(self, request):
        self.requests.append((request.method, request.url.path))
        self.headers.append(dict(request.headers))
        if request.method == 'POST' and request.url.path == '/api/v1/runs':
            key = request.headers.get('idempotency-key')
            if key is None:
                return httpx.Response(422, json={'detail': 'IDEMPOTENCY_KEY_REQUIRED'})
            digest = er._host_request_hash(json.loads(request.content))
            if key in self.keys:
                if self.keys[key] != digest:
                    return httpx.Response(409, json={'detail': 'IDEMPOTENCY_KEY_CONFLICT'})
                return httpx.Response(200, json={'run': {'runId': self.run_id}})
            self.keys[key] = digest
            return httpx.Response(201, json={'run': {'runId': self.run_id}})
        if request.method == 'GET' and request.url.path.startswith('/api/v1/submissions/'):
            key = request.url.path.rsplit('/', 1)[1]
            if key not in self.keys:
                return httpx.Response(404, json={'detail': 'SUBMISSION_NOT_FOUND'})
            return httpx.Response(200, json={'internalKey': key, 'status': 'CREATED', 'requestSha256': self.keys[key],
                                             'runId': self.run_id})
        if request.method == 'GET' and request.url.path == f'/api/v1/runs/{self.run_id}':
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return httpx.Response(200, json={'runId': self.run_id, 'status': status})
        return httpx.Response(404)

    def client(self, api):
        return httpx.Client(base_url=api, transport=httpx.MockTransport(self.handler))


@pytest.mark.parametrize('final', ['FINISHED', 'ABORTED', 'WAITING_INPUT', 'HUMAN_REVIEW'])
def test_wait_stops_at_end_or_pause_and_never_resumes(final):
    host = _Host(['PLANNING', 'IMPLEMENTING', final])
    with host.client('http://host') as client:
        assert er.wait(client, host.run_id, timeout_seconds=60, sleep=lambda _: None) == final
    assert all(method == 'GET' for method, _ in host.requests)
    assert not any(path.endswith(('/resume', '/recover', '/cancel')) for _, path in host.requests)


def test_wait_times_out_without_cancelling():
    host, ticks = _Host(['IMPLEMENTING']), iter(range(0, 1000, 10))
    with host.client('http://host') as client:
        assert er.wait(client, host.run_id, timeout_seconds=30, clock=lambda: next(ticks), sleep=lambda _: None) == 'TIMEOUT'
    assert not any(path.endswith('/cancel') for _, path in host.requests)


def test_submission_rejection_is_reported():
    def reject(request):
        return httpx.Response(422, json={'detail': 'OWNED_AGENT_CONFIGURATION_INVALID'})
    with httpx.Client(base_url='http://host', transport=httpx.MockTransport(reject)) as client:
        with pytest.raises(er.ExperimentRunError, match='RUN_SUBMISSION_REJECTED_422'):
            er.submit(client, {})


# ---------------------------------------------------------------- 인계·기록

def test_stores_follow_the_runtime_database_paths(tmp_path):
    runtime = tmp_path / 'configs' / 'multi-runtime.json'
    runtime.parent.mkdir()
    runtime.write_text(json.dumps({'orchestrator': {'databasePath': '../.data/orchestrator.sqlite3'},
                                   'agents': {name: {'databasePath': f'../.data/agents/{name.lower()}.sqlite3'}
                                              for name in ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')}}),
                       encoding='utf-8')
    database, stores = er._stores(runtime, 'MULTI_AGENT')
    assert database == (tmp_path / '.data/orchestrator.sqlite3').resolve()
    assert stores['QA'] == str((tmp_path / '.data/agents/qa.sqlite3').resolve()) and len(stores) == 4
    with pytest.raises(er.ExperimentRunError, match='RUNTIME_AGENTS_MISMATCH'):
        er._stores(runtime, 'SINGLE_AGENT')


RUNTIME_LIMITS = {'max_model_calls': 200, 'max_tool_calls': 200, 'max_output_tokens': 8192, 'max_total_tokens': None,
                  'model_timeout_seconds': 120, 'tool_timeout_seconds': 120}


def _matching_plan(**changes):
    # _approved()와 RUNTIME_LIMITS에 맞춘 계획(모든 칸이 실제 설정과 같다)
    plan = _plan(model={'provider': 'openai', 'modelId': 'gpt-4.1-mini-2025-04-14', 'modelRevision': None,
                        'temperature': 0, 'seed': None},
                 limits={'modelCalls': 200, 'toolCalls': 200, 'outputTokens': 8192, 'totalTokens': None,
                         'modelTimeoutSeconds': 120, 'toolTimeoutSeconds': 120, 'runtimeBudgetMs': 3_600_000,
                         'maxFixAttempts': 3, 'maxToolRetries': 2},
                 source={'repositoryPath': 'start', 'commitHash': 'a' * 40, 'lockPath': 'requirements.lock'},
                 image='fixture/eval@sha256:' + 'c' * 64, hardwareProfile='fixture')
    plan.update(changes)
    return plan


def _runtime_files(folder, llm=None):
    for name in ('SINGLE_AGENT', 'MULTI_AGENT'):
        (folder / f'{name}-runtime.json').write_text(json.dumps({'llmLimits': llm or RUNTIME_LIMITS}), encoding='utf-8')


def _handoff(folder, plan, architecture, run_id, *, status='FINISHED', configuration=None):
    # Minimal no-source export with the real load_handoff contract and byte hashes.
    folder.mkdir(parents=True)
    try:
        order = int(folder.name.split('-', 1)[0])
    except ValueError:
        order = next(row['order'] for row in er.schedule(plan)['runs'] if row['architecture'] == architecture)
    configuration = configuration or er.submission(plan, _approved(architecture=architecture), order)['configuration']
    verdict = 'SUCCESS' if status == 'FINISHED' else None
    run = {'runId': run_id, 'status': status, 'verdict': verdict, 'finalCodeVersion': None, 'exportedCodeVersions': []}
    documents = {'run.json': run,
                 'bundle.json': {'run': {'runId': run_id, 'status': status, 'verdict': verdict, 'codeVersion': None},
                                 'configuration': {'configuration': configuration}},
                 'trace.json': {'runId': run_id}, 'a2a-messages.json': {'runId': run_id}}
    files = {}
    for name, document in documents.items():
        data = json.dumps(document).encode('utf-8')
        (folder / name).write_bytes(data)
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'sizeBytes': len(data)}
    (folder / 'handoff.json').write_text(json.dumps({'runId': run_id, 'architecture': architecture,
        'runStatus': status, 'orchestratorVerdict': verdict, 'files': files}), encoding='utf-8')
    return folder


def _exporter(calls, fail=0, status='FINISHED'):
    # 처음 fail번은 실패하는 가짜 인계 함수
    def exporter(plan, architecture, run_id, folder, base):
        calls.append((architecture, run_id, folder))
        if len(calls) <= fail:
            raise OSError('export interrupted')
        return _handoff(folder, plan, architecture, run_id, status=status)
    return exporter


def _events(folder):
    return [json.loads(line) for line in (folder / 'runs.jsonl').read_text(encoding='utf-8').splitlines()]


def test_run_one_submits_waits_exports_and_records(monkeypatch, tmp_path):
    host, calls = _Host(['PLANNING', 'WAITING_INPUT']), []
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: _approved(architecture=architecture))
    _runtime_files(tmp_path)
    plan = _matching_plan()
    row = er.schedule(plan)['runs'][0]
    result = er.run_one(plan, row, tmp_path / 'results', tmp_path, client_factory=host.client,
                        sleep=lambda _: None, exporter=_exporter(calls, status='WAITING_INPUT'))
    assert result['status'] == 'WAITING_INPUT' and result['resumed'] is False and result['reusedSubmission'] is False
    assert calls == [('SINGLE_AGENT', host.run_id, tmp_path / 'results' / f'001-single_agent-{host.run_id}')]
    assert [(item['event'], item.get('runId')) for item in _events(tmp_path / 'results')] == [
        ('SUBMITTING', None), ('SUBMITTED', host.run_id), ('ENDED', host.run_id), ('EXPORTED', host.run_id)]
    assert [path for method, path in host.requests if method == 'POST'] == ['/api/v1/runs']


def test_accepted_run_is_recorded_before_export_and_retry_reuses_it(monkeypatch, tmp_path):
    # 점검 3: 인계가 실패해도 수락된 Run이 기록에 남고, 다시 실행하면 새 Run을 만들지 않고 인계만 이어 한다.
    host, calls = _Host(['FINISHED']), []
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: _approved(architecture=architecture))
    _runtime_files(tmp_path)
    plan, out = _matching_plan(), tmp_path / 'results'
    row = er.schedule(plan)['runs'][0]
    exporter = _exporter(calls, fail=1)
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, tmp_path, client_factory=host.client, sleep=lambda _: None, exporter=exporter)
    assert [item['event'] for item in _events(out)] == ['SUBMITTING', 'SUBMITTED', 'ENDED', 'EXPORT_FAILED']
    result = er.run_one(plan, row, out, tmp_path, client_factory=host.client, sleep=lambda _: None, exporter=exporter)
    assert result['runId'] == host.run_id and result['reusedSubmission'] is True and result['status'] == 'FINISHED'
    assert [path for method, path in host.requests if method == 'POST'] == ['/api/v1/runs']  # Run은 하나
    assert [item['event'] for item in _events(out)][-1] == 'EXPORTED'

    def unused(api):
        raise AssertionError('이미 인계한 순서는 Host에 다시 묻지 않는다')
    again = er.run_one(plan, row, out, tmp_path, client_factory=unused, sleep=lambda _: None, exporter=exporter)
    assert again['export'] == result['export'] and len(calls) == 2


def test_timeout_is_recorded_and_a_retry_resumes_waiting(monkeypatch, tmp_path):
    host, calls, ticks = _Host(['IMPLEMENTING']), [], iter(range(0, 100_000, 1000))
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: _approved(architecture=architecture))
    _runtime_files(tmp_path)
    plan, out = _matching_plan(), tmp_path / 'results'
    row = er.schedule(plan)['runs'][0]
    first = er.run_one(plan, row, out, tmp_path, client_factory=host.client, sleep=lambda _: None,
                       clock=lambda: next(ticks), exporter=_exporter(calls))
    assert first['status'] == 'TIMEOUT' and first['export'] is None and calls == []
    host.statuses = ['FINISHED']
    second = er.run_one(plan, row, out, tmp_path, client_factory=host.client, sleep=lambda _: None,
                        exporter=_exporter(calls))
    assert second['status'] == 'FINISHED' and second['runId'] == first['runId'] and len(calls) == 1
    assert [path for method, path in host.requests if method == 'POST'] == ['/api/v1/runs']


def test_ledger_with_two_runs_for_one_order_is_refused(tmp_path):
    plan, out = _matching_plan(), tmp_path / 'results'
    row = er.schedule(plan)['runs'][0]
    fingerprint = er._plan_fingerprint(plan, tmp_path)
    from datetime import datetime, timezone
    er._record(out, plan, row, 'SUBMITTING', fingerprint=fingerprint, intentAt=datetime.now(timezone.utc).isoformat(),
               submissionSha256='a' * 64, configurationSha256='b' * 64, hostRequestSha256='c' * 64,
               idempotencyKey=str(uuid4()))
    er._record(out, plan, row, 'SUBMITTED', fingerprint=fingerprint, runId=str(uuid4()))
    er._record(out, plan, row, 'SUBMITTED', fingerprint=fingerprint, runId=str(uuid4()))
    with pytest.raises(er.ExperimentRunError, match='LEDGER_HAS_TWO_RUNS_FOR_ONE_ORDER'):
        er._ledger(out)


# ---------------------------------------------------------------- 계획과 실제 설정 대조(점검 4)

def test_matching_plan_has_no_differences():
    assert er.plan_differences(_matching_plan(), _approved(), {'llmLimits': RUNTIME_LIMITS}) == []


@pytest.mark.parametrize('change, field', [
    ({'model': {'provider': 'openai', 'modelId': 'gpt-4.1', 'modelRevision': None, 'temperature': 0, 'seed': None}},
     'model.modelId'),
    ({'source': {'commitHash': 'b' * 40}}, 'source.commitHash'),
    ({'image': 'fixture/eval@sha256:' + 'd' * 64}, 'image'),
    ({'hardwareProfile': 'other'}, 'hardwareProfile'),
    ({'image': None}, 'image'),
])
def test_plan_differences_find_model_commit_image_and_hardware(change, field):
    assert field in er.plan_differences(_matching_plan(**change), _approved(), {'llmLimits': RUNTIME_LIMITS})


@pytest.mark.parametrize('key, target, value', [
    ('modelCalls', 'max_model_calls', 199), ('toolCalls', 'max_tool_calls', 20), ('outputTokens', 'max_output_tokens', 2048),
    ('totalTokens', 'max_total_tokens', 100000), ('modelTimeoutSeconds', 'model_timeout_seconds', 60),
    ('toolTimeoutSeconds', 'tool_timeout_seconds', 60),
])
def test_plan_differences_find_every_llm_limit(key, target, value):
    assert er.plan_differences(_matching_plan(), _approved(), {'llmLimits': dict(RUNTIME_LIMITS, **{target: value})}) == [
        f'limits.{key}']
    limits = dict(_matching_plan()['limits'])
    del limits[key]
    assert f'limits.{key}' in er.plan_differences(_matching_plan(limits=limits), _approved(), {'llmLimits': RUNTIME_LIMITS})


def test_plan_differences_find_the_runtime_budget():
    assert er.plan_differences(_matching_plan(), _approved(budget=1_800_000), {'llmLimits': RUNTIME_LIMITS}) == [
        'limits.runtimeBudgetMs']


def test_run_one_refuses_a_plan_mismatch_before_submitting(monkeypatch, tmp_path):
    host = _Host(['FINISHED'])
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: _approved(architecture=architecture))
    _runtime_files(tmp_path, dict(RUNTIME_LIMITS, max_tool_calls=20))
    plan = _matching_plan()
    with pytest.raises(er.ExperimentRunError, match='PLAN_CONFIGURATION_MISMATCH') as caught:
        er.run_one(plan, er.schedule(plan)['runs'][0], tmp_path / 'results', tmp_path, client_factory=host.client,
                   sleep=lambda _: None, exporter=_exporter([]))
    assert caught.value.detail == ['limits.toolCalls'] and host.requests == []
    assert not (tmp_path / 'results' / 'runs.jsonl').exists()


# ---------------------------------------------------------------- 실제 dev Host 설정 검사와의 연결

def test_real_dev_inspection_feeds_a_valid_submission(tmp_path):
    tests_dir = str(ROOT / 'tests')
    if tests_dir not in sys.path:
        sys.path.insert(0, tests_dir)
    module = pytest.importorskip('test_owned_host')
    if shutil.which('git') is None:
        pytest.skip('git이 있어야 dev Host 설정 시험 도구를 만든다')

    class Fixture(module._HostFixture, unittest.TestCase):
        def runTest(self):
            pass
    fixture = Fixture()
    fixture.setUp()
    try:
        runtime, tools = fixture.runtime, fixture.tools
        limits = {key: runtime['llmLimits'][target] for key, target in er.RUNTIME_LIMITS.items()}
        plan = _plan(hostConfigurations={'MULTI_AGENT': {
            'api': 'http://127.0.0.1:8000', 'runtime': str(fixture.runtime_file), 'tools': str(fixture.tools_file),
            'source': str(fixture.source_file)}},
            model={key: runtime['model'].get(key) for key in ('provider', 'modelId', 'modelRevision', 'temperature', 'seed')},
            limits={**limits, 'runtimeBudgetMs': runtime['runtimeBudgetMs'], 'maxFixAttempts': 3, 'maxToolRetries': 2},
            source={'commitHash': fixture.commit}, image=tools['imageReference'], hardwareProfile=tools['hardwareProfile'])
        approved = er.approved_configuration(plan, 'MULTI_AGENT', tmp_path)
        assert er.plan_differences(plan, approved, runtime) == []
        assert er.plan_differences(dict(plan, model=dict(plan['model'], modelId='other-model')), approved, runtime) == [
            'model.modelId']
        body = er.submission(plan, approved, order=1)
        from orchestrator.domain.run_configuration import RunConfiguration
        submitted = RunConfiguration.model_validate(body['configuration'])
        assert submitted.experiment_id is not None and submitted.starting_commit_hash == fixture.commit
        # dev Host 제출 검사: 승인 칸이 모두 같아야 받는다.
        from agents.platform.host import inspect_host_configuration
        plan_object = inspect_host_configuration(runtime_file=fixture.runtime_file, tools_file=fixture.tools_file,
                                                 source_file=fixture.source_file)
        plan_object.validate_configuration(submitted)
    finally:
        fixture.doCleanups()


# ---------------------------------------------------------------- 결정 값: Host 실행 설정·시범 계획

PILOTS = [ROOT / 'examples/evaluation' / f'scn001-pilot.{version}.plan.json' for version in ('ver1', 'ver2')]


def _pilot(index=0):
    return json.loads(PILOTS[index].read_text(encoding='utf-8'))


def test_runtime_configuration_fills_plan_limits_and_passes_the_dev_loader(tmp_path):
    plan, written = _pilot(), {}
    for architecture, example in (('MULTI_AGENT', 'owned-runtime.example.json'),
                                  ('SINGLE_AGENT', 'single-runtime.example.json')):
        out = er.write_runtime_configuration(ROOT / 'configs' / example, plan, architecture,
                                             tmp_path / f'{architecture}.json')
        written[architecture] = json.loads(out.read_text(encoding='utf-8'))
    multi, single = written['MULTI_AGENT'], written['SINGLE_AGENT']
    assert multi['model']['modelId'] == 'gpt-4.1-mini-2025-04-14' and multi['model']['temperature'] == 0
    assert multi['llmLimits']['max_model_calls'] == 200 and multi['llmLimits']['max_tool_calls'] == 200
    assert multi['llmLimits']['max_output_tokens'] == 8192 and multi['llmLimits']['max_total_tokens'] is None
    assert multi['runtimeBudgetMs'] == 3_600_000
    # 두 구조는 모델·한도·시간 예산이 같고, Orchestrator 포트만 계획의 api 주소대로 다르다.
    for key in ('model', 'llmLimits', 'runtimeBudgetMs'):
        assert multi[key] == single[key]
    assert (multi['orchestrator']['port'], single['orchestrator']['port']) == (8000, 8010)


def test_runtime_configuration_refuses_values_the_dev_loader_rejects(tmp_path):
    plan = _pilot()
    plan['model'] = dict(plan['model'], provider='other')
    out = tmp_path / 'runtime.json'
    with pytest.raises(er.ExperimentRunError, match='RUNTIME_CONFIGURATION_INVALID'):
        er.write_runtime_configuration(ROOT / 'configs/owned-runtime.example.json', plan, 'MULTI_AGENT', out)
    assert not out.exists()
    out.write_text('{}', encoding='utf-8')
    with pytest.raises(er.ExperimentRunError, match='OUTPUT_EXISTS'):
        er.write_runtime_configuration(ROOT / 'configs/owned-runtime.example.json', _pilot(), 'MULTI_AGENT', out)


def test_pilot_plans_record_the_decisions(monkeypatch):
    import subprocess
    from evaluation import preflight
    ver1, ver2 = _pilot(0), _pilot(1)
    assert ver1['experimentId'] == ver2['experimentId']  # 두 버전은 같은 실험으로 선언한다
    assert (ver1['loopPolicy'], ver2['loopPolicy']) == ('IN_LOOP_AND_FINAL', 'FINAL_ONLY')
    assert 'protectedCases' in ver1['hostConfigurations']['MULTI_AGENT']
    assert 'protectedCases' not in ver2['hostConfigurations']['MULTI_AGENT']
    for plan in (ver1, ver2):
        assert [row['architecture'] for row in er.schedule(plan)['runs']] == ['SINGLE_AGENT', 'MULTI_AGENT']
    original = subprocess.run

    def run(args, **kwargs):
        if args[0] != 'fixture-docker':
            return original(args, **kwargs)
        return subprocess.CompletedProcess(args, 1, b'', b'')  # Docker 없음: 환경 항목은 미검증으로 남는다

    monkeypatch.setattr(preflight.subprocess, 'run', run)
    result = preflight.check_preparation(ver1, docker='fixture-docker')
    status = {row['check']: row['status'] for row in result['checks']}
    assert all(status[name] == 'PASS' for name in
               ('experimentId', 'model', 'plannedRuns', 'limits', 'loopPolicy', 'executionOrder', 'warmupState'))
    assert result['ready'] is False  # 시작 저장소·이미지·하드웨어·Host 설정 파일은 아직 없다


def test_forced_update_keeps_the_good_file_when_the_new_configuration_is_invalid(tmp_path):
    # 점검 8: --force 갱신이 dev 검사에 실패해도 기존 정상 설정은 그대로 남고, 임시 파일도 남지 않는다.
    example, out = ROOT / 'configs/owned-runtime.example.json', tmp_path / 'runtime.json'
    er.write_runtime_configuration(example, _pilot(), 'MULTI_AGENT', out)
    good = out.read_bytes()
    bad = _pilot()
    bad['model'] = dict(bad['model'], provider='other')
    with pytest.raises(er.ExperimentRunError, match='RUNTIME_CONFIGURATION_INVALID'):
        er.write_runtime_configuration(example, bad, 'MULTI_AGENT', out, force=True)
    assert out.read_bytes() == good and [path.name for path in tmp_path.iterdir()] == ['runtime.json']
    changed = _pilot()
    changed['limits'] = dict(changed['limits'], modelCalls=150)
    er.write_runtime_configuration(example, changed, 'MULTI_AGENT', out, force=True)
    assert json.loads(out.read_text(encoding='utf-8'))['llmLimits']['max_model_calls'] == 150
