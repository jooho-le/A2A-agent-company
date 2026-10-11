"""Strict, bounded browser receipts; never execute Source or collect raw DOM."""

from dataclasses import dataclass, field
import json
import re


MAX_BROWSER_REPORT_BYTES = 1024 * 1024
MAX_BROWSER_CASES = 100
MAX_BROWSER_STEPS = 1000
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_PLAYWRIGHT_VERSION = re.compile(r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_BROWSER_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){3}\Z")
ACTIONS = frozenset({"goto", "fill", "click", "assert_text", "assert_visible", "assert_url"})
DETAIL_CODES = frozenset({"ASSERTION_FAILED", "ACTION_TIMEOUT", "ACTION_FAILED", "ORIGIN_DENIED"})


class BrowserReportError(ValueError):
    def __init__(self, code="TEST_RUNNER_ERROR"):
        self.code = code if code in {"TEST_RUNNER_ERROR", "BROWSER_START_FAILED"} else "TEST_RUNNER_ERROR"
        super().__init__(self.code)


@dataclass(frozen=True)
class BrowserTestStep:
    index: int
    action: str
    outcome: str
    duration_ms: int

    def to_dict(self):
        return {"index": self.index, "action": self.action, "outcome": self.outcome,
                "durationMs": self.duration_ms}


@dataclass(frozen=True)
class BrowserTestCase:
    test_id: str = field(repr=False)
    outcome: str
    steps: tuple[BrowserTestStep, ...] = field(repr=False)
    details: str | None = field(default=None, repr=False)

    def to_dict(self):
        result = {"testId": self.test_id, "outcome": self.outcome,
                  "steps": [step.to_dict() for step in self.steps]}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class BrowserTestReport:
    total: int
    passed: int
    failed: int
    suite_name: str = field(repr=False)
    playwright_version: str
    browser_version: str
    tests: tuple[BrowserTestCase, ...] = field(repr=False)

    def to_dict(self):
        return {"format": "browser-v1", "suiteName": self.suite_name,
                "playwrightVersion": self.playwright_version, "browserVersion": self.browser_version,
                "total": self.total, "passed": self.passed, "failed": self.failed,
                "tests": [test.to_dict() for test in self.tests]}


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrowserReportError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise BrowserReportError()


def _matching(value, expression):
    if type(value) is not str or expression.fullmatch(value) is None:
        raise BrowserReportError()
    return value


def parse_browser_report(stdout: str, exit_code: int) -> BrowserTestReport:
    """Validate counts, executed step sequence and 0/1 product result exits.

    Only stable error codes are retained, not screenshots, selectors, values,
    console output, exceptions, URLs or DOM content. A receipt is not proof of
    requirement coverage or an attestation against hostile Container code.
    """
    try:
        if (type(stdout) is not str or not stdout or
                len(stdout.encode("utf-8")) > MAX_BROWSER_REPORT_BYTES or type(exit_code) is not int):
            raise BrowserReportError()
        data = json.loads(stdout, object_pairs_hook=_object, parse_constant=_nonfinite)
        if type(data) is not dict:
            raise BrowserReportError()
        if set(data) == {"error"}:
            if data["error"] == "BROWSER_START_FAILED" and exit_code == 3:
                raise BrowserReportError("BROWSER_START_FAILED")
            raise BrowserReportError()
        if exit_code not in (0, 1) or set(data) != {
            "format", "suiteName", "playwrightVersion", "browserVersion", "total", "passed", "failed", "tests",
        } or data["format"] != "browser-v1":
            raise BrowserReportError()
        suite_name = _matching(data["suiteName"], _NAME)
        playwright_version = _matching(data["playwrightVersion"], _PLAYWRIGHT_VERSION)
        if len(playwright_version) > 32:
            raise BrowserReportError()
        browser_version = _matching(data["browserVersion"], _BROWSER_VERSION)
        counts = {key: data[key] for key in ("total", "passed", "failed")}
        if (any(type(value) is not int or not 0 <= value <= MAX_BROWSER_CASES for value in counts.values())
                or type(data["tests"]) is not list or not 1 <= counts["total"] == len(data["tests"]) <= MAX_BROWSER_CASES
                or counts["total"] != counts["passed"] + counts["failed"]):
            raise BrowserReportError()
        seen, tests, total_steps = set(), [], 0
        for row in data["tests"]:
            if type(row) is not dict or not {"testId", "outcome", "steps"} <= set(row) <= {"testId", "outcome", "steps", "details"}:
                raise BrowserReportError()
            test_id = _matching(row["testId"], _ID)
            if (test_id in seen or type(row["outcome"]) is not str or row["outcome"] not in {"PASS", "FAIL"}
                    or type(row["steps"]) is not list or not 1 <= len(row["steps"]) <= 100):
                raise BrowserReportError()
            seen.add(test_id)
            total_steps += len(row["steps"])
            if total_steps > MAX_BROWSER_STEPS:
                raise BrowserReportError()
            steps = []
            for index, step in enumerate(row["steps"], 1):
                if (type(step) is not dict or set(step) != {"index", "action", "outcome", "durationMs"}
                        or type(step["index"]) is not int or step["index"] != index
                        or type(step["action"]) is not str or step["action"] not in ACTIONS
                        or type(step["outcome"]) is not str or step["outcome"] not in {"PASS", "FAIL"}
                        or type(step["durationMs"]) is not int or not 0 <= step["durationMs"] <= 600000):
                    raise BrowserReportError()
                steps.append(BrowserTestStep(index, step["action"], step["outcome"], step["durationMs"]))
            if steps[0].action != "goto":
                raise BrowserReportError()
            details = row.get("details")
            if row["outcome"] == "PASS":
                if ("details" in row or any(step.outcome != "PASS" for step in steps)
                        or not any(step.action.startswith("assert_") for step in steps)):
                    raise BrowserReportError()
            elif (type(details) is not str or details not in DETAIL_CODES or steps[-1].outcome != "FAIL"
                  or any(step.outcome != "PASS" for step in steps[:-1])):
                raise BrowserReportError()
            tests.append(BrowserTestCase(test_id, row["outcome"], tuple(steps), details))
        if (sum(case.outcome == "PASS" for case in tests) != counts["passed"] or
                sum(case.outcome == "FAIL" for case in tests) != counts["failed"] or
                (exit_code == 0) != (counts["failed"] == 0)):
            raise BrowserReportError()
        return BrowserTestReport(suite_name=suite_name, playwright_version=playwright_version,
                                 browser_version=browser_version, tests=tuple(tests), **counts)
    except BrowserReportError:
        raise
    except (ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise BrowserReportError() from None
