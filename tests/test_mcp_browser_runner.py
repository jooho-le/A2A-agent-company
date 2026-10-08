"""Trusted fake-browser harness checks; never run generated Source on Host."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
import signal
import subprocess
import sys
from types import SimpleNamespace, ModuleType
import unittest
from unittest.mock import Mock, patch

from mcp_tools.tools import browser_runner
from mcp_tools.tools.browser_contract import parse_browser_suite
from mcp_tools.tools.browser_report import parse_browser_report


def host(**changes):
    result = {"suite_name": "signup", "suite_path": "tests/browser/suite.json",
              "service_argv": ["/usr/local/bin/python", "-I", "/snapshot/server.py"],
              "base_url": "http://127.0.0.1:8765", "ready_path": "/health",
              "startup_timeout_seconds": 1, "action_timeout_ms": 100, "playwright_version": "1.55.0"}
    result.update(changes)
    return result


def suite(*steps, cases=1):
    actions = list(steps) or [{"action": "goto", "path": "/signup"},
                            {"action": "assert_visible", "selector": "#form"}]
    return {"format": "browser-suite-v1", "tests": [
        {"testId": f"case-{index}", "steps": [dict(step) for step in actions]} for index in range(cases)
    ]}


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class FakeService:
    pid = 424242

    def __init__(self):
        self.exit_code = None

    def poll(self):
        return self.exit_code


class FakeLocator:
    def __init__(self, page, selector):
        self.page = page
        self.selector = selector

    def fill(self, value, *, timeout):
        self.page.call("fill", value)

    def click(self, *, timeout):
        self.page.call("click", self.selector)

    def wait_for(self, *, state, timeout):
        self.page.call("assert_visible", state)

    def inner_text(self, *, timeout):
        self.page.call("assert_text", self.selector)
        return self.page.browser.text


class FakePage:
    def __init__(self, context):
        self.context, self.browser = context, context.browser
        self.url = "about:blank"
        self.frames = [self]
        self.closed = False

    def call(self, action, value):
        if self.browser.action_hook:
            self.browser.action_hook(self, action, value)

    def goto(self, url, *, timeout, wait_until):
        self.call("goto", url)
        self.url = self.browser.redirect or url
        return SimpleNamespace(status=self.browser.status)

    def locator(self, selector):
        return FakeLocator(self, selector)

    def wait_for_url(self, predicate, *, timeout, wait_until):
        self.call("assert_url", None)
        if not predicate(self.url):
            raise TimeoutError("raw URL or product data must not escape")

    def is_closed(self):
        return self.closed


class FakeContext:
    def __init__(self, browser, options):
        self.browser, self.options = browser, options
        self.pages = []
        self.closed = False
        self.route_handler = None

    def set_default_timeout(self, value):
        self.timeout = value

    def route(self, pattern, handler):
        self.pattern, self.route_handler = pattern, handler

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self):
        self.closed = True
        if self.browser.close_context_error:
            raise RuntimeError("raw DOM cleanup exception")


class FakeBrowser:
    version = "128.0.6613.85"

    def __init__(self):
        self.contexts = []
        self.connected = True
        self.closed = False
        self.close_context_error = False
        self.close_error = False
        self.action_hook = None
        self.text = "Created"
        self.redirect = None
        self.status = 200

    def new_context(self, **options):
        context = FakeContext(self, options)
        self.contexts.append(context)
        return context

    def is_connected(self):
        return self.connected

    def close(self):
        self.closed = True
        if self.close_error:
            raise RuntimeError("private browser exception")


class FakeManager:
    def __init__(self, browser):
        self.browser = browser
        self.enter_error = False
        self.launch_error = False
        self.exit_error = False
        self.exited = False
        self.launch_options = None

    def __enter__(self):
        if self.enter_error:
            raise RuntimeError("raw manager startup")
        return SimpleNamespace(chromium=self)

    def launch(self, **options):
        self.launch_options = options
        if self.launch_error:
            raise RuntimeError("raw browser startup")
        return self.browser

    def __exit__(self, *args):
        self.exited = True
        if self.exit_error:
            raise RuntimeError("raw manager shutdown")


class BrowserRunnerTests(unittest.TestCase):
    def setUp(self):
        self.clock, self.service, self.browser = Clock(), FakeService(), FakeBrowser()
        self.manager = FakeManager(self.browser)
        self.start = Mock(return_value=self.service)
        self.stop = Mock()
        self.probe = Mock(return_value=True)
        self.factory = Mock(return_value=self.manager)
        self.version = Mock(return_value="1.55.0")

    def run_tool(self, data=None, config=None, **changes):
        options = dict(contract_parser=parse_browser_suite, playwright_factory=self.factory,
                       version_provider=self.version, service_factory=self.start, service_stopper=self.stop,
                       readiness_probe=self.probe, clock=self.clock, sleep=self.clock.sleep)
        options.update(changes)
        return browser_runner.run_browser_tests(config or host(), data or suite(), **options)

    def error(self, result, expected="TEST_RUNNER_ERROR"):
        self.assertEqual(result, ({"error": expected}, 3 if expected == "BROWSER_START_FAILED" else 2))

    def test_import_and_configuration_are_inert(self):
        browser_runner._host_payload(host())
        self.start.assert_not_called()
        self.factory.assert_not_called()

    def test_success_exact_version_sandbox_and_cleanup(self):
        data, exit_code = self.run_tool()
        parsed = parse_browser_report(json.dumps(data), exit_code)
        self.assertEqual((parsed.total, parsed.passed, parsed.failed), (1, 1, 0))
        self.version.assert_called_once_with("playwright")
        self.assertEqual(self.manager.launch_options, {"headless": True, "chromium_sandbox": True, "timeout": 100})
        self.start.assert_called_once_with(host()["service_argv"])
        self.stop.assert_called_once_with(self.service)
        self.assertTrue(self.browser.closed)
        self.assertTrue(self.manager.exited)
        self.assertTrue(self.browser.contexts[0].closed)

    def test_fresh_context_per_case_downloads_and_workers_disabled(self):
        data, code = self.run_tool(suite(cases=3))
        self.assertEqual(parse_browser_report(json.dumps(data), code).passed, 3)
        self.assertEqual(len(self.browser.contexts), 3)
        self.assertEqual(len({id(context) for context in self.browser.contexts}), 3)
        for context in self.browser.contexts:
            self.assertEqual(context.options, {"accept_downloads": False, "service_workers": "block"})
            self.assertEqual(context.timeout, 100)
            self.assertEqual(context.pattern, "**/*")

    def test_all_actions_and_data_never_in_report(self):
        actions = [{"action": "goto", "path": "/signup"},
                   {"action": "fill", "selector": "#private-password", "value": "private-credential"},
                   {"action": "click", "selector": "#private-submit"},
                   {"action": "assert_text", "selector": "#private-status", "text": "Created"},
                   {"action": "assert_visible", "selector": "#private-form"},
                   {"action": "assert_url", "path": "/signup"}]
        data, code = self.run_tool(suite(*actions))
        parsed = parse_browser_report(json.dumps(data), code)
        self.assertEqual(len(parsed.tests[0].steps), 6)
        self.assertNotIn("private", json.dumps(data))
        self.assertNotIn("Created", json.dumps(data))
        self.assertNotIn("/signup", json.dumps(data))

    def test_assert_text_failure_exit_one_not_tool_failure(self):
        self.browser.text = "actual private DOM"
        data, code = self.run_tool(suite({"action": "goto", "path": "/"},
                                       {"action": "assert_text", "selector": "#private", "text": "Expected"}))
        self.assertEqual(code, 1)
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ASSERTION_FAILED")
        self.assertNotIn("private", json.dumps(data))
        self.assertTrue(self.browser.contexts[0].closed)

    def test_locator_timeout_safe_case_failure(self):
        def fail(_page, action, _value):
            if action == "assert_visible":
                raise TimeoutError("private selector and DOM")
        self.browser.action_hook = fail
        data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ACTION_TIMEOUT")
        self.assertNotIn("private", json.dumps(data))

    def test_unknown_action_exception_safe_case_failure(self):
        def fail(_page, action, _value):
            if action == "goto":
                raise RuntimeError("private source path")
        self.browser.action_hook = fail
        data, code = self.run_tool()
        report = parse_browser_report(json.dumps(data), code)
        self.assertEqual(report.tests[0].details, "ACTION_FAILED")
        self.assertEqual(len(report.tests[0].steps), 1)

    def test_case_failure_does_not_skip_later_cases(self):
        calls = [0]
        def fail(_page, action, _value):
            if action == "assert_visible":
                calls[0] += 1
                if calls[0] == 1:
                    raise TimeoutError("private")
        self.browser.action_hook = fail
        data, code = self.run_tool(suite(cases=2))
        report = parse_browser_report(json.dumps(data), code)
        self.assertEqual((report.total, report.passed, report.failed), (2, 1, 1))

    def test_off_origin_request_aborted_not_ignored(self):
        request_route = SimpleNamespace(request=SimpleNamespace(url="https://external.example/private"),
                                        abort=Mock(), continue_=Mock())
        def request(page, action, _value):
            if action == "goto":
                page.context.route_handler(request_route)
        self.browser.action_hook = request
        data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ORIGIN_DENIED")
        request_route.abort.assert_called_once_with("blockedbyclient")
        request_route.continue_.assert_not_called()
        self.assertNotIn("external", json.dumps(data))

    def test_same_origin_http_requests_continue(self):
        request_route = SimpleNamespace(request=SimpleNamespace(url="http://127.0.0.1:8765/api"),
                                        abort=Mock(), continue_=Mock())
        self.run_tool()
        self.browser.contexts[0].route_handler(request_route)
        request_route.continue_.assert_called_once_with()
        request_route.abort.assert_not_called()

    def test_request_denial_pumped_during_context_close_cannot_escape_as_pass(self):
        original_close = FakeContext.close
        request_route = SimpleNamespace(request=SimpleNamespace(url="https://external.example/private"),
                                        abort=Mock(), continue_=Mock())
        def close(context):
            original_close(context)
            context.route_handler(request_route)
        with patch.object(FakeContext, "close", close):
            data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ORIGIN_DENIED")
        self.assertEqual(data["tests"][0]["steps"][-1]["outcome"], "FAIL")

    def test_off_origin_navigation_frames_and_popups_fail(self):
        for url in ("https://external.example", "file:///snapshot/private", "data:text/html,private",
                    "http://127.0.0.1:8766", "http://user:private@127.0.0.1:8765"):
            with self.subTest(url=url):
                self.setUp()
                self.browser.redirect = url
                data, code = self.run_tool()
                self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ORIGIN_DENIED")
        self.setUp()
        def frame(page, action, _value):
            if action == "goto":
                page.frames.append(SimpleNamespace(url="https://external.example/private"))
        self.browser.action_hook = frame
        data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ORIGIN_DENIED")
        self.setUp()
        def popup(page, action, _value):
            if action == "goto":
                page.context.pages.append(SimpleNamespace(url="https://external.example/private", frames=[]))
        self.browser.action_hook = popup
        data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ORIGIN_DENIED")

    def test_goto_server_error_is_valid_failed_case(self):
        self.browser.status = 500
        data, code = self.run_tool()
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ACTION_FAILED")

    def test_empty_unknown_and_nonasserting_suite_never_launch(self):
        for data in ({"format": "browser-suite-v1", "tests": []}, {"format": "browser-suite-v2", "tests": []},
                     suite({"action": "goto", "path": "/"}), suite({"action": "eval", "code": "private"})):
            self.error(self.run_tool(data))
        self.start.assert_not_called()
        self.factory.assert_not_called()

    def test_missing_dependency_version_and_manager_failure_are_browser_start_errors(self):
        for version in (None, "1.54.0", "1.55.0-beta"):
            self.error(self.run_tool(version_provider=Mock(return_value=version)), "BROWSER_START_FAILED")
        self.error(self.run_tool(version_provider=Mock(side_effect=ImportError("private"))), "BROWSER_START_FAILED")
        self.start.assert_not_called()
        self.manager.enter_error = True
        self.error(self.run_tool(), "BROWSER_START_FAILED")
        self.stop.assert_called_once_with(self.service)

    def test_browser_launch_failed_no_relaxed_sandbox_retry(self):
        self.manager.launch_error = True
        self.error(self.run_tool(), "BROWSER_START_FAILED")
        self.assertEqual(self.manager.launch_options["chromium_sandbox"], True)
        self.factory.assert_called_once_with()
        self.assertTrue(self.manager.exited)
        self.stop.assert_called_once_with(self.service)

    def test_missing_invalid_actual_browser_version_never_pass(self):
        self.browser.version = "private browser"
        self.error(self.run_tool(), "BROWSER_START_FAILED")
        self.assertTrue(self.browser.closed)

    def test_service_start_failure_and_exit_never_pass(self):
        self.error(self.run_tool(service_factory=Mock(side_effect=OSError("private"))))
        self.stop.assert_not_called()
        self.service.exit_code = 1
        self.error(self.run_tool())
        self.stop.assert_called_once_with(self.service)
        self.factory.assert_not_called()

    def test_startup_deadline_is_bounded_and_probe_respects_remaining_budget(self):
        self.probe.return_value = False
        self.error(self.run_tool())
        self.assertLessEqual(self.clock.value, 1.00001)
        self.assertGreater(self.probe.call_count, 1)
        for call in self.probe.call_args_list:
            self.assertGreater(call.args[2], 0)
            self.assertLessEqual(call.args[2], 0.5)
        self.factory.assert_not_called()
        self.stop.assert_called_once_with(self.service)

    def test_service_exiting_after_readiness_is_infrastructure_failure(self):
        def probe(*_args):
            self.service.exit_code = 1
            return True
        self.error(self.run_tool(readiness_probe=probe))
        self.factory.assert_not_called()

    def test_browser_disconnect_or_closed_page_never_looks_like_assertion_failure(self):
        def disconnected(_page, _action, _value):
            self.browser.connected = False
            raise RuntimeError("private browser crash")
        self.browser.action_hook = disconnected
        self.error(self.run_tool())
        self.assertTrue(self.browser.closed)

    def test_service_dies_during_case_never_publish_success(self):
        self.browser.action_hook = lambda *_args: setattr(self.service, "exit_code", 1)
        self.error(self.run_tool())

    def test_context_setup_and_cleanup_failures_are_infrastructure(self):
        self.error(self.run_tool(playwright_factory=lambda: self.manager,
                                 service_stopper=Mock(side_effect=RuntimeError("private cleanup"))))
        self.setUp()
        self.browser.new_context = Mock(side_effect=RuntimeError("private setup"))
        self.error(self.run_tool())
        self.setUp()
        self.browser.close_context_error = True
        self.error(self.run_tool())
        self.setUp()
        self.browser.close_error = True
        self.error(self.run_tool())
        self.setUp()
        self.manager.exit_error = True
        self.error(self.run_tool())

    def test_host_unknown_keys_types_and_unsafe_origin_paths_rejected_before_launch(self):
        configurations = [host(extra="private")]
        for origin in ("https://127.0.0.1:8765", "http://localhost:8765", "http://127.0.0.1:8765/",
                       "http://127.0.0.1:80", "http://127.0.0.1:8765/path", "http://user:private@127.0.0.1:8765"):
            configurations.append(host(base_url=origin))
        for path in ("//external", "/../private", "/api?secret=private", "/api%2F", "/a b", "/a//b"):
            configurations.append(host(ready_path=path))
        for config in configurations:
            self.error(self.run_tool(config=config))
        self.start.assert_not_called()

    def test_host_timeout_suitepath_serviceargv_and_version_bounds(self):
        for name, values in {
            "startup_timeout_seconds": (True, 0, -1, 61, float("nan"), float("inf")),
            "action_timeout_ms": (True, 0, -1, 30001, 1.0),
            "suite_path": ("../private.json", "tests/../private.json", "tests/a.py", "tests//a.json"),
            "service_argv": ([], ["python"], ["/bin/bash"], ["/usr//bin/python"], ["/python", "a\n"]),
            "playwright_version": ("1.01.0", "2.0.0", "1.0", "1." + "1" * 32 + ".0"),
        }.items():
            for value in values:
                self.error(self.run_tool(config=host(**{name: value})))
        self.start.assert_not_called()

    def test_host_service_argument_empty_and_8192_bytes_match_host_policy(self):
        data, code = self.run_tool(config=host(service_argv=["/usr/local/bin/python", "", "a" * 8192]))
        self.assertEqual(parse_browser_report(json.dumps(data), code).passed, 1)

    def test_assert_url_predicate_exact_not_glob(self):
        self.browser.redirect = "http://127.0.0.1:8765/aX"
        data, code = self.run_tool(suite({"action": "goto", "path": "/aX"}, {"action": "assert_url", "path": "/a*"}))
        self.assertEqual(parse_browser_report(json.dumps(data), code).tests[0].details, "ACTION_TIMEOUT")

    def test_unicode_paths_use_same_encoded_browser_url_for_navigation_and_assertion(self):
        data, code = self.run_tool(suite({"action": "goto", "path": "/한글/"},
                                        {"action": "assert_url", "path": "/한글/"}))
        self.assertEqual(parse_browser_report(json.dumps(data), code).passed, 1)
        self.assertEqual(self.browser.contexts[0].pages[0].url,
                         "http://127.0.0.1:8765/%ED%95%9C%EA%B8%80/")
        self.assertNotIn("한글", json.dumps(data))

    def test_local_path_reserved_punctuation_preserved_and_non_url_chars_encoded(self):
        self.assertEqual(browser_runner._encoded_path("/a:!$&'()*+,;=@[]^|"), "/a:!$&'()*+,;=@[]%5E|")
        self.assertEqual(browser_runner._encoded_path("/한글/{x}/<y>`"),
                         "/%ED%95%9C%EA%B8%80/%7Bx%7D/%3Cy%3E%60")

    def test_subprocess_service_is_argv_no_shell_no_stdio_new_session(self):
        with patch.object(browser_runner.subprocess, "Popen", return_value=self.service) as start:
            result = browser_runner._start_service(host()["service_argv"])
        self.assertIs(result, self.service)
        self.assertEqual(start.call_args.args, (tuple(host()["service_argv"]),))
        self.assertEqual(start.call_args.kwargs, {
            "shell": False, "cwd": "/work", "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "start_new_session": True,
        })

    def test_service_group_cleanup_terminates_then_kills_descendants(self):
        process = Mock(pid=424242)
        with patch.object(browser_runner.os, "killpg") as kill:
            browser_runner._stop_service(process)
        self.assertEqual(kill.call_args_list, [unittest.mock.call(424242, signal.SIGTERM),
                                             unittest.mock.call(424242, signal.SIGKILL)])
        self.assertEqual(process.wait.call_count, 2)
        self.assertTrue(all(call.kwargs == {"timeout": 2} for call in process.wait.call_args_list))

    def test_service_group_cleanup_handles_exited_leader_and_timeout(self):
        process = Mock(pid=424242)
        process.wait.side_effect = [subprocess.TimeoutExpired("approved", 2), None]
        with patch.object(browser_runner.os, "killpg", side_effect=ProcessLookupError):
            browser_runner._stop_service(process)
        self.assertEqual(process.wait.call_count, 2)

    def test_readiness_http_no_proxy_redirect_or_body_read(self):
        response = Mock(status=302)
        connection = Mock()
        connection.getresponse.return_value = response
        with patch.object(browser_runner.http.client, "HTTPConnection", return_value=connection) as connect:
            self.assertFalse(browser_runner._readiness_probe("http://127.0.0.1:8765", "/health", .25))
        connect.assert_called_once_with("127.0.0.1", 8765, timeout=.25)
        connection.request.assert_called_once_with("GET", "/health")
        response.read.assert_not_called()
        connection.close.assert_called_once_with()

    def test_readiness_connection_failure_bounded_false(self):
        connection = Mock()
        connection.request.side_effect = OSError("private")
        with patch.object(browser_runner.http.client, "HTTPConnection", return_value=connection):
            self.assertFalse(browser_runner._readiness_probe("http://127.0.0.1:8765", "/health", .25))
        connection.close.assert_called_once_with()

    def test_readiness_unicode_path_encoded_without_proxy_or_redirect(self):
        connection = Mock()
        connection.getresponse.return_value = Mock(status=200)
        with patch.object(browser_runner.http.client, "HTTPConnection", return_value=connection):
            self.assertTrue(browser_runner._readiness_probe("http://127.0.0.1:8765", "/상태/", .25))
        connection.request.assert_called_once_with("GET", "/%EC%83%81%ED%83%9C/")

    def test_no_python_source_path_in_runner_imports(self):
        with open(browser_runner.__file__, encoding="utf-8") as source:
            text = source.read()
        self.assertNotIn('sys.path.insert(0, "/snapshot")', text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)
        self.assertNotIn("pip install", text)
        self.assertNotIn("no-sandbox", text)

    def test_cli_invalid_arguments_emit_only_safe_error(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = browser_runner.main(["--private"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "TEST_RUNNER_ERROR"})
        self.assertEqual(errors.getvalue(), "")

    def test_cli_contract_only_import_json_and_safe_output(self):
        contract_module = ModuleType("_browser_contract")
        contract_module.parse_browser_suite = parse_browser_suite
        payload, exit_code = self.run_tool()
        def noisy(*_args, **_kwargs):
            print("private Python console")
            os.write(1, b"private fd output")
            os.write(2, b"private fd error")
            return payload, exit_code
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"_browser_contract": contract_module}), \
                patch.object(browser_runner, "_read_json", side_effect=[host(), suite()]) as read, \
                patch.object(browser_runner, "run_browser_tests", side_effect=noisy), \
                redirect_stdout(output), redirect_stderr(errors):
            original_path = list(sys.path)
            code = browser_runner.main([])
            self.assertEqual(sys.path, original_path)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("private", output.getvalue())
        self.assertEqual(errors.getvalue(), "")
        self.assertEqual(str(read.call_args_list[0].args[0]), "/inputs/_browser_host.json")
        self.assertEqual(str(read.call_args_list[1].args[0]), "/inputs/tests/browser/suite.json")


if __name__ == "__main__":
    unittest.main()
