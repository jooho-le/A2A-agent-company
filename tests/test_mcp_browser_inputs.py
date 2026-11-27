"""Real temporary QA tree capture; no product/test source is executed."""

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
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.browser_inputs import BrowserInputsError, BrowserTestInputs, prepare_browser_inputs
from mcp_tools.tools.unit_inputs import _files_hash
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


RUNNER = b"# trusted browser runner\n"
CONTRACT = b"# trusted declarative parser\n"
SUITE = json.dumps({"format": "browser-suite-v1", "tests": [{"testId": "signup", "steps": [
    {"action": "goto", "path": "/signup"}, {"action": "assert_visible", "selector": "#email"}]}]}, separators=(",", ":")).encode()


def selected(**changes):
    values = {"name": "signup", "kind": "QA_TESTS"}
    values.update(changes)
    return BrowserTestSuite(**values)


def policy(suite=None):
    return BrowserTestConfiguration(suites=(suite or selected(),),
        service_argv=("/usr/local/bin/python", "/snapshot/app.py"), playwright_version="1.55.0")


def digest(value):
    return hashlib.sha256(value).hexdigest()


class BrowserInputsTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-browser-inputs-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.root = self.base / str(self.run.workspace_id)
        self.repository.create_run(self.run, (), (), workspace=WorkspaceRecord(workspace_id=self.run.workspace_id,
            run_id=self.run.run_id, root_path=str(self.root)))
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.context = self.bind(AgentRole.QA)

    def bind(self, role):
        return MCPExecutionContext(binding=MCPBinding(role=role, agent_role=role, run_id=self.run.run_id,
            workspace_id=self.run.workspace_id), workspace=self.registry.bind(self.run.workspace_id,
                run_id=self.run.run_id, role=role))

    def put(self, path="outputs/qa/tests/browser/suite.json", content=SUITE):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def prepare(self, suite=None, context=None, configuration=None, runner=RUNNER, contract=CONTRACT):
        chosen = suite or selected()
        return prepare_browser_inputs(context or self.context, chosen, configuration or policy(chosen), runner, contract)

    def assert_error(self, code, operation):
        with self.assertRaises(BrowserInputsError) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(caught.exception.args, (code,))
        self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def rebuild(self, files, **changes):
        tests = {path: content for path, content in files.items() if path.startswith("tests/")}
        values = {"files": files, "inputs_sha256": _files_hash(files), "runner_sha256": digest(files["_browser_runner.py"]),
            "contract_sha256": digest(files["_browser_contract.py"]), "host_configuration_sha256": digest(files["_browser_host.json"]),
            "test_files_sha256": _files_hash(tests)}
        values.update(changes)
        return BrowserTestInputs(**values)

    def test_qa_json_bytes_and_all_trusted_assets_are_frozen(self):
        target = self.put()
        self.put("outputs/qa/tests/nested/fixture.json", b'{"result":"ok"}')
        inputs = self.prepare()
        target.write_bytes(b"changed")
        self.assertEqual(inputs.files["tests/browser/suite.json"], SUITE)
        self.assertEqual(inputs.files["_browser_runner.py"], RUNNER)
        self.assertEqual(inputs.files["_browser_contract.py"], CONTRACT)
        with self.assertRaises(TypeError):
            inputs.files["tests/browser/suite.json"] = b"changed"
        with self.assertRaises(FrozenInstanceError):
            inputs.inputs_sha256 = "a" * 64
        self.assertNotIn("#email", repr(inputs))

    def test_all_hashes_are_deterministic_and_test_hash_excludes_assets(self):
        self.put()
        inputs = self.prepare()
        self.assertEqual(inputs.inputs_sha256, _files_hash(inputs.files))
        self.assertEqual(inputs.runner_sha256, digest(RUNNER))
        self.assertEqual(inputs.contract_sha256, digest(CONTRACT))
        self.assertEqual(inputs.host_configuration_sha256, digest(inputs.files["_browser_host.json"]))
        self.assertEqual(inputs.test_files_sha256, _files_hash({"tests/browser/suite.json": SUITE}))
        self.assertEqual(inputs, self.prepare())

    def test_host_json_is_canonical_closed_runner_policy(self):
        self.put()
        inputs = self.prepare()
        raw = inputs.files["_browser_host.json"]
        host = json.loads(raw)
        self.assertEqual(set(host), {"suite_name", "suite_path", "service_argv", "base_url", "ready_path",
            "startup_timeout_seconds", "action_timeout_ms", "playwright_version"})
        self.assertEqual(raw, json.dumps(host, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode())

    def test_protected_tests_never_read_workspace_or_fall_back(self):
        fixed = selected(kind="PROTECTED", protected_files={"tests/browser/suite.json": SUITE.decode()},
            protected_suite_ref="artifact://protected/tests")
        self.put(content=b"hostile mutable replacement")
        with patch("os.open", side_effect=AssertionError("no Host filesystem reads")):
            inputs = self.prepare(fixed)
        self.assertEqual(inputs.files["tests/browser/suite.json"], SUITE)

    def test_only_qa_binding_can_capture_or_run_suites(self):
        self.put()
        for role in AgentRole:
            if role is not AgentRole.QA:
                self.assert_error("PERMISSION_DENIED", lambda: self.prepare(context=self.bind(role)))

    def test_mismatched_workspace_binding_denied(self):
        other = WorkflowRun(scenario_id=SCN_001_ID, request_text="다른 요청")
        forged = MCPExecutionContext(binding=MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA,
            run_id=other.run_id, workspace_id=other.workspace_id), workspace=self.context.workspace)
        self.assert_error("PERMISSION_DENIED", lambda: self.prepare(context=forged))

    def test_missing_qa_tree_and_selected_suite_are_not_success(self):
        self.assert_error("FILE_NOT_FOUND", self.prepare)
        self.put("outputs/qa/tests/other.json", b"{}")
        self.assert_error("FILE_NOT_FOUND", self.prepare)

    def test_invalid_empty_or_executable_suite_denied_before_docker(self):
        for text in (b"{}", b"", b'print("generated code")', b'{"format":"browser-suite-v1","tests":[]}'):
            self.put(content=text)
            self.assert_error("TEST_RUNNER_ERROR", self.prepare)

    def test_chosen_suite_must_belong_to_host_policy(self):
        self.put()
        self.assert_error("TEST_RUNNER_ERROR", lambda: self.prepare(configuration=policy(selected(name="other"))))

    def test_symlink_file_and_directory_denied(self):
        target = self.put()
        link = target.parent / "linked.json"
        link.symlink_to("suite.json")
        self.assert_error("PATH_DENIED", self.prepare)
        link.unlink()
        (target.parent / "linked-dir").symlink_to(".", target_is_directory=True)
        self.assert_error("PATH_DENIED", self.prepare)

    def test_symlink_parent_cannot_read_source(self):
        self.put("source/tests/browser/suite.json")
        (self.root / "outputs/qa/tests").symlink_to("../../source/tests", target_is_directory=True)
        self.assert_error("PATH_DENIED", self.prepare)

    def test_hardlink_and_fifo_are_not_inputs(self):
        target = self.put()
        link = target.parent / "linked.json"
        os.link(target, link)
        self.assert_error("PATH_DENIED", self.prepare)
        link.unlink()
        os.mkfifo(target.parent / "pipe")
        self.assert_error("PATH_DENIED", self.prepare)

    def test_secret_names_and_contents_not_captured(self):
        self.put()
        target = self.put("outputs/qa/tests/.env", b"fixture")
        self.assert_error("PATH_DENIED", self.prepare)
        target.unlink()
        target = self.put("outputs/qa/tests/test_private.py", b'password = "private-value"')
        self.assert_error("SECRET_DENIED", self.prepare)
        self.assertEqual(target.read_bytes(), b'password = "private-value"')

    def test_binary_or_nul_inputs_denied(self):
        for content in (b"\xff", b"x\x00y"):
            self.put(content=content)
            self.assert_error("FILE_ENCODING_ERROR", self.prepare)

    def test_per_file_and_file_count_limits(self):
        self.put()
        target = self.put("outputs/qa/tests/large.json", b"x" * (1024 * 1024 + 1))
        self.assert_error("FILE_TOO_LARGE", self.prepare)
        target.unlink()
        for index in range(64):
            self.put(f"outputs/qa/tests/f-{index}.json", b"{}")
        self.assert_error("FILE_TOO_LARGE", self.prepare)

    def test_cooperative_write_lock_denies_capture(self):
        self.put()
        with self.context.workspace.opener() as descriptor:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                self.assert_error("WRITE_CONFLICT", self.prepare)
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)

    def test_context_is_not_an_arbitrary_fake_filesystem(self):
        self.assert_error("PERMISSION_DENIED", lambda: prepare_browser_inputs(None, selected(), policy(), RUNNER, CONTRACT))

    def test_bad_trusted_asset_types_and_bounds(self):
        self.put()
        for asset in (None, "source", b"", b"x" * (1024 * 1024 + 1)):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.prepare(runner=asset))
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.prepare(contract=asset))

    def test_trusted_asset_content_is_validated_not_executed(self):
        self.put()
        with patch("subprocess.Popen", side_effect=AssertionError("never execute Host code")):
            self.prepare(runner=b"raise RuntimeError('would execute only inside container')\n")
        self.assert_error("SECRET_DENIED", lambda: self.prepare(contract=b'password = "private-value"'))

    def test_forged_input_hashes_are_rejected(self):
        self.put()
        captured = self.prepare()
        for name in ("inputs_sha256", "runner_sha256", "test_files_sha256", "host_configuration_sha256", "contract_sha256"):
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: self.rebuild(dict(captured.files), **{name: "0" * 64}))

    def test_required_assets_cannot_be_absent_or_aliased(self):
        self.put()
        captured = self.prepare()
        for name in ("_browser_runner.py", "_browser_contract.py", "_browser_host.json"):
            files = dict(captured.files)
            files.pop(name)
            self.assert_error("TOOL_EXECUTION_FAILED", lambda: BrowserTestInputs(files=files, inputs_sha256="0" * 64,
                runner_sha256="0" * 64, contract_sha256="0" * 64, host_configuration_sha256="0" * 64, test_files_sha256="0" * 64))

    def test_forged_host_json_extra_remote_shell_duplicate_denied(self):
        self.put()
        captured = self.prepare()
        host = json.loads(captured.files["_browser_host.json"])
        for value in ({**host, "base_url": "https://example.org"}, {**host, "shell": True},
                      {**host, "service_argv": ["/bin/sh", "-c", "echo fixture"]}, {**host, "suite_name": "../unsafe"}):
            files = {**captured.files, "_browser_host.json": json.dumps(value).encode()}
            self.assert_error("TEST_RUNNER_ERROR", lambda: self.rebuild(files))
        files = {**captured.files, "_browser_host.json": captured.files["_browser_host.json"][:-1] + b',"suite_name":"other"}'}
        self.assert_error("TEST_RUNNER_ERROR", lambda: self.rebuild(files))

    def test_forged_test_outside_tree_or_file_parent_collision_denied(self):
        self.put()
        captured = self.prepare()
        for extra in ({"outside.py": b"fixture"}, {"tests/browser": b"fixture"}, {"tests/../evil": b"fixture"}):
            self.assert_error("PATH_DENIED", lambda: self.rebuild({**captured.files, **extra}))

    def test_host_configuration_is_canonical_not_only_same_json_values(self):
        self.put()
        captured = self.prepare()
        files = {**captured.files, "_browser_host.json": json.dumps(json.loads(captured.files["_browser_host.json"])).encode()}
        self.assert_error("TEST_RUNNER_ERROR", lambda: self.rebuild(files))

    def test_total_input_byte_limit_includes_all_three_assets(self):
        self.put()
        captured = self.prepare()
        files = dict(captured.files)
        for index in range(15):
            files[f"tests/f-{index}.json"] = b" " * (1024 * 1024)
        overhead = sum(len(value) for path, value in files.items() if not path.startswith("tests/f-"))
        files["tests/last.json"] = b" " * (1024 * 1024 - overhead)
        self.assertEqual(sum(map(len, self.rebuild(files).files.values())), 16 * 1024 * 1024)
        files["tests/last.json"] += b" "
        self.assert_error("FILE_TOO_LARGE", lambda: self.rebuild(files))

    def test_selected_json_reparsed_when_bundle_is_constructed(self):
        self.put()
        captured = self.prepare()
        files = {**captured.files, "tests/browser/suite.json": b'{}'}
        self.assert_error("TEST_RUNNER_ERROR", lambda: self.rebuild(files))

    def test_error_constructor_never_echoes_unrecognized_content(self):
        error = BrowserInputsError("password=private")
        self.assertEqual(str(error), "TOOL_EXECUTION_FAILED")
