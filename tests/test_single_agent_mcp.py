"""Single principal and phase policy over real files/SQLite/local MCP stdio."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
import time
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from pydantic import ValidationError

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import ToolCall, ToolContext
from agents.single.mcp import build_single_mcp_configuration, single_phase_tool_definitions
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client
from mcp_tools.core.config import MCPSettings
from mcp_tools.core.policy import ROLE_TOOL_NAMES, SINGLE_TOOL_NAMES, SINGLE_PHASE_TOOL_ROLES
from mcp_tools.runtime import MCPBinding, MCPConfigurationError, MCPDispatcher, MCPProtocolError
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope, decode_unit_configuration, encode_unit_configuration
from mcp_tools.tools.unit import UnitTestTools
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.domain.scenario_registry import RequirementValidator, get_scenario
from orchestrator.sandbox.contracts import CLIResult
from orchestrator.sandbox.runtime import SandboxRuntime
import test_mcp_files as file_fixtures
import test_mcp_unit_store as unit_fixtures
import test_sandbox_runtime as sandbox_fixtures


def binding(phase=SinglePhase.IMPLEMENTING, **changes):
    fields = dict(role=SingleAgentIdentity.SINGLE_AGENT, agent_role=SingleAgentIdentity.SINGLE_AGENT,
                  single_phase=phase, run_id=uuid4(), workspace_id=uuid4())
    fields.update(changes)
    return MCPBinding(**fields)


class SingleMCPPolicyTests(unittest.TestCase):
    def config(self, phase=SinglePhase.IMPLEMENTING, **changes):
        fields = dict(binding=binding(phase), database_path=Path("/tmp/single-fixture.sqlite3"),
                      workspace_root=Path("/tmp/single-fixture-workspaces"))
        fields.update(changes)
        return MCPChildConfiguration(**fields)

    def test_single_capabilities_equal_multi_union_and_phase_maps_are_immutable(self):
        union = set().union(*map(set, ROLE_TOOL_NAMES.values()))
        self.assertEqual(set(SINGLE_TOOL_NAMES), union)
        self.assertEqual(len(SINGLE_TOOL_NAMES), 10)
        self.assertEqual(set().union(*(set(mapping) for mapping in SINGLE_PHASE_TOOL_ROLES.values())), union)
        with self.assertRaises(TypeError):
            SINGLE_PHASE_TOOL_ROLES[SinglePhase.QA]["write_source_file"] = AgentRole.DEVELOPER
        with self.assertRaises(FrozenInstanceError):
            binding().single_phase = SinglePhase.SECURITY

    def test_identity_phase_and_settings_cannot_be_mixed_or_supplied_as_raw_roles(self):
        for changes in ({"single_phase": None}, {"single_phase": "QA"}, {"role": "SINGLE_AGENT"},
                        {"agent_role": AgentRole.DEVELOPER}, {"role": AgentRole.DEVELOPER, "agent_role": AgentRole.DEVELOPER}):
            with self.subTest(changes=changes), self.assertRaises(MCPConfigurationError):
                binding(**changes)
        with self.assertRaises(ValidationError):
            MCPSettings(role="SINGLE_AGENT", _env_file=None)
        with self.assertRaises(ValidationError):
            MCPSettings(role="QA", single_phase="QA", _env_file=None)
        settings = MCPSettings(role="SINGLE_AGENT", single_phase="QA", _env_file=None)
        self.assertEqual(settings.allowed_tool_names, SINGLE_TOOL_NAMES)

    def test_validation_requires_frozen_selection_and_child_keeps_single_identity(self):
        for phase in (SinglePhase.QA, SinglePhase.SECURITY):
            with self.assertRaises(MCPClientError):
                self.config(phase)
        config = self.config(SinglePhase.FIXING)
        args = child_parameters(config).args
        self.assertEqual(args[args.index("--role") + 1], "SINGLE_AGENT")
        self.assertEqual(args[args.index("--agent-role") + 1], "SINGLE_AGENT")
        self.assertEqual(args[args.index("--single-phase") + 1], "FIXING")
        with self.assertRaises(MCPConfigurationError):
            MCPDispatcher(binding(SinglePhase.QA), Mock())

    def test_host_factory_filters_scopes_without_exposing_protected_tests(self):
        unit = UnitTestConfiguration(scopes=(
            UnitTestScope(name="dev", kind="SNAPSHOT"),
            UnitTestScope(name="qa", kind="QA_TESTS"),
            UnitTestScope(name="sec", kind="QA_TESTS", role="SECURITY"),
            UnitTestScope(name="hidden", kind="PROTECTED", protected_files={"tests/test_hidden.py": "pass\n"},
                          protected_suite_ref="artifact://protected/tests"),
        ))
        source = FrozenSourceSelection(project_artifact_id=uuid4(), snapshot_sha256="a" * 64)
        expected = {SinglePhase.PLANNING: (), SinglePhase.IMPLEMENTING: ("dev",), SinglePhase.FIXING: ("dev",),
                    SinglePhase.QA: ("qa",), SinglePhase.SECURITY: ("sec",)}
        for phase, names in expected.items():
            config = build_single_mcp_configuration(phase=phase, run_id=uuid4(), workspace_id=uuid4(),
                database_path=Path("/tmp/single.sqlite3"), workspace_root=Path("/tmp/single-workspaces"),
                frozen_source=source, unit_test_configuration=unit)
            self.assertEqual(tuple(scope.name for scope in config.unit_test_configuration.scopes)
                             if config.unit_test_configuration is not None else (), names)
        self.assertEqual(len(unit.scopes), 4)
        with self.assertRaises(MCPClientError):
            self.config(unit_test_configuration=unit)

    def test_security_scope_round_trip_preserves_old_scope_encoding(self):
        old = UnitTestConfiguration(scopes=(UnitTestScope(name="qa", kind="QA_TESTS"),))
        old_json = encode_unit_configuration(old)
        self.assertNotIn('"role"', old_json)
        security = UnitTestConfiguration(scopes=(UnitTestScope(name="sec", kind="QA_TESTS", role="SECURITY"),))
        restored = decode_unit_configuration(encode_unit_configuration(security))
        self.assertEqual(restored.scopes[0].roles, (AgentRole.SECURITY,))
        self.assertEqual(encode_unit_configuration(decode_unit_configuration(old_json)), old_json)


class SingleMCPFileTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = file_fixtures.FileToolTests("runTest")
        self.fixture.architecture = "SINGLE_AGENT"
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.selection = self.fixture.source_selection()
        self.run = self.fixture.run

    def binding(self, phase):
        return binding(phase, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def set_phase(self, phase):
        status = {SinglePhase.IMPLEMENTING: WorkflowStatus.IMPLEMENTING, SinglePhase.FIXING: WorkflowStatus.FIXING,
                  SinglePhase.QA: WorkflowStatus.VALIDATING, SinglePhase.SECURITY: WorkflowStatus.VALIDATING,
                  SinglePhase.PLANNING: WorkflowStatus.PLANNING}[phase]
        run = self.fixture.repository.get_run(self.run.run_id)
        updated = type(run).model_validate({**run.model_dump(), "status": status, "code_version": 1})
        with self.fixture.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (status.value, updated.model_dump_json(), str(run.run_id)))

    def dispatcher(self, phase, *, handlers=None):
        tools = FileTools(self.fixture.store, frozen_source=self.selection)
        handlers = handlers if handlers is not None else {
            name: tools.handlers(role)[name] for name, role in SINGLE_PHASE_TOOL_ROLES[phase].items()
            if name in tools.handlers(role)}
        return MCPDispatcher(self.binding(phase), self.fixture.registry, handlers=handlers,
                             frozen_snapshot_id=self.selection.project_artifact_id)

    def configuration(self, phase, **changes):
        return build_single_mcp_configuration(
            phase=phase, run_id=self.run.run_id, workspace_id=self.run.workspace_id,
            database_path=self.fixture.database, workspace_root=self.fixture.base,
            frozen_source=self.selection, **changes)

    def args(self, **changes):
        return self.fixture.arguments(**changes)

    async def test_real_source_write_patch_and_role_provenance(self):
        dispatcher = self.dispatcher(SinglePhase.IMPLEMENTING)
        result = await dispatcher.call_tool("write_source_file", self.args(content="password=request.password\n"))
        self.assertIsNone(result.error_code)
        self.assertEqual(self.fixture.file.read_text(), "password=request.password\n")
        self.fixture.file.write_bytes(self.fixture.original)
        patch = await self.dispatcher(SinglePhase.FIXING).call_tool("apply_patch", self.fixture.patch_arguments(self.selection))
        self.assertEqual(patch.error_code, "PERMISSION_DENIED")  # stored phase is still IMPLEMENTING
        self.set_phase(SinglePhase.FIXING)
        patch = await self.dispatcher(SinglePhase.FIXING).call_tool("apply_patch", self.fixture.patch_arguments(self.selection))
        self.assertIsNone(patch.error_code)
        self.assertIn("updated", self.fixture.file.read_text())

        seen = []
        async def spy(context, arguments):
            seen.append(context)
            return {"path": arguments["path"], "content": "", "sha256": sha256(b"").hexdigest(), "sizeBytes": 0}
        result = await self.dispatcher(SinglePhase.FIXING, handlers={"read_project_file": spy}).call_tool("read_project_file", self.args())
        self.assertIsNone(result.error_code)
        self.assertIs(seen[0].principal_binding.role, SingleAgentIdentity.SINGLE_AGENT)
        self.assertIs(seen[0].binding.role, AgentRole.DEVELOPER)

    async def test_validators_read_frozen_source_and_write_only_their_test_folder(self):
        self.fixture.file.write_text("# changed working tree\n")
        for phase, folder, other in ((SinglePhase.QA, "qa", "security"), (SinglePhase.SECURITY, "security", "qa")):
            self.set_phase(phase)
            dispatcher = self.dispatcher(phase)
            read = await dispatcher.call_tool("read_project_file", self.args())
            self.assertEqual(read.data["content"], self.fixture.original.decode())
            write = await dispatcher.call_tool("write_test_file", self.args(path=f"outputs/{folder}/tests/test_signup.py", content="pass\n"))
            self.assertIsNone(write.error_code)
            for path in ("source/signup.py", f"outputs/{other}/tests/test_signup.py", f"outputs/{folder}/report.json",
                         f"snapshots/{self.selection.project_artifact_id}/source/signup.py", "../protected/test_exam.py", "source/.env"):
                outcome = await dispatcher.call_tool("write_test_file", self.args(path=path, content="overwrite"))
                self.assertIsNotNone(outcome.error_code, path)
            for name, arguments in (("write_source_file", self.args(content="overwrite")),
                                    ("apply_patch", self.fixture.patch_arguments(self.selection))):
                outcome = await dispatcher.call_tool(name, arguments)
                self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.fixture.file.read_text(), "# changed working tree\n")

    async def test_workspace_snapshot_and_argument_spoofing_stop_before_handler(self):
        self.set_phase(SinglePhase.QA)
        handler = AsyncMock()
        dispatcher = self.dispatcher(SinglePhase.QA, handlers={"run_unit_tests": handler})
        base = {"workspaceId": str(self.run.workspace_id), "snapshotId": str(self.selection.project_artifact_id), "testScope": "qa"}
        for args in ({**base, "workspaceId": str(uuid4())}, {**base, "snapshotId": str(uuid4())}):
            self.assertEqual((await dispatcher.call_tool("run_unit_tests", args)).error_code, "PERMISSION_DENIED")
        for field in ("role", "singlePhase", "command", "hostPath"):
            with self.assertRaises(MCPProtocolError):
                await dispatcher.call_tool("run_unit_tests", {**base, field: "override"})
        handler.assert_not_awaited()

    async def test_planning_and_stale_phase_cannot_execute_even_registered_handlers(self):
        handler = AsyncMock()
        planner = self.dispatcher(SinglePhase.PLANNING, handlers={"write_source_file": handler})
        self.assertEqual((await planner.call_tool("write_source_file", self.args(content="bad"))).error_code, "PERMISSION_DENIED")
        self.set_phase(SinglePhase.QA)
        stale = self.dispatcher(SinglePhase.IMPLEMENTING, handlers={"write_source_file": handler})
        self.assertEqual((await stale.call_tool("write_source_file", self.args(content="bad"))).error_code, "PERMISSION_DENIED")
        handler.assert_not_awaited()

    async def test_single_binding_cannot_attach_to_a_multi_run(self):
        other = file_fixtures.FileToolTests("runTest")
        other.setUp()
        self.addCleanup(other.doCleanups)
        handler = AsyncMock()
        principal = binding(run_id=other.run.run_id, workspace_id=other.run.workspace_id)
        dispatcher = MCPDispatcher(principal, other.registry, handlers={"write_source_file": handler})
        outcome = await dispatcher.call_tool("write_source_file", other.arguments(content="bad"))
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        handler.assert_not_awaited()

    async def test_old_candidate_is_refused_even_when_its_snapshot_hash_is_valid(self):
        self.set_phase(SinglePhase.QA)
        run = self.fixture.repository.get_run(self.run.run_id)
        updated = type(run).model_validate({**run.model_dump(), "code_version": 2})
        with self.fixture.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?", (updated.model_dump_json(), str(run.run_id)))
        outcome = await self.dispatcher(SinglePhase.QA).call_tool("read_project_file", self.args())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")

    async def test_stdio_keeps_single_binding_and_enforces_policy_after_client_bypass(self):
        self.set_phase(SinglePhase.SECURITY)
        configuration = self.configuration(SinglePhase.SECURITY)
        async with open_mcp_client(configuration) as client:
            self.assertEqual({tool.name for tool in client.list_tools()}, set(SINGLE_TOOL_NAMES))
            self.assertEqual({tool.name for tool in single_phase_tool_definitions(client)},
                             set(SINGLE_PHASE_TOOL_ROLES[SinglePhase.SECURITY]))
            args = self.args(path="outputs/security/tests/test_stdio.py", content="pass\n")
            call = ToolCall("single-write", "write_test_file", json.dumps(args))
            context = ToolContext(SingleAgentIdentity.SINGLE_AGENT, str(self.run.workspace_id), time.monotonic() + 10)
            await client.execute(call, args, context)
            with self.assertRaises(MCPClientError):
                await client.execute(call, args, replace(context, role=AgentRole.SECURITY))
            with self.assertRaises(MCPClientError):
                await client.call_tool("write_source_file", self.args(content="bad"))
            # Bypass local list/filter/adapter: the real stdio server must deny.
            denied = await client._client.session.call_tool("write_source_file", self.args(content="bad"))
            self.assertTrue(denied.is_error)
            self.assertEqual(denied.content[0].text, "PERMISSION_DENIED")
            frozen = await client.call_tool("read_project_file", self.args())
            self.assertEqual(frozen["content"], self.fixture.original.decode())
        self.assertEqual((self.fixture.root / "outputs/security/tests/test_stdio.py").read_text(), "pass\n")
        self.assertEqual(self.fixture.file.read_bytes(), self.fixture.original)


class SingleMCPSecurityExecutionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = unit_fixtures.UnitTestOutputStoreTests("runTest")
        self.fixture.architecture = "SINGLE_AGENT"
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        step = self.fixture.qa_context()
        self.fixture.mutate_step(step=step, agent_role=AgentRole.SECURITY,
            requirement_ids=list(get_scenario(self.fixture.run.scenario_id).requirement_ids_for(RequirementValidator.SECURITY)))
        self.scope = UnitTestScope(name="security-tests", kind="QA_TESTS", role="SECURITY")
        self.configuration = UnitTestConfiguration(scopes=(self.scope,), image_reference=sandbox_fixtures.IMAGE_ID)
        for role in ("qa", "security"):
            folder = self.fixture.root / "outputs" / role / "tests"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "test_signup.py").write_text(f"# {role} owned tests\n")
        self.docker = sandbox_fixtures.FakeDocker()
        artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, artifacts, docker=self.docker)
        tools = UnitTestTools(artifacts, sandbox, self.fixture.store, configuration=self.configuration)
        principal = binding(SinglePhase.SECURITY, run_id=self.fixture.run.run_id, workspace_id=self.fixture.run.workspace_id)
        self.dispatcher = MCPDispatcher(principal, self.fixture.registry, handlers=tools.handlers(AgentRole.SECURITY),
                                        frozen_snapshot_id=self.fixture.source.artifact_id)

    async def test_security_tests_use_own_scratch_and_measured_frozen_manifest(self):
        self.docker.start_result = CLIResult(returncode=0, stdout=self.fixture.report_json().encode(), stderr=b"")
        def inspect(container):
            inputs = next(item for item in container["Mounts"] if item["Destination"] == "/inputs")
            self.assertEqual((Path(inputs["Source"]) / "tests/test_signup.py").read_text(), "# security owned tests\n")
        self.docker.start_hook = inspect
        outcome = await self.dispatcher.call_tool("run_unit_tests", {
            "workspaceId": str(self.fixture.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
            "testScope": self.scope.name})
        self.assertIsNone(outcome.error_code)
        record = self.fixture.store.get(self.fixture.run.run_id, outcome.data["executionManifestId"])
        self.assertEqual(record.execution_manifest, self.fixture.source.execution_manifest())
        self.assertEqual(record.scope.roles, (AgentRole.SECURITY,))
        self.assertEqual(outcome.data["passed"], 1)
        self.assertIsNone(self.fixture.repository.get_run(self.fixture.run.run_id).verdict)
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_failing_security_test_is_product_failure_with_completed_tool_receipt(self):
        self.docker.exit_code = 1
        self.docker.start_result = CLIResult(returncode=1, stdout=self.fixture.report_json(outcome="FAIL").encode(), stderr=b"")
        outcome = await self.dispatcher.call_tool("run_unit_tests", {
            "workspaceId": str(self.fixture.run.workspace_id), "snapshotId": str(self.fixture.source.artifact_id),
            "testScope": self.scope.name})
        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.data["failed"], 1)
        self.assertIsNone(self.fixture.repository.get_run(self.fixture.run.run_id).verdict)


if __name__ == "__main__":
    unittest.main()
