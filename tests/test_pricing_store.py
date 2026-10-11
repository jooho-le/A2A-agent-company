"""Private pricing receipts against real temporary Workflow/Budget/Usage rows.

Every rate, identity and usage here is an inert authored fixture. These tests
do not call a provider, calculate current vendor prices or execute products.
"""

from contextlib import contextmanager
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from agents.llm.budget import LLMLimits
from agents.llm.contracts import TokenUsage, UsageRecord
from agents.platform.budgets import RunBudgetRegistry
from agents.platform.pricing import load_price_book
from agents.platform.pricing_store import PricingStoreError, RunPricingLedger
from agents.platform.telemetry_store import AgentTelemetryStore
from orchestrator.domain import AgentRole, FinalVerdict, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def fixture_prices(*, output_rate="4", version="inert-1"):
    return {"schemaVersion": 1, "priceBookId": "inert-prices", "priceBookVersion": version,
        "sourceRef": "https://developers.openai.com/api/docs/pricing", "currency": "USD",
        "billingScope": "OPENAI_RESPONSES_GLOBAL_TEXT", "entries": [{
            "entryId": "inert-default", "provider": "openai", "requestedModelId": "fixture-model",
            "reportedModelId": "fixture-model-2099-01-01", "serviceTier": "default",
            "minInputTokens": 0, "maxInputTokens": None, "cacheWriteMode": "NOT_APPLICABLE",
            "inputUsdPerMillion": "2", "cachedInputUsdPerMillion": "0.5",
            "cacheWriteUsdPerMillion": None, "outputUsdPerMillion": output_rate}]}


class PricingStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-private-pricing-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.approval = self.directory / "inert-prices.json"
        self.approval.write_text(_json(fixture_prices()), encoding="utf-8")
        self.book = load_price_book(self.approval)
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.now = datetime.now(timezone.utc)
        self.telemetry = AgentTelemetryStore(self.repository.database_path, clock=lambda: self.now, price_book=self.book)
        self.registry = RunBudgetRegistry(self.repository, limits=LLMLimits(max_model_calls=16), telemetry_store=self.telemetry)
        self.ledger = RunPricingLedger()
        self.model = ModelConfiguration(provider="openai", model_id="fixture-model", temperature=0)
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="INERT_PRICING_FIXTURE",
                               status=WorkflowStatus.PLANNING, created_at=self.now)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER, status=WorkflowStepStatus.RUNNING)
        self.configuration = RunConfigurationArtifact(run_id=self.run.run_id, scenario_id=self.run.scenario_id,
            workspace_id=self.run.workspace_id,
            configuration=RunConfiguration(model=self.model, limits=ExecutionLimits(runtime_budget_ms=60000)))
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.configuration)
        self.budget = self.registry.admit(self.run.run_id)
        self.binding = {"runId": str(self.run.run_id), "workflowStepId": str(self.step.workflow_step_id),
            "a2aTaskId": "opaque-inert-task", "agentContextId": "opaque-inert-context", "role": "PLANNER",
            "attempt": 0, "codeVersion": None, "requirementIds": [], "inputArtifactIds": [], "snapshotSha256": None}

    @contextmanager
    def connection(self, *, write=False):
        if write:
            with self.repository._transaction() as connection:
                yield connection
        else:
            with sqlite3.connect(self.repository.database_path.as_uri() + "?mode=ro", uri=True) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                yield connection

    def call(self, method, *arguments, write=False):
        with self.connection(write=write) as connection:
            return getattr(self.ledger, method)(connection, self.run.run_id, *arguments)

    def tables(self):
        with self.connection() as connection:
            return tuple(row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    def pin(self):
        self.call("pin", self.book, write=True)

    def usage(self, *, known=True, outcome="completed", model="fixture-model-2099-01-01", tier="default",
              context="OPENAI_RESPONSES_GLOBAL_TEXT", cached=2, role=None):
        self.pin()
        sequence, _, _ = self.budget.reserve_model_call()
        tokens = TokenUsage(10, 5, 15, cached_input_tokens=cached, reasoning_output_tokens=2) if known else None
        self.budget.account_usage(tokens, sequence=sequence)
        record = UsageRecord(sequence=sequence, role=role or AgentRole.PLANNER, requested_model=self.model,
            reported_model_id=model, outcome=outcome, duration_ms=3, usage=tokens,
            reported_service_tier=tier, billing_context=context)
        self.telemetry.append_usage(self.binding, record)
        return sequence

    def rejected(self, method, *arguments, write=False):
        with self.assertRaises(PricingStoreError) as caught:
            self.call(method, *arguments, write=write)
        self.assertEqual(caught.exception.code, "RUN_PRICING_UNAVAILABLE")
        self.assertEqual(str(caught.exception), "RUN_PRICING_UNAVAILABLE")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(str(self.directory), str(caught.exception))

    def change_run(self, **changes):
        run = WorkflowRun.model_validate(self.run.model_copy(update=changes).model_dump())
        with self.connection(write=True) as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (run.status.value, run.model_dump_json(), str(run.run_id)))

    def change_step(self, **changes):
        step = WorkflowStep.model_validate(self.step.model_copy(update=changes).model_dump())
        with self.connection(write=True) as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                               (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def drop_guards(self, table):
        with self.connection(write=True) as connection:
            for operation in ("update", "delete", "replace"):
                connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_{operation}")

    def tamper_receipt(self, mutate, *, consistent_hash=True):
        self.drop_guards("agent_llm_cost_receipts")
        with self.connection(write=True) as connection:
            row = connection.execute("SELECT payload_json FROM agent_llm_cost_receipts WHERE run_id=? AND model_sequence=1",
                                     (str(self.run.run_id),)).fetchone()
            data = json.loads(row[0])
            mutate(data)
            connection.execute("UPDATE agent_llm_cost_receipts SET payload_json=?,metadata_sha256=? WHERE run_id=? AND model_sequence=1",
                               (_json(data), _hash(data) if consistent_hash else "f" * 64, str(self.run.run_id)))

    def test_constructor_is_inert(self):
        with patch.object(sqlite3, "connect", side_effect=AssertionError("NO_IO")):
            self.assertEqual(repr(RunPricingLedger()), "RunPricingLedger()")
        self.assertNotIn("agent_run_pricing", self.tables())

    def test_absent_policy_and_usage_are_read_only_none(self):
        before = self.tables()
        with patch("agents.platform.pricing_store._schema", side_effect=AssertionError("NO_DDL")):
            self.assertIsNone(self.call("policy"))
            self.assertIsNone(self.call("get", 1))
            self.rejected("require", self.book)
        self.assertEqual(before, self.tables())

    def test_pin_fresh_claim_freezes_descriptor_and_sha_without_canonical_changes(self):
        events = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        self.pin()
        self.assertEqual(self.call("policy"), {"descriptor": self.book.descriptor, "priceBookSha256": self.book.sha256})
        self.assertEqual(self.repository.get_run(self.run.run_id), self.run)
        self.assertEqual(self.repository.list_steps(self.run.run_id), [self.step])
        self.assertEqual(events, self.repository.list_events(self.run.run_id, limit=100, offset=0))
        self.assertEqual(self.repository.get_run_configuration(self.run.run_id), self.configuration)

    def test_repeated_pin_matches_without_ddl_or_replacement(self):
        self.pin()
        with patch("agents.platform.pricing_store._schema", side_effect=AssertionError("NO_DDL")):
            self.pin()
            self.assertIsNone(self.call("require", self.book))

    def test_policy_public_value_is_detached(self):
        self.pin()
        data = self.call("policy")
        data["descriptor"]["priceBook"]["entries"][0]["outputUsdPerMillion"] = "0"
        self.assertEqual(self.call("policy")["descriptor"], self.book.descriptor)

    def test_same_book_reopens_and_is_idempotent_after_completion(self):
        self.pin()
        self.change_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        self.change_step(status=WorkflowStepStatus.SUCCEEDED)
        self.ledger = RunPricingLedger()
        self.pin()
        self.assertIsNone(self.call("require", self.book))

    def test_initial_pin_rejects_wrong_state_or_fix_or_code_version(self):
        for changes in ({"status": WorkflowStatus.RECEIVED}, {"status": WorkflowStatus.IMPLEMENTING},
                        {"fix_attempt": 1}, {"code_version": 1}):
            with self.subTest(changes=changes):
                self.change_run(**changes)
                self.rejected("pin", self.book, write=True)
                self.assertNotIn("agent_run_pricing", self.tables())
        self.change_run()

    def test_initial_pin_rejects_non_running_non_planner_or_later_attempt(self):
        for changes in ({"status": WorkflowStepStatus.PENDING}, {"agent_role": AgentRole.DEVELOPER}, {"attempt": 1}):
            with self.subTest(changes=changes):
                self.change_step(**changes)
                self.rejected("pin", self.book, write=True)
        self.change_step()

    def test_initial_pin_rejects_second_step(self):
        step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.QA)
        with self.connection(write=True) as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (str(step.workflow_step_id), str(step.run_id), step.status.value, step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))
        self.rejected("pin", self.book, write=True)

    def test_first_pending_model_reservation_can_pin_but_second_cannot(self):
        self.budget.reserve_model_call()
        self.pin()
        self.assertEqual(self.call("policy")["priceBookSha256"], self.book.sha256)

    def test_two_pending_reservations_cannot_backfill_prices(self):
        self.budget.reserve_model_call()
        self.budget.reserve_model_call()
        self.rejected("pin", self.book, write=True)

    def test_already_accounted_model_cannot_backfill_prices(self):
        sequence = self.budget.reserve_model_call()[0]
        self.budget.account_usage(TokenUsage(1, 1, 2), sequence=sequence)
        self.rejected("pin", self.book, write=True)

    def test_old_unpriced_usage_never_receives_policy_or_receipt(self):
        legacy = AgentTelemetryStore(self.repository.database_path, clock=lambda: self.now)
        sequence = self.budget.reserve_model_call()[0]
        tokens = TokenUsage(10, 5, 15, cached_input_tokens=2)
        self.budget.account_usage(tokens, sequence=sequence)
        legacy.append_usage(self.binding, UsageRecord(sequence, AgentRole.PLANNER, self.model,
            "fixture-model-2099-01-01", "completed", 3, tokens))
        self.rejected("pin", self.book, write=True)
        self.assertIsNone(self.call("policy"))
        self.assertIsNone(self.call("get", sequence))

    def test_changed_book_cannot_replace_policy(self):
        self.pin()
        other_file = self.directory / "inert-other.json"
        other_file.write_text(_json(fixture_prices(output_rate="9", version="inert-2")), encoding="utf-8")
        other = load_price_book(other_file)
        self.rejected("pin", other, write=True)
        self.rejected("require", other)
        self.assertEqual(self.call("policy")["descriptor"], self.book.descriptor)

    def test_current_approval_change_rejects_execution_but_historical_reads_survive(self):
        sequence = self.usage()
        previous = self.call("get", sequence)
        self.approval.write_text(_json(fixture_prices(output_rate="9", version="inert-2")), encoding="utf-8")
        self.rejected("require", self.book)
        self.rejected("pin", self.book, write=True)
        self.assertEqual(self.call("get", sequence), previous)

    def test_historical_reads_do_not_require_approval_file(self):
        sequence = self.usage()
        self.approval.unlink()
        self.assertEqual(self.call("get", sequence)["costUsd"], "0.000037")

    def test_publish_actual_usage_exact_decimal_and_metadata_only(self):
        sequence = self.usage()
        value = self.call("get", sequence)
        self.assertEqual(value, {"costUsd": "0.000037", "pricingEntryId": "inert-default", "costStatus": "KNOWN",
            "priceBookSha256": self.book.sha256, "priceBookId": "inert-prices", "priceBookVersion": "inert-1",
            "calculationVersion": "USD_PER_MILLION_V1"})
        with self.connection() as connection:
            payload = connection.execute("SELECT payload_json FROM agent_llm_cost_receipts").fetchone()[0]
            self.assertNotIn("INERT_PRICING_FIXTURE", payload)
            self.assertNotIn("requestedModel", payload)
            self.assertNotIn(str(self.directory), payload)
            self.assertEqual(json.loads(payload)["usageEventId"], self.telemetry.list_usage(self.run.run_id)[0][0]["eventId"])

    def test_exact_duplicate_publish_is_idempotent(self):
        sequence = self.usage()
        expected = self.call("get", sequence)
        self.assertEqual(self.call("publish", sequence, write=True), expected)
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_llm_cost_receipts").fetchone()[0], 1)

    def test_unknown_usage_identity_tier_context_and_cache_remain_null(self):
        for settings, reason in (({"known": False}, "USAGE_UNKNOWN"), ({"model": None}, "MODEL_UNMATCHED"),
                                 ({"tier": None}, "TIER_UNKNOWN"), ({"context": None}, "CONTEXT_UNKNOWN"),
                                 ({"cached": None}, "TOKEN_DETAILS_UNKNOWN")):
            with self.subTest(reason=reason):
                sequence = self.usage(**settings)
                receipt = self.call("get", sequence)
                self.assertIsNone(receipt["costUsd"])
                self.assertEqual(receipt["costStatus"], reason)

    def test_failed_incomplete_refused_usage_is_still_charged(self):
        for outcome in ("failed", "incomplete", "refused", "LLM_RESPONSE_INVALID"):
            with self.subTest(outcome=outcome):
                sequence = self.usage(outcome=outcome)
                self.assertEqual(self.call("get", sequence)["costUsd"], "0.000037")

    def test_missing_cost_receipt_is_corruption_not_historical_backfill(self):
        sequence = self.usage()
        self.drop_guards("agent_llm_cost_receipts")
        with self.connection(write=True) as connection:
            connection.execute("DELETE FROM agent_llm_cost_receipts WHERE run_id=?", (str(self.run.run_id),))
        self.rejected("get", sequence)
        self.assertIsNone(self.call("get", sequence + 1))

    def test_publish_requires_actual_usage_and_policy(self):
        self.rejected("publish", 1, write=True)
        self.pin()
        self.rejected("publish", 1, write=True)
        self.assertIsNone(self.call("get", 1))

    def test_changed_amount_rejected_even_with_consistent_receipt_hash(self):
        self.usage()
        self.tamper_receipt(lambda data: data.update(costUsd="0"))
        self.rejected("get", 1)
        self.rejected("publish", 1, write=True)

    def test_receipt_noncanonical_bool_sequence_cannot_equal_integer(self):
        self.usage()
        self.tamper_receipt(lambda data: data.update(sequence=True))
        self.rejected("get", 1)

    def test_changed_receipt_hash_rejected(self):
        self.usage()
        self.tamper_receipt(lambda data: None, consistent_hash=False)
        self.rejected("get", 1)

    def test_changed_usage_payload_is_detected_by_full_hash(self):
        self.usage()
        with self.connection(write=True) as connection:
            connection.execute("DROP TRIGGER agent_telemetry_no_update")
            row = connection.execute("SELECT payload_json FROM agent_runtime_events WHERE idempotency_key='usage:1'").fetchone()
            data = json.loads(row[0])
            data["detail"]["reportedModelId"] = "fixture-model-2099-01-02"
            connection.execute("UPDATE agent_runtime_events SET payload_json=? WHERE idempotency_key='usage:1'", (_json(data),))
        self.rejected("get", 1)

    def test_changed_original_trace_binding_rejected(self):
        self.usage()
        with self.connection(write=True) as connection:
            connection.execute("DROP TRIGGER trace_events_no_update")
            row = connection.execute("SELECT event_id,payload_json FROM trace_events WHERE event_type='LLM_CALL_FINISHED'").fetchone()
            data = json.loads(row[1])
            data["a2aTaskId"] = "other-task"
            connection.execute("UPDATE trace_events SET payload_json=? WHERE event_id=?", (_json(data), row[0]))
        self.rejected("get", 1)

    def test_policy_metadata_or_descriptor_tampering_rejected(self):
        self.pin()
        self.drop_guards("agent_run_pricing")
        with self.connection(write=True) as connection:
            connection.execute("UPDATE agent_run_pricing SET price_book_sha256=? WHERE run_id=?", ("f" * 64, str(self.run.run_id)))
        self.rejected("policy")
        self.rejected("require", self.book)

    def test_extra_cost_prose_or_host_path_never_accepted(self):
        self.usage()
        self.tamper_receipt(lambda data: data.update(prompt="password=INERT_DO_NOT_LOG"))
        self.rejected("get", 1)

    def test_missing_policy_with_existing_receipt_is_corruption(self):
        self.usage()
        self.drop_guards("agent_run_pricing")
        with self.connection(write=True) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
        # Deliberate privileged fixture damage; normal writes keep foreign keys.
        with sqlite3.connect(self.repository.database_path) as connection:
            connection.execute("DELETE FROM agent_run_pricing WHERE run_id=?", (str(self.run.run_id),))
        self.rejected("policy")
        self.rejected("get", 1)

    def test_sql_update_delete_and_replace_are_blocked_for_both_tables(self):
        self.usage()
        for table in ("agent_run_pricing", "agent_llm_cost_receipts"):
            for operation in ("UPDATE", "DELETE", "REPLACE"):
                with self.subTest(table=table, operation=operation), self.connection(write=True) as connection:
                    with self.assertRaises(sqlite3.IntegrityError):
                        if operation == "UPDATE":
                            connection.execute(f"UPDATE {table} SET metadata_sha256='x'")
                        elif operation == "DELETE":
                            connection.execute(f"DELETE FROM {table}")
                        else:
                            connection.execute(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}")
        self.assertEqual(self.call("get", 1)["costUsd"], "0.000037")

    def test_caller_rollback_removes_pin_and_ddl(self):
        with self.repository._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.ledger.pin(connection, self.run.run_id, self.book)
            connection.rollback()
        self.assertNotIn("agent_run_pricing", self.tables())

    def test_mutation_requires_caller_transaction(self):
        with self.repository._connection() as connection:
            with self.assertRaises(PricingStoreError):
                self.ledger.pin(connection, self.run.run_id, self.book)
        self.assertNotIn("agent_run_pricing", self.tables())

    def test_unknown_run_and_bad_uuid_sequence_connection_rejected(self):
        with self.connection(write=True) as connection:
            for run_id in (uuid4(), uuid1(), str(self.run.run_id), None):
                with self.subTest(run_id=run_id), self.assertRaises(PricingStoreError):
                    self.ledger.pin(connection, run_id, self.book)
            for sequence in (True, False, 0, -1, "1", 1.0):
                with self.subTest(sequence=sequence), self.assertRaises(PricingStoreError):
                    self.ledger.get(connection, self.run.run_id, sequence)
        for connection in (None, object(), str(self.repository.database_path)):
            with self.assertRaises(PricingStoreError):
                self.ledger.policy(connection, self.run.run_id)

    def test_historical_receipt_survives_step_and_run_completion(self):
        sequence = self.usage()
        expected = self.call("get", sequence)
        self.change_step(status=WorkflowStepStatus.SUCCEEDED, attempt=1)
        self.change_run(status=WorkflowStatus.ABORTED, termination_reason="INERT_STOP")
        self.assertEqual(self.call("get", sequence), expected)

    def test_four_roles_share_global_sequences_and_all_costs_are_recorded(self):
        self.usage()
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            # These are authored accounting-only Step/Usage fixtures, not
            # executable Developer/QA/Security products or their verdicts.
            step = WorkflowStep(run_id=self.run.run_id, agent_role=role, status=WorkflowStepStatus.RUNNING)
            with self.connection(write=True) as connection:
                connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                    (str(step.workflow_step_id), str(step.run_id), step.status.value, step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))
            self.binding = {**self.binding, "workflowStepId": str(step.workflow_step_id), "role": role.value}
            sequence = self.usage(role=role)
            self.assertEqual(sequence, list(AgentRole).index(role) + 1)
            self.assertEqual(self.call("get", sequence)["costUsd"], "0.000037")
        with self.connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM agent_llm_cost_receipts").fetchone()[0], 4)

    def test_original_usage_hash_binds_bytes_not_only_decoded_json(self):
        self.usage()
        with self.connection(write=True) as connection:
            connection.execute("DROP TRIGGER agent_telemetry_no_update")
            connection.execute("UPDATE agent_runtime_events SET payload_json=' '||payload_json WHERE idempotency_key='usage:1'")
        self.rejected("get", 1)

    def test_legacy_accounted_cache_write_absent_matches_default_none(self):
        sequence = self.usage()
        with self.connection() as connection:
            usage = json.loads(connection.execute("SELECT payload_json FROM agent_runtime_events WHERE idempotency_key='usage:1'").fetchone()[0])
        self.assertNotIn("cacheWriteInputTokens", usage["detail"])
        self.assertEqual(self.call("get", sequence)["costStatus"], "KNOWN")

    def test_receipt_entry_and_reason_cannot_be_changed_with_valid_hash(self):
        self.usage()
        self.tamper_receipt(lambda data: data.update(pricingEntryId="inert-other", costStatus="RATE_UNMATCHED", costUsd=None))
        self.rejected("get", 1)

    def test_usage_count_coercion_in_budget_is_not_accepted(self):
        self.usage()
        with self.connection(write=True) as connection:
            data = json.loads(connection.execute("SELECT payload_json FROM agent_budget_ledger WHERE run_id=?", (str(self.run.run_id),)).fetchone()[0])
            data["accounted_usage"]["1"]["input_tokens"] = 10.0
            connection.execute("UPDATE agent_budget_ledger SET payload_json=? WHERE run_id=?", (_json(data), str(self.run.run_id)))
        self.rejected("get", 1)

    def test_wrong_stored_step_role_cannot_reassign_usage(self):
        self.usage()
        self.change_step(agent_role=AgentRole.DEVELOPER)
        self.rejected("get", 1)

    def test_stored_untrusted_amount_is_never_used(self):
        self.usage()
        with self.connection(write=True) as connection:
            connection.execute("DROP TRIGGER agent_telemetry_no_update")
            row = connection.execute("SELECT payload_json FROM agent_runtime_events WHERE idempotency_key='usage:1'").fetchone()
            data = json.loads(row[0])
            data["detail"]["costUsd"] = "0"
            connection.execute("UPDATE agent_runtime_events SET payload_json=? WHERE idempotency_key='usage:1'", (_json(data),))
        self.rejected("get", 1)

    def test_closed_policy_revalidated_even_when_metadata_hash_is_consistent(self):
        self.pin()
        self.drop_guards("agent_run_pricing")
        with self.connection(write=True) as connection:
            descriptor = self.book.descriptor
            descriptor["priceBook"]["operatorComment"] = "DO_NOT_STORE_FREEFORM_PROSE"
            book_hash = _hash(descriptor)
            metadata = {"runId": str(self.run.run_id), "descriptor": descriptor, "priceBookSha256": book_hash}
            connection.execute("UPDATE agent_run_pricing SET descriptor_json=?,price_book_sha256=?,metadata_sha256=?",
                               (_json(descriptor), book_hash, _hash(metadata)))
        self.rejected("policy")

    def test_unknown_run_is_not_silently_a_legacy_unpriced_run(self):
        with self.connection() as connection:
            for method, arguments in (("policy", ()), ("get", (1,))):
                with self.subTest(method=method), self.assertRaises(PricingStoreError):
                    getattr(self.ledger, method)(connection, uuid4(), *arguments)

    def test_first_pin_rejects_coerced_workflow_counters(self):
        with self.connection(write=True) as connection:
            data = json.loads(connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (str(self.run.run_id),)).fetchone()[0])
            data["fix_attempt"] = False
            connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?", (_json(data), str(self.run.run_id)))
        self.rejected("pin", self.book, write=True)
        self.assertNotIn("agent_run_pricing", self.tables())


if __name__ == "__main__":
    unittest.main()
