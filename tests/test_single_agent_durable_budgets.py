"""Single ledger provenance and fail-closed restart without external calls."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agents.core.identity import SingleAgentIdentity
from agents.llm.budget import LLMLimits
from agents.llm.contracts import LLMRuntimeError, TokenUsage, UsageRecord
from agents.platform.budgets import RunBudgetRegistry, RunBudgetError
from agents.platform.telemetry_store import AgentTelemetryStore, TelemetryStoreError
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository


class SingleDurableBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="a2a-single-durable-")
        self.addCleanup(directory.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(directory.name) / "workflow.sqlite3")
        self.now = datetime.now(timezone.utc)
        self.store = AgentTelemetryStore(self.repository.database_path, clock=lambda: self.now)
        self.limits = LLMLimits(max_model_calls=4, max_tool_calls=4, max_total_tokens=128)
        self.registry = self.restart()
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="동결 회원가입 구현", status=WorkflowStatus.PLANNING, created_at=self.now)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER, status=WorkflowStepStatus.RUNNING)
        self.model = ModelConfiguration(provider="fake", model_id="fake-model", temperature=0)
        self.configuration = RunConfigurationArtifact(run_id=self.run.run_id, scenario_id=self.run.scenario_id,
            workspace_id=self.run.workspace_id, configuration=RunConfiguration(architecture="SINGLE_AGENT", model=self.model,
                limits=ExecutionLimits(runtime_budget_ms=60000)))
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.configuration)
        self.binding = {"runId": str(self.run.run_id), "workflowStepId": str(self.step.workflow_step_id),
            "a2aTaskId": "opaque-single-task", "agentContextId": "opaque-single-context", "role": "SINGLE_AGENT",
            "attempt": 0, "codeVersion": None, "requirementIds": [], "inputArtifactIds": [], "snapshotSha256": None}

    def restart(self, limits=None):
        return RunBudgetRegistry(self.repository, limits=limits or self.limits, telemetry_store=self.store)

    def account(self, budget, usage=TokenUsage(1, 2, 3), *, publish=True):
        sequence, _, _ = budget.reserve_model_call()
        budget.account_usage(usage, sequence=sequence)
        record = UsageRecord(sequence, SingleAgentIdentity.SINGLE_AGENT, self.model, "fake-model", "completed", 1, usage)
        if publish:
            self.store.append_usage(self.binding, record)
        return record

    def test_restart_keeps_single_receipts_counters_tokens_and_absolute_deadline(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget)
        sequence, _ = budget.reserve_tracked_tool_call()
        budget.account_tool_call(sequence)
        before = budget.snapshot
        self.now += timedelta(seconds=7)
        restored = self.restart().resolve(self.configuration)
        self.assertEqual((restored.model_calls, restored.tool_calls, restored.total_tokens), (1, 1, 3))
        self.assertEqual(restored.snapshot.deadline_utc, before.deadline_utc)
        self.assertLessEqual(restored.remaining_seconds(), 53)
        self.assertGreater(restored.remaining_seconds(), 52)
        self.assertEqual(restored.reserve_model_call()[0], 2)

    def test_pending_model_call_is_unknown_and_never_replayed_with_new_budget(self):
        self.registry.admit(self.run.run_id).reserve_model_call()
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["pendingModelSequences"], [1])
        self.assertIsNone(summary["totalTokens"])
        self.assertTrue(summary["executionBlocked"])

    def test_pending_tool_effect_blocks_restart(self):
        self.registry.admit(self.run.run_id).reserve_tracked_tool_call()
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        snapshot = self.store.load_budget(self.run.run_id)
        self.assertEqual((snapshot.tool_calls, snapshot.pending_tool_sequences), (1, (1,)))

    def test_missing_model_receipt_blocks_restore_even_with_accounted_tokens(self):
        self.account(self.registry.admit(self.run.run_id), publish=False)
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        self.assertEqual(self.store.usage_summary(self.run.run_id)["missingUsageSequences"], [1])

    def test_unknown_tokens_remain_null_and_block_restart(self):
        self.account(self.registry.admit(self.run.run_id), usage=None)
        with self.assertRaises(RunBudgetError):
            self.restart().resolve(self.configuration)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertIsNone(summary["totalTokens"])
        self.assertIsNone(summary["costUsd"])

    def test_clock_rollback_or_expiry_cannot_restore_runtime(self):
        budget = self.registry.admit(self.run.run_id)
        self.account(budget)
        self.now -= timedelta(seconds=1)
        with self.assertRaises((RunBudgetError, LLMRuntimeError)):
            self.restart().resolve(self.configuration)
        self.now += timedelta(seconds=62)
        with self.assertRaises((RunBudgetError, LLMRuntimeError)):
            self.restart().resolve(self.configuration)
        self.assertEqual(self.store.load_budget(self.run.run_id).model_calls, 1)

    def test_changed_host_limits_cannot_refill_old_run(self):
        self.account(self.registry.admit(self.run.run_id))
        with self.assertRaises(RunBudgetError):
            self.restart(LLMLimits(max_model_calls=5)).resolve(self.configuration)
        self.assertEqual(self.store.load_budget(self.run.run_id).limits, self.limits)

    def test_receipts_cannot_claim_four_independent_agents_for_a_single_run(self):
        record = self.account(self.registry.admit(self.run.run_id), publish=False)
        fake = UsageRecord(record.sequence, AgentRole.PLANNER, self.model, "fake-model", "completed", 1, record.usage)
        with self.assertRaises(TelemetryStoreError):
            self.store.append_usage({**self.binding, "role": "PLANNER"}, fake)
        self.store.append_usage(self.binding, record)
        entries, count = self.store.list_usage(self.run.run_id)
        self.assertEqual(count, 1)
        self.assertEqual(entries[0]["role"], "SINGLE_AGENT")

    def test_single_actor_cannot_be_attached_to_multi_agent_run(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="Multi 기준선", status=WorkflowStatus.PLANNING, created_at=self.now)
        step = WorkflowStep(run_id=other.run_id, agent_role=AgentRole.PLANNER, status=WorkflowStepStatus.RUNNING)
        config = RunConfigurationArtifact(run_id=other.run_id, scenario_id=other.scenario_id, workspace_id=other.workspace_id,
            configuration=RunConfiguration(architecture="MULTI_AGENT", model=self.model, limits=ExecutionLimits(runtime_budget_ms=60000)))
        self.repository.create_run(other, (step,), (), run_configuration=config)
        binding = {**self.binding, "runId": str(other.run_id), "workflowStepId": str(step.workflow_step_id)}
        record = UsageRecord(1, SingleAgentIdentity.SINGLE_AGENT, self.model, "fake-model", "completed", 1, TokenUsage(1, 2, 3))
        with self.assertRaises(TelemetryStoreError):
            self.store.append_usage(binding, record)


if __name__ == "__main__":
    unittest.main()
