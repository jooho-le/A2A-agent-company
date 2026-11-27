"""Real private Git/SQLite/ASGI downloads; never execute generated Source."""

import asyncio
from hashlib import sha256
import io
import shutil
import tarfile
import unittest
from unittest.mock import patch
from uuid import uuid4
import zipfile

import httpx
from pydantic import SecretStr

import test_artifact_service as fixtures
import test_sandbox_materialization as archive_fixtures
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import GitSnapshotLimits
from orchestrator.core.config import Settings
from orchestrator.core.security import register_secret_values
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import _insert_artifact, _json_model, _upsert_step
from orchestrator.main import create_app


TOKEN = "source-download-fixture-service-token-not-a-real-key"


class SourceDownloadTests(unittest.TestCase):
    make_run = fixtures.ArtifactServiceTests.make_run
    git = fixtures.ArtifactServiceTests.git
    freeze = fixtures.ArtifactServiceTests.freeze

    def setUp(self):
        fixtures.ArtifactServiceTests.setUp(self)
        self.app = create_app(self.repository, settings=Settings(_env_file=None, log_level="CRITICAL",
            database_path=str(self.repository.database_path), workspace_root=str(self.base)))
        self.app.state.artifact_store = self.store
        self.app.state.host_api_auth_required = True
        self.app.state.host_api_token = SecretStr(TOKEN)

    def register(self, source):
        with self.repository._transaction() as connection:
            _insert_artifact(connection, source, self.repository.get_run(source.run_id).status)
        return source

    def download(self, source=None, *, run_id=None, artifact_id=None, headers=None, query=""):
        run_id = run_id or source.run_id
        artifact_id = artifact_id or source.artifact_id

        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app, raise_app_exceptions=False),
                                         base_url="http://host") as client:
                return await client.get(f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/download" + query,
                    headers={"Authorization": "Bearer " + TOKEN} if headers is None else headers)
        return asyncio.run(request())

    def rejected_events(self, run_id):
        events, _ = self.repository.list_events(run_id, limit=500, offset=0)
        return [event for event in events if event.event_type == "SOURCE_DOWNLOAD_REJECTED"]

    def hostile_archive(self, content):
        template = self.freeze()
        run, step, _ = self.make_run()
        source = template.model_copy(update={"run_id": run.run_id, "workflow_step_id": step.workflow_step_id,
            "artifact_id": uuid4(), "snapshot_sha256": sha256(content).hexdigest()})
        # Model the historical BLOB ingress: malformed tar was accepted as opaque
        # Source when its byte hash matched. The download must reject its shape.
        self.store._contents.put(source, content, "application/x-tar", grants=(AgentRole.QA, AgentRole.SECURITY))
        return self.register(source)

    def test_zip_uses_frozen_bytes_after_working_tree_is_changed_and_removed(self):
        source = self.register(self.freeze())
        original = self.source_file.read_bytes()
        self.source_file.write_text("# changed working Source, never part of ZIP\n")
        (self.source / "untracked.txt").write_text("working-only")
        shutil.rmtree(self.root)
        before = self.repository.list_events(source.run_id, limit=500, offset=0)[1]
        response = self.download(source)
        self.assertEqual(response.status_code, 200, response.text[:100] if response.status_code != 200 else "")
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(int(response.headers["content-length"]), len(response.content))
        self.assertEqual(response.headers["content-disposition"],
                         f'attachment; filename="source-{source.run_id}-v1.zip"')
        self.assertEqual(response.headers["x-snapshot-sha256"], source.snapshot_sha256)
        self.assertEqual(response.headers["x-archive-sha256"], sha256(response.content).hexdigest())
        self.assertNotEqual(response.headers["x-archive-sha256"], source.snapshot_sha256)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["requirements.lock", "src/signup.py"])
            self.assertEqual(archive.read("src/signup.py"), original)
            self.assertEqual(archive.read("requirements.lock"), self.lock_bytes)
            self.assertIsNone(archive.testzip())
        self.assertEqual(before, self.repository.list_events(source.run_id, limit=500, offset=0)[1])
        self.assertEqual(self.download(source).content, response.content)

    def test_unregistered_staged_non_source_cross_run_missing_ids_and_extra_paths_are_denied(self):
        source = self.freeze()
        self.assertEqual(self.download(source).status_code, 404)
        self.register(source)
        other, _, _ = self.make_run()
        self.assertEqual(self.download(source, run_id=other.run_id).status_code, 404)
        self.assertEqual(self.download(source, artifact_id=uuid4()).status_code, 404)
        config = self.repository.get_run_configuration(source.run_id)
        self.assertEqual(self.download(source, artifact_id=config.artifact_id).status_code, 404)
        self.assertEqual(self.download(source, headers={}).status_code, 401)
        self.assertEqual(self.download(source, artifact_id="not-a-uuid").status_code, 422)
        self.assertEqual(self.download(source, query="?artifact_uri=file:///private/not-read").status_code, 422)

    def test_selected_old_version_and_new_version_keep_their_own_bytes(self):
        first = self.register(self.freeze())
        updated = self.run.model_copy(update={"status": WorkflowStatus.FIXING, "fix_attempt": 1, "code_version": 1})
        second_step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, requirement_ids=list(self.requirement_ids), code_version=2, attempt=1)
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (updated.status.value, _json_model(updated), str(updated.run_id)))
            _upsert_step(connection, second_step)
        self.step = second_step
        self.source_file.write_text("def signup():\n    return 'second'\n", encoding="utf-8")
        self.git("add", "src/signup.py")
        self.git("commit", "-m", "private fixture second Source")
        second = self.register(self.freeze(commit_hash=self.git("rev-parse", "HEAD").strip()))
        self.assertEqual(second.code_version, 2)
        old, new = self.download(first), self.download(second)
        self.assertEqual((old.status_code, new.status_code), (200, 200))
        with zipfile.ZipFile(io.BytesIO(old.content)) as archive:
            self.assertIn(b"'initial'", archive.read("src/signup.py"))
        with zipfile.ZipFile(io.BytesIO(new.content)) as archive:
            self.assertIn(b"'second'", archive.read("src/signup.py"))
        self.assertEqual(old.headers["x-code-version"], "1")
        self.assertEqual(new.headers["x-code-version"], "2")
        self.assertNotEqual(old.headers["x-snapshot-sha256"], new.headers["x-snapshot-sha256"])

    def test_executable_unicode_empty_and_binary_files_are_preserved(self):
        (self.source / "실행.sh").write_bytes(b"#!/bin/sh\nexit 0\n")
        (self.source / "실행.sh").chmod(0o755)
        (self.source / "empty.txt").write_bytes(b"")
        (self.source / "asset.bin").write_bytes(bytes(range(256)))
        self.git("add", "실행.sh", "empty.txt", "asset.bin")
        self.git("commit", "-m", "private fixture assets")
        source = self.register(self.freeze(commit_hash=self.git("rev-parse", "HEAD").strip()))
        response = self.download(source)
        self.assertEqual(response.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.read("asset.bin"), bytes(range(256)))
            self.assertEqual(archive.read("empty.txt"), b"")
            self.assertEqual((archive.getinfo("실행.sh").external_attr >> 16) & 0o777, 0o755)

    def test_newly_registered_secret_in_old_source_is_rejected_without_mutating_snapshot_or_verdict(self):
        sentinel = "opaque-legacy-source-download-marker-for-redaction-test"
        self.source_file.write_text("# " + sentinel + "\n", encoding="utf-8")
        self.git("add", "src/signup.py")
        self.git("commit", "-m", "private fixture legacy marker")
        source = self.register(self.freeze(commit_hash=self.git("rev-parse", "HEAD").strip()))
        register_secret_values(sentinel)
        before = self.repository.get_run(source.run_id)
        for _ in range(2):
            response = self.download(source)
            self.assertEqual(response.status_code, 409, response.text)
            self.assertNotIn(sentinel, response.text)
            self.assertNotIn(str(self.directory), response.text)
        self.assertEqual(self.repository.get_run(source.run_id), before)
        self.assertEqual(len(self.rejected_events(source.run_id)), 1)
        registered = self.repository.list_project_artifacts(source.run_id)[0]
        self.assertEqual(registered.snapshot_sha256, source.snapshot_sha256)

    def test_current_host_file_count_size_and_total_limits_are_enforced(self):
        source = self.register(self.freeze())
        for limits in (GitSnapshotLimits(max_files=1), GitSnapshotLimits(max_file_bytes=1),
                       GitSnapshotLimits(max_total_bytes=1), GitSnapshotLimits(max_archive_bytes=1)):
            self.store._git_limits = limits
            response = self.download(source)
            self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual(len(self.rejected_events(source.run_id)), 1)

    def test_malformed_archive_and_hidden_trailing_bytes_are_rejected(self):
        for content in (b"not a tar", archive_fixtures._archive() + b"hidden-trailer"):
            with self.subTest(size=len(content)):
                source = self.hostile_archive(content)
                self.assertEqual(self.download(source).status_code, 409)
                self.assertEqual(len(self.rejected_events(source.run_id)), 1)

    def test_paths_links_and_duplicate_names_are_rejected_without_partial_download(self):
        cases = (
            archive_fixtures._archive((("../escape.py", b"safe fixture", 0o644),)),
            archive_fixtures._archive(((".env", b"safe fixture", 0o644),)),
            archive_fixtures._archive((("same.py", b"a", 0o644), ("same.py", b"b", 0o644))),
            archive_fixtures._archive((("link", b"", 0o644),), amend=lambda m: setattr(m, "type", tarfile.SYMTYPE)),
        )
        for content in cases:
            source = self.hostile_archive(content)
            response = self.download(source)
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.headers["content-type"], "application/json")

    def test_runtime_databases_logs_and_host_configuration_are_rejected_not_filtered(self):
        for path in ("state.sqlite3", "logs/run.txt", ".data/state.txt", "owned-runtime.local.json"):
            content = archive_fixtures._archive(((path, b"safe runtime fixture", 0o644),))
            source = self.hostile_archive(content)
            response = self.download(source)
            self.assertEqual(response.status_code, 409, path)
            self.assertNotIn(path, response.text)

    def test_storage_unavailable_and_rejection_record_failure_are_safe_errors(self):
        source = self.register(self.freeze())
        with patch.object(self.store._contents, "get", side_effect=ArtifactAccessError(ArtifactErrorCode.IO)):
            self.assertEqual(self.download(source).status_code, 503)
        with patch.object(self.store._contents, "get", side_effect=ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)):
            self.assertEqual(self.download(source).status_code, 404)
        with patch.object(self.store._contents, "get", side_effect=ArtifactAccessError(ArtifactErrorCode.INTEGRITY)), \
                patch.object(self.repository, "record_source_download_rejection", side_effect=RuntimeError(TOKEN)):
            response = self.download(source)
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(TOKEN, response.text)

    def test_final_a2a_ids_may_differ_but_registered_content_identity_must_match(self):
        staged = self.freeze()
        source = self.register(staged.model_copy(update={
            "a2a_task_id": "developer-task-opaque", "a2a_artifact_id": "source-artifact-opaque"}))
        self.assertEqual(self.download(source).status_code, 200)
        false_claim = source.model_copy(update={"snapshot_sha256": "f" * 64})
        with patch.object(self.repository, "list_project_artifacts", return_value=[false_claim]):
            response = self.download(source)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.rejected_events(source.run_id)), 1)

    def test_unprotected_bootstrap_cannot_download_preserved_source(self):
        source = self.register(self.freeze())
        self.app.state.host_api_auth_required = False
        response = self.download(source, headers={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "HOST_API_NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
