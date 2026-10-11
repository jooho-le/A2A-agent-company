"""Step24 end-to-end file handlers with real SQLite, Git, bytes and stdio."""

import asyncio
from hashlib import sha256
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from mcp_tools.client import MCPChildConfiguration, MCPClientError, child_parameters, open_mcp_client
from mcp_tools.core.policy import ROLE_TOOL_NAMES
from mcp_tools.runtime import MCPBinding, MCPDispatcher, MCPExecutionContext, MCPToolExecutionError
from mcp_tools.tools.files import FileTools, _run_file_operation
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import (
    AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus,
    WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


class FileToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-mcp-files-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.database = self.directory / "workflow.sqlite3"
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.database)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
                                 status=WorkflowStepStatus.RUNNING, code_version=1,
                                 requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids))
        self.lock = b"local-fixture==1\n"
        configuration = RunConfigurationArtifact(
            run_id=self.run.run_id, scenario_id=SCN_001_ID, workspace_id=self.run.workspace_id,
            configuration=RunConfiguration(environment=ExecutionBaseline(
                container_image_digest="sha256:" + "d" * 64,
                dependency_lock_hash="sha256:" + sha256(self.lock).hexdigest(),
                hardware_profile="file-tool-fixture")),
        )
        self.root = self.base / str(self.run.workspace_id)
        record = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id, root_path=str(self.root))
        self.repository.create_run(self.run, (self.step,), (), workspace=record, run_configuration=configuration)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.store = ArtifactStore(self.repository, self.registry)
        self.original = b"def signup():\n    return 'initial'\n"
        self.file = self.root / "source/signup.py"
        self.file.write_bytes(self.original)
        (self.root / "source/requirements.lock").write_bytes(self.lock)

    def binding(self, role=AgentRole.DEVELOPER):
        return MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)

    def dispatcher(self, role=AgentRole.DEVELOPER, selection=None, **extra):
        tools = FileTools(self.store, frozen_source=selection)
        return MCPDispatcher(self.binding(role), self.registry, handlers=tools.handlers(role), **extra)

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id), "path": "source/signup.py", **extra}

    def source_selection(self):
        source = self.root / "source"
        for args in (("init", "--object-format=sha1"), ("add", "signup.py", "requirements.lock"), ("commit", "-m", "fixture source")):
            subprocess.run(["git", "-c", "user.name=File Test", "-c", "user.email=file@example.invalid",
                            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
                           cwd=source, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"},
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20)
        commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=source, check=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=20, text=True).stdout.strip()
        snapshot = self.store.bind(self.run.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=self.step.workflow_step_id, commit_hash=commit,
            repository_id="file-fixture", lock_path="requirements.lock",
        )
        return FrozenSourceSelection(project_artifact_id=snapshot.artifact_id, snapshot_sha256=snapshot.snapshot_sha256)

    @staticmethod
    def diff():
        return "--- a/source/signup.py\n+++ b/source/signup.py\n@@ -1,2 +1,2 @@\n def signup():\n-    return 'initial'\n+    return 'updated'\n"

    def patch_arguments(self, selection, diff=None, **extra):
        return {"workspaceId": str(self.run.workspace_id), "patch": self.diff() if diff is None else diff,
                "baseSnapshotSha256": selection.snapshot_sha256, **extra}

    async def test_real_working_read_hash_size_and_exact_source(self):
        result = await self.dispatcher().call_tool("read_project_file", self.arguments())
        self.assertIsNone(result.error_code)
        self.assertEqual(result.data, {"path": "source/signup.py", "content": self.original.decode(),
                                       "sha256": sha256(self.original).hexdigest(), "sizeBytes": len(self.original)})

    async def test_write_read_noop_and_expected_hash_conflict(self):
        dispatcher = self.dispatcher()
        value = "password=request.password\r\n# 한글\n"
        arguments = self.arguments(content=value, expectedSha256=sha256(self.original).hexdigest())
        first = await dispatcher.call_tool("write_source_file", arguments)
        self.assertTrue(first.data["changed"])
        self.assertEqual(first.data["sha256"], sha256(value.encode()).hexdigest())
        self.assertEqual(self.file.read_bytes(), value.encode())
        conflict = await dispatcher.call_tool("write_source_file", arguments)
        self.assertEqual(conflict.error_code, "WRITE_CONFLICT")
        same = await dispatcher.call_tool("write_source_file", self.arguments(content=value))
        self.assertFalse(same.data["changed"])
        read = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(read.data["content"], value)

    async def test_source_write_creates_parents_without_executing_code(self):
        target = self.root / "source/new/nested/main.py"
        result = await self.dispatcher().call_tool("write_source_file", self.arguments(
            path="source/new/nested/main.py", content="raise RuntimeError('must not execute')\n"))
        self.assertIsNone(result.error_code)
        self.assertEqual(target.read_text(), "raise RuntimeError('must not execute')\n")

    async def test_missing_file_and_binary_file_are_execution_errors(self):
        dispatcher = self.dispatcher()
        missing = await dispatcher.call_tool("read_project_file", self.arguments(path="source/missing.py"))
        self.assertEqual(missing.error_code, "FILE_NOT_FOUND")
        self.file.write_bytes(b"\xff\x00")
        binary = await dispatcher.call_tool("read_project_file", self.arguments())
        self.assertEqual(binary.error_code, "FILE_ENCODING_ERROR")

    async def test_secret_read_is_rejected_not_silently_rehashed_or_logged(self):
        content = b'api_key="fixture-credential"\n'
        self.file.write_bytes(content)
        result = await self.dispatcher().call_tool("read_project_file", self.arguments())
        self.assertEqual(result.error_code, "SECRET_DENIED")
        self.assertIsNone(result.data)
        self.assertNotIn("fixture-credential", repr(result))
        self.assertEqual(self.file.read_bytes(), content)

    async def test_qa_writes_only_test_area_and_exact_output_schema(self):
        dispatcher = self.dispatcher(AgentRole.QA)
        allowed = await dispatcher.call_tool("write_test_file", self.arguments(
            path="outputs/qa/tests/test_signup.py", content="def test_signup(): pass\n"))
        self.assertEqual(set(allowed.data), {"path", "sha256", "changed"})
        self.assertTrue(allowed.data["changed"])
        for path in ("source/signup.py", "outputs/qa/report.json", "outputs/qa/tests"):
            denied = await dispatcher.call_tool("write_test_file", self.arguments(path=path, content="{}"))
            self.assertEqual(denied.error_code, "PATH_DENIED")
        self.assertEqual(self.file.read_bytes(), self.original)

    async def test_qa_and_security_source_read_never_fallback_to_working_copy(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            result = await self.dispatcher(role).call_tool("read_project_file", self.arguments())
            self.assertEqual(result.error_code, "SNAPSHOT_REQUIRED")
        selection = self.source_selection()
        self.file.write_bytes(b"working copy changed\n")
        for role in (AgentRole.QA, AgentRole.SECURITY):
            result = await self.dispatcher(role, selection).call_tool("read_project_file", self.arguments())
            self.assertEqual(result.data["content"].encode(), self.original)
            self.assertEqual(result.data["sha256"], sha256(self.original).hexdigest())

    async def test_explicit_frozen_alias_uses_only_selected_artifact(self):
        selection = self.source_selection()
        self.file.write_bytes(b"new working copy\n")
        dispatcher = self.dispatcher(selection=selection)
        path = f"snapshots/{selection.project_artifact_id}/source/signup.py"
        result = await dispatcher.call_tool("read_project_file", self.arguments(path=path))
        self.assertEqual(result.data["path"], path)
        self.assertEqual(result.data["content"].encode(), self.original)
        foreign = await dispatcher.call_tool("read_project_file", self.arguments(path=f"snapshots/{uuid4()}/source/signup.py"))
        self.assertEqual(foreign.error_code, "PATH_DENIED")

    async def test_validator_output_symlink_cannot_bypass_frozen_source(self):
        selection = self.source_selection()
        self.file.write_bytes(b"mutable working secret source\n")
        link = self.root / "outputs/qa/link.py"
        link.symlink_to(self.file)
        directory_link = self.root / "planning/source_alias"
        directory_link.symlink_to(self.root / "source", target_is_directory=True)
        for role in (AgentRole.QA, AgentRole.SECURITY):
            for selected in (None, selection):
                dispatcher = self.dispatcher(role, selected)
                for path in ("outputs/qa/link.py", "planning/source_alias/signup.py"):
                    result = await dispatcher.call_tool("read_project_file", self.arguments(path=path))
                    self.assertEqual(result.error_code, "PATH_DENIED")
                    self.assertIsNone(result.data)

    async def test_client_also_verifies_content_hash_and_byte_size(self):
        # Exercise the public bound Client validator with a synthetic peer;
        # actual server hashes are checked in the stdio integration tests.
        from test_mcp_client import FakeClient, configuration, read_output, successful_result
        from mcp_tools.client import BoundMCPClient
        config = configuration()
        sdk = FakeClient(config)
        client = BoundMCPClient(configuration=config, _client=sdk)
        for changed in ({"sha256": "f" * 64}, {"sizeBytes": 0}):
            sdk.session.result = successful_result(read_output() | changed)
            with self.assertRaises(MCPClientError) as raised:
                await client.call_tool("read_project_file", {"workspaceId": str(config.binding.workspace_id), "path": "source/signup.py"})
            self.assertEqual(raised.exception.code, "MCP_CLIENT_OUTPUT_INVALID")

    async def test_patch_applies_real_bytes_without_mutating_frozen_source(self):
        selection = self.source_selection()
        result = await self.dispatcher(selection=selection).call_tool("apply_patch", self.patch_arguments(selection))
        updated = self.original.replace(b"initial", b"updated")
        self.assertEqual(result.data, {"changedFiles": ["source/signup.py"], "newHashes": {"source/signup.py": sha256(updated).hexdigest()}})
        self.assertEqual(self.file.read_bytes(), updated)
        frozen = await self.dispatcher(AgentRole.QA, selection).call_tool("read_project_file", self.arguments())
        self.assertEqual(frozen.data["content"].encode(), self.original)

    async def test_patch_requires_matching_selected_hash_and_entire_affected_file(self):
        selection = self.source_selection()
        dispatcher = self.dispatcher(selection=selection)
        wrong = await dispatcher.call_tool("apply_patch", self.patch_arguments(selection, baseSnapshotSha256="f" * 64))
        self.assertEqual(wrong.error_code, "BASE_MISMATCH")
        self.file.write_bytes(self.original + b"# extra untouched line\n")
        mismatch = await dispatcher.call_tool("apply_patch", self.patch_arguments(selection))
        self.assertEqual(mismatch.error_code, "BASE_MISMATCH")
        self.assertEqual(self.file.read_bytes(), self.original + b"# extra untouched line\n")

    async def test_invalid_second_patch_file_does_not_partially_modify_first(self):
        selection = self.source_selection()
        text = self.diff() + "--- /dev/null\n+++ b/source/new.py\n@@ -0,0 +1 @@\n+new\n"
        (self.root / "source/new.py").write_bytes(b"unexpected current\n")
        result = await self.dispatcher(selection=selection).call_tool("apply_patch", self.patch_arguments(selection, text))
        self.assertEqual(result.error_code, "BASE_MISMATCH")
        self.assertEqual(self.file.read_bytes(), self.original)

    async def test_patch_can_add_and_delete_and_does_not_hash_deleted_file_as_empty(self):
        selection = self.source_selection()
        text = ("--- a/source/signup.py\n+++ /dev/null\n@@ -1,2 +0,0 @@\n-def signup():\n-    return 'initial'\n"
                "--- /dev/null\n+++ b/source/new.py\n@@ -0,0 +1 @@\n+new content\n")
        result = await self.dispatcher(selection=selection).call_tool("apply_patch", self.patch_arguments(selection, text))
        self.assertIsNone(result.error_code)
        self.assertEqual(set(result.data["changedFiles"]), {"source/signup.py", "source/new.py"})
        self.assertEqual(result.data["newHashes"], {"source/new.py": sha256(b"new content\n").hexdigest()})
        self.assertFalse(self.file.exists())
        self.assertEqual((self.root / "source/new.py").read_bytes(), b"new content\n")

    async def test_patch_cas_detects_change_between_snapshot_comparison_and_write(self):
        selection = self.source_selection()
        from mcp_tools.tools.file_io import apply_changes as real_apply_changes

        def changed_before_commit(workspace, changes, expected):
            self.file.write_bytes(b"other writer\n")
            return real_apply_changes(workspace, changes, expected)

        with patch("mcp_tools.tools.files.apply_changes", side_effect=changed_before_commit):
            result = await self.dispatcher(selection=selection).call_tool("apply_patch", self.patch_arguments(selection))
        self.assertEqual(result.error_code, "WRITE_CONFLICT")
        self.assertEqual(self.file.read_bytes(), b"other writer\n")

    async def test_build_and_test_handlers_are_still_not_fake_success(self):
        result = await self.dispatcher().call_tool("run_build", {
            "workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4())})
        self.assertEqual(result.error_code, "TOOL_NOT_IMPLEMENTED")

    async def test_handler_and_snapshot_selection_construction_are_inert(self):
        with patch.object(self.repository, "get_run", side_effect=AssertionError("must not read")):
            tools = FileTools(self.store)
            for role in AgentRole:
                handlers = tools.handlers(role)
                self.assertTrue(set(handlers) <= set(ROLE_TOOL_NAMES[role]))
            self.assertEqual(set(tools.handlers(AgentRole.QA)), {"read_project_file", "write_test_file"})

    async def test_child_selection_is_host_only_and_not_visible_in_config_repr(self):
        selection = self.source_selection()
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.database,
                                       workspace_root=self.base, frozen_source=selection)
        arguments = child_parameters(config).args
        self.assertIn("--source-artifact-id", arguments)
        self.assertIn(str(selection.project_artifact_id), arguments)
        self.assertIn(selection.snapshot_sha256, arguments)
        self.assertNotIn(selection.snapshot_sha256, repr(config))
        with self.assertRaises(MCPClientError):
            MCPChildConfiguration(binding=self.binding(), database_path=self.database, workspace_root=self.base, frozen_source={})

    async def test_actual_stdio_child_writes_then_reads_real_source_and_qa_test(self):
        config = MCPChildConfiguration(binding=self.binding(), database_path=self.database, workspace_root=self.base)
        async with open_mcp_client(config) as client:
            result = await client.call_tool("write_source_file", self.arguments(path="source/new.py", content="value=42\n"))
            self.assertTrue(result["changed"])
            read = await client.call_tool("read_project_file", self.arguments(path="source/new.py"))
            self.assertEqual(read["content"], "value=42\n")
            self.assertEqual(read["sha256"], sha256(b"value=42\n").hexdigest())
        qa = MCPChildConfiguration(binding=self.binding(AgentRole.QA), database_path=self.database, workspace_root=self.base)
        async with open_mcp_client(qa) as client:
            result = await client.call_tool("write_test_file", self.arguments(path="outputs/qa/tests/test_file.py", content="assert 1==1\n"))
            self.assertEqual(set(result), {"path", "sha256", "changed"})
        self.assertEqual((self.root / "outputs/qa/tests/test_file.py").read_bytes(), b"assert 1==1\n")

    async def test_actual_stdio_child_frozen_read_and_patch_preserve_snapshot(self):
        selection = self.source_selection()
        developer = MCPChildConfiguration(binding=self.binding(), database_path=self.database,
                                          workspace_root=self.base, frozen_source=selection)
        async with open_mcp_client(developer) as client:
            result = await client.call_tool("apply_patch", self.patch_arguments(selection))
            self.assertEqual(result["changedFiles"], ["source/signup.py"])
        for role in (AgentRole.QA, AgentRole.SECURITY):
            config = MCPChildConfiguration(binding=self.binding(role), database_path=self.database,
                                           workspace_root=self.base, frozen_source=selection)
            async with open_mcp_client(config) as client:
                result = await client.call_tool("read_project_file", self.arguments())
                self.assertEqual(result["content"].encode(), self.original)


class FileWorkerCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancel_waits_for_actual_worker_before_finishing(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def operation():
            entered.set()
            release.wait(5)
            finished.set()

        task = asyncio.create_task(_run_file_operation(operation))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
        finally:
            release.set()
            if not task.done():
                await asyncio.gather(task, return_exceptions=True)

    async def test_safe_execution_error_does_not_echo_unknown_submitted_value(self):
        error = MCPToolExecutionError("private-submitted-value")
        self.assertEqual(str(error), "TOOL_EXECUTION_FAILED")
        self.assertNotIn("private-submitted-value", repr(error))


if __name__ == "__main__":
    unittest.main()
