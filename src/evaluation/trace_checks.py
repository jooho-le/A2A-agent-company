# D4: 개발 실행(Run)이 남긴 기록을 독립적으로 다시 읽어 확인하는 검사 두 가지다.
#
#   check_trace_exposure(documents, criteria)  REQ-006 중 Run 기록 부분(A2A 본문 문서를 주면 그것까지).
#       Trace 사건·Issue·프로젝트 Artifact·도구 시도·telemetry에 비밀번호나 비밀번호 해시가 남았는가?
#   check_traceability(bundle, scenario_contract)  REQ-008.
#       요구사항 → 개발(코드 버전) → 검증 → Issue → 수정 → 재검증 → 판정의 연결을 기록만으로 복원할 수 있는가?
#
# 판정: FAIL = 기록에서 위반을 실제로 찾음, UNVERIFIED = 기록이 모자라 확인을 끝내지 못함, PASS = 둘 다 아님.
# 결과에는 위치(문서 이름·JSON 경로·사건 종류)와 근거 코드만 담고 비밀번호·해시 값은 담지 않는다.
#
# 한계: A2A Message 본문은 Run 조회 API로는 보지 못한다. Host가 dev `agents.platform.a2a_export`로 에이전트 작업 저장소에서
#   꺼낸 문서를 documents['a2aMessages']로 함께 주면 본문(받은 요청·대화 기록·Task·Artifact·상태 사건)까지 찾는다. 노출 검사는 알려 준 값(known_values), 비밀번호 해시 형식,
# 비밀번호 이름의 필드 값만 찾으며 base64·암호화 등으로 바뀐 값은 찾지 못한다. 추적 검사의 규칙은 dev 파이프라인
# 테스트가 만든 실제 Run 기록(SUCCESS·FAIL·HUMAN_REVIEW·빌드 실패 경로)의 사건 흐름에 맞췄다.
#
# [처음 읽는 분을 위한 용어]
# - Trace 사건(event): Orchestrator가 일이 일어날 때마다 남기는 기록 한 줄. 종류(eventType)·시각·역할(actor)·
#   코드 버전(codeVersion)·관련 요구사항(requirementIds)·Issue 번호(issueId) 등이 들어 있다.
# - Issue: QA·Security(또는 빌드)가 찾은 문제 기록. 어느 버전에서 찾았는지, 몇 번 버전으로 고치라고 했는지(newCodeVersion) 등이 있다.
# - JSON 경로(JSON Pointer): 문서 안의 위치를 "/0/arguments/password"처럼 적는 방법. 값 대신 위치만 남길 때 쓴다.
import re  # 글자 모양을 찾는 정규식 도구
from dataclasses import dataclass, field  # 값을 담는 "데이터 상자"를 짧게 만드는 도구
from typing import Iterable, Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from .aggregation import CheckResult, Status, _uuid4  # 검사 결과 상자, 상태 목록, UUID 모양 검사
from .run_metrics import PAGE_LIMIT  # 시스템 API가 한 번에 돌려주는 최대 개수
from .security.static_analysis import NameMatcher  # 비밀번호 이름을 단어 단위로 알아보는 도구(정적 판정과 같은 규칙)

# 실제 비밀번호 해시 값의 모양(Argon2·bcrypt·PBKDF2·scrypt 저장 형식). 보호 테스트(SEC-SIGNUP-005)와 같은 규칙이다.
# 머리말("$argon2id$")만으로는 찾지 않는다. 보고서 설명 글에 형식 이름이 나오는 것은 노출이 아니기 때문이다.
# (줄마다 한 방식: Argon2 → bcrypt → PBKDF2 → scrypt. 설정 숫자, salt, 해시 부분까지 갖춘 모양만 찾는다.)
HASH_FORMAT = re.compile(
    r'[$]argon2(id|i|d)[$](v=[0-9]+[$])?m=[0-9]+,t=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{8,}[$][A-Za-z0-9+/.=-]{16,}'
    r'|[$]2[abxy][$][0-9]{2}[$][./A-Za-z0-9]{53}'
    r'|[$]pbkdf2(-[a-z0-9]+)?[$][0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}'
    r'|[$]scrypt[$]ln=[0-9]+,r=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}')
