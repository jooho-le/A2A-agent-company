"""Role paths and OS file-boundary tests inside owned temporary Workspaces."""

from contextlib import contextmanager
import os
from pathlib import Path
import socket
import stat
import tempfile
import traceback
import unittest
from unittest.mock import patch

from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import (
    WORKSPACE_PERMISSIONS,
    WorkspaceAccess,
    WorkspaceAccessError,
    WorkspaceErrorCode,
    authorize_path,
    relative_parts,
)
from orchestrator.workspaces.registry import WorkspaceRegistry
from orchestrator.workspaces import filesystem


class WorkspacePathPolicyTests(unittest.TestCase):
    def test_relative_path_is_validated_before_normalization(self):
        invalid = (
            "", " ", "/source/file.py", "../source/file.py", "source/../file.py",
            "source/./file.py", "source//file.py", "source/file.py/",
            "source\\file.py", "C:/source/file.py", "\\\\server\\share\\file.py",
            "//server/share/file.py", "source/drive:stream", "source/a\x00b",
            "source/a\nb", "source/a\x7fb", "source/" + "a" * 4096,
            "file://source/file.py", "registry://source/file.py", "https://example.test/file.py",
        )
        for path in invalid:
            with self.subTest(path=path), self.assertRaises(WorkspaceAccessError) as raised:
                relative_parts(path)
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.PATH_DENIED)

    def test_secret_paths_are_denied_in_every_region_and_case(self):
        for suffix in (
            ".env", ".env.local", ".ENV.production", ".ssh/id_rsa", ".aws/config",
            ".git/config", "credentials", "credentials.json", "secrets/key",
            "private.pem", "PRIVATE.KEY", "identity.p12", "identity.pfx", ".netrc",
            ".workspace.json", "docker.sock",
        ):
            path = "source/" + suffix
            with self.subTest(path=path), self.assertRaises(WorkspaceAccessError):
                relative_parts(path)

    def test_valid_relative_path_preserves_unicode_and_spaces_exactly(self):
        self.assertEqual(relative_parts("source/한글 디렉터리/ 가입.py"), ("source", "한글 디렉터리", " 가입.py"))

    def test_directory_grants_use_component_boundaries(self):
        for path in ("sourceevil/file.py", "outputs/qaequiv/file.py", "snapshots-copy/file.py"):
            for role in AgentRole:
                with self.subTest(role=role, path=path), self.assertRaises(WorkspaceAccessError):
                    authorize_path(role, path, WorkspaceAccess.READ)

    def test_all_snapshot_writes_and_other_role_writes_are_denied(self):
        for role in AgentRole:
            for path in ("snapshots/source.py", "snapshots/nested/source.py"):
                with self.subTest(role=role, path=path), self.assertRaises(WorkspaceAccessError):
                    authorize_path(role, path, WorkspaceAccess.WRITE)
        own_path = {
            AgentRole.PLANNER: "planning/plan.json", AgentRole.DEVELOPER: "source/app.py",
            AgentRole.QA: "outputs/qa/report.json", AgentRole.SECURITY: "outputs/security/report.json",
        }
        for role in AgentRole:
            self.assertIsNotNone(authorize_path(role, own_path[role], WorkspaceAccess.WRITE))
            for other_role, path in own_path.items():
                if role is not other_role:
                    with self.subTest(role=role, other_role=other_role), self.assertRaises(WorkspaceAccessError):
                        authorize_path(role, path, WorkspaceAccess.WRITE)


