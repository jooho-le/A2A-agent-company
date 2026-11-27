"""Durable owned Host accounting; no cloud, Docker or generated execution."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMRuntimeError, TokenUsage, UsageRecord
from agents.platform.budgets import RunBudgetError, RunBudgetRegistry
from agents.platform.telemetry_store import AgentTelemetryStore, TelemetryStoreError
from orchestrator.domain import AgentRole, SCN_001_ID, TraceEvent, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository


class DurableBudgetTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-durable-budget-")
        self.addCleanup(temporary.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(temporary.name) / "workflow.sqlite3")
        self.current = datetime.now(timezone.utc)
        self.store = AgentTelemetryStore(self.repository.database_path, clock=lambda: self.current)
        self.limits = LLMLimits(max_model_calls=5, max_tool_calls=5, max_total_tokens=128)
        self.registry = RunBudgetRegistry(self.repository, limits=self.limits, telemetry_store=self.store)
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 요구사항",
            status=WorkflowStatus.PLANNING, created_at=self.current)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.RUNNING)
        self.model = ModelConfiguration(provider="fake", model_id="fake-model", temperature=0)
        self.configuration = RunConfigurationArtifact(run_id=self.run.run_id,
            scenario_id=self.run.scenario_id, workspace_id=self.run.workspace_id,
            configuration=RunConfiguration(model=self.model, limits=ExecutionLimits(runtime_budget_ms=60000)))
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.configuration)
        self.initial_event_count = self.repository.list_events(self.run.run_id, limit=100, offset=0)[1]
        self.binding = {"runId": str(self.run.run_id), "workflowStepId": str(self.step.workflow_step_id),
            "a2aTaskId": "opaque-secret-like-task-password=abc", "agentContextId": "opaque-context-token=abc",
            "role": "PLANNER", "attempt": 0, "codeVersion": None, "requirementIds": [],
            "inputArtifactIds": [], "snapshotSha256": None}

    def restart(self, *, limits=None):
        return RunBudgetRegistry(self.repository, limits=limits or self.limits, telemetry_store=self.store)

    def account(self, budget, sequence, usage=None):
        budget.account_usage(usage, sequence=sequence)
        record = UsageRecord(sequence, AgentRole.PLANNER, self.model, "fake-model", "completed", 3, usage)
        return record, self.store.append_usage(self.binding, record)

    def test_restart_keeps_counters_and_absolute_deadline(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _, _ = budget.reserve_model_call()
        self.account(budget, sequence, TokenUsage(3, 4, 7))
        tool, _ = budget.reserve_tracked_tool_call()
        budget.account_tool_call(tool)
        original = budget.snapshot
        self.current += timedelta(seconds=7)
        restored = self.restart().resolve(self.configuration)
        self.assertEqual((restored.model_calls, restored.tool_calls, restored.total_tokens), (1, 1, 7))
        self.assertEqual(restored.snapshot.deadline_utc, original.deadline_utc)
        self.assertLessEqual(restored.remaining_seconds(), 53)
        self.assertGreater(restored.remaining_seconds(), 52)
        self.assertEqual(restored.reserve_model_call()[0], 2)

    def test_pending_model_is_unknown_not_zero_and_blocks_restart(self):
        budget = self.registry.admit(self.run.run_id)
        budget.reserve_model_call()
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["modelCalls"], 1)
        self.assertEqual(summary["pendingModelSequences"], [1])
        self.assertIsNone(summary["totalTokens"])
        self.assertFalse(summary["usageComplete"])
        self.assertIsNone(summary["costUsd"])

    def test_pending_tool_blocks_restart_without_resetting_count(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _ = budget.reserve_tracked_tool_call()
        self.assertEqual(sequence, 1)
        with self.assertRaises(RunBudgetError):
            self.restart().admit(self.run.run_id)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["toolCalls"], 1)
        self.assertEqual(summary["pendingToolSequences"], [1])
        self.assertTrue(summary["executionBlocked"])

    def test_missing_usage_trace_blocks_restart_even_when_tokens_were_accounted(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _, _ = budget.reserve_model_call()
        budget.account_usage(TokenUsage(1, 2, 3), sequence=sequence)
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["knownTotalTokens"], 3)
        self.assertEqual(summary["missingUsageSequences"], [1])
        self.assertTrue(summary["executionBlocked"])

    def test_unknown_usage_blocks_restart_even_without_a_token_cap(self):
        limits = LLMLimits(max_model_calls=5, max_tool_calls=5)
        registry = RunBudgetRegistry(self.repository, limits=limits, telemetry_store=self.store)
        budget = registry.admit(self.run.run_id)
        self.account(budget, budget.reserve_model_call()[0])
        # Existing live uncapped behavior is unchanged; restart has no ownership.
        self.assertEqual(budget.reserve_model_call()[0], 2)
        self.account(budget, 2, TokenUsage(2, 3, 5))
        with self.assertRaises(RunBudgetError):
            self.restart(limits=limits).resolve(self.configuration)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["knownTotalTokens"], 5)
        self.assertIsNone(summary["totalTokens"])

    def test_clock_rollback_stops_execution_but_keeps_accounting_readable(self):
        budget = self.registry.admit(self.run.run_id)
        self.current += timedelta(seconds=5)
        self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 1, 2))
        self.current -= timedelta(seconds=1)
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_model_call()
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        self.assertEqual(self.store.usage_summary(self.run.run_id)["knownTotalTokens"], 2)

    def test_deadline_expiry_never_mints_new_runtime(self):
        budget = self.registry.admit(self.run.run_id)
        self.current += timedelta(seconds=61)
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_tracked_tool_call()
        with self.assertRaises(LLMRuntimeError):
            self.restart().resolve(self.configuration)
        self.assertEqual(self.store.load_budget(self.run.run_id).tool_calls, 0)

    def test_live_clock_rollback_after_nonconsuming_check_is_fail_closed(self):
        budget = self.registry.admit(self.run.run_id)
        self.current += timedelta(seconds=5)
        budget.check_model_call()
        self.current -= timedelta(seconds=1)
        with self.assertRaises(LLMRuntimeError):
            budget.check_model_call()
        self.assertTrue(self.store.load_budget(self.run.run_id).invalidated)
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)

    def test_reservation_write_time_counts_before_any_side_effect(self):
        budget = self.registry.admit(self.run.run_id)
        original = budget._persistence

        def slow_save(snapshot, revision):
            saved = original(snapshot, revision)
            self.current += timedelta(seconds=61)
            return saved

        budget._persistence = slow_save
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_model_call()
        snapshot = self.store.load_budget(self.run.run_id)
        self.assertEqual(snapshot.model_calls, 1)
        self.assertEqual(snapshot.pending_model_sequences, (1,))

    def test_limits_change_cannot_restore_or_overwrite_budget(self):
        self.registry.admit(self.run.run_id)
        with self.assertRaises(RunBudgetError):
            self.restart(limits=LLMLimits(max_model_calls=6)).resolve(self.configuration)
        self.assertEqual(self.store.load_budget(self.run.run_id).limits, self.limits)

    def test_frozen_configuration_change_cannot_restore(self):
        self.registry.admit(self.run.run_id)
        changed = self.configuration.model_copy(update={"artifact_id": uuid4()})
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(changed)

    def test_terminal_run_cannot_restore_for_execution(self):
        self.registry.admit(self.run.run_id)
        for status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED):
            with patch.object(self.repository, "get_run", return_value=self.run.model_copy(update={"status": status})):
                with self.assertRaises(RunBudgetError):
                    self.restart().resolve(self.configuration)
        self.assertEqual(self.store.usage_summary(self.run.run_id)["modelCalls"], 0)

    def test_reservation_failure_prevents_call_and_retains_old_counter(self):
        budget = self.registry.admit(self.run.run_id)
        with patch.object(self.store, "save_budget", side_effect=TelemetryStoreError()):
            # The budget retains its bound persistence writer; inject that seam.
            with patch.object(budget, "_persistence", side_effect=TelemetryStoreError()):
                with self.assertRaises(LLMRuntimeError):
                    budget.reserve_model_call()
        self.assertEqual(budget.model_calls, 0)
        self.assertEqual(self.store.load_budget(self.run.run_id).model_calls, 0)
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_model_call()

    def test_accounting_failure_preserves_pending_reservation(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _, _ = budget.reserve_model_call()
        with patch.object(budget, "_persistence", side_effect=TelemetryStoreError()):
            with self.assertRaises(LLMRuntimeError):
                budget.account_usage(TokenUsage(1, 2, 3), sequence=sequence)
        snapshot = self.store.load_budget(self.run.run_id)
        self.assertEqual(snapshot.pending_model_sequences, (1,))
        self.assertEqual(snapshot.known_total_tokens, 0)
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)

    def test_same_usage_and_trace_duplicate_are_idempotent(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _, _ = budget.reserve_model_call()
        usage = TokenUsage(1, 2, 3, cached_input_tokens=1)
        record, saved = self.account(budget, sequence, usage)
        budget.account_usage(usage, sequence=sequence)
        duplicate = self.store.append_usage(self.binding, record)
        self.assertEqual(saved["eventId"], duplicate["eventId"])
        self.assertEqual(budget.total_tokens, 3)
        self.assertEqual(self.store.list_usage(self.run.run_id)[1], 1)
        self.assertEqual(self.repository.list_events(self.run.run_id, limit=100, offset=0)[1], self.initial_event_count + 1)
        with self.assertRaises(LLMRuntimeError):
            budget.account_usage(TokenUsage(2, 2, 4), sequence=sequence)

    def test_conflicting_usage_or_binding_never_inserts_second_event(self):
        budget = self.registry.admit(self.run.run_id)
        record, _ = self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 2, 3))
        for binding, altered in (({**self.binding, "a2aTaskId": "different"}, record),
                                 (self.binding, replace(record, duration_ms=4))):
            with self.assertRaises(TelemetryStoreError):
                self.store.append_usage(binding, altered)
        self.assertEqual(self.repository.list_events(self.run.run_id, limit=100, offset=0)[1], self.initial_event_count + 1)

    def test_canonical_trace_is_exact_schema_and_opaque_ids_are_unchanged(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 1, 2))
        events, _ = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        event = next(event for event in events if event.event_type == "LLM_CALL_FINISHED")
        self.assertEqual(event.a2a_task_id, self.binding["a2aTaskId"])
        self.assertEqual(event.agent_context_id, self.binding["agentContextId"])
        self.assertNotIn("totalTokens", event.to_trace_json())
        self.assertNotIn("requestedModel", event.to_trace_json())
        self.assertEqual(self.store.list_usage(self.run.run_id)[0][0]["totalTokens"], 2)

    def event(self, event_type="LLM_MODEL_CALLED"):
        return TraceEvent(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            a2a_task_id=self.binding["a2aTaskId"], agent_context_id=self.binding["agentContextId"],
            event_type=event_type, actor="PLANNER", attempt=0)

    def test_event_detail_rejects_unstructured_or_secret_fields(self):
        base = {"sequence": 1, "requestedModel": self.model.model_dump(mode="json", by_alias=True)}
        for key in ("prompt", "source", "arguments", "stdout", "stderr", "password", "environment"):
            with self.subTest(key=key), self.assertRaises(TelemetryStoreError):
                self.store.append_event(self.event(), kind="LLM", detail={**base, key: "DO_NOT_STORE"},
                    idempotency_key="model-start:1")
        self.assertEqual(self.repository.list_events(self.run.run_id, limit=100, offset=0)[1], self.initial_event_count)

    def test_model_start_event_is_idempotent_and_distinct_from_usage(self):
        budget = self.registry.admit(self.run.run_id)
        sequence, _, _ = budget.reserve_model_call()
        detail = {"sequence": sequence, "requestedModel": self.model.model_dump(mode="json", by_alias=True)}
        first = self.store.append_event(self.event(), kind="LLM", detail=detail, idempotency_key="model-start:1")
        again = self.store.append_event(self.event(), kind="LLM", detail=detail, idempotency_key="model-start:1")
        self.assertEqual(first["eventId"], again["eventId"])
        self.account(budget, sequence, TokenUsage(1, 2, 3))
        self.assertEqual(self.store.list_events(self.run.run_id)[1], 2)
        self.assertEqual(self.store.list_usage(self.run.run_id)[1], 1)

    def test_mcp_detail_links_step_attempt_and_physical_attempt(self):
        call_id, attempt_id = uuid4(), uuid4()
        detail = {"toolName": "read_source_file", "logicalCallId": str(call_id),
            "toolAttempt": 0, "workflowAttempt": 0, "attemptId": str(attempt_id), "status": "STARTED",
            "resultUnknown": False, "retryDecision": "DO_NOT_RETRY",
            "inputSha256": "a" * 64, "configurationSha256": "b" * 64,
            "evidenceRef": f"artifact://{attempt_id}/tool-attempt.json"}
        saved = self.store.append_event(self.event("MCP_TOOL_CALLED"), kind="MCP", detail=detail,
                                       idempotency_key=f"mcp:{call_id}:0:called")
        self.assertEqual(saved["logicalCallId"], str(call_id))
        self.assertEqual(self.store.list_usage(self.run.run_id)[1], 0)
        with self.assertRaises(TelemetryStoreError):
            self.store.append_event(self.event("MCP_TOOL_CALLED"), kind="MCP",
                detail={**detail, "workflowAttempt": 1}, idempotency_key="wrong-attempt")

    def test_trace_and_detail_transaction_rolls_back_together(self):
        event = self.event().model_copy(update={"workflow_step_id": uuid4()})
        with self.assertRaises(TelemetryStoreError):
            self.store.append_event(event, kind="LLM", detail={"sequence": 1,
                "requestedModel": self.model.model_dump(mode="json", by_alias=True)}, idempotency_key="bad-step")
        self.assertEqual(self.repository.list_events(self.run.run_id, limit=100, offset=0)[1], self.initial_event_count)
        self.assertEqual(self.store.list_events(self.run.run_id)[1], 0)

    def test_details_are_append_only_and_budget_cannot_be_deleted(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 1, 2))
        for query in ("DELETE FROM agent_runtime_events", "UPDATE agent_runtime_events SET kind='MCP'",
                      "DELETE FROM agent_budget_ledger"):
            with sqlite3.connect(self.repository.database_path) as connection:
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(query)

    def test_stale_revision_cannot_reserve_another_call(self):
        budget = self.registry.admit(self.run.run_id)
        stale = ExecutionBudget(runtime_budget_ms=1, limits=self.limits, snapshot=budget.snapshot,
            persistence=self.store.save_budget, utc_clock=self.store.now)
        budget.reserve_model_call()
        with self.assertRaises(LLMRuntimeError):
            stale.reserve_model_call()
        self.assertEqual(self.store.load_budget(self.run.run_id).model_calls, 1)

    def test_invalidated_sink_failure_survives_restart(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 1, 2))
        budget.invalidate()
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_model_call()
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        self.assertTrue(self.store.usage_summary(self.run.run_id)["executionBlocked"])

    def test_detail_lists_are_bounded_and_unknown_cost_stays_null(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget, budget.reserve_model_call()[0], TokenUsage(1, 1, 2))
        self.assertEqual(self.store.list_usage(self.run.run_id, limit=1, offset=1), ([], 1))
        for limit, offset in ((0, 0), (501, 0), (1, -1), (True, 0)):
            with self.assertRaises(TelemetryStoreError):
                self.store.list_usage(self.run.run_id, limit=limit, offset=offset)
        self.assertIsNone(self.store.list_usage(self.run.run_id)[0][0]["costUsd"])


if __name__ == "__main__":
    unittest.main()