# 가린 값으로 보는 글자: [REDACTED]·REDACTED·<masked>·***** 등. 이런 값은 비밀번호 칸에 있어도 노출이 아니다.
_REDACTED = re.compile(r'\s*(\[|<)?\s*(redacted|masked|hidden|removed)\s*(\]|>)?\s*|\s*\*+\s*', re.IGNORECASE)
MAX_FINDINGS = 64  # 결과에 남기는 위치 정보의 최대 개수(문서가 아주 커도 결과가 끝없이 커지지 않게)
EXPOSURE_DOCUMENTS = ('events', 'issues', 'artifacts', 'toolAttempts', 'telemetry')  # 노출 검사가 읽는 기록 다섯 가지
# 지금 수집 경로로는 받을 수 없어 노출 검사가 보지 못하는 출력. 그래서 이 검사의 PASS는 REQ-006 전체 충족이 아니다.
#   A2A_MESSAGE_BODY      : 에이전트 사이 A2A Message 본문(Run 조회 API 없음. a2aMessages 문서를 주면 본 것으로 셈)
#   ARTIFACT_FILE_CONTENT : Artifact 조회는 등록된 JSON 기록만 준다(원본 파일·Snapshot 내용은 아님)
#   AGENT_PROCESS_OUTPUT  : 에이전트·Orchestrator 서버 프로세스의 로그·출력 스트림
#   A2A_MESSAGE_BODY_BEFORE_REDACTION: a2aMessages 문서를 주면 대신 들어감. 저장된 기록은 저장할 때 이미 가린 값이라
#                         가리기 전 송수신 본문은 보지 못한다
NOT_COLLECTED_OUTPUTS = ('A2A_MESSAGE_BODY', 'ARTIFACT_FILE_CONTENT', 'AGENT_PROCESS_OUTPUT')
A2A_DOCUMENT = 'a2aMessages'  # (선택) dev a2a_export가 꺼낸 A2A Message 본문 문서. 주면 A2A_MESSAGE_BODY를 본 것으로 센다
A2A_ROLES = ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')  # A2A 본문 문서의 역할(에이전트 작업 저장소 하나씩)
A2A_ROWS = ('contexts', 'tasks', 'history', 'receipts', 'events')  # 역할마다 있어야 하는 목록
# A2A 본문 문서는 에이전트 저장소에 저장된 기록이고, 저장할 때 이미 가린(마스킹) 값이다. 그래서 문서를 보더라도
# "가리기 전 송수신 본문"은 보지 못한 출력으로 남긴다(PASS를 송수신 경계의 증거로 넓히지 않게).
A2A_BEFORE_REDACTION = 'A2A_MESSAGE_BODY_BEFORE_REDACTION'
MAX_DEPTH = 64     # JSON을 파고드는 최대 깊이(지나치게 깊은 문서가 검사기를 멈추지 않게)


