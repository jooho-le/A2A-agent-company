"""Actual bounded engine + durable telemetry, using authored provider fixtures."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import unittest

import test_agent_durable_budgets as budget_fixtures
import test_llm_runtime as llm_helpers
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, StructuredOutput, ToolCall, ToolDefinition, TokenUsage, UsageRecord, json_text
from agents.llm.engine import LLMEngine
from agents.platform.budgets import RunBudgetError
from agents.platform.telemetry_store import TelemetryStoreError
from agents.roles.prompts import prepare_role_prompt
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.domain.states import AgentRole


class LLMRuntimeTelemetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = budget_fixtures.DurableBudgetTests("runTest")
        self.fixture.setUp()
        for callback, arguments, keywords in self.fixture._cleanups:
            self.addCleanup(callback, *arguments, **keywords)
        self.fixture._cleanups.clear()
        self.budget = self.fixture.registry.admit(self.fixture.run.run_id)
        self.store = self.fixture.store
        self.role = AgentRole.PLANNER
        self.model = self.fixture.model
        self.output = StructuredOutput("analysis", llm_helpers.object_schema({"answer": {"type": "string"}}))
        self.metadata = A2AWorkflowMetadata(run_id=self.fixture.run.run_id,
            workflow_step_id=self.fixture.step.workflow_step_id, scenario_id=self.fixture.run.scenario_id,
            attempt=0)
        self.prompt = prepare_role_prompt(self.role, task_input={"request": "회원가입 계획"}, metadata=self.metadata)
        self.events = []
        persistence = self.budget._persistence

        def measured_save(snapshot, revision):
            previous = self.budget.snapshot
            if snapshot.model_calls > previous.model_calls:
                label = f"reserve-model:{snapshot.model_calls}"
            elif snapshot.tool_calls > previous.tool_calls:
                label = f"reserve-tool:{snapshot.tool_calls}"
            elif set(snapshot.accounted_usage) - set(previous.accounted_usage):
                label = f"account-model:{max(set(snapshot.accounted_usage) - set(previous.accounted_usage))}"
            elif set(snapshot.accounted_tool_sequences) - set(previous.accounted_tool_sequences):
                label = f"account-tool:{max(set(snapshot.accounted_tool_sequences) - set(previous.accounted_tool_sequences))}"
            else:
                label = "invalidate" if snapshot.invalidated else "observe"
            saved = persistence(snapshot, revision)
            self.events.append(label)
            return saved

        self.budget._persistence = measured_save

    def start(self, sequence):
        self.events.append(f"start:{sequence}")
        self.store.append_event(self.fixture.event(), kind="LLM", detail={"sequence": sequence,
            "requestedModel": self.model.model_dump(mode="json", by_alias=True)},
            idempotency_key=f"model-start:{sequence}")

    def usage(self, record):
        self.events.append(f"usage:{record.sequence}")
        self.store.append_usage(self.fixture.binding, record)

    async def run_engine(self, provider, *, started=None, usage=None, engine=None, prompt=None):
        selected = engine or LLMEngine(role=self.role, provider=provider)
        return await selected.run(prompt=prompt or self.prompt, model=self.model, output=self.output,
            budget=self.budget, workspace_id=str(self.fixture.run.workspace_id),
            call_started_sink=self.start if started is None else started,
            usage_sink=self.usage if usage is None else usage)

    def assert_execution_blocked(self):
        with self.assertRaises(LLMRuntimeError):
            self.budget.reserve_model_call()
        with self.assertRaises(RunBudgetError):
            self.fixture.restart().resolve(self.fixture.configuration)

    async def test_order_is_durable_reserve_start_provider_account_usage(self):
        def provider_response(_request):
            self.events.append("provider:1")
            snapshot = self.store.load_budget(self.fixture.run.run_id)
            self.assertEqual(snapshot.pending_model_sequences, (1,))
            rows, total = self.store.list_events(self.fixture.run.run_id)
            self.assertEqual(total, 1)
            self.assertEqual(rows[0]["sequence"], 1)
            self.assertEqual(rows[0]["recordType"], "EVENT")
            return llm_helpers.text_response()

        provider = llm_helpers.FakeProvider(provider_response)
        result = await self.run_engine(provider)
        self.assertEqual(self.events, ["reserve-model:1", "start:1", "provider:1", "account-model:1", "usage:1"])
        self.assertEqual(result.data, {"answer": "done"})
        snapshot = self.store.load_budget(self.fixture.run.run_id)
        self.assertEqual(snapshot.accounted_usage[1], llm_helpers.measured_usage())
        self.assertEqual(snapshot.pending_model_sequences, ())

    async def test_failed_reservation_never_calls_start_sink_or_provider(self):
        def unavailable(*_):
            raise TelemetryStoreError()

        self.budget._persistence = unavailable
        provider = llm_helpers.FakeProvider(llm_helpers.text_response())
        with self.assertRaises(LLMRuntimeError):
            await self.run_engine(provider)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.events, [])
        self.assertEqual(self.store.load_budget(self.fixture.run.run_id).model_calls, 0)

    async def test_start_trace_failure_invalidates_before_provider(self):
        def unavailable(sequence):
            self.events.append(f"start-failed:{sequence}")
            raise RuntimeError("password=DO_NOT_LOG_START_EXCEPTION")

        provider = llm_helpers.FakeProvider(llm_helpers.text_response())
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, started=unavailable)
        self.assertEqual(provider.requests, [])
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        snapshot = self.store.load_budget(self.fixture.run.run_id)
        self.assertEqual(snapshot.pending_model_sequences, (1,))
        self.assertTrue(snapshot.invalidated)
        self.assert_execution_blocked()

    async def test_usage_sink_failure_stops_later_effects_and_survives_restart(self):
        def unavailable(record):
            self.usage(record)
            raise RuntimeError("password=DO_NOT_LOG_USAGE_EXCEPTION")

        provider = llm_helpers.FakeProvider(llm_helpers.text_response(), llm_helpers.text_response())
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, usage=unavailable)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.budget.total_tokens, 15)
        self.assertEqual(self.store.list_usage(self.fixture.run.run_id)[1], 1)
        self.assertTrue(self.store.load_budget(self.fixture.run.run_id).invalidated)
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        self.assert_execution_blocked()

    async def test_usage_failure_before_approved_tool_prevents_tool_and_next_provider(self):
        workspace = str(self.fixture.run.workspace_id)
        call = ToolCall("approved-read-1", "read_project_file", json_text({"workspaceId": workspace, "path": "source/signup.py"}))
        tool = ToolDefinition("read_project_file", "Authored read fixture",
            llm_helpers.object_schema({"workspaceId": {"type": "string"}, "path": {"type": "string"}}), self.output.schema)
        executor = llm_helpers.FakeToolExecutor({"answer": "read"})
        provider = llm_helpers.FakeProvider(llm_helpers.tool_response(call), llm_helpers.text_response())
        engine = LLMEngine(role=AgentRole.DEVELOPER, provider=provider, tools=(tool,), tool_executor=executor)
        prompt = prepare_role_prompt(AgentRole.DEVELOPER, task_input={"request": "회원가입 구현"}, metadata=self.metadata)

        def failed_usage(_record):
            raise RuntimeError("required durable observer failed")

        # This case isolates engine ordering, not a Planner-to-Developer Run.
        with self.assertRaises(LLMRuntimeError):
            await self.run_engine(provider, started=lambda _sequence: None, usage=failed_usage, engine=engine, prompt=prompt)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.budget.tool_calls, 0)
        self.assert_execution_blocked()

    async def test_reservation_storage_delay_exhausts_deadline_before_start(self):
        original = self.budget._persistence

        def slow_writer(snapshot, revision):
            saved = original(snapshot, revision)
            self.fixture.current += timedelta(seconds=61)
            return saved

        self.budget._persistence = slow_writer
        provider = llm_helpers.FakeProvider(llm_helpers.text_response())
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.events, ["reserve-model:1"])
        self.assertEqual(self.store.load_budget(self.fixture.run.run_id).pending_model_sequences, (1,))

    async def test_start_trace_delay_exhausts_deadline_before_provider(self):
        def delayed_start(sequence):
            self.start(sequence)
            self.fixture.current += timedelta(seconds=61)

        provider = llm_helpers.FakeProvider(llm_helpers.text_response())
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, started=delayed_start)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.store.list_events(self.fixture.run.run_id)[1], 1)
        self.assertEqual(self.store.list_usage(self.fixture.run.run_id)[1], 0)
        self.assertIsNone(self.store.usage_summary(self.fixture.run.run_id)["totalTokens"])

    async def test_provider_failure_records_unknown_usage_not_zero(self):
        provider = llm_helpers.FakeProvider(RuntimeError("password=DO_NOT_LOG_PROVIDER_EXCEPTION"))
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.PROVIDER)
        self.assertNotIn("DO_NOT_LOG", str(caught.exception))
        rows, total = self.store.list_usage(self.fixture.run.run_id)
        self.assertEqual(total, 1)
        self.assertFalse(rows[0]["usageKnown"])
        self.assertIsNone(rows[0]["totalTokens"])
        self.assertEqual(rows[0]["outcome"], LLMErrorCode.PROVIDER.value)
        snapshot = self.store.load_budget(self.fixture.run.run_id)
        self.assertIsNone(snapshot.accounted_usage[1])
        self.assertEqual(snapshot.pending_model_sequences, ())
        self.assertIsNone(self.budget.total_tokens)
        self.assert_execution_blocked()

    async def test_provider_failure_with_measured_usage_records_exact_known_tokens(self):
        usage = TokenUsage(9, 6, 15, cached_input_tokens=4, reasoning_output_tokens=2)
        provider = llm_helpers.FakeProvider(LLMRuntimeError(LLMErrorCode.INCOMPLETE, usage=usage))
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.INCOMPLETE)
        self.assertEqual(self.budget.total_tokens, 15)
        row = self.store.list_usage(self.fixture.run.run_id)[0][0]
        self.assertEqual((row["inputTokens"], row["outputTokens"], row["cachedInputTokens"], row["reasoningOutputTokens"]), (9, 6, 4, 2))
        self.assertEqual(self.fixture.restart().resolve(self.fixture.configuration).total_tokens, 15)

    async def test_canceled_provider_is_drained_before_unknown_usage_receipt(self):
        entered, drained = asyncio.Event(), asyncio.Event()

        async def blocked_provider(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                self.events.append("provider-drained")
                drained.set()

        def cancellation_usage(record):
            self.assertTrue(drained.is_set())
            self.usage(record)

        provider = llm_helpers.FakeProvider(blocked_provider)
        task = asyncio.create_task(self.run_engine(provider, usage=cancellation_usage))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        row = self.store.list_usage(self.fixture.run.run_id)[0][0]
        self.assertEqual(row["outcome"], "canceled")
        self.assertIsNone(row["totalTokens"])
        self.assertLess(self.events.index("provider-drained"), self.events.index("account-model:1"))
        self.assert_execution_blocked()

    async def test_invalid_model_output_does_not_discard_measured_usage(self):
        provider = llm_helpers.FakeProvider(llm_helpers.text_response("not-json"))
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
        self.assertEqual(self.budget.total_tokens, 15)
        self.assertEqual(len(caught.exception.records), 1)
        self.assertEqual(self.store.list_usage(self.fixture.run.run_id)[0][0]["outcome"], "completed")

    async def test_multiple_engine_invocations_keep_global_sequence_and_exact_accounting(self):
        for _ in range(2):
            await self.run_engine(llm_helpers.FakeProvider(llm_helpers.text_response()))
        rows, count = self.store.list_usage(self.fixture.run.run_id)
        self.assertEqual(count, 2)
        self.assertEqual([row["sequence"] for row in rows], [1, 2])
        self.assertEqual(self.budget.total_tokens, 30)
        self.budget.account_usage(llm_helpers.measured_usage(), sequence=1)
        self.assertEqual(self.budget.total_tokens, 30)
        restored = self.fixture.restart().resolve(self.fixture.configuration)
        self.assertEqual(restored.model_calls, 2)
        self.assertEqual(restored.total_tokens, 30)

    async def test_usage_trace_rejects_tokens_different_from_durable_accounting(self):
        sequence, _, _ = self.budget.reserve_model_call()
        expected = TokenUsage(5, 3, 8)
        self.budget.account_usage(expected, sequence=sequence)
        valid = UsageRecord(sequence, self.role, self.model, "fake-model", "completed", 1, expected)
        for different in (TokenUsage(4, 4, 8), TokenUsage(5, 4, 9), None):
            with self.subTest(usage=different), self.assertRaises(TelemetryStoreError):
                self.store.append_usage(self.fixture.binding, replace(valid, usage=different))
        self.assertEqual(self.store.list_usage(self.fixture.run.run_id)[1], 0)
        self.store.append_usage(self.fixture.binding, valid)
        self.assertEqual(self.store.list_usage(self.fixture.run.run_id)[1], 1)


if __name__ == "__main__":
    unittest.main()
