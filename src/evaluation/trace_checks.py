# D4: 개발 실행(Run)이 남긴 기록을 독립적으로 다시 읽어 확인하는 검사 두 가지다.
#
#   check_trace_exposure(documents, criteria)  REQ-006 중 Orchestrator 기록 부분.
#       Trace 사건·Issue·프로젝트 Artifact·도구 시도·telemetry에 비밀번호나 비밀번호 해시가 남았는가?
#   check_traceability(bundle, scenario_contract)  REQ-008.
#       요구사항 → 개발(코드 버전) → 검증 → Issue → 수정 → 재검증 → 판정의 연결을 기록만으로 복원할 수 있는가?
#
# 판정: FAIL = 기록에서 위반을 실제로 찾음, UNVERIFIED = 기록이 모자라 확인을 끝내지 못함, PASS = 둘 다 아님.
# 결과에는 위치(문서 이름·JSON 경로·사건 종류)와 근거 코드만 담고 비밀번호·해시 값은 담지 않는다.
#
# 한계: A2A Message 본문은 조회 API가 없어 보지 못한다. 노출 검사는 알려 준 값(known_values), 비밀번호 해시 형식,
# 비밀번호 이름의 필드 값만 찾으며 base64·암호화 등으로 바뀐 값은 찾지 못한다. 추적 검사의 규칙은 dev 파이프라인
# 테스트가 만든 실제 Run 기록(SUCCESS·FAIL·HUMAN_REVIEW·빌드 실패 경로)의 사건 흐름에 맞췄다.
import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional

from .aggregation import CheckResult, Status, _uuid4
from .run_metrics import PAGE_LIMIT
from .security.static_analysis import NameMatcher

# 실제 비밀번호 해시 값의 모양(Argon2·bcrypt·PBKDF2·scrypt 저장 형식). 보호 테스트(SEC-SIGNUP-005)와 같은 규칙이다.
# 머리말("$argon2id$")만으로는 찾지 않는다. 보고서 설명 글에 형식 이름이 나오는 것은 노출이 아니기 때문이다.
HASH_FORMAT = re.compile(
    r'[$]argon2(id|i|d)[$](v=[0-9]+[$])?m=[0-9]+,t=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{8,}[$][A-Za-z0-9+/.=-]{16,}'
    r'|[$]2[abxy][$][0-9]{2}[$][./A-Za-z0-9]{53}'
    r'|[$]pbkdf2(-[a-z0-9]+)?[$][0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}'
    r'|[$]scrypt[$]ln=[0-9]+,r=[0-9]+,p=[0-9]+[$][A-Za-z0-9+/.=-]{4,}[$][A-Za-z0-9+/.=-]{16,}')
# 가린 값으로 보는 글자: [REDACTED]·REDACTED·<masked>·***** 등
_REDACTED = re.compile(r'\s*(\[|<)?\s*(redacted|masked|hidden|removed)\s*(\]|>)?\s*|\s*\*+\s*', re.IGNORECASE)
MAX_FINDINGS = 64
MAX_DEPTH = 64


@dataclass(frozen=True)
class TraceCheck:
    status: str                                   # PASS / FAIL / UNVERIFIED
    reasons: tuple[str, ...] = ()                 # 판정 근거 코드
    findings: tuple[dict, ...] = field(default=())  # 위치 정보(값 없음)

    def to_dict(self) -> dict:
        return {'status': self.status, 'reasons': list(self.reasons), 'findings': list(self.findings)}

    def to_check_result(self, check_id: str, evidence_id: str, *, expected: Optional[str] = None) -> CheckResult:
        # 독립 평가 보고서에 넣을 검사 결과로 바꾼다. UNVERIFIED는 ERROR(첫 근거 코드)로 옮긴다.
        _uuid4(check_id)
        _uuid4(evidence_id)
        reason = ', '.join(self.reasons) or None
        if self.status in ('PASS', 'FAIL'):
            return CheckResult(check_id, Status(self.status), (evidence_id,), expected_result=expected,
                               actual_result=reason or self.status)
        return CheckResult(check_id, Status.ERROR, expected_result=expected, actual_result=reason,
                           error_code=self.reasons[0] if self.reasons else 'TRACE_UNVERIFIED')


# ---------------------------------------------------------------- 기록 받기

