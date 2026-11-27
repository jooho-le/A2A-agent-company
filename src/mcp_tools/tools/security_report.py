"""Bounded AST scanner receipts, not a security verdict or exploit proof."""

from dataclasses import dataclass, field
import re

from mcp_tools.tools.security_contract import (
    SecurityContractError, _name, _profile_reference, _rule, _source_path, _version, parse_json,
)


MAX_SECURITY_REPORT_BYTES = 1024 * 1024
MAX_SECURITY_FILES = 1000
MAX_SECURITY_FINDINGS = 1000
_TEST_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_RANKS = frozenset({"LOW", "MEDIUM", "HIGH"})


class SecurityReportError(ValueError):
    code = "SCANNER_ERROR"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class SecurityFinding:
    rule_id: str
    test_name: str
    path: str = field(repr=False)
    line: int
    column: int
    severity: str
    confidence: str

    def to_dict(self):
        return {"ruleId": self.rule_id, "testName": self.test_name, "path": self.path,
                "line": self.line, "column": self.column, "severity": self.severity,
                "confidence": self.confidence, "status": "SUSPECTED"}

    def sort_key(self):
        return (self.path, self.line, self.column, self.rule_id, self.test_name, self.severity, self.confidence)


@dataclass(frozen=True)
class SecurityScanReport:
    profile_name: str = field(repr=False)
    scanner_version: str
    rule_ids: tuple[str, ...]
    profile_ref: str = field(repr=False)
    scanned_files: tuple[str, ...] = field(repr=False)
    findings: tuple[SecurityFinding, ...] = field(repr=False)

    def to_dict(self):
        return {"format": "bandit-v1", "profileName": self.profile_name, "scanner": "bandit",
                "scannerVersion": self.scanner_version, "ruleIds": list(self.rule_ids),
                "profileRef": self.profile_ref, "scannedFiles": list(self.scanned_files),
                "findings": [finding.to_dict() for finding in self.findings]}


def parse_security_report(stdout: str, exit_code: int) -> SecurityScanReport:
    """Require a closed, canonical full-inventory receipt with matching exit.

    Detection candidates have only fixed metadata. No snippets, issue text,
    CWE URL, credential, Source stdout, exclusion, final verdict or coverage
    claim is accepted. Unknown/empty/partial engine results are not clean.
    """
    try:
        if type(exit_code) is not int or exit_code not in (0, 1):
            raise SecurityReportError()
        data = parse_json(stdout, max_bytes=MAX_SECURITY_REPORT_BYTES)
        if (type(data) is not dict or set(data) != {
                "format", "profileName", "scanner", "scannerVersion", "ruleIds", "profileRef", "scannedFiles", "findings",
        } or data["format"] != "bandit-v1" or data["scanner"] != "bandit"):
            raise SecurityReportError()
        name = _name(data["profileName"])
        version = _version(data["scannerVersion"])
        reference = _profile_reference(data["profileRef"])
        if type(data["ruleIds"]) is not list or not 1 <= len(data["ruleIds"]) <= 128:
            raise SecurityReportError()
        rules = tuple(_rule(value) for value in data["ruleIds"])
        if rules != tuple(sorted(set(rules))):
            raise SecurityReportError()
        if type(data["scannedFiles"]) is not list or not 1 <= len(data["scannedFiles"]) <= MAX_SECURITY_FILES:
            raise SecurityReportError()
        files = tuple(_source_path(value) for value in data["scannedFiles"])
        if files != tuple(sorted(set(files))) or any(not path.endswith(".py") for path in files):
            raise SecurityReportError()
        if type(data["findings"]) is not list or len(data["findings"]) > MAX_SECURITY_FINDINGS:
            raise SecurityReportError()
        findings = []
        for row in data["findings"]:
            if type(row) is not dict or set(row) != {
                "ruleId", "testName", "path", "line", "column", "severity", "confidence", "status",
            }:
                raise SecurityReportError()
            rule = _rule(row["ruleId"])
            path = _source_path(row["path"])
            name_value = row["testName"]
            if (rule not in rules or path not in files or type(name_value) is not str
                    or _TEST_NAME.fullmatch(name_value) is None or row["status"] != "SUSPECTED"
                    or type(row["line"]) is not int or not 1 <= row["line"] <= MAX_SECURITY_REPORT_BYTES
                    or type(row["column"]) is not int or not 0 <= row["column"] <= MAX_SECURITY_REPORT_BYTES
                    or type(row["severity"]) is not str or row["severity"] not in _RANKS
                    or type(row["confidence"]) is not str or row["confidence"] not in _RANKS):
                raise SecurityReportError()
            findings.append(SecurityFinding(rule, name_value, path, row["line"], row["column"],
                                            row["severity"], row["confidence"]))
        keys = [finding.sort_key() for finding in findings]
        if keys != sorted(set(keys)) or (exit_code == 0) != (len(findings) == 0):
            raise SecurityReportError()
        return SecurityScanReport(name, version, rules, reference, files, tuple(findings))
    except (SecurityReportError, SecurityContractError, TypeError, ValueError, KeyError,
            UnicodeError, OverflowError, RecursionError):
        raise SecurityReportError() from None
