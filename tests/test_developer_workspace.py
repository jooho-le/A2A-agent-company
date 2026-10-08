"""Real Git checkpoint objects/Workspace handles, no generated Source execution."""

from dataclasses import FrozenInstanceError, replace
from contextlib import contextmanager
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from agents.runtime.developer_workspace import DeveloperCheckpointError, GitDeveloperCheckpoint
from mcp_tools.tools.file_io import _locked_root, write_working
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.materialization import _archive_entries
from orchestrator.workspaces.registry import WorkspaceRegistry


class DeveloperWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-developer-checkpoint-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="Host checkpoint fixture")
        record = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id,
            root_path=str(self.registry.base_path / str(self.run.workspace_id)))
        self.repository.create_run(self.run, (), (), workspace=record)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.workspace = self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=AgentRole.DEVELOPER)
        self.source = Path(record.root_path) / "source"
        (self.source / "requirements.lock").write_bytes(b"local-fixture==1\n")
        (self.source / "app.py").write_bytes(b"raise RuntimeError('Host must not execute this Source')\n")
        (self.source / "removed.txt").write_bytes(b"remove this fixture\n")
        self.git("init", "-q", "--object-format=sha1")
        self.baseline = self.commit()
        self.checkpoint = self.make_checkpoint()

    def git(self, *arguments):
        return subprocess.run(["git", "-c", "user.name=Checkpoint Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments], cwd=self.source,
            env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": "/dev/null", "LANG": "C",
                 "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=15).stdout.strip()

    def commit(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "Host fixture baseline")
        return self.git("rev-parse", "HEAD").decode("ascii")

    def make_checkpoint(self, **changes):
        values = dict(baseline_commit_hash=self.baseline, lock_path="requirements.lock", repository_id="checkpoint-fixture")
        values.update(changes)
        return GitDeveloperCheckpoint(self.workspace, **values)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(DeveloperCheckpointError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, "DEVELOPER_CHECKPOINT_" + code)
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        return caught.exception

    def mutate_source(self):
        (self.source / "app.py").write_bytes(b"password=request.password\n")
        (self.source / "new.bin").write_bytes(bytes(range(256)))
        (self.source / "removed.txt").unlink()

    def test_constructor_is_inert_and_safe_repr(self):
        with patch("subprocess.Popen", side_effect=AssertionError("constructor IO")), patch("pathlib.Path.lstat", side_effect=AssertionError("constructor IO")):
            checkpoint = self.make_checkpoint()
        self.assertEqual(repr(checkpoint), "GitDeveloperCheckpoint()")
        self.assertNotIn(str(self.source), repr(checkpoint))

    def test_constructor_rejects_untrusted_baseline_references(self):
        for value in ("HEAD", self.baseline[:10], self.baseline.upper(), self.baseline + "^{tree}", "--help", None, True):
            with self.subTest(value=value):
                self.assert_code("INVALID", self.make_checkpoint, baseline_commit_hash=value)

    def test_constructor_rejects_unsafe_lock_or_repository_metadata(self):
        for path in ("../requirements.lock", "/etc/passwd", "nested/.env", "a//b", "a\\b", None):
            with self.subTest(path=path):
                self.assert_code("INVALID", self.make_checkpoint, lock_path=path)
        for identity in (" ", " repo", "repo\nname", "password=test-only-secret", "x" * 257, None):
            with self.subTest(identity=identity):
                self.assert_code("INVALID", self.make_checkpoint, repository_id=identity)

    def test_constructor_rejects_wrong_role_and_forged_limits(self):
        self.assert_code("INVALID", GitDeveloperCheckpoint, replace(self.workspace, role=AgentRole.QA),
            baseline_commit_hash=self.baseline, lock_path="requirements.lock", repository_id="fixture")
        limits = GitSnapshotLimits()
        object.__setattr__(limits, "max_files", True)
        self.assert_code("INVALID", self.make_checkpoint, limits=limits)

    def test_limits_cannot_expand_past_the_shared_snapshot_parser_bounds(self):
        for limits in (GitSnapshotLimits(max_files=1001), GitSnapshotLimits(max_file_bytes=1048577),
            GitSnapshotLimits(max_total_bytes=16 * 1048576 + 1), GitSnapshotLimits(max_archive_bytes=20 * 1048576 + 1)):
            with self.subTest(limits=limits):
                self.assert_code("INVALID", self.make_checkpoint, limits=limits)

    def test_clean_prepare_has_no_snapshot_registration_or_git_mutation(self):
        before = (self.source / ".git/index").read_bytes()
        self.assertIsNone(self.checkpoint.prepare())
        self.assertEqual((self.source / ".git/index").read_bytes(), before)
        self.assertEqual(self.git("rev-parse", "HEAD").decode(), self.baseline)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='artifact_contents'").fetchone())

    def test_dirty_starting_bytes_are_not_adopted(self):
        (self.source / "app.py").write_bytes(b"unrelated user edits\n")
        self.assert_code("BASELINE_MISMATCH", self.checkpoint.prepare)
        self.assert_code("STATE_INVALID", self.checkpoint.prepare)

    def test_untracked_starting_file_is_not_adopted(self):
        (self.source / "untracked.py").write_bytes(b"Host unrelated file\n")
        self.assert_code("BASELINE_MISMATCH", self.checkpoint.prepare)

    def test_starting_mode_change_is_not_adopted(self):
        (self.source / "app.py").chmod(0o755)
        self.assert_code("BASELINE_MISMATCH", self.checkpoint.prepare)

    def test_prepare_and_checkpoint_are_single_use(self):
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)
        self.checkpoint.prepare()
        self.assert_code("STATE_INVALID", self.checkpoint.prepare)
        self.mutate_source()
        self.checkpoint.checkpoint()
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)

    def test_no_change_fails_without_commit_or_replay(self):
        self.checkpoint.prepare()
        with patch.object(self.checkpoint, "_commit", side_effect=AssertionError("must not create objects")):
            self.assert_code("NO_CHANGES", self.checkpoint.checkpoint)
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)

    def test_actual_added_modified_deleted_commit_preserves_working_copy_head_and_index(self):
        self.checkpoint.prepare()
        self.mutate_source()
        before_index = (self.source / ".git/index").read_bytes()
        before_files = {path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file()}
        result = self.checkpoint.checkpoint()
        self.assertNotEqual(result.commit_hash, self.baseline)
        self.assertEqual(result.repository_id, "checkpoint-fixture")
        self.assertEqual(result.lock_path, "requirements.lock")
        self.assertEqual(result.baseline_commit_hash, self.baseline)
        self.assertEqual([(row.path, row.action) for row in result.changes],
            [("source/app.py", "MODIFIED"), ("source/new.bin", "ADDED"), ("source/removed.txt", "DELETED")])
        self.assertEqual(self.git("rev-parse", "HEAD").decode(), self.baseline)
        self.assertEqual((self.source / ".git/index").read_bytes(), before_index)
        self.assertEqual({path.name: path.read_bytes() for path in self.source.iterdir() if path.is_file()}, before_files)
        self.assertIn(b"parent " + self.baseline.encode(), self.git("cat-file", "commit", result.commit_hash))
        snapshot = GitSnapshotBuilder(self.source).build(result.commit_hash, result.lock_path)
        files = {path: content for path, content, _mode in _archive_entries(snapshot.archive)}
        self.assertEqual(files, before_files)
        self.assertEqual(snapshot.dependency_lock_hash, "sha256:" + sha256(before_files["requirements.lock"]).hexdigest())
        self.assertEqual(repr(result), "DeveloperCheckpointResult()")
        with self.assertRaises(FrozenInstanceError):
            result.commit_hash = "not mutable"

    def test_mode_only_diff_and_binary_unicode_spaces_are_captured(self):
        self.checkpoint.prepare()
        (self.source / "app.py").chmod(0o755)
        folder = self.source / "소스 폴더"
        folder.mkdir()
        (folder / "바이너리.bin").write_bytes(bytes(range(256)))
        result = self.checkpoint.checkpoint()
        entries = dict((path, (content, mode)) for path, content, mode in
            _archive_entries(GitSnapshotBuilder(self.source).build(result.commit_hash, result.lock_path).archive))
        self.assertEqual(entries["app.py"][1], 0o755)
        self.assertEqual(entries["소스 폴더/바이너리.bin"][0], bytes(range(256)))
        self.assertEqual([(row.path, row.action) for row in result.changes],
            [("source/app.py", "MODIFIED"), ("source/소스 폴더/바이너리.bin", "ADDED")])

    def test_dependency_lock_bytes_or_mode_or_deletion_cannot_change(self):
        for mutation in ("bytes", "mode", "delete"):
            with self.subTest(mutation=mutation):
                (self.source / "requirements.lock").write_bytes(b"local-fixture==1\n")
                (self.source / "requirements.lock").chmod(0o644)
                checkpoint = self.make_checkpoint()
                checkpoint.prepare()
                if mutation == "bytes":
                    (self.source / "requirements.lock").write_bytes(b"weakened dependencies\n")
                elif mutation == "mode":
                    (self.source / "requirements.lock").chmod(0o755)
                else:
                    (self.source / "requirements.lock").unlink()
                self.assert_code("LOCK_MISMATCH", checkpoint.checkpoint)

    def test_secret_symlink_hardlink_fifo_and_alias_collision_are_refused(self):
        self.checkpoint.prepare()
        mutations = (
            lambda target: target.write_bytes(b"secret path fixture"),
            lambda target: target.symlink_to(self.source / "app.py"),
            lambda target: os.link(self.source / "app.py", target),
            lambda target: os.mkfifo(target),
        )
        for index, mutation in enumerate(mutations):
            target = self.source / (".env" if index == 0 else "unsafe")
            mutation(target)
            checkpoint = self.make_checkpoint()
            self.assert_code("FAILED", checkpoint.prepare)
            target.unlink()
        (self.source / "APP.py").write_bytes(b"case alias")
        if (self.source / "APP.py").samefile(self.source / "app.py"):
            # A case-insensitive volume cannot hold both names. Simulate the
            # ambiguous directory listing, preserving actual descriptor reads.
            original_scandir = os.scandir
            @contextmanager
            def ambiguous_listing(path):
                with original_scandir(path) as entries:
                    values = list(entries)
                if isinstance(path, int) and any(entry.name == "app.py" for entry in values):
                    values.append(SimpleNamespace(name="APP.py"))
                yield iter(values)
            with patch("agents.runtime.developer_workspace.os.scandir", side_effect=ambiguous_listing):
                self.assert_code("FAILED", self.checkpoint.checkpoint)
        else:
            self.assert_code("FAILED", self.checkpoint.checkpoint)

    def test_symlink_directory_and_gitdir_file_are_not_followed(self):
        outside = self.directory / "outside"
        outside.mkdir()
        (outside / "app.py").write_bytes(b"outside source")
        link = self.source / "link"
        link.symlink_to(outside, target_is_directory=True)
        self.assert_code("FAILED", self.checkpoint.prepare)
        link.unlink()
        gitdir = self.source / ".git"
        gitdir.rename(self.directory / "metadata")
        gitdir.write_text("gitdir: ../../metadata\n")
        self.assert_code("FAILED", self.make_checkpoint().prepare)

    def test_bounds_reject_big_new_file_before_git_object_writes(self):
        checkpoint = self.make_checkpoint(limits=GitSnapshotLimits(max_file_bytes=1024))
        checkpoint.prepare()
        (self.source / "big.bin").write_bytes(b"x" * 1025)
        with patch.object(checkpoint, "_commit", side_effect=AssertionError("no objects")):
            self.assert_code("FAILED", checkpoint.checkpoint)

    def test_file_count_and_total_byte_limits_are_enforced(self):
        for limits in (GitSnapshotLimits(max_files=3), GitSnapshotLimits(max_total_bytes=128)):
            with self.subTest(limits=limits):
                checkpoint = self.make_checkpoint(limits=limits)
                checkpoint.prepare()
                (self.source / "extra.bin").write_bytes(b"x" * 256)
                self.assert_code("FAILED", checkpoint.checkpoint)
                (self.source / "extra.bin").unlink()

    def test_expired_and_invalid_deadlines_do_not_start_git(self):
        for deadline, code in ((time.monotonic() - 1, "TIMEOUT"), (True, "INVALID"), (float("nan"), "INVALID"), ("now", "INVALID")):
            with self.subTest(deadline=deadline), patch("subprocess.Popen", side_effect=AssertionError("no launch")):
                self.assert_code(code, self.make_checkpoint().prepare, deadline_monotonic=deadline)

    def test_checkpoint_expiry_cannot_extend_host_deadline(self):
        self.checkpoint.prepare()
        self.mutate_source()
        self.assert_code("TIMEOUT", self.checkpoint.checkpoint, deadline_monotonic=time.monotonic() - 1)
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)

    def test_cooperating_file_tools_share_nonblocking_workspace_lock(self):
        self.checkpoint.prepare()
        with _locked_root(self.workspace, write=True):
            self.assert_code("FAILED", self.checkpoint.checkpoint)
        (self.source / "app.py").write_bytes(b"later fixture")
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)

    def test_real_file_tool_write_is_captured_as_actual_diff(self):
        self.checkpoint.prepare()
        result = write_working(self.workspace, "source/app.py", b"password=request.password\n",
            sha256((self.source / "app.py").read_bytes()).hexdigest())
        self.assertTrue(result["changed"])
        final = self.checkpoint.checkpoint()
        self.assertEqual([(change.path, change.action) for change in final.changes], [("source/app.py", "MODIFIED")])

    def test_git_clean_filters_hooks_signers_and_fsmonitor_are_never_executed(self):
        (self.source / ".gitattributes").write_bytes(b"*.py filter=unsafe\n")
        self.baseline = self.commit()
        sentinel = self.directory / "MUST_NOT_EXIST"
        command = "touch " + str(sentinel)
        self.git("config", "filter.unsafe.clean", command)
        self.git("config", "filter.unsafe.required", "true")
        self.git("config", "core.fsmonitor", command)
        self.git("config", "commit.gpgsign", "true")
        self.git("config", "gpg.program", command)
        for name in ("pre-commit", "post-index-change"):
            hook = self.source / ".git/hooks" / name
            hook.write_text("#!/bin/sh\n" + command + "\n")
            hook.chmod(0o755)
        checkpoint = self.make_checkpoint()
        checkpoint.prepare()
        (self.source / "app.py").write_bytes(b"password=request.password\n")
        result = checkpoint.checkpoint()
        self.assertFalse(sentinel.exists())
        files = {path: content for path, content, _mode in _archive_entries(
            GitSnapshotBuilder(self.source).build(result.commit_hash, result.lock_path).archive)}
        self.assertEqual(files["app.py"], b"password=request.password\n")

    def test_git_environment_is_not_inherited_and_real_index_is_unused(self):
        self.checkpoint.prepare()
        self.mutate_source()
        real_index = self.source / ".git/index"
        original = real_index.read_bytes()
        with patch.dict(os.environ, {"GIT_DIR": "/no/source", "GIT_WORK_TREE": "/", "GIT_INDEX_FILE": "/no/index",
            "GIT_OBJECT_DIRECTORY": "/", "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path",
            "GIT_CONFIG_VALUE_0": "/do-not-read", "GIT_AUTHOR_NAME": "DUMMY_UNLABELLED_HOST_SECRET"}):
            result = self.checkpoint.checkpoint()
        self.assertEqual(real_index.read_bytes(), original)
        body = self.git("cat-file", "commit", result.commit_hash)
        self.assertNotIn(b"DUMMY_UNLABELLED_HOST_SECRET", body)
        self.assertIn(b"A2A Developer Runtime", body)

    def test_nested_git_or_private_staging_names_are_not_committed(self):
        for relative in ("nested/.git", ".mcp-write-private", "nested/.ssh", "．mcp-write-private", "a／b.py", "password=private-value.py"):
            with self.subTest(relative=relative):
                checkpoint = self.make_checkpoint()
                checkpoint.prepare()
                target = self.source / relative
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(b"private bytes")
                self.assert_code("FAILED", checkpoint.checkpoint)
                target.unlink()

    def test_git_metadata_is_revalidated_before_any_object_writes(self):
        self.checkpoint.prepare()
        self.mutate_source()
        config = self.source / ".git/config"
        config.write_bytes(config.read_bytes() + b"\n[include]\npath=/must-not-read\n")
        with patch.object(self.checkpoint, "_commit", side_effect=AssertionError("no objects")):
            self.assert_code("FAILED", self.checkpoint.checkpoint)

    def test_source_root_replacement_symlink_is_not_followed(self):
        self.checkpoint.prepare()
        self.source.rename(self.directory / "moved-source")
        self.source.symlink_to(self.directory / "moved-source", target_is_directory=True)
        self.assert_code("FAILED", self.checkpoint.checkpoint)

    def test_checkpoint_captures_ignored_files_without_git_add_filters(self):
        (self.source / ".gitignore").write_bytes(b"*.ignored\n")
        self.baseline = self.commit()
        checkpoint = self.make_checkpoint()
        checkpoint.prepare()
        (self.source / "generated.ignored").write_bytes(b"captured actual Source bytes\n")
        result = checkpoint.checkpoint()
        files = {path: content for path, content, _mode in _archive_entries(
            GitSnapshotBuilder(self.source).build(result.commit_hash, result.lock_path).archive)}
        self.assertEqual(files["generated.ignored"], b"captured actual Source bytes\n")

    def test_sha256_git_object_format_is_supported_without_oid_guessing(self):
        # Replace only this fixture's administrative metadata. Source files
        # are unchanged; no real project or user's Git metadata is touched.
        (self.source / ".git").rename(self.directory / "old-fixture-git")
        self.git("init", "-q", "--object-format=sha256")
        self.baseline = self.commit()
        checkpoint = self.make_checkpoint()
        checkpoint.prepare()
        self.mutate_source()
        result = checkpoint.checkpoint()
        self.assertEqual(len(result.commit_hash), 64)
        candidate = GitSnapshotBuilder(self.source).build(result.commit_hash, result.lock_path)
        self.assertEqual(candidate.git_object_format, "sha256")
        self.assertNotEqual(candidate.commit_hash, self.baseline)

    def test_failure_has_safe_code_without_nested_subprocess_or_source_error(self):
        self.checkpoint.prepare()
        self.mutate_source()
        with patch.object(self.checkpoint, "_commit", side_effect=ValueError("DUMMY_RAW_SOURCE_SECRET")):
            caught = self.assert_code("FAILED", self.checkpoint.checkpoint)
        self.assertNotIn("DUMMY_RAW_SOURCE_SECRET", repr(caught))

    def test_private_index_is_cleaned_even_after_object_write_failure(self):
        self.checkpoint.prepare()
        self.mutate_source()
        created = []
        original_temporary = TemporaryDirectory
        def temporary(*args, **kwargs):
            instance = original_temporary(*args, **kwargs)
            created.append(Path(instance.name))
            return instance
        original_run = self.checkpoint._run
        def fail_after_blobs(argv, environment, deadline, **options):
            if "update-index" in argv:
                raise ValueError("DUMMY_PRIVATE_GIT_ERROR")
            return original_run(argv, environment, deadline, **options)
        before_index = (self.source / ".git/index").read_bytes()
        with patch("agents.runtime.developer_workspace.tempfile.TemporaryDirectory", side_effect=temporary),\
                patch.object(self.checkpoint, "_run", side_effect=fail_after_blobs):
            self.assert_code("FAILED", self.checkpoint.checkpoint)
        self.assertEqual(len(created), 1)
        self.assertFalse(created[0].exists())
        self.assertEqual((self.source / ".git/index").read_bytes(), before_index)
        self.assertEqual(self.git("rev-parse", "HEAD").decode(), self.baseline)
        self.assert_code("STATE_INVALID", self.checkpoint.checkpoint)


if __name__ == "__main__":
    unittest.main()
