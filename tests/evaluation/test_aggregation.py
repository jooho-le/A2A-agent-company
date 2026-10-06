"""aggregate() 테스트: 누락·근거 없는 결과로 PASS가 나오지 않고, 확인된 FAIL은 보존되는지."""
import json
from uuid import UUID
import pytest
from evaluation.aggregation import CheckPlan, CheckResult, Status, aggregate


def uid(number):
    """테스트용 가짜 UUIDv4(같은 숫자면 같은 값)."""
    return str(UUID(int=number, version=4))


def result(number, status):
    """증거 ID(100+number)가 붙은 결과."""
    return CheckResult(uid(number), status, (uid(100 + number),))


def test_full_pass_and_json_serialization():
    """필수 검사가 모두 PASS면 PASS·통과율 100%이고, JSON으로 바꿀 수 있으며 finalVerdict는 없다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert "finalVerdict" not in json.loads(json.dumps(report))


def test_missing_check_is_not_hidden_by_other_passes():
    """검사 2개 중 1개만 결과가 오면, 나머지는 NOT_RUN(RESULT_MISSING)으로 남고 전체는 UNVERIFIED다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] == 0.5
    assert report["unverifiedTestIds"] == [uid(2)]
    assert report["results"][1]["errorCode"] == "RESULT_MISSING"


def test_zero_collected_results_never_pass():
    """결과를 하나도 못 받은 경우 "실패가 없으니 통과"로 처리하지 않는다."""
    report = aggregate([CheckPlan(uid(1))], [])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["summary"]["required"]["notRun"] == 1


def test_failure_survives_mixed_unverified_result():
    """FAIL과 ERROR가 함께 있으면 전체는 UNVERIFIED지만, 확인된 FAIL ID는 따로 남는다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))],
                       [result(1, Status.FAIL), result(2, Status.ERROR)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["confirmedFailureIds"] == [uid(1)]
    assert report["unverifiedTestIds"] == [uid(2)]


def test_verified_product_failure():
    """증거가 있는 필수 검사 FAIL은 제품 결함(FAIL)으로 집계된다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.FAIL)])
    assert report["validationVerdict"] == "FAIL"


@pytest.mark.parametrize("status", [Status.PASS, Status.FAIL])
def test_missing_evidence_cannot_support_product_verdict(status):
    """증거 ID 없는 PASS·FAIL은 판정 근거가 없으므로 ERROR(EVIDENCE_MISSING)로 바뀐다."""
    report = aggregate([CheckPlan(uid(1))], [CheckResult(uid(1), status)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EVIDENCE_MISSING"
    assert report["confirmedFailureIds"] == []


def test_unapproved_exclusion_is_incomplete():
    """결과 쪽에서 NOT_APPLICABLE이라고 주장해도, 계획에 승인이 없으면 ERROR로 처리한다."""
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EXCLUSION_NOT_APPROVED"


def test_approved_exclusion_uses_trusted_plan_and_adjusts_denominator():
    """계획에서 승인된 적용 제외는 인정되고, 통과율 분모에서 빠진다(1/1 = 100%)."""
    report = aggregate([
        CheckPlan(uid(1)), CheckPlan(uid(2), exclusion_approval_ref="policy-1",
                                    exclusion_reason="No applicable rendering"),
    ], [result(1, Status.PASS), result(2, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert report["results"][1]["exclusionApprovalRef"] == "policy-1"


def test_all_excluded_is_not_success_or_100_percent():
    """모든 검사가 제외되면 확인한 것이 없으므로 PASS도 100%도 아니다(UNVERIFIED, 통과율 None)."""
    report = aggregate([CheckPlan(uid(1), exclusion_approval_ref="policy-1",
                                  exclusion_reason="Out of scope")],
                       [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("status", [Status.FAIL, Status.ERROR])
def test_optional_results_preserved_without_changing_required_verdict(status):
    """선택 검사의 FAIL·ERROR는 optional 목록에만 남고 필수 판정(PASS)과 필수 목록에 섞이지 않는다."""
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2), required=False)],
                       [result(1, Status.PASS), result(2, status)])
    assert report["validationVerdict"] == "PASS"
    field = "ConfirmedFailureIds" if status == Status.FAIL else "UnverifiedTestIds"
    assert report["optional" + field] == [uid(2)]
    assert report["confirmedFailureIds"] == [] and report["unverifiedTestIds"] == []
    assert sum(report["summary"]["optional"].values()) == 1


def test_optional_only_plan_cannot_prove_required_success():
    """선택 검사만 있는 계획으로는 필수 요구 충족을 주장할 수 없다."""
    report = aggregate([CheckPlan(uid(1), required=False)], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("plan,results", [
    ([], []),                                                    # 빈 계획
    ([CheckPlan(uid(1)), CheckPlan(uid(1))], []),                # 계획에 같은 검사 중복
    ([CheckPlan(uid(1))], [result(2, Status.PASS)]),             # 계획에 없는 검사의 결과
    ([CheckPlan(uid(1))], [result(1, Status.PASS), result(1, Status.FAIL)]),  # 같은 검사 결과 2개
    ([CheckPlan("QA-SIGNUP-001")], []),                          # UUID 대신 표시 이름 사용
    ([CheckPlan(uid(1), required="false")], []),                 # required가 bool이 아님
    ([CheckPlan(uid(1), exclusion_approval_ref="policy-1")], []),  # 승인만 있고 사유 없음
    ([CheckPlan(uid(1))], [CheckResult(uid(1), "PASS", (uid(100),))]),  # 상태가 Status가 아닌 문자열
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, ("fake-id",))]),  # 증거 ID가 UUID 아님
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, (uid(100), uid(100)))]),  # 증거 ID 중복
])
def test_ambiguous_or_invalid_input_rejected(plan, results):
    """애매하거나 잘못된 입력은 그럴듯한 요약을 만드는 대신 ValueError로 거부한다."""
    with pytest.raises(ValueError):
        aggregate(plan, results)


@pytest.mark.parametrize('field', ['expected_result', 'actual_result', 'reason', 'normalized_location', 'error_code'])
def test_invalid_diagnostic_text_rejected(field):
    """진단 설명 필드에 공백뿐인 문자열을 넣으면 거부한다. 읽을 수 없는 설명은 근거가 아니다."""
    with pytest.raises(ValueError):
        aggregate([CheckPlan(uid(1))],
                  [CheckResult(uid(1), Status.FAIL, (uid(101),), **{field: ' '})])


def test_explicit_execution_error_reason_preserved():
    """실행 오류의 사유와 오류 코드를 뭉뚱그리지 않고 그대로 전달한다."""
    report = aggregate([CheckPlan(uid(1))], [CheckResult(uid(1), Status.ERROR,
                       reason='테스트 DB에 연결할 수 없음', error_code='DB_UNAVAILABLE')])
    assert report['results'][0]['errorCode'] == 'DB_UNAVAILABLE'
    assert report['results'][0]['reason'] == '테스트 DB에 연결할 수 없음'
