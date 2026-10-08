"""Stdlib-only trusted harness mounted read-only as /inputs/_unit_runner.py.

Import is inert. Only the explicit CLI runs discovery inside the Container.
The Host must not use this module to execute Agent-generated Source locally.
"""

import argparse
from contextlib import contextmanager
import io
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import unittest  # Import stdlib before inserting any untrusted Source path.


MAX_CASES = 1000
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_ID_BYTES = 512
MAX_DETAILS_BYTES = 4096
_SAFE_PATTERN = re.compile(r"[A-Za-z0-9_*?\[\].-]{1,128}\.py\Z")
_SECRET_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".codex", ".agents", ".gnupg", ".kube",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "docker.sock", ".workspace.json",
})


def _safe_directory(value):
    # Mirror the Host's tests-relative lexical policy without importing the
    # application or inspecting any Host path in this standalone harness.
    if (type(value) is not str or not value or value.startswith("/") or "\\" in value
            or PureWindowsPath(value).drive or len(value.encode("utf-8")) > 4096
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        return False
    parts = value.split("/")
    return parts[0] == "tests" and len(parts) <= 64 and all(
        part not in {"", ".", ".."} and ":" not in part
        and part.casefold() not in _SECRET_NAMES
        and not part.casefold().startswith((".env", ".mcp-write-"))
        and not part.casefold().endswith((".pem", ".key", ".p12", ".pfx"))
        for part in parts
    )


class _HarnessError(Exception):
    """Stable internal marker; never returned with exception text or paths."""


class _DiscardStream(io.TextIOBase):
    def __init__(self, budget):
        self._budget = budget
        self.buffer = _DiscardBytes(budget)

    @property
    def encoding(self):
        return "utf-8"

    def writable(self):
        return True

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError("text required")
        self.buffer.write(value.encode("utf-8", errors="replace"))
        return len(value)


class _DiscardBytes:
    def __init__(self, budget):
        self._budget = budget

    def write(self, value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            raise TypeError("bytes required")
        self._budget[0] += len(value)
        if self._budget[0] > MAX_OUTPUT_BYTES:
            self._budget[1] = True
            raise _HarnessError()
        return len(value)

    def flush(self):
        return None


def _bounded(value, limit):
    return value.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


class _Result(unittest.TestResult):
    """Aggregate failed subtests into their parent, count class callbacks too."""

    def __init__(self, expected):
        super().__init__()
        self.expected = expected
        self.seen = set()
        self.blocked = set()
        self.rows = {}
        self.active = {}
        self.started = 0
        self.finished = 0
        self.invalid = False

    def _id(self, test):
        test_id = test.id()
        if (
            type(test_id) is not str or not test_id
            or len(test_id.encode("utf-8")) > MAX_ID_BYTES
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in test_id)
        ):
            raise _HarnessError()
        return test_id

    def startTest(self, test):
        super().startTest(test)
        test_id = self._id(test)
        if test_id in self.rows or id(test) in self.active or len(self.rows) >= MAX_CASES:
            raise _HarnessError()
        if test_id not in self.expected:
            raise _HarnessError()
        self.rows[test_id] = {"testId": test_id, "outcome": None}
        self.active[id(test)] = test_id
        self.seen.add(test_id)
        self.started += 1

    def stopTest(self, test):
        test_id = self.active.pop(id(test), None)
        if test_id is None or self.rows[test_id]["outcome"] is None:
            self.invalid = True
        self.finished += 1
        super().stopTest(test)

    def _outcome(self, test, outcome, details=None):
        test_id = self.active.get(id(test))
        if test_id is None:
            # setUpClass/tearDownClass/module fixture errors and class skips.
            test_id = self._id(test)
            if test_id in self.rows or len(self.rows) >= MAX_CASES:
                raise _HarnessError()
            self.rows[test_id] = {"testId": test_id, "outcome": None}
            if test_id.startswith("setUpClass (") and test_id.endswith(")"):
                class_name = test_id[len("setUpClass ("):-1]
                self.blocked.update(name for name, identity in self.expected.items() if identity[0] == class_name)
            elif test_id.startswith("setUpModule (") and test_id.endswith(")"):
                module_name = test_id[len("setUpModule ("):-1]
                self.blocked.update(name for name, identity in self.expected.items() if identity[1] == module_name)
        row = self.rows[test_id]
        rank = {None: -1, "PASS": 0, "SKIP": 1, "FAIL": 2}
        if rank[outcome] >= rank[row["outcome"]]:
            row["outcome"] = outcome
            if details is not None:
                row["details"] = _bounded(details, MAX_DETAILS_BYTES)

    @staticmethod
    def _error(err):
        return _bounded(err[0].__name__ + ": " + str(err[1]), MAX_DETAILS_BYTES)

    def addSuccess(self, test):
        self._outcome(test, "PASS")

    def addFailure(self, test, err):
        self._outcome(test, "FAIL", self._error(err))

    def addError(self, test, err):
        self._outcome(test, "FAIL", self._error(err))

    def addSkip(self, test, reason):
        parent = getattr(test, "test_case", None)
        self._outcome(parent if parent is not None else test, "SKIP", str(reason))

    def addExpectedFailure(self, test, err):
        self._outcome(test, "SKIP", "expected failure")

    def addUnexpectedSuccess(self, test):
        self._outcome(test, "FAIL", "unexpected success")

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self._outcome(test, "FAIL", self._error(err))

    def report(self):
        if (self.invalid or self.active or self.started != self.finished or not self.rows
                or self.seen | self.blocked != set(self.expected)):
            raise _HarnessError()
        rows = list(self.rows.values())
        counts = {name: sum(row["outcome"] == outcome for row in rows)
                  for name, outcome in (("passed", "PASS"), ("failed", "FAIL"), ("skipped", "SKIP"))}
        if sum(counts.values()) != len(rows):
            raise _HarnessError()
        return {"format": "unittest-v1", "total": len(rows), **counts, "tests": rows}


def _expected_cases(suite):
    expected = {}

    def visit(node, depth):
        if depth > 64:
            raise _HarnessError()
        if isinstance(node, unittest.TestSuite):
            for child in node:
                visit(child, depth + 1)
        elif isinstance(node, unittest.TestCase):
            test_id = node.id()
            if (type(test_id) is not str or not test_id or test_id in expected
                    or len(test_id.encode("utf-8")) > MAX_ID_BYTES or len(expected) >= MAX_CASES):
                raise _HarnessError()
            case_type = type(node)
            expected[test_id] = (case_type.__module__ + "." + case_type.__qualname__, case_type.__module__)
        else:
            raise _HarnessError()

    visit(suite, 0)
    if not expected:
        raise _HarnessError()
    return expected


def run_tests(test_root, source_root, pattern="test_*.py"):
    """Run explicit trusted-fixture tests or, in the Container, mounted tests.

    Return (report, exit_code), where report is None and exit is 2 for empty,
    discovery, incomplete, oversized, or framework failures. Test prints are
    discarded, never interpreted as report bytes. This is not an attestation
    boundary against Python tests intentionally modifying the runner itself.
    """
    budget = [0, False]
    original_stdout, original_stderr = sys.stdout, sys.stderr
    original_path = sys.path[:]
    try:
        sys.stdout = _DiscardStream(budget)
        sys.stderr = _DiscardStream(budget)
        root = Path(test_root)
        source = Path(source_root)
        if (type(pattern) is not str or _SAFE_PATTERN.fullmatch(pattern) is None
                or not root.is_dir() or not source.is_dir()):
            raise _HarnessError()
        sys.path.insert(0, str(source))
        loader = unittest.TestLoader()
        suite = loader.discover(str(root), pattern=pattern, top_level_dir=str(root))
        if loader.errors or not 1 <= suite.countTestCases() <= MAX_CASES or budget[1]:
            raise _HarnessError()
        result = _Result(_expected_cases(suite))
        suite.run(result)
        if loader.errors or result.shouldStop or budget[1] or result.testsRun != result.started:
            raise _HarnessError()
        report = result.report()
        encoded = json.dumps(report, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise _HarnessError()
        return report, 1 if report["failed"] else 0
    except BaseException:
        return None, 2
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        sys.path[:] = original_path


@contextmanager
def _discard_file_descriptors():
    """Suppress direct os.write(1/2) as well as the Python streams in the CLI."""
    saved_stdout = saved_stderr = devnull = None
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        yield
    finally:
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if devnull is not None:
            os.close(devnull)


def main(argv=None):
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--kind", choices=("SNAPSHOT", "QA_TESTS", "PROTECTED"), required=True)
    parser.add_argument("--directory", required=True)
    parser.add_argument("--pattern", required=True)
    try:
        options = parser.parse_args(argv)
        if not _safe_directory(options.directory) or _SAFE_PATTERN.fullmatch(options.pattern) is None:
            raise _HarnessError()
        test_root = Path("/snapshot" if options.kind == "SNAPSHOT" else "/inputs") / options.directory
        with _discard_file_descriptors():
            report, exit_code = run_tests(test_root, Path("/snapshot"), options.pattern)
    except BaseException:
        report, exit_code = None, 2
    payload = report if report is not None else {"error": "TEST_RUNNER_ERROR"}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
