"""Real registry/DB/Marker policy and trusted fake callbacks, not product Tools."""

import asyncio
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid1, uuid4

import anyio

from mcp_tools.core.catalog import MAX_JSON_BYTES
from mcp_tools.core.policy import ROLE_TOOL_NAMES
from mcp_tools.runtime import (
    MCPBinding, MCPConfigurationError, MCPDispatcher, MCPExecutionError,
    MCPProtocolError, ToolOutcome,
)
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import OWNER_MARKER
from orchestrator.workspaces.registry import WorkspaceRegistry


class MCPBindingTests(unittest.TestCase):
    def binding(self, **overrides):
        values = dict(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                      run_id=uuid4(), workspace_id=uuid4())
        values.update(overrides)
        return MCPBinding(**values)

    def test_binding_is_frozen_uuid4_and_performs_no_io(self):
        binding = self.binding()
        with self.assertRaises(FrozenInstanceError):
            binding.role = AgentRole.QA
        self.assertNotIn(str(binding.run_id), repr(binding))
        self.assertNotIn(str(binding.workspace_id), repr(binding))

    def test_uuid4_host_strings_are_normalized(self):
        run_id, workspace_id = uuid4(), uuid4()
        binding = self.binding(run_id=str(run_id).upper(), workspace_id=str(workspace_id).upper())
        self.assertEqual((binding.run_id, binding.workspace_id), (run_id, workspace_id))

    def test_role_mismatch_or_non_enum_role_is_rejected(self):
        for overrides in (
            {"agent_role": AgentRole.QA}, {"role": "DEVELOPER"},
            {"agent_role": "DEVELOPER"}, {"role": None},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(MCPConfigurationError):
                self.binding(**overrides)

    def test_non_uuid4_host_identifiers_are_rejected_without_echo(self):
        for value in (uuid1(), "not-an-id-or-secret", True, None, 42):
            for field in ("run_id", "workspace_id"):
                with self.subTest(field=field, value=value), self.assertRaises(MCPConfigurationError) as raised:
                    self.binding(**{field: value})
                self.assertEqual(str(raised.exception), "MCP_CONFIGURATION_INVALID")

    def test_constructor_and_discovery_perform_no_registry_io(self):
        registry = Mock()
        dispatcher = MCPDispatcher(self.binding(), registry)
        self.assertEqual(tuple(tool.name for tool in dispatcher.list_tools()), ROLE_TOOL_NAMES[AgentRole.DEVELOPER])
        self.assertFalse(dispatcher.is_implemented("read_project_file"))
        self.assertFalse(dispatcher.is_implemented({}))
        self.assertEqual(registry.mock_calls, [])

    def test_exact_role_ordered_catalog_and_planner_empty(self):
        registry = Mock()
        for role in AgentRole:
            with self.subTest(role=role):
                dispatcher = MCPDispatcher(self.binding(role=role, agent_role=role), registry)
                self.assertEqual(tuple(tool.name for tool in dispatcher.list_tools()), ROLE_TOOL_NAMES[role])
        self.assertEqual(registry.mock_calls, [])

    def test_handler_registry_rejects_unknown_or_role_forbidden_names(self):
        for handlers in ({"arbitrary_shell": Mock()}, {"write_test_file": Mock()}, {"read_project_file": None}, []):
            with self.subTest(handlers=handlers), self.assertRaises(MCPConfigurationError):
                MCPDispatcher(self.binding(), Mock(), handlers=handlers)

    def test_host_timeout_settings_are_bounded_finite_and_not_bool(self):
        for value in (True, None, 0, -1, 601, float("nan"), float("inf"), "60"):
            with self.subTest(value=value), self.assertRaises(MCPConfigurationError):
                MCPDispatcher(self.binding(), Mock(), max_call_seconds=value)

    def test_tool_outcome_requires_data_xor_known_error_code(self):
        for values in ({}, {"data": {}, "error_code": "TIMEOUT"}, {"data": []}, {"error_code": "arbitrary secret"}):
            with self.subTest(values=values), self.assertRaises(MCPConfigurationError):
                ToolOutcome(**values)
        self.assertNotIn("private-content", repr(ToolOutcome(data={"value": "private-content"})))


class MCPDispatcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-mcp-runtime-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "runtime.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base / str(self.run.workspace_id)
        self.record = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id, root_path=str(self.root))
        self.repository.create_run(self.run, (), (), workspace=self.record)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)

    def dispatcher(self, role=AgentRole.DEVELOPER, *, handlers=None, **settings):
        return MCPDispatcher(MCPBinding(
            role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id,
        ), self.registry, handlers=handlers, **settings)

    def arguments(self, **overrides):
        values = {"workspaceId": str(self.run.workspace_id), "path": "source/signup.py"}
        values.update(overrides)
        return values

    def read_result(self, content="password = request.password\n", path="source/signup.py"):
        return {"path": path, "content": content,
                "sha256": hashlib.sha256(content.encode()).hexdigest(), "sizeBytes": len(content.encode())}

    async def test_authorized_undeveloped_tool_is_explicit_error_not_fake_success(self):
        outcome = await self.dispatcher().call_tool("read_project_file", self.arguments())
        self.assertIsNone(outcome.data)
        self.assertEqual(outcome.error_code, MCPExecutionError.TOOL_NOT_IMPLEMENTED.value)
        self.assertEqual(self.repository.get_run(self.run.run_id), self.run)

    async def test_all_role_stubs_have_valid_contracts_and_do_not_create_product_files(self):
        values = {
            "read_project_file": self.arguments(),
            "write_source_file": self.arguments(content="# source\n"),
            "write_test_file": self.arguments(path="outputs/qa/test_signup.py", content="# test\n"),
            "apply_patch": {"workspaceId": str(self.run.workspace_id), "patch": "--- a/app.py\n+++ b/app.py\n", "baseSnapshotSha256": "a" * 64},
            "run_build": {"workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4())},
            "run_unit_tests": {"workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4()), "testScope": "unit"},
            "run_browser_tests": {"workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4()), "testSuite": "signup"},
            "run_security_scan": {"workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4()), "scannerProfile": "default"},
            "read_test_report": {"workspaceId": str(self.run.workspace_id), "reportRef": f"artifact://{uuid4()}/report.json"},
            "read_security_report": {"workspaceId": str(self.run.workspace_id), "reportRef": f"artifact://{uuid4()}/report.json"},
        }
        for role in AgentRole:
            dispatcher = self.dispatcher(role)
            for name in ROLE_TOOL_NAMES[role]:
                with self.subTest(role=role, name=name):
                    result = await dispatcher.call_tool(name, values[name])
                    self.assertEqual(result.error_code, "TOOL_NOT_IMPLEMENTED")
        self.assertFalse((self.root / "source/signup.py").exists())
        self.assertFalse((self.root / "outputs/qa/test_signup.py").exists())

    async def test_direct_forbidden_tool_calls_are_rejected_despite_unknown_discovery(self):
        for role, name in ((AgentRole.QA, "write_source_file"), (AgentRole.SECURITY, "apply_patch"), (AgentRole.PLANNER, "read_project_file")):
            with self.subTest(role=role), self.assertRaises(MCPProtocolError) as raised:
                await self.dispatcher(role).call_tool(name, {})
            self.assertEqual(raised.exception.code, -32602)
            self.assertEqual(str(raised.exception), "Invalid parameters")

    async def test_unknown_tool_name_does_not_echo_or_touch_registry(self):
        for name in ("arbitrary_shell_secret", None, 1, {}):
            with self.subTest(name=name), patch.object(self.registry, "get_record") as lookup:
                with self.assertRaises(MCPProtocolError):
                    await self.dispatcher().call_tool(name, {})
                lookup.assert_not_called()

    async def test_input_schema_rejects_extra_fields_role_root_run_and_wrong_types(self):
        for overrides in ({"role": "DEVELOPER"}, {"agentRole": "QA"}, {"rootPath": str(self.root)}, {"runId": str(self.run.run_id)}, {"path": 3}, {"workspaceId": None}):
            with self.subTest(overrides=overrides), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("read_project_file", self.arguments(**overrides))

    async def test_input_must_be_finite_bounded_json_object(self):
        for arguments in (None, [], {"workspaceId": str(self.run.workspace_id), "path": float("nan")},
                          {"workspaceId": str(self.run.workspace_id), "path": "source/" + "a" * MAX_JSON_BYTES}):
            with self.subTest(arguments_type=type(arguments)), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("read_project_file", arguments)

    async def test_non_uuid4_workspace_input_is_protocol_error(self):
        for value in (str(uuid1()), "bad-id", 4, True):
            with self.subTest(value=value), self.assertRaises(MCPProtocolError):
                await self.dispatcher().call_tool("read_project_file", self.arguments(workspaceId=value))

    async def test_cross_workspace_is_denied_before_any_registry_lookup(self):
        with patch.object(self.registry, "get_record") as lookup:
            outcome = await self.dispatcher().call_tool("read_project_file", self.arguments(workspaceId=str(uuid4())))
        lookup.assert_not_called()
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")

    async def test_uppercase_known_workspace_uuid_normalizes_without_mutating_input(self):
        seen = []
        async def handler(context, arguments):
            seen.append(arguments["workspaceId"])
            return self.read_result()
        arguments = self.arguments(workspaceId=str(self.run.workspace_id).upper())
        outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", arguments)
        self.assertIsNotNone(outcome.data)
        self.assertEqual(seen, [str(self.run.workspace_id)])
        self.assertEqual(arguments["workspaceId"], str(self.run.workspace_id).upper())

    async def test_path_traversal_absolute_secret_and_wrong_region_are_denied(self):
        for path in ("../../etc/passwd", "/etc/passwd", "source/../signup.py", "source/.env", "source/.ssh/id_rsa", "source\\signup.py", "C:/signup.py", "planning/plan.json", "outputs/qa/test_signup.py"):
            with self.subTest(path=path):
                result = await self.dispatcher().call_tool("write_source_file", self.arguments(path=path, content="# source\n"))
                self.assertEqual(result.error_code, "PATH_DENIED")

    async def test_qa_test_write_is_only_qa_output_not_source_security_or_snapshot(self):
        for path in ("source/test_signup.py", "outputs/security/test_signup.py", "snapshots/test_signup.py"):
            with self.subTest(path=path):
                result = await self.dispatcher(AgentRole.QA).call_tool("write_test_file", self.arguments(path=path, content="# test\n"))
                self.assertEqual(result.error_code, "PATH_DENIED")

    async def test_report_reference_must_be_internal_artifact_not_host_or_url(self):
        for reference in ("/tmp/report.json", "https://example.test/report.json", "file:///tmp/report.json", f"artifact://{uuid4()}/../report.json", f"artifact://{uuid4()}/report.json?token=secret", "artifact://bad-id/report.json"):
            with self.subTest(reference=reference):
                result = await self.dispatcher(AgentRole.QA).call_tool("read_test_report", {"workspaceId": str(self.run.workspace_id), "reportRef": reference})
                self.assertEqual(result.error_code, "PATH_DENIED")

    async def test_credentials_in_source_are_rejected_not_rewritten(self):
        calls = []
        async def handler(context, arguments):
            calls.append(arguments)
        dispatcher = self.dispatcher(handlers={"write_source_file": handler})
        for source in ('password = "private-password"', 'api_key = "private-key"', "# Bearer abcdef123456"):
            with self.subTest(source=source):
                outcome = await dispatcher.call_tool("write_source_file", self.arguments(content=source))
                self.assertEqual(outcome.error_code, "SECRET_DENIED")
                self.assertNotIn(source, repr(outcome))
        self.assertEqual(calls, [])

    async def test_normal_source_code_is_preserved_exactly_in_input_and_output(self):
        source = "password = request.password\n# 한글\n"
        seen = []
        async def writer(context, arguments):
            seen.append(arguments["content"])
            return {"path": arguments["path"], "sha256": hashlib.sha256(source.encode()).hexdigest(), "sizeBytes": len(source.encode()), "changed": True}
        outcome = await self.dispatcher(handlers={"write_source_file": writer}).call_tool("write_source_file", self.arguments(content=source))
        self.assertEqual(seen, [source])
        self.assertIsNotNone(outcome.data)
        async def reader(context, arguments):
            return self.read_result(source)
        outcome = await self.dispatcher(handlers={"read_project_file": reader}).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.data["content"], source)
        self.assertEqual(outcome.data["sha256"], hashlib.sha256(source.encode()).hexdigest())

    async def test_optional_expected_hash_remains_optional(self):
        for additions in ({}, {"expectedSha256": "a" * 64}):
            with self.subTest(additions=additions):
                outcome = await self.dispatcher().call_tool("write_source_file", self.arguments(content="# source\n", **additions))
                self.assertEqual(outcome.error_code, "TOOL_NOT_IMPLEMENTED")

    async def test_one_mib_source_survives_escaped_json_envelope_overhead(self):
        source = "\n" * 1_048_576
        async def handler(context, arguments):
            return self.read_result(source)
        outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.data["content"], source)
        self.assertEqual(outcome.data["sizeBytes"], 1_048_576)
        outcome = await self.dispatcher().call_tool("write_source_file", self.arguments(content=source))
        self.assertEqual(outcome.error_code, "TOOL_NOT_IMPLEMENTED")

    async def test_handler_receives_same_role_run_workspace_capability(self):
        seen = []
        async def handler(context, arguments):
            seen.append(context)
            return self.read_result()
        outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        self.assertIsNotNone(outcome.data)
        context = seen[0]
        self.assertEqual(context.binding.run_id, self.run.run_id)
        self.assertEqual(context.workspace.workspace_id, self.run.workspace_id)
        self.assertIs(context.workspace.role, AgentRole.DEVELOPER)
        self.assertNotIn(str(self.root), repr(context))

    async def test_unprovisioned_workspace_is_not_implicitly_created(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 Run")
        other_root = self.base / str(other.workspace_id)
        self.repository.create_run(other, (), (), workspace=WorkspaceRecord(workspace_id=other.workspace_id, run_id=other.run_id, root_path=str(other_root)))
        dispatcher = MCPDispatcher(MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER, run_id=other.run_id, workspace_id=other.workspace_id), self.registry)
        outcome = await dispatcher.call_tool("read_project_file", {"workspaceId": str(other.workspace_id), "path": "source/signup.py"})
        self.assertEqual(outcome.error_code, "WORKSPACE_UNAVAILABLE")
        self.assertFalse(other_root.exists())

    async def test_marker_change_between_calls_is_revalidated_and_denied(self):
        dispatcher = self.dispatcher()
        first = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(first.error_code, "TOOL_NOT_IMPLEMENTED")
        marker = self.root / OWNER_MARKER
        value = json.loads(marker.read_text())
        value["runId"] = str(uuid4())
        marker.write_text(json.dumps(value))
        outcome = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")

    async def test_database_identity_change_is_denied_after_binding(self):
        dispatcher = self.dispatcher()
        value = self.record.model_dump(mode="json", by_alias=True)
        value["runId"] = str(uuid4())
        with self.repository._connection() as connection:
            connection.execute("DROP TRIGGER workspaces_no_update")
            connection.execute("UPDATE workspaces SET payload_json=? WHERE workspace_id=?", (json.dumps(value), str(self.run.workspace_id)))
            connection.commit()
        outcome = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "PERMISSION_DENIED")

    async def test_registry_generic_error_is_not_leaked(self):
        with patch.object(self.registry, "get_record", side_effect=RuntimeError("/private/root password=do-not-leak")):
            outcome = await self.dispatcher().call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "WORKSPACE_UNAVAILABLE")
        self.assertNotIn("private", repr(outcome))

    async def test_mutating_original_handler_map_does_not_change_dispatch(self):
        async def first(context, arguments):
            return self.read_result("original\n")
        async def replacement(context, arguments):
            return self.read_result("replacement\n")
        handlers = {"read_project_file": first}
        dispatcher = self.dispatcher(handlers=handlers)
        handlers["read_project_file"] = replacement
        self.assertTrue(dispatcher.is_implemented("read_project_file"))
        outcome = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.data["content"], "original\n")

    async def test_handler_failure_does_not_leak_raw_exception_or_source(self):
        async def handler(context, arguments):
            raise RuntimeError("password=private-key /Users/private/root")
        outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "TOOL_EXECUTION_FAILED")
        self.assertNotIn("private", repr(outcome))

    async def test_sync_handler_does_not_count_as_success(self):
        outcome = await self.dispatcher(handlers={"read_project_file": lambda context, args: self.read_result()}).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "TOOL_EXECUTION_FAILED")

    async def test_output_schema_validation_rejects_extra_fields_bool_and_nonfinite_json(self):
        for output in (self.read_result() | {"verdict": "SUCCESS"}, self.read_result() | {"sizeBytes": True}, self.read_result() | {"sizeBytes": float("inf")}, "not an object"):
            async def handler(context, arguments):
                return output
            with self.subTest(output_type=type(output)):
                outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
                self.assertEqual(outcome.error_code, "TOOL_OUTPUT_INVALID")

    async def test_source_output_with_credential_literal_is_rejected_not_hash_rewritten(self):
        async def handler(context, arguments):
            return self.read_result('password = "private-password"\n')
        outcome = await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "TOOL_OUTPUT_INVALID")
        self.assertNotIn("private-password", repr(outcome))

    async def test_nonzero_product_build_exit_is_successful_tool_data_not_execution_error(self):
        async def handler(context, arguments):
            return {"exitCode": 1, "durationMs": 17, "executionManifestId": str(uuid4())}
        outcome = await self.dispatcher(handlers={"run_build": handler}).call_tool("run_build", {"workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4())})
        self.assertIsNone(outcome.error_code)
        self.assertEqual(outcome.data["exitCode"], 1)
        self.assertNotIn("verdict", outcome.data)

    async def test_handler_timeout_reaps_cooperative_handler_without_retry(self):
        calls = []
        stopped = asyncio.Event()
        async def handler(context, arguments):
            calls.append(1)
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        outcome = await self.dispatcher(handlers={"read_project_file": handler}, max_call_seconds=0.01).call_tool("read_project_file", self.arguments())
        self.assertEqual(outcome.error_code, "TIMEOUT")
        self.assertTrue(stopped.is_set())
        self.assertEqual(calls, [1])

    async def test_caller_cancellation_propagates_after_cooperative_handler_cleanup(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def handler(context, arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)
                stopped.set()
        task = asyncio.create_task(self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_repeated_cancellation_does_not_interrupt_handler_cleanup(self):
        started, cleaning, release, stopped = (asyncio.Event() for _ in range(4))
        async def handler(context, arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                stopped.set()
        task = asyncio.create_task(self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments()))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.wait_for(cleaning.wait(), 1)
        task.cancel()
        await asyncio.sleep(0)
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(stopped.is_set())

    async def test_anyio_level_cancellation_allows_handler_finally_to_finish(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def handler(context, arguments):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                stopped.set()
        async def invoke():
            await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        async with anyio.create_task_group() as group:
            group.start_soon(invoke)
            await started.wait()
            group.cancel_scope.cancel()
        self.assertTrue(stopped.is_set())

    async def test_runtime_does_not_write_trace_or_change_run_product_state(self):
        async def handler(context, arguments):
            return self.read_result()
        before = self.repository.get_run(self.run.run_id)
        with self.repository._connection() as connection:
            before_count = connection.execute("SELECT count(*) FROM trace_events").fetchone()[0]
        await self.dispatcher(handlers={"read_project_file": handler}).call_tool("read_project_file", self.arguments())
        self.assertEqual(self.repository.get_run(self.run.run_id), before)
        with self.repository._connection() as connection:
            count = connection.execute("SELECT count(*) FROM trace_events").fetchone()[0]
        self.assertEqual(count, before_count)