def fetch_trace_documents(get, run_id: str) -> dict:
    # 노출 검사에 쓸 기록을 시스템 API에서 모은다. get은 run_metrics.fetch_run_bundle과 같은 모양의 함수다.
    # events·telemetry는 페이지로 나뉘어 있어 전체 개수(total)에 닿을 때까지 받는다.
    _uuid4(run_id)
    base = f'/api/v1/runs/{run_id}'

    def paged(path):
        items, offset = [], 0
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
    return '/' + '/'.join(str(part).replace('~', '~0').replace('/', '~1') for part in parts)


def check_trace_exposure(documents: Mapping, criteria: Mapping, *, known_values: Iterable[str] = ()) -> TraceCheck:
    # documents   : {문서 이름: JSON 값}. fetch_trace_documents의 결과(또는 그 일부)
    # criteria    : Security 기준 JSON(scn001_security_criteria.json). 비밀번호 이름 규칙을 같이 쓴다.
    # known_values: 이 Run에서 쓴 것으로 알려진 비밀번호 등(있으면). 빈 값은 받지 않는다.
    secrets = tuple(known_values)
    if any(not isinstance(value, str) or not value for value in secrets):
        raise ValueError('Known values must be nonempty strings')
    names = NameMatcher(criteria['sensitiveNamePattern'], criteria.get('nonSecretNameWords', ()))
    findings, reasons = [], set()

    def add(document, path, kind):
        reasons.add(kind)
        if len(findings) < MAX_FINDINGS:
            findings.append({'document': document, 'path': _pointer(path), 'kind': kind})

    def text(document, path, value):
        if HASH_FORMAT.search(value):
            add(document, path, 'PASSWORD_HASH_FORMAT')
        if any(secret in value for secret in secrets):
            add(document, path, 'KNOWN_SECRET_VALUE')

    def walk(document, path, value, depth):
        if depth > MAX_DEPTH:
            add(document, path, 'DOCUMENT_TOO_DEEP')
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if isinstance(key, str):
                    text(document, path + [key], key)
                if (isinstance(key, str) and names.search(key) and isinstance(item, str)
                        and item.strip() and not _REDACTED.fullmatch(item)):
                    add(document, path + [key], 'PASSWORD_FIELD_VALUE')
                walk(document, path + [key], item, depth + 1)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(document, path + [index], item, depth + 1)
        elif isinstance(value, str):
            text(document, path, value)

    missing = []
    for name in ('events', 'issues', 'artifacts', 'toolAttempts', 'telemetry'):
        if documents.get(name) is None:
            missing.append(name)
        else:
            walk(name, [], documents[name], 0)
    incomplete = [name for name, total in (('events', 'eventsTotal'), ('telemetry', 'telemetryTotal'))
                  if documents.get(name) is not None and documents.get(total) != len(documents[name])]
    if reasons - {'DOCUMENT_TOO_DEEP'}:
        return TraceCheck('FAIL', tuple(sorted(reasons)), tuple(findings))
    if missing or incomplete or reasons:
        codes = (['TRACE_DOCUMENT_MISSING'] if missing else []) + (['TRACE_DOCUMENT_INCOMPLETE'] if incomplete else [])
        return TraceCheck('UNVERIFIED', tuple(codes + sorted(reasons)),
                          tuple({'document': name, 'kind': 'MISSING'} for name in missing)
                          + tuple({'document': name, 'kind': 'INCOMPLETE'} for name in incomplete) + tuple(findings))
    return TraceCheck('PASS')


# ---------------------------------------------------------------- REQ-008: 추적 가능성

