"""Real Git/SQLite logical-call ledger; no Docker/LLM/product execution."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

import test_mcp_unit_store as unit_fixtures
import test_mcp_build_store as build_fixtures
import test_mcp_browser_store as browser_fixtures
import test_mcp_security_store as security_fixtures
from mcp_tools.execution_store import ToolExecutionStore, ToolStoreError, ToolAttemptToken
from mcp_tools.runtime import MCPBinding
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.retry_policy import RetryDecision
from orchestrator.domain.states import FinalVerdict
from orchestrator.domain.tool_evidence import ToolExecutionOutcome


class ToolExecutionStoreTests(unittest.TestCase):
    def setUp(self):
        self.use_fixture(unit_fixtures.UnitTestOutputStoreTests)

    def use_fixture(self, fixture_type):
        fixture = fixture_type("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        for name in ("repository", "binding", "source", "run", "step"):
            setattr(self, name, getattr(fixture, name))
        self.store = ToolExecutionStore(self.repository)
        self.tool_name = fixture.result.tool_name

    def create(self, **changes):
        kwargs = dict(source_artifact_id=self.source.artifact_id, configuration_sha256="b" * 64,
            selector_sha256=sha256(self.fixture.result.profile_name.encode()).hexdigest() if self.tool_name.startswith("run_") else None)
        kwargs.update(changes)
        return self.store.create(self.binding, self.step.workflow_step_id, self.tool_name, "a" * 64, **kwargs)

    def finish_error(self, token, **changes):
        kwargs = dict(outcome=ToolExecutionOutcome.UNVERIFIED, error_kind="PROCESS_STARTUP_FAILURE",
            retry_safe=True, retry_decision=RetryDecision.RETRY, duration_ms=10, delivery_state="NOT_SENT")
        kwargs.update(changes)
        return self.store.finish(self.binding, token, **kwargs)

    def finish_success(self, record=None, output=None):
        record = self.create() if record is None else record
        token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        output = receipt.tool_output() if output is None else output
        return self.store.finish(self.binding, token, outcome="PASS", output=output), receipt

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(ToolStoreError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def rewrite(self, table, where, changes):
        with self.repository._transaction() as connection:
            connection.execute(f"DROP TRIGGER IF EXISTS {table}_no_update")
            row = connection.execute(f"SELECT payload_json FROM {table} WHERE {where[0]}=?", (where[1],)).fetchone()
            data = json.loads(row[0])
            data.update(changes)
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
            connection.execute(f"UPDATE {table} SET payload_json=?,payload_sha256=? WHERE {where[0]}=?",
                (raw, sha256(raw.encode()).hexdigest(), where[1]))

    def test_constructor_inert_and_safe_repr(self):
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("must be inert")):
            store = ToolExecutionStore(self.repository)
        self.assertEqual(repr(store), "ToolExecutionStore()")
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='tool_execution_calls'").fetchone())

    def test_constructor_requires_repository(self):
        self.assert_code("TOOL_EVIDENCE_INVALID", ToolExecutionStore, object())

    def test_call_frozen_snapshot_identity_and_read_evidence(self):
        record = self.create()
        self.assertEqual(record.logical_call_id.version, 4)
        self.assertEqual(record.execution_manifest, self.source.execution_manifest())
        self.assertEqual(record.input_sha256, "a" * 64)
        self.assertEqual(record.configuration_sha256, "b" * 64)
        self.assertEqual(record.attempts, ())
        self.assertEqual(self.store.get(self.binding, record.logical_call_id), record)
        self.assertEqual(self.store.read_evidence(self.binding, record.evidence_ref), record.to_dict())
        with self.assertRaises(FrozenInstanceError):
            record.tool_name = "apply_patch"

    def test_claim_persists_started_and_prevents_reentry(self):
        record = self.create()
        token = self.store.claim(self.binding, record.logical_call_id)
        self.assertEqual(token.attempt, 0)
        self.assertEqual(token.attempt_id.version, 4)
        self.assertNotEqual(token.attempt_id, record.logical_call_id)
        started = self.store.get(self.binding, record.logical_call_id)
        self.assertEqual(started.attempts[0].status, "STARTED")
        self.assertEqual(started.attempts[0].outcome, None)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_INCOMPLETE", started.to_tool_evidence)
        self.assertEqual(self.store.read_evidence(self.binding, token.evidence_ref)["status"], "STARTED")

    def test_concurrent_claim_only_one_enters(self):
        record = self.create()
        def claim():
            try:
                return self.store.claim(self.binding, record.logical_call_id)
            except ToolStoreError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=4) as executor:
            outputs = list(executor.map(lambda _: claim(), range(4)))
        self.assertEqual(sum(isinstance(item, ToolAttemptToken) for item in outputs), 1)
        self.assertEqual(outputs.count("TOOL_EVIDENCE_RETRY_DENIED"), 3)

    def test_three_attempts_are_initial_plus_two_retries(self):
        record = self.create()
        for index in range(3):
            token = self.store.claim(self.binding, record.logical_call_id)
            self.assertEqual(token.attempt, index)
            record = self.finish_error(token, retry_decision="RETRY" if index < 2 else "DO_NOT_RETRY")
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)
        evidence = record.to_tool_evidence()
        self.assertEqual(evidence.execution_id, record.logical_call_id)
        self.assertTrue(evidence.retries_exhausted)
        self.assertEqual(evidence.retries_used, 2)
        self.assertEqual([item.attempt for item in evidence.attempts], [0, 1, 2])
        self.assertEqual(len({item.attempt_id for item in record.attempts}), 3)

    def test_last_attempt_cannot_authorize_fourth(self):
        record = self.create()
        for _ in range(2):
            token = self.store.claim(self.binding, record.logical_call_id)
            self.finish_error(token)
        token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token)
        self.assertEqual(self.store.get(self.binding, record.logical_call_id).attempts[-1].status, "STARTED")

    def test_completed_call_not_replayed(self):
        record, receipt = self.finish_success()
        self.assertEqual(record.to_tool_evidence().outcome, ToolExecutionOutcome.PASS)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)
        self.assertEqual(record.attempts[0].execution_id, receipt.execution_id)
        self.assertEqual(record.attempts[0].execution_manifest_id, receipt.execution_manifest_id)
        self.assertNotEqual(record.logical_call_id, receipt.execution_id)

    def test_success_receipt_requires_byte_identity_of_output(self):
        record = self.create()
        token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        output = receipt.tool_output()
        output["passed"] = 0
        self.assert_code("TOOL_EVIDENCE_INVALID", self.store.finish, self.binding, token, outcome="PASS", output=output)
        self.assertEqual(self.store.get(self.binding, record.logical_call_id).attempts[0].status, "STARTED")

    def test_no_receipt_cannot_fabricate_execution_pass(self):
        record = self.create()
        token = self.store.claim(self.binding, record.logical_call_id)
        output = dict(total=1, passed=1, failed=0, skipped=0, reportRef=f"artifact://{uuid4()}/unit-test-report.json", executionManifestId=str(uuid4()))
        self.assert_code("TOOL_EVIDENCE_NOT_FOUND", self.store.finish, self.binding, token, outcome="PASS", output=output)

    def test_unit_product_failure_is_tool_pass(self):
        stdout = self.fixture.report_json(outcome="FAIL", details="assertion failed")
        from mcp_tools.tools.unit_report import parse_unit_report
        self.fixture.report = parse_unit_report(stdout, 1)
        self.fixture.result = replace(self.fixture.result, stdout=stdout, exit_code=1)
        record, _ = self.finish_success()
        self.assertEqual(record.attempts[0].outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(record.attempts[0].product_failure_kind, "QA_ASSERTION_FAILURE")
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_build_nonzero_product_failure_is_tool_pass(self):
        self.use_fixture(build_fixtures.BuildOutputStoreTests)
        self.fixture.result = replace(self.fixture.result, exit_code=1)
        record, receipt = self.finish_success()
        self.assertEqual(record.to_tool_evidence().outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(record.attempts[0].product_failure_kind, "BUILD_CODE_FAILURE")
        self.assertEqual(record.attempts[0].receipt_refs, (receipt.stdout_ref, receipt.stderr_ref))

    def test_browser_assertion_failure_is_tool_pass(self):
        self.use_fixture(browser_fixtures.BrowserTestOutputStoreTests)
        from mcp_tools.tools.browser_report import parse_browser_report
        stdout = self.fixture.report_json(outcome="FAIL")
        self.fixture.report = parse_browser_report(stdout, 1)
        self.fixture.result = replace(self.fixture.result, stdout=stdout, exit_code=1)
        record, receipt = self.finish_success()
        self.assertEqual(record.attempts[0].product_failure_kind, "QA_ASSERTION_FAILURE")
        self.assertEqual(record.attempts[0].receipt_refs, (receipt.report_ref, *receipt.trace_refs))

    def test_security_suspected_finding_is_tool_pass(self):
        self.use_fixture(security_fixtures.SecurityScanOutputStoreTests)
        from mcp_tools.tools.security_report import parse_security_report
        stdout = self.fixture.report_json(findings=[self.fixture.finding()])
        self.fixture.report = parse_security_report(stdout, 1)
        self.fixture.result = replace(self.fixture.result, stdout=stdout, exit_code=1)
        record, _ = self.finish_success()
        self.assertEqual(record.attempts[0].outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(record.attempts[0].product_failure_kind, "SECURITY_FINDING")

    def test_product_failure_hint_must_equal_real_receipt(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        self.assert_code("TOOL_EVIDENCE_INVALID", self.store.finish, self.binding, token, outcome="PASS",
            output=receipt.tool_output(), product_failure_kind="QA_ASSERTION_FAILURE")

    def test_file_call_needs_no_manifest_and_stores_no_content(self):
        self.tool_name = "read_project_file"
        record = self.create(source_artifact_id=None)
        token = self.store.claim(self.binding, record.logical_call_id)
        content = "source content must not appear in ledger"
        output = {"path": "source/main.py", "content": content, "sha256": sha256(content.encode()).hexdigest(), "sizeBytes": len(content.encode())}
        record = self.store.finish(self.binding, token, outcome="PASS", output=output)
        self.assertIsNone(record.execution_manifest)
        self.assert_code("TOOL_EVIDENCE_INCOMPLETE", record.to_tool_evidence)
        with self.repository._connection() as connection:
            rows = connection.execute("SELECT payload_json FROM tool_execution_attempt_finishes").fetchall()
        self.assertNotIn(content, "".join(row[0] for row in rows))
        self.assertEqual(record.attempts[0].output_sha256, sha256(json.dumps(output, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest())

    def test_execution_source_required(self):
        self.assert_code("TOOL_EVIDENCE_INVALID", self.create, source_artifact_id=None)

    def test_file_tool_cannot_claim_fake_snapshot(self):
        self.tool_name = "read_project_file"
        self.assert_code("TOOL_EVIDENCE_INVALID", self.create)

    def test_role_cannot_create_security_tool_as_developer(self):
        self.tool_name = "run_security_scan"
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.create)

    def test_invalid_hashes_rejected(self):
        for value in (True, "a" * 63, "A" * 64, "secret", None):
            with self.subTest(value=value):
                self.assert_code("TOOL_EVIDENCE_INVALID", self.create, configuration_sha256=value)

    def test_wrong_and_nonv4_id_rejected(self):
        self.assert_code("TOOL_EVIDENCE_INVALID", self.store.get, self.binding, uuid1())
        self.assert_code("TOOL_EVIDENCE_NOT_FOUND", self.store.get, self.binding, uuid4())

    def test_cross_role_and_run_reads_denied(self):
        record = self.create()
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        stranger = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER, run_id=uuid4(), workspace_id=uuid4())
        for binding in (qa, stranger):
            self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.get, binding, record.logical_call_id)
            self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.read_evidence, binding, record.evidence_ref)

    def test_inactive_creation_denied(self):
        self.fixture.mutate_step(status=WorkflowStepStatus.CANCELED)
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.create)

    def test_stale_context_cannot_claim(self):
        record = self.create()
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="user cancelled")
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_abort_allows_failure_finish_but_not_retry_claim(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="user cancelled")
        self.fixture.mutate_step(status=WorkflowStepStatus.CANCELED)
        record = self.finish_error(token, error_kind="CANCELLED", retry_safe=False, retry_decision="DO_NOT_RETRY",
            result_unknown=True, delivery_state="UNKNOWN")
        self.assertEqual(record.attempts[0].status, "FINISHED")
        self.assertTrue(record.attempts[0].result_unknown)
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_finished_historical_success_is_readable(self):
        record, _ = self.finish_success()
        self.fixture.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.fixture.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        self.assertEqual(self.store.get(self.binding, record.logical_call_id), record)

    def test_abort_success_finish_denied_keeps_started(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="user cancelled")
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.finish, self.binding, token, outcome="PASS", output=receipt.tool_output())
        self.assertEqual(self.store.get(self.binding, record.logical_call_id).attempts[0].status, "STARTED")

    def test_permission_error_never_retryable(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, error_kind="PERMISSION_DENIED")
        record = self.finish_error(token, error_kind="PERMISSION_DENIED", retry_safe=False, retry_decision="DO_NOT_RETRY")
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_unknown_error_text_not_accepted(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_INVALID", self.finish_error, token, error_kind="password=do-not-retain")

    def test_boolean_duration_or_safety_not_coerced(self):
        for changes in ({"duration_ms": True}, {"duration_ms": 1.0}, {"retry_safe": 1}, {"result_unknown": 0}):
            record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
            self.assert_code("TOOL_EVIDENCE_INVALID", self.finish_error, token, **changes)

    def test_tool_fail_outcome_not_confused_with_product_failure(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_INVALID", self.finish_error, token, outcome="FAIL")

    def test_uncertain_timeout_requires_inspection_not_retry(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, error_kind="TOOL_TIMEOUT", retry_safe=False)
        record = self.finish_error(token, error_kind="TOOL_TIMEOUT", retry_safe=False, retry_decision="INSPECT_STATE",
            delivery_state="UNKNOWN", result_unknown=True)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_confirmed_safe_timeout_can_retry(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.finish_error(token, error_kind="TOOL_TIMEOUT", delivery_state="UNKNOWN")
        self.assertEqual(self.store.claim(self.binding, record.logical_call_id).attempt, 1)

    def test_startup_retry_requires_explicit_safe(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, retry_safe=False)

    def test_write_retry_only_proven_not_sent_startup(self):
        self.tool_name = "write_source_file"
        record = self.create(source_artifact_id=None); token = self.store.claim(self.binding, record.logical_call_id)
        for changes in ({"delivery_state": "REPLIED"}, {"delivery_state": "UNKNOWN"},
            {"error_kind": "TOOL_TIMEOUT"}, {"error_kind": "MCP_TRANSPORT_INTERRUPTED"}):
            self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, **changes)
        self.finish_error(token)
        self.assertEqual(self.store.claim(self.binding, record.logical_call_id).attempt, 1)

    def test_write_unknown_always_inspect(self):
        self.tool_name = "apply_patch"
        record = self.create(source_artifact_id=None); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, error_kind="WRITE_RESULT_UNKNOWN",
            retry_decision="DO_NOT_RETRY", retry_safe=False)
        record = self.finish_error(token, error_kind="WRITE_RESULT_UNKNOWN", retry_decision="INSPECT_STATE",
            retry_safe=False, delivery_state="UNKNOWN", result_unknown=True)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_duplicate_finish_and_wrong_token_denied(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_CONFLICT", self.finish_error, replace(token, attempt_id=uuid4()))
        self.finish_error(token, retry_decision="DO_NOT_RETRY")
        self.assert_code("TOOL_EVIDENCE_CONFLICT", self.finish_error, token)

    def test_source_actual_blob_hash_verified_on_each_read(self):
        record = self.create()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET content=zeroblob(size_bytes) WHERE artifact_id=?", (str(self.source.artifact_id),))
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_receipt_actual_blob_hash_verified_on_each_read(self):
        record, receipt = self.finish_success()
        self.fixture.corrupt(receipt, report=b"{}")
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_qa_source_grant_required_on_create_and_history(self):
        self.step = self.fixture.qa_context()
        self.binding = self.fixture.binding
        record = self.create()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(self.source.artifact_id),))
        self.assert_code("TOOL_EVIDENCE_CONTEXT_DENIED", self.store.get, self.binding, record.logical_call_id)

    def test_step_identity_mutation_detected(self):
        record = self.create()
        self.fixture.mutate_step(requirement_ids=[self.source.requirement_ids[0]])
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_hash_tamper_detected(self):
        record = self.create()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER tool_execution_calls_no_update")
            connection.execute("UPDATE tool_execution_calls SET payload_sha256=? WHERE logical_call_id=?", ("c" * 64, str(record.logical_call_id)))
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_rehashed_attempt_gap_still_detected(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.rewrite("tool_execution_attempt_starts", ("attempt_id", str(token.attempt_id)), {"attempt": 1})
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_rehashed_invalid_retry_still_detected(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.finish_error(token, error_kind="PERMISSION_DENIED", retry_safe=False, retry_decision="DO_NOT_RETRY")
        self.rewrite("tool_execution_attempt_finishes", ("attempt_id", str(token.attempt_id)), {"retryDecision": "RETRY", "retrySafe": True})
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_all_three_tables_block_update_delete_and_replace(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.finish_error(token, retry_decision="DO_NOT_RETRY")
        for table in ("tool_execution_calls", "tool_execution_attempt_starts", "tool_execution_attempt_finishes"):
            for operation in (f"UPDATE {table} SET payload_sha256=payload_sha256", f"DELETE FROM {table}",
                f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"):
                with self.subTest(operation=operation), self.assertRaises(sqlite3.IntegrityError):
                    with self.repository._transaction() as connection:
                        connection.execute(operation)

    def test_logical_uuid_collision_with_source_denied(self):
        with patch("mcp_tools.execution_store.uuid4", return_value=self.source.artifact_id):
            self.assert_code("TOOL_EVIDENCE_CONFLICT", self.create)

    def test_attempt_uuid_collision_with_private_receipt_denied(self):
        receipt = self.fixture.publish(); record = self.create()
        with patch("mcp_tools.execution_store.uuid4", return_value=receipt.execution_id):
            self.assert_code("TOOL_EVIDENCE_CONFLICT", self.store.claim, self.binding, record.logical_call_id)

    def test_physical_receipt_cannot_be_reused_by_second_logical_call(self):
        record, receipt = self.finish_success()
        second = self.create(); token = self.store.claim(self.binding, second.logical_call_id)
        self.assert_code("TOOL_EVIDENCE_CONFLICT", self.store.finish, self.binding, token,
            outcome="PASS", output=receipt.tool_output())
        self.assertEqual(self.store.get(self.binding, second.logical_call_id).attempts[0].status, "STARTED")
        self.assertEqual(self.store.get(self.binding, record.logical_call_id), record)

    def test_rehashed_timestamp_cannot_carry_peer_text(self):
        record = self.create()
        self.rewrite("tool_execution_calls", ("logical_call_id", str(record.logical_call_id)), {"createdAt": "password=never-store-peer-text"})
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_rehashed_file_receipt_cannot_acquire_partial_manifest(self):
        self.tool_name = "read_project_file"
        record = self.create(source_artifact_id=None)
        self.rewrite("tool_execution_calls", ("logical_call_id", str(record.logical_call_id)), {"sourceArtifactId": str(self.source.artifact_id)})
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_frozen_configuration_mutation_detected_on_history(self):
        record = self.create()
        # Privileged fixture intentionally changes a different valid frozen
        # configuration field; real writes are forbidden by its trigger.
        data = json.loads(self.fixture.configuration.model_dump_json())
        data["configuration"]["protected_test_suite_ref"] = "https://criteria.example.invalid/another/v2"
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?", (json.dumps(data), str(self.run.run_id)))
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_execution_receipt_from_another_step_cannot_be_claimed(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        # A mutated receipt remains invalid even after the metadata's digest
        # has been recomputed; Source lineage binds its actual producer step.
        metadata = self.fixture.metadata(receipt)
        metadata["workflowStepId"] = str(uuid4())
        self.fixture.replace_metadata(receipt, metadata)
        self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.finish, self.binding, token, outcome="PASS", output=receipt.tool_output())

    def test_cancellation_record_contains_no_implicit_retry(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        record = self.finish_error(token, error_kind="CANCELLED", retry_safe=False,
            retry_decision="DO_NOT_RETRY", delivery_state="UNKNOWN", result_unknown=True)
        self.assertEqual(record.attempts[0].retry_decision, RetryDecision.DO_NOT_RETRY)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_safe_but_exhausted_deadline_can_stop_without_retry(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        record = self.finish_error(token, retry_decision="DO_NOT_RETRY")
        self.assertTrue(record.attempts[0].retry_safe)
        self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.store.claim, self.binding, record.logical_call_id)

    def test_unused_receipt_published_before_claim_is_not_fresh(self):
        for fixture_type in (unit_fixtures.UnitTestOutputStoreTests, build_fixtures.BuildOutputStoreTests,
            browser_fixtures.BrowserTestOutputStoreTests, security_fixtures.SecurityScanOutputStoreTests):
            with self.subTest(tool=fixture_type.__name__):
                self.use_fixture(fixture_type)
                receipt = self.fixture.publish()
                record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
                self.assertGreaterEqual(self.store.get(self.binding, record.logical_call_id).attempts[0].receipt_rowid_floor, 1)
                self.assert_code("TOOL_EVIDENCE_CONFLICT", self.store.finish, self.binding, token,
                    outcome="PASS", output=receipt.tool_output())

    def test_two_early_claims_cannot_share_one_fresh_execution(self):
        first, second = self.create(), self.create()
        first_token = self.store.claim(self.binding, first.logical_call_id)
        second_token = self.store.claim(self.binding, second.logical_call_id)
        receipt = self.fixture.publish()
        self.store.finish(self.binding, first_token, outcome="PASS", output=receipt.tool_output())
        self.assert_code("TOOL_EVIDENCE_CONFLICT", self.store.finish, self.binding, second_token,
            outcome="PASS", output=receipt.tool_output())

    def test_freshness_floor_is_canonical_strict_integer(self):
        for floor in (True, 1.0, -1, 2**63, "0", None):
            with self.subTest(floor=floor):
                record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
                self.rewrite("tool_execution_attempt_starts", ("attempt_id", str(token.attempt_id)), {"receiptRowidFloor": floor})
                self.assert_code("TOOL_EVIDENCE_INTEGRITY_ERROR", self.store.get, self.binding, record.logical_call_id)

    def test_fresh_correct_source_receipt_still_needs_exact_selector(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        self.fixture.scope = replace(self.fixture.scope, name="different-approved-unit")
        self.fixture.profile = self.fixture.profile_for(self.fixture.scope)
        self.fixture.result = replace(self.fixture.result, profile_name=self.fixture.scope.name)
        receipt = self.fixture.publish()
        self.assert_code("TOOL_EVIDENCE_INVALID", self.store.finish, self.binding, token,
            outcome="PASS", output=receipt.tool_output())

    def test_missing_selector_can_record_failure_but_cannot_pass(self):
        failed = self.create(selector_sha256=None); failed_token = self.store.claim(self.binding, failed.logical_call_id)
        failed = self.finish_error(failed_token, error_kind="PROFILE_NOT_FOUND", retry_safe=False, retry_decision="DO_NOT_RETRY")
        self.assertIsNone(failed.selector_sha256)
        record = self.create(selector_sha256=None); token = self.store.claim(self.binding, record.logical_call_id)
        receipt = self.fixture.publish()
        self.assert_code("TOOL_EVIDENCE_INVALID", self.store.finish, self.binding, token, outcome="PASS", output=receipt.tool_output())

    def test_file_call_cannot_hold_selector_hash(self):
        self.tool_name = "read_project_file"
        self.assert_code("TOOL_EVIDENCE_INVALID", self.create, source_artifact_id=None, selector_sha256="c" * 64)

    def test_unknown_startup_delivery_cannot_claim_safe_retry(self):
        record = self.create(); token = self.store.claim(self.binding, record.logical_call_id)
        for kind in ("PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"):
            self.assert_code("TOOL_EVIDENCE_RETRY_DENIED", self.finish_error, token, error_kind=kind, delivery_state="UNKNOWN")

    def test_read_reference_cannot_fetch_or_select_host_path(self):
        for ref in ("file:///tmp/private.json", "https://example.invalid/data", "artifact://bad/tool-attempt.json",
            f"artifact://{uuid4()}/../tool-attempt.json", f"artifact://{uuid4()}/tool-result.json"):
            self.assert_code("TOOL_EVIDENCE_INVALID", self.store.read_evidence, self.binding, ref)

    def test_unapproved_error_code_is_not_echoed(self):
        error = ToolStoreError("password=hidden")
        self.assertEqual(str(error), "TOOL_EVIDENCE_STORAGE_ERROR")


if __name__ == "__main__":
    unittest.main()
