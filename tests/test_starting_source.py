"""Step 40 starting Source: real temporary Git/SQLite, no product execution.

All repositories are private, inert fixtures. These tests never write the
project Git metadata or invoke Docker, an LLM, a build, or generated Python.
"""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import time
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from agents.platform.starting_source import (
    RegisteredSourcePreparer, SourceConfiguration, SourcePreparationError,
    VerifiedStartingSource, decode_source_configuration, load_source_configuration,
    verify_starting_source,
)
from agents.platform.check_source import main as check_source
from agents.runtime.developer_workspace import GitDeveloperCheckpoint
from mcp_tools.tools.file_io import write_working
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.domain import (
    AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, ExecutionLimits, ModelConfiguration, RunConfiguration,
    RunConfigurationArtifact,
)
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.domain.trace import TraceEvent
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _insert_event
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, authorize_path,
)
from orchestrator.workspaces.registry import WorkspaceRegistry
from test_tool_configuration import LOCK, configuration_data


_IMAGE = "sha256:" + "d" * 64
_LOCK_HASH = "sha256:" + sha256(LOCK).hexdigest()
_SOURCE_MARKER = ".source-preparation.json"
_SECRET = "test-only-private-source-value"


def _files(root):
    """Snapshot fixture files, excluding access time changed by ordinary reads."""
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            path.read_bytes(), stat.S_IMODE(path.stat().st_mode), path.stat().st_mtime_ns,
        )
        for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    }


class _SourceFixture:
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-starting-source-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.seed = self.directory / "reference-repository"
        self.seed.mkdir()
        self.base = self.directory / "config"
        self.base.mkdir()
        self.file = self.base / "owned-source.json"
        (self.seed / "requirements.lock").write_bytes(LOCK)
        (self.seed / "signup.py").write_text("# inert starting Source; never executed\n", encoding="utf-8")
        (self.seed / "도움말.txt").write_text("시작 코드의 유니코드 파일\n", encoding="utf-8")
        executable = self.seed / "scripts" / "inert.sh"
        executable.parent.mkdir()
        executable.write_text("#!/bin/sh\n# inert fixture; never executed\n", encoding="utf-8")
        executable.chmod(0o755)
        self.git("init", "--object-format=sha1")
        self.git("add", "requirements.lock", "signup.py", "도움말.txt", "scripts/inert.sh")
        self.git("commit", "-m", "initial inert Source fixture")
        self.parent_commit = self.git("rev-parse", "HEAD").strip()
        (self.seed / "signup.py").write_text("# exact second committed baseline\n", encoding="utf-8")
        self.git("add", "signup.py")
        self.git("commit", "-m", "second inert Source fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.snapshot = GitSnapshotBuilder(self.seed).build(self.commit, "requirements.lock")
        self.environment = ExecutionBaseline(
            container_image_digest=_IMAGE, dependency_lock_hash=_LOCK_HASH,
            hardware_profile="offline-source-fixture", network_policy="DENY",
        )
        self.data = {
            "schemaVersion": 1, "repositoryPath": "../reference-repository",
            "repositoryId": "inert-source-fixture", "commitHash": self.commit,
            "snapshotSha256": self.snapshot.snapshot_sha256,
            "lockPath": "requirements.lock", "gitTimeoutSeconds": 30,
        }
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")

    def git(self, *arguments, cwd=None, check=True):
        result = subprocess.run([
            "git", "-c", "user.name=Starting Source Fixture",
            "-c", "user.email=source@example.invalid", "-c", "core.hooksPath=/dev/null",
            "-c", "commit.gpgsign=false", *arguments,
        ], cwd=self.seed if cwd is None else cwd, env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C", "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0",
        }, check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout if check else result

    def decode(self, data=None):
        return decode_source_configuration(json.dumps(self.data if data is None else data))

    def verify(self, data=None, *, environment=None):
        return verify_starting_source(self.decode(data), base_directory=self.base,
            environment=self.environment if environment is None else environment)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(SourcePreparationError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertNotIn(_SECRET, str(caught.exception))
        return caught.exception

    def create_run(self, *, configuration=None, status=WorkflowStatus.PLANNING,
                   planner_status=WorkflowStepStatus.RUNNING, submitted=False, **step_fields):
        fields = {}
        if status is WorkflowStatus.HUMAN_REVIEW:
            fields["resume_state"] = WorkflowStatus.PLANNING
        elif status is WorkflowStatus.ABORTED:
            fields["termination_reason"] = "inert source fixture abort"
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="동결된 시작 Source 준비", status=status, **fields)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER,
            status=planner_status, **step_fields)
        configuration = configuration or RunConfiguration(
            starting_commit_hash=self.commit, starting_snapshot_sha256=self.snapshot.snapshot_sha256,
            model=ModelConfiguration(provider="fake", model_id="fake-model", temperature=0),
            limits=ExecutionLimits(runtime_budget_ms=30000), environment=self.environment,
        )
        frozen = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
            workspace_id=run.workspace_id, configuration=configuration)
        workspace = WorkspaceRecord(workspace_id=run.workspace_id, run_id=run.run_id,
            root_path=str(self.registry.base_path / str(run.workspace_id)))
        events = (TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
            event_type="A2A_MESSAGE_SENT", actor="Orchestrator", attempt=step.attempt,
            workflow_state=run.status),) if submitted else ()
        self.repository.create_run(run, (step,), events, run_configuration=frozen, workspace=workspace)
        return run, step, Path(workspace.root_path)

    def preparer(self):
        return RegisteredSourcePreparer(self.repository, self.registry, self.verify())


