"""Step 24 frozen Source reads over real isolated Artifact/Registry fixtures."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import io
import tarfile
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

import test_artifact_service as artifact_fixtures

from mcp_tools.runtime import MCPBinding, MCPConfigurationError
from mcp_tools.tools.snapshots import (
    FrozenSourceSelection, SnapshotReader, SnapshotReadError,
)
from orchestrator.artifacts.service import BoundArtifactStore
from orchestrator.domain.states import AgentRole


def _archive(files=(("requirements.lock", b"fixture==1\n"), ("src/signup.py", b"# frozen\n")), *, amend=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, content in files:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = 0o644
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            if amend is not None:
                amend(member)
            archive.addfile(member, io.BytesIO(content) if member.isfile() else None)
    return output.getvalue()


class MCPFileSnapshotTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing real Git/SQLite/Workspace fixture without
        # inheriting (or rerunning) its unrelated Step 21 test methods.
        self.fixture = artifact_fixtures.ArtifactServiceTests(methodName="runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.snapshot = self.fixture.freeze()
        self.stored = self.fixture.developer.read(self.snapshot.artifact_id)
        self.selection = self.selection_for(self.snapshot)
        self.reader = SnapshotReader(self.fixture.store)
        self.binding = self.binding_for(AgentRole.QA)

    def selection_for(self, snapshot):
        return FrozenSourceSelection(
            project_artifact_id=snapshot.artifact_id,
            snapshot_sha256=snapshot.snapshot_sha256,
        )

    def binding_for(self, role, *, run=None, workspace_id=None):
        run = run or self.fixture.run
        return MCPBinding(
            role=role, agent_role=role, run_id=run.run_id,
            workspace_id=workspace_id or run.workspace_id,
        )

    def assert_error(self, operation, code):
        with self.assertRaises(SnapshotReadError) as raised:
            operation()
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)
        self.assertNotIn(str(self.fixture.directory), repr(raised.exception))
        return raised.exception

    def synthetic_content(self, content):
        digest = hashlib.sha256(content).hexdigest()
        metadata = self.stored.metadata.model_copy(update={"snapshot_sha256": digest})
        return replace(self.stored, metadata=metadata, content=content,
                       content_sha256=digest, size_bytes=len(content))

    def read_synthetic(self, content, path="source/src/signup.py"):
        stored = self.synthetic_content(content)
        selection = self.selection_for(stored.metadata)
        with patch.object(BoundArtifactStore, "read", return_value=stored):
            return self.reader.read(self.binding, selection, path)

    def test_constructor_is_inert_and_repr_contains_no_host_or_selected_identity(self):
        with patch.object(self.fixture.store, "_binding") as binding:
            reader = SnapshotReader(self.fixture.store)
            binding.assert_not_called()
        self.assertEqual(repr(reader), "SnapshotReader()")
        self.assertEqual(repr(self.selection), "FrozenSourceSelection()")

    def test_host_selection_is_uuid4_hash_validated_and_immutable(self):
        values = (uuid1(), True, None, "../../private", "not-a-uuid")
        for value in values:
            with self.subTest(value=value), self.assertRaises(MCPConfigurationError):
                FrozenSourceSelection(project_artifact_id=value, snapshot_sha256="a" * 64)
        for value in (True, None, "a" * 63, "A" * 64, "sha256:" + "a" * 64):
            with self.subTest(value=value), self.assertRaises(MCPConfigurationError):
                FrozenSourceSelection(project_artifact_id=uuid4(), snapshot_sha256=value)
        with self.assertRaises(FrozenInstanceError):
            self.selection.snapshot_sha256 = "0" * 64

    def test_all_source_roles_read_actual_committed_bytes(self):
        expected = self.fixture.source_file.read_bytes()
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                value = self.reader.read(self.binding_for(role), self.selection, "source/src/signup.py")
                self.assertEqual(value, expected)
                self.assertEqual(hashlib.sha256(value).hexdigest(), hashlib.sha256(expected).hexdigest())

    def test_working_copy_changes_and_untracked_files_do_not_change_frozen_reads(self):
        original = self.reader.read(self.binding, self.selection, "source/src/signup.py")
        self.fixture.source_file.write_bytes(b"# mutable Working Copy changed\n")
        (self.fixture.source / "untracked.py").write_bytes(b"# not frozen\n")
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                self.assertEqual(self.reader.read(self.binding_for(role), self.selection, "source/src/signup.py"), original)
                self.assert_error(lambda: self.reader.read(self.binding_for(role), self.selection,
                                                          "source/untracked.py"), "FILE_NOT_FOUND")

    def test_missing_selection_does_not_choose_latest_snapshot_or_working_copy(self):
        with patch.object(self.fixture.store._contents, "latest_source", side_effect=AssertionError("no fallback")):
            for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
                with self.subTest(role=role):
                    self.assert_error(lambda: self.reader.read(self.binding_for(role), None,
                                                              "source/src/signup.py"), "SNAPSHOT_REQUIRED")
        self.assert_error(lambda: self.reader.verify_base(self.binding, None, self.snapshot.snapshot_sha256), "SNAPSHOT_REQUIRED")

    def test_planner_and_wrong_workspace_cannot_read_selected_snapshot(self):
        self.assert_error(lambda: self.reader.read(self.binding_for(AgentRole.PLANNER), self.selection,
                                                  "source/src/signup.py"), "PATH_DENIED")
        other_workspace = self.binding_for(AgentRole.QA, workspace_id=uuid4())
        self.assert_error(lambda: self.reader.read(other_workspace, self.selection,
                                                  "source/src/signup.py"), "PATH_DENIED")

    def test_cross_run_selected_artifact_cannot_be_read(self):
        other_run, _, _ = self.fixture.make_run()
        binding = self.binding_for(AgentRole.QA, run=other_run)
        self.assert_error(lambda: self.reader.read(binding, self.selection, "source/src/signup.py"),
                          "SNAPSHOT_INTEGRITY_ERROR")

    def test_hash_and_missing_artifact_selection_fail_closed(self):
        wrong_hash = replace(self.selection, snapshot_sha256="0" * 64)
        self.assert_error(lambda: self.reader.read(self.binding, wrong_hash, "source/src/signup.py"),
                          "SNAPSHOT_INTEGRITY_ERROR")
        missing = replace(self.selection, project_artifact_id=uuid4())
        self.assert_error(lambda: self.reader.read(self.binding, missing, "source/src/signup.py"),
                          "SNAPSHOT_INTEGRITY_ERROR")

    def test_real_read_grant_is_rechecked_on_every_call(self):
        self.reader.read(self.binding, self.selection, "source/src/signup.py")
        # Deliberate corruption of this temporary DB, not a production mutation.
        with self.fixture.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'",
                               (str(self.snapshot.artifact_id),))
        self.assert_error(lambda: self.reader.read(self.binding, self.selection, "source/src/signup.py"),
                          "SNAPSHOT_INTEGRITY_ERROR")

    def test_real_workspace_owner_marker_is_rechecked_on_every_call(self):
        self.reader.read(self.binding, self.selection, "source/src/signup.py")
        (self.fixture.root / ".workspace.json").write_bytes(b"{}")
        self.assert_error(lambda: self.reader.read(self.binding, self.selection, "source/src/signup.py"), "PATH_DENIED")

    def test_file_not_found_is_distinct_from_an_invalid_selected_archive(self):
        self.assert_error(lambda: self.reader.read(self.binding, self.selection, "source/missing.py"), "FILE_NOT_FOUND")

    def test_source_paths_are_relative_secret_checked_and_source_only(self):
        denied = (
            "source", "source/", "source/../private", "source//src/signup.py", "/source/src/signup.py",
            "source/../../private", "source/.git/config", "source/.env", "source/id_rsa",
            "source/a.key", "source/\\etc/passwd", "source/C:/private", "planning/requirements.json",
            "source/．env", "source/a／b.py", "source/\ud800.py", "source/x\n.py",
        )
        for path in denied:
            with self.subTest(path=repr(path)):
                self.assert_error(lambda: self.reader.read(self.binding, self.selection, path), "PATH_DENIED")

    def test_stored_metadata_bytes_size_media_and_identity_are_reverified(self):
        invalid = (
            replace(self.stored, media_type="application/json"),
            replace(self.stored, content=b"changed"),
            replace(self.stored, content=bytearray(self.stored.content)),
            replace(self.stored, content_sha256="0" * 64),
            replace(self.stored, size_bytes=len(self.stored.content) + 1),
            replace(self.stored, size_bytes=True),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"run_id": uuid4()})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"artifact_id": uuid4()})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"artifact_uri": "artifact://" + str(uuid4()) + "/source.tar"})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"snapshot_sha256": "0" * 64})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"artifact_type": "QA_REPORT"})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"created_by": AgentRole.QA})),
        )
        for stored in invalid:
            with self.subTest(stored=stored), patch.object(BoundArtifactStore, "read", return_value=stored):
                self.assert_error(lambda: self.reader.read(self.binding, self.selection, "source/src/signup.py"),
                                  "SNAPSHOT_INTEGRITY_ERROR")

    def test_full_archive_is_checked_before_returning_a_valid_target(self):
        archives = (
            _archive((("src/signup.py", b"# target\n"), ("../hidden", b"unsafe"))),
            _archive((("src/signup.py", b"# target\n"), ("src/signup.py", b"duplicate"))),
            _archive((("src/signup.py", b"# target\n"), ("src/Signup.py", b"case collision"))),
            _archive((("src/signup.py", b"# target\n"), ("src/．env", b"secret path"))),
        )
        for content in archives:
            with self.subTest(digest=hashlib.sha256(content).hexdigest()):
                self.assert_error(lambda: self.read_synthetic(content), "PATH_DENIED")

    def test_links_devices_sparse_and_unexpected_header_fields_are_rejected(self):
        def amend_field(name, value):
            def amend(member):
                if member.name == "src/signup.py":
                    setattr(member, name, value)
            return amend

        changes = (
            ("type", tarfile.SYMTYPE), ("type", tarfile.LNKTYPE),
            ("type", tarfile.CHRTYPE), ("type", tarfile.BLKTYPE), ("type", tarfile.FIFOTYPE),
            ("type", tarfile.DIRTYPE), ("uid", 1), ("mtime", 1), ("mode", 0o600),
            ("uname", "submitted-secret"), ("pax_headers", {"comment": "submitted-secret"}),
        )
        for name, value in changes:
            with self.subTest(field=name, value=value):
                self.assert_error(lambda: self.read_synthetic(_archive(amend=amend_field(name, value))),
                                  "SNAPSHOT_INTEGRITY_ERROR")

    def test_hidden_trailing_archive_noncanonical_order_and_empty_archive_are_rejected(self):
        archives = (
            _archive() + _archive((("hidden.py", b"# hidden\n"),)),
            _archive((("src/signup.py", b"# target\n"), ("requirements.lock", b"fixture==1\n"))),
            _archive(()),
            _archive() + b"extra-hidden-trailing-data",
        )
        for content in archives:
            with self.subTest(length=len(content)):
                self.assert_error(lambda: self.read_synthetic(content), "SNAPSHOT_INTEGRITY_ERROR")

    def test_original_utf8_crlf_bom_and_long_unicode_pax_paths_are_preserved(self):
        name = "문서/" + "회원가입" * 30 + ".py"
        value = b"\xef\xbb\xbf# " + "원본".encode("utf-8") + b"\r\n"
        content = _archive(((name, value),))
        self.assertEqual(self.read_synthetic(content, "source/" + name), value)

    def test_binary_target_is_preserved_for_file_tool_to_check_encoding(self):
        value = b"\xff\x00"
        self.assertEqual(self.read_synthetic(_archive((("src/signup.py", value),))), value)

    def test_file_and_archive_limits_fail_without_returning_source(self):
        self.assert_error(lambda: self.read_synthetic(_archive((("src/signup.py", b"x" * (1024 * 1024 + 1)),))),
                          "FILE_TOO_LARGE")
        self.assert_error(lambda: self.read_synthetic(b"x" * (20 * 1024 * 1024 + 1)), "FILE_TOO_LARGE")

    def test_file_count_and_total_expanded_bytes_are_bounded(self):
        many = tuple((f"files/{number:04}.py", b"") for number in range(1001))
        self.assert_error(lambda: self.read_synthetic(_archive(many)), "FILE_TOO_LARGE")
        total = tuple((f"files/{number:04}.py", b"x" * (1024 * 1024)) for number in range(17))
        self.assert_error(lambda: self.read_synthetic(_archive(total)), "FILE_TOO_LARGE")

    def test_verify_base_checks_selected_actual_snapshot_and_never_working_copy(self):
        self.assertEqual(self.reader.verify_base(self.binding, self.selection, self.snapshot.snapshot_sha256), self.snapshot)
        self.fixture.source_file.write_bytes(b"# changed Working Copy\n")
        self.assertEqual(self.reader.verify_base(self.binding, self.selection, self.snapshot.snapshot_sha256), self.snapshot)
        self.assert_error(lambda: self.reader.verify_base(self.binding, self.selection, "0" * 64), "BASE_MISMATCH")
        self.assert_error(lambda: self.reader.verify_base(self.binding, None, self.snapshot.snapshot_sha256), "SNAPSHOT_REQUIRED")

    def test_read_base_verifies_archive_once_and_distinguishes_new_file(self):
        with patch.object(BoundArtifactStore, "read", wraps=self.fixture.store.bind(self.fixture.run.run_id,
                                                                                   role=AgentRole.QA).read) as read:
            result = self.reader.read_base(self.binding, self.selection, self.snapshot.snapshot_sha256,
                                           ("source/src/signup.py", "source/new.py"))
            self.assertEqual(read.call_count, 1)
        self.assertEqual(result, {"source/src/signup.py": self.fixture.source_file.read_bytes(), "source/new.py": None})
        self.assert_error(lambda: self.reader.read_base(self.binding, self.selection, "0" * 64,
                                                       ("source/src/signup.py",)), "BASE_MISMATCH")

    def test_read_base_rejects_unbounded_duplicate_or_non_source_paths(self):
        for paths in ([], (), ("source/missing.py",) * 2, ("planning/file.json",), (True,),
                      tuple(f"source/{n}.py" for n in range(1001))):
            with self.subTest(count=len(paths)):
                self.assert_error(lambda: self.reader.read_base(self.binding, self.selection,
                                                               self.snapshot.snapshot_sha256, paths), "PATH_DENIED")

    def test_underlying_exception_body_does_not_enter_error_message(self):
        with patch.object(BoundArtifactStore, "read", side_effect=RuntimeError("raw-source-and-host-path-secret")):
            error = self.assert_error(lambda: self.reader.read(self.binding, self.selection, "source/src/signup.py"),
                                      "SNAPSHOT_INTEGRITY_ERROR")
            self.assertNotIn("raw-source", str(error))


if __name__ == "__main__":
    unittest.main()
