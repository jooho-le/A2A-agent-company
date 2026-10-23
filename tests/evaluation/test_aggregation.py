"""aggregation.py의 aggregate() 함수를 시험하는 파일이다.

핵심 질문 두 가지를 확인한다.
  1) 결과가 빠졌거나 증거가 없을 때 "통과(PASS)"로 잘못 나오지 않는가?
  2) 이미 확인된 실패(FAIL)가 다른 문제에 가려져 사라지지 않는가?

[테스트 파일을 처음 읽는 분을 위한 안내]
 - `test_`로 시작하는 함수 하나가 "시험 하나"이다. pytest가 이런 함수를 찾아 자동으로 실행한다.
 - assert 뒤의 조건이 거짓이면 그 시험은 실패로 표시된다.
 - 시험할 때는 진짜 검사 결과 대신 "가짜 데이터"를 만들어 넣는다.
"""
import json  # 결과를 JSON 글자로 바꿀 수 있는지 확인할 때 쓴다.
from uuid import UUID  # 가짜 고유 번호(UUID)를 만들 때 쓴다.
import pytest  # 테스트 도구. 같은 시험을 여러 입력으로 반복하는 @pytest.mark.parametrize 등을 제공한다.
from evaluation.aggregation import CheckPlan, CheckResult, Status, aggregate


def uid(number):
    """시험용 가짜 UUIDv4를 만든다. 같은 숫자를 주면 항상 같은 값이 나와서 결과를 예측하기 쉽다."""
    return str(UUID(int=number, version=4))


def result(number, status):
    """검사 번호 number, 상태 status인 결과를 만든다. 증거 번호는 (number + 100)번 UUID를 붙인다."""
    return CheckResult(uid(number), status, (uid(100 + number),))


def test_full_pass_and_json_serialization():
    """필수 검사가 모두 PASS면 전체도 PASS(통과율 100%)이다. JSON으로 바꿀 수 있고 최종 판정(finalVerdict)은 없다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert "finalVerdict" not in json.loads(json.dumps(report))


def test_missing_check_is_not_hidden_by_other_passes():
    """검사 2개 중 1개만 결과가 오면, 나머지 1개는 NOT_RUN(결과 없음)으로 남고 전체는 UNVERIFIED(판정 불가)이다.
    다른 검사가 통과했다고 해서 못 본 검사가 가려지면 안 된다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] == 0.5
    assert report["unverifiedTestIds"] == [uid(2)]
    assert report["results"][1]["errorCode"] == "RESULT_MISSING"


def test_zero_collected_results_never_pass():
    """결과를 하나도 못 받았을 때 "실패한 게 없으니 통과"로 처리하면 안 된다. 안 본 것은 안 본 것이다."""
    report = aggregate([CheckPlan(uid(1))], [])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["summary"]["required"]["notRun"] == 1


def test_failure_survives_mixed_unverified_result():
    """FAIL과 ERROR가 함께 있으면 전체는 UNVERIFIED이지만, 이미 확인된 FAIL 검사 번호는 따로 남는다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))],
                       [result(1, Status.FAIL), result(2, Status.ERROR)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["confirmedFailureIds"] == [uid(1)]
    assert report["unverifiedTestIds"] == [uid(2)]


def test_verified_product_failure():
    """증거가 있는 필수 검사의 FAIL은 "제품에 결함이 있다"는 FAIL로 집계된다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.FAIL)])
    assert report["validationVerdict"] == "FAIL"


