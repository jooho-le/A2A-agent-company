"""Repository/tool path separation without a provider, container or file I/O."""

from copy import deepcopy
import unittest

from agents.runtime.file_access import build_file_access, constrain_frozen_read_tools, constrain_workspace_file_tools
from agents.llm.contracts import JsonSchema, ToolDefinition, LLMRuntimeError
from agents.llm.tool_wire import strict_tool_schema
from orchestrator.application.control_input import validate_control_input
from orchestrator.core.security import redact_data
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import WorkspaceAccess, WorkspaceAccessError, authorize_path


class FileAccessGuidanceTests(unittest.TestCase):
    def test_workspace_roots_are_in_model_schema_without_rewriting_mcp(self):
        schema = JsonSchema.from_dict({"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string", "minLength": 1}}, "required": ["path"]})
        originals = tuple(ToolDefinition(name, name, schema, schema)
            for name in ("read_project_file", "write_source_file"))
        read, write = constrain_workspace_file_tools(originals, AgentRole.DEVELOPER)
        for tool in (read, write):
            strict_tool_schema(tool.input_schema).validate({"path": "source/new/auth.py"})
            for invalid in ("main.py", "requirements.lock", "/source/main.py", "source/", "source//main.py"):
                with self.subTest(tool=tool.name, path=invalid), self.assertRaises(LLMRuntimeError):
                    tool.input_schema.validate({"path": invalid})
        read.input_schema.validate({"path": "outputs/qa/tests/test_auth.py"})
        with self.assertRaises(LLMRuntimeError):
            write.input_schema.validate({"path": "outputs/qa/tests/test_auth.py"})
        self.assertNotIn("pattern", originals[0].input_schema.to_dict()["properties"]["path"])
        originals[0].input_schema.validate({"path": "main.py"})

    def test_generated_test_writes_keep_role_roots_and_security_inventory_narrowing(self):
        schema = JsonSchema.from_dict({"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}}, "required": ["path"]})
        write = ToolDefinition("write_test_file", "write", schema, schema)
        for role, root, foreign in ((AgentRole.QA, "qa", "security"), (AgentRole.SECURITY, "security", "qa")):
            tool, = constrain_workspace_file_tools((write,), role)
            strict_tool_schema(tool.input_schema).validate({"path": f"outputs/{root}/tests/test_auth.py"})
            for invalid in ("test_auth.py", "source/test_auth.py", f"outputs/{root}/report.py",
                            f"outputs/{foreign}/tests/test_auth.py"):
                with self.assertRaises(LLMRuntimeError):
                    tool.input_schema.validate({"path": invalid})
        read = ToolDefinition("read_project_file", "read", schema, schema)
        narrowed, = constrain_frozen_read_tools(constrain_workspace_file_tools((read,), AgentRole.SECURITY), ["main.py"])
        strict_tool_schema(narrowed.input_schema).validate({"path": "source/main.py"})
        with self.assertRaises(LLMRuntimeError):
            narrowed.input_schema.validate({"path": "source/absent.py"})

    def test_frozen_read_schema_narrows_paths_without_mutating_mcp_or_reference_inventory(self):
        schema = JsonSchema.from_dict({"type": "object", "additionalProperties": False,
            "properties": {"path": {"type": "string"}}, "required": ["path"]})
        tool = ToolDefinition("read_project_file", "read", schema, schema)
        paths = ["models.py", "source/nested.py"]
        narrowed, = constrain_frozen_read_tools((tool,), paths)
        narrowed.input_schema.validate({"path": "source/models.py"})
        strict_tool_schema(narrowed.input_schema).validate({"path": "source/source/nested.py"})
        for invalid in ("models.py", "source/unknown.py", "source/../models.py"):
            with self.assertRaises(LLMRuntimeError):
                narrowed.input_schema.validate({"path": invalid})
        self.assertNotIn("enum", tool.input_schema.to_dict()["properties"]["path"])
        self.assertEqual(paths, ["models.py", "source/nested.py"])

    def test_frozen_read_schema_does_not_accept_untrusted_inventory_paths(self):
        for paths in ([], ["../models.py"], [".env"]):
            with self.assertRaises((ValueError, WorkspaceAccessError)):
                constrain_frozen_read_tools((), paths)

    def test_all_starting_files_map_without_changing_contract_or_entrypoint(self):
        contract = get_scenario(SCN_001_ID).planner_contract()
        before = deepcopy(contract)
        expected = ["main.py", "database.py", "models.py", "tests/test_health.py", "requirements.lock"]
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                guide = build_file_access(contract, role)
                self.assertEqual(guide["startingFilePaths"], [
                    {"repositoryPath": path, "toolPath": "source/" + path} for path in expected])
                self.assertEqual(guide, redact_data(guide))
                self.assertFalse(guide["listedPathsAreExistenceProof"])
                guide["startingFilePaths"].clear()
                self.assertEqual(contract, before)
        self.assertEqual(contract["externalContract"]["app"]["serve"], "uvicorn main:app")

    def test_repository_directory_named_source_is_not_mistaken_for_tool_prefix(self):
        guide = build_file_access({"externalContract": {"startingFiles": ["source/main.py"]}}, AgentRole.DEVELOPER)
        self.assertEqual(guide["startingFilePaths"], [
            {"repositoryPath": "source/main.py", "toolPath": "source/source/main.py"}])

    def test_legacy_contract_does_not_invent_scn001_files(self):
        for contract in ({}, {"externalContract": {}}):
            self.assertEqual(build_file_access(contract, AgentRole.DEVELOPER)["startingFilePaths"], [])
            self.assertNotIn("startingFiles", contract.get("externalContract", {}))

    def test_measured_inventory_keeps_security_reference_coordinates(self):
        references = ["new/auth.py", "models.py"]
        before = references.copy()
        guide = build_file_access({}, AgentRole.SECURITY, observed_source_paths=references)
        self.assertEqual(guide["observedSourceFiles"], [
            {"repositoryPath": "new/auth.py", "toolPath": "source/new/auth.py"},
            {"repositoryPath": "models.py", "toolPath": "source/models.py"}])
        self.assertEqual(references, before)
        guide["observedSourceFiles"][0]["repositoryPath"] = "changed"
        self.assertEqual(references, before)

    def test_source_and_test_permissions_are_role_specific_and_conditional(self):
        for role, mode, source, tests in (
            (AgentRole.DEVELOPER, "WORKING_COPY", "source/", None),
            (AgentRole.QA, "FROZEN_SNAPSHOT", None, "outputs/qa/tests/"),
            (AgentRole.SECURITY, "FROZEN_SNAPSHOT", None, "outputs/security/tests/"),
        ):
            guide = build_file_access({}, role)
            self.assertEqual((guide["sourceReadMode"], guide["sourceWriteRoot"], guide["testWriteRoot"]),
                             (mode, source, tests))
            self.assertTrue(guide["writesRequireOfferedTool"])
            if source:
                authorize_path(role, source + "main.py", WorkspaceAccess.WRITE)
            else:
                with self.assertRaises(WorkspaceAccessError):
                    authorize_path(role, "source/main.py", WorkspaceAccess.WRITE)
                authorize_path(role, tests + "test_auth.py", WorkspaceAccess.WRITE)

    def test_invalid_secret_absolute_and_traversal_paths_are_never_normalized(self):
        for path in ("../main.py", "/main.py", "C:\\main.py", "./main.py", "a//b.py", ".env",
                     ".git/config", "nested/private.key", "source/", "", None):
            for key in ("starting", "observed"):
                with self.subTest(path=path, key=key), self.assertRaises(WorkspaceAccessError):
                    build_file_access({"externalContract": {"startingFiles": [path] if key == "starting" else []}},
                                      AgentRole.SECURITY, observed_source_paths=[path] if key == "observed" else ())

    def test_malformed_contract_or_wrong_role_is_rejected(self):
        for contract, role in (([], AgentRole.QA), ({"externalContract": []}, AgentRole.QA),
                               ({"externalContract": {"startingFiles": "main.py"}}, AgentRole.QA),
                               ({}, AgentRole.PLANNER), ({}, "DEVELOPER")):
            with self.subTest(contract=contract, role=role), self.assertRaises(ValueError):
                build_file_access(contract, role)

    def test_control_clarification_cannot_replace_host_file_access(self):
        with self.assertRaises(ValueError):
            validate_control_input({"answer": "proceed", "fileAccess": {"sourceRoot": "/"}})
