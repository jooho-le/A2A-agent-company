"""Strict, bounded interpretation of the Host-shipped unittest report.

The parser does not discover or execute tests. A valid report is a receipt
from a configured runner, not attestation that hostile tests are honest.
"""

from dataclasses import dataclass, field
import json

from orchestrator.core.security import redact_text


MAX_UNIT_REPORT_BYTES = 1024 * 1024
MAX_UNIT_TEST_CASES = 1000
MAX_UNIT_TEST_ID_BYTES = 512
MAX_UNIT_DETAILS_BYTES = 4096


class UnitReportError(ValueError):
    code = "TEST_RUNNER_ERROR"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class UnitTestCase:
    test_id: str = field(repr=False)
    outcome: str
    details: str | None = field(default=None, repr=False)

    def to_dict(self):
        result = {"testId": self.test_id, "outcome": self.outcome}
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True)
class UnitTestReport:
    total: int
    passed: int
    failed: int
    skipped: int
    tests: tuple[UnitTestCase, ...] = field(repr=False)

    def to_dict(self):
        return {
            "format": "unittest-v1", "total": self.total, "passed": self.passed,
            "failed": self.failed, "skipped": self.skipped,
            "tests": [test.to_dict() for test in self.tests],
        }


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UnitReportError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise UnitReportError()


def _string(value, maximum, *, nonempty=False):
    if type(value) is not str or nonempty and not value:
        raise UnitReportError()
    if len(value.encode("utf-8")) > maximum:
        raise UnitReportError()
    return value


def parse_unit_report(stdout: str, exit_code: int) -> UnitTestReport:
    """Require one exact report, consistent case/count totals and exit 0/1.

    Expected failures are represented as SKIP by the trusted harness. Counts
    are rederived here; an empty suite or discovery error is never PASS.
    """
    try:
        if type(exit_code) is not int or exit_code not in (0, 1):
            raise UnitReportError()
        _string(stdout, MAX_UNIT_REPORT_BYTES, nonempty=True)
        data = json.loads(stdout, object_pairs_hook=_object, parse_constant=_nonfinite)
        if type(data) is not dict or set(data) != {"format", "total", "passed", "failed", "skipped", "tests"}:
            raise UnitReportError()
        if data["format"] != "unittest-v1" or type(data["tests"]) is not list:
            raise UnitReportError()
        counts = {name: data[name] for name in ("total", "passed", "failed", "skipped")}
        if any(type(value) is not int or not 0 <= value <= MAX_UNIT_TEST_CASES for value in counts.values()):
            raise UnitReportError()
        if not 1 <= counts["total"] == len(data["tests"]) <= MAX_UNIT_TEST_CASES:
            raise UnitReportError()
        if counts["total"] != counts["passed"] + counts["failed"] + counts["skipped"]:
            raise UnitReportError()
        seen, cases = set(), []
        outcomes = {"PASS": 0, "FAIL": 0, "SKIP": 0}
        for row in data["tests"]:
            if type(row) is not dict or not {"testId", "outcome"} <= set(row) <= {"testId", "outcome", "details"}:
                raise UnitReportError()
            test_id = _string(row["testId"], MAX_UNIT_TEST_ID_BYTES, nonempty=True)
            if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in test_id) or redact_text(test_id) != test_id:
                raise UnitReportError()
            if test_id in seen or type(row["outcome"]) is not str or row["outcome"] not in outcomes:
                raise UnitReportError()
            seen.add(test_id)
            details = None
            if "details" in row:
                details = redact_text(_string(row["details"], MAX_UNIT_DETAILS_BYTES))
                _string(details, MAX_UNIT_DETAILS_BYTES)
            cases.append(UnitTestCase(test_id, row["outcome"], details))
            outcomes[row["outcome"]] += 1
        if (outcomes["PASS"], outcomes["FAIL"], outcomes["SKIP"]) != (counts["passed"], counts["failed"], counts["skipped"]):
            raise UnitReportError()
        if (exit_code == 0) != (counts["failed"] == 0):
            raise UnitReportError()
        return UnitTestReport(tests=tuple(cases), **counts)
    except (UnitReportError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise UnitReportError() from None
