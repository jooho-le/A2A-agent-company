# 검사 도구가 낸 결과를 독립 평가의 검사 결과(CheckResult)로 바꾸는 결과 어댑터 두 가지다.
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
import re
from typing import Mapping, Optional

from .aggregation import CheckResult, Status, _uuid4

UNVERIFIED_PREFIX = 'UNVERIFIED:'
_CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}\Z')
MAX_DETAILS = 4096  # dev 보고서의 details 최대 길이(바이트)와 같은 한도


def _valid_report(report) -> bool:
    # dev unit_report.parse_unit_report와 같은 모양 규칙을 확인한다(형식·건수·줄 모양·중복 ID).
    if not isinstance(report, dict) or set(report) != {'format', 'total', 'passed', 'failed', 'skipped', 'tests'}:
        return False
    if report['format'] != 'unittest-v1' or not isinstance(report['tests'], list):
        return False
    counts = [report[name] for name in ('total', 'passed', 'failed', 'skipped')]
    if any(type(value) is not int or value < 0 for value in counts):
        return False
    if not 1 <= report['total'] == len(report['tests']) == sum(counts[1:]):
        return False
    seen, outcomes = set(), {'PASS': 0, 'FAIL': 0, 'SKIP': 0}
    for row in report['tests']:
        if not isinstance(row, dict) or not {'testId', 'outcome'} <= set(row) <= {'testId', 'outcome', 'details'}:
            return False
        test_id, outcome = row['testId'], row['outcome']
        if not isinstance(test_id, str) or not test_id or test_id in seen or outcome not in outcomes:
            return False
        if 'details' in row and not isinstance(row['details'], str):
            return False
        seen.add(test_id)
        outcomes[outcome] += 1
    return [outcomes['PASS'], outcomes['FAIL'], outcomes['SKIP']] == counts[1:]


def _skip_code(details: Optional[str]) -> str:
    # 보호 테스트가 남긴 "UNVERIFIED:코드"에서 코드를 꺼낸다. 모양이 다르면 일반 SKIP 코드다.
    if isinstance(details, str) and details.startswith(UNVERIFIED_PREFIX):
        code = details[len(UNVERIFIED_PREFIX):]
        if _CODE.fullmatch(code):
            return code
    return 'RUNNER_REPORTED_SKIP'


def _text(value: Optional[str]) -> Optional[str]:
    # 빈 글자는 CheckResult에 넣을 수 없으므로 None으로, 너무 긴 글은 한도까지만 남긴다.
    if not isinstance(value, str) or not value.strip():
        return None
    return value.encode('utf-8')[:MAX_DETAILS].decode('utf-8', errors='ignore')


def check_results_from_unit_report(report: Optional[dict], cases: Mapping[str, str], *,
                                   evidence_id: str,
                                   expected: Optional[Mapping[str, str]] = None,
                                   runner_error: str = 'TEST_RUNNER_ERROR') -> tuple[CheckResult, ...]:
    # report      : unittest-v1 보고서. 실행기 오류로 보고서가 없으면 None.
    # cases       : {unittest 테스트 ID: 검사 번호}. 이 실행에서 결과를 받아야 하는 검사 전부다.
    # evidence_id : 보고서를 보관한 증거 번호(UUIDv4). PASS·FAIL은 이 번호를 증거로 단다.
    # expected    : (선택) {검사 번호: 기대 결과 설명}
    # runner_error: 보고서가 None일 때 쓸 오류 코드
    _uuid4(evidence_id)
    if not cases:
        raise ValueError('cases must not be empty')
    check_ids = list(cases.values())
    for check_id in check_ids:
        _uuid4(check_id)
    if len(set(check_ids)) != len(check_ids):
        raise ValueError('Each unittest case needs its own check ID')
    expected = expected or {}

    def result(check_id, status, *, evidence=False, actual=None, error=None):
        return CheckResult(test_id=check_id, status=status,
                           evidence_ids=(evidence_id,) if evidence else (),
                           expected_result=_text(expected.get(check_id)),
                           actual_result=_text(actual), error_code=error)

    if report is None:
        return tuple(result(check_id, Status.ERROR, error=runner_error) for check_id in check_ids)
    if not _valid_report(report):
        return tuple(result(check_id, Status.ERROR, error='UNIT_REPORT_INVALID') for check_id in check_ids)
    rows = {row['testId']: row for row in report['tests']}
    if set(rows) - set(cases):
        return tuple(result(check_id, Status.ERROR, error='UNIT_REPORT_UNBOUND_CASE') for check_id in check_ids)

    results = []
    for test_id, check_id in cases.items():
        row = rows.get(test_id)
        if row is None:
            results.append(result(check_id, Status.ERROR, error='PLANNED_CASE_NOT_REPORTED'))
        elif row['outcome'] == 'SKIP':
            results.append(result(check_id, Status.ERROR, actual=row.get('details'),
                                  error=_skip_code(row.get('details'))))
        else:
            status = Status.PASS if row['outcome'] == 'PASS' else Status.FAIL
            results.append(result(check_id, status, evidence=True, actual=row.get('details') or row['outcome']))
    return tuple(results)


def check_results_from_assessment(requirements: Mapping[str, dict], cases: Mapping[str, str], *,
                                  evidence_id: str,
                                  expected: Optional[Mapping[str, str]] = None) -> tuple[CheckResult, ...]:
    # requirements : 정적 판정 증거 JSON의 "requirements" 부분.
    #                {요구사항 표시 이름: {checkId: "SEC-SIGNUP-001", outcome, basis, observations}}
    # cases        : {검사 표시 이름(SEC-SIGNUP-001 등): 검사 번호(UUIDv4)}
    # evidence_id  : 그 증거 JSON을 보관한 증거 번호(UUIDv4)
    # PASS·FAIL은 그대로(증거 번호를 단다), UNVERIFIED는 ERROR(근거 코드 basis를 오류 코드로)로 옮긴다.
    # 판정에 없는 검사는 결과를 만들지 않아 집계에서 RESULT_MISSING(미검증)이 된다.
    _uuid4(evidence_id)
    if not cases or len(set(cases.values())) != len(cases):
        raise ValueError('cases must be nonempty with distinct check IDs')
    for check_id in cases.values():
        _uuid4(check_id)
    if not isinstance(requirements, Mapping):
        raise ValueError('requirements must be a mapping')
    expected = expected or {}
    found = {}
    for entry in requirements.values():
        label = entry.get('checkId') if isinstance(entry, Mapping) else None
        if label in cases:
            if label in found:
                raise ValueError('A static check appears twice in the assessment')
            found[label] = entry
    results = []
    for label, check_id in cases.items():
        entry = found.get(label)
        if entry is None:
            continue
        outcome, basis = entry.get('outcome'), entry.get('basis')
        code = basis if isinstance(basis, str) and _CODE.fullmatch(basis) else None
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