def check_traceability(bundle: Mapping, scenario_contract: Mapping) -> TraceCheck:
    # bundle: run_metrics.fetch_run_bundle 결과(run·events·eventsTotal·issues 사용)
    # 확인하는 연결(모두 dev Trace 사건과 Issue 기록에서 읽는다):
    #   1) 요구사항: PLANNER_OUTPUT_VALIDATED 사건이 시나리오 요구사항 전부를 담는다.
    #   2) 개발: 코드 버전 1~최종마다 SNAPSHOT_FROZEN 사건이 있고 요구사항 전부와 이어진다.
    #   3) 검증: QA·Security 담당 요구사항마다 담당 역할의 VALIDATION_FINISHED 사건이 한 번 이상 있다.
    #   4) Issue: 기록된 Issue마다 같은 번호의 ISSUE_CREATED 사건이 있고, 요구사항이 시나리오 안에 있다.
    #   5) 수정: 버전 2 이상은 그 버전으로 가는 FIX_REQUESTED(Issue 번호 포함)와 FIX_COMPLETED가 있고,
    #      그 수정의 원인 Issue가 기록돼 있다(Issue.newCodeVersion = 그 버전).
    #   6) 재검증: Issue 요약만 믿지 않고 수정 버전 이후 담당 역할의 검증(빌드 오류면 빌드) 사건도 확인한다.
    #   7) 판정: 최종 판정이 있으면 VERDICT_CREATED 사건이 있다.
    # 최종 판정 없이 멈춘 Run, 사건 일부만 받은 경우는 UNVERIFIED다.
    run, events = bundle['run'], bundle['events']
    run_id = run.get('runId')
    if not run_id or any(item.get('runId') != run_id for item in [*events, *bundle.get('issues', [])]):
        return TraceCheck('UNVERIFIED', ('TRACE_RUN_MISMATCH',))
    if bundle.get('eventsTotal') != len(events):
        return TraceCheck('UNVERIFIED', ('EVENTS_INCOMPLETE',))
    if run.get('verdict') is None:
        return TraceCheck('UNVERIFIED', ('RUN_NOT_FINISHED',))
    requirements = {str(item['requirementId']): item for item in scenario_contract.get('requirements', [])}
    if not requirements:
        raise ValueError('Scenario contract has no requirements')
    final = run.get('codeVersion')
    if type(final) is not int or final < 1:
        return TraceCheck('FAIL', ('CODE_VERSION_MISSING',))
    problems = []

    def note(code, **where):
        problems.append({'kind': code, **where})

    def of_type(kind, **match):
        return [event for event in events if event.get('eventType') == kind
                and all(event.get(key) == value for key, value in match.items())]

    planned = set().union(*[set(event.get('requirementIds') or ()) for event in of_type('PLANNER_OUTPUT_VALIDATED')])
    if not set(requirements) <= planned:
        note('REQUIREMENTS_NOT_PLANNED')
    for version in range(1, final + 1):
        snapshots = of_type('SNAPSHOT_FROZEN', codeVersion=version)
        if not snapshots:
            note('SNAPSHOT_EVENT_MISSING', codeVersion=version)
        elif not set(requirements) <= set().union(*[set(e.get('requirementIds') or ()) for e in snapshots]):
            note('REQUIREMENTS_NOT_DEVELOPED', codeVersion=version)
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
    issues = {str(issue.get('issueId')): issue for issue in bundle.get('issues', [])}
    for issue_id, issue in issues.items():
        if not of_type('ISSUE_CREATED', issueId=issue_id):
            note('ISSUE_EVENT_MISSING', issueId=issue_id)
        if not set(map(str, issue.get('requirementIds') or ())) <= set(requirements) or not issue.get('requirementIds'):
            note('ISSUE_REQUIREMENT_UNKNOWN', issueId=issue_id)
        fixed = issue.get('newCodeVersion')
        if fixed is None:
            continue
        if not of_type('FIX_REQUESTED', issueId=issue_id, codeVersion=fixed):
            note('FIX_REQUEST_UNLINKED', issueId=issue_id)
        reporter = issue.get('reporter')
        later = [event for event in events if isinstance(event.get('codeVersion'), int) and event['codeVersion'] >= fixed
                 and ((event.get('eventType') == 'VALIDATION_FINISHED' and event.get('actor') == reporter)
                      or (reporter == 'DEVELOPER' and event.get('eventType') in ('BUILD_PASSED', 'BUILD_FAILED')))]
        if not later and fixed <= final:
            note('REVALIDATION_UNTRACEABLE', issueId=issue_id)
    for version in range(2, final + 1):
        requested = of_type('FIX_REQUESTED', codeVersion=version)
        if not requested or not of_type('FIX_COMPLETED', codeVersion=version):
            note('FIX_EVENT_MISSING', codeVersion=version)
        if not any(issue.get('newCodeVersion') == version for issue in issues.values()):
            note('FIX_WITHOUT_ISSUE', codeVersion=version)
        if any(str(event.get('issueId')) not in issues for event in requested):
            note('FIX_ISSUE_UNKNOWN', codeVersion=version)
    if not of_type('VERDICT_CREATED'):
        note('VERDICT_EVENT_MISSING')
    if problems:
        return TraceCheck('FAIL', tuple(sorted({item['kind'] for item in problems})), tuple(problems[:MAX_FINDINGS]))
    return TraceCheck('PASS')
