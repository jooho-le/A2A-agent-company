"""실험 등록값·시작 코드·평가 이미지 준비 점검. 모델 호출이나 개발 실행은 하지 않는다.

2026-11-14: 실험 계획과 실제 Host 실행 설정(`hostConfigurations`: 구조별 runtime·tools·source 파일)을 대조한다.
모델·호출/시간 한도·도구 시간 한도·이미지·시작 커밋·lock 해시·두 구조 설정 동일 여부, 그리고 호출 한도가
의미 검토 호출을 포함한 최소 흐름에도 모자라는지 본다. 파일 대조라서 실제 Run이 그 파일로 돌았는지는
evaluation.parity의 Run별 고정 해시로 따로 확인한다.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import UUID, uuid4

from . import protected_tests
from .experiment import starting_source_files
from .final_evaluation import DockerRunner

ARCHITECTURES = ('SINGLE_AGENT', 'MULTI_AGENT')
MODEL_KEYS = ('provider', 'modelId', 'modelRevision', 'temperature', 'seed')
# 최신 dev 시험 하네스(가짜 LLM, 도구 호출 최소)에서 의미 검토 호출까지 센 Multi 호출 수. 실제 Run은 도구를 더 쓰므로
# 이 값은 하한이다: 수정 없이 한 바퀴 10회, 수정 1회 20회(2026-11-13 dev ea06d12 기록).
CALLS_ONE_CYCLE, CALLS_ONE_FIX = 10, 20
_BROWSER_CLEANUP_ERRORS = frozenset({
    'HOST_BROWSER_CLEANUP_FAILED', 'HOST_BROWSER_CLEANUP_TIMEOUT', 'HOST_BROWSER_CLEANUP_UNAVAILABLE',
})


def _digest(value):
    # 'repo@sha256:…'와 'sha256:…'를 같은 이미지로 본다(digest만 비교).
    found = re.search(r'sha256:[0-9a-f]{64}', value) if isinstance(value, str) else None
    return found.group(0) if found else None


def _resolved(value, base):
    if not isinstance(value, str) or not value:
        return None
    path = Path(value)
    return (path if path.is_absolute() else Path(base) / path).resolve()


def _host_files(entry, base):
    # Optional Host files use the parity module's `cases` / `priceBook` names.
    if not isinstance(entry, dict):
        raise ValueError
    paths = {}
    for key in ('runtime', 'tools', 'source'):
        path = _resolved(entry.get(key), base)
        if path is None:
            raise ValueError
        paths[key] = path
    for host_key, parity_key in (('protectedCases', 'cases'), ('priceBook', 'priceBook')):
        if entry.get(host_key) is not None:
            path = _resolved(entry[host_key], base)
            if path is None:
                raise ValueError
            paths[parity_key] = path
    documents = {key: json.loads(path.read_text(encoding='utf-8')) for key, path in paths.items()}
    if any(not isinstance(document, dict) for document in documents.values()):
        raise ValueError('HOST_CONFIGURATION_OBJECT_REQUIRED')
    return paths, documents


def host_configuration_checks(plan, base, add, warnings, lock_bytes=None):
    """계획과 구조별 Host 실행 설정 파일의 대조. 설정을 주지 않았거나 읽지 못하면 미검증이다."""
    from agents.platform.pricing import has_requested_model_rate
    from .experiment_run import RUNTIME_LIMITS, optional_limit_differences
    from .parity import config_parity, static_tool_parity
    configs = plan.get('hostConfigurations')
    if not isinstance(configs, dict) or set(configs) != set(ARCHITECTURES):
        add('hostConfiguration', False, 'HOST_CONFIGURATION_REQUIRED')
        return
    try:
        loaded = {architecture: _host_files(configs[architecture], base) for architecture in ARCHITECTURES}
    except (OSError, ValueError, TypeError):
        add('hostConfiguration', False, 'HOST_CONFIGURATION_UNREADABLE')
        return
    add('hostConfiguration', True, None)
    model = plan.get('model') if isinstance(plan.get('model'), dict) else {}
    limits = plan.get('limits') if isinstance(plan.get('limits'), dict) else {}
    source = plan.get('source') if isinstance(plan.get('source'), dict) else {}
    lock_hash = None if lock_bytes is None else 'sha256:' + hashlib.sha256(lock_bytes).hexdigest()
    for architecture, (paths, data) in loaded.items():
        runtime, tools, host_source = data['runtime'], data['tools'], data['source']
        llm = runtime.get('llmLimits') if isinstance(runtime.get('llmLimits'), dict) else {}
        host_model = runtime.get('model') if isinstance(runtime.get('model'), dict) else {}
        add(f'hostModel.{architecture}', all(host_model.get(key) == model.get(key) for key in MODEL_KEYS),
            'HOST_MODEL_DIFFERS_FROM_PLAN')
        # 계획의 LLM 한도 전체(모델·도구 호출, 출력·총 토큰, 호출 제한시간)와 시간 예산을 대조한다. 계획에 없는 칸도 다르다고 본다.
        differing = [key for key, target in RUNTIME_LIMITS.items()
                     if key not in limits or target not in llm or limits[key] != llm[target]]
        differing.extend(optional_limit_differences(limits, llm))
        if plan.get('requireCostMeasurement') is True or 'priceBook' in paths:
            add(f'hostPriceBook.{architecture}', has_requested_model_rate(paths.get('priceBook'), host_model),
                'COST_UNAVAILABLE_FOR_REQUESTED_MODEL')
        if limits.get('runtimeBudgetMs') is None or runtime.get('runtimeBudgetMs') != limits.get('runtimeBudgetMs'):
            differing.append('runtimeBudgetMs')
        add(f'hostLimits.{architecture}', not differing, 'HOST_LIMITS_DIFFER_FROM_PLAN', detail=differing or None)
        add(f'hostToolTimeout.{architecture}', llm.get('tool_timeout_seconds') == tools.get('maxCallSeconds'),
            'TOOL_TIMEOUT_DIFFERS_FROM_MAX_CALL_SECONDS')
        add(f'hostImage.{architecture}', _digest(plan.get('image')) is not None
            and _digest(tools.get('imageReference')) == _digest(plan.get('image')), 'HOST_IMAGE_DIFFERS_FROM_PLAN')
        add(f'hostHardware.{architecture}', tools.get('hardwareProfile') == plan.get('hardwareProfile'),
            'HOST_HARDWARE_PROFILE_DIFFERS_FROM_PLAN')
        same_repository = _resolved(host_source.get('repositoryPath'), paths['source'].parent) == \
            _resolved(source.get('repositoryPath'), base)
        add(f'hostSource.{architecture}', same_repository and host_source.get('commitHash') == source.get('commitHash')
            and host_source.get('lockPath') == source.get('lockPath'), 'HOST_SOURCE_DIFFERS_FROM_PLAN')
        add(f'hostLockHash.{architecture}', lock_hash is not None and tools.get('dependencyLockHash') == lock_hash,
            'HOST_LOCK_HASH_DIFFERS_FROM_COMMIT')
        add(f'hostProtectedCases.{architecture}', ('cases' in paths) == (plan.get('loopPolicy') == 'IN_LOOP_AND_FINAL'),
            'HOST_PROTECTED_CASES_DIFFER_FROM_LOOP_POLICY')
        calls = llm.get('max_model_calls')
        if type(calls) is int and calls < CALLS_ONE_CYCLE:
            warnings.append({'code': 'HOST_MODEL_CALL_LIMIT_TOO_LOW', 'architecture': architecture, 'value': calls})
    rows = [static_tool_parity(), *config_parity({key: str(path) for key, path in loaded['SINGLE_AGENT'][0].items()},
                                                 {key: str(path) for key, path in loaded['MULTI_AGENT'][0].items()})]
    failed = [row['check'] for row in rows if row['status'] != 'PASS']
    add('architectureParity', not failed, 'ARCHITECTURE_CONFIGURATION_DIFFERS', detail=failed or None)


def _browser_run(docker, image, browser, files, timeout=150):
    """dev 브라우저 도구 한 번을 dev 샌드박스와 같은 컨테이너 옵션으로 실행한다(2026-11-15 추가).

    컨테이너 옵션은 dev SandboxRuntime._create가 만든 것을 그대로 쓴다(권한 전부 제거·네트워크 없음·읽기 전용·
    UID 10001·no-new-privileges·기본 seccomp·--ipc none). 입력은 dev 실행기·계약 파일·도구 설정의 서비스 명령과
    시작 코드다. 시험은 준비 경로를 여는 것 하나뿐이고 생성 코드나 Host 파일·키는 넣지 않는다.
    돌려주는 값: 실패 사유 코드 또는 None(통과).
    """
    import tempfile
    from types import SimpleNamespace
    import mcp_tools.tools.browser_contract as contract_module
    import mcp_tools.tools.browser_runner as runner_module
    from mcp_tools.tools.browser_config import browser_host_payload
    from orchestrator.sandbox.contracts import ExecutionProfile
    from orchestrator.sandbox.runtime import SandboxRuntime
    suite = browser.suites[0]
    host = browser_host_payload(browser, suite)
    ready = host['ready_path']
    with tempfile.TemporaryDirectory(prefix='evaluation-browser-check-') as directory:
        root = Path(directory)
        snapshot, inputs = root / 'snapshot', root / 'inputs'
        documents = {**{snapshot / name: data for name, data in files.items()},
                     inputs / '_browser_runner.py': Path(runner_module.__file__).read_bytes(),
                     inputs / '_browser_contract.py': Path(contract_module.__file__).read_bytes(),
                     inputs / '_browser_host.json': json.dumps(host).encode('utf-8'),
                     inputs / suite.suite_path: json.dumps({'format': 'browser-suite-v1', 'tests': [
                         {'testId': 'preflight.ready', 'steps': [{'action': 'goto', 'path': ready},
                                                                 {'action': 'assert_url', 'path': ready}]}]}).encode('utf-8')}
        for path, data in documents.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        for path in [root, *root.rglob('*')]:
            path.chmod(0o755 if path.is_dir() else 0o644)  # 컨테이너 안 UID 10001이 읽을 수 있게
        profile = ExecutionProfile(name=suite.name, tool_name='run_browser_tests',
                                   argv=(browser.python_executable, '-I', '-B', '/inputs/_browser_runner.py'),
                                   limits=browser.limits, image_reference=browser.image_reference)
        name = 'evaluation-browser-check-' + uuid4().hex
        args = SandboxRuntime._create(name, {'evaluation.preflight': 'browser'},
                                      SimpleNamespace(source_root=snapshot, inputs_root=inputs), profile, image)
        failure = cleanup_failure = None
        try:
            created = subprocess.run([docker, *args], capture_output=True, text=True, timeout=30)
            if created.returncode != 0:
                failure = 'HOST_BROWSER_CONTAINER_NOT_CREATED'
            else:
                done = subprocess.run([docker, 'container', 'start', '--attach', name], capture_output=True, text=True,
                                      timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            failure = 'HOST_BROWSER_CHECK_TIMEOUT'
        finally:
            try:
                removed = subprocess.run([docker, 'container', 'rm', '-f', name], capture_output=True, timeout=15)
                if removed.returncode != 0:
                    cleanup_failure = 'HOST_BROWSER_CLEANUP_FAILED'
            except subprocess.TimeoutExpired:
                cleanup_failure = 'HOST_BROWSER_CLEANUP_TIMEOUT'
            except OSError:
                cleanup_failure = 'HOST_BROWSER_CLEANUP_UNAVAILABLE'
        # A successful force-removal response confirms termination/removal.
        # A failed/timed-out CLI does not; never publish readiness from its
        # earlier PASS output or silently start another browser container.
        if cleanup_failure is not None:
            return cleanup_failure
        if failure is not None:
            return failure
    try:
        report = json.loads(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return 'HOST_BROWSER_REPORT_INVALID'
    if not isinstance(report, dict):
        return 'HOST_BROWSER_REPORT_INVALID'
    if report.get('error') == 'BROWSER_START_FAILED':
        return 'HOST_BROWSER_START_FAILED'
    if (report.get('format') != 'browser-v1' or type(report.get('total')) is not int or report['total'] < 1
            or report.get('passed') != report['total'] or done.returncode != 0):
        return 'HOST_BROWSER_EXECUTION_FAILED'
    return None


def browser_execution_checks(plan, base, add, *, docker, files, enabled):
    """구조별 Host 브라우저 도구가 실제로 브라우저 시험을 끝까지 돌리는지 본다. 같은 이미지·설정은 한 번만 실행한다."""
    from mcp_tools.tools.browser_config import browser_host_payload, decode_browser_configuration
    configurations = plan.get('hostConfigurations') if isinstance(plan.get('hostConfigurations'), dict) else {}
    seen, cleanup_unverified = {}, False
    for architecture in ARCHITECTURES:
        name = f'hostBrowser.{architecture}'
        if not enabled:
            add(name, False, 'DOCKER_CHECK_DISABLED')
            continue
        if files is None:
            add(name, False, 'STARTING_SOURCE_NOT_CHECKED_OR_EXPOSED')
            continue
        try:
            # 브라우저 부분만 dev와 같은 디코더로 읽는다(dev 도구 설정 해석과 같은 공통 이미지·Docker 주소 주입).
            _paths, documents = _host_files(configurations.get(architecture), Path(base))
            tools = documents['tools']
            if tools.get('browser', 'MISSING') is None:
                # Explicitly absent capability, not a passing browser execution.
                add(name + '.notConfigured', True, None)
                continue
            image = tools['imageReference']
            browser = decode_browser_configuration(json.dumps({**tools['browser'], 'image_reference': image,
                                                               'docker_endpoint': tools['dockerEndpoint']}))
            key = (image, json.dumps(browser_host_payload(browser, browser.suites[0]), sort_keys=True))
        except Exception:
            add(name, False, 'HOST_BROWSER_CONFIGURATION_INVALID')
            continue
        if key not in seen:
            seen[key] = ('HOST_BROWSER_NOT_RUN_AFTER_UNVERIFIED_CLEANUP' if cleanup_unverified
                         else _browser_run(docker, image, browser, files))
            cleanup_unverified = cleanup_unverified or seen[key] in _BROWSER_CLEANUP_ERRORS
        add(name, seen[key] is None, seen[key])


def _call_limit_checks(limits, add, warnings):
    # 호출 한도가 의미 검토 호출까지 센 최소 흐름에도 모자라면 실험이 한도 초과로 끝난다(예시 설정의 8회가 그렇다).
    calls = limits.get('modelCalls') if isinstance(limits, dict) else None
    if type(calls) is not int:
        return
    add('modelCallFloor', calls >= CALLS_ONE_CYCLE, 'MODEL_CALL_LIMIT_BELOW_ONE_CYCLE')
    if CALLS_ONE_CYCLE <= calls < CALLS_ONE_FIX:
        warnings.append({'code': 'MODEL_CALL_LIMIT_BELOW_ONE_FIX_CYCLE', 'value': calls,
                         'detail': f'시험 하네스 기준 수정 1회 흐름은 의미 검토 포함 {CALLS_ONE_FIX}회 이상 호출한다.'})


def check_preparation(plan, *, base=Path('.'), docker='docker', connect_docker=True):
    checks, warnings = [], []
    from .experiment_run import ExperimentRunError, _planned_request_digest, _registered_run_token_limit

    def add(name, valid, reason, detail=None):
        row = {'check': name, 'status': 'PASS' if valid else 'UNVERIFIED', 'reason': None if valid else reason}
        if detail is not None and not valid:
            row['detail'] = detail
        checks.append(row)

    if type(connect_docker) is not bool:
        raise ValueError('DOCKER_CHECK_OPTION_INVALID')
    if not isinstance(plan, dict):
        raise ValueError('PLAN_INVALID')
    try:
        _planned_request_digest(plan)
    except ExperimentRunError as error:
        add('requestText', False, error.code)
    else:
        add('requestText', True, None)
    try:
        valid_id = UUID(plan.get('experimentId')).version == 4
    except (ValueError, TypeError, AttributeError):
        valid_id = False
    add('experimentId', valid_id, 'EXPERIMENT_ID_REQUIRED')
    add('scenario', plan.get('scenarioKey') == 'SCN-001', 'SCENARIO_NOT_SUPPORTED')
    model = plan.get('model') or {}
    add('model', isinstance(model, dict) and bool(model.get('provider')) and bool(model.get('modelId'))
        and type(model.get('temperature')) in (int, float) and 0 <= model['temperature'] <= 2,
        'MODEL_CONFIGURATION_REQUIRED')
    counts = plan.get('plannedRuns') or {}
    add('plannedRuns', isinstance(counts, dict) and set(counts) == {'SINGLE_AGENT', 'MULTI_AGENT'}
        and all(type(n) is int and n > 0 for n in counts.values()), 'PLANNED_RUN_COUNTS_REQUIRED')
    limits = plan.get('limits') or {}
    add('limits', isinstance(limits, dict)
        and all(type(limits.get(key)) is int and limits[key] > 0 for key in ('modelCalls', 'runtimeBudgetMs'))
        and limits.get('maxFixAttempts') == 3 and limits.get('maxToolRetries') == 2,
        'CALL_AND_RUNTIME_LIMITS_REQUIRED')
    add('loopPolicy', plan.get('loopPolicy') in ('IN_LOOP_AND_FINAL', 'FINAL_ONLY'), 'LOOP_POLICY_REQUIRED')
    add('executionOrder', plan.get('executionOrder') in ('ALTERNATING', 'RANDOMIZED_RECORDED'),
        'EXECUTION_ORDER_REQUIRED')
    add('warmupState', bool(plan.get('warmupState')), 'WARMUP_STATE_REQUIRED')
    add('hardwareProfile', bool(plan.get('hardwareProfile')), 'HARDWARE_PROFILE_REQUIRED')
    _call_limit_checks(limits, add, warnings)
    if plan.get('requireTotalTokenLimit') is True:
        add('runTotalTokenLimit', _registered_run_token_limit(plan), 'RUN_TOTAL_TOKEN_LIMIT_NOT_SET')
    elif plan.get('requireCostMeasurement') is True and not _registered_run_token_limit(plan):
        warnings.append({'code': 'RUN_TOTAL_TOKEN_LIMIT_NOT_SET'})
    source = plan.get('source')
    source_ok, lock_ok, clean, lock_bytes, files = False, False, False, None, None
    if isinstance(source, dict):
        try:
            files = starting_source_files(source, Path(base))
            source_ok = True
            lock_ok = isinstance(source.get('lockPath'), str) and bool(files.get(source['lockPath']))
            lock_bytes = files.get(source['lockPath']) if lock_ok else None
            clean = not protected_tests.exam_exposure(files)
        except Exception:
            pass  # 시작 코드나 예외의 자유문을 결과에 섞지 않는다.
    add('startingSource', source_ok, 'STARTING_COMMIT_UNAVAILABLE')
    add('dependencyLock', lock_ok, 'SOURCE_LOCK_UNAVAILABLE')
    add('examIsolation', source_ok and clean, 'STARTING_SOURCE_NOT_CHECKED_OR_EXPOSED')
    host_configuration_checks(plan, Path(base), add, warnings, lock_bytes=lock_bytes)
    try:
        runner = DockerRunner(plan.get('image'), docker=docker)
        pinned = True
    except (TypeError, ValueError):
        runner, pinned = None, False
    add('pinnedImage', pinned, 'PINNED_IMAGE_REQUIRED')
    available = False
    if connect_docker:
        try:
            result = subprocess.run([docker, 'info', '--format', '{{.ServerVersion}}'],
                                    capture_output=True, timeout=8)
            available = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    add('docker', available, 'DOCKER_UNAVAILABLE' if connect_docker else 'DOCKER_CHECK_DISABLED')
    image_ok = available and runner is not None and runner.image_digest() is not None
    add('imageAvailable', image_ok, 'IMAGE_UNAVAILABLE_OR_MISMATCH' if connect_docker else 'DOCKER_CHECK_DISABLED')
    packages = False
    if image_ok:
        # 승인된 이미지 안 패키지 메타데이터만 읽는다. 생성 코드·네트워크·Host 파일은 주지 않는다.
        code = ('from importlib.metadata import version; '
                '[version(n) for n in ("fastapi","httpx","sqlalchemy","argon2-cffi")]')
        container = 'evaluation-preflight-' + uuid4().hex
        try:
            result = subprocess.run([docker, 'run', '--rm', '--name', container, '--pull', 'never', '--network', 'none', '--read-only',
                                     '--user', '10001:10001', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                                     '--memory', '256m', '--cpus', '1', '--pids-limit', '64',
                                     '--entrypoint', '/usr/local/bin/python', plan['image'], '-I', '-B', '-c', code],
                                    capture_output=True, timeout=20)
            packages = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
        finally:
            # CLI 대기가 만료되어도 이 점검이 만든 컨테이너만 정리한다.
            try:
                subprocess.run([docker, 'rm', '-f', container], capture_output=True, timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                pass
    add('evaluationPackages', packages, 'EVALUATION_PACKAGES_NOT_VERIFIED' if connect_docker else 'DOCKER_CHECK_DISABLED')
    # 설정 파일 대조만으로는 Host 브라우저 도구가 실제로 뜨는지 알 수 없다(2026-11-15 Chromium 샌드박스 실패를 놓친 원인).
    browser_execution_checks(plan, Path(base), add, docker=docker, files=files if source_ok and clean else None,
                             enabled=image_ok)
    return {'schemaVersion': 1, 'ready': all(row['status'] == 'PASS' for row in checks),
            'checks': checks, 'warnings': warnings, 'scope': 'PREPARATION_ONLY',
            'dockerChecksEnabled': connect_docker,
            'limitations': ['공식 실험 승인·가격표·Provider 인증·실제 생성물 연결 검증은 별도다.',
                            'Host 설정은 파일로 대조한다. 실제 Run이 그 파일로 돌았는지는 evaluation.parity의 Run별 고정 해시로 확인한다.',
                            '최신 dev는 에이전트 결과마다 의미 검토 호출을 같은 Run 한도에서 쓰므로 호출 한도는 검토 호출을 포함해 정한다.',
                            f'호출 하한({CALLS_ONE_CYCLE}·{CALLS_ONE_FIX}회)은 시험 하네스 기준이며 실제 Run은 도구 호출만큼 더 쓴다.']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--no-docker', action='store_true', help='Docker에 접속하지 않고 파일·설정만 검사한다')
    args = parser.parse_args(argv)
    try:
        report = check_preparation(json.loads(args.plan.read_text()), base=args.plan.resolve().parent,
                                   connect_docker=not args.no_docker)
        text = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text)
        else:
            print(text, end='')
        return 0 if report['ready'] else 2
    except (OSError, ValueError, TypeError):
        sys.stderr.write('PREFLIGHT_INPUT_INVALID\n')
        return 1


if __name__ == '__main__':
    sys.exit(main())