@unittest.skipUnless(os.name == "posix", "File descriptor Workspace boundary requires POSIX")
class WorkspaceFileAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-ws-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base_root = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "access.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base_root / str(self.run.workspace_id)
        self.repository.create_run(self.run, (), (), workspace=WorkspaceRecord(
            workspace_id=self.run.workspace_id, run_id=self.run.run_id, root_path=str(self.root),
        ))
        self.registry = WorkspaceRegistry(repository=self.repository, base_root=self.base_root)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.bound = self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=AgentRole.DEVELOPER)

    def bind(self, role):
        return self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=role)

    def assert_denied(self, operation):
        with self.assertRaises(WorkspaceAccessError):
            with operation():
                self.fail("Unsafe file access succeeded")

    def test_role_read_matrix_matches_public_policy(self):
        paths = ("planning/file.txt", "source/file.txt", "snapshots/file.txt", "outputs/qa/file.txt", "outputs/security/file.txt")
        for path in paths:
            (self.root / path).write_text(path)
        for role in AgentRole:
            bound = self.bind(role)
            for path in paths:
                allowed = any(path.startswith(prefix) for prefix in WORKSPACE_PERMISSIONS[role].read)
                with self.subTest(role=role, path=path):
                    if allowed:
                        with bound.open_read(path) as descriptor:
                            self.assertIs(type(descriptor), int)
                            self.assertEqual(os.read(descriptor, 1000).decode(), path)
                    else:
                        self.assert_denied(lambda: bound.open_read(path))

    def test_role_write_matrix_cannot_cross_owned_regions(self):
        paths = ("planning/new.txt", "source/new.txt", "snapshots/new.txt", "outputs/qa/new.txt", "outputs/security/new.txt")
        for role in AgentRole:
            bound = self.bind(role)
            for path in paths:
                allowed = any(path.startswith(prefix) for prefix in WORKSPACE_PERMISSIONS[role].write)
                with self.subTest(role=role, path=path):
                    if allowed:
                        with bound.open_write(path, create=True) as descriptor:
                            os.write(descriptor, role.value.encode())
                        self.assertEqual((self.root / path).read_text(), role.value)
                    else:
                        self.assert_denied(lambda: bound.open_write(path, create=True))
        self.assertFalse((self.root / "snapshots/new.txt").exists())

    def test_write_open_does_not_truncate_before_explicit_host_write(self):
        path = self.root / "source" / "existing.py"
        path.write_bytes(b"abcdefgh")
        with self.bound.open_write("source/existing.py") as descriptor:
            self.assertIs(type(descriptor), int)
            self.assertEqual(path.read_bytes(), b"abcdefgh")
            self.assertTrue(stat.S_ISREG(os.fstat(descriptor).st_mode))
            os.write(descriptor, b"XY")
        self.assertEqual(path.read_bytes(), b"XYcdefgh")

    def test_missing_file_is_only_created_when_explicitly_requested(self):
        self.assert_denied(lambda: self.bound.open_write("source/not-created.py"))
        self.assertFalse((self.root / "source/not-created.py").exists())
        with self.bound.open_write("source/created.py", create=True) as descriptor:
            os.write(descriptor, b"# created\n")
        self.assertEqual((self.root / "source/created.py").read_bytes(), b"# created\n")

    def test_missing_product_file_is_not_misclassified_as_unprovisioned_workspace(self):
        for open_file in (self.bound.open_read, self.bound.open_write):
            for path in ("source/missing.py", "source/missing-parent/file.py"):
                with self.subTest(operation=open_file, path=path), self.assertRaises(WorkspaceAccessError) as raised:
                    with open_file(path):
                        self.fail("Missing product file opened")
                self.assertEqual(raised.exception.code, WorkspaceErrorCode.FILE_NOT_FOUND)

    def test_read_and_write_descriptors_close_on_context_exit(self):
        (self.root / "source/file.py").write_text("# Source\n")
        for open_file in (self.bound.open_read, self.bound.open_write):
            with open_file("source/file.py") as descriptor:
                os.fstat(descriptor)
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_ensure_parent_creates_only_authorized_parent_directories(self):
        self.bound.ensure_parent("source/generated/nested/app.py")
        self.assertTrue((self.root / "source/generated/nested").is_dir())
        self.assertFalse((self.root / "source/generated/nested/app.py").exists())
        with self.bound.open_write("source/generated/nested/app.py", create=True) as descriptor:
            os.write(descriptor, b"# Source\n")
        for path in ("snapshots/generated/file.py", "planning/generated/file.json", "outputs/qa/generated/test.py", "source/secrets/generated/file.py"):
            with self.subTest(path=path), self.assertRaises(WorkspaceAccessError):
                self.bound.ensure_parent(path)
            self.assertFalse((self.root / path).parent.exists())

    def test_host_path_traversal_uri_and_secret_rejection_has_no_filesystem_side_effect(self):
        outside = self.directory / "outside.txt"
        for path in (str(outside), "source/../../outside.txt", "file://" + str(outside), "source/.env.generated", "source/.git/config"):
            with self.subTest(path=path):
                self.assert_denied(lambda: self.bound.open_write(path, create=True))
        self.assertFalse(outside.exists())
        self.assertFalse((self.root / "source/.env.generated").exists())
        self.assertFalse((self.root / "source/.git").exists())

    def test_write_rejects_symlink_leaf_even_when_target_is_in_source(self):
        target = self.root / "source/target.py"
        target.write_text("Original Source")
        (self.root / "source/link.py").symlink_to("target.py")
        self.assert_denied(lambda: self.bound.open_write("source/link.py"))
        self.assertEqual(target.read_text(), "Original Source")

    def test_write_and_parent_creation_reject_symlink_ancestors(self):
        actual = self.root / "source/actual"
        actual.mkdir()
        (self.root / "source/linked").symlink_to("actual", target_is_directory=True)
        self.assert_denied(lambda: self.bound.open_write("source/linked/new.py", create=True))
        with self.assertRaises(WorkspaceAccessError):
            self.bound.ensure_parent("source/linked/nested/file.py")
        self.assertFalse((actual / "new.py").exists())
        self.assertFalse((actual / "nested").exists())

    def test_read_may_follow_internal_authorized_symlink_without_changing_source(self):
        target = self.root / "source/target.py"
        target.write_text("Authorized Source")
        (self.root / "source/link.py").symlink_to("target.py")
        with self.bound.open_read("source/link.py") as descriptor:
            self.assertEqual(os.read(descriptor, 1000).decode(), "Authorized Source")
        self.assertEqual(target.read_text(), "Authorized Source")

    def test_read_symlink_target_is_checked_against_secret_and_role_grants(self):
        (self.root / "source/.env").write_text("test-only-env-secret")
        (self.root / "source/private.txt").symlink_to(".env")
        self.assert_denied(lambda: self.bound.open_read("source/private.txt"))
        (self.root / "source/app.py").write_text("Product Source")
        (self.root / "planning/link.py").symlink_to("../source/app.py")
        planner = self.bind(AgentRole.PLANNER)
        self.assert_denied(lambda: planner.open_read("planning/link.py"))

    def test_symlink_to_host_file_or_another_workspace_is_rejected(self):
        outside = self.directory / "outside.txt"
        outside.write_text("test-only-outside-secret")
        (self.root / "source/outside.py").symlink_to(outside)
        self.assert_denied(lambda: self.bound.open_read("source/outside.py"))
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="별도 작업")
        other_root = self.base_root / str(other.workspace_id)
        self.repository.create_run(other, (), (), workspace=WorkspaceRecord(workspace_id=other.workspace_id, run_id=other.run_id, root_path=str(other_root)))
        self.registry.provision(other.workspace_id, run_id=other.run_id)
        (other_root / "source/app.py").write_text("Other Run Source")
        (self.root / "source/other.py").symlink_to(other_root / "source/app.py")
        self.assert_denied(lambda: self.bound.open_read("source/other.py"))

    def test_hardlinked_files_cannot_be_read_or_written(self):
        target = self.root / "source/original.py"
        target.write_text("Shared inode Source")
        os.link(target, self.root / "source/hardlink.py")
        for path in ("source/original.py", "source/hardlink.py"):
            with self.subTest(path=path):
                self.assert_denied(lambda: self.bound.open_read(path))
                self.assert_denied(lambda: self.bound.open_write(path))
        self.assertEqual(target.read_text(), "Shared inode Source")

    def test_read_leaf_swap_to_external_symlink_is_rejected_after_resolution(self):
        self.assert_swap_race(self.bound.open_read)

    def test_write_leaf_swap_to_external_symlink_is_rejected_before_open(self):
        self.assert_swap_race(self.bound.open_write)

    def assert_swap_race(self, open_file):
        target = self.root / "source/race.py"
        target.write_text("Original authorized Source")
        outside = self.directory / "outside-race.txt"
        outside.write_text("External file must not be touched")
        original_open = filesystem.open_regular_file
        swaps = []
        @contextmanager
        def swap_before_open(root_fd, parts, flags, **kwargs):
            if parts == ("source", "race.py") and not swaps:
                swaps.append(True)
                target.rename(self.root / "source/race-original.py")
                target.symlink_to(outside)
            with original_open(root_fd, parts, flags, **kwargs) as descriptor:
                yield descriptor
        with patch.object(filesystem, "open_regular_file", swap_before_open):
            self.assert_denied(lambda: open_file("source/race.py"))
        self.assertEqual(swaps, [True])
        self.assertEqual(outside.read_text(), "External file must not be touched")
        self.assertEqual((self.root / "source/race-original.py").read_text(), "Original authorized Source")

    def test_directory_fifo_and_socket_are_rejected_as_nonregular_files(self):
        directory = self.root / "source/subdirectory"
        directory.mkdir()
        os.mkfifo(self.root / "source/pipe")
        endpoint = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(endpoint.close)
        endpoint.bind(str(self.root / "source/sock"))
        for path in ("source/subdirectory", "source/pipe", "source/sock"):
            with self.subTest(path=path):
                self.assert_denied(lambda: self.bound.open_read(path))
                self.assert_denied(lambda: self.bound.open_write(path))

    def test_missing_file_error_does_not_disclose_path_or_nested_os_error(self):
        sentinel = "source/test-only-private-missing.py"
        try:
            with self.bound.open_read(sentinel):
                self.fail("Nonexistent file was opened")
        except WorkspaceAccessError as error:
            self.assertEqual(error.code, WorkspaceErrorCode.FILE_NOT_FOUND)
            formatted = "".join(traceback.format_exception(error))
            self.assertNotIn("test-only-private-missing", formatted)
            self.assertNotIn(str(self.root), formatted)


if __name__ == "__main__":
    unittest.main()
