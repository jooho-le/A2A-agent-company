"""Step 39 approved Tool profiles: offline JSON/lock checks, no Docker.

Digests, package versions and argv below are fake validation fixtures, not
approved execution images or evidence that product code has been executed.
"""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.platform.tool_configuration import (
    ToolConfigurationError, decode_tool_configuration, load_tool_configuration,
)
from agents.platform.check_tools import main as check_tools
from mcp_tools.tools.build_config import BuildConfiguration
from mcp_tools.tools.browser_config import BrowserTestConfiguration
from mcp_tools.tools.security_config import SecurityScanConfiguration
from mcp_tools.tools.unit_config import UnitTestConfiguration
from orchestrator.domain.run_configuration import ExecutionBaseline
from orchestrator.domain.states import AgentRole


IMAGE = "sha256:" + "d" * 64
LOCK = b"offline-fixture-dependency==1.0\n"
LOCK_HASH = "sha256:" + sha256(LOCK).hexdigest()
ENDPOINT = "unix:///var/run/docker.sock"
PREFIX = "TOOL_CONFIGURATION_"
SUITE_REF = "https://criteria.example.invalid/signup/v1"


def limits():
    return {
        "cpus": 1, "memory_bytes": 256 * 1024 * 1024, "pids": 64,
        "timeout_seconds": 30, "tmpfs_bytes": 64 * 1024 * 1024,
        "max_stdout_bytes": 1024 * 1024, "max_stderr_bytes": 1024 * 1024,
        "control_timeout_seconds": 2,
    }


def configuration_data():
    return {
        "schemaVersion": 1, "imageReference": IMAGE,
        "dependencyLockFile": "../templates/requirements.lock", "dependencyLockHash": LOCK_HASH,
        "hardwareProfile": "offline-test-profile", "dockerEndpoint": ENDPOINT, "maxCallSeconds": 60,
        "build": {"profile": {
            "name": "fixture-build", "argv": ["/usr/local/bin/python", "-B", "/snapshot/build.py"],
            "limits": limits(),
        }},
        "unit": {
            "scopes": [{"name": "source-unit", "kind": "SNAPSHOT"},
                       {"name": "qa-unit", "kind": "QA_TESTS"}],
            "python_executable": "/usr/local/bin/python", "limits": limits(),
        },
        "browser": {
            "suites": [{"name": "qa-browser", "kind": "QA_TESTS"}],
            "service_argv": ["/usr/local/bin/python", "-B", "/snapshot/service.py"],
            "python_executable": "/usr/local/bin/python", "playwright_version": "1.60.0",
            "base_url": "http://127.0.0.1:8765", "ready_path": "/", "startup_timeout_seconds": 5,
            "action_timeout_ms": 1000, "limits": limits(),
        },
        "security": {
            "profiles": [{"name": "python-security", "scanner_version": "1.8.6",
                          "rule_ids": ["B101", "B307"],
                          "profile_ref": "https://criteria.example.invalid/security/bandit/v1"}],
            "python_executable": "/usr/local/bin/python", "limits": limits(),
        },
    }


def protected_unit():
    return {"name": "protected-unit", "kind": "PROTECTED", "protected_suite_ref": SUITE_REF,
            "protected_files": {"tests/test_signup.py": "# approved independent fixture; never executed\n"}}


def protected_browser():
    suite = {"format": "browser-suite-v1", "tests": [{"testId": "signup.visible", "steps": [
        {"action": "goto", "path": "/"}, {"action": "assert_visible", "selector": "#signup"}]}]}
    return {"name": "protected-browser", "kind": "PROTECTED", "protected_suite_ref": SUITE_REF,
            "protected_files": {"tests/browser/suite.json": json.dumps(suite)}}


class _ToolConfigurationFixture:
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-tool-settings-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base = self.directory / "config"
        self.base.mkdir()
        self.file = self.base / "owned-tools.json"
        self.lock_file = self.directory / "templates/requirements.lock"
        self.data = configuration_data()

    def decode(self, data=None):
        return decode_tool_configuration(json.dumps(self.data if data is None else data))

    def write(self, data=None, *, lock=True):
        self.file.write_text(json.dumps(self.data if data is None else data), encoding="utf-8")
        if lock:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True)
            self.lock_file.write_bytes(LOCK)
        return self.file

    def assert_code(self, suffix, operation, *args, **kwargs):
        with self.assertRaises(ToolConfigurationError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, PREFIX + suffix)
        self.assertEqual(str(caught.exception), PREFIX + suffix)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertNotIn("private-tool-secret", str(caught.exception))


