"""Real immutable SQLite content and grants; no cloud/Agent product execution."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from uuid import uuid1, uuid4

from orchestrator.artifacts.contracts import MAX_CONTENT_BYTES, ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore, canonical_report_content
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.developer_artifacts import ChangeReportArtifact
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _insert_artifact


class ArtifactBlobStoreTests(unittest.TestCase):
    archive = b"test-only immutable source archive bytes"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-blob-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "registry.sqlite3")
        self.requirement_ids = [uuid4()]
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
        self.step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, requirement_ids=self.requirement_ids,
        )
        self.environment = ExecutionBaseline(
            container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash="sha256:" + "e" * 64, hardware_profile="test-only-local",
        )
        self.configuration = RunConfigurationArtifact(
            run_id=self.run.run_id, scenario_id=self.run.scenario_id,
            workspace_id=self.run.workspace_id,
            configuration=RunConfiguration(environment=self.environment),
        )
        self.repository.create_run(self.run, [self.step], (), run_configuration=self.configuration)
        self.store = SQLiteArtifactContentStore(self.repository)
        self.source = self.make_source()

    def make_source(self, **changes):
        artifact_id = uuid4()
        values = dict(
            artifact_id=artifact_id, artifact_version=1, run_id=self.run.run_id,
            workflow_step_id=self.step.workflow_step_id,
            requirement_ids=tuple(self.requirement_ids), code_version=1,
            repository_id="test-only-repository", commit_hash="a" * 40,
            git_object_format="sha1", tree_hash="b" * 40,
            snapshot_sha256=sha256(self.archive).hexdigest(),
            artifact_uri=f"artifact://{artifact_id}/source.tar",
            container_image_digest=self.environment.container_image_digest,
            dependency_lock_hash=self.environment.dependency_lock_hash,
        )
        values.update(changes)
        return CodeSnapshotArtifact(**values)

    def put(self, source=None, **changes):
        values = dict(content=self.archive, media_type="application/x-tar", grants=(AgentRole.QA, AgentRole.SECURITY))
        values.update(changes)
        return self.store.put(source or self.source, **values)

    def assert_error(self, code, operation):
        with self.assertRaises(ArtifactAccessError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn("test-only-private", str(raised.exception))

    def replace_run(self, **changes):
        data = self.repository.get_run(self.run.run_id).model_dump(mode="python")
        data.update(changes)
        updated = WorkflowRun.model_validate(data)
        with self.repository._transaction() as connection:
            connection.execute(
                "UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (updated.status.value, updated.model_dump_json(), str(updated.run_id)),
            )
        return updated

    def replace_step(self, **changes):
        data = self.step.model_dump(mode="python")
        data.update(changes)
        updated = WorkflowStep.model_validate(data)
        with self.repository._transaction() as connection:
            connection.execute(
                "UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (updated.status.value, updated.model_dump_json(), str(updated.workflow_step_id)),
            )
        return updated

    def report(self, **changes):
        values = dict(
            artifact_id=uuid4(), artifact_version=1, run_id=self.run.run_id,
            workflow_step_id=self.step.workflow_step_id, a2a_task_id="test-only-task",
            a2a_artifact_id="test-only-change", requirement_ids=tuple(self.requirement_ids),
            code_version=1, summary="회원가입 구현",
            changes=({"path": "src/signup.py", "action": "ADDED"},),
        )
        values.update(changes)
        return ChangeReportArtifact(**values)

    def register_report(self, report):
        with self.repository._transaction() as connection:
            _insert_artifact(connection, report, self.run.status)

    def test_construction_is_lazy_and_does_not_install_schema(self):
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_contents'").fetchone())
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='snapshot_read_grants'").fetchone())

    def test_real_bytes_and_metadata_survive_repository_restart(self):
        stored = self.put()
        reopened = SQLiteArtifactContentStore(SQLiteWorkflowRepository(self.repository.database_path))
        fetched = reopened.get(self.run.run_id, self.source.artifact_id)
        self.assertEqual(fetched, stored)
        self.assertEqual(fetched.content, self.archive)
        self.assertEqual(fetched.content_sha256, sha256(self.archive).hexdigest())
        self.assertEqual(fetched.size_bytes, len(self.archive))
        self.assertEqual(fetched.artifact_id, self.source.artifact_id)
        self.assertEqual(fetched.run_id, self.run.run_id)

    def test_content_and_both_read_only_grants_publish_together(self):
        self.put()
        self.assertTrue(self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.QA))
        self.assertTrue(self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.SECURITY))
        self.assertFalse(self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.DEVELOPER))
        self.assertFalse(self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.PLANNER))
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM snapshot_read_grants").fetchone()[0], 2)

    def test_source_requires_exact_qa_security_grants(self):
        for grants in ((), (AgentRole.QA,), (AgentRole.QA, AgentRole.QA), (AgentRole.QA, AgentRole.PLANNER)):
            self.assert_error(ArtifactErrorCode.DENIED, lambda grants=grants: self.put(grants=grants))
        for grants in ([AgentRole.QA, AgentRole.SECURITY], ("QA", "SECURITY")):
            self.assert_error(ArtifactErrorCode.INVALID, lambda grants=grants: self.put(grants=grants))

    def test_wrong_hash_and_arbitrary_media_type_do_not_publish_content(self):
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.put(content=self.archive + b"changed"))
        self.assert_error(ArtifactErrorCode.INVALID, lambda: self.put(media_type="text/plain"))
        self.assert_error(ArtifactErrorCode.INVALID, lambda: self.put(content=bytearray(self.archive)))
        self.assertIsNone(self.store.latest_source(self.run.run_id))

    def test_size_limit_rejects_before_publication(self):
        self.assert_error(ArtifactErrorCode.TOO_LARGE, lambda: self.put(content=b"x" * (MAX_CONTENT_BYTES + 1)))
        self.assertIsNone(self.store.latest_source(self.run.run_id))

    def test_snapshot_registration_does_not_mutate_workflow_or_trace(self):
        before_run = self.repository.get_run(self.run.run_id)
        before_steps = self.repository.list_steps(self.run.run_id)
        before_events = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        self.put()
        self.assertEqual(self.repository.get_run(self.run.run_id), before_run)
        self.assertEqual(self.repository.list_steps(self.run.run_id), before_steps)
        self.assertEqual(self.repository.list_events(self.run.run_id, limit=100, offset=0), before_events)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])

    def test_exact_publication_is_idempotent_but_other_payload_same_id_conflicts(self):
        first = self.put()
        self.assertEqual(self.put(grants=(AgentRole.SECURITY, AgentRole.QA)), first)
        changed = self.make_source(artifact_id=self.source.artifact_id, tree_hash="f" * 40, artifact_uri=self.source.artifact_uri)
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.put(changed))
        self.assertEqual(self.store.get(self.run.run_id, self.source.artifact_id), first)

    def test_two_simultaneous_identical_publications_are_idempotent(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            contents = list(pool.map(lambda _: self.put(), range(8)))
        self.assertTrue(all(item == contents[0] for item in contents))
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_contents").fetchone()[0], 1)

    def test_version_one_cannot_be_replaced_by_another_artifact_id(self):
        self.put()
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.put(self.make_source()))

    def test_successive_source_requires_same_run_predecessor_and_next_versions(self):
        self.put()
        self.replace_run(status=WorkflowStatus.FIXING, fix_attempt=1)
        self.replace_step(attempt=1, code_version=2)
        second = self.make_source(artifact_version=2, previous_artifact_id=self.source.artifact_id, code_version=2)
        self.put(second)
        self.assertEqual(self.store.latest_source(self.run.run_id), second)
        self.assertEqual(self.store.get(self.run.run_id, self.source.artifact_id).metadata, self.source)

    def test_nonexistent_or_wrong_predecessor_and_version_skips_are_rejected(self):
        self.put()
        self.replace_run(status=WorkflowStatus.FIXING, fix_attempt=1)
        self.replace_step(attempt=1, code_version=2)
        for changes in (
            {"previous_artifact_id": uuid4(), "artifact_version": 2},
            {"previous_artifact_id": self.source.artifact_id, "artifact_version": 3},
            {"previous_artifact_id": self.source.artifact_id, "artifact_version": 2, "repository_id": "another-repository"},
        ):
            candidate = self.make_source(code_version=2, **changes)
            self.assert_error(ArtifactErrorCode.CONFLICT, lambda candidate=candidate: self.put(candidate))

    def test_first_source_cannot_start_at_later_version(self):
        self.replace_run(status=WorkflowStatus.FIXING, fix_attempt=1)
        self.replace_step(attempt=1, code_version=2)
        candidate = self.make_source(artifact_version=2, previous_artifact_id=uuid4(), code_version=2)
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.put(candidate))

    def test_run_and_step_identity_must_belong_to_same_owner(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 실행")
        self.repository.create_run(other, (), ())
        candidate = self.make_source(run_id=other.run_id)
        self.assert_error(ArtifactErrorCode.DENIED, lambda: self.put(candidate))
        self.assert_error(ArtifactErrorCode.NOT_FOUND, lambda: self.put(self.make_source(workflow_step_id=uuid4())))
        self.replace_step(agent_role=AgentRole.QA)
        self.assert_error(ArtifactErrorCode.DENIED, self.put)

    def test_reads_and_grants_cannot_cross_runs(self):
        self.put()
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 실행")
        self.repository.create_run(other, (), ())
        self.assert_error(ArtifactErrorCode.NOT_FOUND, lambda: self.store.get(other.run_id, self.source.artifact_id))
        self.assert_error(ArtifactErrorCode.NOT_FOUND, lambda: self.store.has_grant(other.run_id, self.source.artifact_id, AgentRole.QA))

    def test_invalid_project_identifiers_and_untrusted_role_are_rejected(self):
        self.put()
        for invalid in (uuid1(), "../../test-only-private", True, None):
            self.assert_error(ArtifactErrorCode.INVALID, lambda invalid=invalid: self.store.get(invalid, self.source.artifact_id))
        self.assert_error(ArtifactErrorCode.INVALID, lambda: self.store.has_grant(self.run.run_id, self.source.artifact_id, "QA"))

    def test_cancelled_and_non_active_execution_cannot_publish_but_existing_read_survives(self):
        self.put()
        self.replace_run(status=WorkflowStatus.ABORTED, termination_reason="사용자 취소")
        self.assert_error(ArtifactErrorCode.DENIED, self.put)
        self.assertEqual(self.store.get(self.run.run_id, self.source.artifact_id).content, self.archive)

    def test_step_state_codeversion_and_requirements_are_rechecked(self):
        for changes in (
            {"status": WorkflowStepStatus.CANCELED}, {"status": WorkflowStepStatus.PENDING},
            {"status": WorkflowStepStatus.FAILED}, {"status": WorkflowStepStatus.WAITING_INPUT},
            {"code_version": 2},
            {"requirement_ids": [uuid4()]},
        ):
            with self.subTest(changes=changes):
                self.replace_step(**changes)
                self.assert_error(ArtifactErrorCode.DENIED, self.put)
                self.replace_step()

    def test_developer_input_continuation_attempt_does_not_increment_source_code_version(self):
        # A2A input/auth continuation is not a product Code Fix cycle. Source
        # metadata has no A2A attempt; exact in-flight attempts are bound by
        # the ToolExecutionStore journal, not inferred from Source version.
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                step = self.replace_step(attempt=attempt, code_version=1)
                stored = self.put()
                self.assertEqual(step.attempt, attempt)
                self.assertEqual(stored.metadata.code_version, 1)
                self.assertEqual(stored.metadata.workflow_step_id, step.workflow_step_id)
                self.assertEqual(stored.metadata.requirement_ids, tuple(self.requirement_ids))
                self.assertEqual(self.repository.get_run(self.run.run_id).fix_attempt, 0)
                self.assertTrue(self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.QA))

    def test_continuation_attempt_cannot_authorize_old_source_in_new_fix_cycle(self):
        self.replace_run(status=WorkflowStatus.FIXING, fix_attempt=1)
        self.replace_step(attempt=2, code_version=2)
        self.assert_error(ArtifactErrorCode.DENIED, self.put)

    def test_continuation_does_not_remove_current_task_binding(self):
        self.replace_step(attempt=1, code_version=1, a2a_task_id="current-developer-task")
        stale = self.make_source(a2a_task_id="other-developer-task", a2a_artifact_id="other-developer-artifact")
        self.assert_error(ArtifactErrorCode.DENIED, lambda: self.put(stale))

    def test_source_image_and_dependency_baseline_must_match(self):
        for changes in (
            {"container_image_digest": "sha256:" + "f" * 64},
            {"dependency_lock_hash": "sha256:" + "f" * 64},
        ):
            candidate = self.make_source(**changes)
            self.assert_error(ArtifactErrorCode.DENIED, lambda candidate=candidate: self.put(candidate))

    def test_missing_execution_baseline_never_gets_fake_values(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="미설정", status=WorkflowStatus.IMPLEMENTING)
        step = WorkflowStep(run_id=other.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING, requirement_ids=self.requirement_ids)
        self.repository.create_run(other, [step], ())
        candidate = self.make_source(run_id=other.run_id, workflow_step_id=step.workflow_step_id)
        self.assert_error(ArtifactErrorCode.CONFIGURATION, lambda: self.put(candidate))

    def test_report_is_registered_semantic_metadata_and_canonical_redacted_bytes_only(self):
        report = self.report(summary="password=test-only-private-value")
        self.register_report(report)
        raw = canonical_report_content(report)
        self.assertNotIn(b"test-only-private-value", raw)
        stored = self.store.put(report, raw, "application/json")
        self.assertEqual(stored.content, raw)
        self.assertIn("[REDACTED]", stored.metadata.summary)
        self.assertEqual(self.store.get(self.run.run_id, report.artifact_id), stored)

    def test_report_arbitrary_content_unregistered_metadata_or_role_grants_are_rejected(self):
        report = self.report()
        raw = canonical_report_content(report)
        self.assert_error(ArtifactErrorCode.NOT_FOUND, lambda: self.store.put(report, raw, "application/json"))
        self.register_report(report)
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.put(report, b"arbitrary test-only-private", "application/json"))
        self.assert_error(ArtifactErrorCode.DENIED, lambda: self.store.put(report, raw, "application/json", grants=(AgentRole.QA,)))
        changed = self.report(artifact_id=report.artifact_id, summary="tampered metadata")
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.store.put(changed, canonical_report_content(changed), "application/json"))

    def test_source_identity_cannot_be_silently_changed_by_redaction(self):
        candidate = self.make_source(repository_id="password=test-only-private-source-id")
        self.assert_error(ArtifactErrorCode.INVALID, lambda: self.put(candidate))

    def test_global_artifact_id_cannot_collision_with_run_configuration_or_report(self):
        candidate = self.make_source(artifact_id=self.configuration.artifact_id)
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.put(candidate))
        report = self.report()
        self.register_report(report)
        candidate = self.make_source(artifact_id=report.artifact_id)
        self.assert_error(ArtifactErrorCode.CONFLICT, lambda: self.put(candidate))

    def test_blob_and_grant_sql_update_delete_and_replace_are_blocked(self):
        self.put()
        statements = (
            ("UPDATE artifact_contents SET content=content WHERE artifact_id=?", (str(self.source.artifact_id),)),
            ("DELETE FROM artifact_contents WHERE artifact_id=?", (str(self.source.artifact_id),)),
            ("INSERT OR REPLACE INTO artifact_contents SELECT * FROM artifact_contents WHERE artifact_id=?", (str(self.source.artifact_id),)),
            ("UPDATE snapshot_read_grants SET access='READ_ONLY' WHERE artifact_id=?", (str(self.source.artifact_id),)),
            ("DELETE FROM snapshot_read_grants WHERE artifact_id=?", (str(self.source.artifact_id),)),
            ("INSERT OR REPLACE INTO snapshot_read_grants SELECT * FROM snapshot_read_grants WHERE artifact_id=?", (str(self.source.artifact_id),)),
        )
        for statement, args in statements:
            with self.subTest(statement=statement), self.assertRaises(sqlite3.IntegrityError):
                with self.repository._transaction() as connection:
                    connection.execute(statement, args)
        self.assertEqual(self.store.get(self.run.run_id, self.source.artifact_id).content, self.archive)

    def test_failed_grant_insert_rolls_back_source_bytes_and_both_grants(self):
        # Install the lazy schema first, then simulate a mid-publication DB failure.
        self.assertIsNone(self.store.latest_source(self.run.run_id))
        with self.repository._transaction() as connection:
            connection.execute("CREATE TRIGGER test_only_reject_security BEFORE INSERT ON snapshot_read_grants WHEN NEW.role='SECURITY' BEGIN SELECT RAISE(ABORT,'test-only-private'); END")
        self.assert_error(ArtifactErrorCode.CONFLICT, self.put)
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM artifact_contents").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM snapshot_read_grants").fetchone()[0], 0)

    def test_stored_content_or_metadata_corruption_is_detected_before_read(self):
        self.put()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET metadata_json=replace(metadata_json,'test-only-repository','test-only-private') WHERE artifact_id=?", (str(self.source.artifact_id),))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.get(self.run.run_id, self.source.artifact_id))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.has_grant(self.run.run_id, self.source.artifact_id, AgentRole.QA))

    def test_sql_replace_new_id_same_source_version_cannot_delete_old_content(self):
        self.put()
        with self.assertRaises(sqlite3.IntegrityError):
            with self.repository._transaction() as connection:
                connection.execute(
                    "INSERT OR REPLACE INTO artifact_contents SELECT ?,run_id,workflow_step_id,artifact_type,code_version,metadata_json,metadata_sha256,content_sha256,size_bytes,media_type,content FROM artifact_contents WHERE artifact_id=?",
                    (str(uuid4()), str(self.source.artifact_id)),
                )
        self.assertEqual(self.store.get(self.run.run_id, self.source.artifact_id).content, self.archive)

    def test_baseline_or_step_identity_corruption_is_detected_on_existing_read(self):
        self.put()
        self.replace_step(agent_role=AgentRole.QA)
        self.assert_error(ArtifactErrorCode.DENIED, lambda: self.store.get(self.run.run_id, self.source.artifact_id))
        self.replace_step()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            changed = self.configuration.model_dump(mode="json")
            changed["configuration"]["environment"]["dependency_lock_hash"] = "sha256:" + "f" * 64
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?", (json.dumps(changed), str(self.run.run_id)))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.get(self.run.run_id, self.source.artifact_id))

    def test_equal_length_content_corruption_is_detected(self):
        self.put()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET content=? WHERE artifact_id=?", (b"x" * len(self.archive), str(self.source.artifact_id)))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.get(self.run.run_id, self.source.artifact_id))

    def test_missing_or_mutated_snapshot_grant_is_integrity_failure(self):
        self.put()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(self.source.artifact_id),))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.store.get(self.run.run_id, self.source.artifact_id))

    def test_metadata_only_existing_project_record_does_not_supply_blob(self):
        with self.repository._transaction() as connection:
            _insert_artifact(connection, self.source, self.run.status)
        self.assert_error(ArtifactErrorCode.NOT_FOUND, lambda: self.store.get(self.run.run_id, self.source.artifact_id))
        self.assertIsNone(self.store.latest_source(self.run.run_id))

    def test_return_value_is_frozen_and_repr_hides_source_bytes(self):
        stored = self.put()
        with self.assertRaises(FrozenInstanceError):
            stored.content = b"mutated"
        self.assertNotIn(self.archive.decode(), repr(stored))
        self.assertNotIn("commitHash", repr(stored))

    def test_database_errors_do_not_leak_original_exception(self):
        class BrokenRepository:
            def _transaction(self):
                raise RuntimeError("test-only-private-host-root")
        broken = SQLiteArtifactContentStore(BrokenRepository())
        self.assert_error(ArtifactErrorCode.IO, lambda: broken.get(self.run.run_id, self.source.artifact_id))