@dataclass(frozen=True)  # frozen=True: 만든 뒤에는 값을 바꿀 수 없는 상자
class TraceCheck:
    # 검사 하나의 결과 상자.
    status: str                                   # PASS / FAIL / UNVERIFIED
    reasons: tuple[str, ...] = ()                 # 판정 근거 코드
    findings: tuple[dict, ...] = field(default=())  # 위치 정보(값 없음)
    coverage: Optional[dict] = None               # (노출 검사만) 읽은 기록·못 받은 기록·보지 못하는 출력·알려 준 값 개수

    def to_dict(self) -> dict:
        # JSON으로 저장하기 쉬운 사전 모양으로 바꾼다.
        result = {'status': self.status, 'reasons': list(self.reasons), 'findings': list(self.findings)}
        if self.coverage is not None:
            result['coverage'] = self.coverage
        return result

    def to_check_result(self, check_id: str, evidence_id: str, *, expected: Optional[str] = None) -> CheckResult:
        # 독립 평가 보고서에 넣을 검사 결과로 바꾼다. UNVERIFIED는 ERROR(첫 근거 코드)로 옮긴다.
        _uuid4(check_id)
        _uuid4(evidence_id)
        reason = ', '.join(self.reasons) or None  # 근거 코드들을 "A, B"처럼 이어 붙인다. 없으면 None.
        if self.status in ('PASS', 'FAIL'):
            return CheckResult(check_id, Status(self.status), (evidence_id,), expected_result=expected,
                               actual_result=reason or self.status)
        # 확인하지 못한 결과에는 증거 번호를 달지 않는다(집계에서 미검증으로 센다).
        return CheckResult(check_id, Status.ERROR, expected_result=expected, actual_result=reason,
                           error_code=self.reasons[0] if self.reasons else 'TRACE_UNVERIFIED')


# ---------------------------------------------------------------- 기록 받기

def fetch_trace_documents(get, run_id: str) -> dict:
    # 노출 검사에 쓸 기록을 시스템 API에서 모은다. get은 run_metrics.fetch_run_bundle과 같은 모양의 함수다
    # ("get(경로, 쿼리 사전 또는 None) → 응답 JSON"). events·telemetry는 페이지로 나뉘어 있어 전체 개수(total)에 닿을 때까지 받는다.
    # 돌려주는 사전에 runId를 함께 담아, 나중에 다른 Run의 기록이 섞이지 않았는지 대조할 수 있게 한다.
    _uuid4(run_id)
    base = f'/api/v1/runs/{run_id}'

    def paged(path):
        # 한 페이지씩 받아 이어 붙인다. 빈 페이지가 오면 더 받을 수 없으므로 멈추고, total과 비교하는 일은 검사 쪽이 한다.
        items, offset = [], 0  # offset = 지금까지 받은 개수(다음에 어디서부터 받을지)
        while True:
            page = get(f'{base}/{path}', {'limit': PAGE_LIMIT, 'offset': offset})
            items.extend(page['events'])
            offset += len(page['events'])
            if offset >= page['total'] or not page['events']:
                return items, page['total']

    events, events_total = paged('events')
    telemetry, telemetry_total = paged('telemetry')
    return {
        'runId': run_id,
        'events': events, 'eventsTotal': events_total,
        'telemetry': telemetry, 'telemetryTotal': telemetry_total,
        'issues': get(f'{base}/issues', None)['issues'],
        'artifacts': get(f'{base}/artifacts', None)['artifacts'],
        'toolAttempts': get(f'{base}/tool-attempts', None)['attempts'],
    }


# ---------------------------------------------------------------- REQ-006: 기록 속 노출

