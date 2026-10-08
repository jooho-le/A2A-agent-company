"""Offline Tool contracts, not tests of the deferred filesystem/runner handlers."""

from dataclasses import FrozenInstanceError, replace
import json
import unittest
from uuid import UUID

from agents.llm.contracts import JsonSchema, LLMRuntimeError
from mcp_tools.core.catalog import (
    JSON_SCHEMA_DIALECT, MAX_FILE_BYTES, MAX_PATH_LENGTH, TOOL_CONTRACTS,
    ToolContract, ToolSchemaError, get_tool_contract,
)
from mcp_tools.core.policy import ROLE_TOOL_NAMES
from orchestrator.domain.states import AgentRole


WORKSPACE = "993d1a6f-1b7e-44cf-a83c-8bcf1d73e728"
SNAPSHOT = "b6ed212a-8213-48f3-8f63-2387480dba9d"
MANIFEST = "840e929a-0314-4b5e-a0df-f51f36c92ae0"
HASH = "b" * 64

INPUTS = {
    "read_project_file": {"workspaceId": WORKSPACE, "path": "src/main.py"},
    "write_source_file": {"workspaceId": WORKSPACE, "path": "src/main.py", "content": "print('hello')\n"},
    "write_test_file": {"workspaceId": WORKSPACE, "path": "qa/test_signup.py", "content": "assert 1 == 1\n"},
    "apply_patch": {"workspaceId": WORKSPACE, "patch": "--- a/src/main.py\n+++ b/src/main.py\n", "baseSnapshotSha256": HASH},
    "run_build": {"workspaceId": WORKSPACE, "snapshotId": SNAPSHOT},
    "run_unit_tests": {"workspaceId": WORKSPACE, "snapshotId": SNAPSHOT, "testScope": "signup-unit"},
    "run_browser_tests": {"workspaceId": WORKSPACE, "snapshotId": SNAPSHOT, "testSuite": "signup-browser"},
    "run_security_scan": {"workspaceId": WORKSPACE, "snapshotId": SNAPSHOT, "scannerProfile": "signup-security"},
    "read_test_report": {"workspaceId": WORKSPACE, "reportRef": "artifact://test-report/report.json"},
    "read_security_report": {"workspaceId": WORKSPACE, "reportRef": "artifact://security-report/report.json"},
}

OUTPUTS = {
    "read_project_file": {"path": "src/main.py", "content": "print('hello')\n", "sha256": HASH, "sizeBytes": 15},
    "write_source_file": {"path": "src/main.py", "sha256": HASH, "sizeBytes": 15, "changed": True},
    "write_test_file": {"path": "qa/test_signup.py", "sha256": HASH, "changed": True},
    "apply_patch": {"changedFiles": ["src/main.py"], "newHashes": {"src/main.py": HASH}},
    "run_build": {"exitCode": 0, "durationMs": 12, "executionManifestId": MANIFEST},
    "run_unit_tests": {"total": 3, "passed": 2, "failed": 1, "skipped": 0, "reportRef": "artifact://tests/result.json", "executionManifestId": MANIFEST},
    "run_browser_tests": {"total": 2, "passed": 1, "failed": 1, "traceRefs": [], "executionManifestId": MANIFEST},
    "run_security_scan": {"findings": [{"ruleId": "example", "details": {"line": 2}}], "reportRef": "artifact://security/result.json", "executionManifestId": MANIFEST},
    "read_test_report": {"testResult": {"tests": [{"name": "signup", "passed": False}], "metadata": None}},
    "read_security_report": {"securityResult": {"findings": [], "complete": True}},
}


