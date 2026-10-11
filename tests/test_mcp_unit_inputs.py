"""True temporary Workspace test capture; product/test code is never run here."""

from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import fcntl
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from mcp_tools.runtime import MCPBinding, MCPExecutionContext
from mcp_tools.tools import unit_inputs
from mcp_tools.tools.unit_config import UnitTestScope
from mcp_tools.tools.unit_inputs import UnitTestInputs, UnitTestInputsError, prepare_unit_inputs
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


RUNNER = b"# trusted runner\nimport unittest\n"


def scope(**changes):
    values = dict(name="qa", kind="QA_TESTS")
    values.update(changes)
    return UnitTestScope(**values)


def digest(content):
    return hashlib.sha256(content).hexdigest()


def files_digest(files):
    manifest = [{"path": path, "sha256": digest(content), "sizeBytes": len(content)} for path, content in sorted(files.items())]
    return digest(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode())


class UnitTestInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-unit-inputs-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base / str(self.run.workspace_id)
        record = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id, root_path=str(self.root))
        self.repository.create_run(self.run, (), (), workspace=record)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.context = self.bind(AgentRole.QA)

    def bind(self, role):
        return MCPExecutionContext(binding=MCPBinding(role=role, agent_role=role, run_id=self.run.run_id,
            workspace_id=self.run.workspace_id), workspace=self.registry.bind(self.run.workspace_id, run_id=self.run.run_id, role=role))

    def put(self, path, content=b"# test fixture\n"):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def prepare(self, selected=None, context=None, runner=RUNNER):
        return prepare_unit_inputs(context or self.context, selected or scope(), runner)

    def assert_error(self, code, operation):
        with self.assertRaises(UnitTestInputsError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.args, (code,))
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_nested_qa_inputs_are_copied_frozen_and_exact_bytes(self):
        code = "# 한글\r\npassword = request.password\r\n".encode()
        first = self.put("outputs/qa/tests/test_auth.py", code)
        self.put("outputs/qa/tests/package/__init__.py", b"")
        self.put("outputs/qa/tests/package/test_other.py", b"# nested\n")
        captured = self.prepare()
        self.assertEqual(captured.files, {"_unit_runner.py": RUNNER, "tests/test_auth.py": code,
            "tests/package/__init__.py": b"", "tests/package/test_other.py": b"# nested\n"})
        first.write_bytes(b"# changed afterward\n")
        self.assertEqual(captured.files["tests/test_auth.py"], code)
        with self.assertRaises(TypeError):
            captured.files["tests/test_auth.py"] = b"changed"
        with self.assertRaises(FrozenInstanceError):
            captured.inputs_sha256 = "a" * 64
        self.assertNotIn("request.password", repr(captured))

    def test_input_hashes_are_deterministic_file_manifests_not_source_hashes(self):
        self.put("outputs/qa/tests/test_z.py", b"# z\n")
        self.put("outputs/qa/tests/test_a.py", "# 한글\n".encode())
        captured = self.prepare()
        tests = {path: content for path, content in captured.files.items() if path != "_unit_runner.py"}
        self.assertEqual(captured.runner_sha256, digest(RUNNER))
        self.assertEqual(captured.inputs_sha256, files_digest(captured.files))
        self.assertEqual(captured.test_files_sha256, files_digest(tests))
        self.assertEqual(self.prepare(), captured)

    def test_source_scope_never_reads_mutable_source_or_scratch_tests(self):
        self.put("source/tests/test_auth.py", b"# mutable\n")
        context = self.bind(AgentRole.DEVELOPER)
        with patch("os.open", side_effect=AssertionError("no filesystem reads")):
            captured = self.prepare(scope(name="self", kind="SNAPSHOT"), context)
        self.assertEqual(captured.files, {"_unit_runner.py": RUNNER})
        self.assertEqual(captured.test_files_sha256, files_digest({}))

    def test_protected_scope_uses_host_bytes_never_workspace_tests(self):
        selected = scope(name="fixed", kind="PROTECTED", protected_files={"tests/test_auth.py": "# protected\n"},
            protected_suite_ref="artifact://protected/tests")
        self.put("outputs/qa/tests/test_auth.py", b"# hostile mutable replacement\n")
        with patch("os.open", side_effect=AssertionError("no filesystem reads")):
            captured = self.prepare(selected)
        self.assertEqual(captured.files["tests/test_auth.py"], b"# protected\n")

    def test_role_separation(self):
        fixed = scope(kind="PROTECTED", protected_files={"tests/test.py": "pass"}, protected_suite_ref="artifact://protected/tests")
        for role in AgentRole:
            if role is not AgentRole.QA:
                for selected in (scope(), fixed):
                    self.assert_error("PERMISSION_DENIED", lambda: self.prepare(selected, self.bind(role)))
            if role is not AgentRole.DEVELOPER:
                self.assert_error("PERMISSION_DENIED", lambda: self.prepare(scope(kind="SNAPSHOT"), self.bind(role)))

    def test_missing_qa_tree_does_not_create_it(self):
        self.assert_error("FILE_NOT_FOUND", self.prepare)
        self.assertFalse((self.root / "outputs/qa/tests").exists())

    def test_empty_tree_is_valid_input_and_zero_tests_is_runner_decision(self):
        (self.root / "outputs/qa/tests").mkdir()
        self.assertEqual(self.prepare().files, {"_unit_runner.py": RUNNER})

    def test_symlink_leaf_cannot_read_source_host_or_another_test(self):
        self.put("source/private.py", b"# mutable Source\n")
        target = self.put("outputs/qa/tests/test_original.py")
        link = target.parent / "test_link.py"
        for destination in ("../../../source/private.py", str(self.directory / "outside.py"), "test_original.py"):
            link.symlink_to(destination)
            self.assert_error("PATH_DENIED", self.prepare)
            link.unlink()

    def test_symlink_parent_cannot_alias_source(self):
        self.put("source/tests/test_mutable.py")
        (self.root / "outputs/qa/tests").symlink_to("../../source/tests", target_is_directory=True)
        self.assert_error("PATH_DENIED", self.prepare)

    def test_symlink_nested_directory_denied_even_internal(self):
        self.put("outputs/qa/tests/actual/test.py")
        (self.root / "outputs/qa/tests/link").symlink_to("actual", target_is_directory=True)
        self.assert_error("PATH_DENIED", self.prepare)

    def test_hardlinks_and_fifo_nodes_denied_without_blocking(self):
        target = self.put("outputs/qa/tests/test.py")
        linked = target.parent / "test_linked.py"
        os.link(target, linked)
        self.assert_error("PATH_DENIED", self.prepare)
        linked.unlink()
        fifo = target.parent / "fifo.py"
        os.mkfifo(fifo)
        self.assert_error("PATH_DENIED", self.prepare)

    def test_secret_names_and_private_staging_denied(self):
        for name in (".env", ".git", ".mcp-write-private", ".MCP-WRITE-other", "id_rsa", "fixture.key"):
            target = self.put("outputs/qa/tests/" + name)
            self.assert_error("PATH_DENIED", self.prepare)
            target.unlink()

    def test_credential_content_denied_without_rewriting(self):
        target = self.put("outputs/qa/tests/test.py", b'password = "private-value"\n')
        self.assert_error("SECRET_DENIED", self.prepare)
        self.assertEqual(target.read_bytes(), b'password = "private-value"\n')

    def test_binary_or_nul_test_content_rejected(self):
        target = self.put("outputs/qa/tests/test.py", b"\xff")
        self.assert_error("FILE_ENCODING_ERROR", self.prepare)
        target.write_bytes(b"x\x00y")
        self.assert_error("FILE_ENCODING_ERROR", self.prepare)

    def test_per_file_byte_bound(self):
        target = self.put("outputs/qa/tests/test.py", b"#" * (1024 * 1024 + 1))
        self.assert_error("FILE_TOO_LARGE", self.prepare)
        target.write_bytes(b"#" * (1024 * 1024))
        self.assertEqual(len(self.prepare().files["tests/test.py"]), 1024 * 1024)

    def test_total_byte_bound(self):
        for index in range(17):
            self.put(f"outputs/qa/tests/test_{index}.py", b"#" * (1024 * 1024))
        self.assert_error("FILE_TOO_LARGE", self.prepare)

    def test_total_input_limit_includes_trusted_runner_at_exact_boundary(self):
        for index in range(16):
            self.put(f"outputs/qa/tests/test_{index}.py", b"#" * (1024 * 1024))
        # Scratch tests alone fit the capture cap, but mounting the runner
        # would put the complete input tree over the materializer's 16MiB.
        self.assert_error("FILE_TOO_LARGE", self.prepare)
        last = self.root / "outputs/qa/tests/test_15.py"
        last.write_bytes(b"#" * (1024 * 1024 - len(RUNNER)))
        captured = self.prepare()
        self.assertEqual(sum(map(len, captured.files.values())), 16 * 1024 * 1024)
        self.assertEqual(captured.runner_sha256, digest(RUNNER))
        last.write_bytes(b"#" * (1024 * 1024 - len(RUNNER) + 1))
        self.assert_error("FILE_TOO_LARGE", self.prepare)

    def test_file_count_bound(self):
        for index in range(65):
            self.put(f"outputs/qa/tests/test_{index}.py", b"")
        self.assert_error("FILE_TOO_LARGE", self.prepare)

    def test_empty_directory_forests_bounded(self):
        for index in range(66):
            (self.root / f"outputs/qa/tests/dir_{index}").mkdir(parents=True)
        self.assert_error("FILE_TOO_LARGE", self.prepare)

    def test_directory_depth_bounded(self):
        self.put("outputs/qa/tests/" + "/".join(["dir"] * 65) + "/test.py", b"")
        self.assert_error("PATH_DENIED", self.prepare)

    def test_cooperative_writer_lock_denies_capture(self):
        self.put("outputs/qa/tests/test.py")
        with self.context.workspace.opener() as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assert_error("WRITE_CONFLICT", self.prepare)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def test_file_mutation_during_read_is_denied(self):
        target = self.put("outputs/qa/tests/test.py", b"# before\n")
        actual_read = os.read
        changed = False
        target_inode = target.stat().st_ino
        def mutate(descriptor, size):
            nonlocal changed
            content = actual_read(descriptor, size)
            if content and not changed and os.fstat(descriptor).st_ino == target_inode:
                changed = True
                target.write_bytes(b"# after\n")
            return content
        with patch.object(unit_inputs.os, "read", side_effect=mutate):
            self.assert_error("WRITE_CONFLICT", self.prepare)

    def test_leaf_replacement_after_read_is_denied(self):
        target = self.put("outputs/qa/tests/test.py", b"# before\n")
        actual_read = os.read
        changed = False
        target_inode = target.stat().st_ino
        def replace(descriptor, size):
            nonlocal changed
            content = actual_read(descriptor, size)
            if content and not changed and os.fstat(descriptor).st_ino == target_inode:
                changed = True
                replacement = target.parent / "replacement.tmp"
                replacement.write_bytes(b"# before\n")
                replacement.replace(target)
            return content
        with patch.object(unit_inputs.os, "read", side_effect=replace):
            self.assert_error("WRITE_CONFLICT", self.prepare)

    def test_directory_new_file_during_capture_is_denied(self):
        self.put("outputs/qa/tests/test.py")
        original = unit_inputs._read_file
        def add(directory_fd, name):
            result = original(directory_fd, name)
            self.put("outputs/qa/tests/new.py")
            return result
        with patch.object(unit_inputs, "_read_file", side_effect=add):
            self.assert_error("WRITE_CONFLICT", self.prepare)

    def test_capture_is_read_only_and_test_code_is_not_executed(self):
        sentinel = self.directory / "host-executed"
        code = f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('executed')\n".encode()
        target = self.put("outputs/qa/tests/test.py", code)
        before = target.stat()
        with patch("subprocess.Popen", side_effect=AssertionError("no process")):
            captured = self.prepare()
        self.assertEqual(captured.files["tests/test.py"], code)
        self.assertFalse(sentinel.exists())
        self.assertEqual(target.stat().st_mtime_ns, before.st_mtime_ns)

    def test_invalid_runner_and_context_fail_closed(self):
        for runner in (None, "runner", b"", b"#" * (1024 * 1024 + 1)):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.prepare(runner=runner))
        self.assert_error("PERMISSION_DENIED", lambda: prepare_unit_inputs(None, scope(), RUNNER))
        wrong = MCPExecutionContext(binding=self.bind(AgentRole.DEVELOPER).binding, workspace=self.context.workspace)
        self.assert_error("PERMISSION_DENIED", lambda: self.prepare(context=wrong))

    def test_runner_hash_changes_inputs_hash_but_not_test_hash(self):
        self.put("outputs/qa/tests/test.py")
        first, second = self.prepare(), self.prepare(runner=RUNNER + b"# different\n")
        self.assertNotEqual(first.inputs_sha256, second.inputs_sha256)
        self.assertNotEqual(first.runner_sha256, second.runner_sha256)
        self.assertEqual(first.test_files_sha256, second.test_files_sha256)

    def test_constructor_rechecks_detached_input_bytes_and_hashes(self):
        self.put("outputs/qa/tests/test.py")
        original = self.prepare()
        data = dict(files=original.files, inputs_sha256=original.inputs_sha256,
            runner_sha256=original.runner_sha256, test_files_sha256=original.test_files_sha256)
        self.assertEqual(UnitTestInputs(**data), original)
        for field in ("inputs_sha256", "runner_sha256", "test_files_sha256"):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: UnitTestInputs(**{**data, field: "a" * 64}))
        self.assert_error("TOOL_EXECUTION_FAILED", lambda: UnitTestInputs(**{**data, "files": {"tests/test.py": b""}}))
        self.assert_error("PATH_DENIED", lambda: UnitTestInputs(**{**data, "files": {"_unit_runner.py": RUNNER, "../escape.py": b""}}))
        self.assert_error("SECRET_DENIED", lambda: UnitTestInputs(**{**data, "files": {"_unit_runner.py": RUNNER, "tests/test.py": b'password="private"'}}))

    def test_unknown_error_values_do_not_leak(self):
        error = UnitTestInputsError("private-path-secret")
        self.assertEqual(str(error), "TOOL_EXECUTION_FAILED")