class ToolConfigurationSchemaTests(_ToolConfigurationFixture, unittest.TestCase):
    def test_valid_profiles_share_one_immutable_execution_baseline(self):
        config = self.decode()
        self.assertIs(type(config.baseline), ExecutionBaseline)
        self.assertEqual(config.baseline.container_image_digest, IMAGE)
        self.assertEqual(config.baseline.dependency_lock_hash, LOCK_HASH)
        self.assertEqual(config.baseline.hardware_profile, "offline-test-profile")
        self.assertEqual(config.baseline.network_policy, "DENY")
        self.assertEqual(config.baseline.allowed_hosts, ())
        self.assertEqual(config.image_reference, IMAGE)
        self.assertEqual(config.dependency_lock_file, "../templates/requirements.lock")
        self.assertEqual(config.max_call_seconds, 60)
        self.assertIs(type(config.build_configuration), BuildConfiguration)
        self.assertIs(type(config.browser_configuration), BrowserTestConfiguration)
        self.assertIs(type(config.security_configuration), SecurityScanConfiguration)
        for child in (config.unit_configuration_for(AgentRole.DEVELOPER),
                      config.unit_configuration_for(AgentRole.QA), config.browser_configuration,
                      config.security_configuration):
            self.assertEqual(child.image_reference, IMAGE)
            self.assertEqual(child.docker_endpoint, ENDPOINT)
        self.assertEqual(config.build_configuration.profile.image_reference, IMAGE)
        self.assertEqual(config.build_configuration.docker_endpoint, ENDPOINT)
        self.assertFalse(self.lock_file.exists())

    def test_named_pinned_image_preserves_reference_but_baseline_uses_digest(self):
        image = "registry.example.invalid/team/tool-runtime@" + IMAGE
        config = self.decode({**self.data, "imageReference": image})
        self.assertEqual(config.image_reference, image)
        self.assertEqual(config.baseline.container_image_digest, IMAGE)
        self.assertEqual(config.build_configuration.profile.image_reference, image)

    def test_every_top_level_approved_field_is_explicit(self):
        for field in self.data:
            data = deepcopy(self.data)
            del data[field]
            with self.subTest(field=field):
                self.assert_code("INVALID", self.decode, data)

    def test_required_child_profile_fields_cannot_fall_back_to_codec_defaults(self):
        for block in ("unit", "browser", "security"):
            for field in self.data[block]:
                data = deepcopy(self.data)
                del data[block][field]
                with self.subTest(block=block, field=field):
                    self.assert_code("INVALID", self.decode, data)
        for field in self.data["build"]["profile"]:
            data = deepcopy(self.data)
            del data["build"]["profile"][field]
            with self.subTest(block="build.profile", field=field):
                self.assert_code("INVALID", self.decode, data)

    def test_all_eight_resource_limits_are_required_for_every_tool(self):
        for block in ("build", "unit", "browser", "security"):
            for field in limits():
                data = deepcopy(self.data)
                target = data["build"]["profile"] if block == "build" else data[block]
                del target["limits"][field]
                with self.subTest(block=block, field=field):
                    self.assert_code("INVALID", self.decode, data)

    def test_unknown_keys_are_rejected_at_top_level_and_each_nested_contract(self):
        for selector in ("root", "build", "build.profile", "unit", "unit.scope", "browser", "browser.suite",
                         "security", "security.profile", "build.limits"):
            data = deepcopy(self.data)
            targets = {
                "root": data, "build": data["build"], "build.profile": data["build"]["profile"],
                "unit": data["unit"], "unit.scope": data["unit"]["scopes"][0], "browser": data["browser"],
                "browser.suite": data["browser"]["suites"][0], "security": data["security"],
                "security.profile": data["security"]["profiles"][0], "build.limits": data["build"]["profile"]["limits"],
            }
            targets[selector]["unapproved"] = "private-tool-secret"
            with self.subTest(selector=selector):
                self.assert_code("INVALID", self.decode, data)

    def test_nested_image_and_endpoint_override_is_forbidden_even_when_identical(self):
        for selector in ("build", "build.profile", "unit", "browser", "security"):
            for field, value in (("image_reference", IMAGE), ("image_reference", "sha256:" + "a" * 64),
                                 ("docker_endpoint", ENDPOINT), ("docker_endpoint", "unix:///tmp/other.sock")):
                data = deepcopy(self.data)
                target = data["build"]["profile"] if selector == "build.profile" else data[selector]
                target[field] = value
                with self.subTest(selector=selector, field=field, value=value):
                    self.assert_code("INVALID", self.decode, data)

    def test_unpinned_or_malformed_image_and_lock_hash_are_not_approved(self):
        for field, values in (("imageReference", ("python:latest", "python:3.12", "sha256:abc", IMAGE.upper(), "", None)),
                              ("dependencyLockHash", ("a" * 64, "sha256:abc", LOCK_HASH.upper(), "", None))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_code("INVALID", self.decode, {**self.data, field: value})

    def test_unsupported_remote_or_malformed_docker_endpoints_are_denied(self):
        for endpoint in ("tcp://127.0.0.1:2375", "ssh://remote", "unix:///tmp/../docker.sock",
                         "unix:///var/run/docker.sock?token=private-tool-secret", "", True):
            with self.subTest(endpoint=endpoint):
                self.assert_code("INVALID", self.decode, {**self.data, "dockerEndpoint": endpoint})

    def test_lock_file_reference_is_trusted_path_not_shell_expansion_or_url(self):
        for path in ("", ".", "~/.ssh/private", "$UNAPPROVED/requirements.lock", "file:///tmp/key",
                     "https://example.invalid/lock", "bad\npath", True, None):
            with self.subTest(path=path):
                self.assert_code("INVALID", self.decode, {**self.data, "dependencyLockFile": path})

    def test_top_level_schema_and_max_call_time_use_strict_numeric_contracts(self):
        for value in (True, "1", 1.5, 2, None):
            with self.subTest(version=value):
                self.assert_code("INVALID", self.decode, {**self.data, "schemaVersion": value})
        for value in (True, "60", 0, .001, 601, -1, float("nan"), float("inf")):
            with self.subTest(maxCallSeconds=value):
                self.assert_code("INVALID", self.decode, {**self.data, "maxCallSeconds": value})

    def test_resources_reject_boolean_string_fraction_for_integer_limits(self):
        for block in ("build", "unit", "browser", "security"):
            for field in limits():
                values = (True, "1") if field in ("cpus", "timeout_seconds", "control_timeout_seconds") else (True, "128", 128.5)
                for value in values:
                    data = deepcopy(self.data)
                    target = data["build"]["profile"] if block == "build" else data[block]
                    target["limits"][field] = value
                    with self.subTest(block=block, field=field, value=value):
                        self.assert_code("INVALID", self.decode, data)

    def test_resource_boundaries_and_nonfinite_numbers_remain_enforced(self):
        for field, value in (("cpus", 0), ("memory_bytes", 1), ("pids", 1), ("timeout_seconds", 0),
                             ("tmpfs_bytes", 0), ("max_stdout_bytes", 0), ("max_stderr_bytes", 0),
                             ("control_timeout_seconds", 0), ("cpus", float("nan"))):
            data = deepcopy(self.data)
            data["unit"]["limits"][field] = value
            with self.subTest(field=field, value=value):
                self.assert_code("INVALID", self.decode, data)

    def test_each_tool_must_fit_execution_and_owned_cleanup_in_mcp_timeout(self):
        self.assertEqual(self.decode({**self.data, "maxCallSeconds": 35}).max_call_seconds, 35)
        self.assert_code("INVALID", self.decode, {**self.data, "maxCallSeconds": 34.99})
        for block in ("build", "unit", "browser", "security"):
            data = deepcopy(self.data)
            target = data["build"]["profile"] if block == "build" else data[block]
            target["limits"]["timeout_seconds"] = 56
            with self.subTest(block=block):
                self.assert_code("INVALID", self.decode, data)

    def test_approved_timeouts_are_preserved_without_silent_clamping(self):
        config = self.decode()
        self.assertEqual(config.build_configuration.profile.limits.timeout_seconds, 30)
        for item in (config.unit_configuration_for(AgentRole.DEVELOPER), config.unit_configuration_for(AgentRole.QA),
                     config.browser_configuration, config.security_configuration):
            self.assertEqual(item.limits.timeout_seconds, 30)
            self.assertEqual(item.limits.control_timeout_seconds, 2)

    def test_build_commands_are_native_absolute_container_argv_not_arbitrary_shell(self):
        for argv in (["python", "-m", "compileall"], ["/bin/sh", "-c", "private-tool-secret"],
                     ["/usr/bin/env", "python"], ["/bin/bash"], ["/usr/local/bin/python", "--token=private-tool-secret"]):
            data = deepcopy(self.data)
            data["build"]["profile"]["argv"] = argv
            with self.subTest(argv=argv):
                self.assert_code("INVALID", self.decode, data)

    def test_python_executable_is_identical_for_all_three_runner_profiles(self):
        for block in ("unit", "browser", "security"):
            data = deepcopy(self.data)
            data[block]["python_executable"] = "/usr/local/bin/python3"
            with self.subTest(block=block):
                self.assert_code("INVALID", self.decode, data)

    def test_build_profile_optional_tool_name_cannot_change_the_approved_tool(self):
        data = deepcopy(self.data)
        data["build"]["profile"]["tool_name"] = "run_build"
        self.assertEqual(self.decode(data).build_configuration.profile.tool_name, "run_build")
        for tool_name in ("run_unit_tests", "run_security_scan", "run_browser_tests", "", True):
            data["build"]["profile"]["tool_name"] = tool_name
            with self.subTest(tool_name=tool_name):
                self.assert_code("INVALID", self.decode, data)

    def test_browser_service_commands_are_native_and_origin_stays_local(self):
        for argv in (["python", "/snapshot/service.py"], ["/bin/sh", "-c", "private-tool-secret"],
                     ["/usr/bin/env", "python"], ["/bin/bash"]):
            data = deepcopy(self.data)
            data["browser"]["service_argv"] = argv
            with self.subTest(argv=argv):
                self.assert_code("INVALID", self.decode, data)
        for origin in ("https://example.invalid", "http://localhost:8765", "http://127.0.0.1:80",
                       "http://127.0.0.1:8765/?token=private-tool-secret"):
            data = deepcopy(self.data)
            data["browser"]["base_url"] = origin
            with self.subTest(origin=origin):
                self.assert_code("INVALID", self.decode, data)

    def test_browser_approved_timeouts_are_explicit_strict_and_bounded(self):
        for field, values in (("startup_timeout_seconds", (True, "5", 0, 61, float("nan"))),
                              ("action_timeout_ms", (True, "1000", 1.5, 0, 30001))):
            for value in values:
                data = deepcopy(self.data)
                data["browser"][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_code("INVALID", self.decode, data)


class ToolConfigurationRoleTests(_ToolConfigurationFixture, unittest.TestCase):
    def test_role_projection_exposes_developer_snapshot_only_and_qa_tests_only(self):
        data = deepcopy(self.data)
        data["unit"]["scopes"].append(protected_unit())
        config = self.decode(data)
        developer = config.unit_configuration_for(AgentRole.DEVELOPER)
        qa = config.unit_configuration_for(AgentRole.QA)
        self.assertIs(type(developer), UnitTestConfiguration)
        self.assertIs(type(qa), UnitTestConfiguration)
        self.assertEqual(tuple(scope.kind for scope in developer.scopes), ("SNAPSHOT",))
        self.assertEqual(tuple(scope.kind for scope in qa.scopes), ("QA_TESTS", "PROTECTED"))
        self.assertTrue(all(scope.roles == (AgentRole.QA,) for scope in qa.scopes))

    def test_planner_security_and_string_role_cannot_request_unit_capability(self):
        config = self.decode()
        for role in (AgentRole.PLANNER, AgentRole.SECURITY, "DEVELOPER", "ORCHESTRATOR", None):
            with self.subTest(role=role):
                self.assert_code("INVALID", config.unit_configuration_for, role)

    def test_combined_unit_policy_requires_both_snapshot_and_qa_test_scopes(self):
        for scopes in ([], [{"name": "only-source", "kind": "SNAPSHOT"}],
                       [{"name": "only-qa", "kind": "QA_TESTS"}], [protected_unit()]):
            data = deepcopy(self.data)
            data["unit"]["scopes"] = scopes
            with self.subTest(scopes=scopes):
                self.assert_code("INVALID", self.decode, data)

    def test_browser_requires_qa_tests_even_when_protected_suite_exists(self):
        for suites in ([], [protected_browser()], [{"name": "invalid-source", "kind": "SNAPSHOT"}]):
            data = deepcopy(self.data)
            data["browser"]["suites"] = suites
            with self.subTest(suites=suites):
                self.assert_code("INVALID", self.decode, data)

    def test_optional_protected_policy_preserves_bytes_and_read_only_mapping(self):
        data = deepcopy(self.data)
        protected = protected_unit()
        data["unit"]["scopes"].append(protected)
        data["browser"]["suites"].append(protected_browser())
        config = self.decode(data)
        scope = config.unit_configuration_for(AgentRole.QA).scopes[-1]
        self.assertEqual(scope.protected_suite_ref, SUITE_REF)
        self.assertEqual(scope.protected_files, protected["protected_files"])
        with self.assertRaises(TypeError):
            scope.protected_files["tests/test_signup.py"] = "modified"
        for content in scope.protected_files.values():
            self.assertNotIn(content, repr(config))

    def test_protected_reference_cannot_be_credentialed_url_or_host_file(self):
        for ref in ("file:///tmp/protected", "https://user:private-tool-secret@example.invalid/suite",
                    SUITE_REF + "?token=private-tool-secret", SUITE_REF + "#fragment"):
            data = deepcopy(self.data)
            scope = protected_unit()
            scope["protected_suite_ref"] = ref
            data["unit"]["scopes"].append(scope)
            with self.subTest(ref=ref):
                self.assert_code("INVALID", self.decode, data)

    def test_duplicate_unit_browser_scanner_selector_names_are_rejected(self):
        for block, field in (("unit", "scopes"), ("browser", "suites"), ("security", "profiles")):
            data = deepcopy(self.data)
            data[block][field].append(deepcopy(data[block][field][0]))
            with self.subTest(block=block):
                self.assert_code("INVALID", self.decode, data)

    def test_all_security_profiles_use_one_approved_bandit_version(self):
        data = deepcopy(self.data)
        second = deepcopy(data["security"]["profiles"][0])
        second["name"] = "python-security-other"
        data["security"]["profiles"].append(second)
        config = self.decode(data)
        self.assertEqual(len(config.security_configuration.profiles), 2)
        data["security"]["profiles"][1]["scanner_version"] = "1.8.7"
        self.assert_code("INVALID", self.decode, data)

    def test_unknown_security_rule_and_duplicate_rule_ids_are_rejected(self):
        for rules in ([], ["B001"], ["invalid"], ["B101", "B101"]):
            data = deepcopy(self.data)
            data["security"]["profiles"][0]["rule_ids"] = rules
            with self.subTest(rules=rules):
                self.assert_code("INVALID", self.decode, data)


class ToolConfigurationIntegrityTests(_ToolConfigurationFixture, unittest.TestCase):
    def test_decode_and_property_access_are_pure_without_lock_docker_or_environment_lookup(self):
        with patch("os.open", side_effect=AssertionError("unexpected file access")), \
             patch("subprocess.run", side_effect=AssertionError("unexpected subprocess")), \
             patch.dict(os.environ, {"DOCKER_HOST": "tcp://private-tool-secret", "MCP_ROLE": "SECURITY"}, clear=True):
            config = self.decode()
            config.baseline
            config.build_configuration
            config.unit_configuration_for(AgentRole.QA)
            config.browser_configuration
            config.security_configuration
        self.assertFalse(self.lock_file.exists())

    def test_original_mutation_does_not_change_decoded_policy(self):
        data = deepcopy(self.data)
        data["unit"]["scopes"].append(protected_unit())
        config = self.decode(data)
        data["build"]["profile"]["argv"][:] = ["/bin/sh", "-c", "private-tool-secret"]
        data["unit"]["scopes"][-1]["protected_files"]["tests/test_signup.py"] = "changed"
        data["security"]["profiles"][0]["rule_ids"][:] = ["B001"]
        self.assertEqual(config.build_configuration.profile.argv[0], "/usr/local/bin/python")
        self.assertIn("approved independent fixture", config.unit_configuration_for(AgentRole.QA).scopes[-1].protected_files["tests/test_signup.py"])
        self.assertEqual(config.security_configuration.profiles[0].rule_ids, ("B101", "B307"))

    def test_each_property_returns_fresh_validated_objects_after_mutated_old_return(self):
        config = self.decode()
        build = config.build_configuration
        object.__setattr__(build.profile, "name", "tampered")
        self.assertEqual(config.build_configuration.profile.name, "fixture-build")
        unit = config.unit_configuration_for(AgentRole.DEVELOPER)
        object.__setattr__(unit, "python_executable", "/bin/sh")
        self.assertEqual(config.unit_configuration_for(AgentRole.DEVELOPER).python_executable, "/usr/local/bin/python")
        security = config.security_configuration
        object.__setattr__(security.profiles[0], "scanner_version", "1.9.0")
        self.assertEqual(config.security_configuration.profiles[0].scanner_version, "1.8.6")

    def test_owned_top_level_configuration_is_frozen(self):
        config = self.decode()
        with self.assertRaises((FrozenInstanceError, AttributeError, ValueError, TypeError)):
            config.max_call_seconds = 1

    def test_direct_dataclass_replace_cannot_restore_missing_resource_defaults(self):
        config = self.decode()
        for block in ("build", "unit", "browser", "security"):
            attribute = "_" + block + "_json"
            for field in limits():
                internal = json.loads(getattr(config, attribute))
                target = internal["profile"] if block == "build" else internal
                del target["limits"][field]
                with self.subTest(block=block, missing_limit=field):
                    self.assert_code("INVALID", replace, config, **{attribute: json.dumps(internal)})
            internal = json.loads(getattr(config, attribute))
            target = internal["profile"] if block == "build" else internal
            target["limits"] = {}
            with self.subTest(block=block, empty_limits=True):
                self.assert_code("INVALID", replace, config, **{attribute: json.dumps(internal)})

    def test_direct_dataclass_replace_cannot_restore_missing_canonical_fields(self):
        config = self.decode()
        for block in ("build", "unit", "browser", "security"):
            attribute = "_" + block + "_json"
            original = json.loads(getattr(config, attribute))
            for field in original:
                internal = deepcopy(original)
                del internal[field]
                with self.subTest(block=block, missing_field=field):
                    self.assert_code("INVALID", replace, config, **{attribute: json.dumps(internal)})
        original = json.loads(config._build_json)
        for field in original["profile"]:
            internal = deepcopy(original)
            del internal["profile"][field]
            with self.subTest(block="build.profile", missing_field=field):
                self.assert_code("INVALID", replace, config, _build_json=json.dumps(internal))

    def test_direct_dataclass_replace_preserves_valid_canonical_configuration(self):
        config = self.decode()
        copied = replace(config)
        self.assertEqual(copied.baseline, config.baseline)
        self.assertEqual(copied.build_configuration, config.build_configuration)
        self.assertEqual(copied.unit_configuration_for(AgentRole.QA), config.unit_configuration_for(AgentRole.QA))
        self.assertEqual(copied.browser_configuration, config.browser_configuration)
        self.assertEqual(copied.security_configuration, config.security_configuration)

    def test_malformed_duplicate_nonfinite_and_nonobject_json_are_safe(self):
        for text in ("{", "[]", "null", '{"schemaVersion":1,"schemaVersion":1}',
                     '{"private-tool-secret":NaN}', '{"private-tool-secret":Infinity}',
                     '{"private-tool-secret":1e9999}', " " * (9 * 1024 * 1024)):
            with self.subTest(prefix=text[:50]):
                self.assert_code("INVALID", decode_tool_configuration, text)

    def test_real_dependency_lock_hash_is_measured_on_load_not_trusted_from_json(self):
        config = load_tool_configuration(self.write())
        self.assertEqual(config.baseline.dependency_lock_hash, LOCK_HASH)
        self.lock_file.write_bytes(LOCK + b"changed-dependency==2\n")
        self.assert_code("LOCK_MISMATCH", load_tool_configuration, self.file)

    def test_missing_lock_file_does_not_fall_back_to_environment_or_working_directory(self):
        self.write(lock=False)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)

    def test_missing_nonregular_invalid_utf8_and_oversized_config_files_fail_safely(self):
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.base)
        self.file.write_bytes(b"\xff")
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)
        self.file.write_bytes(b" " * (9 * 1024 * 1024))
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)

    def test_json_contract_failure_in_regular_file_is_invalid_not_lock_mismatch(self):
        data = {**self.data, "privateSecret": "private-tool-secret"}
        self.assert_code("INVALID", load_tool_configuration, self.write(data))

    def test_malformed_json_in_regular_file_retains_schema_error_code(self):
        self.write()
        for text in ("{", "[]", '{"schemaVersion":1,"schemaVersion":1}', '{"privateSecret":NaN}'):
            self.file.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                self.assert_code("INVALID", load_tool_configuration, self.file)

    def test_empty_and_oversized_lock_files_fail_before_hash_comparison(self):
        self.write()
        for content in (b"", b"x" * (1_048_576 + 1)):
            self.lock_file.write_bytes(content)
            with self.subTest(size=len(content)):
                self.assert_code("FILE_INVALID", load_tool_configuration, self.file)

    def test_lock_file_directory_is_rejected_without_reading_or_hashing_it(self):
        self.write(lock=False)
        self.lock_file.mkdir(parents=True)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink API required")
    def test_config_and_lock_symlinks_are_rejected_without_following_them(self):
        self.write()
        alias = self.base / "tools-alias.json"
        alias.symlink_to(self.file)
        self.assert_code("FILE_INVALID", load_tool_configuration, alias)
        actual_lock = self.lock_file.parent / "real.lock"
        self.lock_file.rename(actual_lock)
        self.lock_file.symlink_to(actual_lock)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFO API required")
    def test_config_and_lock_named_pipes_are_rejected_without_blocking(self):
        os.mkfifo(self.file)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)
        self.file.unlink()
        self.write(lock=False)
        self.lock_file.parent.mkdir()
        os.mkfifo(self.lock_file)
        self.assert_code("FILE_INVALID", load_tool_configuration, self.file)


