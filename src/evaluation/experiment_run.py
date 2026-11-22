"""측정 Run 실행 (개발정의서 11-A 실행 순서·실험 Run 선언, 측정 Run 질문·보류는 재개하지 않음).

    python -m evaluation.experiment_run runtime --plan <실험 계획 JSON> --architecture MULTI_AGENT \\
        --example configs/owned-runtime.example.json --out configs/owned-runtime.local.json
    python -m evaluation.experiment_run schedule --plan <실험 계획 JSON>
    python -m evaluation.experiment_run run --plan <실험 계획 JSON> --order <N> --out <결과 상위 폴더>
    python -m evaluation.experiment_run all --plan <실험 계획 JSON> --out <결과 상위 폴더>

runtime은 계획의 모델·한도(limits)·시간 예산을 Host 실행 설정 예시에 넣어 두 구조가 같은 값을 쓰게 한다.

실험 계획(experiment-plan)은 버전(loopPolicy) 하나당 한 파일이다. Ver1·Ver2 계획은 같은 experimentId를 쓴다.
구조별 hostConfigurations 항목에는 runtime·tools·source 외에 이 도구가 쓰는 칸을 둔다.
  api: 그 구조 Host의 주소(예: http://127.0.0.1:8000)
  protectedCases: Ver1(IN_LOOP_AND_FINAL)일 때 Host에 준 보호 케이스 파일(케이스 파일 2판)
  priceBook: Host에 준 가격표 파일(있으면)
하는 일:
  1. schedule: 반복마다 구조 순서를 번갈아(1회차 Single→Multi, 2회차 Multi→Single, …) 실행 순서(executionOrder)를 매긴다.
     RANDOMIZED_RECORDED면 experimentId로 정한 시드로 섞고 그 시드를 남긴다.
  2. run: dev Host 설정 검사(inspect_host_configuration, 읽기 전용)로 승인 설정을 얻고 experimentId·executionOrder·warmupState를
     넣어 Run을 제출한다(Host는 나머지 칸이 승인 값과 다르면 거부한다). 승인 설정의 보호 테스트 참조가 계획의 버전과 맞는지도 본다.
  3. 끝(FINISHED·ABORTED)이나 멈춤(WAITING_INPUT·HUMAN_REVIEW)까지 기다린다. 멈춘 Run에 답하거나 재개하지 않는다.
  4. dev evaluation_export와 같은 방식(모든 코드 버전 포함)으로 인계 폴더를 만들고, runs.jsonl에 결과 한 줄을 덧붙인다.
실제 Host(dev 83·85번)는 API마다 Bearer 토큰을, Run 생성에는 중복 방지 키를 요구한다. 토큰은 환경 변수
A2A_HOST_API_TOKEN에서만 읽고, 키는 제출 의도를 기록할 때 만들어 runs.jsonl에 함께 남긴다(같은 키 재전송은 같은 Run).
같은 원장의 신규 제출은 앞 순서들이 모두 수락되고 종료·보류로 확인된 뒤에만 가능하다. run --order는
계획의 다음 슬롯 실행 또는 이미 제출한 슬롯 복구에 사용하며, 미실행 순서를 건너뛰지 않는다.
채점·Agent별 평가표는 인계 폴더로 evaluation.workflow를 돌려 만든다.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import os
import random
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, Mapping, Optional
from uuid import UUID, uuid4

ARCHITECTURES = ('SINGLE_AGENT', 'MULTI_AGENT')
LOOP_POLICIES = ('IN_LOOP_AND_FINAL', 'FINAL_ONLY')
ENDED = ('FINISHED', 'ABORTED')
PAUSED = ('WAITING_INPUT', 'HUMAN_REVIEW')  # 측정 Run에서는 재개하지 않고 종료로 집계한다
DEFAULT_REQUEST_TEXT = '이메일과 비밀번호로 회원가입 기능을 만들어줘.'


class ExperimentRunError(Exception):
    def __init__(self, code: str, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def _object(value, code='PLAN_INVALID'):
    if not isinstance(value, Mapping):
        raise ExperimentRunError(code)
    return value


def _plan_id(plan: Mapping) -> UUID:
    try:
        value = UUID(str(plan['experimentId']))
    except (KeyError, ValueError, TypeError):
        raise ExperimentRunError('EXPERIMENT_ID_REQUIRED') from None
    if value.version != 4:
        raise ExperimentRunError('EXPERIMENT_ID_REQUIRED')
    return value


def schedule(plan: Mapping) -> dict:
    # 계획의 구조별 Run 수로 실행 순서를 정한다. 돌려주는 값: {'seed': 시드 또는 None, 'runs': [{order, architecture,
    # repetition, loopPolicy}, ...]}
    plan = _object(plan)
    counts = plan.get('plannedRuns') or {}
    if (not isinstance(counts, Mapping) or set(counts) != set(ARCHITECTURES)
            or any(type(counts[name]) is not int or counts[name] < 1 for name in ARCHITECTURES)):
        raise ExperimentRunError('PLANNED_RUN_COUNTS_REQUIRED')
    if plan.get('loopPolicy') not in LOOP_POLICIES:
        raise ExperimentRunError('LOOP_POLICY_REQUIRED')
    experiment_id = _plan_id(plan)
    rows = []
    for repetition in range(max(counts.values())):
        order = ARCHITECTURES if repetition % 2 == 0 else tuple(reversed(ARCHITECTURES))
        rows += [{'architecture': name, 'repetition': repetition + 1} for name in order if repetition < counts[name]]
    seed = None
    if plan.get('executionOrder') == 'RANDOMIZED_RECORDED':
        seed = experiment_id.int % (2 ** 32)
        random.Random(seed).shuffle(rows)
    elif plan.get('executionOrder') != 'ALTERNATING':
        raise ExperimentRunError('EXECUTION_ORDER_REQUIRED')
    return {'seed': seed, 'runs': [dict(row, order=index + 1, loopPolicy=plan['loopPolicy'])
                                   for index, row in enumerate(rows)]}


def _registered_run_token_limit(plan):
    """Plans that require a ceiling cannot submit until its value is registered."""
    limits = plan.get('limits')
    tokens = limits.get('totalTokens') if isinstance(limits, dict) else None
    return type(tokens) is int and tokens > 0


def _planned_request_digest(plan):
    from orchestrator.domain.submissions import measurement_request_sha256
    text = plan.get('requestText', DEFAULT_REQUEST_TEXT)
    if not isinstance(text, str) or not text.strip():
        raise ExperimentRunError('REQUEST_TEXT_INVALID')
    digest = measurement_request_sha256(text)
    if plan.get('requestTextSha256', digest) != digest:
        raise ExperimentRunError('REQUEST_TEXT_DIFFERS_FROM_REGISTRATION')
    return digest


def _host_entry(plan: Mapping, architecture: str, base: Path) -> dict:
    # 계획의 구조별 Host 칸에서 파일 경로(계획 파일 위치 기준)와 API 주소를 읽는다.
    configurations = _object(plan.get('hostConfigurations') or {}, 'HOST_CONFIGURATION_REQUIRED')
    entry = configurations.get(architecture)
    if not isinstance(entry, Mapping):
        raise ExperimentRunError('HOST_CONFIGURATION_REQUIRED')

    def path(key, required=True):
        value = entry.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not value:
            raise ExperimentRunError('HOST_CONFIGURATION_REQUIRED')
        candidate = Path(value)
        return candidate if candidate.is_absolute() else (Path(base) / candidate).resolve()

    api = entry.get('api')
    if not isinstance(api, str) or not api.startswith(('http://', 'https://')):
        raise ExperimentRunError('HOST_API_REQUIRED')
    return {'api': api.rstrip('/'), 'runtime': path('runtime'), 'tools': path('tools'), 'source': path('source'),
            'protectedCases': path('protectedCases', required=False), 'priceBook': path('priceBook', required=False)}


def approved_configuration(plan: Mapping, architecture: str, base: Path):
    # dev Host가 승인하는 실행 설정(RunConfiguration)을 Host와 같은 검사로 얻는다. 자격 증명·저장소는 열지 않는다.
    from agents.platform.host import inspect_host_configuration
    entry = _host_entry(plan, architecture, base)
    try:
        inspected = inspect_host_configuration(
            runtime_file=entry['runtime'], tools_file=entry['tools'], source_file=entry['source'],
            protected_cases_file=entry['protectedCases'], price_book_file=entry['priceBook'],
            architecture=architecture)
    except Exception:
        raise ExperimentRunError('HOST_CONFIGURATION_INVALID') from None
    approved = inspected.approved_configuration
    in_loop = approved.protected_test_suite_ref is not None
    if in_loop != (plan.get('loopPolicy') == 'IN_LOOP_AND_FINAL'):
        raise ExperimentRunError('LOOP_POLICY_MISMATCH')  # Ver1 계획에 Ver2 설정(또는 반대)을 준 경우
    return approved


def submission(plan: Mapping, approved, order: int) -> dict:
    # 승인 설정에 실험 Run 선언 칸만 넣은 Run 생성 요청 본문
    from orchestrator.domain.run_configuration import RunConfiguration
    from orchestrator.domain.scenario_registry import SCN_001_ID
    if plan.get('scenarioKey') != 'SCN-001':
        raise ExperimentRunError('SCENARIO_NOT_SUPPORTED')
    if not isinstance(plan.get('warmupState'), str) or not plan['warmupState'].strip():
        raise ExperimentRunError('WARMUP_STATE_REQUIRED')
    data = approved.model_dump(mode='json', by_alias=True)
    data.update({'experimentId': str(_plan_id(plan)), 'executionOrder': order, 'warmupState': plan['warmupState']})
    try:
        configuration = RunConfiguration.model_validate(data)
    except Exception:
        raise ExperimentRunError('EXPERIMENT_BASELINE_INCOMPLETE') from None
    request_text = plan.get('requestText', DEFAULT_REQUEST_TEXT)
    _planned_request_digest(plan)
    return {'scenarioId': str(SCN_001_ID), 'requestText': request_text,
            'configuration': configuration.model_dump(mode='json', by_alias=True)}


HOST_TOKEN_ENV = 'A2A_HOST_API_TOKEN'  # dev 83번: 실제 Host API의 서버 전용 Bearer 토큰(에이전트·모델 토큰과 별개)


def host_client(api: str):
    """실제 Host API 클라이언트. 토큰은 환경 변수에서만 읽고 기록·결과 파일에 남기지 않는다."""
    token = os.environ.get(HOST_TOKEN_ENV)
    if not token:
        raise ExperimentRunError('HOST_API_TOKEN_REQUIRED')
    import httpx
    return httpx.Client(base_url=api, timeout=30, headers={'Authorization': f'Bearer {token}'})


def _host_request_hash(body: Mapping) -> str:
    # dev 85번이 접수 기록에 남기는 requestSha256과 같은 방식(원본 요청 JSON을 키 정렬·공백 없이 직렬화)
    return sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


def submit(client, body: Mapping, *, idempotency_key: Optional[str] = None) -> str:
    # dev 85번: 실제 Host는 Run 생성에 Idempotency-Key(UUIDv4)를 요구한다. 같은 키·같은 본문을 다시 보내면 새 Run을 만들지
    # 않고 처음 접수 응답을 200으로 돌려준다(그래서 200도 성공이다). 같은 키에 다른 본문이면 409다.
    headers = {} if idempotency_key is None else {'Idempotency-Key': idempotency_key}
    response = client.post('/api/v1/runs', json=body, headers=headers)
    if response.status_code not in (200, 201):
        raise ExperimentRunError(f'RUN_SUBMISSION_REJECTED_{response.status_code}')
    return _run_id(response.json()['run']['runId'])


def wait(client, run_id: str, *, timeout_seconds: float, poll_seconds: float = 5.0,
         clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> str:
    # 끝·멈춤 상태가 될 때까지 기다린다. 시간이 지나도 진행 중이면 'TIMEOUT'을 돌려준다(Run을 취소하지 않는다).
    started = clock()
    while True:
        response = client.get(f'/api/v1/runs/{run_id}')
        if response.status_code != 200:
            raise ExperimentRunError(f'RUN_STATUS_UNAVAILABLE_{response.status_code}')
        status = response.json().get('status')
        if status in ENDED or status in PAUSED:
            return status
        if clock() - started >= timeout_seconds:
            return 'TIMEOUT'
        sleep(poll_seconds)


def _stores(runtime_file: Path, architecture: str) -> tuple[Path, dict]:
    # runtime 설정의 DB 경로(설정 파일 위치 기준)로 (orchestrator DB, {에이전트 이름: DB})를 만든다.
    runtime = json.loads(Path(runtime_file).read_text(encoding='utf-8'))
    base = Path(runtime_file).parent

    def resolve(value):
        path = Path(value)
        return path if path.is_absolute() else (base / path).resolve()

    agents = runtime.get('agents') or {}
    expected = {'SINGLE_AGENT'} if architecture == 'SINGLE_AGENT' else {'PLANNER', 'DEVELOPER', 'QA', 'SECURITY'}
    if set(agents) != expected:
        raise ExperimentRunError('RUNTIME_AGENTS_MISMATCH')
    return (resolve(runtime['orchestrator']['databasePath']),
            {name: str(resolve(item['databasePath'])) for name, item in sorted(agents.items())})


def export(plan: Mapping, architecture: str, run_id: str, out: Path, base: Path) -> Path:
    # dev evaluation_export와 같은 인계 폴더(모든 코드 버전 포함). --final-only는 쓰지 않는다.
    from agents.platform.evaluation_export import write_evaluation_export
    from agents.platform.export_readonly import ReadOnlyWorkflowRepository
    entry = _host_entry(plan, architecture, base)
    database, stores = _stores(entry['runtime'], architecture)
    repository = ReadOnlyWorkflowRepository(database)
    wait_package_production(repository, run_id)
    write_evaluation_export(repository, run_id, Path(out), stores=stores, all_versions=True)
    return Path(out)


def wait_package_production(repository, run_id, *, timeout_seconds=30.0, poll_seconds=0.2,
                            clock=time.monotonic, sleep=time.sleep):
    """A terminal Run may precede Host package commit. Wait without producing it."""
    from agents.platform.package_records import package_production_status
    if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0
            for value in (timeout_seconds, poll_seconds))):
        raise ExperimentRunError('PACKAGE_WAIT_INVALID')
    started = clock()
    while True:
        try:
            record = package_production_status(repository, run_id)
            if (record.get('runId') != str(run_id) or record.get('status') not in
                    ('PENDING', 'COMPLETED', 'NOT_COMPLETED', 'NOT_CONFIGURED', 'ERROR')):
                raise ValueError()
        except Exception:
            raise ExperimentRunError('PACKAGE_STATUS_UNAVAILABLE') from None
        if record['status'] != 'PENDING':
            return record  # Missing/failed packages remain explicit in the handoff.
        if clock() - started >= timeout_seconds:
            raise ExperimentRunError('PACKAGE_PRODUCTION_TIMEOUT')
        sleep(min(poll_seconds, timeout_seconds))


def _digest(value):
    found = re.search(r'sha256:[0-9a-f]{64}', value) if isinstance(value, str) else None
    return found.group(0) if found else None


def optional_limit_differences(limits, llm):
    return [key for key, target in OPTIONAL_RUNTIME_LIMITS.items()
            if limits.get(key, 0) != llm.get(target, 0)]


def plan_differences(plan: Mapping, approved, runtime: Mapping) -> list:
    # 계획과 실제 Host 설정(승인 실행 설정·Host 실행 설정 파일)이 다른 칸 이름 목록. 계획 칸이 비어 있어도 다르다고 본다.
    # 같은 조건으로만 비교한다는 11-A 계약을 제출 직전에 지키기 위한 대조다(Ver1·Ver2 보호 테스트 참조는 따로 본다).
    model, limits, source = (_object(plan.get(key) or {}) for key in ('model', 'limits', 'source'))
    runtime = _object(runtime, 'HOST_CONFIGURATION_INVALID')
    actual_model = approved.model.model_dump(by_alias=True) if approved.model is not None else {}
    differences = [f'model.{key}' for key in ('provider', 'modelId', 'modelRevision', 'temperature', 'seed')
                   if key not in model or model.get(key) != actual_model.get(key)]
    llm = runtime.get('llmLimits') if isinstance(runtime.get('llmLimits'), Mapping) else {}
    differences += [f'limits.{key}' for key, target in RUNTIME_LIMITS.items()
                    if key not in limits or target not in llm or limits[key] != llm[target]]
    # 부작용 전 형식 실수의 재생성 한도와 자율 실행의 질문 안내 횟수(없으면 0 = 끔, agents.llm.corrections).
    # 두 구조가 같은 값이어야 한다.
    differences.extend(f'limits.{key}' for key in optional_limit_differences(limits, llm))
    actual_limits = approved.limits
    for key, value in (('runtimeBudgetMs', actual_limits.runtime_budget_ms),
                       ('maxFixAttempts', actual_limits.max_fix_attempts),
                       ('maxToolRetries', actual_limits.max_tool_retries)):
        if limits.get(key) is None or limits.get(key) != value:
            differences.append(f'limits.{key}')
    if source.get('commitHash') is None or source.get('commitHash') != approved.starting_commit_hash:
        differences.append('source.commitHash')
    environment = approved.environment
    image = _digest(plan.get('image'))
    if image is None or environment is None or image != environment.container_image_digest:
        differences.append('image')
    if plan.get('hardwareProfile') is None or environment is None or plan['hardwareProfile'] != environment.hardware_profile:
        differences.append('hardwareProfile')
    return differences


LEDGER = 'runs.jsonl'
LEDGER_VERSION = 3  # 3: 제출 의도에 Host 중복 제출 방지 키(idempotencyKey)와 Host 요청 지문(hostRequestSha256)을 남긴다


def _hash(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


def _run_id(value):
    parsed = UUID(str(value))
    if parsed.version != 4:
        raise ValueError('Run ID must be UUID4')
    return str(parsed)


def _plan_fingerprint(plan, base):
    # Bind the complete registration, effective request, file base, and scheduled slots.
    # Store hashes only; request text and local configuration contents stay out of the journal.
    effective = dict(plan, requestText=plan.get('requestText') or DEFAULT_REQUEST_TEXT)
    return _hash({'plan': effective, 'schedule': schedule(plan), 'base': str(Path(base).resolve())})


@contextmanager
def _run_lock(out_root):
    # One writer/active slot per output journal, including across separate CLI processes.
    # A nonblocking lock avoids silently starting a second overlapping measurement.
    import fcntl
    root = Path(out_root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.runs.lock').open('a') as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExperimentRunError('EXPERIMENT_RUN_BUSY') from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _ledger(out_root: Path) -> dict:
    # runs.jsonl(사건을 덧붙이는 기록)을 읽어 (experimentId, loopPolicy, order)마다 마지막 상태를 만든다.
    # SUBMITTING is durable before the request. An intent without a Run ID must
    # be reconciled explicitly; absence of a response never permits a new POST.
    state = {}
    path = Path(out_root) / LEDGER
    if not path.is_file():
        return state
    for line in path.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            key = (item['experimentId'], item['loopPolicy'], item['order'])
            if (item.get('schemaVersion') != LEDGER_VERSION or not isinstance(item.get('planFingerprint'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', item['planFingerprint'])):
                raise ExperimentRunError('LEGACY_OR_UNBOUND_LEDGER')
            if type(item['order']) is not int or item['order'] < 1:
                raise ValueError()
            if item.get('event') not in {'SUBMITTING', 'SUBMITTED', 'SUBMISSION_UNCERTAIN', 'ENDED', 'EXPORTED', 'EXPORT_FAILED'}:
                raise ValueError()
            if item['event'] == 'SUBMITTING':
                if 'requestTextSha256' in item and not re.fullmatch(r'[0-9a-f]{64}', item['requestTextSha256']):
                    raise ValueError()
                if any(not isinstance(item.get(name), str) or not re.fullmatch(r'[0-9a-f]{64}', item[name])
                       for name in ('submissionSha256', 'configurationSha256', 'hostRequestSha256')):
                    raise ValueError()
                if datetime.fromisoformat(item['intentAt']).utcoffset() is None:
                    raise ValueError()
                key_value = UUID(item['idempotencyKey'])
                if key_value.version != 4 or str(key_value) != item['idempotencyKey']:
                    raise ValueError()
            if item['event'] == 'ENDED' and item.get('status') not in (*ENDED, *PAUSED, 'TIMEOUT'):
                raise ValueError()
            if 'runId' in item:
                item['runId'] = _run_id(item['runId'])
        except (ValueError, KeyError, TypeError):
            raise ExperimentRunError('LEDGER_INVALID') from None
        current = state.setdefault(key, {})
        if current.get('planFingerprint') not in (None, item['planFingerprint']):
            raise ExperimentRunError('LEDGER_PLAN_CHANGED')
        if 'runId' in item and current.get('runId') not in (None, item['runId']):
            raise ExperimentRunError('LEDGER_HAS_TWO_RUNS_FOR_ONE_ORDER')
        if item.get('event') == 'SUBMITTING':
            if current:
                raise ExperimentRunError('LEDGER_HAS_TWO_SUBMISSION_INTENTS')
            if 'requestTextSha256' in item:
                current['requestTextSha256'] = item['requestTextSha256']
            current.update({name: item[name] for name in (
                'intentAt', 'submissionSha256', 'configurationSha256', 'hostRequestSha256', 'idempotencyKey')})
        elif not current.get('intentAt'):
            raise ExperimentRunError('LEGACY_OR_UNBOUND_LEDGER')
        current.update({name: item[name] for name in ('runId', 'architecture', 'repetition', 'planFingerprint') if name in item})
        if item.get('event') == 'ENDED':
            current['status'] = item.get('status')
        elif item.get('event') == 'EXPORTED':
            current['export'] = item.get('export')
            current.pop('exportError', None)
        elif item.get('event') == 'EXPORT_FAILED':
            current['exportError'] = item.get('error')
            # A formerly verified folder may now be incomplete or invalid.
            # Keep the events, but derive current state from the latest outcome.
            current.pop('export', None)
    return state


def _record(out_root: Path, plan: Mapping, row: Mapping, event: str, *, fingerprint: str, **fields) -> None:
    # 사건 한 줄을 바로 덧붙인다(그다음 단계가 실패해도 수락된 Run이 기록에 남는다).
    Path(out_root).mkdir(parents=True, exist_ok=True)
    item = {'schemaVersion': LEDGER_VERSION, 'planFingerprint': fingerprint,
            'event': event, 'experimentId': str(_plan_id(plan)), 'loopPolicy': plan['loopPolicy'],
            'order': row['order'], 'architecture': row['architecture'], 'repetition': row['repetition'], **fields}
    path = Path(out_root) / LEDGER
    previous = path.read_bytes() if path.exists() else b''
    # Atomic replacement under _run_lock keeps an interrupted write from leaving
    # a torn JSONL tail. A failed write before SUBMITTING prevents the POST.
    staged = None
    try:
        with tempfile.NamedTemporaryFile(dir=out_root, prefix='.runs-', delete=False) as handle:
            staged = Path(handle.name)
            handle.write(previous + (json.dumps(item, ensure_ascii=False) + '\n').encode('utf-8'))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
        descriptor = os.open(out_root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if staged is not None:
            staged.unlink(missing_ok=True)


def _submission_hash(body):
    from orchestrator.api.schemas.runs import CreateRunRequest
    # Match Host trimming/redaction and defaults, not merely the wire spelling.
    return _hash(CreateRunRequest.model_validate(body).model_dump(mode='json', by_alias=True))


def _reconcile_submission(plan, row, state, entry, base, client_factory):
    """응답을 못 받은 제출 의도의 명시적 복구(--reconcile-submission). dev 85·86번 접수 계약을 쓴다.

    제출 의도에 남긴 같은 키로 Host 접수 기록을 조회한다. 기록이 있으면 그 Run을 이어 쓰고(요청 지문이 같아야 함),
    "기록 없음"이 확인되면 Run이 만들어지지 않은 것이므로 같은 키·같은 본문으로만 다시 보낸다. Host가 키로 중복을
    막으므로 Run이 둘 생기지 않는다. 그 밖의 응답(옛 Host의 404·장애)이면 다시 보내지 않고 멈춘다.
    """
    key = state['idempotencyKey']
    try:
        with client_factory(entry['api']) as client:
            response = client.get(f'/api/v1/submissions/{key}')
            if response.status_code == 200:
                data = response.json()
                if (data.get('internalKey') != key or data.get('status') != 'CREATED'
                        or data.get('requestSha256') != state['hostRequestSha256']):
                    raise ExperimentRunError('SUBMISSION_RECONCILIATION_CONFLICT')
                return _run_id(data['runId'])
            detail = response.json().get('detail') if response.status_code == 404 else None
            if detail != 'SUBMISSION_NOT_FOUND':
                raise ExperimentRunError('SUBMISSION_RECONCILIATION_UNAVAILABLE')
            body = submission(plan, approved_configuration(plan, row['architecture'], base), row['order'])
            if (_host_request_hash(body) != state['hostRequestSha256']
                    or _submission_hash(body) != state['submissionSha256']):
                raise ExperimentRunError('SUBMISSION_RECONCILIATION_CONFLICT')
            return submit(client, body, idempotency_key=key)
    except ExperimentRunError:
        raise
    except Exception:
        raise ExperimentRunError('SUBMISSION_RECONCILIATION_UNAVAILABLE') from None


def _verified_export(folder, run_id, architecture, status, configuration_hash):
    from .workflow import load_handoff
    from orchestrator.domain.run_configuration import RunConfiguration
    if folder.is_symlink():
        raise ExperimentRunError('EXPORT_IDENTITY_MISMATCH')
    try:
        handoff, run = load_handoff(folder)
    except Exception:
        return False
    versions, final = run.get('exportedCodeVersions'), run.get('finalCodeVersion')
    if not (final is None and versions == []):
        if (type(final) is not int or not 1 <= final <= 4 or not isinstance(versions, list)
                or any(type(version) is not int for version in versions)
                or sorted(versions) != list(range(1, final + 1))):
            return False
    # load_handoff checks every declared file, but a truncated declaration could
    # omit the snapshots. An all-versions export must include each required byte.
    required = {f'v{version}/{name}' for version in versions for name in ('source.tar', 'manifest.json')}
    if not required <= handoff['handoff']['files'].keys():
        return False
    if (run['runId'] != run_id or handoff['handoff']['architecture'] != architecture
            or run['status'] != status):
        raise ExperimentRunError('EXPORT_IDENTITY_MISMATCH')
    try:
        actual = RunConfiguration.model_validate(handoff['bundle']['configuration']['configuration'])
        if _hash(actual.model_dump(mode='json', by_alias=True)) != configuration_hash:
            raise ValueError()
    except Exception:
        raise ExperimentRunError('EXPORT_CONFIGURATION_MISMATCH') from None
    return True


def _export_or_recover(plan, architecture, run_id, folder, base, status, configuration_hash, exporter):
    if folder.exists():
        if _verified_export(folder, run_id, architecture, status, configuration_hash):
            return str(folder), True
        # Preserve incomplete bytes for diagnosis. Never overwrite another folder.
        quarantine = folder.with_name(f'.{folder.name}.incomplete-{uuid4().hex}')
        os.rename(folder, quarantine)
    exported = Path(exporter(plan, architecture, run_id, folder, base))
    if exported.resolve() != folder.resolve() or not _verified_export(folder, run_id, architecture, status, configuration_hash):
        raise ExperimentRunError('EXPORT_INCOMPLETE')
    return str(folder), False


def run_one(plan: Mapping, row: Mapping, out_root: Path, base: Path, *, client_factory=None,
            poll_seconds: float = 5.0, sleep=time.sleep, clock=time.monotonic, exporter=export,
            reconcile_submission: bool = False) -> dict:
    if type(poll_seconds) not in (int, float) or not math.isfinite(poll_seconds) or poll_seconds <= 0:
        raise ExperimentRunError('POLL_INTERVAL_INVALID')
    with _run_lock(out_root):
        return _run_one_locked(plan, row, out_root, base, client_factory=client_factory, poll_seconds=poll_seconds,
                               sleep=sleep, clock=clock, exporter=exporter, reconcile_submission=reconcile_submission)


def _run_one_locked(plan, row, out_root, base, *, client_factory, poll_seconds, sleep, clock, exporter,
                    reconcile_submission):
    # 실행 순서 한 줄(row)을 제출·대기·인계한다. 각 단계 결과를 runs.jsonl에 바로 남기고, 다시 부르면
    # 이미 수락된 Run을 새로 제출하지 않고 그 Run의 대기·인계를 이어 한다.
    planned = schedule(plan)['runs']
    if (type(row.get('order')) is not int or type(row.get('repetition')) is not int
            or not any(all(row.get(k) == expected[k] for k in ('order', 'architecture', 'repetition')) for expected in planned)):
        raise ExperimentRunError('ORDER_NOT_PLANNED')
    architecture = row['architecture']
    if row.get('loopPolicy', plan.get('loopPolicy')) != plan.get('loopPolicy'):
        raise ExperimentRunError('LOOP_POLICY_MISMATCH')
    key = (str(_plan_id(plan)), plan['loopPolicy'], row['order'])
    fingerprint = _plan_fingerprint(plan, base)
    journal = _ledger(out_root)
    if any(value['planFingerprint'] != fingerprint for identity, value in journal.items() if identity[:2] == key[:2]):
        raise ExperimentRunError('LEDGER_PLAN_CHANGED')
    request_digest = _planned_request_digest(plan)
    if any(value.get('requestTextSha256') not in (None, request_digest) for identity, value in journal.items()
           if identity[0] == key[0]):
        raise ExperimentRunError('EXPERIMENT_REQUEST_MISMATCH')
    state = journal.get(key, {})
    if state and state.get('architecture') not in (None, architecture):
        raise ExperimentRunError('LEDGER_ORDER_MISMATCH')
    entry = _host_entry(plan, architecture, base)
    budget_ms = _object(plan.get('limits') or {}).get('runtimeBudgetMs')
    if client_factory is None:
        client_factory = host_client
    run_id, reused = state.get('runId'), state.get('runId') is not None
    configuration_hash = state.get('configurationSha256')
    status = state.get('status') if state.get('status') not in (None, 'TIMEOUT') else None
    if run_id is None and state:
        if not reconcile_submission:
            raise ExperimentRunError('SUBMISSION_REQUIRES_RECONCILIATION')
        run_id = _reconcile_submission(plan, row, state, entry, base, client_factory)
        reused = True
        _record(out_root, plan, row, 'SUBMITTED', fingerprint=fingerprint, runId=run_id, reconciled=True)
    elif run_id is None:
        # Waiting TIMEOUT is not a Host terminal state. The same journal must
        # not launch another measured Run while an earlier submission is still
        # active or uncertain, including a separate `run --order` invocation.
        # Existing Run recovery/export remains available above and below this gate.
        if any(identity != key and value.get('status') not in (*ENDED, *PAUSED)
               for identity, value in journal.items()):
            raise ExperimentRunError('PREVIOUS_RUN_NOT_CLOSED')
        # A missing predecessor is not a completed slot. Otherwise an explicit
        # `run --order N` could silently replace the registered execution order.
        # Export success is deliberately not required: a closed Run with an
        # interrupted export may be recovered after subsequent slots finish.
        for previous in planned[:row['order'] - 1]:
            prior = journal.get((*key[:2], previous['order']), {})
            if (not prior.get('runId') or prior.get('status') not in (*ENDED, *PAUSED)
                    or any(prior.get(name) != previous[name] for name in ('architecture', 'repetition'))):
                raise ExperimentRunError('PREVIOUS_ORDER_NOT_COMPLETED')
        if plan.get('requireTotalTokenLimit') is True and not _registered_run_token_limit(plan):
            raise ExperimentRunError('RUN_TOTAL_TOKEN_LIMIT_NOT_SET')
        approved = approved_configuration(plan, architecture, base)
        if plan.get('requireCostMeasurement') is True or entry.get('priceBook') is not None:
            from agents.platform.pricing import has_requested_model_rate
            if not has_requested_model_rate(entry.get('priceBook'), plan.get('model')):
                raise ExperimentRunError('COST_UNAVAILABLE_FOR_REQUESTED_MODEL')
        if approved.architecture != architecture:
            raise ExperimentRunError('PLAN_CONFIGURATION_MISMATCH', ['architecture'])
        differences = plan_differences(plan, approved, json.loads(Path(entry['runtime']).read_text(encoding='utf-8')))
        if differences:
            raise ExperimentRunError('PLAN_CONFIGURATION_MISMATCH', differences)
        body = submission(plan, approved, row['order'])
        budget_ms = approved.limits.runtime_budget_ms
        configuration_hash = _hash(body['configuration'])
        submission_key = str(uuid4())  # Host 중복 제출 방지 키(위 원장 키 key와 다른 값)
        # 클라이언트를 먼저 만든다: 토큰이 없으면 제출 의도를 남기기 전에 멈춘다(보낸 요청이 없다).
        client = client_factory(entry['api'])
        with client:
            _record(out_root, plan, row, 'SUBMITTING', fingerprint=fingerprint,
                    intentAt=datetime.now(timezone.utc).isoformat(), submissionSha256=_submission_hash(body),
                    configurationSha256=configuration_hash, hostRequestSha256=_host_request_hash(body),
                    idempotencyKey=submission_key, requestTextSha256=request_digest)
            try:
                run_id = submit(client, body, idempotency_key=submission_key)
            except Exception:
                # Intent remains durable even if recording this diagnostic fails.
                _record(out_root, plan, row, 'SUBMISSION_UNCERTAIN', fingerprint=fingerprint)
                raise ExperimentRunError('SUBMISSION_OUTCOME_UNKNOWN') from None
        _record(out_root, plan, row, 'SUBMITTED', fingerprint=fingerprint, runId=run_id)
    if type(budget_ms) is not int or budget_ms <= 0:
        raise ExperimentRunError('RUNTIME_BUDGET_REQUIRED')
    if status is None:
        with client_factory(entry['api']) as client:
            status = wait(client, run_id, timeout_seconds=budget_ms / 1000 + 300, poll_seconds=poll_seconds,
                          clock=clock, sleep=sleep)
        _record(out_root, plan, row, 'ENDED', fingerprint=fingerprint, runId=run_id, status=status)
    exported = None
    if status != 'TIMEOUT':
        folder = Path(out_root) / f"{row['order']:03d}-{architecture.lower()}-{run_id}"
        if state.get('export') is not None and Path(state['export']).resolve() != folder.resolve():
            raise ExperimentRunError('LEDGER_EXPORT_MISMATCH')
        try:
            exported, recovered = _export_or_recover(plan, architecture, run_id, folder, base, status, configuration_hash, exporter)
        except Exception as error:
            _record(out_root, plan, row, 'EXPORT_FAILED', fingerprint=fingerprint, runId=run_id,
                    error=error.code if isinstance(error, ExperimentRunError) else type(error).__name__)
            if isinstance(error, ExperimentRunError):
                raise
            raise ExperimentRunError('EXPORT_FAILED') from None
        if state.get('export') != exported:
            _record(out_root, plan, row, 'EXPORTED', fingerprint=fingerprint,
                    runId=run_id, export=exported, recovered=recovered)
    return {'order': row['order'], 'architecture': architecture, 'repetition': row['repetition'],
            'loopPolicy': plan['loopPolicy'], 'experimentId': str(_plan_id(plan)), 'runId': run_id,
            'status': status, 'resumed': False, 'reusedSubmission': reused, 'export': exported}


# 계획 limits 칸 → Host 실행 설정 llmLimits 칸. 두 구조가 같은 한도를 쓰도록 계획 한 곳에서 채운다.
RUNTIME_LIMITS = {'modelCalls': 'max_model_calls', 'toolCalls': 'max_tool_calls', 'outputTokens': 'max_output_tokens',
                  'totalTokens': 'max_total_tokens', 'modelTimeoutSeconds': 'model_timeout_seconds',
                  'toolTimeoutSeconds': 'tool_timeout_seconds'}
# 없으면 0(끔)으로 보는 한도. 측정 Run 전용 동작이라 제품 Host 설정에는 넣지 않는다(docs/live-api/02-failure-types-and-recovery.md).
OPTIONAL_RUNTIME_LIMITS = {'outputCorrections': 'max_output_corrections', 'questionReminders': 'max_question_reminders',
                           'providerRetries': 'max_provider_retries'}


def runtime_configuration(example: Mapping, plan: Mapping, architecture: str) -> dict:
    # Host 실행 설정 예시(configs/*-runtime.example.json)에 계획의 모델·한도·시간 예산을 넣는다.
    # Orchestrator 포트는 계획의 api 주소를 따른다(두 구조의 Host를 함께 띄울 수 있게).
    import copy
    from urllib.parse import urlparse
    document = copy.deepcopy(dict(_object(example, 'RUNTIME_CONFIGURATION_INVALID')))
    plan = _object(plan)
    model, limits = (_object(plan.get(key) or {}) for key in ('model', 'limits'))
    document['model'] = {key: model.get(key) for key in ('provider', 'modelId', 'modelRevision', 'temperature', 'seed')}
    llm = dict(document.get('llmLimits') or {})
    llm.update({target: limits[source] for source, target in RUNTIME_LIMITS.items() if source in limits})
    llm.update({target: limits[source] for source, target in OPTIONAL_RUNTIME_LIMITS.items() if source in limits})
    document['llmLimits'] = llm
    document['runtimeBudgetMs'] = limits.get('runtimeBudgetMs')
    configurations = _object(plan.get('hostConfigurations') or {}, 'HOST_CONFIGURATION_REQUIRED')
    api = _object(configurations.get(architecture) or {}, 'HOST_CONFIGURATION_REQUIRED').get('api')
    port = urlparse(api).port if isinstance(api, str) else None
    if port is not None:
        document.setdefault('orchestrator', {})['port'] = port
    return document


def write_runtime_configuration(example_file: Path, plan: Mapping, architecture: str, out: Path,
                                force: bool = False) -> Path:
    # 위 설정을 쓰고 dev 설정 읽기(load_runtime_configuration)로 확인한다. dev가 받지 않으면 쓴 파일을 지운다.
    from agents.platform.configuration import load_runtime_configuration
    out = Path(out)
    if out.exists() and not force:
        raise ExperimentRunError('OUTPUT_EXISTS')
    document = runtime_configuration(json.loads(Path(example_file).read_text(encoding='utf-8')), plan, architecture)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 같은 폴더의 임시 파일에 쓰고 검사한 뒤 바꿔 끼운다. 검사에 실패하면 임시 파일만 지우고 기존 파일은 그대로 둔다.
    staged = out.with_name(f'.{out.name}.{os.getpid()}.tmp')
    staged.write_text(json.dumps(document, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    try:
        load_runtime_configuration(staged, architecture=architecture)
    except Exception:
        staged.unlink(missing_ok=True)
        raise ExperimentRunError('RUNTIME_CONFIGURATION_INVALID') from None
    os.replace(staged, out)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m evaluation.experiment_run', description='측정 Run 실행(11-A)')
    commands = parser.add_subparsers(dest='command', required=True)
    item = commands.add_parser('runtime', help='계획의 모델·한도로 Host 실행 설정 만들기')
    item.add_argument('--plan', required=True, type=Path)
    item.add_argument('--architecture', required=True, choices=ARCHITECTURES)
    item.add_argument('--example', required=True, type=Path, help='configs/owned-runtime.example.json 등')
    item.add_argument('--out', required=True, type=Path)
    item.add_argument('--force', action='store_true')
    for name in ('schedule', 'run', 'all'):
        item = commands.add_parser(name)
        item.add_argument('--plan', required=True, type=Path)
        if name != 'schedule':
            item.add_argument('--out', required=True, type=Path)
            item.add_argument('--poll-seconds', type=float, default=5.0)
            item.add_argument('--reconcile-submission', action='store_true',
                              help='응답을 못 받은 제출을 같은 중복 방지 키로 Host 접수 조회. 접수 기록이 없다고 확인될 때만 '
                                   '같은 키·같은 본문으로 다시 보낸다(Host가 중복 Run을 막음)')
        if name == 'run':
            item.add_argument('--order', required=True, type=int)
    args = parser.parse_args(argv)
    try:
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        base = args.plan.resolve().parent
        if args.command == 'runtime':
            print(write_runtime_configuration(args.example, plan, args.architecture, args.out, args.force))
            return 0
        planned = schedule(plan)
        if args.command == 'schedule':
            print(json.dumps(planned, ensure_ascii=False, indent=2))
            return 0
        rows = planned['runs'] if args.command == 'all' else [row for row in planned['runs']
                                                                if row['order'] == args.order]
        if not rows:
            raise ExperimentRunError('ORDER_NOT_PLANNED')
        for row in rows:
            result = run_one(plan, row, args.out, base, poll_seconds=args.poll_seconds,
                             reconcile_submission=args.reconcile_submission)
            print(json.dumps(result, ensure_ascii=False))
            if result['status'] == 'TIMEOUT':
                # Preserve the accepted Run and stop this batch. A later call
                # polls that same Run; it neither cancels nor replaces it.
                raise ExperimentRunError('RUN_WAIT_TIMEOUT')
    except ExperimentRunError as error:
        sys.stderr.write(error.code + (' ' + ','.join(error.detail) if error.detail else '') + '\n')
        return 1
    except (OSError, ValueError, TypeError):
        sys.stderr.write('EXPERIMENT_INPUT_INVALID\n')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
