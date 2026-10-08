"""Pure JSON validation: no product/test execution or Docker required."""

from dataclasses import FrozenInstanceError
import json
import unittest

from mcp_tools.tools.unit_report import (
    MAX_UNIT_REPORT_BYTES, UnitReportError, parse_unit_report,
)


def document(*rows, **changes):
    tests = list(rows) or [{"testId": "fixture.Case.test_ok", "outcome": "PASS"}]
    data = {
        "format": "unittest-v1", "total": len(tests),
        "passed": sum(row.get("outcome") == "PASS" for row in tests),
        "failed": sum(row.get("outcome") == "FAIL" for row in tests),
        "skipped": sum(row.get("outcome") == "SKIP" for row in tests), "tests": tests,
    }
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class UnitReportTests(unittest.TestCase):
    def invalid(self, text, code=0):
        with self.assertRaises(UnitReportError) as caught:
            parse_unit_report(text, code)
        self.assertEqual(caught.exception.code, "TEST_RUNNER_ERROR")
        self.assertEqual(str(caught.exception), "TEST_RUNNER_ERROR")
        self.assertEqual(caught.exception.args, ("TEST_RUNNER_ERROR",))
        self.assertIsNone(caught.exception.__cause__)

    def test_pass_round_trip_and_immutable_private_repr(self):
        result = parse_unit_report(document(), 0)
        self.assertEqual((result.total, result.passed, result.failed, result.skipped), (1, 1, 0, 0))
        self.assertEqual(result.tests[0].test_id, "fixture.Case.test_ok")
        self.assertNotIn("fixture", repr(result))
        self.assertNotIn("fixture", repr(result.tests[0]))
        with self.assertRaises(FrozenInstanceError):
            result.total = 2
        with self.assertRaises(FrozenInstanceError):
            result.tests[0].outcome = "FAIL"
        self.assertEqual(parse_unit_report(json.dumps(result.to_dict()), 0), result)

    def test_mixed_product_failure_is_valid_exit_one(self):
        result = parse_unit_report(document(
            {"testId": "a", "outcome": "PASS"},
            {"testId": "b", "outcome": "FAIL", "details": "AssertionError: expected 1"},
            {"testId": "c", "outcome": "SKIP", "details": "expected failure"},
        ), 1)
        self.assertEqual((result.total, result.passed, result.failed, result.skipped), (3, 1, 1, 1))

    def test_skipped_only_suite_is_nonempty_exit_zero(self):
        result = parse_unit_report(document({"testId": "a", "outcome": "SKIP"}), 0)
        self.assertEqual((result.passed, result.failed, result.skipped), (0, 0, 1))

    def test_details_credentials_are_redacted_and_round_trip_idempotent(self):
        result = parse_unit_report(document({
            "testId": "a", "outcome": "FAIL", "details": "password=fixture-secret; Authorization: Bearer abc123",
        }), 1)
        self.assertNotIn("fixture-secret", result.tests[0].details)
        self.assertNotIn("abc123", result.tests[0].details)
        self.assertEqual(parse_unit_report(json.dumps(result.to_dict()), 1), result)

    def test_unicode_and_optional_empty_details_preserved(self):
        result = parse_unit_report(document({"testId": "테스트.Case.test_ok", "outcome": "PASS", "details": ""}), 0)
        self.assertEqual(result.tests[0].details, "")
        self.assertEqual(result.tests[0].test_id, "테스트.Case.test_ok")

    def test_error_marker_missing_empty_malformed_and_extra_prints_rejected(self):
        for text in (None, "", "null", "[]", "1", "true", "{}", '{"error":"TEST_RUNNER_ERROR"}',
                     "test printed before\n" + document(), document() + "\nprinted after", document() + document()):
            with self.subTest(text=text):
                self.invalid(text)

    def test_report_bytes_utf8_limit(self):
        self.invalid(" " * (MAX_UNIT_REPORT_BYTES + 1))
        self.invalid("한" * (MAX_UNIT_REPORT_BYTES // 3 + 1))

    def test_surrogates_anywhere_rejected(self):
        self.invalid(document({"testId": "bad\ud800", "outcome": "PASS"}))
        self.invalid(document({"testId": "a", "outcome": "PASS", "details": "bad\ud800"}))
        self.invalid("\ud800")

    def test_duplicate_keys_at_top_and_case_levels_rejected(self):
        self.invalid(document().replace('"total": 1', '"total": 1, "total": 1'))
        self.invalid(document().replace('"outcome": "PASS"', '"outcome": "PASS", "outcome": "PASS"'))

    def test_nonfinite_numbers_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity", "1e10000"):
            with self.subTest(constant=constant):
                self.invalid(document().replace('"passed": 1', '"passed": ' + constant))

    def test_nested_unbounded_json_fails_safe(self):
        self.invalid("[" * 2000 + "0" + "]" * 2000)

    def test_unknown_top_and_case_fields_rejected(self):
        self.invalid(document(stdout="private"))
        self.invalid(document({"testId": "a", "outcome": "PASS", "command": "shell"}))

    def test_missing_top_fields_rejected(self):
        data = json.loads(document())
        for name in tuple(data):
            copy = dict(data)
            del copy[name]
            self.invalid(json.dumps(copy))

    def test_closed_version(self):
        for version in (None, 1, True, "unittest-v2", "UNiTTEST-v1", {"format": "unittest-v1"}):
            self.invalid(document(format=version))

    def test_count_types_bounds_and_exact_sum(self):
        for name in ("total", "passed", "failed", "skipped"):
            for value in (None, True, False, 1.0, "1", -1, 1001, []):
                with self.subTest(name=name, value=value):
                    self.invalid(document(**{name: value}))
        self.invalid(document(passed=0))
        self.invalid(document(total=2))
        self.invalid(document(failed=1))

    def test_count_list_derivation_not_just_sum(self):
        self.invalid(document(passed=0, skipped=1))
        self.invalid(document(passed=0, failed=1), 1)

    def test_case_array_types_and_empty_suite_rejected(self):
        for cases in (None, {}, "tests", [], [None], [1], ["test"]):
            self.invalid(document(tests=cases, total=0 if cases == [] else 1))

    def test_one_thousand_cases_allowed_but_over_limit_rejected(self):
        rows = [{"testId": f"case.{index}", "outcome": "PASS"} for index in range(1000)]
        self.assertEqual(parse_unit_report(document(*rows), 0).total, 1000)
        self.invalid(document(*rows, {"testId": "extra", "outcome": "PASS"}))

    def test_duplicate_test_ids_rejected(self):
        self.invalid(document({"testId": "a", "outcome": "PASS"}, {"testId": "a", "outcome": "PASS"}))

    def test_id_types_empty_control_credentials_and_byte_bounds(self):
        for value in (None, True, 7, "", "a\n", "a\x00", "a\x7f", "a\x85", "x" * 513,
                      "한" * 171, "password=fixture-secret", "Bearer abc123", "eyJabc.abc.abc"):
            with self.subTest(value=value):
                self.invalid(document({"testId": value, "outcome": "PASS"}))
        self.assertEqual(len(parse_unit_report(document({"testId": "x" * 512, "outcome": "PASS"}), 0).tests), 1)

    def test_outcome_types_closed_enum_and_missing_keys(self):
        for value in (None, True, 1, "pass", "ERROR", [], {}):
            self.invalid(document({"testId": "a", "outcome": value}))
        self.invalid(document({"testId": "a"}, passed=1))
        self.invalid(document({"outcome": "PASS"}))

    def test_details_types_and_bounds(self):
        for value in (None, True, 1, {}, [], "x" * 4097, "한" * 1366):
            self.invalid(document({"testId": "a", "outcome": "PASS", "details": value}))
        self.assertEqual(len(parse_unit_report(document({"testId": "a", "outcome": "PASS", "details": "x" * 4096}), 0).tests[0].details), 4096)

    def test_exit_type_range_and_count_consistency(self):
        for value in (None, True, False, "0", 0.0, -9, 2, 127, 255):
            self.invalid(document(), value)
        self.invalid(document(), 1)
        self.invalid(document({"testId": "a", "outcome": "FAIL"}), 0)

    def test_whitespace_after_report_is_json_whitespace_not_an_extra_record(self):
        self.assertEqual(parse_unit_report(" \n" + document() + "\n\t", 0).total, 1)
