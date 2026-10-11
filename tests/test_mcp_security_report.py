"""Pure scanner receipt validation; no Bandit, Container or Source execution."""

from dataclasses import FrozenInstanceError
import json
import unittest

from mcp_tools.tools.security_report import SecurityReportError, parse_security_report


def finding(**changes):
    data = {"ruleId": "B102", "testName": "exec_used", "path": "app.py", "line": 2, "column": 0,
            "severity": "MEDIUM", "confidence": "HIGH", "status": "SUSPECTED"}
    data.update(changes)
    return data


def document(findings=None, **changes):
    data = {"format": "bandit-v1", "profileName": "default", "scanner": "bandit", "scannerVersion": "1.8.6",
            "ruleIds": ["B102", "B307"], "profileRef": "artifact://policy/security-profile.json",
            "scannedFiles": ["app.py", "tests/test_app.py"], "findings": [] if findings is None else findings}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class SecurityReportTests(unittest.TestCase):
    def invalid(self, text, code=0):
        with self.assertRaises(SecurityReportError) as caught:
            parse_security_report(text, code)
        self.assertEqual(caught.exception.code, "SCANNER_ERROR")
        self.assertEqual(str(caught.exception), "SCANNER_ERROR")
        self.assertEqual(caught.exception.args, ("SCANNER_ERROR",))
        self.assertIsNone(caught.exception.__cause__)

    def test_clean_receipt_round_trip_frozen_private_fields_and_no_verdict(self):
        report = parse_security_report(document(), 0)
        self.assertEqual(report.scanner_version, "1.8.6")
        self.assertEqual(report.scanned_files, ("app.py", "tests/test_app.py"))
        self.assertEqual(report.findings, ())
        self.assertNotIn("default", repr(report))
        self.assertNotIn("app.py", repr(report))
        self.assertEqual(parse_security_report(json.dumps(report.to_dict()), 0), report)
        self.assertNotIn("PASS", json.dumps(report.to_dict()))
        with self.assertRaises(FrozenInstanceError):
            report.profile_name = "changed"

    def test_suspected_findings_exit_one_and_fixed_minimal_metadata(self):
        report = parse_security_report(document([finding()]), 1)
        item = report.findings[0]
        self.assertEqual(item.sort_key(), ("app.py", 2, 0, "B102", "exec_used", "MEDIUM", "HIGH"))
        self.assertEqual(item.to_dict(), finding())
        self.assertNotIn("app.py", repr(item))
        self.assertEqual(parse_security_report(json.dumps(report.to_dict()), 1), report)
        with self.assertRaises(FrozenInstanceError):
            item.line = 3

    def test_malformed_empty_nonobject_multiple_and_error_markers_rejected(self):
        for text in (None, "", "null", "[]", "true", "1", "{}", '{"error":"SCANNER_ERROR"}',
                     "printed\n" + document(), document() + "printed", document() + document()):
            self.invalid(text)

    def test_strict_exit_types_and_findings_consistency(self):
        for code in (None, True, False, 0.0, "0", -1, 2, 137):
            self.invalid(document(), code)
        self.invalid(document(), 1)
        self.invalid(document([finding()]), 0)

    def test_report_utf8_depth_node_and_surrogate_budgets(self):
        self.invalid(" " * (1024 * 1024 + 1))
        self.invalid("한" * (1024 * 1024 // 3 + 1))
        self.invalid("\ud800")
        self.invalid("[" * 66 + "0" + "]" * 66)
        self.invalid(document(scannedFiles=["bad\ud800.py"]))
        self.invalid(document(findings=[{}] * 65537))

    def test_duplicate_json_keys_top_and_finding_rejected(self):
        self.invalid(document().replace('"scanner": "bandit"', '"scanner": "bandit", "scanner": "bandit"'))
        self.invalid(document([finding()]).replace('"line": 2', '"line": 2, "line": 2'), 1)

    def test_nonfinite_numeric_values_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e10000"):
            self.invalid(document([finding()]).replace('"line": 2', '"line": ' + value), 1)

    def test_missing_extra_closed_top_fields(self):
        data = json.loads(document())
        for key in data:
            copy = dict(data)
            del copy[key]
            self.invalid(json.dumps(copy))
        for name in ("total", "PASS", "coverage", "stdout", "errors", "baseline", "excludedFiles"):
            self.invalid(document(**{name: "private"}))

    def test_format_scanner_name_version_and_profile_reference_closed(self):
        for value in (None, True, [], "bandit-v2"):
            self.invalid(document(format=value))
        for value in (None, True, [], "Bandit", "semgrep"):
            self.invalid(document(scanner=value))
        for value in (None, True, [], "", "Default", "a" * 65, "private/name"):
            self.invalid(document(profileName=value))
        for value in (None, True, [], "1.08.6", "2.0.0", "1.8.6-beta", "1.8", "1." + "9" * 40 + ".0"):
            self.invalid(document(scannerVersion=value))
        for value in (None, True, [], "file:///private", "https://user:secret@example.test/policy",
                      "artifact://policy/../private", "artifact://policy/policy?token=secret", "artifact://policy/.env"):
            self.invalid(document(profileRef=value))

    def test_rule_list_types_bounds_sort_order_duplicates_and_umbrella(self):
        for rules in (None, {}, "B102", [], [None], [True], ["b102"], ["B1022"], ["B001"],
                      ["B307", "B102"], ["B102", "B102"], [f"B{index:03d}" for index in range(2, 131)]):
            self.invalid(document(ruleIds=rules))

    def test_file_list_types_empty_bounds_sort_and_duplicates(self):
        for files in (None, {}, "app.py", [], [None], [True], ["b.py", "a.py"], ["a.py", "a.py"],
                      [f"a{index:04d}.py" for index in range(1001)]):
            self.invalid(document(scannedFiles=files))
        files = [f"a{index:04d}.py" for index in range(1000)]
        self.assertEqual(len(parse_security_report(document(scannedFiles=files), 0).scanned_files), 1000)

    def test_file_paths_traversal_secrets_extensions_controls_and_noncanonical(self):
        for path in ("/app.py", "../app.py", "a/../app.py", "a//app.py", "a\\app.py", "a:app.py",
                     ".env/app.py", ".git/app.py", "secrets/app.py", "key.pem/app.py", "a\n.py", "a\x85.py",
                     "app.pyw", "app.PY", "a.txt", "/".join(["a"] * 129) + ".py"):
            self.invalid(document(scannedFiles=[path]))

    def test_unicode_source_paths_supported_without_uri_decoding(self):
        report = parse_security_report(document(scannedFiles=["한글/a.py", "한글/b%20.py"]), 0)
        self.assertEqual(report.scanned_files, ("한글/a.py", "한글/b%20.py"))

    def test_findings_types_count_duplicate_and_canonical_sort(self):
        for values in (None, {}, "findings", [None], [True], [1]):
            # None is converted to [] by helper, hence use override after decode.
            data = json.loads(document())
            data["findings"] = values
            self.invalid(json.dumps(data))
        self.invalid(document([finding(), finding()]), 1)
        self.invalid(document([finding(line=3), finding(line=2)]), 1)
        self.invalid(document([finding(line=index + 1) for index in range(1001)]), 1)
        self.assertEqual(len(parse_security_report(document([finding(line=index + 1) for index in range(1000)]), 1).findings), 1000)

    def test_missing_extra_raw_finding_fields_rejected(self):
        row = finding()
        for key in row:
            copy = dict(row)
            del copy[key]
            self.invalid(document([copy]), 1)
        for name in ("id", "description", "text", "code", "snippet", "url", "CWE", "endLine"):
            self.invalid(document([finding(**{name: "private"})]), 1)

    def test_finding_rule_path_membership_and_test_name_closed(self):
        self.invalid(document([finding(ruleId="B608")]), 1)
        self.invalid(document([finding(path="not-scanned.py")]), 1)
        for name in (None, True, [], "", "a" * 129, "Exec_used", "private source", "password=secret", "a\n"):
            self.invalid(document([finding(testName=name)]), 1)

    def test_finding_location_strict_integer_bounds(self):
        for value in (None, True, False, "1", 1.0, [], -1, 0, 1024 * 1024 + 1):
            self.invalid(document([finding(line=value)]), 1)
        for value in (None, True, False, "1", 1.0, [], -1, 1024 * 1024 + 1):
            self.invalid(document([finding(column=value)]), 1)

    def test_severity_confidence_and_candidate_status_closed(self):
        for name in ("severity", "confidence"):
            for value in (None, True, [], "CRITICAL", "INFO", "high", "HIGH\n"):
                self.invalid(document([finding(**{name: value})]), 1)
        for value in (None, True, [], "PASS", "CONFIRMED", "suspected"):
            self.invalid(document([finding(status=value)]), 1)

    def test_all_supported_severity_confidence_ranks(self):
        for severity in ("LOW", "MEDIUM", "HIGH"):
            for confidence in ("LOW", "MEDIUM", "HIGH"):
                result = parse_security_report(document([finding(severity=severity, confidence=confidence)]), 1)
                self.assertEqual(result.findings[0].severity, severity)
                self.assertEqual(result.findings[0].confidence, confidence)


if __name__ == "__main__":
    unittest.main()
