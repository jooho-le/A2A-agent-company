"""Step 21 service contracts using real isolated Git/SQLite/Workspace fixtures."""

import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.domain import (
    AgentContext,
    AgentRole,
    BuildReportArtifact,
    CodeSnapshotArtifact,
    SCN_001_ID,
    SCENARIO_REGISTRY,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
    QAReportArtifact,
    SecurityReportArtifact,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline,
    RunConfiguration,
    RunConfigurationArtifact,
)
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.snapshot_handoff import SnapshotHandoff
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _insert_artifact
from orchestrator.workspaces.registry import WorkspaceRegistry


class ArtifactServiceTests(unittest.TestCase):
    """Only temporary fixtures are changed; no real Agent or product execution."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-artifacts-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "registry.sqlite3")
        self.registry = WorkspaceRegistry(repository=self.repository, base_root=self.base)
        self.requirement_ids = SCENARIO_REGISTRY[SCN_001_ID].requirement_ids
        self.lock_bytes = b"example-local-dependency==1.0\n"
        self.lock_hash = "sha256:" + hashlib.sha256(self.lock_bytes).hexdigest()
        self.run, self.step, self.root = self.make_run()
        self.source = self.root / "source"
        self.source_file = self.source / "src" / "signup.py"
        self.source_file.parent.mkdir()
        self.source_file.write_text("def signup():\n    return 'initial'\n", encoding="utf-8")
        (self.source / "requirements.lock").write_bytes(self.lock_bytes)
        self.git("init", "--object-format=sha1")
        self.git("add", "src/signup.py", "requirements.lock")
        self.git("commit", "-m", "fixture initial source")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.tree = self.git("rev-parse", "HEAD^{tree}").strip()
        self.store = ArtifactStore(self.repository, self.registry)
        self.developer = self.store.bind(self.run.run_id, role=AgentRole.DEVELOPER)

    def git(self, *arguments):
        result = subprocess.run(
            [
                "git", "-c", "user.name=Artifact Test", "-c", "user.email=fixture@example.invalid",
                "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                *arguments,
            ],
            cwd=self.source,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
            timeout=20,
        )
        return result.stdout

    def make_run(
        self, *, environment=True, lock_hash=None, run_status=WorkflowStatus.IMPLEMENTING,
        step_status=WorkflowStepStatus.RUNNING, step_role=AgentRole.DEVELOPER,
        step_code_version=1,
    ):
        run = WorkflowRun(
            scenario_id=SCN_001_ID, request_text="회원가입 구현", status=run_status,
        )
        step = WorkflowStep(
            run_id=run.run_id, agent_role=step_role, status=step_status,
            requirement_ids=list(self.requirement_ids), code_version=step_code_version,
        )
        baseline = ExecutionBaseline(
            container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash=lock_hash or self.lock_hash,
            hardware_profile="isolated-local-fixture",
        ) if environment else None
        configuration = RunConfigurationArtifact(
            run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
            configuration=RunConfiguration(environment=baseline),
        )
        root = self.base / str(run.workspace_id)
        workspace = WorkspaceRecord(
            workspace_id=run.workspace_id, run_id=run.run_id, root_path=str(root),
        )
        self.repository.create_run(run, (step,), (), run_configuration=configuration, workspace=workspace)
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        return run, step, root

    def freeze(self, **overrides):
        values = {
            "workflow_step_id": self.step.workflow_step_id,
            "commit_hash": self.commit,
            "repository_id": "company-signup-demo",
            "lock_path": "requirements.lock",
        }
        values.update(overrides)
        return self.developer.freeze_source(**values)

    def assert_denied(self, operation):
        with self.assertRaises(ArtifactAccessError) as raised:
            operation()
        self.assertNotIn(str(self.directory), str(raised.exception))
        return raised.exception

    def events(self):
        return self.repository.list_events(self.run.run_id, limit=1000, offset=0)

    def save_fixture_step(self, step):
        self.repository.save_task_update(
            self.repository.get_run(self.run.run_id), step,
            AgentContext(run_id=self.run.run_id, agent_id="artifact-fixture"),
            TraceEvent(run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
                       event_type="FIXTURE", actor="test", attempt=step.attempt),
        )

    def register_fixture_report(self, report):
        # Trusted completed-record fixture; publication must not accept arbitrary
        # model JSON as a new Project Artifact Registry record.
        with self.repository._transaction() as connection:
            _insert_artifact(connection, report, self.repository.get_run(self.run.run_id).status)

    def test_source_metadata_contains_actual_git_and_lock_identities(self):
        snapshot = self.freeze()
        self.assertIsInstance(snapshot, CodeSnapshotArtifact)
        self.assertEqual(snapshot.run_id, self.run.run_id)
        self.assertEqual(snapshot.workflow_step_id, self.step.workflow_step_id)
        self.assertEqual(snapshot.requirement_ids, self.requirement_ids)
        self.assertEqual(snapshot.commit_hash, self.commit)
        self.assertEqual(snapshot.tree_hash, self.tree)
        self.assertEqual(snapshot.git_object_format.value, "sha1")
        self.assertEqual(snapshot.dependency_lock_hash, self.lock_hash)
        self.assertEqual(snapshot.container_image_digest, "sha256:" + "d" * 64)
        self.assertEqual(snapshot.artifact_version, 1)
        self.assertEqual(snapshot.code_version, 1)
        self.assertIsNone(snapshot.previous_artifact_id)
        self.assertEqual(snapshot.artifact_id.version, 4)
        self.assertEqual(snapshot.artifact_uri, f"artifact://{snapshot.artifact_id}/source.tar")
        stored = self.developer.read(snapshot.artifact_id)
        self.assertEqual(stored.content_sha256, hashlib.sha256(stored.content).hexdigest())
        self.assertEqual(stored.content_sha256, snapshot.snapshot_sha256)
        self.assertEqual(stored.size_bytes, len(stored.content))

    def test_snapshot_archive_contains_committed_source_and_lock_not_git_metadata(self):
        snapshot = self.freeze()
        content = self.developer.read(snapshot.artifact_id).content
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            self.assertEqual(set(archive.getnames()), {"requirements.lock", "src/signup.py"})
            self.assertEqual(archive.extractfile("requirements.lock").read(), self.lock_bytes)
            self.assertEqual(archive.extractfile("src/signup.py").read(), self.source_file.read_bytes())
            self.assertTrue(all(item.isfile() for item in archive.getmembers()))

    def test_same_immutable_bytes_are_readable_by_developer_qa_and_security(self):
        snapshot = self.freeze()
        expected = self.developer.read(snapshot.artifact_id)
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                bound = self.store.bind(self.run.run_id, role=role)
                by_id = bound.read(snapshot.artifact_id)
                by_uri = bound.read_uri(snapshot.artifact_uri)
                self.assertEqual(by_id.content, expected.content)
                self.assertEqual(by_uri.content, expected.content)
                self.assertEqual(by_id.metadata, snapshot)

    def test_source_read_is_not_implicitly_granted_to_planner(self):
        snapshot = self.freeze()
        planner = self.store.bind(self.run.run_id, role=AgentRole.PLANNER)
        self.assert_denied(lambda: planner.read(snapshot.artifact_id))
        self.assert_denied(lambda: planner.read_uri(snapshot.artifact_uri))

    def test_only_developer_can_freeze_and_role_is_bound_by_host(self):
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                bound = self.store.bind(self.run.run_id, role=role)
                self.assert_denied(lambda: bound.freeze_source(
                    workflow_step_id=self.step.workflow_step_id, commit_hash=self.commit,
                    repository_id="company-signup-demo", lock_path="requirements.lock",
                ))
        for role in ("DEVELOPER", "developer", None, True):
            with self.subTest(role=role):
                self.assert_denied(lambda: self.store.bind(self.run.run_id, role=role))

    def test_snapshot_has_no_write_api_and_source_modifications_cannot_change_frozen_bytes(self):
        snapshot = self.freeze()
        expected = self.developer.read(snapshot.artifact_id).content
        self.source_file.write_text("# changed after freezing\n", encoding="utf-8")
        (self.source / "untracked.txt").write_text("not part of committed source", encoding="utf-8")
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            bound = self.store.bind(self.run.run_id, role=role)
            self.assertEqual(bound.read(snapshot.artifact_id).content, expected)
            self.assertFalse(hasattr(bound, "write"))
        self.assertEqual(self.freeze(), snapshot)

    def test_duplicate_freeze_is_idempotent_without_product_state_or_success_transition(self):
        before = self.repository.get_run(self.run.run_id)
        first = self.freeze()
        after_first = self.events()[1]
        second = self.freeze()
        after_second = self.events()[1]
        self.assertEqual(second, first)
        self.assertEqual(after_second, after_first)
        current = self.repository.get_run(self.run.run_id)
        self.assertEqual(current.status, before.status)
        self.assertEqual(current.fix_attempt, 0)
        self.assertIsNone(current.verdict)
        self.assertEqual(self.store.get_snapshot(self.run.run_id, first.artifact_id), first)

    def test_source_stage_is_available_after_service_and_database_reopen(self):
        snapshot = self.freeze()
        content = self.developer.read(snapshot.artifact_id).content
        repository = SQLiteWorkflowRepository(self.repository.database_path)
        registry = WorkspaceRegistry(repository=repository, base_root=self.base)
        store = ArtifactStore(repository, registry)
        self.assertEqual(store.get_snapshot(self.run.run_id, snapshot.artifact_id), snapshot)
        self.assertEqual(store.bind(self.run.run_id, role=AgentRole.QA).read(snapshot.artifact_id).content, content)

    def test_cross_run_source_reads_host_lookup_and_handoffs_are_denied(self):
        snapshot = self.freeze()
        other, _, _ = self.make_run()
        bound = self.store.bind(other.run_id, role=AgentRole.QA)
        self.assert_denied(lambda: bound.read(snapshot.artifact_id))
        self.assert_denied(lambda: bound.read_uri(snapshot.artifact_uri))
        self.assert_denied(lambda: bound.verify_handoff(SnapshotHandoff.from_snapshot(snapshot)))
        self.assert_denied(lambda: self.store.get_snapshot(other.run_id, snapshot.artifact_id))

    def test_unregistered_and_non_uuid4_ids_do_not_resolve_to_paths(self):
        self.assert_denied(lambda: self.store.bind(uuid4(), role=AgentRole.DEVELOPER))
        for invalid in ("not-a-uuid", uuid1(), True, None, "../../private"):
            with self.subTest(invalid=invalid):
                self.assert_denied(lambda: self.store.bind(invalid, role=AgentRole.QA))
                self.assert_denied(lambda: self.developer.read(invalid))
        self.assert_denied(lambda: self.developer.read(uuid4()))

    def test_read_uri_requires_exact_canonical_registry_uri(self):
        snapshot = self.freeze()
        aid = str(snapshot.artifact_id)
        unsafe = (
            f"artifact://{aid}/source.tar?role=QA", f"artifact://{aid}/source.tar#content",
            f"artifact://{aid}/../source.tar", f"artifact://{aid}//source.tar",
            f"artifact://{aid}/%73ource.tar", f"artifact://{aid}/source%2etar",
            f"artifact://{aid}/source.tar/", f"artifact://{aid}/change-report.json",
            f"artifact://{aid.upper()}/source.tar", f"artifact://user@{aid}/source.tar",
            f"artifact://{aid}:80/source.tar", f"registry://{aid}/source.tar",
            str(self.source_file), "file:///etc/passwd", "https://example.invalid/source.tar",
        )
        for uri in unsafe:
            with self.subTest(uri=uri):
                self.assert_denied(lambda: self.developer.read_uri(uri))

    def test_same_manifest_handoff_is_verified_for_both_validation_roles(self):
        snapshot = self.freeze()
        handoff = SnapshotHandoff.from_snapshot(snapshot)
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                content = self.store.bind(self.run.run_id, role=role).verify_handoff(handoff)
                self.assertEqual(content.metadata.execution_manifest(), handoff.execution_manifest)
                self.assertEqual(hashlib.sha256(content.content).hexdigest(), handoff.execution_manifest.snapshot_sha256)
        self.assert_denied(lambda: self.developer.verify_handoff(handoff))

    def test_handoff_rejects_unpersisted_snapshot_and_different_uri_or_manifest(self):
        snapshot = self.freeze()
        handoff = SnapshotHandoff.from_snapshot(snapshot)
        qa = self.store.bind(self.run.run_id, role=AgentRole.QA)
        uri = handoff.model_copy(update={"artifact_uri": f"artifact://{snapshot.artifact_id}/other.tar"})
        self.assert_denied(lambda: qa.verify_handoff(uri))
        changed = handoff.execution_manifest.model_copy(update={"snapshot_sha256": "f" * 64})
        self.assert_denied(lambda: qa.verify_handoff(handoff.model_copy(update={"execution_manifest": changed})))
        ghost = snapshot.model_copy(update={
            "artifact_id": uuid4(), "artifact_uri": f"artifact://{uuid4()}/source.tar",
        })
        self.assert_denied(lambda: qa.verify_handoff(SnapshotHandoff.from_snapshot(ghost)))

    def test_candidate_checks_actual_source_not_model_supplied_hash_claims(self):
        snapshot = self.freeze()
        self.assertEqual(self.store.verify_candidate(snapshot), snapshot)
        for changes in (
            {"snapshot_sha256": "f" * 64}, {"tree_hash": "f" * 40},
            {"repository_id": "another-repo"}, {"commit_hash": "f" * 40},
            {"dependency_lock_hash": "sha256:" + "f" * 64},
            {"container_image_digest": "sha256:" + "f" * 64},
            {"artifact_uri": f"artifact://{snapshot.artifact_id}/another.tar"},
            {"workflow_step_id": uuid4()}, {"requirement_ids": (uuid4(),)},
        ):
            with self.subTest(changes=changes):
                self.assert_denied(lambda: self.store.verify_candidate(snapshot.model_copy(update=changes)))

    def test_candidate_may_add_a2a_references_without_changing_actual_content_identity(self):
        snapshot = self.freeze()
        with_references = snapshot.model_copy(update={
            "a2a_task_id": "opaque-host-task", "a2a_artifact_id": "opaque-host-artifact",
        })
        checked = self.store.verify_candidate(with_references)
        self.assertEqual(checked.artifact_id, snapshot.artifact_id)
        self.assertEqual(checked.snapshot_sha256, snapshot.snapshot_sha256)

    def test_different_committed_source_cannot_overwrite_same_candidate_version(self):
        first = self.freeze()
        expected = self.developer.read(first.artifact_id).content
        self.source_file.write_text("def signup():\n    return 'second'\n", encoding="utf-8")
        self.git("add", "src/signup.py")
        self.git("commit", "-m", "fixture different source")
        second_commit = self.git("rev-parse", "HEAD").strip()
        self.assertNotEqual(second_commit, self.commit)
        self.assert_denied(lambda: self.freeze(commit_hash=second_commit))
        self.assertEqual(self.developer.read(first.artifact_id).content, expected)
        self.assertEqual(self.freeze(), first)

    def test_freeze_does_not_use_dirty_worktree_lock_or_source_instead_of_committed_objects(self):
        (self.source / "requirements.lock").write_text("dirty==9.9\n", encoding="utf-8")
        self.source_file.write_text("# dirty code\n", encoding="utf-8")
        snapshot = self.freeze()
        self.assertEqual(snapshot.dependency_lock_hash, self.lock_hash)
        with tarfile.open(fileobj=io.BytesIO(self.developer.read(snapshot.artifact_id).content)) as archive:
            self.assertEqual(archive.extractfile("requirements.lock").read(), self.lock_bytes)
            self.assertIn(b"initial", archive.extractfile("src/signup.py").read())

    def test_missing_environment_is_not_replaced_with_a_dummy_digest(self):
        run, step, _ = self.make_run(environment=False)
        bound = self.store.bind(run.run_id, role=AgentRole.DEVELOPER)
        error = self.assert_denied(lambda: bound.freeze_source(
            workflow_step_id=step.workflow_step_id, commit_hash=self.commit,
            repository_id="company-signup-demo", lock_path="requirements.lock",
        ))
        self.assertEqual(error.code, ArtifactErrorCode.CONFIGURATION)

    def test_baseline_dependency_lock_hash_must_equal_actual_committed_lock_bytes(self):
        self.git("add", "requirements.lock")
        (self.source / "requirements.lock").write_text("changed==2.0\n", encoding="utf-8")
        self.git("add", "requirements.lock")
        self.git("commit", "-m", "fixture lock mismatch")
        commit = self.git("rev-parse", "HEAD").strip()
        self.assert_denied(lambda: self.freeze(commit_hash=commit))
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_invalid_git_ref_missing_lock_and_unsafe_lock_path_are_rejected(self):
        for value in ("HEAD", "main", "--output=/tmp/no", "f" * 40, self.commit.upper()):
            with self.subTest(commit=value):
                self.assert_denied(lambda: self.freeze(commit_hash=value))
        for value in ("missing.lock", "/etc/passwd", "../requirements.lock", "source/requirements.lock", ".git/config"):
            with self.subTest(lock=value):
                self.assert_denied(lambda: self.freeze(lock_path=value))

    def test_foreign_non_developer_or_inactive_step_cannot_freeze(self):
        other, step, _ = self.make_run()
        self.assert_denied(lambda: self.freeze(workflow_step_id=step.workflow_step_id))
        self.assert_denied(lambda: self.freeze(workflow_step_id=uuid4()))
        for role, status in (
            (AgentRole.QA, WorkflowStepStatus.RUNNING),
            (AgentRole.DEVELOPER, WorkflowStepStatus.PENDING),
            (AgentRole.DEVELOPER, WorkflowStepStatus.FAILED),
            (AgentRole.DEVELOPER, WorkflowStepStatus.CANCELED),
        ):
            with self.subTest(role=role, status=status):
                run, own_step, _ = self.make_run(step_role=role, step_status=status)
                bound = self.store.bind(run.run_id, role=AgentRole.DEVELOPER)
                self.assert_denied(lambda: bound.freeze_source(
                    workflow_step_id=own_step.workflow_step_id, commit_hash=self.commit,
                    repository_id="company-signup-demo", lock_path="requirements.lock",
                ))

    def test_inactive_or_mismatched_code_version_run_cannot_freeze(self):
        for status in (WorkflowStatus.RECEIVED, WorkflowStatus.PLANNING, WorkflowStatus.VALIDATING):
            with self.subTest(status=status):
                run, step, _ = self.make_run(run_status=status)
                bound = self.store.bind(run.run_id, role=AgentRole.DEVELOPER)
                self.assert_denied(lambda: bound.freeze_source(
                    workflow_step_id=step.workflow_step_id, commit_hash=self.commit,
                    repository_id="company-signup-demo", lock_path="requirements.lock",
                ))
        self.step.code_version = 2
        self.repository.save_task_update(
            self.run, self.step,
            AgentContext(run_id=self.run.run_id, agent_id="developer-fixture"),
            TraceEvent(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
                       event_type="FIXTURE", actor="test", attempt=0),
        )
        self.assert_denied(lambda: self.freeze())

    def test_missing_workspace_and_removed_owner_marker_block_store_access(self):
        snapshot = self.freeze()
        (self.root / ".workspace.json").unlink()
        self.assert_denied(lambda: self.developer.read(snapshot.artifact_id))
        self.assert_denied(lambda: self.freeze())

    def test_content_repr_and_errors_do_not_expose_host_paths_or_source_bytes(self):
        snapshot = self.freeze()
        content = self.developer.read(snapshot.artifact_id)
        for value in (repr(self.store), repr(self.developer), repr(content)):
            self.assertNotIn(str(self.directory), value)
            self.assertNotIn("def signup", value)
        self.assert_denied(lambda: self.developer.read_uri(str(self.source_file)))
        events = self.events()[0]
        trace = json.dumps([event.to_trace_json() for event in events])
        self.assertNotIn(str(self.directory), trace)
        self.assertNotIn("def signup", trace)

    def test_unregistered_report_ids_cannot_be_published_or_accepted_from_model_content(self):
        for role in AgentRole:
            with self.subTest(role=role):
                bound = self.store.bind(self.run.run_id, role=role)
                self.assert_denied(lambda: bound.publish_report(uuid4()))

    def test_constructor_is_inert_and_does_not_install_artifact_tables(self):
        with self.repository._connection() as connection:
            before = list(connection.execute("SELECT name FROM sqlite_master ORDER BY name"))
        candidate = ArtifactStore(self.repository, self.registry)
        self.assertIsNotNone(candidate)
        with self.repository._connection() as connection:
            after = list(connection.execute("SELECT name FROM sqlite_master ORDER BY name"))
        self.assertEqual([row[0] for row in before], [row[0] for row in after])
        self.assertEqual(self.source_file.read_text(), "def signup():\n    return 'initial'\n")

    def test_fix_candidate_increments_versions_and_keeps_prior_source_immutable(self):
        first = self.freeze()
        original_bytes = self.developer.read(first.artifact_id).content
        fixing = self.repository.get_run(self.run.run_id).model_copy(update={
            "status": WorkflowStatus.FIXING, "fix_attempt": 1, "code_version": 1,
        })
        self.repository.save_run_update(fixing, ())
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, attempt=1, code_version=2,
            requirement_ids=list(self.requirement_ids), input_artifact_ids=[first.artifact_id],
        )
        self.save_fixture_step(step)
        self.source_file.write_text("def signup():\n    return 'fixed'\n", encoding="utf-8")
        self.git("add", "src/signup.py")
        self.git("commit", "-m", "fixture fixed source")
        commit = self.git("rev-parse", "HEAD").strip()
        second = self.freeze(workflow_step_id=step.workflow_step_id, commit_hash=commit)
        self.assertEqual(second.code_version, 2)
        self.assertEqual(second.artifact_version, 2)
        self.assertEqual(second.previous_artifact_id, first.artifact_id)
        self.assertNotEqual(second.artifact_id, first.artifact_id)
        self.assertNotEqual(second.snapshot_sha256, first.snapshot_sha256)
        self.assertEqual(self.developer.read(first.artifact_id).content, original_bytes)
        self.assertEqual(self.freeze(workflow_step_id=step.workflow_step_id, commit_hash=commit), second)
        self.assertEqual(self.repository.get_run(self.run.run_id).status, WorkflowStatus.FIXING)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_candidate_version_cannot_skip_a_predecessor(self):
        first = self.freeze()
        fixing = self.repository.get_run(self.run.run_id).model_copy(update={
            "status": WorkflowStatus.FIXING, "fix_attempt": 2, "code_version": 2,
        })
        self.repository.save_run_update(fixing, ())
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, attempt=2, code_version=3,
            requirement_ids=list(self.requirement_ids),
        )
        self.save_fixture_step(step)
        self.assert_denied(lambda: self.freeze(workflow_step_id=step.workflow_step_id))
        self.assertEqual(self.store.get_snapshot(self.run.run_id, first.artifact_id), first)

    def test_requirement_report_publishes_only_redacted_registered_json_by_producer(self):
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.SUCCEEDED, requirement_ids=list(self.requirement_ids),
        )
        self.save_fixture_step(step)
        aid = uuid4()
        artifact = RequirementArtifact(
            artifact_id=aid, run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
            a2a_task_id="opaque-task", a2a_artifact_id="opaque-artifact",
            requirement_ids=self.requirement_ids,
            artifact_uri=f"artifact://{aid}/requirements.json",
            payload={"description": "email signup", "password": "report-secret-fixture"},
        )
        self.register_fixture_report(artifact)
        self.assert_denied(lambda: self.developer.publish_report(aid))
        planner = self.store.bind(self.run.run_id, role=AgentRole.PLANNER)
        # Existing registry metadata is not evidence that report bytes exist.
        self.assert_denied(lambda: planner.read(aid))
        stored = planner.publish_report(aid)
        self.assertEqual(stored.media_type, "application/json")
        self.assertNotIn(b"report-secret-fixture", stored.content)
        self.assertIn(b"[REDACTED]", stored.content)
        payload = json.loads(stored.content)
        self.assertEqual(payload["artifactId"], str(aid))
        self.assertEqual(payload["runId"], str(self.run.run_id))
        self.assertEqual(stored.content, planner.read_uri(artifact.artifact_uri).content)
        self.assertEqual(planner.publish_report(aid).content, stored.content)
        self.assertEqual(self.repository.get_run(self.run.run_id).status, WorkflowStatus.IMPLEMENTING)
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            self.assertEqual(self.store.bind(self.run.run_id, role=role).read(aid).content, stored.content)

    def test_report_uri_is_not_used_as_a_file_path_or_network_fetch(self):
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.SUCCEEDED, requirement_ids=list(self.requirement_ids),
        )
        self.save_fixture_step(step)
        aid = uuid4()
        report = RequirementArtifact(
            artifact_id=aid, run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
            a2a_task_id="opaque-task", a2a_artifact_id="opaque-artifact",
            requirement_ids=self.requirement_ids,
            artifact_uri="https://example.invalid/requirements.json", payload={},
        )
        self.register_fixture_report(report)
        planner = self.store.bind(self.run.run_id, role=AgentRole.PLANNER)
        self.assert_denied(lambda: planner.publish_report(aid))

    def test_build_report_publication_requires_exact_actual_snapshot_manifest(self):
        snapshot = self.freeze()
        aid = uuid4()
        report = BuildReportArtifact(
            artifact_id=aid, artifact_version=1, run_id=self.run.run_id,
            workflow_step_id=self.step.workflow_step_id, a2a_task_id="opaque-task",
            a2a_artifact_id="opaque-build", requirement_ids=self.requirement_ids,
            code_version=1, source_artifact_id=snapshot.artifact_id, exit_code=0,
            duration_ms=10, execution_manifest_id=uuid4(), execution_manifest=snapshot.execution_manifest(),
        )
        self.register_fixture_report(report)
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            self.assert_denied(lambda: self.store.bind(self.run.run_id, role=role).publish_report(aid))
        stored = self.developer.publish_report(aid)
        self.assert_denied(lambda: self.store.bind(self.run.run_id, role=AgentRole.PLANNER).read(aid))
        self.assertEqual(json.loads(stored.content)["executionManifest"], snapshot.execution_manifest().model_dump(mode="json", by_alias=True))
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)
        wrong_manifest = snapshot.execution_manifest().model_copy(update={"snapshot_sha256": "f" * 64})
        forged = report.model_copy(update={
            "artifact_id": uuid4(), "artifact_uri": f"artifact://{uuid4()}/build-report.json",
            "execution_manifest": wrong_manifest, "artifact_version": 2,
            "previous_artifact_id": report.artifact_id,
        })
        forged = forged.model_copy(update={"artifact_uri": f"artifact://{forged.artifact_id}/build-report.json"})
        self.register_fixture_report(forged)
        self.assert_denied(lambda: self.developer.publish_report(forged.artifact_id))

    def test_claimed_handoff_grants_cannot_replace_persisted_read_grants(self):
        snapshot = self.freeze()
        handoff = SnapshotHandoff.from_snapshot(snapshot)
        with self.repository._connection() as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='snapshot_read_grants'",
            )]
            for name in names:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute(
                "DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(snapshot.artifact_id),),
            )
            connection.commit()
        qa = self.store.bind(self.run.run_id, role=AgentRole.QA)
        self.assert_denied(lambda: qa.verify_handoff(handoff))

    def test_actual_source_content_tampering_is_detected_even_with_same_metadata_hash(self):
        snapshot = self.freeze()
        original = self.developer.read(snapshot.artifact_id).content
        tampered = bytes((original[0] ^ 1,)) + original[1:]
        with self.repository._connection() as connection:
            names = [row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='artifact_contents'",
            )]
            for name in names:
                connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
            connection.execute("UPDATE artifact_contents SET content=? WHERE artifact_id=?", (tampered, str(snapshot.artifact_id)))
            connection.commit()
        self.assert_denied(lambda: self.developer.read(snapshot.artifact_id))
        self.assert_denied(lambda: self.store.get_snapshot(self.run.run_id, snapshot.artifact_id))

    def test_report_producer_metadata_cannot_substitute_for_owned_workflow_step(self):
        aid = uuid4()
        report = RequirementArtifact(
            artifact_id=aid, run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            a2a_task_id="opaque-task", a2a_artifact_id="opaque-artifact",
            requirement_ids=self.requirement_ids,
            artifact_uri=f"artifact://{aid}/requirements.json", payload={},
        )
        self.register_fixture_report(report)
        planner = self.store.bind(self.run.run_id, role=AgentRole.PLANNER)
        self.assert_denied(lambda: planner.publish_report(aid))

    def test_empty_repository_identity_and_non_uuid_step_inputs_are_denied(self):
        for repository_id in ("", " ", None, True, "a\nrepo", "r" * 257):
            with self.subTest(repository_id=repository_id):
                self.assert_denied(lambda: self.freeze(repository_id=repository_id))
        for step in (None, True, uuid1(), "../../step"):
            with self.subTest(step=step):
                self.assert_denied(lambda: self.freeze(workflow_step_id=step))

    def validation_report_fixture(self, role, snapshot):
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=role, status=WorkflowStepStatus.SUCCEEDED,
            requirement_ids=list(self.requirement_ids), code_version=1,
            input_artifact_ids=[snapshot.artifact_id],
        )
        self.save_fixture_step(step)
        values = {
            "artifact_id": uuid4(), "artifact_version": 1, "run_id": self.run.run_id,
            "workflow_step_id": step.workflow_step_id, "a2a_task_id": "opaque-validation-task",
            "a2a_artifact_id": "opaque-validation-artifact", "requirement_ids": self.requirement_ids,
            "code_version": 1, "execution_manifest": snapshot.execution_manifest(),
        }
        if role is AgentRole.QA:
            return QAReportArtifact(**values, tests=[{
                "testId": "fixture-test", "requirementId": self.requirement_ids[0],
                "outcome": "UNVERIFIED", "title": "fixture only, no product execution",
            }])
        return SecurityReportArtifact(**values, requirementResults=[{
            "requirementId": self.requirement_ids[0], "outcome": "UNVERIFIED",
            "details": "fixture only, no Security scan executed",
        }])

    def assert_validation_publication(self, role):
        snapshot = self.freeze()
        report = self.validation_report_fixture(role, snapshot)
        self.assertEqual(report.artifact_uri, f"artifact://{report.artifact_id}/report.json")
        self.register_fixture_report(report)
        for other_role in set(AgentRole) - {role}:
            bound = self.store.bind(self.run.run_id, role=other_role)
            self.assert_denied(lambda: bound.publish_report(report.artifact_id))
        producer = self.store.bind(self.run.run_id, role=role)
        content = producer.publish_report(report.artifact_id)
        self.assertEqual(content.media_type, "application/json")
        self.assertEqual(content.content, producer.read_uri(report.artifact_uri).content)
        manifest = json.loads(content.content)["executionManifest"]
        self.assertEqual(manifest, snapshot.execution_manifest().model_dump(mode="json", by_alias=True))
        for recipient in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            self.assertEqual(self.store.bind(self.run.run_id, role=recipient).read(report.artifact_id).content, content.content)
        current = self.repository.get_run(self.run.run_id)
        self.assertEqual(current.status, WorkflowStatus.IMPLEMENTING)
        self.assertIsNone(current.verdict)

    def test_qa_report_publish_uses_existing_default_uri_actual_manifest_and_producer_role(self):
        self.assert_validation_publication(AgentRole.QA)

    def test_security_report_publish_uses_existing_default_uri_actual_manifest_and_producer_role(self):
        self.assert_validation_publication(AgentRole.SECURITY)

    def test_duplicate_freeze_cannot_ignore_a_changed_step_code_version(self):
        self.freeze()
        self.step.code_version = 2
        self.save_fixture_step(self.step)
        self.assert_denied(lambda: self.freeze())

    def test_duplicate_freeze_rechecks_run_cancellation_after_git_build(self):
        snapshot = self.freeze()
        expected = self.developer.read(snapshot.artifact_id).content
        original_build = GitSnapshotBuilder.build

        def build_then_cancel(builder, *arguments, **kwargs):
            result = original_build(builder, *arguments, **kwargs)
            current = self.repository.get_run(self.run.run_id)
            aborted = WorkflowRun.model_validate({
                **current.model_dump(), "status": WorkflowStatus.ABORTED,
                "termination_reason": "fixture cancellation while Git was running",
            })
            self.repository.save_run_update(aborted, ())
            return result

        with patch.object(GitSnapshotBuilder, "build", build_then_cancel):
            self.assert_denied(lambda: self.freeze())
        current = self.repository.get_run(self.run.run_id)
        self.assertEqual(current.status, WorkflowStatus.ABORTED)
        self.assertEqual(self.developer.read(snapshot.artifact_id).content, expected)


if __name__ == "__main__":
    unittest.main()
