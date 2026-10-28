# 검사 도구가 낸 결과를 독립 평가의 검사 결과(CheckResult)로 바꾸는 "결과 어댑터" 두 가지다.
#   check_results_from_unit_report : dev unittest 실행 도구(run_unit_tests, dev 26번)의 "unittest-v1" 보고서
#   check_results_from_assessment  : Security 정적 판정(security/assess.py evaluate)의 요구사항별 결과
#
# [unittest 보고서] 보고서 한 줄 = unittest 테스트 하나(testId, outcome, details). 이것을 미리 정한 대응표
# {unittest 테스트 ID: 검사 번호(UUIDv4)}로 검사 결과에 옮긴다.
#
# 바꾸는 규칙
#   PASS → PASS, FAIL → FAIL (둘 다 보고서 증거 번호를 붙인다)
#   SKIP → ERROR(미검증). 보호 테스트가 "UNVERIFIED:코드"로 건너뛰었으면 그 코드를 오류 코드로 쓴다.
#   보고서에 없는 계획 검사 → ERROR(PLANNED_CASE_NOT_REPORTED)
#   실행기 오류(보고서 없음)·형식이 틀린 보고서·대응표에 없는 줄이 섞인 보고서
#     → 모든 검사를 ERROR로 둔다. 일부 줄만 믿고 PASS를 만들지 않는다.
#     (setUpClass 실패처럼 대응표에 없는 줄이 생기면 보고서 전체가 정상 실행이 아니다.
#      dev QA도 이 경우 보고서를 거부한다: qa_services.py QA_REPORT_BINDING_INVALID)
# 이 파일은 테스트를 실행하지 않고, 보고서가 정말 그 실행에서 나왔는지(증거 진위)도 확인하지 않는다.
#
# [처음 읽는 분을 위한 용어]
# - 어댑터(adapter): 한 도구의 결과 모양을 다른 모듈이 받는 모양으로 바꿔 주는 연결 부품.
# - unittest-v1 보고서: dev 실행기가 만드는 JSON. {format, total, passed, failed, skipped, tests:[{testId, outcome, details}]}
# - outcome: 테스트 하나의 결과. PASS(통과) / FAIL(실패) / SKIP(건너뜀) 중 하나.
# - 증거 번호(evidence_id): 보고서 원본을 보관한 곳의 번호표. PASS·FAIL에는 반드시 붙어야 집계에서 인정된다(aggregation.py).
import re  # 글자 모양을 검사하는 정규식 도구
from typing import Mapping, Optional  # 자료형 이름표(실행에는 영향 없음)

from .aggregation import CheckResult, Status, _uuid4  # 검사 결과 상자, 상태 목록, UUID 모양 검사

UNVERIFIED_PREFIX = 'UNVERIFIED:'  # 보호 테스트가 "확인 못 함"으로 건너뛸 때 남기는 머리말
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')  # 오류 코드로 인정하는 모양: 대문자·숫자·밑줄(예: APP_IMPORT_FAILED)
MAX_DETAILS = 4096  # dev 보고서의 details 최대 길이(바이트)와 같은 한도


def _valid_report(report) -> bool:
    # 보고서가 dev unit_report.parse_unit_report와 같은 모양 규칙을 지키는지 확인한다. 지키면 True.
    # 확인하는 것: 필드 이름이 정확한가, 형식 이름이 unittest-v1인가, 건수가 0 이상 정수이고 서로 맞는가,
    #             줄마다 testId·outcome이 있고 testId가 겹치지 않는가, outcome별 개수가 건수와 같은가.
    if not isinstance(report, dict) or set(report) != {'format', 'total', 'passed', 'failed', 'skipped', 'tests'}:
        return False
    if report['format'] != 'unittest-v1' or not isinstance(report['tests'], list):
        return False
    counts = [report[name] for name in ('total', 'passed', 'failed', 'skipped')]
    if any(type(value) is not int or value < 0 for value in counts):
        return False  # bool(True/False)도 int처럼 보이므로 type(...) is int로 정확히 본다.
    # 1 <= 전체 건수 == 줄 수 == 통과+실패+건너뜀. (빈 보고서는 "검사를 안 했다"는 뜻이라 거부한다.)
    if not 1 <= report['total'] == len(report['tests']) == sum(counts[1:]):
        return False
    seen, outcomes = set(), {'PASS': 0, 'FAIL': 0, 'SKIP': 0}  # 이미 본 testId, 결과별 개수
    for row in report['tests']:
        # 줄의 필드는 testId·outcome이 꼭 있어야 하고, 그 밖에는 details만 허용한다.
        if not isinstance(row, dict) or not {'testId', 'outcome'} <= set(row) <= {'testId', 'outcome', 'details'}:
            return False
        test_id, outcome = row['testId'], row['outcome']
        if not isinstance(test_id, str) or not test_id or test_id in seen or outcome not in outcomes:
            return False
        if 'details' in row and not isinstance(row['details'], str):
            return False
        seen.add(test_id)
        outcomes[outcome] += 1
    # 줄에서 센 결과별 개수가 보고서에 적힌 passed·failed·skipped와 같아야 한다.
    return [outcomes['PASS'], outcomes['FAIL'], outcomes['SKIP']] == counts[1:]


