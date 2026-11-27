"""Inert Host-only security configuration and standalone policy validation."""

from dataclasses import FrozenInstanceError
import json
import unittest
from unittest.mock import patch

from mcp_tools.tools.security_config import (
    MAX_SECURITY_CONFIGURATION_BYTES, SecurityConfigurationError, SecurityScanConfiguration,
    SecurityScannerProfile, decode_security_configuration, encode_security_configuration, security_host_payload,
)
from mcp_tools.tools.security_contract import (
    SecurityContractError, _name, _profile_reference, _rule, _source_path, _version,
    canonical_json, parse_json, validate_security_host_payload,
)
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import SandboxLimits


def profile(**changes):
    values = {"name": "python-static", "scanner_version": "1.8.6", "rule_ids": ("B307", "B101"),
        "profile_ref": "artifact://profiles/python-static/v1"}
    values.update(changes)
    return SecurityScannerProfile(**values)


def configuration(**changes):
    values = {"profiles": (profile(),)}
    values.update(changes)
    return SecurityScanConfiguration(**values)


def document(**changes):
    values = {"profiles": [{"name": "python-static", "scanner_version": "1.8.6", "rule_ids": ["B307", "B101"],
        "profile_ref": "artifact://profiles/python-static/v1"}]}
    values.update(changes)
    return json.dumps(values, ensure_ascii=False)


