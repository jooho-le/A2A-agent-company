"""One in-step fix of generated tests that could not run (docs/live-api/05 section 4).

석민 확정 2026-11-18. When the Host runs a READY draft's generated tests and a
generated scope records a technical not-run (``orchestrator.domain.test_not_run``),
the role gets exactly one chance, in the same step, to fix only its test files:

* unit: code version x role; the step is that unit, and the durable tool
  journal decides whether the chance is still open, so a restart cannot reset it;
* the product Snapshot, Requirements and declared cases stay fixed: the second
  READY must declare exactly the same cases (no removal, no weaker expectation);
* the first run's tool records and receipts stay in their stores as evidence;
* the shared Run budget pays for the extra model call, like any other.

Protected scopes never trigger it: they are Host tests, not the model's.
"""
from agents.llm.contracts import json_text, parse_json
from agents.runtime.test_guidance import CONCURRENT_SIGNUP_GUIDANCE, APP_FIXTURE_USAGE_GUIDANCE, PASSWORD_FIXTURE_GUIDANCE
from orchestrator.domain import test_not_run


def generated_test_feedback(measured, allowed_scopes):
    """Bounded facts from existing generated-test receipts, never protected inputs or raw output."""
    import json
    allowed = set(allowed_scopes)
    rows = []
    for key, value in measured.items():
        pair = key if isinstance(key, tuple) else ("run_unit_tests", key)
        if pair not in allowed:
            continue
        receipt, _evidence = value
        for actual in receipt.report.tests:
            if len(rows) >= 32:
                return rows
            row = {"toolName": pair[0], "selector": pair[1], "testId": actual.test_id,
                   "caseOutcome": actual.outcome if actual.outcome in ("PASS", "FAIL") else "UNVERIFIED"}
            reason = test_not_run.runner_reason(actual)
            if reason in test_not_run.TECHNICAL:
                row["reasonCode"] = reason
            reference = getattr(receipt, "report_ref", None)
            if isinstance(reference, str) and reference:
                row["reportRef"] = reference
            marks = getattr(actual, "output_checks", ())
            if marks:
                row["outputChecks"] = dict(marks)
            encoded = getattr(actual, "signup_checks_json", None)
            if encoded:
                facts = json.loads(encoded)
                safe = {}
                if type(facts.get("canonicalEmailUniqueKey")) is bool:
                    safe["canonicalEmailUniqueKey"] = facts["canonicalEmailUniqueKey"]
                parameters = facts.get("storedHashParameters")
                if isinstance(parameters, dict):
                    safe["storedHashParameters"] = {k: v for k, v in parameters.items()
                        if k in ("argon2id", "verified", "memoryKiB", "iterations", "parallelism", "distinctSalts")
                        and type(v) in (bool, int)}
                if safe:
                    row["signupChecks"] = safe
            rows.append(row)
    return rows

INSTRUCTION = (
    "The Host ran your READY tests on the unchanged product Snapshot. Some generated tests could not run or lacked evidence for an assigned requirement "
    "(reasonCodes). NOT_RUN_SCOPE_FAILED: the test module could not be collected or imported; use only modules and "
    "names that exist in the start contract (main.app, database.get_db, models.User). NOT_RUN_SETUP_FAILED: setUp, "
    "asyncSetUp, setUpClass or setUpModule raised before the affected test body ran. For SCN-001 use "
    "from _host_app_test import AppTestCase, subclass it and use self.client/self.db/self.User inside test "
    "methods; remove top-level product imports and custom lifecycle overrides. For other contracts enter the application's "
    "TestClient lifespan before database setup and register cleanup; entering it only in the test body is too late. "
    "This does not prove the product is correct: preserve its startup behavior, and do not create missing product "
    "tables in the test to hide a startup defect. NOT_RUN_ASYNC_NOT_AWAITED: an async def test in a plain TestCase never "
    "ran; use def. NOT_RUN_FIXTURE_MEMBER_MISSING: a missing Host member was used in the failing test frame. "
    "NOT_RUN_FIXTURE_RETURN_MISUSED: the test used the Host return shape or a result key incorrectly. "
    "For distinctSalts, first_params never updates: observe two distinct committed accounts and assert "
    "second_params['distinctSalts'] only on the SECOND return. Do not replace the check with .get(..., True), "
    "a constant, skipTest, or remove it. Numeric hash settings are public minimums, checked with >=. "
    "NOT_RUN_FIXTURE_ARGUMENT_INVALID: the trusted helper rejected its arguments before any product request; "
    "use the exact argument types and synthetic data lifetime in testTargets.appFixture.methods. "
    "These are narrowly verified test-writing errors, not proof that the product passed. "
    "Fix only your test files with write_test_file. Keep every declared case exactly as it was (same "
    "testId, requirementId, title and expectedResult); do not remove or weaken a check. Then return the same READY JSON.")