def _skip_code(details: Optional[str]) -> str:
    # 보호 테스트가 남긴 "UNVERIFIED:코드"에서 코드를 꺼낸다. 모양이 다르면 일반 SKIP 코드다.
    # 예) "UNVERIFIED:APP_IMPORT_FAILED" → "APP_IMPORT_FAILED",  "not supported" → "RUNNER_REPORTED_SKIP"
    if isinstance(details, str) and details.startswith(UNVERIFIED_PREFIX):
        code = details[len(UNVERIFIED_PREFIX):]  # 머리말 뒤의 글자
        if _CODE.fullmatch(code):
            return code
    return 'RUNNER_REPORTED_SKIP'


def _text(value: Optional[str]) -> Optional[str]:
    # 빈 글자는 CheckResult에 넣을 수 없으므로 None으로, 너무 긴 글은 한도까지만 남긴다.
    if not isinstance(value, str) or not value.strip():
        return None
    # 바이트로 바꿔 한도에서 자른 뒤 다시 글자로 되돌린다. 한글이 중간에 잘리면 그 조각은 버린다(errors='ignore').
    return value.encode('utf-8')[:MAX_DETAILS].decode('utf-8', errors='ignore')


def check_results_from_unit_report(report: Optional[dict], cases: Mapping[str, str], *,
                                   evidence_id: str,
                                   expected: Optional[Mapping[str, str]] = None,
                                   runner_error: str = 'TEST_RUNNER_ERROR') -> tuple[CheckResult, ...]:
    # unittest-v1 보고서를 검사 결과 목록으로 바꾼다. (매개변수 목록의 `*,` 뒤는 이름을 붙여서만 넘길 수 있다.)
    # report      : unittest-v1 보고서. 실행기 오류로 보고서가 없으면 None.
    # cases       : {unittest 테스트 ID: 검사 번호}. 이 실행에서 결과를 받아야 하는 검사 전부다.
    # evidence_id : 보고서를 보관한 증거 번호(UUIDv4). PASS·FAIL은 이 번호를 증거로 단다.
    # expected    : (선택) {검사 번호: 기대 결과 설명}
    # runner_error: 보고서가 None일 때 쓸 오류 코드
    # 1) 입력 확인: 증거 번호·검사 번호가 UUIDv4이고, 검사 번호가 서로 겹치지 않아야 한다.
    _uuid4(evidence_id)
    if not cases:
        raise ValueError('cases must not be empty')
    check_ids = list(cases.values())
    for check_id in check_ids:
        _uuid4(check_id)
    if len(set(check_ids)) != len(check_ids):
        raise ValueError('Each unittest case needs its own check ID')
    expected = expected or {}

    # 검사 결과 상자 하나를 만드는 안쪽 도우미. evidence=True일 때만 보고서 증거 번호를 붙인다.
    def result(check_id, status, *, evidence=False, actual=None, error=None):
        return CheckResult(test_id=check_id, status=status,
                           evidence_ids=(evidence_id,) if evidence else (),
                           expected_result=_text(expected.get(check_id)),
                           actual_result=_text(actual), error_code=error)

    # 2) 보고서 전체를 믿을 수 없는 경우: 모든 검사를 미검증(ERROR)으로 둔다.
    if report is None:
        return tuple(result(check_id, Status.ERROR, error=runner_error) for check_id in check_ids)
    if not _valid_report(report):
        return tuple(result(check_id, Status.ERROR, error='UNIT_REPORT_INVALID') for check_id in check_ids)
    rows = {row['testId']: row for row in report['tests']}  # {testId: 보고서 줄}
    if set(rows) - set(cases):
        # 대응표에 없는 줄(예: setUpClass 실패 줄)이 섞였다 → 정상 실행이 아니므로 통과한 줄도 믿지 않는다.
        return tuple(result(check_id, Status.ERROR, error='UNIT_REPORT_UNBOUND_CASE') for check_id in check_ids)

    # 3) 검사마다 보고서 줄을 찾아 옮긴다.
    results = []
    for test_id, check_id in cases.items():
        row = rows.get(test_id)
        if row is None:
            results.append(result(check_id, Status.ERROR, error='PLANNED_CASE_NOT_REPORTED'))  # 계획했는데 실행 안 됨
        elif row['outcome'] == 'SKIP':
            # 건너뛴 테스트는 PASS가 아니라 미검증이다. 보호 테스트가 남긴 이유 코드를 오류 코드로 쓴다.
            results.append(result(check_id, Status.ERROR, actual=row.get('details'),
                                  error=_skip_code(row.get('details'))))
        else:
            status = Status.PASS if row['outcome'] == 'PASS' else Status.FAIL
            # `A or B`: details가 비어 있으면 outcome 글자(PASS/FAIL)를 실제 결과 설명으로 쓴다.
            results.append(result(check_id, status, evidence=True, actual=row.get('details') or row['outcome']))
    return tuple(results)