class SourceConfigurationTests(_SourceFixture, unittest.TestCase):
    def test_decode_is_closed_alias_only_frozen_and_inert(self):
        before = _files(self.seed)
        configuration = self.decode()
        self.assertIs(type(configuration), SourceConfiguration)
        self.assertEqual(configuration.repository_path, "../reference-repository")
        self.assertEqual(configuration.commit_hash, self.commit)
        self.assertEqual(configuration.snapshot_sha256, self.snapshot.snapshot_sha256)
        with self.assertRaises(ValidationError):
            configuration.repository_id = "not-authorized"
        self.assertEqual(_files(self.seed), before)
        self.assertFalse(self.registry.base_path.exists())

    def test_all_configuration_fields_are_required(self):
        for field in self.data:
            changed = {name: value for name, value in self.data.items() if name != field}
            with self.subTest(field=field):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, changed)

    def test_extra_fields_and_python_field_spellings_are_not_accepted(self):
        for field in ("unapproved", "repository_path", "repository_id", "commit_hash", "snapshot_sha256",
                      "lock_path", "git_timeout_seconds", "schema_version"):
            with self.subTest(field=field):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, field: _SECRET})

    def test_schema_version_and_timeout_reject_coercion_and_nonfinite_numbers(self):
        for value in (True, False, "1", 1.0, 0, 2, None):
            with self.subTest(schema=value):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, "schemaVersion": value})
        for value in (True, False, "30", 0, -1, 300.01, None, float("inf"), float("nan")):
            with self.subTest(timeout=value):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, "gitTimeoutSeconds": value})
        for value in (.5, 1, 300):
            with self.subTest(valid_timeout=value):
                self.assertEqual(self.decode({**self.data, "gitTimeoutSeconds": value}).git_timeout_seconds, value)

    def test_exact_lowercase_full_object_ids_and_archive_hashes_required(self):
        for field, valid_lengths in (("commitHash", (40, 64)), ("snapshotSha256", (64,))):
            for length in valid_lengths:
                with self.subTest(field=field, length=length):
                    self.decode({**self.data, field: "a" * length})
            for value in ("a" * 7, "A" * 64, "g" * 64, "sha256:" + "a" * 64, "", None, True):
                with self.subTest(field=field, invalid=value):
                    self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, field: value})

    def test_lock_path_requires_canonical_safe_source_relative_syntax(self):
        for value in ("", ".", "../requirements.lock", "a/../requirements.lock", "/requirements.lock",
                      "a//lock", "a/./lock", "a\\lock", ".env", ".ssh/private", "secrets.json", "a.key",
                      "Ｃ:/lock", "a\nlock", None, True):
            with self.subTest(path=value):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, "lockPath": value})

    def test_repository_identity_and_path_cannot_contain_controls_or_shell_expansion(self):
        for field, values in (
            ("repositoryId", ("", " ", "bad\nrepo", True, None)),
            ("repositoryPath", ("", " ", "~/.ssh", "$SOURCE_ROOT", "https://invalid/repository", "bad\npath", True, None)),
        ):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, field: value})

    def test_unicode_repository_identity_uses_checkpoint_compatible_byte_limit(self):
        self.assertEqual(self.decode({**self.data, "repositoryId": "가" * 32}).repository_id, "가" * 32)
        self.assert_code("SOURCE_CONFIGURATION_INVALID", self.decode, {**self.data, "repositoryId": "가" * 100})

    def test_duplicate_json_keys_nonfinite_invalid_utf8_and_bad_json_are_safe(self):
        examples = (
            '{"schemaVersion":1,"schemaVersion":1}',
            json.dumps(self.data)[:-1] + ',"gitTimeoutSeconds":NaN}',
            "[]", "null", "not json " + _SECRET,
        )
        for text in examples:
            with self.subTest(text=text):
                self.assert_code("SOURCE_CONFIGURATION_INVALID", decode_source_configuration, text)

    def test_configuration_loader_reads_only_bounded_regular_utf8_file(self):
        self.file.write_text(json.dumps(self.data), encoding="utf-8")
        self.assertEqual(load_source_configuration(self.file), self.decode())
        for value in (b"\xff", b"x" * (65536 + 1)):
            self.file.write_bytes(value)
            with self.subTest(length=len(value)):
                self.assert_code("SOURCE_CONFIGURATION_FILE_INVALID", load_source_configuration, self.file)
        self.assert_code("SOURCE_CONFIGURATION_FILE_INVALID", load_source_configuration, self.base / "missing.json")
        self.assert_code("SOURCE_CONFIGURATION_FILE_INVALID", load_source_configuration, self.base)

    def test_loader_does_not_follow_symlink_or_accept_duplicate_or_unknown_config(self):
        self.file.write_text(json.dumps(self.data), encoding="utf-8")
        linked = self.base / "linked.json"
        linked.symlink_to(self.file)
        self.assert_code("SOURCE_CONFIGURATION_FILE_INVALID", load_source_configuration, linked)
        for text in ('{"schemaVersion":1,"schemaVersion":1}', json.dumps({**self.data, "unknown": _SECRET})):
            self.file.write_text(text, encoding="utf-8")
            with self.subTest(text=text):
                with self.assertRaises(SourcePreparationError) as caught:
                    load_source_configuration(self.file)
                self.assertIn(caught.exception.code, {"SOURCE_CONFIGURATION_FILE_INVALID", "SOURCE_CONFIGURATION_INVALID"})
                self.assertNotIn(_SECRET, str(caught.exception))


