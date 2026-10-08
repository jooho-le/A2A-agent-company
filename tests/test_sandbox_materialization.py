"""Temporary Snapshot/Workspace preparation; no generated-code execution."""

from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from orchestrator.artifacts.contracts import StoredContent
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.domain import SCN_001_ID, WorkflowRun
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.materialization import SnapshotMaterializer
from orchestrator.sandbox import materialization
from orchestrator.workspaces.registry import WorkspaceRegistry


def _archive(files=(('requirements.lock', b'fixture==1\n', 0o644), ('src/signup.py', b'# frozen source\n', 0o644)), *, amend=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, content, mode in files:
            info = tarfile.TarInfo(path)
            info.mode = mode
            info.size = len(content)
            info.uid = info.gid = info.mtime = 0
            info.uname = info.gname = ""
            if amend:
                amend(info)
            archive.addfile(info, io.BytesIO(content) if info.isfile() else None)
    return output.getvalue()


class SandboxMaterializationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="a2a-sandbox-material-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "registry.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.base = self.directory / "workspaces"
        self.root = self.base / str(self.run.workspace_id)
        self.record = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id, root_path=str(self.root))
        self.repository.create_run(self.run, (), (), workspace=self.record)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.materializer = SnapshotMaterializer(self.registry)
        self.stored = self.content(_archive())
        self.execution_id = uuid4()
        self.sentinel = self.root / "source" / "user-source.py"
        self.sentinel.write_text("# user's actual Working Copy\n")

    def content(self, value):
        digest = hashlib.sha256(value).hexdigest()
        artifact_id = uuid4()
        metadata = CodeSnapshotArtifact(
            artifact_id=artifact_id, artifact_version=1,
            run_id=self.run.run_id, workflow_step_id=uuid4(), requirement_ids=(uuid4(),),
            code_version=1, repository_id="temporary-demo", commit_hash="a" * 40,
            tree_hash="b" * 40, git_object_format="sha1", snapshot_sha256=digest,
            artifact_uri=f"artifact://{artifact_id}/source.tar", container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash="sha256:" + "e" * 64,
        )
        return StoredContent(metadata=metadata, content=value, media_type="application/x-tar",
                             content_sha256=digest, size_bytes=len(value))

    def prepare(self, stored=None, **kwargs):
        return self.materializer.prepare(self.record, stored or self.stored, self.execution_id, **kwargs)

    def assert_error(self, operation, code=None):
        with self.assertRaises(SandboxError) as raised:
            operation()
        if code:
            self.assertEqual(raised.exception.code, code)
        self.assertNotIn(str(self.directory), str(raised.exception))
        return raised.exception

    def assert_unprepared(self):
        self.assertFalse((self.root / ".sandbox").exists())
        self.assertEqual(self.sentinel.read_text(), "# user's actual Working Copy\n")

    def test_constructor_does_not_create_execution_base(self):
        SnapshotMaterializer(self.registry)
        self.assert_unprepared()

    def test_prepare_creates_private_owned_readonly_snapshot_only(self):
        material = self.prepare()
        self.assertEqual(material.execution_id, self.execution_id)
        self.assertEqual(material.run_id, self.run.run_id)
        self.assertEqual(material.workspace_id, self.run.workspace_id)
        self.assertEqual(material.source_artifact_id, self.stored.artifact_id)
        self.assertEqual(material.snapshot_sha256, self.stored.content_sha256)
        self.assertEqual(material.source_root / "src/signup.py", self.root / ".sandbox" / str(self.execution_id) / "source/src/signup.py")
        self.assertEqual((material.source_root / "src/signup.py").read_bytes(), b"# frozen source\n")
        self.assertEqual(stat.S_IMODE(material.execution_root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(material.source_root.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((material.source_root / "src").stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE((material.source_root / "src/signup.py").stat().st_mode), 0o444)
        self.assertIsNone(material.inputs_root)
        self.assertNotIn(str(self.directory), repr(material))
        self.materializer.validate(material, self.stored)
        self.materializer.cleanup(material)
        self.assertFalse(material.execution_root.exists())
        self.assertEqual(self.sentinel.read_text(), "# user's actual Working Copy\n")

    def test_executable_file_mode_is_readonly_executable(self):
        material = self.prepare(self.content(_archive((('script.py', b'# fixture\n', 0o755),))))
        self.assertEqual(stat.S_IMODE((material.source_root / "script.py").stat().st_mode), 0o555)
        self.materializer.cleanup(material)

    def test_real_git_snapshot_archive_materializes_without_executing_code(self):
        source = self.directory / "git-fixture"
        source.mkdir()
        (source / "requirements.lock").write_bytes(b"fixture==1\n")
        (source / "source.py").write_bytes(b"# fixture, never executed\n")
        for arguments in (("init",), ("add", "requirements.lock", "source.py"), ("commit", "-m", "temporary fixture")):
            subprocess.run(["git", "-c", "user.name=Sandbox Fixture", "-c", "user.email=fixture@example.invalid",
                            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
                           cwd=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, text=True, stdout=subprocess.PIPE, check=True, timeout=20).stdout.strip()
        snapshot = GitSnapshotBuilder(source).build(commit, "requirements.lock")
        stored = self.content(snapshot.archive)
        material = self.prepare(stored)
        self.materializer.validate(material, stored)
        self.assertEqual((material.source_root / "source.py").read_bytes(), b"# fixture, never executed\n")
        self.materializer.cleanup(material)

    def test_pax_long_unicode_paths_are_supported(self):
        name = "문서/" + "회원가입" * 30 + ".py"
        stored = self.content(_archive(((name, b"# unicode\n", 0o644),)))
        material = self.prepare(stored)
        self.assertEqual((material.source_root / name).read_bytes(), b"# unicode\n")
        self.materializer.validate(material, stored)
        self.materializer.cleanup(material)

    def test_host_supplied_inputs_are_separate_and_readonly(self):
        material = self.prepare(inputs={"qa/test_signup.py": b"# independently supplied test\n"})
        self.assertEqual((material.inputs_root / "qa/test_signup.py").read_bytes(), b"# independently supplied test\n")
        self.assertFalse((material.source_root / "qa/test_signup.py").exists())
        self.assertEqual(stat.S_IMODE((material.inputs_root / "qa/test_signup.py").stat().st_mode), 0o444)
        marker = json.loads((material.execution_root / ".execution.json").read_bytes())
        self.assertEqual(marker["runId"], str(self.run.run_id))
        self.assertEqual(marker["executionId"], str(self.execution_id))
        self.assertNotIn(str(self.directory), json.dumps(marker))
        self.materializer.validate(material, self.stored)
        self.materializer.cleanup(material)

    def test_existing_execution_id_is_not_adopted_overwritten_or_removed(self):
        material = self.prepare()
        before = (material.source_root / "src/signup.py").read_bytes()
        self.assert_error(self.prepare, SandboxErrorCode.INVALID)
        self.assertEqual((material.source_root / "src/signup.py").read_bytes(), before)
        self.materializer.cleanup(material)

    def test_invalid_execution_id_does_not_touch_filesystem(self):
        for invalid in (True, None, uuid1(), "../outside", "not-a-uuid"):
            with self.subTest(invalid=invalid):
                self.assert_error(lambda: self.materializer.prepare(self.record, self.stored, invalid), SandboxErrorCode.INVALID)
        self.assert_unprepared()

    def test_stored_hash_size_media_type_and_snapshot_identity_are_verified(self):
        invalid = (
            replace(self.stored, content_sha256="0" * 64),
            replace(self.stored, size_bytes=self.stored.size_bytes + 1),
            replace(self.stored, size_bytes=True),
            replace(self.stored, size_bytes=float(self.stored.size_bytes)),
            replace(self.stored, media_type="application/json"),
            replace(self.stored, content=b"changed"),
            replace(self.stored, content=bytearray(self.stored.content)),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"run_id": uuid4()})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"snapshot_sha256": "0" * 64})),
            replace(self.stored, metadata=self.stored.metadata.model_copy(update={"artifact_uri": "artifact://" + str(uuid4()) + "/source.tar"})),
        )
        for stored in invalid:
            with self.subTest(stored=stored):
                self.assert_error(lambda: self.prepare(stored), SandboxErrorCode.INTEGRITY)
        self.assert_unprepared()

    def test_unregistered_or_root_substituted_workspace_is_rejected(self):
        bad = self.record.model_copy(update={"root_path": str(self.directory / "foreign")})
        self.assert_error(lambda: self.materializer.prepare(bad, self.stored, self.execution_id), SandboxErrorCode.INTEGRITY)
        self.assert_unprepared()

    def test_traversal_absolute_backslash_secret_and_compatibility_paths_rejected(self):
        for name in ("../../escape", "/etc/passwd", "a/../escape", "a\\b.py", ".env", ".ssh/key", ".git/config",
                     "docker.sock", "nested/id_rsa", "Ａ/../b", "．ｅｎｖ", "a//b", "a/./b", "c:/foo", "a\x00b", "/".join(["x"] * 129)):
            with self.subTest(name=name):
                self.assert_error(lambda: self.prepare(self.content(_archive(((name, b"x", 0o644),)))))
                self.assert_unprepared()

    def test_casefold_unicode_duplicate_and_ancestor_conflicts_rejected(self):
        for names in (("a.py", "a.py"), ("A.py", "a.py"), ("dir/A.py", "DIR/b.py"), ("é.py", "e\u0301.py"),
                      ("a", "a/b"), ("a/b", "a"), ("Ａ.py", "A.py")):
            with self.subTest(names=names):
                files = tuple((name, b"x", 0o644) for name in sorted(names, key=lambda item: item.encode("utf-8")))
                self.assert_error(lambda: self.prepare(self.content(_archive(files))), SandboxErrorCode.PATH)
                self.assert_unprepared()

    def test_symlink_hardlink_directory_fifo_device_and_sparse_rejected(self):
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.DIRTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE, tarfile.BLKTYPE, tarfile.GNUTYPE_SPARSE):
            def amend(info):
                info.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    info.linkname = "../../foreign"
            with self.subTest(kind=kind):
                self.assert_error(lambda: self.prepare(self.content(_archive((("file", b"", 0o644),), amend=amend))), SandboxErrorCode.INTEGRITY)
                self.assert_unprepared()

    def test_noncanonical_metadata_order_and_hidden_trailing_archive_rejected(self):
        def mutate(attribute, value):
            return lambda info: setattr(info, attribute, value)
        for attribute, value in (("uid", 1), ("gid", 1), ("mtime", 1), ("uname", "host"), ("mode", 0o777), ("pax_headers", {"comment": "unexpected"})):
            with self.subTest(attribute=attribute):
                self.assert_error(lambda: self.prepare(self.content(_archive(amend=mutate(attribute, value)))), SandboxErrorCode.INTEGRITY)
        for value in (_archive((('z.py', b'x', 0o644), ('a.py', b'x', 0o644))), _archive() + _archive(), b"invalid tar", _archive(())):
            self.assert_error(lambda: self.prepare(self.content(value)), SandboxErrorCode.INTEGRITY)
        self.assert_unprepared()

    def test_file_count_size_total_and_archive_limits_are_enforced(self):
        archives = (
            _archive(tuple((f"file-{index:04}.py", b"", 0o644) for index in range(1001))),
            _archive((("large", b"x" * (1024 * 1024 + 1), 0o644),)),
            _archive(tuple((f"file-{index:02}", b"x" * (1024 * 1024), 0o644) for index in range(17))),
            b"x" * (20 * 1024 * 1024 + 1),
        )
        for value in archives:
            self.assert_error(lambda: self.prepare(self.content(value)), SandboxErrorCode.INTEGRITY)
        self.assert_unprepared()

    def test_input_paths_content_and_count_limits_checked_before_io(self):
        for inputs in ({"../foreign": b"x"}, {".env": b"x"}, {"test.py": "not bytes"}, {"test.py": b"x" * (1024 * 1024 + 1)},
                       {str(index): b"" for index in range(1001)}, {"Test.py": b"a", "test.py": b"b"}, [], "not a mapping"):
            self.assert_error(lambda: self.prepare(inputs=inputs))
        self.assert_unprepared()

    def test_input_aggregate_bytes_and_directory_metadata_limits_checked_before_io(self):
        excessive_bytes = {f"test-{index:02}.py": b"x" * (1024 * 1024) for index in range(17)}
        self.assert_error(lambda: self.prepare(inputs=excessive_bytes), SandboxErrorCode.INVALID)
        # Few zero-byte files must not create unbounded directory metadata.
        excessive_directories = {
            f"group-{index:02}/" + "/".join(["nested"] * 100) + "/test.py": b""
            for index in range(42)
        }
        self.assert_error(lambda: self.prepare(inputs=excessive_directories), SandboxErrorCode.PATH)
        self.assert_unprepared()

    def test_private_base_symlink_and_nonprivate_directory_rejected_without_adoption(self):
        foreign = self.directory / "foreign"
        foreign.mkdir()
        (self.root / ".sandbox").symlink_to(foreign, target_is_directory=True)
        self.assert_error(self.prepare, SandboxErrorCode.PATH)
        self.assertEqual(list(foreign.iterdir()), [])
        (self.root / ".sandbox").unlink()
        (self.root / ".sandbox").mkdir(mode=0o755)
        self.assert_error(self.prepare, SandboxErrorCode.PATH)
        self.assertEqual(list((self.root / ".sandbox").iterdir()), [])

    def test_partial_creation_failure_only_cleans_own_new_execution(self):
        with patch("orchestrator.sandbox.materialization._create_tree", side_effect=OSError("private fixture path")):
            self.assert_error(self.prepare, SandboxErrorCode.PATH)
        self.assertFalse((self.root / ".sandbox" / str(self.execution_id)).exists())
        self.assertEqual(self.sentinel.read_text(), "# user's actual Working Copy\n")

    def test_validate_detects_source_content_and_mode_changes(self):
        material = self.prepare()
        target = material.source_root / "src/signup.py"
        target.chmod(0o644)
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.INTEGRITY)
        target.write_bytes(b"changed source")
        target.chmod(0o444)
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.INTEGRITY)
        target.chmod(0o644)
        target.write_bytes(b"# frozen source\n")
        target.chmod(0o444)
        self.materializer.cleanup(material)

    def test_validate_detects_missing_extra_files_and_extra_empty_directory(self):
        material = self.prepare()
        extra = material.source_root / "extra.py"
        extra.write_bytes(b"foreign")
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.INTEGRITY)
        extra.unlink()
        directory = material.source_root / "empty"
        directory.mkdir(mode=0o755)
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.INTEGRITY)
        directory.rmdir()
        target = material.source_root / "src/signup.py"
        value = target.read_bytes()
        target.unlink()
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.INTEGRITY)
        target.write_bytes(value)
        target.chmod(0o444)
        self.materializer.cleanup(material)

    def test_validate_and_cleanup_refuse_links_without_touching_external_files(self):
        material = self.prepare()
        outside = self.directory / "never-delete.txt"
        outside.write_bytes(b"foreign data")
        target = material.source_root / "src/signup.py"
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(outside)
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.PATH)
        self.assert_error(lambda: self.materializer.cleanup(material), SandboxErrorCode.CLEANUP)
        self.assertEqual(outside.read_bytes(), b"foreign data")
        target.unlink()
        os.link(outside, target)
        self.assert_error(lambda: self.materializer.validate(material, self.stored), SandboxErrorCode.PATH)
        self.assert_error(lambda: self.materializer.cleanup(material), SandboxErrorCode.CLEANUP)
        target.unlink()
        target.write_bytes(original)
        target.chmod(0o444)
        self.materializer.cleanup(material)

    def test_tampered_private_owner_marker_prevents_cleanup(self):
        material = self.prepare()
        marker = material.execution_root / ".execution.json"
        before = marker.read_bytes()
        marker.write_bytes(b"{}")
        self.assert_error(lambda: self.materializer.cleanup(material), SandboxErrorCode.CLEANUP)
        self.assertTrue(material.execution_root.exists())
        marker.write_bytes(before)
        self.materializer.cleanup(material)

    def test_substituted_paths_or_foreign_capability_cannot_trigger_deletion(self):
        material = self.prepare()
        forged = replace(material, execution_root=self.root, source_root=self.root / "source")
        self.assert_error(lambda: self.materializer.cleanup(forged), SandboxErrorCode.INTEGRITY)
        forged = replace(material, run_id=uuid4())
        self.assert_error(lambda: self.materializer.cleanup(forged), SandboxErrorCode.INTEGRITY)
        self.assertEqual(self.sentinel.read_text(), "# user's actual Working Copy\n")
        self.materializer.cleanup(material)

    def test_validate_requires_original_artifact_identity_not_just_same_bytes(self):
        material = self.prepare()
        second = self.content(self.stored.content)
        self.assert_error(lambda: self.materializer.validate(material, second), SandboxErrorCode.INTEGRITY)
        self.materializer.cleanup(material)

    def test_missing_dirfd_removal_support_fails_closed_before_prepare(self):
        supported = os.supports_dir_fd - {os.unlink}
        with patch("orchestrator.sandbox.materialization.os.supports_dir_fd", supported):
            self.assert_error(self.prepare, SandboxErrorCode.UNAVAILABLE)
        self.assert_unprepared()

    def test_cleanup_uses_descriptor_unlink_and_rmdir_not_shutil(self):
        material = self.prepare(inputs={"test.py": b"# approved input\n"})
        # Simulate Python 3.10's lack of shutil.rmtree(dir_fd=...). The library
        # must not invoke shutil at all; actual supported os APIs delete files.
        with patch("shutil.rmtree", side_effect=TypeError("dir_fd is unsupported in Python 3.10")):
            self.materializer.cleanup(material)
        self.assertFalse(material.execution_root.exists())
        self.assertEqual(self.sentinel.read_text(), "# user's actual Working Copy\n")

    def test_execution_directory_inode_substitution_prevents_foreign_removal(self):
        material = self.prepare()
        moved = material.execution_root.with_name("owned-temporary-moved")
        def substitute(_fd):
            material.execution_root.rename(moved)
            material.execution_root.mkdir(mode=0o700)
            (material.execution_root / "foreign.txt").write_bytes(b"do not remove")
        with patch("orchestrator.sandbox.materialization._remove_tree", side_effect=substitute):
            self.assert_error(lambda: self.materializer.cleanup(material), SandboxErrorCode.CLEANUP)
        self.assertEqual((material.execution_root / "foreign.txt").read_bytes(), b"do not remove")
        self.assertTrue((moved / ".execution.json").exists())
        (material.execution_root / "foreign.txt").unlink()
        material.execution_root.rmdir()
        moved.rename(material.execution_root)
        self.materializer.cleanup(material)

    def test_directory_link_swap_during_removal_never_traverses_foreign_tree(self):
        material = self.prepare()
        outside = self.directory / "outside-never-delete"
        outside.mkdir()
        (outside / "foreign.txt").write_bytes(b"foreign")
        moved = material.execution_root / "held-source"
        original_remove = materialization._remove_tree
        original_stat = os.stat
        swapped = False
        def remove_with_race(root_fd, **kwargs):
            def swap_on_stat(name, *args, **options):
                nonlocal swapped
                info = original_stat(name, *args, **options)
                if name == "source" and not swapped:
                    swapped = True
                    material.source_root.rename(moved)
                    material.source_root.symlink_to(outside, target_is_directory=True)
                return info
            with patch("orchestrator.sandbox.materialization.os.stat", side_effect=swap_on_stat):
                original_remove(root_fd, **kwargs)
        with patch("orchestrator.sandbox.materialization._remove_tree", side_effect=remove_with_race):
            self.assert_error(lambda: self.materializer.cleanup(material), SandboxErrorCode.CLEANUP)
        self.assertEqual((outside / "foreign.txt").read_bytes(), b"foreign")
        self.assertTrue((material.execution_root / ".execution.json").exists())
        material.source_root.unlink()
        moved.rename(material.source_root)
        self.materializer.cleanup(material)


if __name__ == "__main__":
    unittest.main()
