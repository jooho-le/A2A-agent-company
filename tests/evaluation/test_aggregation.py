import json
from uuid import UUID
import pytest
from evaluation.aggregation import CheckPlan, CheckResult, Status, aggregate


def uid(number):
    return str(UUID(int=number, version=4))


def result(number, status):
    return CheckResult(uid(number), status, (uid(100 + number),))


def test_full_pass_and_json_serialization():
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert "finalVerdict" not in json.loads(json.dumps(report))


def test_missing_check_is_not_hidden_by_other_passes():
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] == 0.5
    assert report["unverifiedTestIds"] == [uid(2)]
    assert report["results"][1]["errorCode"] == "RESULT_MISSING"


def test_zero_collected_results_never_pass():
    report = aggregate([CheckPlan(uid(1))], [])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["summary"]["required"]["notRun"] == 1


def test_failure_survives_mixed_unverified_result():
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2))],
                       [result(1, Status.FAIL), result(2, Status.ERROR)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["confirmedFailureIds"] == [uid(1)]
    assert report["unverifiedTestIds"] == [uid(2)]


def test_verified_product_failure():
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.FAIL)])
    assert report["validationVerdict"] == "FAIL"


@pytest.mark.parametrize("status", [Status.PASS, Status.FAIL])
def test_missing_evidence_cannot_support_product_verdict(status):
    report = aggregate([CheckPlan(uid(1))], [CheckResult(uid(1), status)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EVIDENCE_MISSING"
    assert report["confirmedFailureIds"] == []


def test_unapproved_exclusion_is_incomplete():
    report = aggregate([CheckPlan(uid(1))], [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["results"][0]["errorCode"] == "EXCLUSION_NOT_APPROVED"


def test_approved_exclusion_uses_trusted_plan_and_adjusts_denominator():
    report = aggregate([
        CheckPlan(uid(1)), CheckPlan(uid(2), exclusion_approval_ref="policy-1",
                                    exclusion_reason="No applicable rendering"),
    ], [result(1, Status.PASS), result(2, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "PASS"
    assert report["requiredPassRate"] == 1
    assert report["results"][1]["exclusionApprovalRef"] == "policy-1"


def test_all_excluded_is_not_success_or_100_percent():
    report = aggregate([CheckPlan(uid(1), exclusion_approval_ref="policy-1",
                                  exclusion_reason="Out of scope")],
                       [result(1, Status.NOT_APPLICABLE)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("status", [Status.FAIL, Status.ERROR])
def test_optional_results_preserved_without_changing_required_verdict(status):
    report = aggregate([CheckPlan(uid(1)), CheckPlan(uid(2), required=False)],
                       [result(1, Status.PASS), result(2, status)])
    assert report["validationVerdict"] == "PASS"
    field = "confirmedFailureIds" if status == Status.FAIL else "unverifiedTestIds"
    assert report[field] == [uid(2)]
    assert sum(report["summary"]["optional"].values()) == 1


def test_optional_only_plan_cannot_prove_required_success():
    report = aggregate([CheckPlan(uid(1), required=False)], [result(1, Status.PASS)])
    assert report["validationVerdict"] == "UNVERIFIED"
    assert report["requiredPassRate"] is None


@pytest.mark.parametrize("plan,results", [
    ([], []),
    ([CheckPlan(uid(1)), CheckPlan(uid(1))], []),
    ([CheckPlan(uid(1))], [result(2, Status.PASS)]),
    ([CheckPlan(uid(1))], [result(1, Status.PASS), result(1, Status.FAIL)]),
    ([CheckPlan("QA-SIGNUP-001")], []),
    ([CheckPlan(uid(1), required="false")], []),
    ([CheckPlan(uid(1), exclusion_approval_ref="policy-1")], []),
    ([CheckPlan(uid(1))], [CheckResult(uid(1), "PASS", (uid(100),))]),
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, ("fake-id",))]),
    ([CheckPlan(uid(1))], [CheckResult(uid(1), Status.PASS, (uid(100), uid(100)))]),
])
def test_ambiguous_or_invalid_input_rejected(plan, results):
    with pytest.raises(ValueError):
        aggregate(plan, results)
