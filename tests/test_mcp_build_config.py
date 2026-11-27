"""Pure Host build configuration checks; no Docker or generated-code execution."""

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from mcp_tools.tools.build_config import (
    MAX_BUILD_CONFIGURATION_BYTES, BuildConfiguration, BuildConfigurationError,
    decode_build_configuration, encode_build_configuration,
)
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits


IMAGE = "sha256:" + "a" * 64


def profile(**changes):
    args = dict(name="host-build", tool_name="run_build", argv=("/usr/bin/npm", "run", "build"))
    args.update(changes)
    return ExecutionProfile(**args)


def document(**changes):
    data = {"profile": {"name": "host-build", "argv": ["/usr/bin/npm", "run", "build"]}}
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


class BuildConfigurationTests(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(BuildConfigurationError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "BUILD_CONFIGURATION_INVALID")
        self.assertEqual(str(caught.exception), "BUILD_CONFIGURATION_INVALID")
        self.assertEqual(caught.exception.args, ("BUILD_CONFIGURATION_INVALID",))
        self.assertIsNone(caught.exception.__cause__)
        return caught.exception

    def test_defaults_are_limits_and_local_endpoint_not_a_command(self):
        configuration = BuildConfiguration(profile=profile())
        self.assertEqual(configuration.docker_endpoint, "unix:///var/run/docker.sock")
        self.assertEqual(configuration.profile.argv, ("/usr/bin/npm", "run", "build"))
        self.assertEqual(configuration.profile.limits, SandboxLimits())
        with self.assertRaises(TypeError):
            BuildConfiguration()
        with self.assertRaises(TypeError):
            BuildConfiguration(profile())

    def test_immutable_and_repr_hides_argv_endpoint_and_image(self):
        configuration = BuildConfiguration(
            profile=profile(argv=("/usr/bin/python3", "-m", "compileall", "private-directory"), image_reference=IMAGE),
            docker_endpoint="unix:///private/operator/docker.sock",
        )
        self.assertEqual(repr(configuration), "BuildConfiguration()")
        with self.assertRaises(FrozenInstanceError):
            configuration.docker_endpoint = "unix:///bad.sock"

    def test_full_round_trip_and_canonical_serialization(self):
        configuration = BuildConfiguration(
            profile=profile(limits=SandboxLimits(cpus=2.0, pids=256, timeout_seconds=90), image_reference=IMAGE),
            docker_endpoint="unix:///private/docker.sock",
        )
        encoded = encode_build_configuration(configuration)
        self.assertEqual(decode_build_configuration(encoded), configuration)
        self.assertEqual(encode_build_configuration(decode_build_configuration(encoded)), encoded)
        data = json.loads(encoded)
        self.assertEqual(set(data), {"profile", "docker_endpoint"})
        self.assertEqual(set(data["profile"]), {"name", "tool_name", "argv", "limits", "image_reference"})
        self.assertEqual(len(data["profile"]["limits"]), 8)

    def test_minimal_document_and_partial_limits_are_supported(self):
        configuration = decode_build_configuration(document())
        self.assertEqual(configuration.profile.tool_name, "run_build")
        self.assertEqual(configuration.profile.limits, SandboxLimits())
        data = json.loads(document())
        data["profile"]["limits"] = {"timeout_seconds": 30}
        self.assertEqual(decode_build_configuration(json.dumps(data)).profile.limits.timeout_seconds, 30)

    def test_unicode_argv_preserved(self):
        configuration = BuildConfiguration(profile=profile(argv=("/usr/bin/python3", "-m", "compileall", "회원가입")))
        encoded = encode_build_configuration(configuration)
        self.assertIn("회원가입", encoded)
        self.assertEqual(decode_build_configuration(encoded), configuration)

    def test_valid_empty_argument_is_preserved(self):
        configuration = BuildConfiguration(profile=profile(argv=("/usr/bin/python3", "-m", "compileall", "")))
        self.assertEqual(decode_build_configuration(encode_build_configuration(configuration)), configuration)

    def test_invalid_top_level_and_missing_fields(self):
        for value in (None, "", "null", "[]", "1", "true", "{}", '{"profile":null}', '{"profile":{}}',
                      '{"profile":{"name":"build"}}', '{"profile":{"argv":["/usr/bin/npm"]}}'):
            with self.subTest(value=value):
                self.assert_invalid(lambda: decode_build_configuration(value))

    def test_unknown_top_level_fields_cannot_select_execution_behavior(self):
        for key in ("env", "stdin", "shell", "command", "cwd", "run_id", "workspace_id", "source_path"):
            with self.subTest(key=key):
                self.assert_invalid(lambda: decode_build_configuration(document(**{key: "private-value"})))

    def test_unknown_profile_fields_are_rejected(self):
        for key in ("env", "stdin", "shell", "command", "cwd", "mounts", "network"):
            with self.subTest(key=key):
                data = json.loads(document())
                data["profile"][key] = "private-value"
                self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_unknown_limit_fields_rejected(self):
        data = json.loads(document())
        data["profile"]["limits"] = {"cpus": 1, "privileged": True}
        self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_duplicate_keys_rejected_at_each_level(self):
        for text in (
            '{"profile":{"name":"build","argv":["/usr/bin/npm"]},"profile":{"name":"other","argv":["/bin/sh"]}}',
            '{"profile":{"name":"build","name":"other","argv":["/usr/bin/npm"]}}',
            '{"profile":{"name":"build","argv":["/usr/bin/npm"],"limits":{"cpus":1,"cpus":2}}}',
            '{"profile":{"name":"build","argv":["/usr/bin/npm"]},"docker_endpoint":"unix:///a.sock","docker_endpoint":"unix:///b.sock"}',
        ):
            self.assert_invalid(lambda: decode_build_configuration(text))

    def test_nonfinite_json_and_float_overflow_rejected(self):
        for constant in ("NaN", "Infinity", "-Infinity", "1e10000"):
            text = '{"profile":{"name":"build","argv":["/usr/bin/npm"],"limits":{"cpus":' + constant + '}}}'
            self.assert_invalid(lambda: decode_build_configuration(text))

    def test_invalid_json_is_code_only(self):
        secret = 'password=fixture-private-secret'
        self.assert_invalid(lambda: decode_build_configuration('{"private":' + secret))

    def test_invalid_types_for_every_profile_field(self):
        for key, value in (("name", None), ("name", True), ("argv", "npm"), ("argv", None),
                           ("argv", [1]), ("tool_name", True), ("limits", None),
                           ("limits", []), ("image_reference", 7)):
            with self.subTest(key=key, value=value):
                data = json.loads(document())
                data["profile"][key] = value
                self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_only_run_build_tool_allowed(self):
        for tool in ("run_unit_tests", "run_browser_tests", "run_security_scan", "shell", "RUN_BUILD", ""):
            with self.subTest(tool=tool):
                data = json.loads(document())
                data["profile"]["tool_name"] = tool
                self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))
        self.assert_invalid(lambda: BuildConfiguration(profile=profile(tool_name="run_unit_tests")))

    def test_known_shell_and_environment_dispatchers_rejected(self):
        for executable in ("/bin/sh", "/bin/bash", "/bin/dash", "/usr/bin/env", "/bin/busybox", "/usr/bin/pwsh"):
            with self.subTest(executable=executable):
                self.assert_invalid(lambda: BuildConfiguration(profile=profile(argv=(executable, "-c", "build"))))

    def test_container_language_and_package_commands_are_permitted(self):
        for argv in (("/usr/bin/npm", "run", "build"), ("/usr/bin/python3", "-m", "compileall", "source"),
                     ("/usr/bin/node", "build.js"), ("/usr/bin/python3", "-c", "print(1)")):
            with self.subTest(argv=argv):
                self.assertEqual(BuildConfiguration(profile=profile(argv=argv)).profile.argv, argv)

    def test_executable_path_canonical_bounds(self):
        for executable in ("npm", "/", "//usr/bin/npm", "/usr/./bin/npm", "/usr/bin/../npm", "/usr/bin/npm/",
                           "/usr\\bin/npm", "/usr/%62in/npm", "/" + "한" * 90):
            with self.subTest(executable=executable):
                data = json.loads(document())
                data["profile"]["argv"] = [executable]
                self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_control_characters_and_surrogates_rejected(self):
        for char in ("\x00", "\n", "\t", "\x7f", "\x85", "\ud800", "\udfff"):
            data = json.loads(document())
            data["profile"]["argv"].append("argument" + char)
            self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_argument_count_and_byte_bounds(self):
        for argv in ([], ["/usr/bin/npm"] + ["a"] * 64, ["/usr/bin/npm", "x" * 8193],
                     ["/usr/bin/npm", "한" * 2731]):
            data = json.loads(document())
            data["profile"]["argv"] = argv
            self.assert_invalid(lambda: decode_build_configuration(json.dumps(data, ensure_ascii=False)))

    def test_total_configuration_byte_bound(self):
        configuration = BuildConfiguration(profile=profile(argv=("/usr/bin/npm", "x" * 7000, "y" * 7000)))
        self.assertLess(len(encode_build_configuration(configuration).encode()), MAX_BUILD_CONFIGURATION_BYTES)
        self.assert_invalid(lambda: BuildConfiguration(profile=profile(argv=("/usr/bin/npm", "x" * 8192, "y" * 8192))))
        self.assert_invalid(lambda: decode_build_configuration(document() + " " * MAX_BUILD_CONFIGURATION_BYTES))
        self.assert_invalid(lambda: decode_build_configuration("한" * 5500))

    def test_recognizable_credentials_are_denied_not_redacted(self):
        for argument in ("password=fixture-secret", "API_KEY=fixture-secret", "Bearer fixture-token",
                         "--password", "--access-token=fixture-token", "--api-key", "--authorization"):
            with self.subTest(argument=argument):
                self.assert_invalid(lambda: BuildConfiguration(profile=profile(argv=("/usr/bin/npm", argument, "fixture-secret"))))

    def test_image_must_be_pinned_digest_when_explicit(self):
        for image in ("node:latest", "sha256:" + "a" * 63, "sha256:" + "A" * 64, "https://host/image", "password=fixture-secret"):
            data = json.loads(document())
            data["profile"]["image_reference"] = image
            self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))
        self.assertEqual(BuildConfiguration(profile=profile(image_reference=IMAGE)).profile.image_reference, IMAGE)

    def test_only_canonical_local_unix_endpoint_accepted(self):
        for endpoint in ("tcp://127.0.0.1:2375", "ssh://operator@host", "http://localhost/docker", "npipe:////./pipe/docker_engine",
                         "unix://host/var/run/docker.sock", "unix:/var/run/docker.sock", "unix:///var/../run/docker.sock",
                         "unix:///var/./run/docker.sock", "unix:////var/run/docker.sock", "unix:///var/run/docker.sock/",
                         "unix:///var/run/docker.sock?x=1", "unix:///var/run/docker.sock#x", "unix:///var/%72un/docker.sock",
                         "unix:///var\\run/docker.sock", "unix:///", "unix:///var/\x00docker.sock", None, 7,
                         "unix:///" + "x" * 1024):
            with self.subTest(endpoint=endpoint):
                self.assert_invalid(lambda: BuildConfiguration(profile=profile(), docker_endpoint=endpoint))

    def test_resource_types_and_bounds_reuse_sandbox_limits(self):
        for key, value in (("cpus", True), ("cpus", 0.09), ("cpus", 4.1), ("cpus", "1"),
                           ("memory_bytes", 1), ("memory_bytes", 5 * 1024 ** 3), ("pids", 1025),
                           ("pids", 1.0), ("timeout_seconds", 601), ("timeout_seconds", 0),
                           ("tmpfs_bytes", 0), ("max_stdout_bytes", 0), ("max_stderr_bytes", 4 * 1024 ** 2 + 1),
                           ("control_timeout_seconds", 31)):
            with self.subTest(key=key, value=value):
                data = json.loads(document())
                data["profile"]["limits"] = {key: value}
                self.assert_invalid(lambda: decode_build_configuration(json.dumps(data)))

    def test_constructor_copies_profile_and_limits(self):
        selected = profile()
        configuration = BuildConfiguration(profile=selected)
        self.assertEqual(configuration.profile, selected)
        self.assertIsNot(configuration.profile, selected)
        self.assertIsNot(configuration.profile.limits, selected.limits)
        object.__setattr__(selected, "argv", ("/bin/sh",))
        self.assertEqual(configuration.profile.argv[0], "/usr/bin/npm")

    def test_forged_or_mutated_profile_rechecked(self):
        self.assert_invalid(lambda: BuildConfiguration(profile=None))
        selected = profile()
        object.__setattr__(selected.limits, "pids", True)
        self.assert_invalid(lambda: BuildConfiguration(profile=selected))
        selected = profile()
        object.__setattr__(selected, "argv", ("/usr/bin/npm", "\ud800"))
        self.assert_invalid(lambda: BuildConfiguration(profile=selected))
        configuration = BuildConfiguration(profile=profile())
        object.__setattr__(configuration, "docker_endpoint", "tcp://private-host:2375")
        self.assert_invalid(lambda: encode_build_configuration(configuration))
        self.assert_invalid(lambda: encode_build_configuration(None))

    def test_constructor_encode_decode_have_no_environment_filesystem_or_process_side_effects(self):
        configuration_json = document()
        with patch("builtins.open", side_effect=AssertionError("no files")), \
             patch("os.stat", side_effect=AssertionError("no files")), \
             patch("os.getenv", side_effect=AssertionError("no environment")), \
             patch("subprocess.Popen", side_effect=AssertionError("no child process")):
            configuration = BuildConfiguration(profile=profile())
            self.assertEqual(decode_build_configuration(encode_build_configuration(configuration)), configuration)
            self.assertEqual(decode_build_configuration(configuration_json), configuration)


if __name__ == "__main__":
    unittest.main()