def _pointer(parts):
    # 경로 조각 목록을 JSON Pointer 글자로 바꾼다. 예) [0, "arguments", "password"] → "/0/arguments/password"
    # (규칙상 이름 안의 "~"는 "~0"으로, "/"는 "~1"로 바꿔 적는다.)
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def check_trace_exposure(documents: Mapping, criteria: Mapping, *, known_values: Iterable[str] = ()) -> TraceCheck:
    # Run 기록에 비밀번호·비밀번호 해시가 남았는지 찾는다.
    # documents   : {문서 이름: JSON 값}. fetch_trace_documents의 결과(또는 그 일부)
    # criteria    : Security 기준 JSON(scn001_security_criteria.json). 비밀번호 이름 규칙을 같이 쓴다.
    # known_values: 이 Run에서 쓴 것으로 알려진 비밀번호 등(있으면). 빈 값은 받지 않는다.
    # 찾는 것 세 가지: ① 실제 해시 값 모양 ② 알려 준 값 ③ 비밀번호 이름의 필드(예: "password")에 가리지 않은 값
    secrets = tuple(known_values)
    if any(not isinstance(value, str) or not value for value in secrets):
        raise ValueError('Known values must be nonempty strings')  # 빈 글자는 모든 글에 "들어 있다"고 나와서 막는다.
    names = NameMatcher(criteria['sensitiveNamePattern'], criteria.get('nonSecretNameWords', ()))
    findings, reasons = [], set()  # 찾은 위치 목록, 찾은 종류(근거 코드) 모음

    def add(document, path, kind):
        # 찾은 것 하나를 기록한다. 종류는 항상 남기고, 위치는 MAX_FINDINGS개까지만 남긴다.
        reasons.add(kind)
        if len(findings) < MAX_FINDINGS:
            findings.append({'document': document, 'path': _pointer(path), 'kind': kind})

    def text(document, path, value):
        # 글자 하나(값 또는 이름)에 해시 값 모양이나 알려 준 값이 들어 있는지 본다.
        if HASH_FORMAT.search(value):
            add(document, path, 'PASSWORD_HASH_FORMAT')
        if any(secret in value for secret in secrets):
            add(document, path, 'KNOWN_SECRET_VALUE')

    def sensitive(value):
        # 글자에 해시 값 모양이나 알려 준 값이 들어 있는가
        return bool(HASH_FORMAT.search(value)) or any(secret in value for secret in secrets)

    def walk(document, path, value, depth):
        # JSON을 끝까지 파고들며(사전 → 목록 → 글자) 모든 이름과 값을 본다. 너무 깊으면 멈추고 그 사실을 기록한다.
        # 위치(path)에는 이름을 그대로 적되, 비밀값이 든 이름은 SENSITIVE_KEY로 바꿔 적는다(결과에 원문을 다시 남기지 않게).
        if depth > MAX_DEPTH:
            add(document, path, 'DOCUMENT_TOO_DEEP')
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                step = '[SENSITIVE_KEY]' if isinstance(key, str) and sensitive(key) else key
                if isinstance(key, str):
                    text(document, path + [step], key)  # 이름 자체에 값이 들어간 경우도 본다.
                # 비밀번호 이름(단어 단위, "policy"·"length" 같은 설명 단어 제외)에 가리지 않은 글자 값이 있으면 노출이다.
                if (isinstance(key, str) and names.search(key) and isinstance(item, str)
                        and item.strip() and not _REDACTED.fullmatch(item)):
                    add(document, path + [step], 'PASSWORD_FIELD_VALUE')
                walk(document, path + [step], item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):  # enumerate: (순서 번호, 값)을 함께 꺼낸다.
                walk(document, path + [index], item, depth + 1)
        elif isinstance(value, str):
            text(document, path, value)

    # 다섯 가지 기록을 모두 훑는다. 없는 기록은 따로 적어 둔다.
    missing = []
    for name in EXPOSURE_DOCUMENTS:
        if documents.get(name) is None:
            missing.append(name)
        else:
            walk(name, [], documents[name], 0)
    # 페이지로 받는 기록은 받은 개수가 전체 개수와 같아야 "다 봤다"고 할 수 있다.
    incomplete = [name for name, total in (('events', 'eventsTotal'), ('telemetry', 'telemetryTotal'))
                  if documents.get(name) is not None and documents.get(total) != len(documents[name])]
    # (선택) A2A Message 본문. 같은 Run의 문서여야 하고 모양(네 역할 = 받은 저장소 + 빠진 저장소)이 맞아야 한다.
    # 빠진 저장소가 있거나, Run 기록상 일을 한 역할의 저장소에 이 Run의 기록이 없으면(엉뚱한 저장소를 연결한 경우 등)
    # 일부만 본 것이다(emptyRoles).
    a2a, not_collected = documents.get(A2A_DOCUMENT), list(NOT_COLLECTED_OUTPUTS)
    checked = [name for name in EXPOSURE_DOCUMENTS if name not in missing and name not in incomplete]
    empty_roles = []
    if a2a is not None:
        _check_a2a_shape(a2a, documents.get('runId'))
        walk(A2A_DOCUMENT, [], a2a['roles'], 0)
        not_collected.remove('A2A_MESSAGE_BODY')
        not_collected.append(A2A_BEFORE_REDACTION)
        empty_roles = sorted(role for role in _working_roles(documents.get('events'))
                             if role in a2a['roles'] and not (a2a['roles'][role]['contexts'] and a2a['roles'][role]['tasks']))
        (incomplete if a2a['missingRoles'] or empty_roles else checked).append(A2A_DOCUMENT)
    # 검사 범위: 끝까지 읽은 기록, 못 받았거나 일부만 받은 기록, 수집 경로가 없어 보지 못하는 출력, 알려 준 값 개수(값은 남기지 않음)
    coverage = {'checkedDocuments': checked, 'missingDocuments': missing, 'incompleteDocuments': incomplete,
                'notCollectedOutputs': not_collected, 'knownValues': len(secrets)}
    if a2a is not None:
        coverage['a2aEmptyRoles'] = empty_roles
    # 판정 순서: 실제로 찾은 노출이 있으면 기록이 모자라도 FAIL(확인된 위반) → 기록이 모자라면 UNVERIFIED → 아니면 PASS.
    if reasons - {'DOCUMENT_TOO_DEEP'}:
        return TraceCheck('FAIL', tuple(sorted(reasons)), tuple(findings), coverage)
    if missing or incomplete or reasons:
        codes = (['TRACE_DOCUMENT_MISSING'] if missing else []) + (['TRACE_DOCUMENT_INCOMPLETE'] if incomplete else [])
        return TraceCheck('UNVERIFIED', tuple(codes + sorted(reasons)),
                          tuple({'document': name, 'kind': 'MISSING'} for name in missing)
                          + tuple({'document': name, 'kind': 'INCOMPLETE'} for name in incomplete) + tuple(findings),
                          coverage)
    return TraceCheck('PASS', coverage=coverage)


