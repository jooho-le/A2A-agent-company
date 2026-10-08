"""Trusted standalone Container-only Playwright harness.

Import is inert: no dependency installation, browser, service or Source import.
The fixed CLI imports only the shipped contract from /inputs. Tests are data,
not executable Python. Browser/service dependencies must exist in the image.
"""

from contextlib import contextmanager
import http.client
import importlib.metadata
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import quote, urlsplit


MAX_BYTES = 1024 * 1024
_VERSION = re.compile(r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_BROWSER_VERSION = re.compile(r"[0-9]{1,4}(?:\.[0-9]{1,4}){3}\Z")
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_SHELL_EXECUTABLES = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish", "powershell",
    "pwsh", "cmd", "cmd.exe", "env", "busybox",
})


class _RunnerError(Exception):
    def __init__(self, code="TEST_RUNNER_ERROR"):
        self.code = code
        super().__init__(code)


class _ActionFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _RunnerError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise _RunnerError()


def _local_path(value):
    if (type(value) is not str or not value.startswith("/") or "//" in value or
            len(value.encode("utf-8")) > 4096 or any(char in value for char in ("%", "?", "#", "\\")) or
            any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159 for char in value) or
            any(part in {".", ".."} for part in value.split("/"))):
        raise _RunnerError()
    return value


def _host_payload(data):
    fields = {"suite_name", "suite_path", "service_argv", "base_url", "ready_path",
              "startup_timeout_seconds", "action_timeout_ms", "playwright_version"}
    if type(data) is not dict or set(data) != fields:
        raise _RunnerError()
    if type(data["suite_name"]) is not str or _NAME.fullmatch(data["suite_name"]) is None:
        raise _RunnerError()
    path = data["suite_path"]
    if (type(path) is not str or not path.startswith("tests/") or not path.endswith(".json")
            or len(path.encode("utf-8")) > 4096 or any(part in {"", ".", ".."} for part in path.split("/"))
            or "\\" in path or ":" in path or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in path)):
        raise _RunnerError()
    argv = data["service_argv"]
    if (type(argv) not in (list, tuple) or not 1 <= len(argv) <= 64 or any(
            type(item) is not str or len(item.encode("utf-8")) > 8192
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in item) for item in argv)
            or not argv[0].startswith("/") or len(argv[0].encode("utf-8")) > 256
            or "\\" in argv[0] or "%" in argv[0]
            or any(part in {"", ".", ".."} for part in argv[0].split("/")[1:])
            or argv[0].rsplit("/", 1)[-1].casefold() in _SHELL_EXECUTABLES):
        raise _RunnerError()
    base = data["base_url"]
    if type(base) is not str:
        raise _RunnerError()
    parsed = urlsplit(base)
    port = parsed.port
    if (port is None or not 1024 <= port <= 65535 or base != "http://127.0.0.1:" + str(port)):
        raise _RunnerError()
    _local_path(data["ready_path"])
    startup = data["startup_timeout_seconds"]
    action = data["action_timeout_ms"]
    if (type(startup) not in (int, float) or not math.isfinite(startup) or not 0 < startup <= 60
            or type(action) is not int or not 1 <= action <= 30000 or
            type(data["playwright_version"]) is not str or len(data["playwright_version"]) > 32
            or _VERSION.fullmatch(data["playwright_version"]) is None):
        raise _RunnerError()
    return dict(data)


