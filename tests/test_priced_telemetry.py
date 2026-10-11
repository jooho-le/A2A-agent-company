"""Approved synthetic rates over real local accounting; not paid API evidence."""

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from agents.llm.budget import LLMLimits
from agents.llm.contracts import TokenUsage, UsageRecord
from agents.platform.budgets import RunBudgetRegistry
from agents.platform.pricing import load_price_book
from agents.platform.telemetry_store import AgentTelemetryStore, TelemetryStoreError
from orchestrator.domain.trace import TraceEvent
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository


def synthetic_prices(*, mode="NOT_APPLICABLE"):
    # Explicit test fixture values, never an approved operational model/price.
    return {"schemaVersion": 1, "priceBookId": "inert-pricing-fixture", "priceBookVersion": "1.0.0",
        "sourceRef": "https://developers.openai.com/api/docs/pricing", "currency": "USD",
        "billingScope": "OPENAI_RESPONSES_GLOBAL_TEXT", "entries": [{
            "entryId": "inert-text-rate", "provider": "openai", "requestedModelId": "fixture-model",
            "reportedModelId": "fixture-model", "serviceTier": "default", "minInputTokens": 0,
            "maxInputTokens": None, "cacheWriteMode": mode, "inputUsdPerMillion": "2",
            "cachedInputUsdPerMillion": "0.5", "cacheWriteUsdPerMillion": "2.5" if mode == "METERED" else None,
            "outputUsdPerMillion": "10"}]}


class PricedTelemetryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-price-accounting-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.path = self.directory / "price-book.json"
        self.definition = synthetic_prices()
        self.book = self.write_book()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.current = datetime.now(timezone.utc)
        self.model = ModelConfiguration(provider="openai", model_id="fixture-model", temperature=0)
        self.initialize_run()

    def initialize_run(self):
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="inert requirements",
            status=WorkflowStatus.PLANNING, created_at=self.current)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.RUNNING)
        self.frozen = RunConfigurationArtifact(run_id=self.run.run_id, scenario_id=self.run.scenario_id,
            workspace_id=self.run.workspace_id, configuration=RunConfiguration(model=self.model,
                limits=ExecutionLimits(runtime_budget_ms=60000)))
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.frozen)
        self.store = AgentTelemetryStore(self.repository.database_path, price_book=self.book, clock=lambda: self.current)
        self.registry = RunBudgetRegistry(self.repository, limits=LLMLimits(
            max_model_calls=10, max_tool_calls=10, max_total_tokens=None), telemetry_store=self.store)
        self.budget = self.registry.admit(self.run.run_id)
        self.store.pin_pricing(self.run.run_id)
        self.binding = {"runId": str(self.run.run_id), "workflowStepId": str(self.step.workflow_step_id),
            "a2aTaskId": "opaque-task", "agentContextId": "opaque-context", "role": "PLANNER",
            "attempt": 0, "codeVersion": None, "requirementIds": [], "inputArtifactIds": [], "snapshotSha256": None}

    def write_book(self):
        self.path.write_text(json.dumps(self.definition), encoding="utf-8")
        return load_price_book(self.path)

    def account(self, usage=None, **changes):
        sequence = self.budget.reserve_model_call()[0]
        self.budget.account_usage(usage, sequence=sequence)
        values = dict(sequence=sequence, role=AgentRole.PLANNER, requested_model=self.model,
            reported_model_id="fixture-model", outcome="completed", duration_ms=3, usage=usage,
            reported_service_tier="default", billing_context="OPENAI_RESPONSES_GLOBAL_TEXT")
        record = UsageRecord(**{**values, **changes})
        return record, self.store.append_usage(self.binding, record)

    def test_measured_tokens_price_exactly_without_double_counting_reasoning(self):
        record, saved = self.account(TokenUsage(100, 30, 130, cached_input_tokens=40, reasoning_output_tokens=20))
        self.assertEqual(saved["costUsd"], "0.00044")
        self.assertEqual(saved["costStatus"], "KNOWN")
        self.assertEqual(record.cost_usd, None)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertTrue(summary["costComplete"])
        self.assertEqual(summary["costUsd"], "0.00044")
        self.assertEqual(summary["priceBookSha256"], self.book.sha256)
        with self.store._connection() as connection:
            raw = json.loads(connection.execute("SELECT payload_json FROM agent_runtime_events WHERE kind='LLM'").fetchone()[0])
        self.assertIsNone(raw["detail"]["costUsd"])

    def test_cache_write_input_is_a_subset_not_a_second_full_input_charge(self):
        self.definition = synthetic_prices(mode="METERED")
        self.book = self.write_book()
        # Different policies require a fresh Run; the old row is never changed.
        self.initialize_run()
        _record, saved = self.account(TokenUsage(100, 30, 130, cached_input_tokens=40,
            reasoning_output_tokens=20, cache_write_input_tokens=20))
        self.assertEqual(saved["costUsd"], "0.00045")

    def test_missing_tier_or_actual_model_never_infers_a_price(self):
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40), reported_service_tier=None)
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40), reported_model_id=None)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertTrue(summary["usageComplete"])
        self.assertFalse(summary["costComplete"])
        self.assertIsNone(summary["costUsd"])
        self.assertEqual(summary["knownCostUsd"], "0.00044")
        self.assertEqual(summary["unknownCostSequences"], [2, 3])

    def test_failed_and_refused_calls_with_known_usage_are_not_free(self):
        for outcome in ("failed", "refused", "incomplete"):
            self.account(TokenUsage(100, 30, 130, cached_input_tokens=40), outcome=outcome)
        self.assertEqual(self.store.usage_summary(self.run.run_id)["costUsd"], "0.00132")

    def test_pending_model_or_missing_usage_receipt_keeps_full_cost_null(self):
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        sequence = self.budget.reserve_model_call()[0]
        summary = self.store.usage_summary(self.run.run_id)
        self.assertIsNone(summary["costUsd"])
        self.assertEqual(summary["unknownCostSequences"], [sequence])
        self.budget.account_usage(TokenUsage(20, 3, 23), sequence=sequence)
        summary = self.store.usage_summary(self.run.run_id)
        self.assertFalse(summary["usageComplete"])
        self.assertFalse(summary["costComplete"])
        self.assertEqual(summary["missingUsageSequences"], [sequence])

    def test_unknown_usage_and_missing_cache_details_are_not_zero(self):
        self.account(None, outcome="LLM_PROVIDER_ERROR")
        self.account(TokenUsage(100, 30, 130))
        records, _total = self.store.list_usage(self.run.run_id)
        self.assertEqual([item["costStatus"] for item in records], ["USAGE_UNKNOWN", "TOKEN_DETAILS_UNKNOWN"])
        self.assertTrue(all(item["costUsd"] is None for item in records))
        self.assertIsNone(self.store.usage_summary(self.run.run_id)["costUsd"])

    def test_pages_and_telemetry_views_do_not_change_full_run_cost(self):
        for _ in range(3):
            self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        records, total = self.store.list_usage(self.run.run_id, limit=1, offset=1)
        self.assertEqual(total, 3)
        self.assertEqual(records[0]["detail"]["costUsd"], "0.00044")
        events, _ = self.store.list_events(self.run.run_id)
        self.assertEqual(events[0]["costUsd"], "0.00044")
        self.assertEqual(self.store.usage_summary(self.run.run_id)["costUsd"], "0.00132")

    def test_historical_cost_uses_frozen_prices_when_current_file_changes(self):
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        self.definition["entries"][0]["inputUsdPerMillion"] = "99"
        self.write_book()
        history = AgentTelemetryStore(self.repository.database_path)
        self.assertEqual(history.usage_summary(self.run.run_id)["costUsd"], "0.00044")
        with self.assertRaises(TelemetryStoreError):
            self.store.pin_pricing(self.run.run_id)

    def test_inflight_usage_survives_approval_changes_but_new_model_admission_is_blocked(self):
        self.definition["entries"][0]["inputUsdPerMillion"] = "99"
        self.write_book()
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        self.path.unlink()
        # A second already-in-flight measured result still uses the frozen book.
        self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        summary = self.store.usage_summary(self.run.run_id)
        self.assertEqual(summary["costUsd"], "0.00088")
        self.assertEqual(summary["missingUsageSequences"], [])
        with self.assertRaises(TelemetryStoreError):
            self.store.pin_pricing(self.run.run_id)
        event = TraceEvent(runId=self.run.run_id, workflowStepId=self.step.workflow_step_id,
            a2aTaskId="opaque-task", agentContextId="opaque-context", actor="PLANNER", attempt=0,
            eventType="LLM_MODEL_CALLED", occurredAt=self.current)
        with self.assertRaises(TelemetryStoreError):
            self.store.append_event(event, kind="LLM", detail={"sequence": 3,
                "requestedModel": self.model.model_dump(mode="json", by_alias=True)},
                idempotency_key="model:3")

    def test_unapproved_legacy_run_is_not_backfilled_or_converted_to_zero(self):
        # Use a different initially unpriced Run, not the approved Run above.
        run = self.run.model_copy(update={"run_id": uuid4(), "workspace_id": uuid4()})
        step = self.step.model_copy(update={"run_id": run.run_id, "workflow_step_id": uuid4()})
        frozen = self.frozen.model_copy(update={"run_id": run.run_id, "workspace_id": run.workspace_id, "artifact_id": uuid4()})
        self.repository.create_run(run, (step,), (), run_configuration=frozen)
        store = AgentTelemetryStore(self.repository.database_path, clock=lambda: self.current)
        registry = RunBudgetRegistry(self.repository, limits=self.registry._limits, telemetry_store=store)
        budget = registry.admit(run.run_id)
        sequence = budget.reserve_model_call()[0]
        usage = TokenUsage(100, 30, 130, cached_input_tokens=40)
        budget.account_usage(usage, sequence=sequence)
        binding = {**self.binding, "runId": str(run.run_id), "workflowStepId": str(step.workflow_step_id)}
        store.append_usage(binding, UsageRecord(sequence, AgentRole.PLANNER, self.model, "fixture-model", "completed", 3, usage))
        self.assertIsNone(store.usage_summary(run.run_id)["costUsd"])
        self.assertIsNone(store.usage_summary(run.run_id)["knownCostUsd"])
        with self.assertRaises(TelemetryStoreError):
            self.store.pin_pricing(run.run_id)

    def test_usage_cannot_inject_cost_and_exact_replay_does_not_double_charge(self):
        record, first = self.account(TokenUsage(100, 30, 130, cached_input_tokens=40))
        again = self.store.append_usage(self.binding, record)
        self.assertEqual(first["eventId"], again["eventId"])
        self.assertEqual(self.store.usage_summary(self.run.run_id)["costUsd"], "0.00044")
        object.__setattr__(record, "cost_usd", "999")
        with self.assertRaises(TelemetryStoreError):
            self.store.append_usage(self.binding, record)

    def test_large_valid_token_counts_keep_exact_receipts_and_summary(self):
        # Synthetic boundary, not a plausible or actually billed API response.
        count = 10 ** 180 + 1
        expected = str(2 * 10 ** 174) + ".000002"
        self.account(TokenUsage(count, 0, count, cached_input_tokens=0))
        self.account(TokenUsage(1, 0, 1, cached_input_tokens=0))
        records, _ = self.store.list_usage(self.run.run_id)
        self.assertEqual(records[0]["costUsd"], expected)
        self.assertEqual(self.store.usage_summary(self.run.run_id)["costUsd"],
                         str(2 * 10 ** 174) + ".000004")
