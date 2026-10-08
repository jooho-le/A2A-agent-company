"""Real journal/Git/SQLite/dispatch with fake daemon; no Host product code."""

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from mcp_types import CallToolResult, TextContent

import test_sandbox_runtime as sandbox_fixture
from agents.llm.contracts import ToolCall, ToolContext
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, open_mcp_client
from mcp_tools.execution_policy import RetrySafetyConfirmation, arguments_sha256
from mcp_tools.execution_runtime import TrackedMCPError, TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore, ToolEvidenceStoreError
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import BuildConfiguration
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError
from mcp_tools.tools.browser_store import BrowserTestOutputStore, BrowserStoreError
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security_store import SecurityScanOutputStore, SecurityStoreError
from mcp_tools.tools.unit_store import UnitTestOutputStore, UnitTestStoreError
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.retry_policy import RetryDecision
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.sandbox.contracts import CLIResult


class DispatcherSession:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.calls = []
        self.errors = []
        self.delays = []
        self.before_reply = None
        self.result_override = None

    async def call_tool(self, name, arguments, **options):
        self.calls.append((name, json.loads(json.dumps(arguments)), options))
        if self.delays:
            await asyncio.sleep(self.delays.pop(0))
        if self.errors:
            code = self.errors.pop(0)
            if isinstance(code, BaseException):
                raise code
            return CallToolResult(isError=True, content=[TextContent(type="text", text=code)])
        if self.result_override is not None:
            return CallToolResult(content=[TextContent(type="text", text="TOOL_COMPLETED")],
                                  structuredContent=self.result_override)
        result = await self.dispatcher.call_tool(name, arguments)
        if self.before_reply is not None:
            await self.before_reply(name, arguments)
        if result.error_code:
            return CallToolResult(isError=True, content=[TextContent(type="text", text=result.error_code)])
        return CallToolResult(content=[TextContent(type="text", text="TOOL_COMPLETED")], structuredContent=result.data)


class ExecutionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = sandbox_fixture.SandboxRuntimeTests("runTest")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.run = self.fixture.run_record
        self.binding = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                                  run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.configuration = MCPChildConfiguration(binding=self.binding,
            database_path=self.fixture.repository.database_path, workspace_root=self.fixture.registry.base_path,
            build_configuration=BuildConfiguration(profile=self.fixture.profile))
        self.outputs = BuildOutputStore(self.fixture.repository)
        self.build = BuildTools(self.fixture.store, self.fixture.runtime, self.outputs,
                               configuration=self.configuration.build_configuration)
        files = FileTools(self.fixture.store)
        self.dispatcher = MCPDispatcher(self.binding, self.fixture.registry,
            handlers={**self.build.handlers(AgentRole.DEVELOPER), **files.handlers(AgentRole.DEVELOPER)})
        self.session = DispatcherSession(self.dispatcher)
        self.sdk = type("SDK", (), {"session": self.session})()
        self.client = BoundMCPClient(configuration=self.configuration, _client=self.sdk)
        self.store = ToolExecutionStore(self.fixture.repository)
        self.executor = self.make_executor()
        self.arguments = {"workspaceId": str(self.run.workspace_id), "snapshotId": str(self.fixture.snapshot.artifact_id)}

    def make_executor(self, **kwargs):
        return TrackedMCPExecutor(self.client, self.store, workflow_step_id=self.fixture.step.workflow_step_id,
                                  retry_delay_seconds=0, **kwargs)

    def record(self, executor=None):
        return self.store.get(self.binding, (executor or self.executor).logical_call_ids[-1])

    def mutate_run(self, **changes):
        original = self.fixture.repository.get_run(self.run.run_id)
        updated = type(original).model_validate({**original.model_dump(), **changes})
        with self.fixture.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (updated.status.value, updated.model_dump_json(), str(updated.run_id)))

    def proof(self, record, token, _error):
        return RetrySafetyConfirmation(logical_call_id=record.logical_call_id, attempt=token.attempt,
            input_sha256=record.input_sha256, cleanup_complete=True, result_known_not_applied=True)

    async def test_build_result_has_actual_receipt_and_compatible_evidence(self):
        before = self.fixture.repository.get_run(self.run.run_id)
        result = await self.executor.invoke("run_build", self.arguments)
        evidence = result.to_tool_evidence()
        self.assertEqual(evidence.outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(evidence.execution_manifest, self.fixture.snapshot.execution_manifest())
        self.assertEqual(evidence.execution_id, result.record.logical_call_id)
        self.assertEqual(evidence.retries_used, 0)
        self.assertEqual(self.store.read_evidence(self.binding, evidence.evidence_ref), result.record.to_dict())
        self.assertEqual(self.fixture.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.fixture.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(len(self.session.calls), 1)

    async def test_code_failure_is_completed_tool_not_retry_or_product_success(self):
        self.fixture.docker.exit_code = 2
        self.fixture.docker.start_result = CLIResult(returncode=2, stdout=b"", stderr=b"SyntaxError\n")
        result = await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(result.data["exitCode"], 2)
        self.assertEqual(result.to_tool_evidence().outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(len(self.session.calls), 1)
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_two_transient_retries_keep_one_identity_and_same_arguments(self):
        self.session.errors = ["RESOURCE_BUSY", "PROCESS_STARTUP_FAILURE"]
        result = await self.executor.invoke("run_build", self.arguments)
        evidence = result.to_tool_evidence()
        self.assertEqual([item.attempt for item in evidence.attempts], [0, 1, 2])
        self.assertEqual([item.outcome.value for item in evidence.attempts], ["UNVERIFIED", "UNVERIFIED", "PASS"])
        self.assertEqual(evidence.retries_used, 2)
        self.assertEqual(len(set(item.evidence_ref for item in evidence.attempts)), 3)
        self.assertEqual(self.executor.logical_call_ids, (result.record.logical_call_id,))
        self.assertTrue(all(args == self.arguments for _name, args, _options in self.session.calls))
        timeouts = [options["read_timeout_seconds"] for _name, _args, options in self.session.calls]
        self.assertEqual(timeouts, sorted(timeouts, reverse=True))

    async def test_three_transient_failures_stop_without_fourth_call_or_verdict(self):
        self.session.errors = ["RESOURCE_BUSY"] * 4
        with self.assertRaises(TrackedMCPError) as caught:
            await self.executor.call_tool("run_build", self.arguments)
        evidence = self.record().to_tool_evidence()
        self.assertTrue(evidence.retries_exhausted)
        self.assertEqual(len(self.session.calls), 3)
        self.assertEqual(caught.exception.logical_call_id, evidence.execution_id)
        self.assertIsNone(self.fixture.repository.get_run(self.run.run_id).verdict)

    async def test_confirmed_timeout_can_retry_exact_nonwrite_call(self):
        self.session.errors = ["TIMEOUT"]
        executor = self.make_executor(safety_verifier=self.proof)
        result = await executor.invoke("run_build", self.arguments)
        evidence = result.to_tool_evidence()
        self.assertEqual(len(self.session.calls), 2)
        self.assertEqual(evidence.attempts[0].error_kind, "TOOL_TIMEOUT")
        self.assertTrue(evidence.attempts[0].retry_safe)

    async def test_timeout_without_confirmation_stops_for_inspection(self):
        self.session.errors = ["TIMEOUT"]
        with self.assertRaises(TrackedMCPError) as caught:
            await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(len(self.session.calls), 1)
        self.assertFalse(self.record().to_tool_evidence().retries_exhausted)

    async def test_semantic_output_failure_cannot_be_reclassified_as_retryable_transport(self):
        identity = uuid4()
        self.session.result_override = {"exitCode": 999, "durationMs": 0,
                                       "executionManifestId": str(identity)}
        executor = self.make_executor(safety_verifier=self.proof)
        with self.assertRaises(TrackedMCPError) as caught:
            await executor.invoke("run_build", self.arguments)
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(self.record(executor).attempts[0].error_kind, "UNKNOWN_ERROR")
        self.assertTrue(self.record(executor).attempts[0].result_unknown)

    async def test_slow_sync_verifier_does_not_block_event_loop_or_extend_deadline(self):
        self.session.errors = ["TIMEOUT"]
        def slow(record, token, error):
            time.sleep(0.15)
            return self.proof(record, token, error)
        executor = self.make_executor(safety_verifier=slow)
        ticks = []
        async def tick():
            await asyncio.sleep(0.015)
            ticks.append(True)
        ticker = asyncio.create_task(tick())
        with self.assertRaises(TrackedMCPError):
            await executor.invoke("run_build", self.arguments, deadline_monotonic=time.monotonic() + 0.1)
        await ticker
        self.assertEqual(ticks, [True])
        self.assertEqual(len(self.session.calls), 1)

    async def test_async_verifier_uses_same_exact_proof(self):
        self.session.errors = ["TIMEOUT"]
        async def inspect_state(record, token, error):
            await asyncio.sleep(0)
            return self.proof(record, token, error)
        result = await self.make_executor(safety_verifier=inspect_state).invoke("run_build", self.arguments)
        self.assertEqual(result.to_tool_evidence().retries_used, 1)

    async def test_wrong_call_confirmation_does_not_enable_retry(self):
        self.session.errors = ["TIMEOUT"]
        def wrong(record, token, error):
            return replace(self.proof(record, token, error), logical_call_id=uuid4())
        executor = self.make_executor(safety_verifier=wrong)
        with self.assertRaises(TrackedMCPError):
            await executor.invoke("run_build", self.arguments)
        self.assertEqual(len(self.session.calls), 1)

    async def test_broken_verifier_is_not_authorization_or_raw_error(self):
        self.session.errors = ["TIMEOUT"]
        def failed(*_):
            raise RuntimeError("password=private-value")
        executor = self.make_executor(safety_verifier=failed)
        with self.assertRaises(TrackedMCPError) as caught:
            await executor.invoke("run_build", self.arguments)
        self.assertEqual(len(self.session.calls), 1)
        self.assertNotIn("private-value", repr(caught.exception))

    async def test_unknown_transport_result_never_repeats_without_confirmation(self):
        self.session.errors = [ConnectionError("private daemon details")]
        with self.assertRaises(TrackedMCPError) as caught:
            await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(len(self.session.calls), 1)
        self.assertNotIn("private daemon", str(caught.exception))

    async def test_policy_and_configuration_errors_never_retry(self):
        for code in ("PERMISSION_DENIED", "PATH_DENIED", "TOOL_NOT_IMPLEMENTED", "SANDBOX_ERROR", "PROFILE_NOT_FOUND"):
            before = len(self.session.calls)
            self.session.errors = [code]
            with self.assertRaises(TrackedMCPError):
                await self.executor.invoke("run_build", self.arguments)
            self.assertEqual(len(self.session.calls), before + 1)

    async def test_error_prose_is_not_interpreted_as_transient_or_retained(self):
        self.session.errors = ["RESOURCE_BUSY: api_key=private-value"]
        with self.assertRaises(TrackedMCPError) as caught:
            await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(len(self.session.calls), 1)
        self.assertNotIn("private-value", str(caught.exception))
        self.assertNotIn("private-value", json.dumps(self.record().to_tool_evidence().model_dump(mode="json")))

    async def test_completed_or_failed_logical_id_is_for_inspection_not_replay(self):
        result = await self.executor.invoke("run_build", self.arguments)
        with self.assertRaises(TrackedMCPError) as caught:
            await self.executor.invoke("run_build", self.arguments, logical_call_id=result.record.logical_call_id)
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_REPLAY_DENIED")
        self.assertEqual(len(self.session.calls), 1)

    async def test_logical_id_cannot_switch_arguments_or_snapshot(self):
        result = await self.executor.invoke("run_build", self.arguments)
        with self.assertRaises(TrackedMCPError):
            await self.executor.invoke("run_build", {**self.arguments, "snapshotId": str(uuid4())},
                                       logical_call_id=result.record.logical_call_id)
        self.assertEqual(len(self.session.calls), 1)

    async def test_invalid_schema_workspace_secret_and_deadline_never_invoke(self):
        cases = [("run_build", {**self.arguments, "command": "unsafe"}),
                 ("run_build", {**self.arguments, "workspaceId": str(uuid4())}),
                 ("write_source_file", {"workspaceId": str(self.run.workspace_id), "path": "source/leak.py",
                                        "content": "api_key='private-value'\n"})]
        for name, arguments in cases:
            with self.assertRaises(TrackedMCPError):
                await self.executor.invoke(name, arguments)
        for deadline in (True, float("nan"), time.monotonic() - 1):
            with self.assertRaises(TrackedMCPError):
                await self.executor.invoke("run_build", self.arguments, deadline_monotonic=deadline)
        self.assertEqual(self.session.calls, [])
        self.assertEqual(self.executor.logical_call_ids, ())

    async def test_unknown_source_or_inactive_step_cannot_call_peer(self):
        with self.assertRaises(TrackedMCPError):
            await self.executor.invoke("run_build", {**self.arguments, "snapshotId": str(uuid4())})
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        with self.assertRaises(TrackedMCPError):
            await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(self.session.calls, [])

    async def test_cancelled_run_between_retries_is_not_called_again(self):
        self.session.errors = ["RESOURCE_BUSY"]
        original_finish = self.store.finish
        def finish(*args, **kwargs):
            result = original_finish(*args, **kwargs)
            self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
            return result
        with patch.object(self.store, "finish", side_effect=finish), self.assertRaises(TrackedMCPError):
            await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(len(self.session.calls), 1)

    async def test_write_lost_reply_is_recorded_and_not_repeated_even_with_confirmation(self):
        async def lost(*_):
            raise asyncio.TimeoutError()
        self.session.before_reply = lost
        executor = self.make_executor(safety_verifier=self.proof)
        arguments = {"workspaceId": str(self.run.workspace_id), "path": "source/added.py", "content": "# desired file\n"}
        with self.assertRaises(TrackedMCPError) as caught:
            await executor.invoke("write_source_file", arguments)
        self.assertEqual((self.fixture.source / "added.py").read_text(), arguments["content"])
        self.assertEqual(len(self.session.calls), 1)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        record = self.record(executor)
        self.assertIsNone(record.execution_manifest)
        with self.assertRaises(ToolEvidenceStoreError):
            record.to_tool_evidence()

    async def test_file_read_output_is_not_persisted_as_source_trace(self):
        result = await self.executor.invoke("read_project_file", {
            "workspaceId": str(self.run.workspace_id), "path": "source/signup.py"})
        self.assertIn("fixture signup", result.data["content"])
        self.assertIsNone(result.record.execution_manifest)
        with self.fixture.repository._connection() as connection:
            for table in ("tool_execution_calls", "tool_execution_attempt_starts", "tool_execution_attempt_finishes"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone():
                    rows = connection.execute(f"SELECT * FROM {table}").fetchall()
                    self.assertNotIn("fixture signup", repr([tuple(row) for row in rows]))

    async def test_receipt_storage_error_does_not_publish_evidence_or_retry(self):
        with patch.object(self.store, "finish", side_effect=ToolEvidenceStoreError("TOOL_EVIDENCE_STORAGE_ERROR")):
            with self.assertRaises(TrackedMCPError) as caught:
                await self.executor.invoke("run_build", self.arguments)
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_EVIDENCE_FAILED")
        self.assertEqual(len(self.session.calls), 1)
        with self.assertRaises(ToolEvidenceStoreError):
            self.record().to_tool_evidence()

    async def test_cancellation_records_uncertainty_after_owned_container_cleanup(self):
        self.fixture.docker.block_start = True
        task = asyncio.create_task(self.executor.invoke("run_build", self.arguments))
        await asyncio.wait_for(self.fixture.docker.start_entered.wait(), 5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.fixture.docker.commands("rm")), 1)
        evidence = self.record().to_tool_evidence()
        self.assertEqual(evidence.outcome, ToolExecutionOutcome.UNVERIFIED)
        self.assertEqual(evidence.attempts[0].error_kind, "CANCELLED")
        self.assertFalse(evidence.attempts[0].retry_safe)

    async def test_one_shared_deadline_does_not_reset_on_each_retry(self):
        self.session.errors = ["RESOURCE_BUSY", "RESOURCE_BUSY"]
        self.session.delays = [0.02, 0.2]
        with self.assertRaises(TrackedMCPError):
            await self.executor.invoke("run_build", self.arguments, deadline_monotonic=time.monotonic() + 0.08)
        self.assertLessEqual(len(self.session.calls), 2)
        self.assertTrue(all(options["read_timeout_seconds"] <= 0.08 for _, _, options in self.session.calls))

    async def test_llm_adapter_validates_binding_arguments_and_duplicate_call_id(self):
        context = ToolContext(AgentRole.DEVELOPER, str(self.run.workspace_id), time.monotonic() + 10)
        call = ToolCall("provider-call-id", "run_build", json.dumps(self.arguments))
        result = await self.executor.execute(call, self.arguments, context)
        self.assertEqual(result["exitCode"], 0)
        with self.assertRaises(TrackedMCPError):
            await self.executor.execute(call, self.arguments, context)
        with self.assertRaises(TrackedMCPError):
            await self.executor.execute(ToolCall("new-id", "run_build", json.dumps(self.arguments)), self.arguments,
                ToolContext(AgentRole.QA, str(self.run.workspace_id), time.monotonic() + 10))
        self.assertEqual(len(self.session.calls), 1)

    async def test_constructor_is_inert_and_bad_configuration_is_safe(self):
        with patch.object(self.fixture.repository, "_transaction", side_effect=AssertionError("must be inert")):
            executor = self.make_executor()
        self.assertEqual(repr(executor), "TrackedMCPExecutor()")
        self.assertEqual(executor.logical_call_ids, ())
        for delay in (True, -1, float("nan"), 6):
            with self.assertRaises(TrackedMCPError):
                TrackedMCPExecutor(self.client, self.store, workflow_step_id=self.fixture.step.workflow_step_id,
                                   retry_delay_seconds=delay)

    def ledger_identities(self):
        record = self.store.create(self.binding, self.fixture.step.workflow_step_id, "run_build",
            arguments_sha256("run_build", self.arguments), source_artifact_id=self.fixture.snapshot.artifact_id,
            configuration_sha256=self.executor._configuration_hash,
            selector_sha256=self.executor._selector_hash("run_build", self.arguments))
        token = self.store.claim(self.binding, record.logical_call_id)
        return record.logical_call_id, token.attempt_id

    async def test_build_publication_cannot_reuse_logical_call_or_attempt_id(self):
        identities = self.ledger_identities()
        result = await self.fixture.execute()
        for identity in identities:
            with patch("mcp_tools.tools.build_store.uuid4", return_value=identity):
                with self.assertRaises(BuildStoreError) as caught:
                    self.outputs.publish(self.binding, self.fixture.snapshot, result, profile=self.fixture.profile)
            self.assertEqual(caught.exception.code, "BUILD_RECORD_CONFLICT")

    async def test_other_execution_stores_reject_ledger_id_namespace(self):
        identities = self.ledger_identities()
        for store, error, code in ((UnitTestOutputStore, UnitTestStoreError, "UNIT_TEST_RECORD_CONFLICT"),
                                    (BrowserTestOutputStore, BrowserStoreError, "BROWSER_TEST_RECORD_CONFLICT"),
                                    (SecurityScanOutputStore, SecurityStoreError, "SECURITY_SCAN_RECORD_CONFLICT")):
            with self.fixture.repository._transaction() as connection:
                store._ensure_schema(connection)
                for identity in identities:
                    with self.assertRaises(error) as caught:
                        store._collision(connection, (str(identity),))
                    self.assertEqual(caught.exception.code, code)

    async def test_real_stdio_retains_tool_error_code_in_durable_tool_evidence(self):
        configuration = replace(self.configuration, build_configuration=None)
        async with open_mcp_client(configuration) as client:
            executor = TrackedMCPExecutor(client, self.store, workflow_step_id=self.fixture.step.workflow_step_id,
                                          retry_delay_seconds=0)
            with self.assertRaises(TrackedMCPError):
                await executor.invoke("run_build", self.arguments)
        evidence = self.record(executor).to_tool_evidence()
        self.assertEqual(len(evidence.attempts), 1)
        self.assertEqual(evidence.attempts[0].error_kind, "SANDBOX_ERROR")
        self.assertFalse(evidence.attempts[0].can_retry)


if __name__ == "__main__":
    unittest.main()