def _same_origin(url, base_url):
    try:
        parsed = urlsplit(url)
        base = urlsplit(base_url)
        return (parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
                and parsed.port == base.port and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


def _encoded_path(path):
    # Serialize Unicode/non-URL characters consistently with the browser's
    # URL representation. Input already rejects %, ?, # and traversal.
    return quote(path, safe="/:@!$&'()*+,;=-._~[]|")


def _readiness_probe(base_url, ready_path, timeout):
    """Direct local HTTP: no proxy environment, redirect following or body log."""
    connection = http.client.HTTPConnection("127.0.0.1", urlsplit(base_url).port, timeout=timeout)
    try:
        connection.request("GET", _encoded_path(ready_path))
        response = connection.getresponse()
        return 200 <= response.status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _start_service(argv):
    return subprocess.Popen(tuple(argv), shell=False, cwd="/work", stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def _stop_service(process):
    # Kill the whole new session even if its leader exited before cleanup.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait(timeout=2)


def _ready(process, host, probe, clock, sleep):
    deadline = clock() + host["startup_timeout_seconds"]
    while True:
        if process.poll() is not None:
            raise _RunnerError()
        remaining = deadline - clock()
        if remaining <= 0:
            raise _RunnerError()
        if probe(host["base_url"], host["ready_path"], min(0.5, remaining)):
            if process.poll() is not None:
                raise _RunnerError()
            return
        sleep(min(0.05, max(0, deadline - clock())))


def _check_origins(context, base, blocked):
    if blocked[0]:
        raise _ActionFailure("ORIGIN_DENIED")
    for page in context.pages:
        if page.url != "about:blank" and not _same_origin(page.url, base):
            raise _ActionFailure("ORIGIN_DENIED")
        for frame in page.frames:
            if frame.url != "about:blank" and not _same_origin(frame.url, base):
                raise _ActionFailure("ORIGIN_DENIED")


def _execute_action(page, step, host, clock, sleep):
    timeout = host["action_timeout_ms"]
    action = step["action"]
    if action == "goto":
        response = page.goto(host["base_url"] + _encoded_path(step["path"]), timeout=timeout, wait_until="domcontentloaded")
        if response is None or not 200 <= response.status < 400:
            raise _ActionFailure("ACTION_FAILED")
    elif action == "fill":
        page.locator(step["selector"]).fill(step["value"], timeout=timeout)
    elif action == "click":
        page.locator(step["selector"]).click(timeout=timeout)
    elif action == "assert_visible":
        page.locator(step["selector"]).wait_for(state="visible", timeout=timeout)
    elif action == "assert_url":
        expected = host["base_url"] + _encoded_path(step["path"])
        # Callable predicate avoids treating '*', '[' etc in a local path as a glob.
        page.wait_for_url(lambda url: str(url) == expected, timeout=timeout, wait_until="domcontentloaded")
        if page.url != expected:
            raise _ActionFailure("ASSERTION_FAILED")
    elif action == "assert_text":
        deadline = clock() + timeout / 1000
        locator = page.locator(step["selector"])
        while True:
            remaining = deadline - clock()
            if remaining <= 0:
                raise _ActionFailure("ASSERTION_FAILED")
            if locator.inner_text(timeout=max(1, int(remaining * 1000))) == step["text"]:
                return
            sleep(min(0.05, max(0, deadline - clock())))
    else:
        raise _RunnerError()


def _run_case(browser, case, host, timeout_error, clock, sleep):
    context = None
    result = None
    blocked = [False]
    try:
        context = browser.new_context(accept_downloads=False, service_workers="block")
        context.set_default_timeout(host["action_timeout_ms"])

        def route(request_route):
            if _same_origin(request_route.request.url, host["base_url"]):
                request_route.continue_()
            else:
                blocked[0] = True
                request_route.abort("blockedbyclient")

        context.route("**/*", route)
        page = context.new_page()
        steps = []
        details = None
        for index, step in enumerate(case["steps"], 1):
            started = clock()
            try:
                _check_origins(context, host["base_url"], blocked)
                _execute_action(page, step, host, clock, sleep)
                _check_origins(context, host["base_url"], blocked)
                outcome = "PASS"
            except _ActionFailure as exc:
                outcome, details = "FAIL", exc.code
            except timeout_error:
                if blocked[0]:
                    outcome, details = "FAIL", "ORIGIN_DENIED"
                else:
                    outcome, details = "FAIL", "ACTION_TIMEOUT"
            except Exception:
                if not browser.is_connected() or page.is_closed():
                    raise _RunnerError() from None
                outcome, details = "FAIL", "ORIGIN_DENIED" if blocked[0] else "ACTION_FAILED"
            duration = max(0, min(600000, int((clock() - started) * 1000)))
            steps.append({"index": index, "action": step["action"], "outcome": outcome, "durationMs": duration})
            if outcome == "FAIL":
                break
        result = {"testId": case["testId"], "outcome": "FAIL" if details else "PASS", "steps": steps}
        if details:
            result["details"] = details
        return result
    finally:
        if context is not None:
            context.close()
        # Closing a context can pump a final routed request. Its denial must
        # not be silently lost after the last assertion produced a result.
        if blocked[0] and result is not None and result["outcome"] == "PASS":
            result["outcome"] = "FAIL"
            result["details"] = "ORIGIN_DENIED"
            result["steps"][-1]["outcome"] = "FAIL"


def run_browser_tests(host_payload, suite, *, contract_parser, playwright_factory=None,
                      version_provider=None, service_factory=None, service_stopper=None,
                      readiness_probe=None, timeout_error=TimeoutError, clock=None, sleep=None):
    """Explicit Container execution; injectable trusted fakes are for tool tests.

    Return (payload,exit): assertion/action failure is a valid exit1 receipt;
    malformed suite/service/cleanup errors exit2; missing dependency, mismatch
    or browser startup errors exit3. No raw exception or product output escapes.
    """
    process = None
    browser = None
    report, error = None, None
    clock, sleep = clock or time.monotonic, sleep or time.sleep
    stopper = service_stopper or _stop_service
    try:
        host = _host_payload(host_payload)
        canonical = contract_parser(json.dumps(suite, ensure_ascii=False, allow_nan=False), host["suite_name"])
        try:
            actual_version = (version_provider or importlib.metadata.version)("playwright")
            if actual_version != host["playwright_version"]:
                raise _RunnerError("BROWSER_START_FAILED")
            if playwright_factory is None:
                from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
                playwright_factory, timeout_error = sync_playwright, PlaywrightTimeoutError
        except Exception:
            raise _RunnerError("BROWSER_START_FAILED") from None
        process = (service_factory or _start_service)(host["service_argv"])
        _ready(process, host, readiness_probe or _readiness_probe, clock, sleep)
        try:
            manager = playwright_factory()
            playwright = manager.__enter__()
        except Exception:
            raise _RunnerError("BROWSER_START_FAILED") from None
        try:
            try:
                browser = playwright.chromium.launch(headless=True, chromium_sandbox=True,
                                                      timeout=host["action_timeout_ms"])
                version = browser.version
                if type(version) is not str or _BROWSER_VERSION.fullmatch(version) is None:
                    raise _RunnerError("BROWSER_START_FAILED")
            except Exception:
                raise _RunnerError("BROWSER_START_FAILED") from None
            tests = []
            for case in canonical["tests"]:
                if process.poll() is not None or not browser.is_connected():
                    raise _RunnerError()
                tests.append(_run_case(browser, case, host, timeout_error, clock, sleep))
            if process.poll() is not None or not browser.is_connected():
                raise _RunnerError()
            report = {"format": "browser-v1", "suiteName": host["suite_name"],
                      "playwrightVersion": actual_version, "browserVersion": version,
                      "total": len(tests), "passed": sum(test["outcome"] == "PASS" for test in tests),
                      "failed": sum(test["outcome"] == "FAIL" for test in tests), "tests": tests}
        finally:
            try:
                if browser is not None:
                    browser.close()
            finally:
                manager.__exit__(None, None, None)
    except _RunnerError as exc:
        error = exc.code
    except BaseException:
        error = "TEST_RUNNER_ERROR"
    finally:
        if process is not None:
            try:
                stopper(process)
            except BaseException:
                error = "TEST_RUNNER_ERROR"
    if error is not None or report is None:
        code = error if error in {"BROWSER_START_FAILED", "TEST_RUNNER_ERROR"} else "TEST_RUNNER_ERROR"
        return {"error": code}, 3 if code == "BROWSER_START_FAILED" else 2
    try:
        if len(json.dumps(report, ensure_ascii=False, allow_nan=False).encode("utf-8")) > MAX_BYTES:
            raise _RunnerError()
    except Exception:
        return {"error": "TEST_RUNNER_ERROR"}, 2
    return report, 1 if report["failed"] else 0


@contextmanager
def _discard_output():
    original_stdout, original_stderr = sys.stdout, sys.stderr
    saved_stdout = saved_stderr = devnull = stream = None
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        stream = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr = stream
        yield
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        if stream is not None:
            stream.close()
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if devnull is not None:
            os.close(devnull)


def _read_json(path):
    with path.open("rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise _RunnerError()
    return json.loads(raw.decode("utf-8"), object_pairs_hook=_object, parse_constant=_nonfinite)


def main(argv=None):
    try:
        if argv is not None and argv or argv is None and len(sys.argv) != 1:
            raise _RunnerError()
        with _discard_output():
            # Only the read-only Host-shipped contract can be imported here.
            # -I/-B makes CWD and user PYTHONPATH/site imports unavailable.
            sys.path.insert(0, "/inputs")
            try:
                from _browser_contract import parse_browser_suite
            finally:
                sys.path.remove("/inputs")
            host = _host_payload(_read_json(Path("/inputs/_browser_host.json")))
            suite = _read_json(Path("/inputs") / host["suite_path"])
            payload, exit_code = run_browser_tests(host, suite, contract_parser=parse_browser_suite)
    except BaseException:
        payload, exit_code = {"error": "TEST_RUNNER_ERROR"}, 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