class StartingSourceVerificationTests(_SourceFixture, unittest.TestCase):
    def test_verified_export_is_immutable_and_does_not_prepare_workspace(self):
        verified = self.verify()
        self.assertIs(type(verified), VerifiedStartingSource)
        self.assertEqual(verified.configuration, self.decode())
        self.assertEqual(verified.environment, self.environment)
        self.assertEqual(verified.reference_repository_path, self.seed)
        with self.assertRaises(FrozenInstanceError):
            verified.reference_repository_path = self.base
        self.assertNotIn(str(self.seed), repr(verified))
        self.assertFalse(self.registry.base_path.exists())

    def test_dirty_and_untracked_reference_working_tree_are_ignored_without_changes(self):
        (self.seed / "signup.py").write_text("# local operator dirty bytes; do not copy\n", encoding="utf-8")
        (self.seed / "untracked.txt").write_text(_SECRET, encoding="utf-8")
        (self.seed / ".env").write_text("API_KEY=" + _SECRET, encoding="utf-8")
        before = _files(self.seed)
        verified = self.verify()
        self.assertEqual(verified.configuration.snapshot_sha256, self.snapshot.snapshot_sha256)
        self.assertEqual(_files(self.seed), before)

    def test_exact_snapshot_and_dependency_lock_hash_must_match(self):
        self.assert_code("SOURCE_BASELINE_MISMATCH", self.verify,
            {**self.data, "snapshotSha256": "a" * 64})
        wrong = self.environment.model_copy(update={"dependency_lock_hash": "sha256:" + "a" * 64})
        self.assert_code("SOURCE_BASELINE_MISMATCH", self.verify, environment=wrong)

    def test_actual_committed_lock_path_and_object_id_required(self):
        for changed in ({**self.data, "commitHash": "a" * 40},
                        {**self.data, "lockPath": "missing.lock"}):
            with self.subTest(changed=changed):
                with self.assertRaises(SourcePreparationError) as caught:
                    self.verify(changed)
                self.assertNotIn(str(self.seed), str(caught.exception))

    def test_allowlist_network_is_not_silently_weakened_to_deny(self):
        allowed = ExecutionBaseline(container_image_digest=_IMAGE, dependency_lock_hash=_LOCK_HASH,
            hardware_profile="offline-source-fixture", network_policy="ALLOWLIST", allowed_hosts=("example.invalid",))
        with self.assertRaises(SourcePreparationError):
            self.verify(environment=allowed)
        self.assertFalse(self.registry.base_path.exists())

    def test_committed_secret_paths_are_not_exported_even_if_snapshot_hash_is_forged(self):
        (self.seed / ".env").write_text("API_KEY=" + _SECRET, encoding="utf-8")
        self.git("add", ".env")
        self.git("commit", "-m", "inert forbidden committed path")
        changed = {**self.data, "commitHash": self.git("rev-parse", "HEAD").strip()}
        with self.assertRaises(SourcePreparationError) as caught:
            self.verify(changed)
        self.assertNotIn(_SECRET, str(caught.exception))
        self.assertFalse(self.registry.base_path.exists())

    def test_operator_reference_symlink_resolves_to_read_only_canonical_repository(self):
        link = self.directory / "reference-link"
        link.symlink_to(self.seed, target_is_directory=True)
        before = _files(self.seed)
        verified = self.verify({**self.data, "repositoryPath": "../reference-link"})
        self.assertEqual(verified.reference_repository_path, self.seed)
        self.assertEqual(_files(self.seed), before)

    def test_checkpoint_reserved_staging_name_is_rejected_before_workspace_writes(self):
        reserved = self.seed / ".MCP-WRITE-private"
        reserved.write_text("# inert reserved staging fixture\n", encoding="utf-8")
        self.git("add", reserved.name)
        self.git("commit", "-m", "inert reserved staging name")
        commit = self.git("rev-parse", "HEAD").strip()
        snapshot = GitSnapshotBuilder(self.seed).build(commit, "requirements.lock")
        data = {**self.data, "commitHash": commit, "snapshotSha256": snapshot.snapshot_sha256}
        before = _files(self.seed)
        self.assert_code("SOURCE_BASELINE_MISMATCH", self.verify, data)
        self.assertEqual(_files(self.seed), before)
        self.assertFalse(self.registry.base_path.exists())

    def test_verified_reference_path_cannot_retain_parent_traversal_components(self):
        verified = self.verify()
        noncanonical = self.seed / ".." / self.seed.name
        self.assertIn("..", noncanonical.parts)
        self.assert_code("SOURCE_BASELINE_MISMATCH", VerifiedStartingSource,
            configuration=verified.configuration, environment=verified.environment,
            export=verified.export, reference_repository_path=noncanonical)
        self.assertFalse(self.registry.base_path.exists())


