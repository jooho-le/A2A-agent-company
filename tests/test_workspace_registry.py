"""Persistent Workspace identity and provisioning; no Agent/cloud execution."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from uuid import uuid1, uuid4

from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import (
    OWNER_MARKER,
    WORKSPACE_LAYOUT,
    WorkspaceAccessError,
    WorkspaceErrorCode,
)
from orchestrator.workspaces.registry import WorkspaceRegistry


class WorkspaceRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-ws-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.base_root = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "registry.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base_root / str(self.run.workspace_id)
        self.record = WorkspaceRecord(
            workspace_id=self.run.workspace_id, run_id=self.run.run_id,
            root_path=str(self.root),
        )
        self.repository.create_run(self.run, (), (), workspace=self.record)
        self.registry = WorkspaceRegistry(repository=self.repository, base_root=self.base_root)

    def provision(self):
        return self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)

    def bind(self, role=AgentRole.DEVELOPER):
        return self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=role)

    def test_constructor_and_record_lookup_do_not_create_workspace_directories(self):
        self.assertFalse(self.base_root.exists())
        self.assertEqual(self.registry.get_record(self.run.workspace_id, run_id=self.run.run_id), self.record)
        self.assertFalse(self.base_root.exists())

    def test_broad_host_base_roots_are_rejected_without_side_effects(self):
        for base in ("/", Path.home(), Path.cwd(), "", " "):
            with self.subTest(base=base), self.assertRaises(WorkspaceAccessError) as raised:
                WorkspaceRegistry(repository=self.repository, base_root=base)
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.ROOT)
        self.assertFalse(self.base_root.exists())

    def test_unregistered_id_is_not_adopted_or_created(self):
        unknown = uuid4()
        for operation in (
            lambda: self.registry.get_record(unknown, run_id=self.run.run_id),
            lambda: self.registry.provision(unknown, run_id=self.run.run_id),
            lambda: self.registry.bind(unknown, run_id=self.run.run_id, role=AgentRole.DEVELOPER),
        ):
            with self.subTest(operation=operation), self.assertRaises(WorkspaceAccessError) as raised:
                operation()
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.NOT_FOUND)
        self.assertFalse((self.base_root / str(unknown)).exists())

    def test_invalid_non_uuid4_workspace_and_run_id_are_rejected(self):
        for invalid in ("not-a-uuid", str(uuid1()), True, None, "../../test-only-secret-root"):
            with self.subTest(invalid=invalid), self.assertRaises(WorkspaceAccessError) as raised:
                self.registry.get_record(invalid, run_id=self.run.run_id)
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.IDENTITY)
        with self.assertRaises(WorkspaceAccessError):
            self.registry.get_record(self.run.workspace_id, run_id="not-a-uuid")
        self.assertFalse(self.base_root.exists())

    def test_workspace_cannot_be_bound_or_provisioned_for_another_run(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 Run")
        self.repository.create_run(other, (), (), workspace=WorkspaceRecord(
            workspace_id=other.workspace_id, run_id=other.run_id,
            root_path=str(self.base_root / str(other.workspace_id)),
        ))
        for operation in (
            lambda: self.registry.get_record(self.run.workspace_id, run_id=other.run_id),
            lambda: self.registry.provision(self.run.workspace_id, run_id=other.run_id),
            lambda: self.registry.bind(self.run.workspace_id, run_id=other.run_id, role=AgentRole.QA),
        ):
            with self.subTest(operation=operation), self.assertRaises(WorkspaceAccessError) as raised:
                operation()
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.IDENTITY)
        self.assertFalse(self.base_root.exists())

    def test_bind_requires_provisioning(self):
        with self.assertRaises(WorkspaceAccessError) as raised:
            self.bind()
        self.assertEqual(raised.exception.code, WorkspaceErrorCode.NOT_PROVISIONED)
        self.assertFalse(self.root.exists())

    def test_provision_creates_owned_layout_and_can_be_bound_for_each_role(self):
        self.provision()
        for relative in WORKSPACE_LAYOUT:
            with self.subTest(relative=relative):
                self.assertTrue((self.root / relative).is_dir())
        marker = self.root / OWNER_MARKER
        self.assertTrue(marker.is_file())
        marker_json = json.loads(marker.read_text())
        self.assertIn(str(self.run.workspace_id), json.dumps(marker_json))
        self.assertIn(str(self.run.run_id), json.dumps(marker_json))
        for role in AgentRole:
            self.assertIsNotNone(self.bind(role))

    def test_reprovision_does_not_erase_existing_source_or_outputs(self):
        self.provision()
        source = self.root / "source" / "signup.py"
        report = self.root / "outputs" / "qa" / "report.json"
        source.write_text("# existing user Source\n")
        report.write_text('{"existing":"report"}')
        marker_before = (self.root / OWNER_MARKER).read_bytes()
        self.provision()
        self.assertEqual(source.read_text(), "# existing user Source\n")
        self.assertEqual(report.read_text(), '{"existing":"report"}')
        self.assertEqual((self.root / OWNER_MARKER).read_bytes(), marker_before)

    def test_nonempty_unmarked_directory_is_rejected_without_erasing_data(self):
        self.root.mkdir(parents=True)
        sentinel = self.root / "user-source.txt"
        sentinel.write_text("Do not remove user data")
        with self.assertRaises(WorkspaceAccessError) as raised:
            self.provision()
        self.assertEqual(raised.exception.code, WorkspaceErrorCode.CONFLICT)
        self.assertEqual(sentinel.read_text(), "Do not remove user data")
        self.assertFalse((self.root / OWNER_MARKER).exists())

    def test_registry_survives_repository_and_manager_restart(self):
        self.provision()
        reopened = SQLiteWorkflowRepository(self.repository.database_path)
        registry = WorkspaceRegistry(repository=reopened, base_root=self.base_root)
        self.assertEqual(registry.get_record(str(self.run.workspace_id), run_id=str(self.run.run_id)), self.record)
        self.assertIsNotNone(registry.bind(str(self.run.workspace_id), run_id=str(self.run.run_id), role=AgentRole.SECURITY))

    def test_database_root_outside_configured_base_is_not_opened_or_created(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 작업")
        outside = self.directory / "untrusted-root" / str(other.workspace_id)
        record = WorkspaceRecord(workspace_id=other.workspace_id, run_id=other.run_id, root_path=str(outside))
        self.repository.create_run(other, (), (), workspace=record)
        for operation in (
            lambda: self.registry.get_record(other.workspace_id, run_id=other.run_id),
            lambda: self.registry.provision(other.workspace_id, run_id=other.run_id),
        ):
            with self.subTest(operation=operation), self.assertRaises(WorkspaceAccessError) as raised:
                operation()
            self.assertEqual(raised.exception.code, WorkspaceErrorCode.ROOT)
        self.assertFalse(outside.exists())

    def test_database_root_must_be_the_exact_workspace_id_child(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 작업")
        wrong_child = self.base_root / "wrong-child"
        record = WorkspaceRecord(workspace_id=other.workspace_id, run_id=other.run_id, root_path=str(wrong_child))
        self.repository.create_run(other, (), (), workspace=record)
        with self.assertRaises(WorkspaceAccessError) as raised:
            self.registry.provision(other.workspace_id, run_id=other.run_id)
        self.assertEqual(raised.exception.code, WorkspaceErrorCode.ROOT)
        self.assertFalse(wrong_child.exists())

    def test_concurrent_provision_serializes_and_preserves_existing_user_files(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.provision(), range(8)))
        source = self.root / "source" / "user.py"
        source.write_text("# Preserve existing Source\n")
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _: self.provision(), range(8)))
        self.assertEqual(source.read_text(), "# Preserve existing Source\n")
        self.assertIsNotNone(self.bind())

    def test_marker_removal_after_binding_prevents_future_access(self):
        self.provision()
        bound = self.bind()
        source = self.root / "source" / "existing.py"
        source.write_text("# Source\n")
        (self.root / OWNER_MARKER).unlink()
        with self.assertRaises(WorkspaceAccessError):
            with bound.open_read("source/existing.py"):
                self.fail("Missing ownership marker permitted access")

    def test_marker_tampering_and_layout_replacement_are_rejected(self):
        self.provision()
        marker = self.root / OWNER_MARKER
        original = marker.read_bytes()
        marker.write_text('{"workspaceId":"not-owner"}')
        with self.assertRaises(WorkspaceAccessError):
            self.bind()
        marker.write_bytes(original)
        (self.root / "planning").rmdir()
        (self.root / "planning").write_text("Not a directory")
        with self.assertRaises(WorkspaceAccessError):
            self.bind()

    def test_marker_duplicate_keys_nonfinite_version_and_boolean_version_are_rejected(self):
        self.provision()
        marker = self.root / OWNER_MARKER
        original = marker.read_bytes()
        workspace_id, run_id = str(self.run.workspace_id), str(self.run.run_id)
        invalid_markers = (
            '{"workspaceId":"' + workspace_id + '","workspaceId":"' + workspace_id + '","runId":"' + run_id + '","layoutVersion":1}',
            json.dumps({"workspaceId": workspace_id, "runId": run_id, "layoutVersion": True}),
            '{"workspaceId":"' + workspace_id + '","runId":"' + run_id + '","layoutVersion":NaN}',
            json.dumps({"workspaceId": workspace_id, "runId": run_id, "layoutVersion": 1, "untrusted": "test-only-marker-secret"}),
        )
        source = self.root / "source/existing.py"
        source.write_text("# Existing Source\n")
        for invalid in invalid_markers:
            marker.write_text(invalid)
            for operation in (self.bind, self.provision):
                with self.subTest(marker=invalid, operation=operation), self.assertRaises(WorkspaceAccessError) as raised:
                    operation()
                self.assertEqual(raised.exception.code, WorkspaceErrorCode.CONFLICT)
                self.assertNotIn("test-only-marker-secret", str(raised.exception))
            self.assertEqual(source.read_text(), "# Existing Source\n")
        marker.write_bytes(original)
        self.assertIsNotNone(self.bind())

    def test_ownership_marker_must_remain_private(self):
        self.provision()
        marker = self.root / OWNER_MARKER
        marker.chmod(0o644)
        with self.assertRaises(WorkspaceAccessError) as raised:
            self.bind()
        self.assertEqual(raised.exception.code, WorkspaceErrorCode.CONFLICT)

    def test_bound_workspace_repr_and_errors_do_not_expose_host_paths(self):
        self.provision()
        bound = self.bind()
        self.assertNotIn(str(self.root), repr(bound))
        self.assertNotIn(str(self.base_root), repr(bound))
        rejected = str(self.directory / "test-only-secret-file")
        try:
            with bound.open_read(rejected):
                self.fail("Host absolute path was accepted")
        except WorkspaceAccessError as error:
            trace = "".join(traceback.format_exception(error))
            self.assertNotIn("test-only-secret-file", trace)
            self.assertNotIn(str(self.root), trace)


if __name__ == "__main__":
    unittest.main()