@pytest.mark.parametrize("status", [Status.PASS, Status.FAIL])  # 같은 시험을 PASS일 때, FAIL일 때 두 번 실행한다.
def test_missing_evidence_cannot_support_product_verdict(status):
    """증거 번호가 없는 PASS·FAIL은 믿을 근거가 없으므로 ERROR(EVIDENCE_MISSING)로 바뀐다."""
    report = aggregate([CheckPlan(uid(1))], [CheckResult(uid(1), status)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EVIDENCE_MISSING"
    assert report["confirmedFailureIds"] == []


def test_unapproved_exclusion_is_incomplete():
    """결과를 보낸 쪽이 "적용 안 함(NOT_APPLICABLE)"이라고 주장해도, 계획에 승인이 없으면 ERROR로 처리한다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EXCLUSION_NOT_APPROVED"


def test_approved_exclusion_uses_trusted_plan_and_adjusts_denominator():
    """계획에서 미리 승인한 "적용 제외"는 인정하고, 통과율을 셀 때 분모에서 뺀다. (1개 중 1개 통과 = 100%)"""
    report = aggregate([
        CheckPlan(uid(1)), CheckPlan(uid(2), exclusion_approval_ref="policy-1",
                                    exclusion_reason="No applicable rendering"),
    ], [result(1, Status.PASS), result(2, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert report["results"][1]["exclusionApprovalRef"] == "policy-1"


def test_all_excluded_is_not_success_or_100_percent():
    """모든 검사를 제외하면 실제로 확인한 것이 없다. PASS도 100%도 아니고 UNVERIFIED, 통과율은 None(계산 불가)이다."""
    report = aggregate([CheckPlan(uid(1), exclusion_approval_ref="policy-1",
                                  exclusion_reason="Out of scope")],
                       [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("status", [Status.FAIL, Status.ERROR])
def test_optional_results_preserved_without_changing_required_verdict(status):
    """선택(참고용) 검사의 FAIL·ERROR는 optional 목록에만 남는다.
    필수 검사의 판정(PASS)이나 필수 목록은 그대로 유지되어야 한다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2), required=False)],
                       [result(1, Status.PASS), result(2, status)])
    assert report["validationVerdict"] == "PASS"
    field = "ConfirmedFailureIds" if status == Status.FAIL else "UnverifiedTestIds"
    assert report["optional" + field] == [uid(2)]
    assert report["confirmedFailureIds"] == [] and report["unverifiedTestIds"] == []
    assert sum(report["summary"]["optional"].values()) == 1


def test_optional_only_plan_cannot_prove_required_success():
    """선택 검사만 있는 계획으로는 "필수 요구를 충족했다"고 주장할 수 없다."""
    report = aggregate([CheckPlan(uid(1), required=False)], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("plan,results", [
    ([], []),                                                    # 빈 계획
    ([CheckPlan(uid(1)), CheckPlan(uid(1))], []),                # 계획에 같은 검사가 두 번
    ([CheckPlan(uid(1))], [result(2, Status.PASS)]),             # 계획에 없는 검사의 결과
    ([CheckPlan(uid(1))], [result(1, Status.PASS), result(1, Status.FAIL)]),  # 같은 검사의 결과가 두 개
    ([CheckPlan("QA-SIGNUP-001")], []),                          # UUID 대신 사람이 읽는 이름을 사용
    ([CheckPlan(uid(1), required="false")], []),                 # required가 True/False가 아니라 글자
    ([CheckPlan(uid(1), exclusion_approval_ref="policy-1")], []),  # 승인 근거만 있고 이유가 없음
    ([CheckPlan(uid(1))], [CheckResult(uid(1), "PASS", (uid(100),))]),  # 상태가 Status가 아니라 그냥 글자
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, ("fake-id",))]),  # 증거 번호가 UUID가 아님
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, (uid(100), uid(100)))]),  # 증거 번호가 중복
])
def test_ambiguous_or_invalid_input_rejected(plan, results):
    """애매하거나 잘못된 입력은 그럴듯한 요약을 만들어 내는 대신 ValueError(값 오류)로 거부한다."""
    with pytest.raises(ValueError):  # "이 코드에서 ValueError가 나야 시험 통과"라는 뜻
        aggregate(plan, results)


@pytest.mark.parametrize('field', ['expected_result', 'actual_result', 'reason', 'normalized_location', 'error_code'])
def test_invalid_diagnostic_text_rejected(field):
    """설명 글 필드(기대 동작·실제 결과·이유 등)에 공백뿐인 글자를 넣으면 거부한다. 읽을 수 없는 설명은 근거가 되지 못한다."""
    with pytest.raises(ValueError):
        aggregate([CheckPlan(uid(1))],
                  [CheckResult(uid(1), Status.FAIL, (uid(101),), **{field: ' '})])


def test_explicit_execution_error_reason_preserved():
    """실행 오류의 이유와 오류 코드를 뭉뚱그리지 않고 그대로 전달한다."""
    report = aggregate([CheckPlan(uid(1))], [CheckResult(uid(1), Status.ERROR,
                       reason='테스트 DB에 연결할 수 없음', error_code='DB_UNAVAILABLE')])
    assert report['results'][0]['errorCode'] == 'DB_UNAVAILABLE'
    assert report['results'][0]['reason'] == '테스트 DB에 연결할 수 없음'