def check_results_from_assessment(requirements: Mapping[str, dict], cases: Mapping[str, str], *,
                                  evidence_id: str,
                                  expected: Optional[Mapping[str, str]] = None) -> tuple[CheckResult, ...]:
    # Security 정적 판정 결과를 검사 결과 목록으로 바꾼다.
    # requirements : 정적 판정 증거 JSON의 "requirements" 부분.
    #                {요구사항 표시 이름: {checkId: "SEC-SIGNUP-001", outcome, basis, observations}}
    # cases        : {검사 표시 이름(SEC-SIGNUP-001 등): 검사 번호(UUIDv4)}
    # evidence_id  : 그 증거 JSON을 보관한 증거 번호(UUIDv4)
    # PASS·FAIL은 그대로(증거 번호를 단다), UNVERIFIED는 ERROR(근거 코드 basis를 오류 코드로)로 옮긴다.
    # 판정에 없는 검사는 결과를 만들지 않아 집계에서 RESULT_MISSING(미검증)이 된다.
    # 1) 입력 확인
    _uuid4(evidence_id)
    if not cases or len(set(cases.values())) != len(cases):
        raise ValueError('cases must be nonempty with distinct check IDs')
    for check_id in cases.values():
        _uuid4(check_id)
    if not isinstance(requirements, Mapping):
        raise ValueError('requirements must be a mapping')
    expected = expected or {}
    # 2) 판정 결과에서 우리 검사(SEC-SIGNUP-00x)에 해당하는 항목만 고른다. 같은 검사가 두 번 나오면 무엇을 믿을지 몰라 멈춘다.
    found = {}  # {검사 표시 이름: 판정 항목}
    for entry in requirements.values():
        label = entry.get('checkId') if isinstance(entry, Mapping) else None
        if label in cases:
            if label in found:
                raise ValueError('A static check appears twice in the assessment')
            found[label] = entry
    # 3) 검사마다 결과를 만든다.
    results = []
    for label, check_id in cases.items():
        entry = found.get(label)
        if entry is None:
            continue  # 결과를 만들지 않으면 집계가 RESULT_MISSING(미검증)으로 처리한다.
        outcome, basis = entry.get('outcome'), entry.get('basis')
        code = basis if isinstance(basis, str) and _CODE.fullmatch(basis) else None  # 모양이 맞는 근거 코드만 쓴다
        if outcome in ('PASS', 'FAIL'):
            results.append(CheckResult(
                test_id=check_id, status=Status(outcome), evidence_ids=(evidence_id,),
                expected_result=_text(expected.get(check_id)), actual_result=code or outcome))
        else:
            # UNVERIFIED는 근거 코드를, 알 수 없는 판정 값은 ASSESSMENT_INVALID를 오류 코드로 쓴다.
            error = (code or 'STATIC_UNVERIFIED') if outcome == 'UNVERIFIED' else 'ASSESSMENT_INVALID'
            results.append(CheckResult(
                test_id=check_id, status=Status.ERROR, expected_result=_text(expected.get(check_id)),
                actual_result=code, error_code=error))
    return tuple(results)
