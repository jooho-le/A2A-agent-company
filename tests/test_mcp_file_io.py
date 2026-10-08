"""Real, temporary Workspace file boundaries; no generated code is executed."""

from contextlib import contextmanager
import errno
import fcntl
import hashlib
import os
from pathlib import Path
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import traceback
import unittest
from unittest.mock import patch

from mcp_tools.tools import file_io
from mcp_tools.tools.file_io import (
    FileOperationError, MAX_BATCH_FILES, MAX_DIRECTORY_DEPTH, MAX_FILE_BYTES,
    MAX_TOTAL_CHANGE_BYTES, apply_changes, read_working, write_working,
)
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import WorkspaceAccessError
from orchestrator.workspaces.registry import WorkspaceRegistry


def digest(content):
    return hashlib.sha256(content).hexdigest()


@unittest.skipUnless(os.name == "posix", "Pinned Workspace descriptors require POSIX")
class MCPFileIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-mcp-file-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base / str(self.run.workspace_id)
        self.record = WorkspaceRecord(
            workspace_id=self.run.workspace_id, run_id=self.run.run_id,
            root_path=str(self.root),
        )
        self.repository.create_run(self.run, (), (), workspace=self.record)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.bound = self.bind(AgentRole.DEVELOPER)

    def bind(self, role):
        return self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=role)

    def put(self, path, content=b"original\n", mode=0o600):
        actual = self.root / path
        actual.parent.mkdir(parents=True, exist_ok=True)
        actual.write_bytes(content)
        actual.chmod(mode)
        return actual

    def assert_error(self, expected, operation):
        with self.assertRaises(FileOperationError) as raised:
            operation()
        self.assertEqual(raised.exception.code, expected)
        self.assertEqual(str(raised.exception), expected)
        self.assertNotIn(str(self.directory), str(raised.exception))
        self.assertNotIn(str(self.directory), "".join(traceback.format_exception_only(raised.exception)))
        return raised.exception

    def assert_no_stages(self):
        self.assertEqual(list(self.root.rglob(".mcp-write-*")), [])

    @contextmanager
    def hold_root_lock(self, mode):
        with self.bound.opener() as descriptor:
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def test_read_preserves_exact_utf8_crlf_and_bytes(self):
        content = "# 한글\r\npassword = request.password\r\n".encode()
        self.put("source/한글 파일.py", content)
        self.assertEqual(read_working(self.bound, "source/한글 파일.py"), content)
        self.assert_no_stages()

    def test_read_empty_file(self):
        self.put("source/empty.py", b"")
        self.assertEqual(read_working(self.bound, "source/empty.py"), b"")

    def test_read_binary_keeps_text_decoding_policy_outside_file_io(self):
        self.put("source/data.bin", b"\xff\x00\x01")
        self.assertEqual(read_working(self.bound, "source/data.bin"), b"\xff\x00\x01")

    def test_read_missing_file_and_parent_are_stable_errors(self):
        for path in ("source/missing.py", "source/missing/leaf.py"):
            with self.subTest(path=path):
                self.assert_error("FILE_NOT_FOUND", lambda: read_working(self.bound, path))
        self.assertFalse((self.root / "source/missing").exists())

    def test_read_accepts_limit_and_denies_oversized_file(self):
        self.put("source/max.py", b"x" * MAX_FILE_BYTES)
        self.assertEqual(len(read_working(self.bound, "source/max.py")), MAX_FILE_BYTES)
        self.put("source/large.py", b"x" * (MAX_FILE_BYTES + 1))
        self.assert_error("FILE_TOO_LARGE", lambda: read_working(self.bound, "source/large.py"))

    def test_read_allowed_internal_symlink_preserves_existing_policy(self):
        self.put("source/target.py", b"# product\n")
        (self.root / "source/link.py").symlink_to("target.py")
        self.assertEqual(read_working(self.bound, "source/link.py"), b"# product\n")

    def test_no_symlink_read_rejects_internal_leaf_and_parent_links(self):
        self.put("source/actual/app.py", b"working product")
        (self.root / "source/link.py").symlink_to("actual/app.py")
        (self.root / "source/linked").symlink_to("actual", target_is_directory=True)
        for path in ("source/link.py", "source/linked/app.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: read_working(self.bound, path, allow_symlinks=False))
        self.assertEqual(read_working(self.bound, "source/actual/app.py", allow_symlinks=False), b"working product")

    def test_qa_scratch_link_cannot_alias_mutable_source_in_no_symlink_mode(self):
        self.put("source/app.py", b"unfrozen working source")
        (self.root / "outputs/qa/link.py").symlink_to("../../source/app.py")
        self.assert_error("PATH_DENIED", lambda: read_working(self.bind(AgentRole.QA), "outputs/qa/link.py", allow_symlinks=False))

    def test_read_alias_cannot_reach_reserved_staging_file(self):
        self.put("source/.mcp-write-private.tmp", b"reserved backup")
        (self.root / "source/alias.py").symlink_to(".mcp-write-private.tmp")
        self.assert_error("PATH_DENIED", lambda: read_working(self.bound, "source/alias.py"))

    def test_read_requires_trusted_boolean_symlink_policy(self):
        self.put("source/app.py")
        self.assert_error("PATH_DENIED", lambda: read_working(self.bound, "source/app.py", allow_symlinks="false"))

    def test_read_rejects_outside_and_secret_symlink_targets(self):
        outside = self.directory / "outside.py"
        outside.write_bytes(b"Host-only source")
        self.put("source/.env", b"TEST_SECRET=private")
        (self.root / "source/host.py").symlink_to(outside)
        (self.root / "source/private.py").symlink_to(".env")
        for path in ("source/host.py", "source/private.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: read_working(self.bound, path))

    def test_read_denies_hardlinks_and_special_files_without_blocking(self):
        actual = self.put("source/hard.py")
        os.link(actual, self.directory / "linked.py")
        os.mkfifo(self.root / "source/fifo.py")
        (self.root / "source/directory.py").mkdir()
        for path in ("source/hard.py", "source/fifo.py", "source/directory.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: read_working(self.bound, path))

    def test_read_detects_uncooperative_byte_changes(self):
        actual = self.put("source/app.py", b"before")
        original_read = os.read
        changed = False

        def mutate(descriptor, size):
            nonlocal changed
            result = original_read(descriptor, size)
            if not changed and result == b"before":
                changed = True
                actual.write_bytes(b"after!!")
            return result

        with patch.object(file_io.os, "read", side_effect=mutate):
            self.assert_error("WRITE_CONFLICT", lambda: read_working(self.bound, "source/app.py"))

    def test_read_does_not_leak_open_file_descriptors(self):
        self.put("source/app.py")
        captured = []
        original = os.read

        def observe(descriptor, size):
            captured.append(descriptor)
            return original(descriptor, size)

        with patch.object(file_io.os, "read", side_effect=observe):
            read_working(self.bound, "source/app.py")
        for descriptor in set(captured):
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_write_new_source_creates_only_scoped_parents_and_private_file(self):
        content = "# 새 파일\n".encode()
        result = write_working(self.bound, "source/generated/nested/app.py", content)
        self.assertEqual(result, {
            "path": "source/generated/nested/app.py", "sha256": digest(content),
            "sizeBytes": len(content), "changed": True,
        })
        self.assertEqual((self.root / result["path"]).read_bytes(), content)
        self.assertEqual(stat.S_IMODE((self.root / result["path"]).stat().st_mode), 0o600)
        self.assert_no_stages()

    def test_write_replaces_complete_file_and_preserves_executable_rwx_mode(self):
        actual = self.put("source/app.py", b"long old source", mode=0o751)
        original_inode = actual.stat().st_ino
        result = write_working(self.bound, "source/app.py", b"new")
        self.assertTrue(result["changed"])
        self.assertEqual(actual.read_bytes(), b"new")
        self.assertNotEqual(actual.stat().st_ino, original_inode)
        self.assertEqual(stat.S_IMODE(actual.stat().st_mode), 0o751)
        self.assert_no_stages()

    def test_write_never_preserves_setuid_or_setgid_bits(self):
        actual = self.put("source/app.py", b"old", mode=0o6751)
        write_working(self.bound, "source/app.py", b"new")
        self.assertEqual(stat.S_IMODE(actual.stat().st_mode), 0o751)

    def test_identical_write_reports_unchanged_without_inode_or_mode_change(self):
        actual = self.put("source/app.py", b"same", mode=0o750)
        before = actual.stat()
        result = write_working(self.bound, "source/app.py", b"same", expected_sha256=digest(b"same"))
        after = actual.stat()
        self.assertFalse(result["changed"])
        self.assertEqual((before.st_ino, before.st_mtime_ns, before.st_mode), (after.st_ino, after.st_mtime_ns, after.st_mode))
        self.assert_no_stages()

    def test_creating_empty_file_is_still_a_change(self):
        result = write_working(self.bound, "source/empty.py", b"")
        self.assertTrue(result["changed"])
        self.assertEqual(result["sha256"], digest(b""))
        self.assertEqual(result["sizeBytes"], 0)
        self.assertTrue((self.root / "source/empty.py").is_file())

    def test_write_hash_compare_and_swap_success_and_conflict(self):
        actual = self.put("source/app.py", b"old")
        self.assert_error("WRITE_CONFLICT", lambda: write_working(self.bound, "source/app.py", b"new", expected_sha256="0" * 64))
        self.assertEqual(actual.read_bytes(), b"old")
        write_working(self.bound, "source/app.py", b"new", expected_sha256=digest(b"old"))
        self.assertEqual(actual.read_bytes(), b"new")

    def test_write_hash_compare_and_swap_never_treats_absence_as_empty_content(self):
        self.assert_error("WRITE_CONFLICT", lambda: write_working(self.bound, "source/new/empty.py", b"", expected_sha256=digest(b"")))
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_optional_write_hash_none_means_no_compare_and_swap(self):
        actual = self.put("source/app.py", b"old")
        write_working(self.bound, "source/app.py", b"new", expected_sha256=None)
        self.assertEqual(actual.read_bytes(), b"new")

    def test_invalid_hash_and_nonbytes_are_denied_before_any_mutation(self):
        for value in ("short", "A" * 64, 3, True):
            with self.subTest(hash=value):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, "source/new/app.py", b"source", expected_sha256=value))
        for content in ("source", bytearray(b"source"), None, 1):
            with self.subTest(content=type(content)):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, "source/new/app.py", content))
        self.assertFalse((self.root / "source/new").exists())

    def test_large_new_content_has_no_filesystem_side_effect(self):
        self.assert_error("FILE_TOO_LARGE", lambda: write_working(self.bound, "source/new/app.py", b"x" * (MAX_FILE_BYTES + 1)))
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_write_role_matrix_cannot_touch_other_regions(self):
        cases = (
            (AgentRole.DEVELOPER, "outputs/qa/test.py"),
            (AgentRole.DEVELOPER, "planning/plan.json"),
            (AgentRole.DEVELOPER, "snapshots/app.py"),
            (AgentRole.QA, "source/app.py"),
            (AgentRole.QA, "outputs/security/report.py"),
            (AgentRole.SECURITY, "outputs/security/report.py"),
            (AgentRole.PLANNER, "planning/plan.py"),
        )
        for role, path in cases:
            with self.subTest(role=role, path=path):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bind(role), path, b"source"))
                self.assertFalse((self.root / path).exists())

    def test_qa_scoped_write_is_available_for_test_tool_wrapper(self):
        result = write_working(self.bind(AgentRole.QA), "outputs/qa/tests/test_app.py", b"# test\n")
        self.assertTrue(result["changed"])
        self.assertEqual((self.root / result["path"]).read_bytes(), b"# test\n")

    def test_bad_paths_and_secret_paths_are_denied_before_any_mutation(self):
        for path in (
            str(self.directory / "host.py"), "../source/file.py", "source/../host.py",
            "source//file.py", "source/file.py/", "source\\file.py", "C:/source/file.py",
            "file://source/file.py", "source/.env", "source/.git/config",
            "source/credentials.json", "source/key.pem", "source/a\x00b",
            "source/a\nb", "source/\ud800.py", "sourceevil/file.py",
        ):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, path, b"source"))
        self.assertEqual(list((self.root / "source").iterdir()), [])

    def test_staging_prefix_is_reserved_for_every_path_component_and_read(self):
        for path in ("source/.mcp-write-guess.tmp", "source/.MCP-WRITE-parent/file.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, path, b"source"))
                self.assert_error("PATH_DENIED", lambda: read_working(self.bound, path))
        self.assert_no_stages()

    def test_write_denies_leaf_and_parent_symlinks_even_when_internal(self):
        actual = self.put("source/actual/app.py", b"old")
        (self.root / "source/linked").symlink_to("actual", target_is_directory=True)
        (self.root / "source/link.py").symlink_to("actual/app.py")
        for path in ("source/link.py", "source/linked/app.py", "source/linked/new/app.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, path, b"new"))
        self.assertEqual(actual.read_bytes(), b"old")
        self.assertFalse((self.root / "source/actual/new").exists())
        self.assert_no_stages()

    def test_write_denies_hardlinks_and_special_nodes_without_truncation(self):
        actual = self.put("source/hard.py", b"old")
        outside = self.directory / "outside.py"
        os.link(actual, outside)
        os.mkfifo(self.root / "source/fifo.py")
        (self.root / "source/directory.py").mkdir()
        for path in ("source/hard.py", "source/fifo.py", "source/directory.py"):
            with self.subTest(path=path):
                self.assert_error("PATH_DENIED", lambda: write_working(self.bound, path, b"new"))
        self.assertEqual(outside.read_bytes(), b"old")
        self.assert_no_stages()

    def test_atomic_replace_uses_pinned_parent_not_reopened_model_path(self):
        original = self.put("source/sub/app.py", b"old")
        outside_dir = self.directory / "host"
        outside_dir.mkdir()
        outside = outside_dir / "app.py"
        outside.write_bytes(b"Host-only bytes")
        original_replace = os.replace
        swapped = False

        def replace(source, destination, **kwargs):
            nonlocal swapped
            self.assertIn("src_dir_fd", kwargs)
            self.assertIn("dst_dir_fd", kwargs)
            if not swapped and destination == "app.py":
                swapped = True
                original.parent.rename(self.root / "source/pinned")
                original.parent.symlink_to(outside_dir, target_is_directory=True)
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            write_working(self.bound, "source/sub/app.py", b"new")
        self.assertEqual(outside.read_bytes(), b"Host-only bytes")
        self.assertEqual((self.root / "source/pinned/app.py").read_bytes(), b"new")
        self.assert_no_stages()

    def test_final_symlink_swap_cannot_redirect_an_atomic_replace_to_host(self):
        actual = self.put("source/app.py", b"old")
        outside = self.directory / "host.py"
        outside.write_bytes(b"Host-only")
        original_replace = os.replace

        def replace(source, destination, **kwargs):
            if destination == "app.py":
                actual.unlink()
                actual.symlink_to(outside)
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            write_working(self.bound, "source/app.py", b"new")
        self.assertFalse(actual.is_symlink())
        self.assertEqual(actual.read_bytes(), b"new")
        self.assertEqual(outside.read_bytes(), b"Host-only")

    def test_partial_os_writes_are_fully_drained(self):
        original_write = os.write

        def short_write(descriptor, content):
            return original_write(descriptor, content[:3])

        with patch.object(file_io.os, "write", side_effect=short_write):
            write_working(self.bound, "source/app.py", b"all bytes must be written")
        self.assertEqual((self.root / "source/app.py").read_bytes(), b"all bytes must be written")
        self.assert_no_stages()

    def test_staging_write_failure_keeps_original_and_removes_new_parents(self):
        actual = self.put("source/existing.py", b"old")
        with patch.object(file_io.os, "write", side_effect=OSError(errno.EIO, "Host-only diagnostic")):
            self.assert_error("WRITE_FAILED", lambda: write_working(self.bound, "source/existing.py", b"new"))
            self.assert_error("WRITE_FAILED", lambda: write_working(self.bound, "source/new/nested/app.py", b"new"))
        self.assertEqual(actual.read_bytes(), b"old")
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_zero_byte_write_is_not_reported_successful(self):
        with patch.object(file_io.os, "write", return_value=0):
            self.assert_error("WRITE_FAILED", lambda: write_working(self.bound, "source/new/app.py", b"new"))
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_repeated_stage_name_collision_does_not_remove_unrelated_file(self):
        existing = self.put("source/.mcp-write-" + "0" * 32 + ".tmp", b"private existing")
        with patch.object(file_io.secrets, "token_hex", return_value="0" * 32):
            self.assert_error("WRITE_FAILED", lambda: write_working(self.bound, "source/app.py", b"new"))
        self.assertEqual(existing.read_bytes(), b"private existing")
        self.assertFalse((self.root / "source/app.py").exists())

    def test_exclusive_lock_blocks_read_and_write_without_waiting(self):
        self.put("source/app.py")
        with self.hold_root_lock(fcntl.LOCK_EX):
            self.assert_error("WRITE_CONFLICT", lambda: read_working(self.bound, "source/app.py"))
            self.assert_error("WRITE_CONFLICT", lambda: write_working(self.bound, "source/app.py", b"new"))
        write_working(self.bound, "source/app.py", b"new")
        self.assertEqual(read_working(self.bound, "source/app.py"), b"new")

    def test_shared_lock_allows_read_but_blocks_write(self):
        self.put("source/app.py", b"old")
        with self.hold_root_lock(fcntl.LOCK_SH):
            self.assertEqual(read_working(self.bound, "source/app.py"), b"old")
            self.assert_error("WRITE_CONFLICT", lambda: write_working(self.bound, "source/app.py", b"new"))

    def test_real_other_process_lock_is_enforced(self):
        program = (
            "import fcntl,os,sys; "
            "fd=os.open(sys.argv[1],os.O_RDONLY|os.O_DIRECTORY); "
            "fcntl.flock(fd,fcntl.LOCK_EX); "
            "print('locked',flush=True); sys.stdin.read(1); os.close(fd)"
        )
        child = subprocess.Popen(
            [sys.executable, "-I", "-c", program, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            self.assertEqual(child.stdout.readline(), "locked\n")
            self.assert_error("WRITE_CONFLICT", lambda: write_working(self.bound, "source/app.py", b"new"))
            child.stdin.write("x")
            child.stdin.flush()
            self.assertEqual(child.wait(timeout=3), 0)
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
            child.stdin.close()
            child.stdout.close()
        self.assertFalse((self.root / "source/app.py").exists())

    def test_bound_workspace_marker_is_revalidated_for_every_operation(self):
        self.put("source/app.py")
        (self.root / ".workspace.json").write_text("{}")
        for operation in (
            lambda: read_working(self.bound, "source/app.py"),
            lambda: write_working(self.bound, "source/app.py", b"new"),
        ):
            with self.subTest(operation=operation), self.assertRaises(WorkspaceAccessError):
                operation()

    def test_multi_file_add_modify_delete_has_correct_hashes_and_changed_files(self):
        self.put("source/modify.py", b"old")
        self.put("source/delete.py", b"delete")
        result = apply_changes(
            self.bound,
            {"source/modify.py": b"new", "source/add/app.py": b"created", "source/delete.py": None},
            {"source/modify.py": digest(b"old"), "source/add/app.py": None, "source/delete.py": digest(b"delete")},
        )
        self.assertEqual(result["changedFiles"], ["source/modify.py", "source/add/app.py", "source/delete.py"])
        self.assertEqual(result["newHashes"], {"source/modify.py": digest(b"new"), "source/add/app.py": digest(b"created")})
        self.assertEqual((self.root / "source/modify.py").read_bytes(), b"new")
        self.assertEqual((self.root / "source/add/app.py").read_bytes(), b"created")
        self.assertFalse((self.root / "source/delete.py").exists())
        self.assert_no_stages()

    def test_batch_unchanged_files_have_hashes_but_are_not_changed_files(self):
        actual = self.put("source/same.py", b"same")
        before = actual.stat().st_ino
        result = apply_changes(self.bound, {"source/same.py": b"same"}, {"source/same.py": digest(b"same")})
        self.assertEqual(result, {"changedFiles": [], "newHashes": {"source/same.py": digest(b"same")}})
        self.assertEqual(actual.stat().st_ino, before)
        self.assert_no_stages()

    def test_batch_none_expected_hash_requires_absence_not_existing_empty_file(self):
        actual = self.put("source/empty.py", b"")
        self.assert_error("WRITE_CONFLICT", lambda: apply_changes(self.bound, {"source/empty.py": b"new"}, {"source/empty.py": None}))
        self.assertEqual(actual.read_bytes(), b"")
        self.assert_no_stages()

    def test_batch_omitted_expected_hash_disables_cas_for_that_target(self):
        actual = self.put("source/app.py", b"old")
        result = apply_changes(self.bound, {"source/app.py": b"new"}, {})
        self.assertEqual(actual.read_bytes(), b"new")
        self.assertEqual(result["changedFiles"], ["source/app.py"])

    def test_batch_all_hashes_are_checked_before_any_mutation_or_directory_creation(self):
        actual = self.put("source/existing.py", b"old")
        self.assert_error("WRITE_CONFLICT", lambda: apply_changes(
            self.bound, {"source/new/nested/app.py": b"new", "source/existing.py": b"changed"},
            {"source/new/nested/app.py": None, "source/existing.py": "0" * 64},
        ))
        self.assertEqual(actual.read_bytes(), b"old")
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_batch_all_paths_are_checked_before_any_mutation(self):
        actual = self.put("source/existing.py", b"old")
        self.assert_error("PATH_DENIED", lambda: apply_changes(
            self.bound, {"source/existing.py": b"new", "source/.git/config": b"bad"}, {},
        ))
        self.assertEqual(actual.read_bytes(), b"old")
        self.assertFalse((self.root / "source/.git").exists())
        self.assert_no_stages()

    def test_batch_missing_delete_is_an_error_before_any_other_edit(self):
        actual = self.put("source/existing.py", b"old")
        self.assert_error("FILE_NOT_FOUND", lambda: apply_changes(self.bound, {"source/existing.py": b"new", "source/missing.py": None}, {}))
        self.assertEqual(actual.read_bytes(), b"old")
        self.assert_no_stages()

    def test_batch_ancestor_and_descendant_targets_are_denied_without_side_effects(self):
        self.assert_error("PATH_DENIED", lambda: apply_changes(self.bound, {"source/new": b"file", "source/new/leaf.py": b"nested"}, {}))
        self.assertFalse((self.root / "source/new").exists())
        self.assert_no_stages()

    def test_batch_unknown_expected_keys_are_denied(self):
        self.assert_error("PATH_DENIED", lambda: apply_changes(self.bound, {"source/app.py": b"new"}, {"source/other.py": None}))
        self.assertFalse((self.root / "source/app.py").exists())

    def test_batch_count_depth_and_total_content_are_bounded_before_mutation(self):
        cases = (
            ({f"source/{i}.py": b"new" for i in range(MAX_BATCH_FILES + 1)}, "PATH_DENIED"),
            ({"source/" + "nested/" * MAX_DIRECTORY_DEPTH + "app.py": b"new"}, "PATH_DENIED"),
            ({f"source/{i}.py": b"x" * MAX_FILE_BYTES for i in range(MAX_TOTAL_CHANGE_BYTES // MAX_FILE_BYTES + 1)}, "FILE_TOO_LARGE"),
        )
        for changes, code in cases:
            with self.subTest(code=code):
                self.assert_error(code, lambda: apply_changes(self.bound, changes, {}))
        self.assertEqual(list((self.root / "source").iterdir()), [])
        self.assert_no_stages()

    def test_batch_original_backup_memory_is_bounded_before_mutation(self):
        paths = [f"source/{i}.py" for i in range(MAX_TOTAL_CHANGE_BYTES // MAX_FILE_BYTES + 1)]
        for path in paths:
            self.put(path, b"x" * MAX_FILE_BYTES)
        self.assert_error("FILE_TOO_LARGE", lambda: apply_changes(self.bound, {path: b"new" for path in paths}, {}))
        self.assertTrue(all((self.root / path).stat().st_size == MAX_FILE_BYTES for path in paths))
        self.assert_no_stages()

    def test_directory_creation_limit_cleans_every_owned_empty_directory(self):
        changes = {f"source/new{i}/" + "deep/" * 8 + "app.py": b"new" for i in range(10)}
        self.assert_error("PATH_DENIED", lambda: apply_changes(self.bound, changes, {}))
        self.assertEqual(list((self.root / "source").iterdir()), [])
        self.assert_no_stages()

    def test_recoverable_second_replace_failure_rolls_back_first_file(self):
        first = self.put("source/first.py", b"first old", mode=0o750)
        second = self.put("source/second.py", b"second old", mode=0o640)
        original_replace = os.replace
        failed = False

        def replace(source, destination, **kwargs):
            nonlocal failed
            if destination == "second.py" and not failed:
                failed = True
                raise OSError(errno.EIO, "Host-only diagnostic")
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            self.assert_error("PATCH_FAILED", lambda: apply_changes(
                self.bound, {"source/first.py": b"first new", "source/second.py": b"second new"}, {},
            ))
        self.assertEqual(first.read_bytes(), b"first old")
        self.assertEqual(second.read_bytes(), b"second old")
        self.assertEqual(stat.S_IMODE(first.stat().st_mode), 0o750)
        self.assert_no_stages()

    def test_recoverable_replace_failure_restores_deleted_original(self):
        deleted = self.put("source/deleted.py", b"restore me", mode=0o750)
        original_replace = os.replace

        def replace(source, destination, **kwargs):
            if destination == "new.py":
                raise OSError(errno.EIO, "Storage write failed")
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            self.assert_error("PATCH_FAILED", lambda: apply_changes(self.bound, {"source/deleted.py": None, "source/new.py": b"new"}, {}))
        self.assertEqual(deleted.read_bytes(), b"restore me")
        self.assertEqual(stat.S_IMODE(deleted.stat().st_mode), 0o750)
        self.assertFalse((self.root / "source/new.py").exists())
        self.assert_no_stages()

    def test_recoverable_replace_failure_removes_already_created_product_and_parents(self):
        self.put("source/existing.py", b"old")
        original_replace = os.replace

        def replace(source, destination, **kwargs):
            if destination == "existing.py":
                raise OSError(errno.EIO, "Storage write failed")
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            self.assert_error("PATCH_FAILED", lambda: apply_changes(
                self.bound, {"source/new/nested/app.py": b"new", "source/existing.py": b"changed"}, {},
            ))
        self.assertFalse((self.root / "source/new").exists())
        self.assertEqual((self.root / "source/existing.py").read_bytes(), b"old")
        self.assert_no_stages()

    def test_unrecoverable_rollback_preserves_backups_and_never_reports_success(self):
        first = self.put("source/first.py", b"first old")
        second = self.put("source/second.py", b"second old")
        original_replace = os.replace
        calls = 0

        def replace(source, destination, **kwargs):
            nonlocal calls
            calls += 1
            if calls >= 2:
                raise OSError(errno.EIO, "Unrecoverable storage failure")
            return original_replace(source, destination, **kwargs)

        with patch.object(file_io.os, "replace", side_effect=replace):
            self.assert_error("PATCH_FAILED", lambda: apply_changes(self.bound, {"source/first.py": b"first new", "source/second.py": b"second new"}, {}))
        self.assertEqual(first.read_bytes(), b"first new")
        self.assertEqual(second.read_bytes(), b"second old")
        backups = list((self.root / "source").glob(".mcp-write-*.tmp"))
        self.assertIn(b"first old", [backup.read_bytes() for backup in backups])
        self.assertIn(b"second old", [backup.read_bytes() for backup in backups])
        for backup in backups:
            self.assert_error("PATH_DENIED", lambda: read_working(self.bound, "source/" + backup.name))

    def test_io_exception_messages_and_causes_are_not_returned(self):
        with patch.object(file_io.os, "write", side_effect=OSError(errno.EIO, "Host-secret-original-diagnostic")):
            error = self.assert_error("WRITE_FAILED", lambda: write_working(self.bound, "source/app.py", b"new"))
        rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        self.assertNotIn("Host-secret-original-diagnostic", rendered)
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    def test_private_transaction_repr_never_contains_new_or_previous_source_bytes(self):
        change = file_io._Change(
            path="source/app.py", parts=("source", "app.py"),
            content=b"new-source-private-sentinel", previous=b"old-source-private-sentinel",
        )
        rendered = repr(change)
        self.assertNotIn("new-source-private-sentinel", rendered)
        self.assertNotIn("old-source-private-sentinel", rendered)
        self.assertNotIn("content=", rendered)
        self.assertNotIn("previous=", rendered)


if __name__ == "__main__":
    unittest.main()
