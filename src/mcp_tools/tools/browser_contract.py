"""Standalone declarative browser suite parser shared with the Container runner.

There is no generated Python/JavaScript execution API. Suites select only the
closed actions below, and navigations are canonical paths on the Host-approved
local origin. This module intentionally imports only the Python standard library.
"""

import json
import re


MAX_SUITE_BYTES = 1024 * 1024
MAX_CASES = 100
MAX_STEPS = 100
MAX_TOTAL_STEPS = 1000
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_TEST_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_FIELDS = {
    "goto": frozenset({"action", "path"}),
    "fill": frozenset({"action", "selector", "value"}),
    "click": frozenset({"action", "selector"}),
    "assert_text": frozenset({"action", "selector", "text"}),
    "assert_visible": frozenset({"action", "selector"}),
    "assert_url": frozenset({"action", "path"}),
}


class BrowserContractError(ValueError):
    """Stable public code only; never echo tests, paths, selectors or values."""

    code = "TEST_RUNNER_ERROR"

    def __init__(self):
        super().__init__(self.code)


def _string(value, maximum, *, empty=False, multiline=False):
    if type(value) is not str or (not empty and not value):
        raise BrowserContractError()
    try:
        if len(value.encode("utf-8")) > maximum:
            raise BrowserContractError()
    except UnicodeError:
        raise BrowserContractError() from None
    if any((ord(char) < 32 and not (multiline and char in "\t\n\r"))
           or 127 <= ord(char) <= 159 for char in value):
        raise BrowserContractError()
    return value


def validate_local_path(value):
    """Return a canonical path, not a URL, query, escaped path or traversal."""
    value = _string(value, 4096)
    if (
        not value.startswith("/") or "//" in value or any(c in value for c in "\\%?#")
        or any(c.isspace() for c in value)
        or any(part in {".", ".."} for part in value.split("/"))
    ):
        raise BrowserContractError()
    return value


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrowserContractError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise BrowserContractError()


def parse_browser_suite(text, suite_name=None):
    """Validate closed JSON and return a fresh canonical declarative suite."""
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_SUITE_BYTES:
            raise BrowserContractError()
        if suite_name is not None and (type(suite_name) is not str or _NAME.fullmatch(suite_name) is None):
            raise BrowserContractError()
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
        if type(data) is not dict or set(data) != {"format", "tests"} or data["format"] != "browser-suite-v1":
            raise BrowserContractError()
        cases = data["tests"]
        if type(cases) is not list or not 1 <= len(cases) <= MAX_CASES:
            raise BrowserContractError()
        tests, identifiers, total_steps = [], set(), 0
        for case in cases:
            if type(case) is not dict or set(case) != {"testId", "steps"}:
                raise BrowserContractError()
            identifier = _string(case["testId"], 128)
            if _TEST_ID.fullmatch(identifier) is None or identifier in identifiers:
                raise BrowserContractError()
            identifiers.add(identifier)
            steps = case["steps"]
            if type(steps) is not list or not 1 <= len(steps) <= MAX_STEPS:
                raise BrowserContractError()
            total_steps += len(steps)
            if total_steps > MAX_TOTAL_STEPS:
                raise BrowserContractError()
            normalized, asserted = [], False
            for index, step in enumerate(steps):
                if type(step) is not dict or type(step.get("action")) is not str:
                    raise BrowserContractError()
                action = step["action"]
                if action not in _FIELDS or set(step) != _FIELDS[action] or (index == 0 and action != "goto"):
                    raise BrowserContractError()
                item = {"action": action}
                if "path" in step:
                    item["path"] = validate_local_path(step["path"])
                if "selector" in step:
                    item["selector"] = _string(step["selector"], 4096)
                if "value" in step:
                    item["value"] = _string(step["value"], 4096, empty=True, multiline=True)
                if "text" in step:
                    item["text"] = _string(step["text"], 4096, empty=True, multiline=True)
                asserted |= action.startswith("assert_")
                normalized.append(item)
            if not asserted:
                raise BrowserContractError()
            tests.append({"testId": identifier, "steps": normalized})
        return {"format": "browser-suite-v1", "tests": tests}
    except (BrowserContractError, ValueError, TypeError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise BrowserContractError() from None
