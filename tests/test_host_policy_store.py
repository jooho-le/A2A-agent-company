"""Host-policy admission evidence in temporary SQLite only; no live agents."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid1, uuid4

from agents.platform.composition import PlatformConfigurationError
from agents.platform.host_policy_store import HostPolicyStore
from orchestrator.domain import (
    A2ATaskState, AgentContext, AgentRole, TraceEvent, WorkflowRun,
    WorkflowStatus, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.infrastructure.sqlite_workflows import (
    SQLiteWorkflowRepository, _insert_event,
)


_POLICY = "a" * 64
_OTHER_POLICY = "b" * 64


class HostPolicyStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-host-policy-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.database_path = self.directory / "workflow.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)
        self.store = HostPolicyStore(self.repository)

    def bundle(self, *, claim=True, submitted=False):
        run = WorkflowRun(scenario_id=uuid4(), request_text="inert Host policy fixture")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        events = [TraceEvent(run_id=run.run_id, event_type="RUN_STARTED",
            actor="Orchestrator", attempt=0, workflow_state=run.status)]
        if submitted:
            events.append(TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                event_type="A2A_MESSAGE_SENT", actor="Orchestrator", attempt=0))
        self.repository.create_run(run, (step,), events)
        return self.repository.claim_planner_dispatch(run.run_id) if claim else (run, step)

    def tables(self):
        with self.repository._connection() as connection:
            return tuple(row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"))

    def policies(self):
        with self.repository._connection() as connection:
            return tuple(tuple(row) for row in connection.execute(
                "SELECT run_id,policy_sha256 FROM run_host_policies ORDER BY run_id"))

    def rejected(self, operation, *arguments):
        with self.assertRaises(PlatformConfigurationError) as caught:
            operation(*arguments)
        self.assertEqual(str(caught.exception), "OWNED_AGENT_CONFIGURATION_INVALID")
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn(str(self.directory), str(caught.exception))

    def update_run(self, run, **changes):
        changed = WorkflowRun.model_validate(run.model_copy(update=changes).model_dump())
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (changed.status.value, changed.model_dump_json(), str(run.run_id)))
        return changed

    def update_step(self, step, **changes):
        changed = WorkflowStep.model_validate(step.model_copy(update=changes).model_dump())
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (changed.status.value, changed.model_dump_json(), str(step.workflow_step_id)))
        return changed

    def test_constructor_is_inert_and_repr_is_private(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("inert")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("inert")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("inert")):
            self.assertEqual(repr(HostPolicyStore(self.repository)), "HostPolicyStore()")
        self.assertNotIn("run_host_policies", self.tables())

    def test_constructor_rejects_non_repository(self):
        for value in (None, object(), str(self.database_path)):
            self.rejected(HostPolicyStore, value)

    def test_registered_initial_claim_can_pin_without_changing_canonical_data(self):
        run, step = self.bundle()
        before_events = self.repository.list_events(run.run_id, limit=1000, offset=0)
        before_configuration = self.repository.get_run_configuration(run.run_id)
        self.assertIsNone(self.store.pin(run.run_id, _POLICY))
        self.assertIsNone(self.store.require(run.run_id, _POLICY))
        self.assertEqual(self.policies(), ((str(run.run_id), _POLICY),))
        self.assertEqual(self.repository.get_run(run.run_id), run)
        self.assertEqual(self.repository.list_steps(run.run_id), [step])
        self.assertEqual(self.repository.list_events(run.run_id, limit=1000, offset=0), before_events)
        self.assertEqual(self.repository.get_run_configuration(run.run_id), before_configuration)

    def test_idempotent_match_does_not_attempt_another_insert_or_schema_update(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        with patch.object(HostPolicyStore, "_schema", side_effect=AssertionError("read-only match")):
            self.store.pin(run.run_id, _POLICY)
        self.assertEqual(self.policies(), ((str(run.run_id), _POLICY),))

    def test_existing_policy_drift_is_rejected_and_old_policy_kept(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        self.rejected(self.store.pin, run.run_id, _OTHER_POLICY)
        self.rejected(self.store.require, run.run_id, _OTHER_POLICY)
        self.store.require(run.run_id, _POLICY)
        self.assertEqual(self.policies(), ((str(run.run_id), _POLICY),))

    def test_reopened_repository_and_store_keep_the_same_binding(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        reopened = HostPolicyStore(SQLiteWorkflowRepository(self.database_path))
        reopened.require(run.run_id, _POLICY)
        reopened.pin(run.run_id, _POLICY)
        self.rejected(reopened.pin, run.run_id, _OTHER_POLICY)

    def test_bound_policy_survives_later_fix_step_and_terminal_state(self):
        run, step = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        self.update_run(run, status=WorkflowStatus.ABORTED, termination_reason="inert stop", fix_attempt=1, code_version=2)
        self.update_step(step, status=WorkflowStepStatus.SUCCEEDED, attempt=2,
            a2a_task_id="opaque-old-task", a2a_task_state=A2ATaskState.COMPLETED)
        self.store.require(run.run_id, _POLICY)
        self.store.pin(run.run_id, _POLICY)

    def test_policy_persists_when_later_preparation_raises(self):
        run, _ = self.bundle()
        with self.assertRaisesRegex(RuntimeError, "inert preparation failure"):
            self.store.pin(run.run_id, _POLICY)
            raise RuntimeError("inert preparation failure")
        HostPolicyStore(SQLiteWorkflowRepository(self.database_path)).require(run.run_id, _POLICY)
        self.rejected(self.store.pin, run.run_id, _OTHER_POLICY)

    def test_same_hash_on_different_registered_runs_has_separate_bindings(self):
        first, _ = self.bundle()
        second, _ = self.bundle()
        self.store.pin(first.run_id, _POLICY)
        self.store.pin(second.run_id, _POLICY)
        self.assertEqual(set(self.policies()), {(str(first.run_id), _POLICY), (str(second.run_id), _POLICY)})

    def test_unknown_run_is_rejected_without_schema_creation(self):
        before = self.tables()
        unknown = uuid4()
        self.rejected(self.store.pin, unknown, _POLICY)
        self.rejected(self.store.require, unknown, _POLICY)
        self.assertEqual(self.tables(), before)

    def test_unknown_run_is_rejected_when_another_run_is_bound(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        self.rejected(self.store.require, uuid4(), _POLICY)
        self.rejected(self.store.pin, uuid4(), _POLICY)
        self.assertEqual(self.policies(), ((str(run.run_id), _POLICY),))

    def test_require_without_table_is_read_only_and_never_adopts(self):
        run, _ = self.bundle()
        before = self.tables()
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("read only")), \
                patch.object(HostPolicyStore, "_schema", side_effect=AssertionError("no schema")):
            self.rejected(self.store.require, run.run_id, _POLICY)
        self.assertEqual(self.tables(), before)

    def test_require_without_row_is_read_only_and_never_adopts(self):
        first, _ = self.bundle()
        second, _ = self.bundle()
        self.store.pin(first.run_id, _POLICY)
        before = self.policies()
        self.rejected(self.store.require, second.run_id, _POLICY)
        self.assertEqual(self.policies(), before)

    def test_require_does_not_recreate_a_missing_database(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        self.database_path.rename(self.directory / "saved-workflow.sqlite3")
        self.rejected(self.store.require, run.run_id, _POLICY)
        self.assertFalse(self.database_path.exists())

    def test_uuid_must_be_an_exact_uuid4_before_any_database_io(self):
        values = (str(uuid4()), uuid4().hex, uuid1(), UUID(int=0), None, True, 1, b"uuid4", object())
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("invalid ID I/O")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("invalid ID I/O")):
            for value in values:
                with self.subTest(value=type(value)):
                    self.rejected(self.store.pin, value, _POLICY)
                    self.rejected(self.store.require, value, _POLICY)

    def test_policy_requires_exact_lowercase_sha256_before_any_database_io(self):
        class StringSubclass(str):
            pass
        values = (None, True, 1, "", "a" * 63, "a" * 65, "A" * 64, "g" * 64,
                  "sha256:" + _POLICY, _POLICY + "\n", StringSubclass(_POLICY), _POLICY.encode())
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("invalid hash I/O")), \
                patch.object(sqlite3, "connect", side_effect=AssertionError("invalid hash I/O")):
            for value in values:
                with self.subTest(value=type(value)):
                    self.rejected(self.store.pin, uuid4(), value)
                    self.rejected(self.store.require, uuid4(), value)

    def test_received_unclaimed_run_cannot_pin(self):
        run, _ = self.bundle(claim=False)
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_non_initial_run_state_cannot_create_a_baseline(self):
        for changes in ({"status": WorkflowStatus.IMPLEMENTING}, {"fix_attempt": 1}, {"code_version": 1}):
            run, _ = self.bundle()
            self.update_run(run, **changes)
            with self.subTest(changes=changes):
                self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_any_non_initial_planner_field_prevents_adoption(self):
        changes = ({"status": WorkflowStepStatus.PENDING}, {"status": WorkflowStepStatus.SUCCEEDED},
            {"agent_role": AgentRole.DEVELOPER}, {"attempt": 1}, {"a2a_task_id": "opaque-task"},
            {"a2a_task_state": A2ATaskState.WORKING}, {"agent_context_id": "opaque-context"},
            {"code_version": 1}, {"requirement_ids": [uuid4()]}, {"a2a_artifact_ids": ["opaque-result"]},
            {"input_artifact_ids": [uuid4()]}, {"output_artifact_ids": [uuid4()]})
        for fields in changes:
            run, step = self.bundle()
            self.update_step(step, **fields)
            with self.subTest(fields=fields):
                self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_multiple_steps_are_not_a_fresh_initial_claim(self):
        run, _ = self.bundle()
        additional = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER)
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps VALUES(?,?,?,?,?,?)", (
                str(additional.workflow_step_id), str(run.run_id), additional.status.value,
                additional.created_at.isoformat(), additional.updated_at.isoformat(), additional.model_dump_json()))
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_old_send_without_task_id_must_not_be_adopted(self):
        run, step = self.bundle(submitted=True)
        self.assertIsNone(step.a2a_task_id)
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_old_send_beyond_trace_pagination_limit_is_rejected(self):
        run, _ = self.bundle()
        with self.repository._transaction() as connection:
            for _ in range(1001):
                _insert_event(connection, TraceEvent(run_id=run.run_id, event_type="INERT_HOST_CHECK",
                    actor="Orchestrator", attempt=0))
            _insert_event(connection, TraceEvent(run_id=run.run_id, event_type="A2A_MESSAGE_SENT",
                actor="Orchestrator", attempt=0))
        self.rejected(self.store.pin, run.run_id, _POLICY)

    def test_registered_agent_context_prevents_late_adoption(self):
        run, _ = self.bundle()
        context = AgentContext(run_id=run.run_id, agent_id="old-agent", agent_context_id="old-context")
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO agent_contexts VALUES(?,?,?)",
                (str(run.run_id), context.agent_id, context.model_dump_json()))
        self.rejected(self.store.pin, run.run_id, _POLICY)

    def test_generated_artifact_prevents_late_adoption(self):
        run, _ = self.bundle()
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)",
                (str(uuid4()), str(run.run_id), "BUILD_REPORT", 1, "{}"))
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_recorded_tool_attempt_prevents_late_adoption(self):
        run, step = self.bundle()
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO tool_attempts VALUES(?,?,?,?,?)",
                (str(run.run_id), str(step.workflow_step_id), str(uuid4()), 0, "{}"))
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertNotIn("run_host_policies", self.tables())

    def test_send_in_payload_is_not_hidden_by_inconsistent_event_index(self):
        run, _ = self.bundle()
        event = TraceEvent(run_id=run.run_id, event_type="A2A_MESSAGE_SENT", actor="Orchestrator", attempt=0)
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO trace_events(event_id,run_id,occurred_at,event_type,payload_json) "
                "VALUES(?,?,?,?,?)", (str(event.event_id), str(run.run_id), event.occurred_at.isoformat(),
                                      "INERT_HOST_CHECK", json.dumps(event.to_trace_json())))
        self.rejected(self.store.pin, run.run_id, _POLICY)

    def test_schema_creation_is_atomic_if_first_pin_fails(self):
        run, _ = self.bundle()
        before = self.tables()
        original = HostPolicyStore._schema
        def interrupted(connection):
            original(connection)
            raise RuntimeError("inert schema admission failure")
        with patch.object(HostPolicyStore, "_schema", interrupted):
            self.rejected(self.store.pin, run.run_id, _POLICY)
        self.assertEqual(self.tables(), before)
        self.store.pin(run.run_id, _POLICY)
        self.store.require(run.run_id, _POLICY)

    def test_policy_remains_valid_after_a2a_send_without_re_adopting(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        with self.repository._transaction() as connection:
            _insert_event(connection, TraceEvent(run_id=run.run_id, event_type="A2A_MESSAGE_SENT",
                actor="Orchestrator", attempt=0))
        self.store.require(run.run_id, _POLICY)
        self.store.pin(run.run_id, _POLICY)
        self.rejected(self.store.pin, run.run_id, _OTHER_POLICY)

    def test_indexed_run_identity_and_state_must_match_validated_payload(self):
        run, _ = self.bundle()
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status='IMPLEMENTING' WHERE run_id=?", (str(run.run_id),))
        self.rejected(self.store.pin, run.run_id, _POLICY)
        self.rejected(self.store.require, run.run_id, _POLICY)

    def test_step_identity_mismatch_cannot_pin(self):
        run, step = self.bundle()
        data = step.model_dump(mode="json")
        data["workflow_step_id"] = str(uuid4())
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET payload_json=? WHERE workflow_step_id=?",
                (json.dumps(data), str(step.workflow_step_id)))
        self.rejected(self.store.pin, run.run_id, _POLICY)

    def test_registered_run_payload_identity_must_match_even_for_an_old_pin(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        data = run.model_dump(mode="json")
        data["run_id"] = str(uuid4())
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?",
                (json.dumps(data), str(run.run_id)))
        self.rejected(self.store.require, run.run_id, _POLICY)
        self.rejected(self.store.pin, run.run_id, _POLICY)

    def test_update_delete_and_replace_are_blocked_by_database(self):
        run, _ = self.bundle()
        self.store.pin(run.run_id, _POLICY)
        commands = (("UPDATE run_host_policies SET policy_sha256=? WHERE run_id=?", (_OTHER_POLICY, str(run.run_id))),
                    ("DELETE FROM run_host_policies WHERE run_id=?", (str(run.run_id),)),
                    ("INSERT OR REPLACE INTO run_host_policies VALUES(?,?)", (str(run.run_id), _OTHER_POLICY)))
        for sql, values in commands:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                with self.repository._transaction() as connection:
                    connection.execute(sql, values)
        self.store.require(run.run_id, _POLICY)

    def test_concurrent_same_policy_is_idempotent(self):
        run, _ = self.bundle()
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(self.store.pin, run.run_id, _POLICY) for _ in range(2)]
            for future in futures:
                self.assertIsNone(future.result(timeout=10))
        self.assertEqual(self.policies(), ((str(run.run_id), _POLICY),))

    def test_concurrent_different_policies_cannot_override_first_binding(self):
        run, _ = self.bundle()
        def pin(policy):
            try:
                self.store.pin(run.run_id, policy)
            except PlatformConfigurationError:
                return False
            return True
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted = list(executor.map(pin, (_POLICY, _OTHER_POLICY)))
        self.assertEqual(sorted(accepted), [False, True])
        self.assertIn(self.policies(), (((str(run.run_id), _POLICY),), ((str(run.run_id), _OTHER_POLICY),)))


if __name__ == "__main__":
    unittest.main()
