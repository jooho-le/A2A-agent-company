"""준비가 안 된 환경을 실행 가능으로 표시하지 않고, 점검 자체가 모델을 호출하지 않는지 확인한다."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from evaluation import environment, preflight
from tests.evaluation.test_experiment import starting_repo
from tests.evaluation.test_final_evaluation import IMAGE
from tests.evaluation.test_run_metrics import uid


# Explicit UI scenario fixture; SCN-001's production configuration has no UI suite.
BROWSER_SECTION = {
    'suites': [{'name': 'qa-browser', 'kind': 'QA_TESTS', 'suite_path': 'tests/browser/suite.json'}],
    'service_argv': [environment.PYTHON, '-m', 'uvicorn', '--app-dir', '/snapshot', 'main:app',
                     '--host', '127.0.0.1', '--port', '8765'],
    'playwright_version': environment.PLAYWRIGHT_VERSION, 'python_executable': environment.PYTHON,
    'base_url': 'http://127.0.0.1:8765', 'ready_path': '/', 'startup_timeout_seconds': 20,
    'action_timeout_ms': 5000, 'limits': environment._limits(90, memory_gib=2),
}


def plan(tmp_path, *, calls=30, change=None):
    data = {'experimentId': uid(1), 'scenarioKey': 'SCN-001', 'source': starting_repo(tmp_path),
            'image': IMAGE, 'model': {'provider': 'fixture', 'modelId': 'fixture', 'temperature': 0},
            'plannedRuns': {'SINGLE_AGENT': 3, 'MULTI_AGENT': 3},
            'limits': {'modelCalls': calls, 'toolCalls': 40, 'outputTokens': 4096, 'totalTokens': None,
                       'modelTimeoutSeconds': 60, 'toolTimeoutSeconds': 60, 'runtimeBudgetMs': 600000,
                       'maxFixAttempts': 3, 'maxToolRetries': 2},
            'loopPolicy': 'FINAL_ONLY', 'executionOrder': 'ALTERNATING', 'warmupState': 'COLD', 'hardwareProfile': 'fixture'}
    data['hostConfigurations'] = host_configurations(tmp_path / 'host', data, change)
    return data


def host_configurations(folder, data, change=None):
    # 계획과 맞는 구조별 Host 실행 설정 파일(runtime·tools·source). change(구조, 이름, 내용)로 일부만 바꾼다.
    source = data['source']
    lock = (Path(source['repositoryPath']) / source['lockPath']).read_bytes()
    result = {}
    for architecture in ('SINGLE_AGENT', 'MULTI_AGENT'):
        documents = {
            'runtime': {'schemaVersion': 1, 'model': {**data['model'], 'modelRevision': None, 'seed': None},
                        'llmLimits': {'max_model_calls': data['limits']['modelCalls'],
                                      'max_tool_calls': data['limits']['toolCalls'],
                                      'max_output_tokens': data['limits']['outputTokens'],
                                      'max_total_tokens': data['limits']['totalTokens'],
                                      'tool_timeout_seconds': data['limits']['toolTimeoutSeconds'],
                                      'model_timeout_seconds': data['limits']['modelTimeoutSeconds']},
                        'runtimeBudgetMs': data['limits']['runtimeBudgetMs']},
            'tools': {'schemaVersion': 1, 'imageReference': 'fixture/eval@' + IMAGE,
                      'dependencyLockHash': 'sha256:' + hashlib.sha256(lock).hexdigest(),
                      'hardwareProfile': data['hardwareProfile'], 'maxCallSeconds': 60,
                      'dockerEndpoint': 'unix:///var/run/docker.sock',
                      'unit': {'scopes': [{'name': 'developer-unit', 'kind': 'SNAPSHOT'}]},
                      'browser': BROWSER_SECTION},
            'source': {'schemaVersion': 1, 'repositoryPath': source['repositoryPath'], 'commitHash': source['commitHash'],
                       'lockPath': source['lockPath'], 'gitTimeoutSeconds': 30},
        }
        if change:
            change(architecture, documents)
        result[architecture] = {}
        for name, document in documents.items():
            path = folder / architecture / f'{name}.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(document), encoding='utf-8')
            result[architecture][name] = str(path)
    return result


BROWSER_PASS = json.dumps({'format': 'browser-v1', 'suiteName': 'qa-browser', 'playwrightVersion': '1.65.2',
                           'browserVersion': '159.0.8154.6', 'total': 1, 'passed': 1, 'failed': 0, 'tests': []})


def fake_docker(monkeypatch, *, daemon=True, image=True, timeout=False, browser=BROWSER_PASS, browser_exit=0):
    # browser: dev 브라우저 실행기가 컨테이너 표준 출력으로 내는 마지막 줄(실제 브라우저 실행 확인 단계)
    calls = []
    original = subprocess.run

    def run(args, **kwargs):
        if args[0] != 'fixture-docker':
            return original(args, **kwargs)
        calls.append(args)
        if args[1] == 'info':
            return subprocess.CompletedProcess(args, 0 if daemon else 1, b'fixture', b'')
        if args[1] == 'run' and timeout:
            raise subprocess.TimeoutExpired(args, 20)
        if args[1:3] == ['container', 'start']:
            return subprocess.CompletedProcess(args, browser_exit, browser + '\n', '')
        return subprocess.CompletedProcess(args, 0, b'', b'')

    monkeypatch.setattr(preflight.subprocess, 'run', run)
    monkeypatch.setattr(preflight.DockerRunner, 'image_digest', lambda self: IMAGE if image else None)
    return calls


def test_template_reports_missing_decisions_without_running_image(monkeypatch):
    path = Path(__file__).parents[2] / 'examples/evaluation/experiment-plan.example.json'
    calls = fake_docker(monkeypatch, daemon=False)
    result = preflight.check_preparation(json.loads(path.read_text()), docker='fixture-docker')
    assert result['ready'] is False
    unknown = {row['check'] for row in result['checks'] if row['status'] == 'UNVERIFIED'}
    assert {'model', 'plannedRuns', 'limits', 'startingSource', 'pinnedImage', 'docker', 'hostConfiguration'} <= unknown
    assert len(calls) == 1 and calls[0][1] == 'info'


def test_prepared_plan_checks_only_isolated_package_metadata(tmp_path, monkeypatch):
    data = plan(tmp_path)
    calls = fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is True and result['scope'] == 'PREPARATION_ONLY' and result['warnings'] == []
    assert {row['check'] for row in result['checks']} >= {'hostModel.SINGLE_AGENT', 'hostLimits.MULTI_AGENT',
                                                         'hostLockHash.SINGLE_AGENT', 'architectureParity', 'modelCallFloor'}
    probe = next(args for args in calls if args[1] == 'run')
    assert probe[probe.index('--network') + 1] == 'none'
    assert '--mount' not in probe and '--env' not in probe and '--read-only' in probe
    removed = next(args for args in calls if args[1:3] == ['rm', '-f'])
    assert removed[-1] == probe[probe.index('--name') + 1]
    # 실제 브라우저 확인은 dev 샌드박스가 만드는 컨테이너 옵션 그대로 한 번만 돌고(두 구조의 설정이 같음) 끝나면 지운다.
    created = [args for args in calls if args[1:3] == ['container', 'create']]
    assert len(created) == 1
    create = created[0]
    for flag, value in (('--network', 'none'), ('--cap-drop', 'ALL'), ('--security-opt', 'no-new-privileges:true'),
                        ('--user', '10001:10001'), ('--ipc', 'none'), ('--pull', 'never')):
        assert create[create.index(flag) + 1] == value
    assert '--read-only' in create and 'PLAYWRIGHT_BROWSERS_PATH=/ms-playwright' in create
    mounts = [create[index + 1] for index, item in enumerate(create) if item == '--mount']
    assert len(mounts) == 2 and all(',readonly,' in mount for mount in mounts)
    assert calls[-1][1:4] == ['container', 'rm', '-f'] and calls[-1][-1] == create[create.index('--name') + 1]


@pytest.mark.parametrize('output, exit_code, reason', [
    (json.dumps({'error': 'BROWSER_START_FAILED'}), 3, 'HOST_BROWSER_START_FAILED'),
    (json.dumps({'format': 'browser-v1', 'total': 1, 'passed': 0, 'failed': 1, 'tests': []}), 1,
     'HOST_BROWSER_EXECUTION_FAILED'),
    ('not json', 2, 'HOST_BROWSER_REPORT_INVALID'),
    ('["browser-v1"]', 0, 'HOST_BROWSER_REPORT_INVALID'),  # JSON이지만 보고서 모양(객체)이 아닌 출력
])
def test_browser_that_cannot_run_under_the_sandbox_options_is_not_ready(tmp_path, monkeypatch, output, exit_code,
                                                                        reason):
    """설정 대조가 모두 맞아도 Host 브라우저 도구가 실제 샌드박스 조건에서 시험을 못 돌리면 준비 완료가 아니다
    (2026-11-15: Chromium 자체 샌드박스가 기본 seccomp에서 실패한 것을 설정 검사만으로는 놓쳤다)."""
    data = plan(tmp_path)
    fake_docker(monkeypatch, browser=output, browser_exit=exit_code)
    result = preflight.check_preparation(data, docker='fixture-docker')
    failing = {row['check']: row['reason'] for row in result['checks'] if row['status'] != 'PASS'}
    assert result['ready'] is False
    assert failing == {'hostBrowser.SINGLE_AGENT': reason, 'hostBrowser.MULTI_AGENT': reason}


@pytest.mark.parametrize('key', ['runtime', 'tools', 'source'])
@pytest.mark.parametrize('value', [[], None, 'invalid', 42])
def test_non_object_host_document_is_unverified(tmp_path, monkeypatch, key, value):
    data = plan(tmp_path)
    fake_docker(monkeypatch)
    Path(data['hostConfigurations']['MULTI_AGENT'][key]).write_text(json.dumps(value))
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is False
    row = next(row for row in result['checks'] if row['check'] == 'hostConfiguration')
    assert row == {'check': 'hostConfiguration', 'status': 'UNVERIFIED', 'reason': 'HOST_CONFIGURATION_UNREADABLE'}


def test_timeout_is_unverified_and_cleans_up_its_own_container(tmp_path, monkeypatch):
    data = plan(tmp_path)
    calls = fake_docker(monkeypatch, timeout=True)
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is False
    probe = next(args for args in calls if args[1] == 'run')
    removed = next(args for args in calls if args[1:3] == ['rm', '-f'])
    assert removed[-1] == probe[probe.index('--name') + 1]  # 시간이 지나도 자기가 만든 컨테이너는 지운다


@pytest.mark.parametrize('damage', ['exam_in_source', 'missing_lock', 'mutable_image', 'no_call_limit'])
def test_missing_or_unsafe_preconditions_cannot_be_ready(tmp_path, monkeypatch, damage):
    data = plan(tmp_path / 'base')
    if damage == 'exam_in_source':
        data['source'] = starting_repo(tmp_path / 'exposed', {'src/evaluation/copied.py': 'secret exam'})
    elif damage == 'missing_lock':
        data['source']['lockPath'] = 'missing.lock'
    elif damage == 'mutable_image':
        data['image'] = 'python:latest'
    else:
        data['limits']['modelCalls'] = None
    fake_docker(monkeypatch)
    assert preflight.check_preparation(data, docker='fixture-docker')['ready'] is False


# ---------------------------------------------------------------- 실제 Host 실행 설정과의 대조(2026-11-14)

def _set(architectures, name, path, value):
    def change(architecture, documents):
        if architecture in architectures:
            target = documents[name]
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value
    return change


BOTH = ('SINGLE_AGENT', 'MULTI_AGENT')


@pytest.mark.parametrize('change,failing', [
    (_set(('SINGLE_AGENT',), 'runtime', ('model', 'temperature'), 0.5), {'hostModel.SINGLE_AGENT', 'architectureParity'}),
    (_set(('MULTI_AGENT',), 'runtime', ('llmLimits', 'max_model_calls'), 25), {'hostLimits.MULTI_AGENT', 'architectureParity'}),
    (_set(BOTH, 'runtime', ('runtimeBudgetMs',), 1), {'hostLimits.SINGLE_AGENT', 'hostLimits.MULTI_AGENT'}),
    # 도구 시간 한도를 바꾸면 도구 설정의 maxCallSeconds와도, 계획의 toolTimeoutSeconds와도 달라진다(점검 4).
    (_set(BOTH, 'runtime', ('llmLimits', 'tool_timeout_seconds'), 30),
     {'hostToolTimeout.SINGLE_AGENT', 'hostToolTimeout.MULTI_AGENT', 'hostLimits.SINGLE_AGENT', 'hostLimits.MULTI_AGENT'}),
    (_set(BOTH, 'tools', ('imageReference',), 'fixture/eval@sha256:' + 'e' * 64),
     {'hostImage.SINGLE_AGENT', 'hostImage.MULTI_AGENT'}),
    (_set(BOTH, 'tools', ('dependencyLockHash',), 'sha256:' + '0' * 64),
     {'hostLockHash.SINGLE_AGENT', 'hostLockHash.MULTI_AGENT'}),
    (_set(BOTH, 'tools', ('hardwareProfile',), 'other'), {'hostHardware.SINGLE_AGENT', 'hostHardware.MULTI_AGENT'}),
    (_set(BOTH, 'source', ('commitHash',), 'f' * 40), {'hostSource.SINGLE_AGENT', 'hostSource.MULTI_AGENT'}),
    (_set(('MULTI_AGENT',), 'tools', ('unit', 'scopes'), []), {'architectureParity'}),
])
def test_host_configuration_that_differs_from_the_plan_is_not_ready(tmp_path, monkeypatch, change, failing):
    data = plan(tmp_path, change=change)
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is False
    assert {row['check'] for row in result['checks'] if row['status'] == 'UNVERIFIED'} == failing


def test_architecture_difference_names_the_differing_part(tmp_path, monkeypatch):
    data = plan(tmp_path, change=_set(('MULTI_AGENT',), 'tools', ('unit', 'scopes'), []))
    fake_docker(monkeypatch)
    row = next(item for item in preflight.check_preparation(data, docker='fixture-docker')['checks']
               if item['check'] == 'architectureParity')
    assert row['reason'] == 'ARCHITECTURE_CONFIGURATION_DIFFERS' and row['detail'] == ['toolConfiguration']


@pytest.mark.parametrize('damage,reason', [('absent', 'HOST_CONFIGURATION_REQUIRED'), ('one_side', 'HOST_CONFIGURATION_REQUIRED'),
                                           ('missing_file', 'HOST_CONFIGURATION_UNREADABLE'),
                                           ('not_json', 'HOST_CONFIGURATION_UNREADABLE')])
def test_missing_or_unreadable_host_configuration_is_not_ready(tmp_path, monkeypatch, damage, reason):
    data = plan(tmp_path)
    if damage == 'absent':
        del data['hostConfigurations']
    elif damage == 'one_side':
        del data['hostConfigurations']['SINGLE_AGENT']
    elif damage == 'missing_file':
        data['hostConfigurations']['MULTI_AGENT']['tools'] = str(tmp_path / 'nowhere.json')
    else:
        Path(data['hostConfigurations']['MULTI_AGENT']['runtime']).write_text('{broken', encoding='utf-8')
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    row = next(item for item in result['checks'] if item['check'] == 'hostConfiguration')
    assert result['ready'] is False and row['reason'] == reason


def test_example_call_limit_of_eight_cannot_finish_one_cycle(tmp_path, monkeypatch):
    data = plan(tmp_path, calls=8)
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    floor = next(item for item in result['checks'] if item['check'] == 'modelCallFloor')
    assert result['ready'] is False and floor['reason'] == 'MODEL_CALL_LIMIT_BELOW_ONE_CYCLE'
    assert {item['code'] for item in result['warnings']} == {'HOST_MODEL_CALL_LIMIT_TOO_LOW'}


def test_limit_below_one_fix_cycle_is_a_warning_not_a_block(tmp_path, monkeypatch):
    data = plan(tmp_path, calls=15)
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    assert result['ready'] is True
    assert [item['code'] for item in result['warnings']] == ['MODEL_CALL_LIMIT_BELOW_ONE_FIX_CYCLE']


def test_relative_host_paths_follow_the_plan_folder(tmp_path, monkeypatch):
    data = plan(tmp_path)
    for files in data['hostConfigurations'].values():
        for name, value in files.items():
            files[name] = str(Path(value).relative_to(tmp_path))
    fake_docker(monkeypatch)
    assert preflight.check_preparation(data, base=tmp_path, docker='fixture-docker')['ready'] is True


@pytest.mark.parametrize('key,check', [('protectedCases', 'protectedCases'), ('priceBook', 'priceBook')])
def test_optional_host_files_are_compared_between_architectures(tmp_path, monkeypatch, key, check):
    data = plan(tmp_path)
    if key == 'protectedCases':
        data['loopPolicy'] = 'IN_LOOP_AND_FINAL'
    for index, entry in enumerate(data['hostConfigurations'].values()):
        path = tmp_path / f'{key}-{index}.json'
        document = {'schemaVersion': 1, 'fixtureValue': index}
        if key == 'priceBook':
            document = json.loads((environment.CONFIGS / 'owned-price-book.scn001.json').read_text())
            document['priceBookId'] = f'fixture-price-{index}'
            data['model'].update(provider='openai', modelId=document['entries'][0]['requestedModelId'])
            runtime_path = Path(entry['runtime'])
            runtime = json.loads(runtime_path.read_text())
            runtime['model'].update(data['model'])
            runtime_path.write_text(json.dumps(runtime))
        path.write_text(json.dumps(document))
        entry[key] = path.name  # optional paths also follow the plan directory
    fake_docker(monkeypatch)
    report = preflight.check_preparation(data, base=tmp_path, docker='fixture-docker')
    row = next(item for item in report['checks'] if item['check'] == 'architectureParity')
    assert report['ready'] is False and row['detail'] == [check]

    (tmp_path / f'{key}-1.json').write_bytes((tmp_path / f'{key}-0.json').read_bytes())
    assert preflight.check_preparation(data, base=tmp_path, docker='fixture-docker')['ready'] is True


def test_required_pricing_registration_and_optional_limits_checked_before_submission(tmp_path, monkeypatch):
    data = plan(tmp_path)
    fake_docker(monkeypatch)
    data.update(requireCostMeasurement=True, requireTotalTokenLimit=True, requestTextSha256='0' * 64)
    data['limits']['outputCorrections'] = 2
    for entry in data['hostConfigurations'].values():
        entry['priceBook'] = str(environment.CONFIGS / 'owned-price-book.scn001.json')
    result = preflight.check_preparation(data, base=tmp_path, docker='fixture-docker')
    failed = {row['check']: row.get('reason') for row in result['checks'] if row['status'] != 'PASS'}
    assert failed['requestText'] == 'REQUEST_TEXT_DIFFERS_FROM_REGISTRATION'
    assert failed['runTotalTokenLimit'] == 'RUN_TOTAL_TOKEN_LIMIT_NOT_SET'
    for arch in data['hostConfigurations']:
        assert failed[f'hostLimits.{arch}'] == 'HOST_LIMITS_DIFFER_FROM_PLAN'
        assert failed[f'hostPriceBook.{arch}'] == 'COST_UNAVAILABLE_FOR_REQUESTED_MODEL'


@pytest.mark.parametrize('key', ['protectedCases', 'priceBook'])
@pytest.mark.parametrize('damage', ['absent_file', 'not_object', 'empty_path'])
def test_unreadable_optional_host_files_cannot_be_ready(tmp_path, monkeypatch, key, damage):
    data = plan(tmp_path)
    path = tmp_path / 'optional.json'
    data['hostConfigurations']['SINGLE_AGENT'][key] = str(path)
    if damage == 'not_object':
        path.write_text('[]')
    elif damage == 'empty_path':
        data['hostConfigurations']['SINGLE_AGENT'][key] = ''
    fake_docker(monkeypatch)
    report = preflight.check_preparation(data, docker='fixture-docker')
    row = next(item for item in report['checks'] if item['check'] == 'hostConfiguration')
    assert report['ready'] is False and row['reason'] == 'HOST_CONFIGURATION_UNREADABLE'


@pytest.mark.parametrize('policy,with_cases', [('IN_LOOP_AND_FINAL', False), ('FINAL_ONLY', True)])
def test_protected_case_presence_must_follow_registered_loop_policy(tmp_path, monkeypatch, policy, with_cases):
    data = plan(tmp_path)
    data['loopPolicy'] = policy
    if with_cases:
        path = tmp_path / 'cases.json'
        path.write_text(json.dumps({'schemaVersion': 1, 'cases': []}))
        for entry in data['hostConfigurations'].values():
            entry['protectedCases'] = str(path)
    fake_docker(monkeypatch)
    report = preflight.check_preparation(data, docker='fixture-docker')
    rows = [row for row in report['checks'] if row['check'].startswith('hostProtectedCases.')]
    assert report['ready'] is False and len(rows) == 2
    assert all(row['reason'] == 'HOST_PROTECTED_CASES_DIFFER_FROM_LOOP_POLICY' for row in rows)


@pytest.mark.parametrize('key, target, value', [
    ('toolCalls', 'max_tool_calls', 20), ('outputTokens', 'max_output_tokens', 2048),
    ('totalTokens', 'max_total_tokens', 50000), ('modelTimeoutSeconds', 'model_timeout_seconds', 30),
])
def test_host_limits_compare_every_llm_limit_of_the_plan(tmp_path, monkeypatch, key, target, value):
    # 점검 4: 도구 호출·출력 토큰·총 토큰·호출 제한시간이 계획과 달라도 사전점검이 잡아야 한다.
    def change(architecture, documents):
        if architecture == 'MULTI_AGENT':
            documents['runtime']['llmLimits'][target] = value
    data = plan(tmp_path, change=change)
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    row = next(row for row in result['checks'] if row['check'] == 'hostLimits.MULTI_AGENT')
    assert row['status'] == 'UNVERIFIED' and row['detail'] == [key] and result['ready'] is False


def test_host_limits_require_the_plan_to_name_every_llm_limit(tmp_path, monkeypatch):
    data = plan(tmp_path)
    del data['limits']['outputTokens']
    fake_docker(monkeypatch)
    result = preflight.check_preparation(data, docker='fixture-docker')
    row = next(row for row in result['checks'] if row['check'] == 'hostLimits.SINGLE_AGENT')
    assert row['status'] == 'UNVERIFIED' and row['detail'] == ['outputTokens']