class ToolConfigurationCLITests(_ToolConfigurationFixture, unittest.TestCase):
    def invoke(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = check_tools(arguments)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_cli_validates_without_claiming_docker_or_execution_readiness(self):
        status, stdout, stderr = self.invoke(["--file", str(self.write())])
        self.assertEqual(status, 0)
        body = json.loads(stdout)
        self.assertEqual(body["status"], "TOOL_CONFIGURATION_VALID")
        self.assertIs(body["executionReady"], False)
        self.assertIs(body["DockerChecked"], False)
        self.assertEqual(stderr, "")
        self.assertNotIn(str(self.directory), stdout)

    def test_cli_failure_reports_only_approved_code_without_paths_source_or_secret(self):
        data = {**self.data, "privateSecret": "private-tool-secret"}
        status, stdout, stderr = self.invoke(["--file", str(self.write(data))])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "INVALID"))
        self.assertNotIn("private-tool-secret", stderr)
        self.assertNotIn(str(self.directory), stderr)

    def test_cli_lock_mismatch_is_distinguishable_but_never_echoes_bytes(self):
        self.write()
        self.lock_file.write_bytes(b"private-tool-secret=unapproved lock bytes\n")
        status, stdout, stderr = self.invoke(["--file", str(self.file)])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "LOCK_MISMATCH"))
        self.assertNotIn("private-tool-secret", stderr)

    def test_cli_missing_file_and_malformed_arguments_are_safe(self):
        status, stdout, stderr = self.invoke(["--file", str(self.file)])
        self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "FILE_INVALID"))
        for arguments in ([], ["--file"], ["--token", "private-tool-secret"], ["--file", str(self.file), "extra"]):
            with self.subTest(arguments=arguments):
                status, stdout, stderr = self.invoke(arguments)
                self.assertEqual((status, stdout, stderr.strip()), (1, "", PREFIX + "INVALID"))

    def test_real_cli_resolves_lock_from_configuration_directory_independent_of_cwd(self):
        file = self.write()
        source = Path(__file__).resolve().parents[1] / "src"
        env = {**os.environ, "PYTHONPATH": str(source), "DOCKER_HOST": "tcp://unapproved.example.invalid"}
        result = subprocess.run(
            [sys.executable, "-m", "agents.platform.check_tools", "--file", str(file)],
            cwd=self.directory, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["DockerChecked"], False)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
