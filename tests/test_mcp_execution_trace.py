"""Physical MCP attempt Trace hooks with real journal, fake model/daemon only."""

import asyncio
import json
import threading
import unittest
from unittest.mock import patch

import anyio

import test_mcp_execution_runtime as runtime_fixture
from mcp_tools.execution_runtime import TrackedMCPError
from mcp_tools.execution_store import ToolCallRecord, ToolEvidenceStoreError
from orchestrator.domain.retry_policy import RetryDecision
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


class MCPExecutionTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = runtime_fixture.ExecutionRuntimeTests("runTest")
        self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.events = []

    def sink(self, event_type, record, attempt, duration_ms):
        self.events.append((event_type, record, attempt, duration_ms))

    def executor(self, sink=None):
        return self.fixture.make_executor(event_sink=self.sink if sink is None else sink)

    async def invoke(self, executor):
        return await executor.invoke("run_build", self.fixture.arguments)

    def record(self, executor):
        return self.fixture.record(executor)

    async def test_called_commits_before_sdk_and_finished_after_receipt(self):
        def sink(event_type, record, attempt, duration_ms):
            self.sink(event_type, record, attempt, duration_ms)
            self.assertIsInstance(record, ToolCallRecord)
            self.assertEqual(self.fixture.store.get(self.fixture.binding, record.logical_call_id), record)
            self.assertEqual(attempt, 0)
            self.assertEqual(len(self.fixture.session.calls), 0 if event_type == "MCP_TOOL_CALLED" else 1)
            if event_type == "MCP_TOOL_CALLED":
                self.assertEqual(record.attempts[0].status, "STARTED")
                self.assertIsNone(duration_ms)
                self.assertIsNone(record.attempts[0].outcome)
            else:
                self.assertEqual(record.attempts[0].status, "FINISHED")
                self.assertEqual(record.attempts[0].outcome, ToolExecutionOutcome.PASS)
                self.assertIsNotNone(record.attempts[0].execution_id)
                self.assertEqual(duration_ms, record.attempts[0].duration_ms)

        executor = self.executor(sink)
        result = await self.invoke(executor)
        self.assertEqual([item[0] for item in self.events], ["MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"])
        self.assertEqual(self.events[-1][1], result.record)

    async def test_every_physical_retry_has_same_logical_id_and_own_pair(self):
        self.fixture.session.errors = ["RESOURCE_BUSY", "PROCESS_STARTUP_FAILURE"]
        result = await self.invoke(self.executor())
        self.assertEqual([item[0] for item in self.events], ["MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"] * 3)
        self.assertEqual([item[2] for item in self.events], [0, 0, 1, 1, 2, 2])
        self.assertEqual({item[1].logical_call_id for item in self.events}, {result.record.logical_call_id})
        self.assertEqual(len({item[1].attempts[item[2]].attempt_id for item in self.events}), 3)
        self.assertEqual(len(self.fixture.session.calls), 3)
        self.assertTrue(all(arguments == self.fixture.arguments for _, arguments, _ in self.fixture.session.calls))

    async def test_exhausted_retries_do_not_make_a_fourth_trace_pair(self):
        self.fixture.session.errors = ["RESOURCE_BUSY"] * 4
        with self.assertRaises(TrackedMCPError):
            await self.invoke(self.executor())
        self.assertEqual(len(self.events), 6)
        self.assertEqual(len(self.fixture.session.calls), 3)
        self.assertEqual(self.events[-1][1].attempts[-1].retry_decision, RetryDecision.DO_NOT_RETRY)

    async def test_called_sink_failure_is_closed_without_client_or_retry(self):
        def broken(*_):
            raise ValueError("raw password/source must never escape")

        executor = self.executor(broken)
        with self.assertRaises(TrackedMCPError) as caught:
            await self.invoke(executor)
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_EVIDENCE_FAILED")
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(str(caught.exception), "MCP_EXECUTION_EVIDENCE_FAILED")
        self.assertEqual(self.fixture.session.calls, [])
        self.assertEqual(self.record(executor).attempts[0].status, "STARTED")

    async def test_finished_sink_failure_keeps_actual_receipt_and_denies_replay(self):
        def broken(event_type, *args):
            if event_type == "MCP_TOOL_FINISHED":
                raise RuntimeError("raw stdout must never escape")

        executor = self.executor(broken)
        with self.assertRaises(TrackedMCPError) as caught:
            await self.invoke(executor)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        record = self.record(executor)
        self.assertEqual(record.attempts[0].status, "FINISHED")
        self.assertEqual(record.attempts[0].outcome, ToolExecutionOutcome.PASS)
        self.assertIsNotNone(record.attempts[0].execution_id)
        with self.assertRaises(TrackedMCPError) as replay:
            await executor.invoke("run_build", self.fixture.arguments, logical_call_id=record.logical_call_id)
        self.assertEqual(replay.exception.code, "MCP_EXECUTION_REPLAY_DENIED")
        self.assertEqual(self.record(executor), record)
        self.assertEqual(len(self.fixture.session.calls), 1)

    async def test_finished_trace_failure_after_retryable_reply_cannot_retry_tool(self):
        self.fixture.session.errors = ["RESOURCE_BUSY"]

        def broken(event_type, *_):
            if event_type == "MCP_TOOL_FINISHED":
                raise ToolEvidenceStoreError("TOOL_EVIDENCE_STORAGE_ERROR")

        executor = self.executor(broken)
        with self.assertRaises(TrackedMCPError) as caught:
            await self.invoke(executor)
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(self.record(executor).attempts[0].retry_decision, RetryDecision.RETRY)
        self.assertEqual(len(self.record(executor).attempts), 1)
        self.assertEqual(len(self.fixture.session.calls), 1)

    async def test_async_sink_runs_on_event_loop(self):
        loop = asyncio.get_running_loop()

        async def sink(*args):
            self.assertIs(asyncio.get_running_loop(), loop)
            await asyncio.sleep(0)
            self.sink(*args)

        await self.invoke(self.executor(sink))
        self.assertEqual(len(self.events), 2)

    async def test_sync_callable_returning_awaitable_is_awaited(self):
        def sink(*args):
            async def publish():
                await asyncio.sleep(0)
                self.sink(*args)
            return publish()

        await self.invoke(self.executor(sink))
        self.assertEqual(len(self.events), 2)

    async def test_hook_does_not_receive_arguments_source_output_or_error_prose(self):
        executor = self.executor()
        result = await executor.invoke("read_project_file", {
            "workspaceId": str(self.fixture.run.workspace_id), "path": "source/signup.py"})
        self.assertIn("fixture signup", result.data["content"])
        serialized = json.dumps([item[1].to_dict() for item in self.events])
        for forbidden in ("fixture signup", "source/signup.py", '"content"', '"output"'):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual({item[1].tool_name for item in self.events}, {"read_project_file"})

    async def test_setter_is_inert_and_locked_after_first_invoke(self):
        executor = self.fixture.make_executor()
        with patch.object(self.fixture.store, "get", side_effect=AssertionError("must be inert")):
            executor.set_event_sink(self.sink)
        await self.invoke(executor)
        self.assertEqual(len(self.events), 2)
        for sink in (None, self.sink, object()):
            with self.assertRaises(TrackedMCPError) as caught:
                executor.set_event_sink(sink)
            self.assertEqual(caught.exception.code, "MCP_EXECUTION_CONFIGURATION_INVALID")

    async def test_invalid_sink_and_setter_after_rejected_call_are_denied(self):
        with self.assertRaises(TrackedMCPError):
            self.fixture.make_executor(event_sink=object())
        executor = self.fixture.make_executor()
        with self.assertRaises(TrackedMCPError):
            await executor.invoke("unsupported", {})
        with self.assertRaises(TrackedMCPError):
            executor.set_event_sink(self.sink)
        self.assertEqual(self.fixture.session.calls, [])

    async def test_cancel_records_uncertainty_after_container_cleanup_before_finished(self):
        self.fixture.fixture.docker.block_start = True

        def sink(*args):
            self.sink(*args)
            if args[0] == "MCP_TOOL_FINISHED":
                self.assertEqual(len(self.fixture.fixture.docker.commands("rm")), 1)

        executor = self.executor(sink)
        caller = asyncio.create_task(self.invoke(executor))
        await asyncio.wait_for(self.fixture.fixture.docker.start_entered.wait(), 5)
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        attempt = self.record(executor).attempts[0]
        self.assertEqual([item[0] for item in self.events], ["MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"])
        self.assertEqual(attempt.outcome, ToolExecutionOutcome.UNVERIFIED)
        self.assertEqual(attempt.error_kind, "CANCELLED")
        self.assertEqual(attempt.delivery_state, "UNKNOWN")
        self.assertTrue(attempt.result_unknown)
        self.assertFalse(attempt.retry_safe)
        self.assertEqual(attempt.retry_decision, RetryDecision.DO_NOT_RETRY)

    async def cancel_during_sink(self, *, event_type, synchronous, anyio_scope=False):
        entered = asyncio.Event()
        release = threading.Event()
        async_release = asyncio.Event()
        loop = asyncio.get_running_loop()
        scopes = []

        def sync_sink(*args):
            if args[0] == event_type:
                loop.call_soon_threadsafe(entered.set)
                if not release.wait(5):
                    raise AssertionError("fixture Trace drain timed out")
            self.sink(*args)

        async def async_sink(*args):
            if args[0] == event_type:
                entered.set()
                await async_release.wait()
            self.sink(*args)

        executor = self.executor(sync_sink if synchronous else async_sink)

        async def requester():
            if anyio_scope:
                with anyio.CancelScope() as scope:
                    scopes.append(scope)
                    await self.invoke(executor)
                self.assertTrue(scope.cancelled_caught)
            else:
                await self.invoke(executor)

        caller = asyncio.create_task(requester())
        try:
            await asyncio.wait_for(entered.wait(), 5)
            for _ in range(3):
                if anyio_scope:
                    scopes[0].cancel()
                else:
                    caller.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(caller.done())
        finally:
            release.set()
            async_release.set()
            if anyio_scope:
                await asyncio.wait_for(caller, 5)
            else:
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(caller, 5)
        self.assertEqual([item[0] for item in self.events], ["MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"])
        attempt = self.record(executor).attempts[0]
        if event_type == "MCP_TOOL_CALLED":
            self.assertEqual(self.fixture.session.calls, [])
            self.assertEqual(attempt.error_kind, "CANCELLED")
            self.assertEqual(attempt.outcome, ToolExecutionOutcome.UNVERIFIED)
        else:
            self.assertEqual(len(self.fixture.session.calls), 1)
            self.assertEqual(attempt.outcome, ToolExecutionOutcome.PASS)
            self.assertIsNotNone(attempt.execution_id)
            self.assertIsNone(attempt.error_kind)

    async def test_repeated_cancel_drains_sync_called_before_uncertain_finish(self):
        await self.cancel_during_sink(event_type="MCP_TOOL_CALLED", synchronous=True)

    async def test_repeated_cancel_drains_async_finished_and_preserves_pass_receipt(self):
        await self.cancel_during_sink(event_type="MCP_TOOL_FINISHED", synchronous=False)

    async def test_cancel_during_store_finish_drains_commit_then_emits_actual_receipt_once(self):
        entered = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        original = self.fixture.store.finish

        def committed_finish(*args, **kwargs):
            record = original(*args, **kwargs)
            loop.call_soon_threadsafe(entered.set)
            if not release.wait(5):
                raise AssertionError("fixture receipt drain timed out")
            return record

        executor = self.executor()
        with patch.object(self.fixture.store, "finish", side_effect=committed_finish):
            caller = asyncio.create_task(self.invoke(executor))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                caller.cancel()
                await asyncio.sleep(.01)
                self.assertFalse(caller.done())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(caller, 5)
        record = self.record(executor)
        self.assertEqual(record.attempts[0].outcome, ToolExecutionOutcome.PASS)
        self.assertIsNotNone(record.attempts[0].execution_id)
        self.assertIsNone(record.attempts[0].error_kind)
        self.assertEqual([item[0] for item in self.events], ["MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"])
        self.assertEqual(self.events[-1][1], record)
        self.assertEqual(len(self.fixture.session.calls), 1)

    async def test_anyio_level_cancel_drains_sync_finished_without_duplicate_event(self):
        await self.cancel_during_sink(event_type="MCP_TOOL_FINISHED", synchronous=True, anyio_scope=True)

    async def test_anyio_level_cancel_drains_async_called_before_uncertain_finish(self):
        await self.cancel_during_sink(event_type="MCP_TOOL_CALLED", synchronous=False, anyio_scope=True)

    async def test_cancel_trace_failure_keeps_finished_cancellation_without_replay(self):
        self.fixture.fixture.docker.block_start = True

        def broken(event_type, *_):
            if event_type == "MCP_TOOL_FINISHED":
                raise RuntimeError("raw private Trace failure")

        executor = self.executor(broken)
        caller = asyncio.create_task(self.invoke(executor))
        await asyncio.wait_for(self.fixture.fixture.docker.start_entered.wait(), 5)
        caller.cancel()
        with self.assertRaises(TrackedMCPError) as caught:
            await caller
        self.assertEqual(caught.exception.code, "MCP_EXECUTION_EVIDENCE_FAILED")
        self.assertEqual(caught.exception.retry_decision, RetryDecision.INSPECT_STATE)
        self.assertEqual(self.record(executor).attempts[0].error_kind, "CANCELLED")
        self.assertEqual(len(self.fixture.session.calls), 1)


if __name__ == "__main__":
    unittest.main()
