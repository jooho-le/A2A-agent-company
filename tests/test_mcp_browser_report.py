"""Strict browser receipt validation without browser, Docker or product code."""

from dataclasses import FrozenInstanceError
import json
import unittest

from mcp_tools.tools.browser_report import BrowserReportError, MAX_BROWSER_REPORT_BYTES, parse_browser_report


def case(identifier="case.ok", failed=False):
    row = {"testId": identifier, "outcome": "FAIL" if failed else "PASS", "steps": [
        {"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 0},
        {"index": 2, "action": "assert_visible", "outcome": "FAIL" if failed else "PASS", "durationMs": 2},
    ]}
    if failed:
        row["details"] = "ASSERTION_FAILED"
    return row


def document(*cases, **changes):
    rows = list(cases) or [case()]
    data = {"format": "browser-v1", "suiteName": "signup", "playwrightVersion": "1.55.0",
            "browserVersion": "128.0.6613.85", "total": len(rows),
            "passed": sum(row.get("outcome") == "PASS" for row in rows),
            "failed": sum(row.get("outcome") == "FAIL" for row in rows), "tests": rows}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class BrowserReportTests(unittest.TestCase):
    def invalid(self, data, code=0, expected="TEST_RUNNER_ERROR"):
        with self.assertRaises(BrowserReportError) as caught:
            parse_browser_report(data, code)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected)
        self.assertEqual(caught.exception.args, (expected,))

    def test_round_trip_and_frozen_private_repr(self):
        report = parse_browser_report(document(), 0)
        self.assertEqual((report.total, report.passed, report.failed), (1, 1, 0))
        self.assertEqual(report.tests[0].steps[1].index, 2)
        self.assertEqual(parse_browser_report(json.dumps(report.to_dict()), 0), report)
        self.assertNotIn("case.ok", repr(report))
        self.assertNotIn("signup", repr(report))
        with self.assertRaises(FrozenInstanceError):
            report.total = 2
        with self.assertRaises(FrozenInstanceError):
            report.tests[0].steps[0].index = 2

    def test_valid_mixed_product_failure_exit_one(self):
        report = parse_browser_report(document(case("ok"), case("fail", True)), 1)
        self.assertEqual((report.total, report.passed, report.failed), (2, 1, 1))
        self.assertEqual(report.tests[1].details, "ASSERTION_FAILED")

    def test_valid_failure_on_first_action(self):
        row = {"testId": "goto-fail", "outcome": "FAIL", "steps": [
            {"index": 1, "action": "goto", "outcome": "FAIL", "durationMs": 5},
        ], "details": "ORIGIN_DENIED"}
        self.assertEqual(parse_browser_report(document(row), 1).failed, 1)

    def test_browser_start_marker_preserves_stable_code(self):
        self.invalid('{"error":"BROWSER_START_FAILED"}', 3, "BROWSER_START_FAILED")
        self.invalid('{"error":"BROWSER_START_FAILED"}', 0)
        self.invalid('{"error":"TEST_RUNNER_ERROR"}', 2)
        self.invalid('{"error":"private"}', 3)
        self.invalid('{"error":"BROWSER_START_FAILED","details":"private"}', 3)

    def test_closed_exit_types_and_count_consistency(self):
        for code in (None, True, False, 0.0, "0", -1, 2, 3, 137):
            self.invalid(document(), code)
        self.invalid(document(), 1)
        self.invalid(document(case(failed=True)), 0)

    def test_malformed_missing_prints_multiple_documents(self):
        for text in (None, "", "null", "[]", "true", "1", "{}", "print\n" + document(),
                     document() + "print", document() + document()):
            self.invalid(text)

    def test_bytes_surrogate_and_nested_budgets(self):
        self.invalid(" " * (MAX_BROWSER_REPORT_BYTES + 1))
        self.invalid("한" * (MAX_BROWSER_REPORT_BYTES // 3 + 1))
        self.invalid("\ud800")
        self.invalid(document(case("bad\ud800")))
        self.invalid("[" * 2000 + "0" + "]" * 2000)

    def test_duplicate_json_keys_rejected_at_all_levels(self):
        for original in ('"total": 1', '"testId": "case.ok"', '"index": 1'):
            self.invalid(document().replace(original, original + ", " + original))

    def test_nonfinite_constants_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e10000"):
            self.invalid(document().replace('"durationMs": 0', '"durationMs": ' + value))

    def test_missing_extra_top_level_keys_rejected(self):
        data = json.loads(document())
        for key in data:
            copy = dict(data)
            del copy[key]
            self.invalid(json.dumps(copy))
        self.invalid(document(skipped=0))
        self.invalid(document(console="private"))

    def test_format_name_and_versions_closed(self):
        for value in (None, True, [], "browser-v2"):
            self.invalid(document(format=value))
        for value in (None, True, [], "", "Signup", "a b", "a/../../b", "a" * 65):
            self.invalid(document(suiteName=value))
        for value in (None, True, [], "", "2.0.0", "1.01.0", "1.5", "1.5.0-beta", "1." + "9" * 40 + ".0"):
            self.invalid(document(playwrightVersion=value))
        for value in (None, True, [], "", "chromium", "128.0.1", "128.0.1.0-dev", "9" * 10 + ".0.1.0"):
            self.invalid(document(browserVersion=value))

    def test_count_types_and_bounds(self):
        for name in ("total", "passed", "failed"):
            for value in (None, True, False, 1.0, "1", [], -1, 101):
                self.invalid(document(**{name: value}))
        self.invalid(document(total=2))
        self.invalid(document(passed=0, failed=1), 1)
        self.invalid(document(failed=1))

    def test_empty_or_invalid_case_lists_rejected(self):
        for rows in (None, {}, "rows", [], [None], [1], [True]):
            self.invalid(document(tests=rows))

    def test_maximum_cases_and_aggregate_steps(self):
        rows = [case(f"case-{index}") for index in range(100)]
        self.assertEqual(parse_browser_report(document(*rows), 0).total, 100)
        self.invalid(document(*rows, case("overflow")))
        for row in rows:
            row["steps"] += [{"index": index, "action": "assert_visible", "outcome": "PASS", "durationMs": 0}
                             for index in range(3, 11)]
        self.assertEqual(parse_browser_report(document(*rows), 0).total, 100)
        rows[0]["steps"].append({"index": 11, "action": "assert_visible", "outcome": "PASS", "durationMs": 0})
        self.invalid(document(*rows))

    def test_case_missing_extra_fields_and_duplicate_ids(self):
        row = case()
        for key in row:
            copy = dict(row)
            del copy[key]
            self.invalid(document(copy))
        self.invalid(document({**row, "screenshot": "private"}))
        self.invalid(document(case("same"), case("same")))

    def test_identifiers_and_outcomes_closed(self):
        for value in (None, True, [], "", "a" * 129, "한글", "a/b", "a\n", "password:private"):
            self.invalid(document({**case(), "testId": value}))
        for value in (None, True, [], "SKIP", "pass"):
            self.invalid(document({**case(), "outcome": value}))

    def test_step_types_fields_indices_and_duration_bounds(self):
        for steps in (None, {}, "steps", [], [None], [1]):
            self.invalid(document({**case(), "steps": steps}))
        for name in ("index", "action", "outcome", "durationMs"):
            row = case()
            del row["steps"][0][name]
            self.invalid(document(row))
        for name, values in {
            "index": (None, True, 1.0, "1", 0, 2),
            "action": (None, True, [], "eval", "screenshot"),
            "outcome": (None, True, [], "SKIP"),
            "durationMs": (None, True, 0.0, "0", -1, 600001),
        }.items():
            for value in values:
                row = case()
                row["steps"][0][name] = value
                self.invalid(document(row))
        row = case()
        row["steps"][0]["selector"] = "private"
        self.invalid(document(row))

    def test_pass_requires_goto_then_assert_and_no_details(self):
        row = case()
        row["steps"][0]["action"] = "click"
        self.invalid(document(row))
        row = case()
        row["steps"][1]["action"] = "fill"
        self.invalid(document(row))
        self.invalid(document({**case(), "details": "ASSERTION_FAILED"}))
        row = case()
        row["steps"][1]["outcome"] = "FAIL"
        self.invalid(document(row))

    def test_fail_requires_only_final_failed_step_and_safe_details(self):
        row = case(failed=True)
        for details in (None, True, [], "", "TimeoutError: selector password", "private", "ASSERTION_FAILED\n"):
            self.invalid(document({**row, "details": details}), 1)
        missing = dict(row)
        del missing["details"]
        self.invalid(document(missing), 1)
        row["steps"][0]["outcome"] = "FAIL"
        self.invalid(document(row), 1)
        row = case(failed=True)
        row["steps"][1]["outcome"] = "PASS"
        self.invalid(document(row), 1)

    def test_all_safe_failure_codes_accepted_without_raw_information(self):
        for code in ("ASSERTION_FAILED", "ACTION_TIMEOUT", "ACTION_FAILED", "ORIGIN_DENIED"):
            result = parse_browser_report(document({**case(failed=True), "details": code}), 1)
            self.assertEqual(result.tests[0].details, code)


if __name__ == "__main__":
    unittest.main()
