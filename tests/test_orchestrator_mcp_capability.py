"""Definition §8 Host capability: real stdio/SQLite, fake Docker only.

No fifth LLM Agent, generated-code Host fallback, network or real Container
execution is introduced. Existing Source and report ACLs remain authoritative.
"""

import json
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

import test_mcp_build as build_fixture
import test_mcp_browser as browser_fixture
import test_mcp_security_store as security_fixture
import test_mcp_unit_store as unit_fixture
from agents.llm.contracts import ToolCall, ToolContext
from mcp_tools.client import (
    BoundMCPClient, MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client,
)
from mcp_tools.core.config import MCPSettings
from mcp_tools.core.policy import MCPHostPrincipal, MCP_TOOL_NAMES, ROLE_TOOL_NAMES
from mcp_tools.execution_runtime import TrackedMCPExecutor, TrackedMCPError
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import (
    MCPBinding, MCPConfigurationError, MCPDispatcher, MCPExecutionContext,
    MCPProtocolError, MCPToolExecutionError, delegate_host_context,
)
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.test_reports import TestReportTools
from mcp_tools.tools.unit import UnitTestTools
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.states import AgentRole, WorkflowStatus
from orchestrator.sandbox.contracts import CLIResult
from orchestrator.sandbox.runtime import SandboxRuntime


HOST = MCPHostPrincipal.ORCHESTRATOR
HOST_TOOLS = ("run_build", "read_test_report", "read_security_report")


def host_binding(run):
    return MCPBinding(role=HOST, agent_role=None,
                      run_id=run.run_id, workspace_id=run.workspace_id)


def child(fixture):
    return MCPChildConfiguration(
        binding=host_binding(fixture.run), database_path=fixture.repository.database_path,
        workspace_root=fixture.base, max_call_seconds=15,
    )


def transfer_cleanups(owner, fixture):
    # An IsolatedAsyncioTestCase fixture has no running runner of its own.
    # Execute its synchronous cleanup with this test's active runner instead.
    for cleanup, args, kwargs in fixture._cleanups:
        owner.addCleanup(cleanup, *args, **kwargs)
    fixture._cleanups.clear()


class HostBindingTests(unittest.TestCase):
    def test_llm_roles_remain_exactly_four_and_host_policy_is_separate(self):
        self.assertEqual(len(AgentRole), 4)
        self.assertEqual(set(ROLE_TOOL_NAMES), set(AgentRole))
        self.assertNotIn(HOST, ROLE_TOOL_NAMES)
        self.assertEqual(MCP_TOOL_NAMES[HOST], HOST_TOOLS)

    def test_host_settings_expose_only_definition_build_and_report_tools(self):
        settings = MCPSettings(role="ORCHESTRATOR", _env_file=None)
        self.assertIs(settings.role, HOST)
        self.assertEqual(settings.allowed_tool_names, HOST_TOOLS)

    def test_host_binding_requires_no_agent_identity(self):
        for identity in AgentRole:
            with self.subTest(role=identity), self.assertRaises(MCPConfigurationError):
                MCPBinding(role=HOST, agent_role=identity, run_id=uuid4(), workspace_id=uuid4())
        with self.assertRaises(MCPConfigurationError):
            MCPBinding(role="ORCHESTRATOR", agent_role=None, run_id=uuid4(), workspace_id=uuid4())

    def test_agent_binding_still_requires_matching_explicit_agent_identity(self):
        for role in AgentRole:
            with self.subTest(role=role), self.assertRaises(MCPConfigurationError):
                MCPBinding(role=role, agent_role=None, run_id=uuid4(), workspace_id=uuid4())

    def test_host_child_configuration_has_no_agent_role_wire_argument(self):
        config = MCPChildConfiguration(
            binding=MCPBinding(role=HOST, agent_role=None, run_id=uuid4(), workspace_id=uuid4()),
            database_path=Path("/tmp/nonexistent-orchestrator.db"), workspace_root=Path("/tmp/nonexistent-orchestrator"),
        )
        parameters = child_parameters(config)
        self.assertEqual(parameters.args[4:6], ["--role", "ORCHESTRATOR"])
        self.assertNotIn("--agent-role", parameters.args)
        self.assertNotIn("OPENAI_API_KEY", parameters.env)

    def test_unrelated_handlers_cannot_be_installed_for_host(self):
        binding = MCPBinding(role=HOST, agent_role=None, run_id=uuid4(), workspace_id=uuid4())
        for name in ("read_project_file", "write_source_file", "apply_patch", "run_security_scan"):
            with self.subTest(tool=name), self.assertRaises(MCPConfigurationError):
                MCPDispatcher(binding, Mock(), handlers={name: Mock()})


class HostClientBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_host_capability_cannot_attach_to_any_llm_agent_role(self):
        config = MCPChildConfiguration(
            binding=MCPBinding(role=HOST, agent_role=None, run_id=uuid4(), workspace_id=uuid4()),
            database_path=Path("/tmp/nonexistent-orchestrator.db"), workspace_root=Path("/tmp/nonexistent-orchestrator"),
        )
        sdk = Mock()
        client = BoundMCPClient(configuration=config, _client=sdk)
        arguments = {"workspaceId": str(config.binding.workspace_id), "snapshotId": str(uuid4())}
        call = ToolCall(call_id="host-spoof", name="run_build", arguments_json=json.dumps(arguments))
        for role in AgentRole:
            context = ToolContext(role=role, workspace_id=str(config.binding.workspace_id),
                                  deadline_monotonic=time.monotonic() + 1)
            with self.subTest(role=role), self.assertRaises(MCPClientError) as caught:
                await client.execute(call, arguments, context)
            self.assertEqual(caught.exception.code, "MCP_CLIENT_PERMISSION_DENIED")
        self.assertEqual(sdk.mock_calls, [])


class HostBuildTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = build_fixture.BuildToolTests("test_actual_handler_stores_manifest_and_stream_refs_with_exact_output_schema")
        self.fixture.setUp()
        transfer_cleanups(self, self.fixture)
        self.run = self.fixture.run
        self.registry = self.fixture.fixture.registry

    def dispatcher(self):
        tools = self.fixture.tools()
        return MCPDispatcher(host_binding(self.run), self.registry, handlers=tools.handlers(HOST))

    async def test_build_runs_frozen_snapshot_through_existing_sandbox_and_receipt_store(self):
        result = await self.dispatcher().call_tool("run_build", self.fixture.arguments())
        self.assertIsNone(result.error_code)
        record = self.fixture.outputs.get(self.run.run_id, result.data["executionManifestId"])
        self.assertEqual(record.execution_manifest, self.fixture.fixture.snapshot.execution_manifest())
        self.assertEqual(record.source_artifact_id, self.fixture.fixture.snapshot.artifact_id)
        self.assertEqual(len(self.fixture.fixture.docker.commands("rm")), 1)
        self.assertFalse(Path(self.fixture.fixture.docker.container["Mounts"][0]["Source"]).exists())
        self.assertIsNone(self.fixture.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_nonzero_compilation_is_tool_success_not_product_pass(self):
        self.fixture.fixture.docker.exit_code = 2
        self.fixture.fixture.docker.start_result = CLIResult(returncode=2, stdout=b"", stderr=b"compile failed\n")
        result = await self.dispatcher().call_tool("run_build", self.fixture.arguments())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data["exitCode"], 2)
        self.assertIsNone(self.fixture.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_unrelated_tools_are_hidden_and_direct_calls_are_denied(self):
        dispatcher = self.dispatcher()
        self.assertEqual(tuple(tool.name for tool in dispatcher.list_tools()), HOST_TOOLS)
        for name in ("read_project_file", "write_source_file", "write_test_file", "apply_patch",
                     "run_unit_tests", "run_browser_tests", "run_security_scan"):
            with self.subTest(tool=name), self.assertRaises(MCPProtocolError):
                await dispatcher.call_tool(name, {})
        self.assertEqual(self.fixture.fixture.docker.calls, [])

    async def test_request_cannot_select_role_command_image_or_host_path(self):
        for field in ("role", "agentRole", "principal", "command", "argv", "image", "hostPath"):
            with self.subTest(field=field), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("run_build", self.fixture.arguments(**{field: "ORCHESTRATOR"}))
        self.assertEqual(self.fixture.fixture.docker.calls, [])

    async def test_wrong_workspace_or_unknown_snapshot_never_starts_docker(self):
        for extra in ({"workspaceId": str(uuid4())}, {"snapshotId": str(uuid4())}):
            result = await self.dispatcher().call_tool("run_build", self.fixture.arguments(**extra))
            self.assertIsNotNone(result.error_code)
        self.assertEqual(self.fixture.fixture.docker.calls, [])

    async def test_cancelled_run_keeps_existing_build_denial(self):
        repo = self.fixture.fixture.repository
        with repo._transaction() as connection:
            run = repo.get_run(self.run.run_id).model_copy(update={
                "status": WorkflowStatus.ABORTED, "termination_reason": "USER_CANCELLED",
            })
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (run.status.value, run.model_dump_json(), str(run.run_id)))
        result = await self.dispatcher().call_tool("run_build", self.fixture.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.fixture.fixture.docker.calls, [])

    async def test_real_stdio_unconfigured_build_fails_closed_without_host_execution(self):
        config = MCPChildConfiguration(
            binding=host_binding(self.run), database_path=self.fixture.fixture.repository.database_path,
            workspace_root=self.fixture.fixture.directory / "workspaces", max_call_seconds=15,
        )
        async with open_mcp_client(config) as client:
            with self.assertRaises(MCPClientError) as caught:
                await client.call_tool("run_build", self.fixture.arguments())
            self.assertEqual(caught.exception.tool_error_code, "SANDBOX_ERROR")
        self.fixture.assert_no_receipts()
        self.assertEqual(self.fixture.fixture.docker.calls, [])

    def test_delegation_requires_server_selected_tool_role_and_matching_scope(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            context = MCPExecutionContext(binding=host_binding(self.run), workspace=self.registry.bind(
                self.run.workspace_id, run_id=self.run.run_id, role=role))
            with self.subTest(role=role), self.assertRaises(MCPToolExecutionError):
                delegate_host_context(context, "run_build")
        context = MCPExecutionContext(binding=host_binding(self.run), workspace=self.registry.bind(
            self.run.workspace_id, run_id=self.run.run_id, role=AgentRole.DEVELOPER))
        with self.assertRaises(MCPToolExecutionError):
            delegate_host_context(context, "write_source_file")


class HostUnitReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = unit_fixture.UnitTestOutputStoreTests("runTest")
        self.fixture.setUp()
        transfer_cleanups(self, self.fixture)
        self.record = self.fixture.publish()
        self.artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        self.sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, self.artifacts)
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store)
        browser = BrowserTestTools(self.artifacts, self.sandbox, BrowserTestOutputStore(self.fixture.repository))
        self.reports = TestReportTools(unit, browser)

    def dispatcher(self, run=None):
        return MCPDispatcher(host_binding(run or self.fixture.run), self.fixture.registry,
                             handlers=self.reports.handlers(HOST))

    def arguments(self):
        return {"workspaceId": str(self.fixture.run.workspace_id), "reportRef": self.record.report_ref}

    def test_host_cannot_impersonate_an_agent_workflow_step_in_tracked_executor(self):
        client = BoundMCPClient(configuration=child(self.fixture), _client=Mock())
        with self.assertRaises(TrackedMCPError) as caught:
            TrackedMCPExecutor(client, ToolExecutionStore(self.fixture.repository),
                               workflow_step_id=self.fixture.step.workflow_step_id)
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_CONFIGURATION_INVALID")

    async def test_host_reads_actual_developer_unit_receipt_with_qa_source_grant(self):
        result = await self.dispatcher().call_tool("read_test_report", self.arguments())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data, {"testResult": self.record.report.to_dict()})

    async def test_report_read_rejects_cross_run_reference_and_wrong_workspace(self):
        second, _, _, _ = self.fixture.make_run()
        result = await self.dispatcher(second).call_tool("read_test_report", {
            "workspaceId": str(second.workspace_id), "reportRef": self.record.report_ref})
        self.assertEqual(result.error_code, "REPORT_NOT_FOUND")
        result = await self.dispatcher().call_tool("read_test_report", self.arguments() | {"workspaceId": str(uuid4())})
        self.assertEqual(result.error_code, "PERMISSION_DENIED")

    async def test_missing_snapshot_reader_grant_is_not_a_host_bypass(self):
        with self.fixture.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'",
                               (str(self.fixture.source.artifact_id),))
        result = await self.dispatcher().call_tool("read_test_report", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")

    async def test_report_path_traversal_and_arbitrary_namespace_are_denied(self):
        for reference in (f"artifact://{uuid4()}/../../source/private.py",
                          f"artifact://{self.record.execution_manifest_id}/security-scan-report.json"):
            result = await self.dispatcher().call_tool("read_test_report", self.arguments() | {"reportRef": reference})
            self.assertEqual(result.error_code, "PATH_DENIED")

    async def test_real_stdio_host_discovers_three_tools_and_reads_persisted_report(self):
        async with open_mcp_client(child(self.fixture)) as client:
            self.assertEqual(tuple(tool.name for tool in client.list_tools()), HOST_TOOLS)
            result = await client.call_tool("read_test_report", self.arguments())
            self.assertEqual(result, {"testResult": self.record.report.to_dict()})
            with self.assertRaises(MCPClientError):
                await client.call_tool("write_source_file", {})


class HostSecurityReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = security_fixture.SecurityScanOutputStoreTests("runTest")
        self.fixture.setUp()
        transfer_cleanups(self, self.fixture)
        self.record = self.fixture.publish()
        artifacts = ArtifactStore(self.fixture.repository, self.fixture.registry)
        sandbox = SandboxRuntime(self.fixture.repository, self.fixture.registry, artifacts)
        self.tools = SecurityScanTools(artifacts, sandbox, self.fixture.store)

    def dispatcher(self):
        return MCPDispatcher(host_binding(self.fixture.run), self.fixture.registry,
                             handlers=self.tools.handlers(HOST))

    def arguments(self):
        return {"workspaceId": str(self.fixture.run.workspace_id), "reportRef": self.record.report_ref}

    async def test_host_can_read_security_report_but_cannot_scan(self):
        self.assertEqual(tuple(self.tools.handlers(HOST)), ("read_security_report",))
        result = await self.dispatcher().call_tool("read_security_report", self.arguments())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data, {"securityResult": self.record.report.to_dict()})
        with self.assertRaises(MCPProtocolError):
            await self.dispatcher().call_tool("run_security_scan", {})

    async def test_security_report_missing_source_grant_remains_denied(self):
        with self.fixture.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='SECURITY'",
                               (str(self.fixture.source.artifact_id),))
        result = await self.dispatcher().call_tool("read_security_report", self.arguments())
        self.assertEqual(result.error_code, "PERMISSION_DENIED")

    async def test_real_stdio_reads_existing_security_report_without_scan_configuration(self):
        async with open_mcp_client(child(self.fixture)) as client:
            result = await client.call_tool("read_security_report", self.arguments())
            self.assertEqual(result, {"securityResult": self.record.report.to_dict()})


class HostBrowserReportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = browser_fixture.BrowserToolTests("test_fixed_snapshot_handler_returns_exact_schema_and_stored_trace_refs")
        self.fixture.setUp()
        transfer_cleanups(self, self.fixture)

    async def test_host_routes_browser_report_and_actual_stdio_read(self):
        output = await self.fixture.dispatcher().call_tool("run_browser_tests", self.fixture.arguments())
        record = self.fixture.outputs.get(self.fixture.run.run_id, output.data["executionManifestId"])
        unit = UnitTestTools(self.fixture.artifacts, self.fixture.sandbox, self.fixture.fixture.store)
        reports = TestReportTools(unit, self.fixture.tools())
        dispatcher = MCPDispatcher(host_binding(self.fixture.run), self.fixture.fixture.registry,
                                   handlers=reports.handlers(HOST))
        arguments = {"workspaceId": str(self.fixture.run.workspace_id), "reportRef": record.report_ref}
        result = await dispatcher.call_tool("read_test_report", arguments)
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data, {"testResult": record.report.to_dict()})
        async with open_mcp_client(child(self.fixture.fixture)) as client:
            self.assertEqual(await client.call_tool("read_test_report", arguments), result.data)


if __name__ == "__main__":
    unittest.main()