def _check_a2a_shape(a2a, run_id):
    # dev a2a_export 문서 모양 확인. 다른 Run의 문서이거나 모양이 틀리면 ValueError(무엇을 봤는지 알 수 없으므로).
    if not isinstance(a2a, Mapping) or a2a.get('runId') != run_id:
        raise ValueError('A2A message document belongs to another Run or is malformed')
    roles, missing = a2a.get('roles'), a2a.get('missingRoles')
    if (not isinstance(roles, Mapping) or not isinstance(missing, list) or set(roles) & set(missing)
            or sorted([*roles, *missing]) != sorted(A2A_ROLES)
            or any(not isinstance(rows, Mapping) or any(not isinstance(rows.get(name), list) for name in A2A_ROWS)
                   for rows in roles.values())):
        raise ValueError('A2A message document belongs to another Run or is malformed')


def _working_roles(events):
    # Run 기록(사건)으로 보아 이 Run에서 일을 받은 역할. 사건을 못 받았으면 네 역할 모두로 본다.
    if not isinstance(events, list):
        return set(A2A_ROLES)
    kinds = {(event.get('eventType'), event.get('actor')) for event in events if isinstance(event, Mapping)}
    found = set()
    if any(kind == 'PLANNER_OUTPUT_VALIDATED' for kind, _actor in kinds):
        found.add('PLANNER')
    if any(kind in ('SNAPSHOT_FROZEN', 'DEVELOPER_ARTIFACTS_VALIDATED', 'FIX_COMPLETED') for kind, _actor in kinds):
        found.add('DEVELOPER')
    found |= {actor for kind, actor in kinds if kind == 'VALIDATION_FINISHED' and actor in ('QA', 'SECURITY')}
    return found


# ---------------------------------------------------------------- REQ-008: 추적 가능성