class TestFixRequired(Exception):
    """Raised by a role service before any report exists; carries closed codes only."""

    __test__ = False  # not a unittest class

    def __init__(self, reasons, selectors, *, observed_cases=()):
        self.reasons = tuple(sorted(set(reasons)))
        self.selectors = tuple(sorted(set(selectors)))
        self.observed_cases = tuple(parse_json(json_text(list(observed_cases)[:32])))
        super().__init__("TEST_FIX_REQUIRED")

    def task_input(self, task_input, previous_ready):
        """The same task input plus the fixed feedback and the model's own previous READY draft."""
        guidance = []
        if set(self.reasons) & test_not_run.FIXTURE_USAGE:
            guidance.extend((APP_FIXTURE_USAGE_GUIDANCE, PASSWORD_FIXTURE_GUIDANCE))
        if any(r.startswith('OUTPUT_NOT_OBSERVED_') for r in self.reasons):
            guidance.append('Exercise the missing output channels with check_signup_output on your own cases.')
        if 'DB_CONSTRAINT_NOT_OBSERVED' in self.reasons:
            guidance.append('Check the deployed DB key and committed spelling with signup_email_state after a fresh '
                            'varied address and its canonical duplicate.')
        if 'HASH_PARAMETERS_NOT_OBSERVED' in self.reasons:
            guidance.append('Measure signup_hash_parameters(canonical_email, password=sample) on two distinct committed accounts within one test to measure their distinct salts; assert the public hash policy.')
        if 'CONCURRENT_SIGNUPS_NOT_OBSERVED' in self.reasons or test_not_run.HELPER_ASSERTION in self.reasons:
            guidance.append(CONCURRENT_SIGNUP_GUIDANCE)
        from agents.runtime.service_scope import is_service_input
        reports = sorted({row['reportRef'] for row in self.observed_cases if isinstance(row.get('reportRef'), str)})
        instruction = INSTRUCTION
        if is_service_input(task_input):
            instruction = (
                "Correct only your generated tests on the same frozen Source and keep every declared case unchanged. "
                "Read serviceDesign and actual Source; use its exact route, JSON/FORM encoding and request fields. "
                "Use ordinary unittest, the advertised testData factory and TestClient lifespan; do not use SCN-001 "
                "AppTestCase, self.User or signup helpers. A not-run scope is not proof of a product FAIL or PASS. "
                "Never install dependencies, create missing product tables or modify Source to hide a product defect. "
                "Report any product/interface contradiction rather than weakening tests. Return the same READY JSON.")
            guidance = []
            instruction += (
                " Only availableReportRefs below may be read. If it is empty, no report was published: "
                "do not invent an artifact URI or call read_test_report; inspect your test files and the frozen Source instead.")
        return {**dict(task_input), "testRunFeedback": {
            "code": "GENERATED_OUTPUT_COVERAGE_INCOMPLETE" if set(self.reasons) - test_not_run.TECHNICAL else "GENERATED_TESTS_NOT_RUN",
            "reasonCodes": list(self.reasons), "scopes": list(self.selectors),
            "observedCases": parse_json(json_text(list(self.observed_cases))),
            "observationScope": "CURRENT_SOURCE_GENERATED_TESTS_ONLY",
            "casePassIsRequirementPass": False,
            **({'availableReportRefs': reports, 'noReportAvailable': not reports} if is_service_input(task_input) else {}),
            "instruction": " ".join([instruction, *guidance]),
            "previousReady": parse_json(json_text(previous_ready))}}


def available_report_tools(tools, task_input):
    from agents.runtime.service_scope import is_service_input
    if not is_service_input(task_input):
        return tools
    refs = task_input.get('testRunFeedback', {}).get('availableReportRefs', [])
    selected = []
    for tool in tools:
        if tool.name != 'read_test_report':
            selected.append(tool)
        elif refs:
            from dataclasses import replace
            from agents.llm.contracts import JsonSchema
            schema = tool.input_schema.to_dict()
            schema['properties']['reportRef'] = {'type': 'string', 'enum': refs}
            selected.append(replace(tool, input_schema=JsonSchema.from_dict(schema)))
    return tuple(selected)


def same_cases(previous, current) -> bool:
    """True when the fixed draft declares exactly the cases of the first one."""
    def key(case):
        return (case.tool_name, case.selector, case.test_id, str(case.requirement_id), case.title, case.expected_result)
    try:
        return sorted(map(key, previous)) == sorted(map(key, current))
    except (AttributeError, TypeError):
        return False


def technical_reasons(not_run_scopes, measured_rows):
    """Closed reasons from scopes that could not run and runner not-run rows of generated scopes."""
    reasons = {test_not_run.SCOPE_FAILED for _scope in not_run_scopes}
    for actual in measured_rows:
        reason = test_not_run.runner_reason(actual)
        if reason in test_not_run.TECHNICAL:
            reasons.add(reason)
    return reasons
