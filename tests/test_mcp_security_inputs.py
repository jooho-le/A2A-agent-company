"""Pure security asset assembly tests; never scan/import/execute Source."""

from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from mcp_tools.tools.security_config import SecurityScanConfiguration, SecurityScannerProfile
from mcp_tools.tools.security_inputs import SecurityInputsError, SecurityScanInputs, prepare_security_inputs
from mcp_tools.tools.unit_inputs import _files_hash


RUNNER = b"# trusted static scanner harness\n"
CONTRACT = b"# trusted standalone contract\n"


def profile(**changes):
    values = {"name": "python-static", "scanner_version": "1.8.6", "rule_ids": ("B307", "B101"),
        "profile_ref": "artifact://profiles/python-static/v1"}
    values.update(changes)
    return SecurityScannerProfile(**values)


def configuration(selected=None):
    return SecurityScanConfiguration(profiles=(selected or profile(),))


def digest(value):
    return sha256(value).hexdigest()


class SecurityInputsTests(unittest.TestCase):
    def prepare(self, *, chosen=None, policy=None, runner=RUNNER, contract=CONTRACT):
        selected = chosen or profile()
        return prepare_security_inputs(policy or configuration(selected), selected, runner, contract)

    def rebuild(self, files, **changes):
        values = {"files": files, "inputs_sha256": _files_hash(files),
            "runner_sha256": digest(files["_security_runner.py"]), "contract_sha256": digest(files["_security_contract.py"]),
            "host_configuration_sha256": digest(files["_security_host.json"])}
        values.update(changes)
        return SecurityScanInputs(**values)

    def assert_error(self, code, operation):
        with self.assertRaises(SecurityInputsError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.args, (code,))
        self.assertIsNone(caught.exception.__cause__)

    def test_exactly_three_trusted_assets_no_source_or_model_files(self):
        inputs = self.prepare()
        self.assertEqual(set(inputs.files), {"_security_runner.py", "_security_contract.py", "_security_host.json"})
        self.assertEqual(inputs.files["_security_runner.py"], RUNNER)
        self.assertEqual(inputs.files["_security_contract.py"], CONTRACT)

    def test_assets_are_copied_frozen_and_not_exposed_in_repr(self):
        inputs = self.prepare()
        files = dict(inputs.files)
        copied = self.rebuild(files)
        files["_security_runner.py"] = b"changed"
        self.assertEqual(copied.files["_security_runner.py"], RUNNER)
        with self.assertRaises(TypeError):
            copied.files["_security_runner.py"] = b"changed"
        with self.assertRaises(FrozenInstanceError):
            copied.runner_sha256 = "0" * 64
        self.assertNotIn("trusted static scanner", repr(copied))

    def test_runner_contract_and_host_plain_hashes_input_manifest_hash(self):
        inputs = self.prepare()
        self.assertEqual(inputs.inputs_sha256, _files_hash(inputs.files))
        self.assertEqual(inputs.runner_sha256, digest(RUNNER))
        self.assertEqual(inputs.contract_sha256, digest(CONTRACT))
        self.assertEqual(inputs.host_configuration_sha256, digest(inputs.files["_security_host.json"]))
        self.assertEqual(inputs, self.prepare())

    def test_host_bytes_canonical_and_policy_keys_exact(self):
        inputs = self.prepare()
        raw = inputs.files["_security_host.json"]
        host = json.loads(raw)
        self.assertEqual(raw, json.dumps(host, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode())
        self.assertEqual(host["rule_ids"], ["B101", "B307"])
        self.assertTrue(host["ignore_nosec"])
        self.assertEqual(host["scan_scope"], "ALL_PYTHON")
        self.assertEqual(set(host), {"scanner", "profile_name", "scanner_version", "rule_ids", "profile_ref", "ignore_nosec", "scan_scope"})

    def test_assembly_is_pure_without_filesystem_subprocess_or_bandit_import(self):
        import builtins
        original_import = builtins.__import__
        def guarded_import(name, *args, **kwargs):
            if name == "bandit" or name.startswith("bandit."):
                raise AssertionError("no Host scanner import")
            return original_import(name, *args, **kwargs)
        with (patch("os.open", side_effect=AssertionError("no filesystem")),
              patch("subprocess.Popen", side_effect=AssertionError("no process")),
              patch("builtins.__import__", side_effect=guarded_import)):
            self.prepare()

    def test_unapproved_profile_cannot_be_prepared(self):
        self.assert_error("SCANNER_ERROR", lambda: self.prepare(policy=configuration(profile(name="other"))))

    def test_bad_configuration_or_profile_type_denied(self):
        self.assert_error("SCANNER_ERROR", lambda: prepare_security_inputs({}, profile(), RUNNER, CONTRACT))
        self.assert_error("SCANNER_ERROR", lambda: prepare_security_inputs(configuration(), {}, RUNNER, CONTRACT))

    def test_forged_profile_and_configuration_rechecked(self):
        chosen = profile()
        selected = configuration(chosen)
        object.__setattr__(chosen, "rule_ids", ("B001",))
        self.assert_error("SCANNER_ERROR", lambda: prepare_security_inputs(selected, chosen, RUNNER, CONTRACT))
        object.__setattr__(selected, "python_executable", "/bin/sh")
        self.assert_error("SCANNER_ERROR", lambda: prepare_security_inputs(selected, selected.profiles[0], RUNNER, CONTRACT))

    def test_empty_wrong_type_and_overlarge_assets_denied(self):
        for value in (None, "source", b"", bytearray(b"source"), b"x" * (1024 * 1024 + 1)):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.prepare(runner=value))
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.prepare(contract=value))

    def test_asset_literal_content_never_executed(self):
        code = b"raise RuntimeError('must not run on Host')\n"
        self.assertEqual(self.prepare(runner=code).files["_security_runner.py"], code)

    def test_asset_encoding_nul_and_credential_literals_denied(self):
        for asset in (b"\xff", b"x\x00y"):
            self.assert_error("FILE_ENCODING_ERROR", lambda: self.prepare(runner=asset))
        self.assert_error("SECRET_DENIED", lambda: self.prepare(contract=b'password = "private-value"'))

    def test_ordinary_source_variable_text_is_not_modified(self):
        content = b"password = request.password\r\n"
        self.assertEqual(self.prepare(runner=content).files["_security_runner.py"], content)

    def test_per_asset_one_mebibyte_boundary(self):
        content = b"#" * (1024 * 1024)
        inputs = self.prepare(runner=content, contract=content)
        self.assertEqual(len(inputs.files["_security_runner.py"]), 1024 * 1024)
        self.assertLess(sum(map(len, inputs.files.values())), 16 * 1024 * 1024)

    def test_no_missing_extra_alias_or_source_asset_allowed(self):
        captured = self.prepare()
        for key in captured.files:
            files = dict(captured.files)
            del files[key]
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: SecurityScanInputs(files=files,
                inputs_sha256="0" * 64, runner_sha256="0" * 64, contract_sha256="0" * 64, host_configuration_sha256="0" * 64))
        for path in ("tests/suite.json", "source/app.py", "../_security_runner.py", "/security_runner.py"):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.rebuild({**captured.files, path: b"fixture"}))

    def test_each_asset_hash_and_input_hash_cannot_be_forged(self):
        files = dict(self.prepare().files)
        for name in ("inputs_sha256", "runner_sha256", "contract_sha256", "host_configuration_sha256"):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.rebuild(files, **{name: "0" * 64}))

    def test_host_raw_same_values_must_still_be_canonical(self):
        captured = self.prepare()
        host = json.loads(captured.files["_security_host.json"])
        self.assert_error("SCANNER_ERROR", lambda: self.rebuild({**captured.files, "_security_host.json": json.dumps(host).encode()}))

    def test_host_noncanonical_rule_order_rejected_even_matching_hash(self):
        captured = self.prepare()
        host = json.loads(captured.files["_security_host.json"])
        host["rule_ids"] = ["B307", "B101"]
        raw = json.dumps(host, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.assert_error("SCANNER_ERROR", lambda: self.rebuild({**captured.files, "_security_host.json": raw}))

    def test_host_cannot_disable_nosec_or_partial_scan(self):
        captured = self.prepare()
        host = json.loads(captured.files["_security_host.json"])
        for key, value in (("ignore_nosec", False), ("ignore_nosec", 1), ("scan_scope", "ONLY_PACKAGE"), ("scanner", "semgrep"),
                           ("rule_ids", []), ("rule_ids", ["B001"]), ("scanner_version", "latest")):
            raw = json.dumps({**host, key: value}, sort_keys=True, separators=(",", ":")).encode()
            self.assert_error("SCANNER_ERROR", lambda: self.rebuild({**captured.files, "_security_host.json": raw}))

    def test_host_unknown_duplicate_nonfinite_or_bad_json_rejected(self):
        captured = self.prepare()
        original = captured.files["_security_host.json"]
        for raw in (b"{}", b"not json", original[:-1] + b',"ignore_nosec":false}',
                    original[:-1] + b',"threshold":NaN}', original[:-1] + b',"skip":["main.py"]}'):
            self.assert_error("SCANNER_ERROR", lambda: self.rebuild({**captured.files, "_security_host.json": raw}))

    def test_error_constructor_masks_unrecognized_code(self):
        self.assertEqual(str(SecurityInputsError("password=private")), "TOOL_EXECUTION_FAILED")
