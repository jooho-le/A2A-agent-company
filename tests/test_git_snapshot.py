"""Real committed Git objects exported without executing/extracting Source."""

import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits


class GitSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-git-snapshot-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "user.name", "Snapshot fixture")
        (self.source / "uv.lock").write_bytes(b"version = 1\n")
        (self.source / "app.py").write_bytes(b"print('test source is never executed')\n")
        self.commit = self.commit_files()
        self.builder = GitSnapshotBuilder(self.source)

    def git(self, *args, source=None):
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith("GIT_"):
                environment.pop(key)
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(["git", *args], cwd=source or self.source, env=environment, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def commit_files(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode("ascii")

    def archive(self, snapshot):
        with tarfile.open(fileobj=io.BytesIO(snapshot.archive), mode="r:") as archive:
            members = archive.getmembers()
            values = {member.name: archive.extractfile(member).read() for member in members}
        return members, values

    def assert_error(self, code, operation):
        with self.assertRaises(ArtifactAccessError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.source), str(raised.exception))
        return raised.exception

    def test_constructor_has_no_io_and_does_not_expose_host_path(self):
        with patch("subprocess.Popen", side_effect=AssertionError("constructor subprocess")), patch("pathlib.Path.lstat", side_effect=AssertionError("constructor IO")):
            builder = GitSnapshotBuilder(self.root / "not-created")
        self.assertEqual(repr(builder), "GitSnapshotBuilder()")
        self.assertFalse((self.root / "not-created").exists())

    def test_committed_exact_bytes_normalized_archive_and_hashes(self):
        (self.source / "app.py").write_bytes(b"dirty working copy")
        first = self.builder.build(self.commit, "uv.lock")
        second = self.builder.build(self.commit, "uv.lock")
        self.assertEqual(first, second)
        self.assertEqual(first.commit_hash, self.commit)
        self.assertEqual(first.tree_hash, self.git("rev-parse", self.commit + "^{tree}").decode("ascii"))
        self.assertEqual(first.git_object_format, "sha1")
        self.assertEqual(first.snapshot_sha256, hashlib.sha256(first.archive).hexdigest())
        self.assertEqual(first.dependency_lock_hash, "sha256:" + hashlib.sha256(b"version = 1\n").hexdigest())
        members, values = self.archive(first)
        self.assertEqual(list(values), ["app.py", "uv.lock"])
        self.assertEqual(values["app.py"], b"print('test source is never executed')\n")
        for member in members:
            self.assertTrue(member.isfile())
            self.assertEqual((member.uid, member.gid, member.mtime, member.uname, member.gname, member.mode), (0, 0, 0, "", "", 0o644))
        self.assertNotIn("test source is never executed", repr(first))

    def test_binary_unicode_spaces_and_executable_mode_are_preserved(self):
        folder = self.source / "소스 폴더"
        folder.mkdir()
        (folder / "바이너리.bin").write_bytes(bytes(range(256)))
        script = self.source / "script.sh"
        script.write_bytes(b"#!/bin/sh\nexit 99\n")
        script.chmod(0o755)
        snapshot = self.builder.build(self.commit_files(), "uv.lock")
        members, values = self.archive(snapshot)
        self.assertEqual(values["소스 폴더/바이너리.bin"], bytes(range(256)))
        self.assertEqual(next(member.mode for member in members if member.name == "script.sh"), 0o755)

    def test_sha256_git_repository_is_verified_with_full_sha256_ids(self):
        source = self.root / "sha256-source"
        source.mkdir()
        self.git("init", "-q", "--object-format=sha256", source=source)
        self.git("config", "user.email", "fixture@example.invalid", source=source)
        self.git("config", "user.name", "fixture", source=source)
        (source / "uv.lock").write_bytes(b"sha256 lock\n")
        (source / "dir").mkdir()
        (source / "dir" / "file").write_bytes(b"nested sha256 blob")
        self.git("add", ".", source=source)
        self.git("commit", "-qm", "fixture", source=source)
        commit = self.git("rev-parse", "HEAD", source=source).decode("ascii")
        snapshot = GitSnapshotBuilder(source).build(commit, "uv.lock")
        self.assertEqual(len(snapshot.commit_hash), 64)
        self.assertEqual(len(snapshot.tree_hash), 64)
        self.assertEqual(snapshot.git_object_format, "sha256")
        self.assertEqual(self.archive(snapshot)[1]["dir/file"], b"nested sha256 blob")

    def test_only_full_lowercase_commit_ids_not_branches_revisions_or_options(self):
        for value in ("HEAD", self.commit[:12], self.commit.upper(), "--help", self.commit + "^{tree}", None, True):
            with self.subTest(value=value):
                self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(value, "uv.lock"))
        tree = self.git("rev-parse", self.commit + "^{tree}").decode("ascii")
        self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(tree, "uv.lock"))

    def test_lock_must_be_present_at_safe_committed_source_relative_path(self):
        self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(self.commit, "missing.lock"))
        for path in ("../uv.lock", "/uv.lock", "source//uv.lock", ".env", "nested/private.key", None):
            with self.subTest(path=path):
                self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, path))

    def test_export_ignore_and_export_subst_attributes_cannot_change_snapshot(self):
        (self.source / ".gitattributes").write_bytes(b"app.py export-ignore\nsubstitute.txt export-subst\n")
        (self.source / "substitute.txt").write_bytes(b"$Format:%H$\n")
        snapshot = self.builder.build(self.commit_files(), "uv.lock")
        values = self.archive(snapshot)[1]
        self.assertIn("app.py", values)
        self.assertEqual(values["substitute.txt"], b"$Format:%H$\n")

    def test_git_replace_objects_and_external_environment_do_not_change_bytes(self):
        original = self.builder.build(self.commit, "uv.lock")
        (self.source / "app.py").write_bytes(b"replacement source")
        replacement = self.commit_files()
        self.git("replace", self.commit, replacement)
        untrusted = {
            "GIT_DIR": str(self.root / "elsewhere"), "GIT_WORK_TREE": "/",
            "GIT_OBJECT_DIRECTORY": "/", "GIT_ALTERNATE_OBJECT_DIRECTORIES": "/",
            "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "include.path",
            "GIT_CONFIG_VALUE_0": "/untrusted", "GIT_REPLACE_REF_BASE": "refs/replace/",
        }
        with patch.dict(os.environ, untrusted):
            snapshot = self.builder.build(self.commit, "uv.lock")
        self.assertEqual(snapshot.archive, original.archive)
        self.assertEqual(snapshot.tree_hash, original.tree_hash)

    def test_repository_root_symlink_and_gitdir_file_are_rejected(self):
        link = self.root / "linked-source"
        link.symlink_to(self.source, target_is_directory=True)
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: GitSnapshotBuilder(link).build(self.commit, "uv.lock"))
        gitdir = self.source / ".git"
        gitdir.rename(self.source / "saved-metadata")
        gitdir.write_text("gitdir: saved-metadata\n")
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))

    def test_git_metadata_links_hardlinks_special_files_and_alternates_are_rejected(self):
        refs = self.source / ".git" / "refs"
        item = refs / "unsafe"
        item.symlink_to(self.source / "uv.lock")
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))
        item.unlink()
        os.link(self.source / "uv.lock", item)
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))
        item.unlink()
        os.mkfifo(item)
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))
        item.unlink()
        alternate = self.source / ".git" / "objects" / "info" / "alternates"
        alternate.write_text(str(self.root / "elsewhere") + "\n")
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))

    def test_include_and_partial_clone_config_are_rejected_before_snapshot_read(self):
        config = self.source / ".git" / "config"
        baseline = config.read_bytes()
        for extra in (b"\n[include]\npath=/never/read/this-secret\n", b"\n[includeIf \"gitdir:/**\"]\npath=/never/read/secret\n", b"\n[extensions]\npartialClone=origin\n", b"\n[remote \"origin\"]\npromisor=true\n"):
            with self.subTest(extra=extra):
                config.write_bytes(baseline + extra)
                self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(self.commit, "uv.lock"))
        config.write_bytes(baseline)

    def test_malformed_loose_object_and_ref_names_are_rejected(self):
        objects = self.source / ".git" / "objects"
        malformed = objects / "bad-object-directory"
        malformed.mkdir()
        self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(self.commit, "uv.lock"))
        malformed.rmdir()
        loose = objects / "ab"
        loose.mkdir(exist_ok=True)
        malformed = loose / "short-hash"
        malformed.write_bytes(b"not a Git object")
        self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(self.commit, "uv.lock"))
        malformed.unlink()
        malformed = self.source / ".git" / "refs" / "bad.lock"
        malformed.write_bytes(self.commit.encode("ascii") + b"\n")
        self.assert_error(ArtifactErrorCode.GIT_INVALID, lambda: self.builder.build(self.commit, "uv.lock"))

    def test_tracked_secret_path_is_rejected_instead_of_silently_omitted(self):
        (self.source / ".env.production").write_bytes(b"fixture-secret")
        commit = self.commit_files()
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(commit, "uv.lock"))

    def test_committed_symlink_is_rejected_not_dereferenced(self):
        (self.source / "link.py").symlink_to("app.py")
        commit = self.commit_files()
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(commit, "uv.lock"))

    def test_committed_gitlink_is_rejected_without_submodule_access(self):
        self.git("update-index", "--add", "--cacheinfo", "160000," + self.commit + ",module")
        self.git("commit", "-qm", "gitlink fixture")
        commit = self.git("rev-parse", "HEAD").decode("ascii")
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(commit, "uv.lock"))

    def test_casefold_collision_is_rejected_even_on_case_sensitive_object_tree(self):
        blob = self.git("rev-parse", self.commit + ":app.py").decode("ascii")
        self.git("update-index", "--add", "--cacheinfo", "100644," + blob + ",APP.py")
        self.git("commit", "-qm", "case collision fixture")
        commit = self.git("rev-parse", "HEAD").decode("ascii")
        self.assert_error(ArtifactErrorCode.PATH_DENIED, lambda: self.builder.build(commit, "uv.lock"))

    def test_actual_commit_object_bytes_must_match_claimed_oid(self):
        blob = self.git("rev-parse", self.commit + ":app.py").decode("ascii")
        path = self.source / ".git" / "objects" / blob[:2] / blob[2:]
        path.chmod(0o600)
        path.write_bytes(zlib.compress(b"blob 3\0bad"))
        self.assert_error(ArtifactErrorCode.INTEGRITY, lambda: self.builder.build(self.commit, "uv.lock"))

    def test_file_count_file_size_total_bytes_and_archive_size_are_bounded(self):
        for limits in (
            GitSnapshotLimits(max_files=1), GitSnapshotLimits(max_file_bytes=8),
            GitSnapshotLimits(max_total_bytes=20), GitSnapshotLimits(max_archive_bytes=1024),
        ):
            with self.subTest(limits=limits):
                self.assert_error(ArtifactErrorCode.TOO_LARGE, lambda: GitSnapshotBuilder(self.source, limits).build(self.commit, "uv.lock"))

    def test_whole_snapshot_deadline_is_bounded(self):
        builder = GitSnapshotBuilder(self.source, GitSnapshotLimits(timeout_seconds=0.000001))
        self.assert_error(ArtifactErrorCode.TIMEOUT, lambda: builder.build(self.commit, "uv.lock"))

    def test_bounded_subprocess_timeout_stdout_and_stderr_fail_without_output_leak(self):
        scripts = (
            (ArtifactErrorCode.TIMEOUT, "import time; time.sleep(3)", 1000, 0.05),
            (ArtifactErrorCode.TOO_LARGE, "import sys; sys.stdout.write('SECRET' * 10000)", 100, 3),
            (ArtifactErrorCode.TOO_LARGE, "import sys; sys.stderr.write('SECRET' * 20000)", 100, 3),
            (ArtifactErrorCode.GIT_INVALID, "import sys; sys.stderr.write('SECRET'); sys.exit(1)", 100, 3),
        )
        for code, script, cap, timeout in scripts:
            with self.subTest(code=code, script=script):
                error = self.assert_error(code, lambda: self.builder._run([sys.executable, "-c", script], {}, time.monotonic() + timeout, cap, cwd=self.source))
                self.assertNotIn("SECRET", str(error))

    def test_runner_receives_only_safe_environment_and_fixed_non_shell_arguments(self):
        original = subprocess.Popen
        calls = []
        def inspect(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)
        with patch("subprocess.Popen", side_effect=inspect), patch.dict(os.environ, {"GIT_CONFIG_SYSTEM": "/unsafe", "SECRET_TEST_TOKEN": "must-not-inherit"}):
            self.builder.build(self.commit, "uv.lock")
        self.assertTrue(calls)
        for args, kwargs in calls:
            self.assertIsInstance(args[0], list)
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["env"]["GIT_NO_REPLACE_OBJECTS"], "1")
            self.assertEqual(kwargs["env"]["GIT_CONFIG_NOSYSTEM"], "1")
            self.assertNotIn("GIT_CONFIG_SYSTEM", kwargs["env"])
            self.assertNotIn("SECRET_TEST_TOKEN", kwargs["env"])

    def test_invalid_limits_fail_before_io(self):
        for kwargs in ({"max_files": True}, {"max_total_bytes": 0}, {"max_archive_bytes": -1}, {"timeout_seconds": float("nan")}, {"timeout_seconds": 0}, {"timeout_seconds": 301}):
            with self.subTest(kwargs=kwargs):
                self.assert_error(ArtifactErrorCode.INVALID, lambda: GitSnapshotLimits(**kwargs))


if __name__ == "__main__":
    unittest.main()