class SecurityConfigurationTests(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(SecurityConfigurationError) as caught:
            operation()
        self.assertEqual(caught.exception.args, ("SECURITY_CONFIGURATION_INVALID",))
        self.assertEqual(caught.exception.code, "SECURITY_CONFIGURATION_INVALID")
        self.assertIsNone(caught.exception.__cause__)

    def test_required_host_profile_has_no_fake_defaults(self):
        with self.assertRaises(TypeError):
            SecurityScanConfiguration()
        for kwargs in ({}, {"name": "static"}, {"name": "static", "scanner_version": "1.8.6"}):
            with self.assertRaises(TypeError):
                SecurityScannerProfile(**kwargs)
        for values in ((), [], None, (profile(),) * 33):
            self.assert_invalid(lambda: configuration(profiles=values))

    def test_only_security_role_can_use_profiles(self):
        self.assertEqual(profile().roles, (AgentRole.SECURITY,))

    def test_profile_names_are_closed_safe_and_bounded(self):
        for name in ("", "Python", "../static", "static normal", "a" * 65, None, 5):
            self.assert_invalid(lambda: profile(name=name))

    def test_versions_are_exact_bandit_one_x_x_not_ranges_or_aliases(self):
        for version in ("latest", "1.8", "^1.8.6", "1.8.6rc1", "2.0.0", "1.08.6", "1.8.06", "1.8.6\n", None):
            self.assert_invalid(lambda: profile(scanner_version=version))
        self.assertEqual(profile(scanner_version="1.0.0").scanner_version, "1.0.0")

    def test_rules_are_explicit_nonempty_unique_tuple(self):
        for rules in ((), [], None, ("B101", "B101"), ("B101",) * 129):
            self.assert_invalid(lambda: profile(rule_ids=rules))
        self.assertEqual(profile().rule_ids, ("B101", "B307"))

    def test_rule_names_are_concrete_no_nosec_or_wildcards(self):
        for rule in ("B001", "B*", "b101", "B10", "B1000", "B101\n", "../B101", "", 5):
            self.assert_invalid(lambda: profile(rule_ids=(rule,)))

    def test_syntax_valid_unknown_rule_is_not_silently_replaced(self):
        # Installed plugin membership is checked by the Container Runner.
        self.assertEqual(profile(rule_ids=("B999",)).rule_ids, ("B999",))

    def test_logical_reference_required_no_fetch_host_path_or_credential(self):
        for ref in (None, "", "/private/profile.json", "file:///private/profile", "http://example.org/profile",
                    "https://u:private@example.org/profile", "artifact://p/../profile", "artifact://p/profile?token=private",
                    "artifact://p/profile#x", "artifact://p/%70rofile", "artifact://p/.env", "artifact://p/pr ofile"):
            self.assert_invalid(lambda: profile(profile_ref=ref))
        for ref in ("artifact://profiles/python/v1", "https://criteria.example.invalid/scanners/python/v1"):
            self.assertEqual(profile(profile_ref=ref).profile_ref, ref)

    def test_credential_like_reference_denied_by_host_redaction(self):
        self.assert_invalid(lambda: profile(profile_ref="artifact://profiles/token=private"))

    def test_duplicate_profile_names_are_not_ambiguous(self):
        self.assert_invalid(lambda: configuration(profiles=(profile(), profile(rule_ids=("B101",)))))

    def test_profile_frozen_and_sensitive_policy_repr_hidden(self):
        selected = profile()
        self.assertNotIn("artifact://", repr(selected))
        self.assertNotIn("B101", repr(selected))
        self.assertNotIn("1.8.6", repr(selected))
        with self.assertRaises(FrozenInstanceError):
            selected.rule_ids = ("B307",)

    def test_configuration_frozen_and_repr_empty(self):
        selected = configuration(docker_endpoint="unix:///private/docker.sock")
        self.assertEqual(repr(selected), "SecurityScanConfiguration()")
        with self.assertRaises(FrozenInstanceError):
            selected.python_executable = "/bin/sh"

    def test_minimal_and_full_round_trip(self):
        minimum = decode_security_configuration(document())
        self.assertEqual(minimum.profiles[0].rule_ids, ("B101", "B307"))
        self.assertEqual(minimum.python_executable, "/usr/local/bin/python")
        chosen = configuration(profiles=(profile(), profile(name="another", rule_ids=("B602",))),
            limits=SandboxLimits(timeout_seconds=90), image_reference="sha256:" + "a" * 64,
            docker_endpoint="unix:///private/docker.sock")
        encoded = encode_security_configuration(chosen)
        self.assertEqual(decode_security_configuration(encoded), chosen)
        self.assertEqual(encode_security_configuration(decode_security_configuration(encoded)), encoded)

    def test_decode_preserves_rules_but_canonicalizes_order(self):
        decoded = decode_security_configuration(document())
        payload = json.loads(encode_security_configuration(decoded))
        self.assertEqual(payload["profiles"][0]["rule_ids"], ["B101", "B307"])

    def test_shell_dispatcher_and_host_executable_path_bans(self):
        for executable in ("python", "/bin/sh", "/bin/bash", "/usr/bin/env", "/bin/busybox", "/", "/usr/../bin/python", "/usr/%70ython", None):
            self.assert_invalid(lambda: configuration(python_executable=executable))

    def test_docker_must_be_local_unix_socket(self):
        for endpoint in ("tcp://127.0.0.1:2375", "ssh://example.org", "unix://host/socket", "unix:///tmp/../socket",
                         "unix:///docker.sock?x=1", "unix:///docker.sock#x", None):
            self.assert_invalid(lambda: configuration(docker_endpoint=endpoint))

    def test_frozen_image_digest_only_no_floating_tag(self):
        for image in ("python:latest", "sha256:nothex", "repository@sha256:" + "f" * 63):
            self.assert_invalid(lambda: configuration(image_reference=image))
        self.assertEqual(configuration(image_reference="repository@sha256:" + "f" * 64).image_reference,
            "repository@sha256:" + "f" * 64)

    def test_unknown_config_fields_do_not_add_scan_options(self):
        for key in ("argv", "shell", "env", "mounts", "network", "cwd", "scope", "skip", "threshold", "baseline", "nosec"):
            self.assert_invalid(lambda: decode_security_configuration(document(**{key: "untrusted"})))

    def test_profile_fields_are_exact_and_cannot_relax_detection(self):
        original = json.loads(document())["profiles"][0]
        for key in ("ignore_nosec", "scan_scope", "skip", "threshold", "baseline", "argv"):
            self.assert_invalid(lambda: decode_security_configuration(document(profiles=[{**original, key: "untrusted"}])))
        for key in original:
            self.assert_invalid(lambda: decode_security_configuration(document(profiles=[{name: value for name, value in original.items() if name != key}])))

    def test_json_types_empty_and_malformed_input_denied(self):
        for text in (None, "", "null", "[]", "true", "{}", "password=private"):
            self.assert_invalid(lambda: decode_security_configuration(text))
        for profiles in (None, {}, [], [5], [{"name": "static"}]):
            self.assert_invalid(lambda: decode_security_configuration(document(profiles=profiles)))

    def test_duplicate_json_keys_at_all_levels_denied(self):
        for text in ('{"profiles":[],"profiles":[]}',
            '{"profiles":[{"name":"x","name":"y","scanner_version":"1.8.6","rule_ids":["B101"],"profile_ref":"artifact://p/v1"}]}',
            document()[:-1] + ',"limits":{"pids":64,"pids":65}}'):
            self.assert_invalid(lambda: decode_security_configuration(text))

    def test_limits_closed_finite_nonboolean(self):
        for value in (None, [], {"network": True}, {"pids": True}, {"timeout_seconds": "60"}):
            self.assert_invalid(lambda: decode_security_configuration(document(limits=value)))
        for number in ("NaN", "Infinity", "-Infinity", "1e10000"):
            self.assert_invalid(lambda: decode_security_configuration(document()[:-1] + ',"limits":{"cpus":' + number + '}}'))

    def test_configuration_total_json_size_bounded(self):
        self.assert_invalid(lambda: decode_security_configuration(" " * (MAX_SECURITY_CONFIGURATION_BYTES + 1)))
        profiles = tuple(profile(name=f"profile-{index}", profile_ref="artifact://profiles/" + "x" * 4000) for index in range(32))
        self.assert_invalid(lambda: configuration(profiles=profiles))

    def test_construction_encode_decode_have_no_side_effects(self):
        with patch("os.open", side_effect=AssertionError("no filesystem")), patch("subprocess.Popen", side_effect=AssertionError("no processes")):
            chosen = configuration()
            self.assertEqual(decode_security_configuration(encode_security_configuration(chosen)), chosen)

    def test_forged_configuration_rechecked_when_encoding(self):
        chosen = configuration()
        object.__setattr__(chosen.profiles[0], "rule_ids", ("B001",))
        self.assert_invalid(lambda: encode_security_configuration(chosen))
        self.assert_invalid(lambda: encode_security_configuration({}))

    def test_host_payload_fixed_seven_fields_no_runtime_paths(self):
        chosen = configuration()
        payload = security_host_payload(chosen, chosen.profiles[0])
        self.assertEqual(set(payload), {"scanner", "profile_name", "scanner_version", "rule_ids", "profile_ref", "ignore_nosec", "scan_scope"})
        self.assertEqual(payload["ignore_nosec"], True)
        self.assertEqual(payload["scan_scope"], "ALL_PYTHON")
        self.assertEqual(payload["scanner"], "bandit")
        self.assertEqual(validate_security_host_payload(payload), payload)
        payload["rule_ids"].append("B602")
        self.assertEqual(chosen.profiles[0].rule_ids, ("B101", "B307"))

    def test_host_payload_cannot_select_unapproved_profile(self):
        chosen = configuration()
        self.assert_invalid(lambda: security_host_payload(chosen, profile(name="other")))


class SecurityStandaloneContractTests(unittest.TestCase):
    def assert_invalid(self, operation):
        with self.assertRaises(SecurityContractError) as caught:
            operation()
        self.assertEqual(caught.exception.args, ("SCANNER_ERROR",))
        self.assertIsNone(caught.exception.__cause__)

    def test_shared_metadata_validators(self):
        self.assertEqual(_name("python-static"), "python-static")
        self.assertEqual(_version("1.8.6"), "1.8.6")
        self.assertEqual(_rule("B101"), "B101")
        self.assert_invalid(lambda: _rule("B001"))

    def test_source_path_is_relative_lexical_not_a_host_path(self):
        for path in ("/snapshot/main.py", "../main.py", "package/../main.py", "package//main.py", "package/", "C:/main.py",
                     "package\\main.py", "https://host/main.py", "file:///main.py", "\nmain.py", "", None):
            self.assert_invalid(lambda: _source_path(path))
        for path in ("main.py", "src/가입.py", "tests/test_normal.py"):
            self.assertEqual(_source_path(path), path)

    def test_source_secret_private_staging_paths_are_denied(self):
        for path in (".git/main.py", ".env/main.py", "src/.SSH/main.py", "src/credentials.json", "keys/private.key",
                     ".mcp-write-staged/main.py", "src/id_rsa/main.py"):
            self.assert_invalid(lambda: _source_path(path))

    def test_source_path_byte_depth_and_control_bounds(self):
        for path in ("x" * 4097, "/".join(["x"] * 129), "src/\ud800.py", "src/\x80.py"):
            self.assert_invalid(lambda: _source_path(path))

    def test_source_nfkc_cannot_disguise_traversal_separator_or_secret(self):
        for path in (".ｅｎｖ/main.py", "src/．．/evil.py", "src／evil.py", "src＼evil.py", "src/．ｇｉｔ/main.py",
                     "src/private．ｋｅｙ", "src：main.py", ".ｍｃｐ-write-private/main.py"):
            self.assert_invalid(lambda: _source_path(path))

    def test_source_nfkc_allowed_names_keep_original_spelling(self):
        for path in ("Ａ/main.py", "src/가입.py", "Ａ/ｓｉｇｎｕｐ.py"):
            self.assertEqual(_source_path(path), path)

    def test_profile_reference_no_remote_fetch_or_secret_channel(self):
        self.assertEqual(_profile_reference("artifact://profiles/python/v1"), "artifact://profiles/python/v1")
        self.assert_invalid(lambda: _profile_reference("https://u:private@example.org/profile"))

    def test_json_encoding_canonical_finite_native_copy(self):
        value = {"z": [1, None, False], "a": "한글"}
        encoded = canonical_json(value)
        self.assertEqual(encoded, '{"a":"한글","z":[1,null,false]}')
        self.assertEqual(parse_json(encoded), value)

    def test_json_scalar_parse_is_allowed_but_host_requires_object(self):
        self.assertIsNone(parse_json("null"))
        self.assertEqual(parse_json("1"), 1)
        self.assert_invalid(lambda: validate_security_host_payload(None))

    def test_json_unknown_types_nonfinite_and_nonstring_keys_denied(self):
        for value in ((1, 2), b"bytes", {1: "number key"}, float("nan"), float("inf"), {"x": object()}):
            self.assert_invalid(lambda: canonical_json(value))
        for text in ("NaN", "Infinity", "-Infinity", "1e10000", '{"x":1,"x":2}', '"\\ud800"'):
            self.assert_invalid(lambda: parse_json(text))

    def test_json_byte_depth_nodes_and_limit_parameters_are_bounded(self):
        self.assert_invalid(lambda: parse_json(" " * (1024 * 1024 + 1)))
        self.assert_invalid(lambda: canonical_json([None] * 65536))
        value = None
        for _ in range(65):
            value = [value]
        self.assert_invalid(lambda: canonical_json(value))
        for limit in (0, -1, True, 1024 * 1024 + 1):
            self.assert_invalid(lambda: canonical_json({}, max_bytes=limit))
            self.assert_invalid(lambda: parse_json("{}", max_bytes=limit))

    def test_host_payload_is_closed_and_policy_cannot_be_weakened(self):
        chosen = configuration()
        original = security_host_payload(chosen, chosen.profiles[0])
        for key, value in (("scanner", "semgrep"), ("ignore_nosec", False), ("ignore_nosec", 1),
                           ("scan_scope", "ONLY_PACKAGE"), ("rule_ids", []), ("rule_ids", ["B101", "B101"]),
                           ("rule_ids", ["B001"]), ("scanner_version", "latest")):
            self.assert_invalid(lambda: validate_security_host_payload({**original, key: value}))
        self.assert_invalid(lambda: validate_security_host_payload({**original, "skip": ["src/main.py"]}))

    def test_host_rules_sorted_and_returned_in_new_list(self):
        chosen = configuration()
        host = security_host_payload(chosen, chosen.profiles[0])
        host["rule_ids"] = ["B307", "B101"]
        checked = validate_security_host_payload(host)
        self.assertEqual(checked["rule_ids"], ["B101", "B307"])
        checked["rule_ids"].append("B602")
        self.assertEqual(host["rule_ids"], ["B307", "B101"])
