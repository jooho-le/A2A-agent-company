"""Pure, closed Host configuration; no Docker or generated test execution."""

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from mcp_tools.tools.unit_config import (
    MAX_UNIT_CONFIGURATION_BYTES, UnitTestConfiguration, UnitTestConfigurationError,
    UnitTestScope, decode_unit_configuration, encode_unit_configuration,
)
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import SandboxLimits


def scope(**changes):
    values = dict(name="self", kind="SNAPSHOT")
    values.update(changes)
    return UnitTestScope(**values)


def document(**changes):
    values = {"scopes": [{"name": "self", "kind": "SNAPSHOT"}]}
    values.update(changes)
    return json.dumps(values, ensure_ascii=False)


class UnitTestConfigurationTests(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(UnitTestConfigurationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "UNIT_TEST_CONFIGURATION_INVALID")
        self.assertEqual(caught.exception.args, ("UNIT_TEST_CONFIGURATION_INVALID",))
        self.assertIsNone(caught.exception.__cause__)

    def test_no_default_scopes(self):
        with self.assertRaises(TypeError):
            UnitTestConfiguration()
        for scopes in ((), [], None, (scope(),) * 33):
            self.assert_invalid(lambda: UnitTestConfiguration(scopes=scopes))

    def test_role_scopes_are_fixed(self):
        self.assertEqual(scope().roles, (AgentRole.DEVELOPER,))
        self.assertEqual(scope(kind="QA_TESTS").roles, (AgentRole.QA,))
        self.assertEqual(scope(kind="PROTECTED", protected_files={"tests/test_auth.py": "pass\n"},
                               protected_suite_ref="artifact://protected/tests").roles, (AgentRole.QA,))

    def test_scope_names_and_kinds_are_closed(self):
        for name in ("", "Self", "self name", "../self", 7, "a" * 65):
            self.assert_invalid(lambda: scope(name=name))
        for kind in ("snapshot", "SOURCE", "", 7, None):
            self.assert_invalid(lambda: scope(kind=kind))

    def test_duplicate_scope_names_are_denied(self):
        self.assert_invalid(lambda: UnitTestConfiguration(scopes=(scope(), scope(kind="QA_TESTS"))))

    def test_discovery_pattern_cannot_be_path_shell_or_nonpython(self):
        for pattern in ("../test_*.py", "tests/test_*.py", "test_*.js", "test_*.py;sh", "-test.py", "", 7):
            self.assert_invalid(lambda: scope(pattern=pattern))
        for pattern in ("test_*.py", "test_auth.py", "test_[ab].py", "test_?.py"):
            self.assertEqual(scope(pattern=pattern).pattern, pattern)

    def test_discovery_directory_is_tests_relative_only(self):
        for path in ("source", "/tests", "../tests", "tests/..", "tests//nested", "tests\\nested",
                     "tests/.env", "tests/.mcp-write-private", "tests/id_rsa", "tests/keys.key", None):
            self.assert_invalid(lambda: scope(source_directory=path))
        self.assertEqual(scope(source_directory="tests/nested").source_directory, "tests/nested")

    def test_nonprotected_scope_cannot_inject_tests_or_reference(self):
        for kind in ("SNAPSHOT", "QA_TESTS"):
            self.assert_invalid(lambda: scope(kind=kind, protected_files={}))
            self.assert_invalid(lambda: scope(kind=kind, protected_suite_ref="artifact://protected/tests"))

    def test_protected_scope_requires_nonempty_files_and_reference(self):
        for files in (None, {}, [], {"tests/test.py": b"pass"}, {"tests/test.py": None}):
            self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files=files,
                                             protected_suite_ref="artifact://protected/tests"))
        self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files={"tests/test.py": "pass"}))

    def test_protected_reference_is_logical_not_host_or_secret(self):
        files = {"tests/test.py": "pass"}
        for reference in ("/Users/private/tests", "file:///tmp/tests", "unix:///a.sock", "https://user:private@example.org/tests",
                          "artifact://protected/../tests", "artifact://protected/tests?token=private", "https://example.org/tests#x",
                          "artifact://protected/%74ests", "artifact://protected/.env", "", None):
            self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files=files, protected_suite_ref=reference))
        for reference in ("artifact://protected/tests", "https://example.org/protected/tests"):
            self.assertEqual(scope(kind="PROTECTED", protected_files=files,
                                  protected_suite_ref=reference).protected_suite_ref, reference)

    def test_protected_paths_are_closed_and_noncolliding(self):
        for path in ("test.py", "source/test.py", "/tests/test.py", "tests/../test.py", "tests/.git/config",
                     "tests/.mcp-write-staged", "tests/api.key", "tests/a:b.py"):
            self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files={path: "pass"},
                                             protected_suite_ref="artifact://protected/tests"))
        self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files={"tests/package": "pass", "tests/package/test.py": "pass"},
                                         protected_suite_ref="artifact://protected/tests"))

    def test_protected_files_are_copied_frozen_and_repr_hidden(self):
        files = {"tests/test_private.py": "# private fixture\n"}
        selected = scope(kind="PROTECTED", protected_files=files, protected_suite_ref="artifact://protected/tests")
        files["tests/test_private.py"] = "changed"
        self.assertEqual(selected.protected_files["tests/test_private.py"], "# private fixture\n")
        with self.assertRaises(TypeError):
            selected.protected_files["tests/test_private.py"] = "changed"
        self.assertNotIn("private fixture", repr(selected))
        self.assertNotIn("artifact://", repr(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.pattern = "changed"

    def test_source_credential_literals_denied_not_password_variables(self):
        for content in ('password = "private-value"\n', 'api_key = "private-value"', 'Bearer fixture-token', '\ud800', 'x\x00y'):
            self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files={"tests/test.py": content},
                                             protected_suite_ref="artifact://protected/tests"))
        code = "password = request.password\r\nassert password is not None\r\n"
        selected = scope(kind="PROTECTED", protected_files={"tests/test.py": code}, protected_suite_ref="artifact://protected/tests")
        self.assertEqual(selected.protected_files["tests/test.py"], code)

    def test_protected_file_count_and_configuration_bytes_bounded(self):
        self.assert_invalid(lambda: scope(kind="PROTECTED", protected_files={f"tests/test_{i}.py": "" for i in range(65)},
                                         protected_suite_ref="artifact://protected/tests"))
        selected = scope(kind="PROTECTED", protected_files={"tests/test.py": "# " + "x" * MAX_UNIT_CONFIGURATION_BYTES},
                         protected_suite_ref="artifact://protected/tests")
        self.assert_invalid(lambda: UnitTestConfiguration(scopes=(selected,)))

    def test_configuration_repr_is_empty_and_is_frozen(self):
        selected = UnitTestConfiguration(scopes=(scope(),), docker_endpoint="unix:///private/docker.sock")
        self.assertEqual(repr(selected), "UnitTestConfiguration()")
        with self.assertRaises(FrozenInstanceError):
            selected.scopes = ()

    def test_minimal_and_full_round_trip_canonical(self):
        minimal = decode_unit_configuration(document())
        self.assertEqual(minimal.python_executable, "/usr/local/bin/python")
        self.assertEqual(minimal.limits, SandboxLimits())
        selected = UnitTestConfiguration(scopes=(scope(), scope(name="qa", kind="QA_TESTS"),
            scope(name="fixed", kind="PROTECTED", protected_files={"tests/test_auth.py": "# 한글\n"},
                  protected_suite_ref="artifact://protected/tests")), limits=SandboxLimits(timeout_seconds=90),
            image_reference="sha256:" + "a" * 64, docker_endpoint="unix:///private/docker.sock")
        encoded = encode_unit_configuration(selected)
        self.assertEqual(decode_unit_configuration(encoded), selected)
        self.assertEqual(encode_unit_configuration(decode_unit_configuration(encoded)), encoded)
        self.assertIn("한글", encoded)

    def test_unknown_fields_and_wrong_types_denied(self):
        for key in ("argv", "shell", "env", "mounts", "network", "cwd", "run_id"):
            self.assert_invalid(lambda: decode_unit_configuration(document(**{key: "private"})))
        for scopes in (None, "self", {}, [7], [{"name": "self"}], [{"name": "self", "kind": "SNAPSHOT", "argv": []}]):
            self.assert_invalid(lambda: decode_unit_configuration(document(scopes=scopes)))
        for text in (None, "", "null", "[]", "true", "{}"):
            self.assert_invalid(lambda: decode_unit_configuration(text))

    def test_duplicate_json_keys_at_all_levels_denied(self):
        for text in (
            '{"scopes":[],"scopes":[]}',
            '{"scopes":[{"name":"self","name":"other","kind":"SNAPSHOT"}]}',
            '{"scopes":[{"name":"self","kind":"SNAPSHOT"}],"limits":{"cpus":1,"cpus":2}}',
            '{"scopes":[{"name":"fixed","kind":"PROTECTED","protected_suite_ref":"artifact://protected/tests","protected_files":{"tests/test.py":"pass","tests/test.py":"fail"}}]}',
        ):
            self.assert_invalid(lambda: decode_unit_configuration(text))

    def test_nonfinite_limits_and_unknown_limits_denied(self):
        for constant in ("NaN", "Infinity", "-Infinity", "1e10000"):
            text = '{"scopes":[{"name":"self","kind":"SNAPSHOT"}],"limits":{"cpus":' + constant + '}}'
            self.assert_invalid(lambda: decode_unit_configuration(text))
        for limits in ({"network": True}, None, [], {"pids": True}, {"timeout_seconds": "60"}):
            self.assert_invalid(lambda: decode_unit_configuration(document(limits=limits)))

    def test_executable_and_endpoint_use_build_policy(self):
        for executable in ("python", "/bin/sh", "/usr/bin/env", "/usr/bin/../python", "/usr/%70ython", "/", None):
            self.assert_invalid(lambda: UnitTestConfiguration(scopes=(scope(),), python_executable=executable))
        for endpoint in ("tcp://localhost:2375", "unix:///tmp/../docker.sock", "unix://host/docker.sock", "unix:///docker.sock?x=1", None):
            self.assert_invalid(lambda: UnitTestConfiguration(scopes=(scope(),), docker_endpoint=endpoint))
        self.assert_invalid(lambda: UnitTestConfiguration(scopes=(scope(),), image_reference="python:latest"))

    def test_total_json_size_and_invalid_json_error_are_code_only(self):
        self.assert_invalid(lambda: decode_unit_configuration(" " * (MAX_UNIT_CONFIGURATION_BYTES + 1)))
        self.assert_invalid(lambda: decode_unit_configuration('{"scopes":password=private'))

    def test_construction_encoding_decoding_are_inert(self):
        with patch("os.open", side_effect=AssertionError("filesystem")), patch("subprocess.Popen", side_effect=AssertionError("process")):
            selected = UnitTestConfiguration(scopes=(scope(),))
            self.assertEqual(decode_unit_configuration(encode_unit_configuration(selected)), selected)

    def test_forged_configuration_and_scope_rechecked_when_encoding(self):
        selected = UnitTestConfiguration(scopes=(scope(),))
        object.__setattr__(selected.scopes[0], "kind", "HOST")
        self.assert_invalid(lambda: encode_unit_configuration(selected))
        self.assert_invalid(lambda: encode_unit_configuration({"scopes": []}))
