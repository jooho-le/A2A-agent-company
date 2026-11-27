"""Pure browser policy and declarative JSON tests, with no generated execution."""

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from mcp_tools.tools.browser_config import (
    MAX_BROWSER_CONFIGURATION_BYTES, BrowserConfigurationError, BrowserTestConfiguration,
    BrowserTestSuite, browser_host_payload, decode_browser_configuration, encode_browser_configuration,
    validate_browser_host_payload,
)
from mcp_tools.tools.browser_contract import BrowserContractError, parse_browser_suite, validate_local_path
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import SandboxLimits


def suite_json(tests=None):
    return json.dumps({"format": "browser-suite-v1", "tests": tests if tests is not None else [
        {"testId": "signup-normal", "steps": [{"action": "goto", "path": "/signup"},
            {"action": "fill", "selector": "input[name=email]", "value": "user@example.test"},
            {"action": "click", "selector": "button[type=submit]"},
            {"action": "assert_text", "selector": "#result", "text": "가입 완료"}]}]}, ensure_ascii=False)


def suite(**changes):
    values = {"name": "signup", "kind": "QA_TESTS"}
    values.update(changes)
    return BrowserTestSuite(**values)


def configuration(**changes):
    values = {"suites": (suite(),), "service_argv": ("/usr/local/bin/python", "-B", "/snapshot/app.py"),
        "playwright_version": "1.55.0"}
    values.update(changes)
    return BrowserTestConfiguration(**values)


def document(**changes):
    values = {"suites": [{"name": "signup", "kind": "QA_TESTS"}],
        "service_argv": ["/usr/local/bin/python", "/snapshot/app.py"], "playwright_version": "1.55.0"}
    values.update(changes)
    return json.dumps(values, ensure_ascii=False)


