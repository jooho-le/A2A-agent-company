import json
from types import SimpleNamespace as N

from agents.runtime.test_fix import TestFixRequired, generated_test_feedback, same_cases
from mcp_tools.tools.unit_report import UnitTestCase
from orchestrator.domain import test_not_run


def receipt(*cases):
    return N(report=N(tests=cases), report_ref="artifact://example/generated-report.json")


def test_generated_repair_keeps_case_id_reason_and_existing_receipt():
    case = UnitTestCase(test_id="test_signup.Tests.test_invalid", outcome="SKIP", details=test_not_run.SETUP_FAILED)
    observed = generated_test_feedback({("run_unit_tests", "qa-unit"): (receipt(case), object())},
                                       [("run_unit_tests", "qa-unit")])
    assert observed == [{"toolName":"run_unit_tests", "selector":"qa-unit", "testId":case.test_id,
        "caseOutcome":"UNVERIFIED", "reasonCode":test_not_run.SETUP_FAILED,
        "reportRef":"artifact://example/generated-report.json"}]
    packet = TestFixRequired([test_not_run.SETUP_FAILED], ["qa-unit"], observed_cases=observed).task_input({}, {"cases":[]})
    assert packet["testRunFeedback"]["observedCases"] == observed
    assert packet["testRunFeedback"]["casePassIsRequirementPass"] is False


def test_protected_measurements_are_not_disclosed_to_test_author():
    protected = UnitTestCase(test_id="private.protected.case", outcome="FAIL", details="private inputs")
    measured = {("run_unit_tests", "protected-qa"): (receipt(protected), object())}
    assert generated_test_feedback(measured, [("run_unit_tests", "qa-unit")]) == []


def test_security_repair_uses_same_fact_filter_and_never_echoes_raw_output():
    case = UnitTestCase(test_id="Tests.test_storage", outcome="PASS", details="DO_NOT_COPY_EXCEPTION_OR_BODY",
        output_checks=(("REJECTION_RESPONSE", "INCOMPLETE"),),
        signup_checks_json=json.dumps({"password":"DO_NOT_COPY_PASSWORD", "canonicalEmailUniqueKey":True,
            "storedHashParameters":{"verified":False,"memoryKiB":19456,"hash":"DO_NOT_COPY_HASH"}}))
    observed = generated_test_feedback({"security-tests": (receipt(case), object())},
                                       [("run_unit_tests", "security-tests")])
    assert "DO_NOT_COPY" not in json.dumps(observed)
    assert observed[0]["caseOutcome"] == "PASS"
    assert observed[0]["outputChecks"] == {"REJECTION_RESPONSE":"INCOMPLETE"}
    assert observed[0]["signupChecks"]["storedHashParameters"] == {"verified":False,"memoryKiB":19456}


def test_feedback_cannot_reset_case_binding_or_mutate_previous_ready():
    cases = [{"testId":"case-1"}]
    ready = {"cases":cases}
    original = {"workspaceId":"unchanged"}
    observed = [{"toolName":"run_unit_tests","selector":"qa-unit","testId":"case-1","caseOutcome":"FAIL"}]
    needed = TestFixRequired([test_not_run.SETUP_FAILED], ["qa-unit"], observed_cases=observed)
    result = needed.task_input(original, ready)
    result["testRunFeedback"]["previousReady"]["cases"].clear()
    result["testRunFeedback"]["observedCases"][0]["testId"] = "changed"
    assert ready == {"cases":[{"testId":"case-1"}]}
    assert needed.observed_cases[0]["testId"] == "case-1"
    assert original == {"workspaceId":"unchanged"}


def test_observed_cases_are_bounded_and_missing_observations_are_not_invented():
    rows = [UnitTestCase(test_id=f"Tests.test_{i}",outcome="PASS") for i in range(50)]
    observed = generated_test_feedback({"security-tests":(receipt(*rows),object())},
                                       [("run_unit_tests","security-tests")])
    assert len(observed) == 32
    assert all("outputChecks" not in row and "signupChecks" not in row for row in observed)


def test_same_cases_still_rejects_changed_expectations():
    old = N(tool_name="run_unit_tests",selector="qa-unit",test_id="t",requirement_id="r",title="title",expected_result="approved")
    new = N(**{**vars(old),"expected_result":"weakened"})
    assert same_cases([old], [old])
    assert not same_cases([old], [new])
