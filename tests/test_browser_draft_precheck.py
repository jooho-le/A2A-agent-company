"""A required browser suite must not bypass QA's pre-READY corrections."""
import json
from types import SimpleNamespace as N
from uuid import uuid4

from agents.runtime.qa_services import QARuntimeServices


def draft_fixture(root):
    tests = root / "outputs/qa/tests"
    tests.mkdir(parents=True)
    (tests / "test_sample.py").write_text(
        "import unittest\nclass Tests(unittest.TestCase):\n def test_ok(self): self.assertTrue(True)\n")
    configuration = N(binding=N(workspace_id=uuid4(), run_id=uuid4()),
        unit_test_configuration=N(scopes=[N(name="qa-unit", pattern="test_*.py", kind="QA_TESTS")]),
        browser_test_configuration=N(suites=[N(name="qa-browser", kind="QA_TESTS", suite_path="tests/browser/suite.json")]))
    workspace = N(record=N(root_path=str(root)))
    service = N(configuration=configuration, _workspaces=N(bind=lambda *a, **k: workspace),
        _repository=N(get_run=lambda _: None),
        _selectors={"run_unit_tests": ("qa-unit",), "run_browser_tests": ("qa-browser",)})
    data = {"cases": [{"toolName": "run_unit_tests", "selector": "qa-unit", "testId": "test_sample.Tests.test_ok"}]}
    return service, data


def test_live_shape_unit_tests_present_but_required_browser_file_missing(tmp_path):
    service, data = draft_fixture(tmp_path)
    issues = QARuntimeServices.generated_test_issues(service, data)
    assert {"fieldPath": "/cases", "reasonCode": "GENERATED_TEST_FILE_MISSING"} in issues


def test_missing_browser_uses_existing_bounded_write_correction_before_ready(tmp_path):
    from agents.llm.corrections import case_expectation_check, checked_rejection
    service, data = draft_fixture(tmp_path)
    data["kind"] = "READY"
    check = case_expectation_check(lambda _: None, extra_checks=(("GENERATED_TESTS_INVALID",
        lambda draft: QARuntimeServices.generated_test_issues(service, draft)),))
    rejection = checked_rejection(check(data))
    assert rejection["code"] == "GENERATED_TESTS_INVALID"
    assert {"fieldPath": "/cases", "reasonCode": "GENERATED_TEST_FILE_MISSING"} in rejection["issues"]
    write_suite(tmp_path / "outputs/qa")
    data["cases"].append(browser_case())
    assert check(data) is None


def write_suite(root, tests=None):
    path = root / "tests/browser/suite.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": "browser-suite-v1", "tests": tests or [
        {"testId": "application.ready", "steps": [{"action": "goto", "path": "/"},
                                                    {"action": "assert_visible", "selector": "body"}]}]}))
    return path


def inspect(root, rows=()):
    from agents.runtime.generated_tests_precheck import inspect_generated_browser_tests
    return inspect_generated_browser_tests(root, [("qa-browser", "tests/browser/suite.json")], rows, "cases")


def browser_case(test_id="application.ready"):
    return {"toolName": "run_browser_tests", "selector": "qa-browser", "testId": test_id}


def test_valid_browser_suite_and_matching_declaration(tmp_path):
    write_suite(tmp_path)
    assert inspect(tmp_path, [browser_case()]) == []


def test_invalid_json_is_named_before_container_execution(tmp_path):
    write_suite(tmp_path).write_text("{broken")
    assert inspect(tmp_path) == [{"fieldPath": "/cases", "reasonCode": "BROWSER_SUITE_INVALID"}]


def test_browser_test_must_be_declared_even_when_all_unit_cases_exist(tmp_path):
    write_suite(tmp_path)
    assert {"fieldPath": "/cases", "reasonCode": "TEST_IN_FILE_NOT_DECLARED"} in inspect(tmp_path)


def test_declared_browser_id_must_exist_in_actual_suite(tmp_path):
    write_suite(tmp_path)
    assert {"fieldPath": "/cases", "reasonCode": "DECLARED_TEST_NOT_IN_FILE"} in inspect(tmp_path, [browser_case("missing")])


def test_unsupported_browser_action_is_not_accepted(tmp_path):
    write_suite(tmp_path, [{"testId": "application.ready", "steps": [{"action": "eval", "script": "arbitrary"}]}])
    assert inspect(tmp_path)[0]["reasonCode"] == "BROWSER_SUITE_INVALID"


def test_symlink_cannot_read_suite_outside_scratch(tmp_path):
    outside = tmp_path / "outside"
    write_suite(outside)
    scratch = tmp_path / "scratch"
    (scratch / "tests").mkdir(parents=True)
    (scratch / "tests/browser").symlink_to(outside / "tests/browser", target_is_directory=True)
    assert inspect(scratch)[0]["reasonCode"] == "BROWSER_SUITE_PATH_INVALID"