def check_traceability(bundle: Mapping, scenario_contract: Mapping) -> TraceCheck:
    # bundle: run_metrics.fetch_run_bundle 결과(run·events·eventsTotal·issues 사용)
    # 확인하는 연결(모두 dev Trace 사건과 Issue 기록에서 읽는다):
    #   1) 요구사항: PLANNER_OUTPUT_VALIDATED 사건이 시나리오 요구사항 전부를 담는다.
    #   2) 개발: 코드 버전 1~최종마다 SNAPSHOT_FROZEN 사건이 있고 요구사항 전부와 이어진다.
    #   3) 검증: QA·Security 담당 요구사항마다 담당 역할의 VALIDATION_FINISHED 사건이 한 번 이상 있다.
    #      SUCCESS로 끝난 Run은 최종 버전에서도 그 검증 사건이 있어야 한다.
    #   4) Issue: 기록된 Issue마다 같은 번호의 ISSUE_CREATED 사건이 있고, 요구사항이 시나리오 안에 있다.
    #   5) 수정: 버전 2 이상은 그 버전으로 가는 FIX_REQUESTED(Issue 번호 포함)와 FIX_COMPLETED가 있고,
    #      그 수정의 원인 Issue가 기록돼 있다(Issue.newCodeVersion = 그 버전).
    #   6) 재검증: Issue 요약만 믿지 않고 수정 버전 이후 담당 역할의 검증(빌드 오류면 빌드) 사건도 확인한다.
    #   7) 판정: 최종 판정이 있으면 VERDICT_CREATED 사건이 있다.
    # 다른 Run의 사건·Issue가 섞였거나, 최종 판정 없이 멈춘 Run, 사건 일부만 받은 경우는 UNVERIFIED다.
    run, events = bundle['run'], bundle['events']
    run_id = run.get('runId')
    # 모든 사건·Issue가 같은 Run의 것인지 먼저 본다. [*a, *b]는 두 목록을 이어 붙인 새 목록이다.
    if not run_id or any(item.get('runId') != run_id for item in [*events, *bundle.get('issues', [])]):
        return TraceCheck('UNVERIFIED', ('TRACE_RUN_MISMATCH',))
    if bundle.get('eventsTotal') != len(events):
        return TraceCheck('UNVERIFIED', ('EVENTS_INCOMPLETE',))
    if run.get('verdict') is None:
        return TraceCheck('UNVERIFIED', ('RUN_NOT_FINISHED',))
    requirements = {str(item['requirementId']): item for item in scenario_contract.get('requirements', [])}
    if not requirements:
        raise ValueError('Scenario contract has no requirements')
    final = run.get('codeVersion')  # 최종 코드 버전(1부터)
    if type(final) is not int or final < 1:
        return TraceCheck('FAIL', ('CODE_VERSION_MISSING',))
    problems = []  # 끊어진 연결 목록

    def note(code, **where):
        # 끊어진 연결 하나를 기록한다. where에는 어느 버전·어느 Issue인지 같은 위치 정보가 들어간다.
        problems.append({'kind': code, **where})

    def of_type(kind, **match):
        # 종류가 kind이고 주어진 값들(예: codeVersion=2)이 모두 같은 사건들만 골라 준다.
        return [event for event in events if event.get('eventType') == kind
                and all(event.get(key) == value for key, value in match.items())]

    # 1) 요구사항: 계획 사건들에 실린 요구사항을 모두 합쳐(set().union) 시나리오 요구사항을 다 덮는지 본다.
    planned = set().union(*[set(event.get('requirementIds') or ()) for event in of_type('PLANNER_OUTPUT_VALIDATED')])
    if not set(requirements) <= planned:
        note('REQUIREMENTS_NOT_PLANNED')
    # 2) 개발: 버전마다 Snapshot 사건이 있고 요구사항 전부와 이어지는가
    for version in range(1, final + 1):
        snapshots = of_type('SNAPSHOT_FROZEN', codeVersion=version)
        if not snapshots:
            note('SNAPSHOT_EVENT_MISSING', codeVersion=version)
        elif not set(requirements) <= set().union(*[set(e.get('requirementIds') or ()) for e in snapshots]):
            note('REQUIREMENTS_NOT_DEVELOPED', codeVersion=version)
    # 3) 검증: 역할별로 맡은 요구사항이 검증 사건에 한 번이라도 나오는가, SUCCESS면 최종 버전에서도 나오는가
    for role in ('QA', 'SECURITY'):
        expected = {key for key, item in requirements.items() if role in item.get('validators', ())}
        validated = set().union(*[set(e.get('requirementIds') or ()) for e in of_type('VALIDATION_FINISHED', actor=role)])
        if expected - validated:
            note('REQUIREMENT_NOT_VALIDATED', role=role)
        if run.get('verdict') == 'SUCCESS':
            final_validated = set().union(*[set(e.get('requirementIds') or ())
                for e in of_type('VALIDATION_FINISHED', actor=role, codeVersion=final)])
            if expected - final_validated:
                note('FINAL_REQUIREMENT_NOT_VALIDATED', role=role, codeVersion=final)
    # 4)~6) Issue마다: 발견 사건, 요구사항, (고쳤다면) 수정 요청과 재검증 사건
    issues = {str(issue.get('issueId')): issue for issue in bundle.get('issues', [])}
    for issue_id, issue in issues.items():
        if not of_type('ISSUE_CREATED', issueId=issue_id):
            note('ISSUE_EVENT_MISSING', issueId=issue_id)
        if not set(map(str, issue.get('requirementIds') or ())) <= set(requirements) or not issue.get('requirementIds'):
            note('ISSUE_REQUIREMENT_UNKNOWN', issueId=issue_id)
        fixed = issue.get('newCodeVersion')  # 이 Issue를 고치라고 요청한 새 코드 버전. 없으면 고치지 않은 Issue다.
        if fixed is None:
            continue
        if not of_type('FIX_REQUESTED', issueId=issue_id, codeVersion=fixed):
            note('FIX_REQUEST_UNLINKED', issueId=issue_id)
        # 재검증: 수정 버전 이후에 보고한 역할의 검증 사건(빌드 오류를 보고한 Developer Issue면 빌드 사건)이 있어야 한다.
        # (첫 수정 버전의 빌드가 실패해 그다음 버전에서 다시 검증되는 경우가 있어서 "그 버전 이후"로 본다.)
        reporter = issue.get('reporter')
        later = [event for event in events if isinstance(event.get('codeVersion'), int) and event['codeVersion'] >= fixed
                 and ((event.get('eventType') == 'VALIDATION_FINISHED' and event.get('actor') == reporter)
                      or (reporter == 'DEVELOPER' and event.get('eventType') in ('BUILD_PASSED', 'BUILD_FAILED')))]
        if not later and fixed <= final:
            note('REVALIDATION_UNTRACEABLE', issueId=issue_id)
    # 5) 수정: 버전 2 이상은 그 버전으로 가는 수정 요청·완료 사건과, 그 수정을 낳은 Issue가 있어야 한다.
    for version in range(2, final + 1):
        requested = of_type('FIX_REQUESTED', codeVersion=version)
        if not requested or not of_type('FIX_COMPLETED', codeVersion=version):
            note('FIX_EVENT_MISSING', codeVersion=version)
        if not any(issue.get('newCodeVersion') == version for issue in issues.values()):
            note('FIX_WITHOUT_ISSUE', codeVersion=version)
        if any(str(event.get('issueId')) not in issues for event in requested):
            note('FIX_ISSUE_UNKNOWN', codeVersion=version)
    # 7) 판정 사건
    if not of_type('VERDICT_CREATED'):
        note('VERDICT_EVENT_MISSING')
    if problems:
        return TraceCheck('FAIL', tuple(sorted({item['kind'] for item in problems})), tuple(problems[:MAX_FINDINGS]))
    return TraceCheck('PASS')