class RegisteredSourcePreparerTests(_SourceFixture, unittest.TestCase):
    def test_constructor_is_inert_and_copies_only_registered_run(self):
        preparer = self.preparer()
        self.assertFalse(self.registry.base_path.exists())
        with self.assertRaises(SourcePreparationError):
            preparer(uuid4())
        self.assertFalse(self.registry.base_path.exists())
        run, _step, root = self.create_run()
        before = _files(self.seed)
        self.assertIsNotNone(preparer(run.run_id))
        self.assertTrue((root / "source/.git").is_dir())
        self.assertTrue((root / _SOURCE_MARKER).is_file())
        self.assertEqual(_files(self.seed), before)

    def test_real_dispatch_claim_transitions_received_pending_before_preparation(self):
        run, step, root = self.create_run(status=WorkflowStatus.RECEIVED,
            planner_status=WorkflowStepStatus.PENDING)
        claimed_run, claimed_step = self.repository.claim_planner_dispatch(run.run_id)
        self.assertEqual(claimed_run.status, WorkflowStatus.PLANNING)
        self.assertEqual(claimed_step.workflow_step_id, step.workflow_step_id)
        self.assertEqual(claimed_step.status, WorkflowStepStatus.RUNNING)
        self.assertEqual(claimed_step.attempt, 0)
        self.preparer()(run.run_id)
        self.assertEqual(GitSnapshotBuilder(root / "source").build(self.commit, "requirements.lock"),
            self.snapshot)

    def test_casefold_and_nfc_reference_workspace_overlap_is_denied_inertly(self):
        verified = self.verify()
        unicode_reference = VerifiedStartingSource(configuration=verified.configuration,
            environment=verified.environment, export=verified.export,
            reference_repository_path=self.directory / "résource")
        aliases = (
            (verified, self.seed.parent / self.seed.name.upper() / "workspaces"),
            (unicode_reference, self.directory / "re\u0301source" / "workspaces"),
        )
        for starting_source, registry_path in aliases:
            with self.subTest(registry_path=registry_path):
                registry = WorkspaceRegistry(self.repository, registry_path)
                self.assert_code("SOURCE_CONFIGURATION_INVALID", RegisteredSourcePreparer,
                    self.repository, registry, starting_source)
                self.assertFalse(registry.base_path.exists())

    def test_prepared_source_matches_exact_commit_snapshot_and_unicode_execute_bits(self):
        run, _step, root = self.create_run()
        self.preparer().prepare(run.run_id)
        source = root / "source"
        candidate = GitSnapshotBuilder(source).build(self.commit, "requirements.lock")
        self.assertEqual(candidate, self.snapshot)
        self.assertEqual((source / "도움말.txt").read_bytes(), (self.seed / "도움말.txt").read_bytes())
        self.assertEqual(stat.S_IMODE((source / "scripts/inert.sh").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((source / "signup.py").stat().st_mode), 0o644)

    def test_sha256_repository_preserves_full_object_format_and_archive_identity(self):
        self.seed = self.directory / "reference-sha256"
        self.seed.mkdir()
        (self.seed / "requirements.lock").write_bytes(LOCK)
        (self.seed / "signup.py").write_text("# inert sha256 Source fixture\n")
        result = self.git("init", "--object-format=sha256", check=False)
        if result.returncode:
            self.skipTest("installed fixture Git does not support sha256 repositories")
        self.git("add", "requirements.lock", "signup.py")
        self.git("commit", "-m", "inert sha256 Source fixture")
        self.commit = self.git("rev-parse", "HEAD").strip()
        self.snapshot = GitSnapshotBuilder(self.seed).build(self.commit, "requirements.lock")
        self.data.update(repositoryPath="../reference-sha256", commitHash=self.commit,
            snapshotSha256=self.snapshot.snapshot_sha256)
        self.assertEqual(len(self.commit), 64)
        run, _step, root = self.create_run()
        self.preparer()(run.run_id)
        candidate = GitSnapshotBuilder(root / "source").build(self.commit, "requirements.lock")
        self.assertEqual(candidate, self.snapshot)
        self.assertEqual(candidate.git_object_format, "sha256")

    def test_reference_dirty_files_hooks_remotes_index_and_parent_history_are_not_copied(self):
        (self.seed / "signup.py").write_text("# operator dirty; not committed\n", encoding="utf-8")
        (self.seed / "untracked.txt").write_text(_SECRET, encoding="utf-8")
        (self.seed / ".git/hooks/pre-commit").write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
        self.git("remote", "add", "origin", "https://not-contacted.example.invalid/private-repository")
        run, _step, root = self.create_run()
        before = _files(self.seed)
        self.preparer()(run.run_id)
        source = root / "source"
        self.assertEqual((source / "signup.py").read_text(), "# exact second committed baseline\n")
        self.assertFalse((source / "untracked.txt").exists())
        self.assertFalse((source / ".git/hooks").exists())
        self.assertFalse((source / ".git/index").exists())
        self.assertEqual(self.git("remote", cwd=source), "")
        self.assertNotEqual(self.git("cat-file", "-e", self.parent_commit, cwd=source, check=False).returncode, 0)
        self.assertEqual(_files(self.seed), before)

    def test_repeated_done_preparation_only_reverifies_and_never_rewrites(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        preparer(run.run_id)
        before = _files(root)
        self.assertIsNotNone(preparer.prepare(run.run_id))
        self.assertEqual(_files(root), before)

    def test_done_marker_source_modification_is_preserved_and_rejected(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        preparer(run.run_id)
        (root / "source/signup.py").write_text("# modified operator Source; never overwrite\n")
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", preparer.prepare, run.run_id)
        self.assertEqual(_files(root), before)

    def test_nonempty_source_without_marker_is_not_adopted_or_overwritten(self):
        run, _step, root = self.create_run()
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        (root / "source/existing.py").write_text("# existing private operator bytes\n")
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", self.preparer().prepare, run.run_id)
        self.assertEqual(_files(root), before)
        self.assertFalse((root / _SOURCE_MARKER).exists())

    def test_partial_started_marker_has_no_auto_replay_or_cleanup(self):
        run, _step, root = self.create_run()
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        (root / _SOURCE_MARKER).write_text('{"status":"STARTED"}', encoding="utf-8")
        (root / _SOURCE_MARKER).chmod(0o600)
        (root / "source/partial.py").write_text("# retained partial Source\n")
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", self.preparer().prepare, run.run_id)
        self.assertEqual(_files(root), before)

    def test_interrupted_real_preparation_preserves_started_receipt_and_partial_source(self):
        import agents.platform.starting_source as module
        run, _step, root = self.create_run()
        preparer = self.preparer()
        write = module._write
        written_source = []

        def interrupt_source(root_fd, parts, content, **options):
            result = write(root_fd, parts, content, **options)
            if parts not in ((_SOURCE_MARKER,), (".source-preparation.done",)):
                written_source.append(tuple(parts))
                raise SourcePreparationError("SOURCE_PREPARATION_FAILED")
            return result

        with patch.object(module, "_write", side_effect=interrupt_source):
            self.assert_code("SOURCE_PREPARATION_FAILED", preparer.prepare, run.run_id)
        self.assertTrue(written_source)
        self.assertEqual(json.loads((root / _SOURCE_MARKER).read_text())["status"], "STARTED")
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", preparer.prepare, run.run_id)
        self.assertEqual(_files(root), before)

    def test_foreign_or_modified_done_receipt_is_not_adopted(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        preparer(run.run_id)
        marker = root / _SOURCE_MARKER
        data = json.loads(marker.read_text())
        data["runId"] = str(uuid4())
        marker.write_text(json.dumps(data))
        marker.chmod(0o600)
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", preparer.prepare, run.run_id)
        self.assertEqual(_files(root), before)

    def test_done_receipt_schema_version_rejects_bool_and_float_not_python_numeric_equality(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        preparer(run.run_id)
        marker = root / _SOURCE_MARKER
        approved = json.loads(marker.read_text())
        for version in (True, 1.0):
            with self.subTest(version=version):
                marker.write_text(json.dumps({**approved, "schemaVersion": version}), encoding="utf-8")
                marker.chmod(0o600)
                before = _files(root)
                self.assert_code("SOURCE_PREPARATION_CONFLICT", preparer.prepare, run.run_id)
                self.assertEqual(_files(root), before)

    def test_done_source_untracked_addition_is_not_overwritten_or_accepted(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        preparer(run.run_id)
        (root / "source/untracked.txt").write_text("# local operator addition\n")
        before = _files(root)
        self.assert_code("SOURCE_PREPARATION_CONFLICT", preparer.prepare, run.run_id)
        self.assertEqual(_files(root), before)

    def test_marker_is_created_before_first_source_object_write(self):
        run, _step, root = self.create_run()
        preparer = self.preparer()
        original_write = os.write
        observed = []

        def inspect_write(descriptor, content):
            if (root / _SOURCE_MARKER).exists() and (root / "source").exists():
                try:
                    marker = json.loads((root / _SOURCE_MARKER).read_text())
                except (ValueError, OSError):
                    marker = None
                if marker is not None:
                    observed.append(marker)
            return original_write(descriptor, content)

        with patch("os.write", side_effect=inspect_write):
            preparer.prepare(run.run_id)
        self.assertTrue(observed, "a durable marker must exist before source/object writes")
        self.assertIn("STARTED", json.dumps(observed))

    def test_frozen_run_commit_snapshot_and_environment_mismatch_fail_before_workspace_write(self):
        base = RunConfiguration(starting_commit_hash=self.commit,
            starting_snapshot_sha256=self.snapshot.snapshot_sha256, environment=self.environment)
        changes = (
            {"starting_commit_hash": "a" * 40}, {"starting_snapshot_sha256": "a" * 64},
            {"environment": self.environment.model_copy(update={"container_image_digest": "sha256:" + "a" * 64})},
            {"environment": self.environment.model_copy(update={"dependency_lock_hash": "sha256:" + "a" * 64})},
            {"environment": self.environment.model_copy(update={"hardware_profile": "different-hardware"})},
        )
        preparer = self.preparer()
        for change in changes:
            run, _step, root = self.create_run(configuration=base.model_copy(update=change))
            with self.subTest(change=change):
                self.assert_code("SOURCE_BASELINE_MISMATCH", preparer.prepare, run.run_id)
                self.assertFalse(root.exists())

    def test_missing_frozen_starting_baseline_cannot_be_backfilled(self):
        run, _step, root = self.create_run(configuration=RunConfiguration(environment=self.environment))
        self.assert_code("SOURCE_BASELINE_MISMATCH", self.preparer().prepare, run.run_id)
        self.assertFalse(root.exists())

    def test_only_initial_claimed_planner_without_a2a_identity_may_prepare(self):
        variants = (
            {"status": WorkflowStatus.RECEIVED}, {"status": WorkflowStatus.IMPLEMENTING},
            {"status": WorkflowStatus.HUMAN_REVIEW}, {"status": WorkflowStatus.ABORTED},
            {"attempt": 1}, {"a2a_task_id": "existing-opaque-task"},
            {"agent_context_id": "existing-opaque-context"},
            {"planner_status": WorkflowStepStatus.PENDING}, {"planner_status": WorkflowStepStatus.SUCCEEDED},
        )
        preparer = self.preparer()
        for values in variants:
            run, _step, root = self.create_run(**values)
            with self.subTest(values=values):
                with self.assertRaises(SourcePreparationError):
                    preparer.prepare(run.run_id)
                self.assertFalse(root.exists())

    def test_a2a_message_sent_without_returned_task_id_cannot_restart_source_preparation(self):
        run, _step, root = self.create_run(submitted=True)
        with self.assertRaises(SourcePreparationError):
            self.preparer().prepare(run.run_id)
        self.assertFalse(root.exists())

    def test_a2a_sent_event_after_first_thousand_trace_rows_still_blocks_preparation(self):
        run, step, root = self.create_run()
        with self.repository._transaction() as connection:
            for _index in range(1000):
                _insert_event(connection, TraceEvent(run_id=run.run_id,
                    event_type="RUN_STARTED", actor="Orchestrator", attempt=0,
                    workflow_state=WorkflowStatus.PLANNING))
            _insert_event(connection, TraceEvent(run_id=run.run_id,
                workflow_step_id=step.workflow_step_id, event_type="A2A_MESSAGE_SENT",
                actor="Orchestrator", attempt=0, workflow_state=WorkflowStatus.PLANNING))
        first_page, total = self.repository.list_events(run.run_id, limit=1000, offset=0)
        self.assertGreater(total, 1000)
        self.assertFalse(any(event.event_type == "A2A_MESSAGE_SENT" for event in first_page))
        self.assert_code("SOURCE_BASELINE_MISMATCH", self.preparer().prepare, run.run_id)
        self.assertFalse(root.exists())

    def test_wrong_workspace_registry_and_run_owner_are_rejected(self):
        run, _step, root = self.create_run()
        wrong_registry = WorkspaceRegistry(self.repository, self.directory / "other-workspaces")
        with self.assertRaises(SourcePreparationError):
            RegisteredSourcePreparer(self.repository, wrong_registry, self.verify()).prepare(run.run_id)
        self.assertFalse(root.exists())
        self.assertFalse(wrong_registry.base_path.exists())

    def test_expired_shared_deadline_cannot_begin_preparation(self):
        run, _step, root = self.create_run()
        self.assert_code("SOURCE_PREPARATION_TIMEOUT", self.preparer().prepare, run.run_id,
            deadline_monotonic=time.monotonic() - 1)
        self.assertFalse(root.exists())

    def test_prepared_git_and_host_marker_are_not_exposed_as_agent_file_tools(self):
        run, _step, root = self.create_run()
        self.preparer()(run.run_id)
        for role in AgentRole:
            for path in ("source/.git/HEAD", "source/.git/config", _SOURCE_MARKER):
                with self.subTest(role=role, path=path), self.assertRaises(WorkspaceAccessError):
                    authorize_path(role, path, WorkspaceAccess.READ)

    def test_seeded_minimal_repository_supports_developer_checkpoint_without_host_code_execution(self):
        run, _step, root = self.create_run()
        self.preparer()(run.run_id)
        workspace = self.registry.bind(run.workspace_id, run_id=run.run_id, role=AgentRole.DEVELOPER)
        checkpoint = GitDeveloperCheckpoint(workspace, baseline_commit_hash=self.commit,
            lock_path="requirements.lock", repository_id="inert-source-fixture")
        checkpoint.prepare()
        write_working(workspace, "source/signup.py", b"# modified inert candidate; never executed\n", None)
        candidate = checkpoint.checkpoint()
        self.assertNotEqual(candidate.commit_hash, self.commit)
        snapshot = GitSnapshotBuilder(root / "source").build(candidate.commit_hash, "requirements.lock")
        self.assertEqual(snapshot.dependency_lock_hash, self.snapshot.dependency_lock_hash)
        self.assertEqual(candidate.parent_commit_hash, self.commit)
        self.assertEqual([item.path for item in candidate.changes], ["source/signup.py"])


class StartingSourceCLITests(_SourceFixture, unittest.TestCase):
    def write_configuration(self):
        self.file.write_text(json.dumps(self.data), encoding="utf-8")
        tools = self.base / "tools.json"
        tools.write_text(json.dumps(configuration_data()), encoding="utf-8")
        lock = self.directory / "templates/requirements.lock"
        lock.parent.mkdir()
        lock.write_bytes(LOCK)
        return tools

    def test_cli_is_offline_read_only_and_does_not_claim_execution_or_workspace_ready(self):
        tools = self.write_configuration()
        before = _files(self.seed)
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = check_source(["--file", str(self.file), "--tools", str(tools)])
        self.assertEqual(result, 0)
        self.assertEqual(errors.getvalue(), "")
        summary = json.loads(output.getvalue())
        self.assertEqual(summary["status"], "SOURCE_BASELINE_VALID")
        self.assertIs(summary["executionReady"], False)
        self.assertIs(summary["workspacePrepared"], False)
        self.assertNotIn(str(self.seed), output.getvalue())
        self.assertEqual(_files(self.seed), before)
        self.assertFalse(self.registry.base_path.exists())

    def test_cli_missing_invalid_and_unexpected_inputs_return_safe_codes_only(self):
        for arguments in ([], ["--file", str(self.file)],
                          ["--file", str(self.file), "--tools", str(self.base / "missing.json")],
                          ["--file", str(self.file), "--tools", "missing", "--unapproved", _SECRET]):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(arguments=arguments), redirect_stdout(output), redirect_stderr(errors):
                result = check_source(arguments)
            self.assertEqual(result, 1)
            self.assertEqual(output.getvalue(), "")
            self.assertNotIn(_SECRET, errors.getvalue())
            self.assertNotIn(str(self.directory), errors.getvalue())
            self.assertRegex(errors.getvalue(), r"^[A-Z_]+\n$")

    def test_cli_baseline_mismatch_is_not_success(self):
        tools = self.write_configuration()
        self.file.write_text(json.dumps({**self.data, "snapshotSha256": "a" * 64}), encoding="utf-8")
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            result = check_source(["--file", str(self.file), "--tools", str(tools)])
        self.assertEqual(result, 1)
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(errors.getvalue(), "SOURCE_BASELINE_MISMATCH\n")