class BrowserConfigurationTests(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(BrowserConfigurationError) as caught:
            operation()
        self.assertEqual(caught.exception.args, ("BROWSER_CONFIGURATION_INVALID",))
        self.assertEqual(caught.exception.code, "BROWSER_CONFIGURATION_INVALID")
        self.assertIsNone(caught.exception.__cause__)

    def test_all_execution_capabilities_are_host_required(self):
        for kwargs in ({}, {"suites": (suite(),)}, {"suites": (suite(),), "service_argv": ()}):
            with self.assertRaises(TypeError):
                BrowserTestConfiguration(**kwargs)
        for suites in (None, [], (), (suite(),) * 33):
            self.assert_invalid(lambda: configuration(suites=suites))
        for argv in (None, [], (), ("python",)):
            self.assert_invalid(lambda: configuration(service_argv=argv))

    def test_qa_only_no_developer_or_snapshot_mode(self):
        self.assertEqual(suite().roles, (AgentRole.QA,))
        for kind in ("SNAPSHOT", "SOURCE", "qa", None, {}, 5):
            self.assert_invalid(lambda: suite(kind=kind))

    def test_closed_suite_names(self):
        for name in ("", "Signup", "../signup", "signup normal", "a" * 65, None, 4):
            self.assert_invalid(lambda: suite(name=name))

    def test_suite_paths_are_json_test_paths_only(self):
        for path in ("source/suite.json", "/tests/suite.json", "tests/../suite.json", "tests//suite.json",
                     "tests/.env", "tests/.git/suite.json", "tests/.mcp-write-private.json", "tests/file.py", "tests/a%2ejson"):
            self.assert_invalid(lambda: suite(suite_path=path))
        self.assertEqual(suite(suite_path="tests/한글/가입.json").suite_path, "tests/한글/가입.json")

    def test_duplicate_suite_names_are_not_ambiguous(self):
        self.assert_invalid(lambda: configuration(suites=(suite(), suite(suite_path="tests/other.json"))))

    def test_unprotected_suite_has_no_host_tests_or_reference(self):
        self.assert_invalid(lambda: suite(protected_files={}))
        self.assert_invalid(lambda: suite(protected_suite_ref="artifact://protected/tests"))

    def test_protected_requires_selected_valid_suite_and_ref(self):
        for files in (None, {}, {"tests/other.json": suite_json()}, {"tests/browser/suite.json": "{}"}):
            self.assert_invalid(lambda: suite(kind="PROTECTED", protected_files=files, protected_suite_ref="artifact://protected/tests"))
        self.assert_invalid(lambda: suite(kind="PROTECTED", protected_files={"tests/browser/suite.json": suite_json()}))

    def test_protected_bytes_are_frozen_and_private(self):
        files = {"tests/browser/suite.json": suite_json()}
        selected = suite(kind="PROTECTED", protected_files=files, protected_suite_ref="artifact://protected/tests")
        files["tests/browser/suite.json"] = "changed"
        self.assertEqual(selected.protected_files["tests/browser/suite.json"], suite_json())
        with self.assertRaises(TypeError):
            selected.protected_files["tests/other.json"] = "changed"
        self.assertNotIn("가입 완료", repr(selected))
        self.assertNotIn("artifact://", repr(selected))

    def test_protected_host_or_secret_reference_denied(self):
        for reference in ("file:///private/tests", "/private/tests", "https://u:secret@example.org/tests",
                          "artifact://p/tests?token=private", "artifact://p/../tests", "artifact://p/%74ests"):
            self.assert_invalid(lambda: suite(kind="PROTECTED", protected_files={"tests/browser/suite.json": suite_json()},
                                              protected_suite_ref=reference))

    def test_shell_and_credential_service_argv_denied(self):
        for argv in (("/bin/sh", "-c", "echo fixture"), ("/usr/bin/env", "python"), ("/bin/bash",),
                     ("/usr/local/bin/python", "--password=private"), ("/usr/local/bin/python", "token=private"),
                     ("/usr/local/bin/python", "\n"), ("/usr/../bin/python",)):
            self.assert_invalid(lambda: configuration(service_argv=argv))

    def test_python_executable_and_docker_policy_shared(self):
        for executable in ("python", "/bin/sh", "/usr/bin/env", "/", "/usr/%70ython", None):
            self.assert_invalid(lambda: configuration(python_executable=executable))
        for endpoint in ("tcp://127.0.0.1:2375", "unix://host/socket", "unix:///tmp/../socket", "unix:///socket?x=1"):
            self.assert_invalid(lambda: configuration(docker_endpoint=endpoint))
        self.assert_invalid(lambda: configuration(image_reference="python:latest"))

    def test_origin_exact_local_numeric_unprivileged_port(self):
        for url in ("http://localhost:8765", "https://127.0.0.1:8765", "http://127.0.0.1:80", "http://127.0.0.1:65536",
                    "http://127.0.0.1:08765", "http://127.0.0.1:8765/", "http://user:private@127.0.0.1:8765",
                    "http://127.0.0.2:8765", "http://example.org:8765", "http://127.0.0.1:8765?x=1", None):
            self.assert_invalid(lambda: configuration(base_url=url))
        for port in (1024, 8765, 65535):
            self.assertEqual(configuration(base_url=f"http://127.0.0.1:{port}").base_url, f"http://127.0.0.1:{port}")

    def test_ready_path_is_local_canonical_not_absolute_url(self):
        for path in ("ready", "//example.org/x", "/../ready", "/ready%2f", "/ready?token=private", "/ready#x", "/\\evil", "/r d"):
            self.assert_invalid(lambda: configuration(ready_path=path))
        self.assertEqual(configuration(ready_path="/health/").ready_path, "/health/")

    def test_playwright_version_exact_not_floating(self):
        for version in ("latest", "^1.55.0", "1.55", "1.55.0-beta", "2.0.0", "1.055.0", "1.55.00", "1.55.0\n", None):
            self.assert_invalid(lambda: configuration(playwright_version=version))
        self.assertEqual(configuration(playwright_version="1.0.0").playwright_version, "1.0.0")

    def test_startup_timeout_finite_bounded_nonboolean(self):
        for value in (0, -1, 61, float("nan"), float("inf"), True, "10", None):
            self.assert_invalid(lambda: configuration(startup_timeout_seconds=value))
        self.assertEqual(configuration(startup_timeout_seconds=0.5).startup_timeout_seconds, 0.5)

    def test_action_timeout_integer_bounded_nonboolean(self):
        for value in (0, -1, 30001, 1.5, True, "5000", None):
            self.assert_invalid(lambda: configuration(action_timeout_ms=value))

    def test_full_and_minimal_round_trip_closed_json(self):
        minimum = decode_browser_configuration(document())
        self.assertEqual(minimum.service_argv, ("/usr/local/bin/python", "/snapshot/app.py"))
        selected = configuration(suites=(suite(), suite(name="fixed", kind="PROTECTED", protected_files={"tests/browser/suite.json": suite_json()},
            protected_suite_ref="artifact://protected/tests")), limits=SandboxLimits(timeout_seconds=90),
            image_reference="sha256:" + "a" * 64, docker_endpoint="unix:///private/docker.sock")
        encoded = encode_browser_configuration(selected)
        self.assertEqual(decode_browser_configuration(encoded), selected)
        self.assertEqual(encode_browser_configuration(decode_browser_configuration(encoded)), encoded)
        self.assertIn("가입 완료", encoded)

    def test_unknown_or_malformed_document_fields_denied(self):
        for key in ("argv", "shell", "env", "mounts", "network", "cwd", "run_id"):
            self.assert_invalid(lambda: decode_browser_configuration(document(**{key: "private"})))
        for value in (None, "", "null", "[]", "true", "{}"):
            self.assert_invalid(lambda: decode_browser_configuration(value))
        for suites in (None, {}, [], [5], [{"name": "signup"}], [{"name": "signup", "kind": "QA_TESTS", "javascript": "eval"}]):
            self.assert_invalid(lambda: decode_browser_configuration(document(suites=suites)))

    def test_duplicate_json_at_nested_levels_denied(self):
        for text in ('{"suites":[],"suites":[]}',
            '{"suites":[{"name":"x","name":"y","kind":"QA_TESTS"}],"service_argv":[],"playwright_version":"1.55.0"}',
            document()[:-1] + ',"limits":{"pids":64,"pids":65}}'):
            self.assert_invalid(lambda: decode_browser_configuration(text))

    def test_nonfinite_or_unknown_limits_are_rejected(self):
        for value in ("NaN", "Infinity", "-Infinity", "1e10000"):
            self.assert_invalid(lambda: decode_browser_configuration(document()[:-1] + ',"startup_timeout_seconds":' + value + '}'))
        for limits in ({"network": True}, None, [], {"pids": True}):
            self.assert_invalid(lambda: decode_browser_configuration(document(limits=limits)))

    def test_size_bound_and_error_messages_hide_input(self):
        self.assert_invalid(lambda: decode_browser_configuration(" " * (MAX_BROWSER_CONFIGURATION_BYTES + 1)))
        self.assert_invalid(lambda: decode_browser_configuration("password=private"))
        self.assert_invalid(lambda: configuration(service_argv=("/usr/local/bin/python", "x" * 9000)))

    def test_inert_construction_and_serialization(self):
        with patch("os.open", side_effect=AssertionError("no files")), patch("subprocess.Popen", side_effect=AssertionError("no processes")):
            selected = configuration()
            self.assertEqual(decode_browser_configuration(encode_browser_configuration(selected)), selected)

    def test_frozen_configuration_and_hidden_repr(self):
        selected = configuration()
        self.assertEqual(repr(selected), "BrowserTestConfiguration()")
        with self.assertRaises(FrozenInstanceError):
            selected.service_argv = ("/bin/sh",)

    def test_forged_configuration_and_suite_rechecked(self):
        selected = configuration()
        object.__setattr__(selected.suites[0], "kind", "SNAPSHOT")
        self.assert_invalid(lambda: encode_browser_configuration(selected))
        self.assert_invalid(lambda: encode_browser_configuration({}))

    def test_host_payload_is_closed_copy_and_suite_selection_matches(self):
        selected = configuration()
        payload = browser_host_payload(selected, selected.suites[0])
        self.assertEqual(validate_browser_host_payload(payload), payload)
        payload["service_argv"].append("changed")
        self.assertEqual(selected.service_argv, configuration().service_argv)
        self.assert_invalid(lambda: browser_host_payload(selected, suite(name="not-selected")))
        self.assert_invalid(lambda: validate_browser_host_payload({**payload, "shell": True}))


class BrowserContractTests(unittest.TestCase):
    def assert_invalid(self, text):
        with self.assertRaises(BrowserContractError) as caught:
            parse_browser_suite(text)
        self.assertEqual(caught.exception.args, ("TEST_RUNNER_ERROR",))
        self.assertIsNone(caught.exception.__cause__)

    def test_valid_declarative_actions_are_preserved_in_order(self):
        self.assertEqual(parse_browser_suite(suite_json()), json.loads(suite_json()))

    def test_all_six_actions_and_empty_multiline_values(self):
        steps = [{"action": "goto", "path": "/"}, {"action": "fill", "selector": "#name", "value": ""},
            {"action": "click", "selector": "#button"}, {"action": "assert_text", "selector": "#result", "text": "한글\n줄"},
            {"action": "assert_visible", "selector": "#result"}, {"action": "assert_url", "path": "/done/"}]
        parsed = parse_browser_suite(suite_json([{"testId": "case_1", "steps": steps}]), "signup")
        self.assertEqual(parsed["tests"][0]["steps"], steps)

    def test_closed_format_and_no_arbitrary_execution_or_timeout(self):
        for extra in ({"script": "eval"}, {"base_url": "https://example.org"}, {"timeout": 60000}):
            self.assert_invalid(json.dumps({**json.loads(suite_json()), **extra}))
        for action in ("evaluate", "exec", "shell", "fetch", "goto_url", "screenshot", "wait_for_timeout"):
            self.assert_invalid(suite_json([{"testId": "case", "steps": [{"action": "goto", "path": "/"}, {"action": action}]}]))

    def test_no_empty_suite_or_duplicate_identifiers(self):
        self.assert_invalid(suite_json([]))
        case = json.loads(suite_json())["tests"][0]
        self.assert_invalid(suite_json([case, case]))

    def test_identifier_ascii_safe_and_bounded(self):
        steps = [{"action": "goto", "path": "/"}, {"action": "assert_url", "path": "/"}]
        for identifier in ("", "../case", "한글", "a" * 129, "case\n", 5):
            self.assert_invalid(suite_json([{"testId": identifier, "steps": steps}]))
        self.assertEqual(parse_browser_suite(suite_json([{"testId": "a" * 128, "steps": steps}]))["tests"][0]["testId"], "a" * 128)

    def test_requires_initial_goto_and_at_least_one_assertion(self):
        for steps in ([], [{"action": "assert_url", "path": "/"}], [{"action": "goto", "path": "/"}],
                      [{"action": "goto", "path": "/"}, {"action": "click", "selector": "#x"}]):
            self.assert_invalid(suite_json([{"testId": "case", "steps": steps}]))

    def test_step_fields_and_types_are_closed(self):
        for step in ({"action": "goto", "path": "/", "url": "https://example.org"}, {"action": "click", "selector": "#x", "timeout": 60000},
                     {"action": "fill", "selector": "#x"}, {"action": "assert_text", "selector": "#x", "text": 5}, None):
            self.assert_invalid(suite_json([{"testId": "case", "steps": [{"action": "goto", "path": "/"}, step,
                {"action": "assert_url", "path": "/"}]}]))

    def test_path_cannot_escape_local_origin(self):
        for path in ("https://example.org/x", "//example.org/x", "/..", "/a/../b", "/%2e%2e/x", "/a?x=1", "/a#x", "/\\x", "/a//b", "/a b"):
            with self.assertRaises(BrowserContractError):
                validate_local_path(path)
        self.assertEqual(validate_local_path("/한글/"), "/한글/")

    def test_per_case_case_count_and_total_steps_limits(self):
        base = [{"action": "goto", "path": "/"}, {"action": "assert_url", "path": "/"}]
        self.assert_invalid(suite_json([{"testId": f"case-{i}", "steps": base} for i in range(101)]))
        self.assert_invalid(suite_json([{"testId": "case", "steps": base + [{"action": "click", "selector": "#x"}] * 99}]))
        self.assert_invalid(suite_json([{"testId": f"case-{i}", "steps": base + [{"action": "click", "selector": "#x"}] * 98} for i in range(11)]))

    def test_text_selector_encoding_control_and_size_bounds(self):
        for selector in ("", "x" * 4097, "#x\n", "\ud800", 5):
            self.assert_invalid(suite_json([{"testId": "case", "steps": [{"action": "goto", "path": "/"},
                {"action": "assert_visible", "selector": selector}]}]))
        self.assert_invalid(suite_json([{"testId": "case", "steps": [{"action": "goto", "path": "/"},
            {"action": "assert_text", "selector": "#x", "text": "\x00"}]}]))

    def test_duplicate_json_nonfinite_and_malformed_types_fail_closed(self):
        for text in (None, "", "null", "[]", "{}", '{"format":"browser-suite-v1","format":"browser-suite-v1","tests":[]}',
                     '{"format":"browser-suite-v1","tests":NaN}', '{"format":"browser-suite-v1","tests":true}',
                     " " * (1024 * 1024 + 1)):
            self.assert_invalid(text)

    def test_optional_suite_name_is_not_embedded_or_unbounded(self):
        self.assertEqual(parse_browser_suite(suite_json(), "signup"), parse_browser_suite(suite_json()))
        for name in ("../signup", "Signup", 5):
            with self.assertRaises(BrowserContractError):
                parse_browser_suite(suite_json(), name)
