"""Real Git minimal exports, pure tamper checks, no Source execution/remote IO."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zlib

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import (
    GitExportObject, GitObjectExport, GitSnapshotBuilder, GitSnapshotLimits,
    MAX_GIT_EXPORT_BYTES, MAX_GIT_EXPORT_OBJECTS, _ObjectCollection,
    _object_hash, validate_git_export,
)


class GitObjectExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-git-export-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.git("init", "-q", "--template=")
        self.git("config", "user.email", "export@example.invalid")
        self.git("config", "user.name", "Object export fixture")
        (self.source / "uv.lock").write_bytes(b"version = 1\n")
        (self.source / "app.py").write_bytes(b"# original product is not executed\n")
        (self.source / "nested").mkdir()
        (self.source / "nested" / "same.py").write_bytes(b"# original product is not executed\n")
        self.commit = self.commit_files()
        self.builder = GitSnapshotBuilder(self.source)

    def git(self, *args, source=None):
        environment = dict(os.environ)
        for key in tuple(environment):
            if key.startswith("GIT_"):
                environment.pop(key)
        environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"})
        return subprocess.run(["git", *args], cwd=source or self.source, env=environment,
                              check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.strip()

    def commit_files(self):
        self.git("add", "--all")
        self.git("commit", "-qm", "fixture")
        return self.git("rev-parse", "HEAD").decode("ascii")

    def export(self):
        return self.builder.export(self.commit, "uv.lock")

    def assert_error(self, operation, code=ArtifactErrorCode.INTEGRITY):
        with self.assertRaises(ArtifactAccessError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.source), str(raised.exception))

    def import_fixture(self, exported):
        destination = self.root / "imported"
        destination.mkdir()
        self.git("init", "-q", "--template=", "--object-format=" + exported.snapshot.git_object_format, source=destination)
        for item in exported.objects:
            folder = destination / ".git" / "objects" / item.oid[:2]
            folder.mkdir(exist_ok=True)
            raw = item.kind.encode() + b" " + str(len(item.content)).encode() + b"\0" + item.content
            (folder / item.oid[2:]).write_bytes(zlib.compress(raw))
        (destination / ".git" / "HEAD").write_bytes(exported.snapshot.commit_hash.encode() + b"\n")
        return destination

    def test_exact_commit_current_tree_blob_export_matches_existing_build(self):
        exported = self.export()
        self.assertEqual(exported.snapshot, self.builder.build(self.commit, "uv.lock"))
        self.assertIsNone(validate_git_export(exported))
        self.assertEqual(sum(item.kind == "commit" for item in exported.objects), 1)
        self.assertEqual(sum(item.kind == "tree" for item in exported.objects), 2)
        self.assertEqual(sum(item.kind == "blob" for item in exported.objects), 2)
        self.assertEqual(tuple(item.oid for item in exported.objects), tuple(sorted(item.oid for item in exported.objects)))
        for item in exported.objects:
            self.assertEqual(_object_hash("sha1", item.kind, item.content), item.oid)

    def test_same_blob_at_multiple_paths_is_collected_once(self):
        exported = self.export()
        oid = self.git("rev-parse", self.commit + ":app.py").decode()
        self.assertEqual(self.git("rev-parse", self.commit + ":nested/same.py").decode(), oid)
        self.assertEqual(sum(item.oid == oid for item in exported.objects), 1)

    def test_history_parent_and_unrelated_objects_are_not_exported(self):
        old_commit = self.commit
        (self.source / "app.py").write_bytes(b"# new product code\n")
        (self.source / "nested" / "same.py").write_bytes(b"# another new product\n")
        self.commit = self.commit_files()
        # A tag and stale parent blob are real repository data, not export data.
        self.git("tag", "fixture-history", old_commit)
        exported = self.export()
        oids = {item.oid for item in exported.objects}
        self.assertNotIn(old_commit, oids)
        old_blob = self.git("rev-parse", old_commit + ":app.py").decode()
        self.assertNotIn(old_blob, oids)
        commit = next(item for item in exported.objects if item.kind == "commit")
        self.assertIn(b"parent " + old_commit.encode() + b"\n", commit.content)
        destination = self.import_fixture(exported)
        self.assertEqual(GitSnapshotBuilder(destination).build(self.commit, "uv.lock"), exported.snapshot)
        self.assertFalse((destination / "app.py").exists())

    def test_dirty_worktree_head_move_and_hook_remote_config_are_not_copied(self):
        hook = self.source / ".git" / "hooks"
        hook.mkdir(exist_ok=True)
        script = hook / "pre-commit"
        sentinel = self.root / "hook-ran"
        script.write_bytes(b"#!/bin/sh\ntouch " + str(sentinel).encode() + b"\n")
        script.chmod(0o755)
        self.git("config", "remote.origin.url", "https://example.invalid/private.git")
        config_before = (self.source / ".git" / "config").read_bytes()
        head_before = (self.source / ".git" / "HEAD").read_bytes()
        index_before = (self.source / ".git" / "index").read_bytes()
        (self.source / "app.py").write_bytes(b"dirty file is excluded\n")
        exported = self.export()
        self.assertFalse(sentinel.exists())
        self.assertNotIn(b"dirty file is excluded", exported.snapshot.archive)
        self.assertNotIn(b"private.git", exported.snapshot.archive)
        self.assertEqual((self.source / ".git" / "config").read_bytes(), config_before)
        self.assertEqual((self.source / ".git" / "HEAD").read_bytes(), head_before)
        self.assertEqual((self.source / ".git" / "index").read_bytes(), index_before)
        self.assertEqual((self.source / "app.py").read_bytes(), b"dirty file is excluded\n")

    def test_export_read_only_source_and_metadata_unchanged(self):
        before = {path.relative_to(self.source).as_posix(): path.read_bytes()
                  for path in self.source.rglob("*") if path.is_file()}
        self.export()
        after = {path.relative_to(self.source).as_posix(): path.read_bytes()
                 for path in self.source.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_packed_current_objects_export_and_reconstruct_without_pack_copy(self):
        self.git("gc", "--prune=now", "--quiet")
        self.assertTrue(tuple((self.source / ".git" / "objects" / "pack").glob("*.pack")))
        exported = self.export()
        destination = self.import_fixture(exported)
        self.assertEqual(GitSnapshotBuilder(destination).build(self.commit, "uv.lock"), exported.snapshot)
        self.assertFalse(tuple((destination / ".git" / "objects" / "pack").glob("*.pack")))

    def test_sha256_repository_object_export_and_reconstruct(self):
        source = self.root / "sha256"
        source.mkdir()
        self.git("init", "-q", "--template=", "--object-format=sha256", source=source)
        self.git("config", "user.email", "export@example.invalid", source=source)
        self.git("config", "user.name", "fixture", source=source)
        (source / "uv.lock").write_bytes(b"version = 2\n")
        (source / "file.bin").write_bytes(bytes(range(256)))
        self.git("add", "--all", source=source)
        self.git("commit", "-qm", "fixture", source=source)
        commit = self.git("rev-parse", "HEAD", source=source).decode()
        exported = GitSnapshotBuilder(source).export(commit, "uv.lock")
        self.assertTrue(all(len(item.oid) == 64 for item in exported.objects))
        destination = self.import_fixture(exported)
        self.assertEqual(GitSnapshotBuilder(destination).build(commit, "uv.lock"), exported.snapshot)

    def test_minimal_reconstructed_repository_has_no_copied_refs_hooks_or_worktree(self):
        exported = self.export()
        destination = self.import_fixture(exported)
        self.assertEqual(GitSnapshotBuilder(destination).build(self.commit, "uv.lock"), exported.snapshot)
        self.assertFalse(tuple(path for path in (destination / ".git" / "refs").rglob("*") if path.is_file()))
        self.assertFalse(tuple((destination / ".git" / "hooks").rglob("*")) if (destination / ".git" / "hooks").exists() else False)
        self.assertEqual({path.name for path in destination.iterdir()}, {".git"})

    def test_export_and_object_reprs_hide_source_bytes_and_commit_messages(self):
        exported = self.export()
        self.assertEqual(repr(exported), "GitObjectExport()")
        for item in exported.objects:
            self.assertNotIn("original product", repr(item))
            self.assertNotIn("Object export fixture", repr(item))

    def test_export_objects_are_immutable_bytes_tuple_and_frozen(self):
        exported = self.export()
        with self.assertRaises(FrozenInstanceError):
            exported.objects = ()
        with self.assertRaises(FrozenInstanceError):
            exported.objects[0].content = b"changed"
        self.assert_error(lambda: GitObjectExport(exported.snapshot, list(exported.objects)), ArtifactErrorCode.INVALID)
        blob = next(item for item in exported.objects if item.kind == "blob")
        for mutable in (bytearray(blob.content), memoryview(blob.content), "not-bytes"):
            self.assert_error(lambda: GitExportObject(blob.oid, blob.kind, mutable), ArtifactErrorCode.INVALID)

    def test_object_constructor_rejects_wrong_oid_format_kind_and_content_hash(self):
        blob = next(item for item in self.export().objects if item.kind == "blob")
        for oid in (blob.oid.upper(), blob.oid[:12], "HEAD", None, True):
            self.assert_error(lambda: GitExportObject(oid, "blob", blob.content), ArtifactErrorCode.INVALID)
        for kind in ("tag", "config", "tree ", None):
            self.assert_error(lambda: GitExportObject(blob.oid, kind, blob.content), ArtifactErrorCode.INVALID)
        self.assert_error(lambda: GitExportObject(blob.oid, "blob", blob.content + b"forged"))

    def test_correct_hash_does_not_accept_noncanonical_commit_or_tree_bytes(self):
        for kind, content in (("commit", b"not a tree header\n"), ("tree", b"100644 file\0too-short")):
            oid = _object_hash("sha1", kind, content)
            self.assert_error(lambda: GitExportObject(oid, kind, content), ArtifactErrorCode.GIT_INVALID)
        blob = next(item for item in self.export().objects if item.kind == "blob")
        raw_oid = bytes.fromhex(blob.oid)
        reversed_names = b"100644 z\0" + raw_oid + b"100644 a\0" + raw_oid
        self.assert_error(lambda: GitExportObject(_object_hash("sha1", "tree", reversed_names), "tree", reversed_names), ArtifactErrorCode.GIT_INVALID)

    def test_missing_commit_root_tree_nested_tree_or_blob_is_rejected(self):
        exported = self.export()
        for item in exported.objects:
            with self.subTest(kind=item.kind, oid=item.oid):
                self.assert_error(lambda: GitObjectExport(exported.snapshot, tuple(other for other in exported.objects if other.oid != item.oid)))

    def test_duplicate_or_extra_unrelated_objects_are_rejected(self):
        exported = self.export()
        self.assert_error(lambda: GitObjectExport(exported.snapshot, exported.objects + (exported.objects[0],)))
        content = b"unrelated old source\n"
        extra = GitExportObject(_object_hash("sha1", "blob", content), "blob", content)
        self.assert_error(lambda: GitObjectExport(exported.snapshot, exported.objects + (extra,)))

    def test_parent_commit_in_object_tuple_is_rejected_despite_valid_hash(self):
        old_commit = self.commit
        parent = next(item for item in self.export().objects if item.oid == old_commit)
        (self.source / "app.py").write_bytes(b"# next product\n")
        self.commit = self.commit_files()
        exported = self.export()
        self.assert_error(lambda: GitObjectExport(exported.snapshot, exported.objects + (parent,)))

    def test_snapshot_archive_hash_or_canonical_bytes_mismatch_is_rejected(self):
        exported = self.export()
        self.assert_error(lambda: GitObjectExport(replace(exported.snapshot, snapshot_sha256="f" * 64), exported.objects))
        hidden = exported.snapshot.archive + b"hidden trailing data"
        snapshot = replace(exported.snapshot, archive=hidden, snapshot_sha256=hashlib.sha256(hidden).hexdigest())
        self.assert_error(lambda: GitObjectExport(snapshot, exported.objects))

    def test_snapshot_commit_tree_format_lock_metadata_mismatches_are_rejected(self):
        exported = self.export()
        self.assert_error(lambda: GitObjectExport(replace(exported.snapshot, tree_hash="f" * 40), exported.objects))
        self.assert_error(lambda: GitObjectExport(replace(exported.snapshot, commit_hash="f" * 40), exported.objects))
        self.assert_error(lambda: GitObjectExport(replace(exported.snapshot, git_object_format="sha256"), exported.objects), ArtifactErrorCode.INVALID)
        self.assert_error(lambda: GitObjectExport(replace(exported.snapshot, dependency_lock_hash="sha256:" + hashlib.sha256(b"not in graph").hexdigest()), exported.objects))

    def test_validate_rechecks_even_frozen_objects_after_host_bypasses_dataclass(self):
        exported = self.export()
        blob = next(item for item in exported.objects if item.kind == "blob")
        object.__setattr__(blob, "content", blob.content + b"tampered by Host")
        self.assert_error(lambda: validate_git_export(exported))

    def test_validation_is_pure_no_git_filesystem_or_subprocess(self):
        exported = self.export()
        with patch("subprocess.Popen", side_effect=AssertionError("must not execute Git")), patch("pathlib.Path.lstat", side_effect=AssertionError("must not touch FS")):
            self.assertIsNone(validate_git_export(exported))
            GitObjectExport(exported.snapshot, exported.objects)

    def test_export_uses_same_branch_path_and_secret_checks_as_build(self):
        for revision in ("HEAD", self.commit[:12], "--help", self.commit.upper()):
            self.assert_error(lambda: self.builder.export(revision, "uv.lock"), ArtifactErrorCode.GIT_INVALID)
        for path in ("../uv.lock", "/uv.lock", ".env", "private.key"):
            self.assert_error(lambda: self.builder.export(self.commit, path), ArtifactErrorCode.PATH_DENIED)

    def test_limits_deadline_and_cumulative_object_collection_are_bounded(self):
        self.assert_error(lambda: GitSnapshotBuilder(self.source, GitSnapshotLimits(max_files=1)).export(self.commit, "uv.lock"), ArtifactErrorCode.TOO_LARGE)
        self.assert_error(lambda: GitSnapshotBuilder(self.source, GitSnapshotLimits(timeout_seconds=0.000001)).export(self.commit, "uv.lock"), ArtifactErrorCode.TIMEOUT)
        collection = _ObjectCollection()
        with patch("orchestrator.artifacts.git_snapshot.MAX_GIT_EXPORT_BYTES", 8):
            self.assert_error(lambda: collection.add(_object_hash("sha1", "blob", b"123456789"), "blob", b"123456789"), ArtifactErrorCode.TOO_LARGE)
        collection.add(_object_hash("sha1", "blob", b"a"), "blob", b"a")
        with patch("orchestrator.artifacts.git_snapshot.MAX_GIT_EXPORT_OBJECTS", 1):
            self.assert_error(lambda: collection.add(_object_hash("sha1", "blob", b"b"), "blob", b"b"), ArtifactErrorCode.TOO_LARGE)
        oid = next(iter(collection.objects))
        self.assert_error(lambda: collection.add(oid, "blob", b"different same-oid content"))
        self.assertLessEqual(collection.size_bytes, MAX_GIT_EXPORT_BYTES)
        self.assertLessEqual(len(collection.objects), MAX_GIT_EXPORT_OBJECTS)