class MCPCatalogTests(unittest.TestCase):
    def assert_schema_error(self, function, value):
        with self.assertRaises(ToolSchemaError) as raised:
            function(value)
        self.assertEqual(str(raised.exception), "MCP_TOOL_SCHEMA_INVALID")
        self.assertIsNone(raised.exception.__cause__)
        self.assertIsNone(raised.exception.__context__)
        self.assertTrue(raised.exception.__suppress_context__)

    def test_exact_ten_definition_tools(self):
        self.assertEqual(set(TOOL_CONTRACTS), set(INPUTS))
        self.assertEqual(len(TOOL_CONTRACTS), 10)

    def test_role_policy_names_have_catalog_contracts(self):
        self.assertEqual(ROLE_TOOL_NAMES[AgentRole.PLANNER], ())
        for role, names in ROLE_TOOL_NAMES.items():
            with self.subTest(role=role):
                self.assertEqual(len(names), len(set(names)))
                self.assertTrue(set(names).issubset(TOOL_CONTRACTS))

    def test_lookup_unknown_and_unhashable_do_not_echo(self):
        self.assertIs(get_tool_contract("run_build"), TOOL_CONTRACTS["run_build"])
        for unknown in ("run_shell", "password=my-secret", {}, [], None, 1):
            with self.subTest(type=type(unknown)):
                self.assertIsNone(get_tool_contract(unknown))

    def test_all_example_inputs_and_outputs_validate(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(name=name):
                self.assertIsNone(contract.validate_input(INPUTS[name]))
                self.assertIsNone(contract.validate_output(OUTPUTS[name]))

    def test_schema_dialect_root_closed_and_workspace_required(self):
        for contract in TOOL_CONTRACTS.values():
            with self.subTest(name=contract.name):
                for schema in (contract.input_schema, contract.output_schema):
                    self.assertEqual(schema["$schema"], JSON_SCHEMA_DIALECT)
                    self.assertEqual(schema["type"], "object")
                    self.assertIs(schema["additionalProperties"], False)
                self.assertIn("workspaceId", contract.input_schema["required"])
                self.assertEqual(contract.input_schema["properties"]["workspaceId"], {"type": "string", "format": "uuid"})

    def test_host_authority_and_arbitrary_shell_fields_rejected(self):
        for name, contract in TOOL_CONTRACTS.items():
            for field in ("role", "runId", "rootPath", "hostPath", "shell", "argv", "command", "environment"):
                with self.subTest(name=name, field=field):
                    self.assert_schema_error(contract.validate_input, {**INPUTS[name], field: "untrusted"})

    def test_missing_workspace_rejected_for_every_tool(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(name=name):
                value = dict(INPUTS[name])
                del value["workspaceId"]
                self.assert_schema_error(contract.validate_input, value)

    def test_extra_output_field_rejected_for_every_tool(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(name=name):
                self.assert_schema_error(contract.validate_output, {**OUTPUTS[name], "extra": "password=my-secret"})

    def test_uuid_format_checker_enforced(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(name=name):
                self.assert_schema_error(contract.validate_input, {**INPUTS[name], "workspaceId": "not-a-uuid"})
        self.assert_schema_error(TOOL_CONTRACTS["run_build"].validate_input, {"workspaceId": WORKSPACE, "snapshotId": "not-a-uuid"})
        self.assert_schema_error(TOOL_CONTRACTS["run_build"].validate_output, {**OUTPUTS["run_build"], "executionManifestId": "not-a-uuid"})

    def test_expected_hash_remains_optional_and_nonnullable(self):
        contract = TOOL_CONTRACTS["write_source_file"]
        self.assertNotIn("expectedSha256", contract.input_schema["required"])
        contract.validate_input(INPUTS[contract.name])
        contract.validate_input({**INPUTS[contract.name], "expectedSha256": HASH})
        for invalid in (None, "B" * 64, "a" * 63, "sha256:" + HASH, "not-a-hash"):
            with self.subTest(invalid=invalid):
                self.assert_schema_error(contract.validate_input, {**INPUTS[contract.name], "expectedSha256": invalid})

    def test_build_output_refs_remain_optional(self):
        contract = TOOL_CONTRACTS["run_build"]
        self.assertEqual(set(contract.output_schema["required"]), {"exitCode", "durationMs", "executionManifestId"})
        contract.validate_output(OUTPUTS[contract.name])
        contract.validate_output({**OUTPUTS[contract.name], "stdoutRef": "artifact://build/stdout.txt", "stderrRef": "artifact://build/stderr.txt"})
        self.assert_schema_error(contract.validate_output, {**OUTPUTS[contract.name], "stdoutRef": None})

    def test_product_failure_and_findings_are_valid_success_payloads(self):
        TOOL_CONTRACTS["run_build"].validate_output({**OUTPUTS["run_build"], "exitCode": 1})
        TOOL_CONTRACTS["run_unit_tests"].validate_output(OUTPUTS["run_unit_tests"])
        TOOL_CONTRACTS["run_security_scan"].validate_output(OUTPUTS["run_security_scan"])

    def test_counters_reject_negative_bool_and_strings(self):
        contract = TOOL_CONTRACTS["run_unit_tests"]
        for invalid in (-1, True, "2", 2.5):
            with self.subTest(invalid=invalid):
                self.assert_schema_error(contract.validate_output, {**OUTPUTS[contract.name], "failed": invalid})

    def test_paths_bounded_but_permission_checks_deferred(self):
        contract = TOOL_CONTRACTS["read_project_file"]
        for path in ("/etc/passwd", "../../outside", ".env", "src/alias"):
            contract.validate_input({"workspaceId": WORKSPACE, "path": path})
        contract.validate_input({"workspaceId": WORKSPACE, "path": "a" * MAX_PATH_LENGTH})
        for path in ("", "a" * (MAX_PATH_LENGTH + 1), None, 1):
            self.assert_schema_error(contract.validate_input, {"workspaceId": WORKSPACE, "path": path})

    def test_source_input_preservation_annotations_are_exact(self):
        expected = {"write_source_file": ("content",), "write_test_file": ("content",), "apply_patch": ("patch",)}
        for contract in TOOL_CONTRACTS.values():
            self.assertEqual(contract.source_argument_fields, expected.get(contract.name, ()))
            self.assertEqual(contract.source_output_fields, ("content",) if contract.name == "read_project_file" else ())

    def test_source_unicode_bytes_limit_not_only_character_count(self):
        contract = TOOL_CONTRACTS["write_source_file"]
        safe = "가" * (MAX_FILE_BYTES // 3)
        contract.validate_input({**INPUTS[contract.name], "content": safe})
        self.assert_schema_error(contract.validate_input, {**INPUTS[contract.name], "content": safe + "가"})

    def test_source_input_character_limit(self):
        for name, field in (("write_source_file", "content"), ("write_test_file", "content"), ("apply_patch", "patch")):
            with self.subTest(name=name):
                TOOL_CONTRACTS[name].validate_input({**INPUTS[name], field: "a" * MAX_FILE_BYTES})
                self.assert_schema_error(TOOL_CONTRACTS[name].validate_input, {**INPUTS[name], field: "a" * (MAX_FILE_BYTES + 1)})

    def test_read_source_output_unicode_bytes_limit(self):
        contract = TOOL_CONTRACTS["read_project_file"]
        self.assert_schema_error(contract.validate_output, {**OUTPUTS[contract.name], "content": "😀" * (MAX_FILE_BYTES // 4 + 1)})

    def test_escaped_max_size_source_is_not_artificially_reduced(self):
        TOOL_CONTRACTS["write_source_file"].validate_input({**INPUTS["write_source_file"], "content": "\u0000" * MAX_FILE_BYTES})

    def test_contract_schemas_are_deep_fresh_copies(self):
        contract = TOOL_CONTRACTS["write_source_file"]
        first = contract.input_schema
        first["properties"]["content"]["maxLength"] = 1
        first["required"].append("role")
        self.assertEqual(contract.input_schema["properties"]["content"]["maxLength"], MAX_FILE_BYTES)
        self.assertNotIn("role", contract.input_schema["required"])
        output = TOOL_CONTRACTS["run_security_scan"].output_schema
        output["$defs"]["jsonObject"]["maxProperties"] = 0
        self.assertEqual(TOOL_CONTRACTS["run_security_scan"].output_schema["$defs"]["jsonObject"]["maxProperties"], 256)

    def test_catalog_and_contract_are_immutable(self):
        with self.assertRaises(TypeError):
            TOOL_CONTRACTS["run_shell"] = TOOL_CONTRACTS["run_build"]
        with self.assertRaises(FrozenInstanceError):
            TOOL_CONTRACTS["run_build"].name = "run_shell"

    def test_schema_text_excluded_from_repr(self):
        contract = TOOL_CONTRACTS["write_source_file"]
        self.assertNotIn("properties", repr(contract))
        self.assertNotIn("maxLength", repr(contract))

    def test_duplicate_schema_keys_rejected_without_data(self):
        contract = TOOL_CONTRACTS["run_build"]
        duplicate = '{"$schema":"' + JSON_SCHEMA_DIALECT + '","type":"object","type":"object","additionalProperties":false}'
        with self.assertRaisesRegex(ToolSchemaError, "^MCP_TOOL_SCHEMA_INVALID$"):
            replace(contract, input_schema_json=duplicate)

    def test_remote_schema_refs_and_scope_changing_keywords_rejected(self):
        contract = TOOL_CONTRACTS["run_build"]
        for key, value in (("$ref", "https://example.invalid/password-secret"), ("$id", "https://example.invalid/"), ("$dynamicRef", "#payload"), ("$recursiveRef", "#")):
            with self.subTest(key=key):
                schema = contract.input_schema
                schema["properties"]["workspaceId"][key] = value
                with self.assertRaisesRegex(ToolSchemaError, "^MCP_TOOL_SCHEMA_INVALID$"):
                    replace(contract, input_schema_json=json.dumps(schema))

    def test_wrong_dialect_open_or_nonobject_schema_rejected(self):
        contract = TOOL_CONTRACTS["run_build"]
        for key, value in (("$schema", "https://json-schema.org/draft-07/schema"), ("additionalProperties", True), ("type", "array")):
            with self.subTest(key=key):
                schema = contract.input_schema
                schema[key] = value
                with self.assertRaises(ToolSchemaError):
                    replace(contract, input_schema_json=json.dumps(schema))

    def test_invalid_source_annotations_rejected(self):
        contract = TOOL_CONTRACTS["write_source_file"]
        for fields in (["content"], ("workspaceId", "workspaceId"), ("unknown",), (1,)):
            with self.subTest(fields=fields):
                with self.assertRaises(ToolSchemaError):
                    replace(contract, source_argument_fields=fields)

    def test_non_json_objects_nonfinite_and_nonstring_keys_rejected(self):
        contract = TOOL_CONTRACTS["read_test_report"]
        class CustomDict(dict):
            pass
        for invalid in (float("nan"), float("inf"), float("-inf"), UUID(WORKSPACE), (1, 2), {1: "value"}, CustomDict(a=1), "\ud800"):
            with self.subTest(type=type(invalid)):
                self.assert_schema_error(contract.validate_output, {"testResult": {"nested": invalid}})

    def test_report_payload_bounds_and_nested_unsupported_values(self):
        contract = TOOL_CONTRACTS["read_test_report"]
        for invalid in ({"values": [1] * 1001}, {"k" + str(i): None for i in range(257)}, {"": None}, {"x": "a" * (MAX_FILE_BYTES + 1)}):
            self.assert_schema_error(contract.validate_output, {"testResult": invalid})
        self.assert_schema_error(contract.validate_output, {"testResult": [1, 2]})
        contract.validate_output({"testResult": {"nested": [{"null": None, "bool": True, "integer": 5, "float": 1.5, "str": "text"}]}})

    def test_depth_and_node_limits_rejected(self):
        contract = TOOL_CONTRACTS["read_test_report"]
        nested = None
        for _ in range(70):
            nested = {"child": nested}
        self.assert_schema_error(contract.validate_output, {"testResult": nested})
        # Each individual array is within its bound, but total nodes are not.
        self.assert_schema_error(contract.validate_output, {"testResult": {str(i): [None] * 1000 for i in range(70)}})

    def test_patch_map_hashes_and_unique_file_list(self):
        contract = TOOL_CONTRACTS["apply_patch"]
        contract.validate_output({"changedFiles": [], "newHashes": {}})
        self.assert_schema_error(contract.validate_output, {"changedFiles": ["src/a.py", "src/a.py"], "newHashes": {"src/a.py": HASH}})
        self.assert_schema_error(contract.validate_output, {"changedFiles": ["src/a.py"], "newHashes": {"src/a.py": "invalid"}})
        self.assert_schema_error(contract.validate_output, {"changedFiles": [], "newHashes": {"": HASH}})

    def test_existing_llm_schema_accepts_catalog_without_weakening(self):
        for name, contract in TOOL_CONTRACTS.items():
            with self.subTest(name=name):
                JsonSchema.from_dict(contract.input_schema).validate(INPUTS[name])
                JsonSchema.from_dict(contract.output_schema).validate(OUTPUTS[name])
        # Preserve the optional MCP contract, rather than changing it to make
        # a provider-specific strict structured-output mode accept every field.
        with self.assertRaises(LLMRuntimeError):
            JsonSchema.from_dict(TOOL_CONTRACTS["write_source_file"].input_schema).require_openai_strict()


if __name__ == "__main__":
    unittest.main()
