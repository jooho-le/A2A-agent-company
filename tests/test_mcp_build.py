"""Step25 Build plumbing with real Source/SQLite and a fake Docker transport.

The stdio tests use the real SDK child, but intentionally unavailable Docker.
No generated product code is run on the Host or claimed tested in a Container.
"""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

import test_sandbox_runtime as sandbox_fixture
from mcp_tools.client import MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client
from mcp_tools.core.catalog import get_tool_contract
from mcp_tools.runtime import MCPBinding, MCPConfigurationError, MCPDispatcher, MCPProtocolError
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import BuildConfiguration, decode_build_configuration
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError
from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.sandbox.contracts import CLIResult, SandboxError, SandboxErrorCode


class BuildToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse its fixture setup, not its test methods or imported TestCase.
        self.fixture = sandbox_fixture.SandboxRuntimeTests("test_product_nonzero_exit_is_a_result_not_infrastructure_error")
        self.fixture.setUp()
        # Its IsolatedAsyncioTestCase runner is intentionally never started;
        # transfer its sync fixture cleanup to this actual test's runner.
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.run = self.fixture.run_record
        self.outputs = BuildOutputStore(self.fixture.repository)
        self.configuration = BuildConfiguration(profile=self.fixture.profile)

    def binding(self, role=AgentRole.DEVELOPER):
        return MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def tools(self, *, configuration=True, max_call_seconds=60, sandbox=None):
        return BuildTools(self.fixture.store, sandbox or self.fixture.runtime, self.outputs,
                          configuration=self.configuration if configuration else None,
                          max_call_seconds=max_call_seconds)

    def dispatcher(self, role=AgentRole.DEVELOPER, **options):
        tools = self.tools(**options)
        return MCPDispatcher(self.binding(role), self.fixture.registry, handlers=tools.handlers(role),
                             max_call_seconds=options.get("max_call_seconds", 60))

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id),
                "snapshotId": str(self.fixture.snapshot.artifact_id), **extra}

    def assert_no_receipts(self):
        with self.fixture.repository._connection() as connection:
            tables = connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            for row in tables:
                if row[0] == "build_execution_records":
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM build_execution_records").fetchone()[0], 0)

    async def test_actual_handler_stores_manifest_and_stream_refs_with_exact_output_schema(self):
        previous = self.fixture.repository.get_run(self.run.run_id)
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertIsNone(outcome.error_code)
        get_tool_contract("run_build").validate_output(outcome.data)
        record = self.outputs.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertEqual(self.outputs.read_output(self.run.run_id, outcome.data["stdoutRef"]), "build output\n")
        self.assertEqual(self.outputs.read_output(self.run.run_id, outcome.data["stderrRef"]), "")
        self.assertNotEqual(str(record.execution_id), outcome.data["executionManifestId"])
        self.assertEqual(outcome.data["exitCode"], 0)
        self.assertEqual(set(outcome.data), {"exitCode", "durationMs", "stdoutRef", "stderrRef", "executionManifestId"})
        self.assertEqual(self.fixture.repository.get_run(self.run.run_id), previous)
        self.assertEqual(self.fixture.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)
        self.assertFalse(Path(self.fixture.docker.container["Mounts"][0]["Source"]).exists())

    async def test_compilation_nonzero_is_successful_tool_response_not_fake_pass(self):
        self.fixture.docker.exit_code = 2
        self.fixture.docker.start_result = CLIResult(returncode=2, stdout=b"", stderr=b"SyntaxError: invalid source\n")
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.data["exitCode"], 2)
        self.assertIn("SyntaxError", self.outputs.read_output(self.run.run_id, outcome.data["stderrRef"]))
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_live_working_copy_is_not_the_build_input(self):
        (self.fixture.source / "signup.py").write_text("print('mutable')\n", encoding="utf-8")
        def frozen(container):
            root = Path(container["Mounts"][0]["Source"])
            self.assertEqual((root / "signup.py").read_text(), "print('fixture signup')\n")
            self.assertNotEqual(root, self.fixture.source)
        self.fixture.docker.start_hook = frozen
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertIsNone(outcome.error_code)

    async def test_secret_output_is_redacted_before_storage_and_not_in_tool_output_or_trace(self):
        self.fixture.docker.start_result = CLIResult(returncode=0, stdout=b"password=fixture-password\nOK\n",
                                                   stderr=b"Authorization: Bearer private-token\n")
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        for key, secret in (("stdoutRef", "fixture-password"), ("stderrRef", "private-token")):
            self.assertNotIn(secret, self.outputs.read_output(self.run.run_id, outcome.data[key]))
        record = self.outputs.get(self.run.run_id, outcome.data["executionManifestId"])
        self.assertNotIn("OK", repr(record))
        self.assertNotIn("fixture-password", json.dumps(outcome.data))
        events, _ = self.fixture.repository.list_events(self.run.run_id, limit=1000, offset=0)
        self.assertNotIn("private-token", json.dumps([event.model_dump(mode="json") for event in events]))

    async def test_model_cannot_choose_command_image_endpoint_or_host_path(self):
        for field in ("argv", "command", "image", "dockerEndpoint", "hostPath", "role", "profile"):
            with self.subTest(field=field), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("run_build", self.arguments(**{field: "untrusted"}))
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_non_developer_roles_have_no_build_handler_or_call_permission(self):
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            self.assertEqual(dict(self.tools().handlers(role)), {})
            with self.subTest(role=role), self.assertRaises(MCPProtocolError):
                await self.dispatcher(role).call_tool("run_build", self.arguments())
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_unregistered_snapshot_and_wrong_workspace_stop_before_docker(self):
        for extra in ({"snapshotId": str(uuid4())}, {"workspaceId": str(uuid4())}):
            outcome = await self.dispatcher().call_tool("run_build", self.arguments(**extra))
            self.assertIsNotNone(outcome.error_code)
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_missing_host_profile_is_config_error_not_unimplemented_or_success(self):
        outcome = await self.dispatcher(configuration=False).call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "SANDBOX_ERROR")
        self.assertTrue(self.dispatcher(configuration=False).is_implemented("run_build"))
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_cancelled_run_before_call_does_not_start_container(self):
        with self.fixture.repository._transaction() as connection:
            run = self.fixture.repository.get_run(self.run.run_id).model_copy(update={
                "status": WorkflowStatus.ABORTED, "termination_reason": "USER_CANCELLED",
            })
            connection.execute("UPDATE workflow_runs SET status=?, payload_json=? WHERE run_id=?",
                               (run.status.value, run.model_dump_json(), str(run.run_id)))
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_run_cancelled_during_build_is_not_published_after_container_cleanup(self):
        def cancel(_container):
            with self.fixture.repository._transaction() as connection:
                run = self.fixture.repository.get_run(self.run.run_id).model_copy(update={
                    "status": WorkflowStatus.ABORTED, "termination_reason": "USER_CANCELLED",
                })
                connection.execute("UPDATE workflow_runs SET status=?, payload_json=? WHERE run_id=?",
                                   (run.status.value, run.model_dump_json(), str(run.run_id)))
        self.fixture.docker.start_hook = cancel
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "BUILD_EXECUTION_ERROR")
        self.assert_no_receipts()
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)

    async def test_timeout_is_safe_execution_error_and_cleanup_finishes_without_receipt(self):
        self.fixture.docker.start_error = SandboxError(SandboxErrorCode.TIMEOUT)
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "TIMEOUT")
        self.assert_no_receipts()
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)

    async def test_start_failure_is_not_a_compilation_result(self):
        self.fixture.docker.start_error = SandboxError(SandboxErrorCode.EXECUTION)
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "BUILD_EXECUTION_ERROR")
        self.assert_no_receipts()

    async def test_oom_is_infrastructure_error_not_a_product_compilation_exit(self):
        self.fixture.docker.after_start_mutation = lambda value: value["State"].update(OOMKilled=True)
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "BUILD_EXECUTION_ERROR")
        self.assert_no_receipts()
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)

    async def test_storage_failure_after_build_cleanup_does_not_return_a_success_or_raw_error(self):
        with patch.object(self.outputs, "publish", side_effect=BuildStoreError("private SQL/Source detail")):
            outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "BUILD_EXECUTION_ERROR")
        self.assertIsNone(outcome.data)
        self.assert_no_receipts()
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)

    async def test_cleanup_failure_preserves_staging_and_has_no_success_record(self):
        self.fixture.docker.rm_result = CLIResult(returncode=1, stdout=b"", stderr=b"private daemon failure")
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "SANDBOX_ERROR")
        self.assertTrue(Path(self.fixture.docker.container["Mounts"][0]["Source"]).exists())
        self.assert_no_receipts()

    async def test_repeated_server_cancellation_drains_owned_cleanup_and_has_no_receipt(self):
        self.fixture.docker.block_start = True
        self.fixture.docker.release_rm = asyncio.Event()
        task = asyncio.create_task(self.dispatcher().call_tool("run_build", self.arguments()))
        await asyncio.wait_for(self.fixture.docker.start_entered.wait(), 5)
        task.cancel()
        await asyncio.wait_for(self.fixture.docker.rm_entered.wait(), 5)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        self.fixture.docker.release_rm.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)
        self.assertFalse(Path(self.fixture.docker.container["Mounts"][0]["Source"]).exists())
        self.assert_no_receipts()

    async def test_default_timeout_is_narrowed_to_leave_owned_cleanup_budget(self):
        tools = self.tools()
        self.assertEqual(tools._profile().limits.timeout_seconds, 39)
        self.assertEqual(self.configuration.profile.limits.timeout_seconds, 60)
        outcome = await self.dispatcher().call_tool("run_build", self.arguments())
        self.assertIsNone(outcome.error_code)
        self.assertTrue(all(call[1] <= 39 for call in self.fixture.docker.calls))

    async def test_insufficient_cleanup_budget_fails_before_docker(self):
        outcome = await self.dispatcher(max_call_seconds=1).call_tool("run_build", self.arguments())
        self.assertEqual(outcome.error_code, "SANDBOX_ERROR")
        self.assertEqual(self.fixture.docker.calls, [])

    async def test_host_configuration_is_serialized_only_in_trusted_launcher_not_repr(self):
        configuration = MCPChildConfiguration(binding=self.binding(),
            database_path=self.fixture.repository.database_path, workspace_root=self.fixture.registry.base_path,
            build_configuration=self.configuration)
        args = child_parameters(configuration).args
        decoded = decode_build_configuration(args[args.index("--build-configuration-json") + 1])
        self.assertEqual(decoded, self.configuration)
        self.assertNotIn("print(1)", repr(configuration))
        with self.assertRaises(MCPClientError):
            replace(configuration, build_configuration={"argv": ["untrusted"]})

    async def test_real_stdio_has_implemented_build_but_requires_host_profile(self):
        configuration = MCPChildConfiguration(binding=self.binding(),
            database_path=self.fixture.repository.database_path, workspace_root=self.fixture.registry.base_path)
        async with open_mcp_client(configuration) as client:
            listing = await client._client.session.list_tools()
            tool = next(item for item in listing.tools if item.name == "run_build")
            self.assertTrue(tool.meta["a2a-agent-company/implemented"])
            result = await client._client.session.call_tool("run_build", self.arguments())
            self.assertTrue(result.is_error)
            self.assertEqual([item.text for item in result.content], ["SANDBOX_ERROR"])
        self.assert_no_receipts()

    async def test_real_stdio_configured_build_unavailable_docker_never_falls_back_to_host(self):
        config = replace(self.configuration, docker_endpoint="unix://" + str(self.fixture.directory / "absent-docker.sock"))
        configuration = MCPChildConfiguration(binding=self.binding(),
            database_path=self.fixture.repository.database_path, workspace_root=self.fixture.registry.base_path,
            build_configuration=config)
        async with open_mcp_client(configuration) as client:
            with self.assertRaises(MCPClientError) as raised:
                await client.call_tool("run_build", self.arguments())
            self.assertEqual(raised.exception.code, "MCP_CLIENT_TOOL_FAILED")
        self.assert_no_receipts()
        self.assertFalse((self.fixture.root / ".sandbox").exists())

    async def test_constructor_is_inert_and_does_not_create_receipt_table_or_probe_docker(self):
        with patch.object(self.fixture.runtime, "bind", side_effect=AssertionError("side effect")), \
             patch.object(self.fixture.store, "bind", side_effect=AssertionError("side effect")):
            tools = self.tools()
            self.assertEqual(set(tools.handlers(AgentRole.DEVELOPER)), {"run_build"})
            self.assertNotIn("print(1)", repr(tools))
        self.assertEqual(self.fixture.docker.calls, [])
        self.assert_no_receipts()

    async def test_invalid_host_build_dependencies_and_call_limits_are_rejected(self):
        for value in (False, 0, -1, 601, float("nan"), float("inf"), "60"):
            with self.subTest(value=repr(value)), self.assertRaises(MCPConfigurationError):
                self.tools(max_call_seconds=value)


if __name__ == "__main__":
    unittest.main()
