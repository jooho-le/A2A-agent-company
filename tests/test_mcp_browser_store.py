"""Real immutable SQLite/Git fixtures; no browser or generated-code execution."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite, browser_host_payload
from mcp_tools.tools.browser_inputs import BrowserTestInputs
from mcp_tools.tools.browser_report import parse_browser_report
from mcp_tools.tools.browser_store import BrowserTestOutputStore, BrowserStoreError, MAX_BROWSER_OUTPUT_BYTES
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.unit_config import UnitTestScope
from mcp_tools.tools.unit_inputs import UnitTestInputs, _files_hash
from mcp_tools.tools.unit_report import parse_unit_report
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.states import FinalVerdict
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits, SandboxResult
from orchestrator.workspaces.registry import WorkspaceRegistry


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class BrowserTestOutputStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-browser-store-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.base = self.directory / "workspaces"
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.lock = b"local-browser-fixture==1\n"
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
        self.developer = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING,
            code_version=1, requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids))
        self.run_configuration = RunConfigurationArtifact(run_id=self.run.run_id, scenario_id=self.run.scenario_id, workspace_id=self.run.workspace_id,
            configuration=RunConfiguration(protected_test_suite_ref="https://criteria.example.invalid/signup/v1",
                environment=ExecutionBaseline(container_image_digest="sha256:" + "d" * 64,
                    dependency_lock_hash="sha256:" + sha256(self.lock).hexdigest(), hardware_profile="browser-store-fixture")))
        self.root = self.base / str(self.run.workspace_id)
        workspace = WorkspaceRecord(run_id=self.run.run_id, workspace_id=self.run.workspace_id, root_path=str(self.root))
        self.repository.create_run(self.run, (self.developer,), (), workspace=workspace, run_configuration=self.run_configuration)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        source_dir = self.root / "source"
        (source_dir / "requirements.lock").write_bytes(self.lock)
        (source_dir / "app.py").write_text("raise RuntimeError('Do not execute this Source')\n", encoding="utf-8")
        for args in (("init", "--object-format=sha1"), ("add", "app.py", "requirements.lock"), ("commit", "-m", "immutable browser fixture")):
            self.git(source_dir, *args)
        artifacts = ArtifactStore(self.repository, self.registry)
        self.source = artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=self.developer.workflow_step_id, commit_hash=self.git(source_dir, "rev-parse", "HEAD").strip(),
            repository_id="browser-store-fixture", lock_path="requirements.lock")
        self.mutate_step(self.developer, status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.VALIDATING, code_version=1)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.QA, status=WorkflowStepStatus.RUNNING,
            attempt=0, code_version=1, requirement_ids=[self.source.requirement_ids[0]], input_artifact_ids=[self.source.artifact_id])
        self.insert_step(self.step)
        self.binding = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.suite = BrowserTestSuite(name="qa-browser", kind="QA_TESTS")
        self.configuration = self.configuration_for(self.suite)
        self.profile = self.profile_for(self.configuration, self.suite)
        self.inputs = self.inputs_for(self.suite_json())
        stdout = self.report_json()
        self.report = parse_browser_report(stdout, 0)
        self.result = SandboxResult(execution_id=uuid4(), run_id=self.run.run_id, source_artifact_id=self.source.artifact_id,
            profile_name=self.suite.name, tool_name="run_browser_tests", execution_manifest=self.source.execution_manifest(),
            image_id="sha256:" + "d" * 64, container_id="c" * 64, exit_code=0, duration_ms=123, stdout=stdout, stderr="")
        self.store = BrowserTestOutputStore(self.repository)

    @staticmethod
    def git(directory, *arguments):
        return subprocess.run(["git", "-c", "user.name=Browser Store Test", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments], cwd=directory,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20, text=True).stdout

    @staticmethod
    def configuration_for(suite, **changes):
        values = dict(suites=(suite,), service_argv=("/usr/local/bin/python", "/snapshot/app.py"),
                      playwright_version="1.58.0", image_reference="sha256:" + "d" * 64)
        values.update(changes)
        return BrowserTestConfiguration(**values)

    @staticmethod
    def profile_for(configuration, suite):
        return ExecutionProfile(name=suite.name, tool_name="run_browser_tests", limits=configuration.limits,
            argv=(configuration.python_executable, "-I", "-B", "/inputs/_browser_runner.py"), image_reference=configuration.image_reference)

    @staticmethod
    def suite_json(*, test_id="signup.normal", steps=None):
        return json.dumps({"format": "browser-suite-v1", "tests": [{"testId": test_id,
            "steps": steps or [{"action": "goto", "path": "/signup"},
                               {"action": "assert_visible", "selector": "#welcome"}]}]}, ensure_ascii=False)

    @staticmethod
    def report_json(*, suite_name="qa-browser", test_id="signup.normal", outcome="PASS", steps=None, version="1.58.0"):
        case = {"testId": test_id, "outcome": outcome, "steps": steps or [
            {"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 4},
            {"index": 2, "action": "assert_visible", "outcome": outcome, "durationMs": 5}]}
        if outcome == "FAIL":
            case["details"] = "ASSERTION_FAILED"
        return json.dumps({"format": "browser-v1", "suiteName": suite_name, "playwrightVersion": version,
            "browserVersion": "145.0.7632.6", "total": 1, "passed": int(outcome == "PASS"), "failed": int(outcome == "FAIL"),
            "tests": [case]})

    def inputs_for(self, suite_json, **changes):
        files = {"_browser_runner.py": b"# Trusted runner fixture, never executed.\n",
            "_browser_contract.py": b"# Trusted contract fixture, never executed.\n",
            "_browser_host.json": canonical(browser_host_payload(self.configuration, self.suite)).encode("utf-8"),
            self.suite.suite_path: suite_json.encode("utf-8")}
        files.update(changes)
        tests = {path: content for path, content in files.items() if path.startswith("tests/")}
        return BrowserTestInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=sha256(files["_browser_runner.py"]).hexdigest(),
            contract_sha256=sha256(files["_browser_contract.py"]).hexdigest(), test_files_sha256=_files_hash(tests),
            host_configuration_sha256=sha256(files["_browser_host.json"]).hexdigest())

    def publish(self, **changes):
        return self.store.publish(self.binding, self.source, replace(self.result, **changes),
            profile=self.profile, suite=self.suite, inputs=self.inputs, report=self.report, configuration=self.configuration)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(BrowserStoreError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def mutate_run(self, **changes):
        original = self.repository.get_run(self.run.run_id)
        run = WorkflowRun.model_validate({**original.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (run.status.value, run.model_dump_json(), str(run.run_id)))

    def mutate_step(self, step=None, **changes):
        original = self.step if step is None else step
        step = WorkflowStep.model_validate({**original.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def insert_step(self, step):
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (str(step.workflow_step_id), str(step.run_id), step.status.value, step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))

    def protected(self, *, reference=None):
        self.suite = BrowserTestSuite(name="qa-browser", kind="PROTECTED", protected_files={"tests/browser/suite.json": self.suite_json()},
            protected_suite_ref=reference or self.run_configuration.configuration.protected_test_suite_ref)
        self.configuration = self.configuration_for(self.suite)
        self.inputs = self.inputs_for(self.suite_json())

    def metadata(self, record):
        with self.repository._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM browser_test_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()
        return json.loads(row[0])

    def corrupt(self, record, **changes):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER IF EXISTS browser_test_records_no_update")
            for column, value in changes.items():
                self.assertIn(column, {"metadata_json", "metadata_sha256", "report", "traces", "stdout", "stderr", "execution_id"})
                connection.execute(f"UPDATE browser_test_execution_records SET {column}=? WHERE execution_manifest_id=?", (value, str(record.execution_manifest_id)))

    def replace_metadata(self, record, data):
        raw = canonical(data)
        self.corrupt(record, metadata_json=raw, metadata_sha256=sha256(raw.encode("utf-8")).hexdigest())

    def test_constructor_is_inert_and_safe(self):
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("must be inert")):
            store = BrowserTestOutputStore(self.repository)
        self.assertEqual(repr(store), "BrowserTestOutputStore()")
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='browser_test_execution_records'").fetchone())
        self.assert_code("BROWSER_TEST_RESULT_INVALID", BrowserTestOutputStore, object())

    def test_record_roundtrip_is_immutable_and_tool_output_exact(self):
        record = self.publish()
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(record.workflow_step_id, self.step.workflow_step_id)
        self.assertNotEqual(record.workflow_step_id, self.source.workflow_step_id)
        self.assertEqual(record.execution_profile, self.profile)
        self.assertEqual(record.report, self.report)
        self.assertEqual(record.inputs["inputsSha256"], self.inputs.inputs_sha256)
        self.assertEqual(record.tool_output(), {"total": 1, "passed": 1, "failed": 0,
            "traceRefs": [f"artifact://{record.execution_manifest_id}/browser-trace-0000.json"],
            "executionManifestId": str(record.execution_manifest_id)})
        self.assertEqual(record.report_ref, f"artifact://{record.execution_manifest_id}/browser-test-report.json")
        with self.assertRaises(FrozenInstanceError):
            record.exit_code = 1
        with self.assertRaises(TypeError):
            record.inputs["files"][0]["sha256"] = "a" * 64

    def test_report_and_json_trace_read_same_run_workspace_only(self):
        record = self.publish()
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())
        trace = self.store.read_trace(self.binding, record.trace_refs[0])
        self.assertEqual(trace["format"], "browser-trace-v1")
        self.assertEqual(trace["testId"], "signup.normal")
        self.assertEqual(trace["steps"], self.report.tests[0].to_dict()["steps"])
        for read, reference in ((self.store.read_report, record.report_ref), (self.store.read_trace, record.trace_refs[0])):
            self.assert_code("BROWSER_TEST_CONTEXT_DENIED", read, replace(self.binding, workspace_id=uuid4()), reference)
            self.assert_code("BROWSER_TEST_RECORD_NOT_FOUND", read, replace(self.binding, run_id=uuid4()), reference)

    def test_trace_excludes_selectors_values_urls_source_and_dom(self):
        suite_text = self.suite_json(steps=[{"action": "goto", "path": "/signup"},
            {"action": "fill", "selector": "#private-input", "value": "fixture-private-value"},
            {"action": "assert_visible", "selector": "#private-result"}])
        self.inputs = self.inputs_for(suite_text)
        stdout = self.report_json(steps=[{"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 1},
            {"index": 2, "action": "fill", "outcome": "PASS", "durationMs": 2},
            {"index": 3, "action": "assert_visible", "outcome": "PASS", "durationMs": 3}])
        self.report = parse_browser_report(stdout, 0)
        record = self.publish(stdout=stdout)
        text = canonical(self.store.read_trace(self.binding, record.trace_refs[0])) + canonical(self.metadata(record))
        for value in ("fixture-private-value", "#private-input", "#private-result", '"path":"/signup"', "raise RuntimeError"):
            self.assertNotIn(value, text)

    def test_product_failure_published_without_qa_artifact_or_verdict(self):
        stdout = self.report_json(outcome="FAIL")
        self.report = parse_browser_report(stdout, 1)
        record = self.publish(stdout=stdout, exit_code=1)
        self.assertEqual((record.exit_code, record.report.failed), (1, 1))
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_failure_can_report_action_prefix_only(self):
        stdout = self.report_json(outcome="FAIL", steps=[{"index": 1, "action": "goto", "outcome": "FAIL", "durationMs": 1}])
        self.report = parse_browser_report(stdout, 1)
        record = self.publish(stdout=stdout, exit_code=1)
        self.assertEqual(len(record.report.tests[0].steps), 1)

    def test_pass_cannot_omit_expected_actions_even_with_an_assertion(self):
        self.inputs = self.inputs_for(self.suite_json(steps=[{"action": "goto", "path": "/signup"},
            {"action": "assert_visible", "selector": "#welcome"}, {"action": "click", "selector": "#continue"}]))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)

    def test_case_id_or_action_mismatch_rejected(self):
        stdout = self.report_json(test_id="another.test")
        self.report = parse_browser_report(stdout, 0)
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, stdout=stdout)
        stdout = self.report_json(steps=[{"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 1},
            {"index": 2, "action": "assert_url", "outcome": "PASS", "durationMs": 1}])
        self.report = parse_browser_report(stdout, 0)
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, stdout=stdout)

    def test_report_must_include_every_selected_suite_case_in_order(self):
        data = json.loads(self.suite_json())
        second = dict(data["tests"][0])
        second["testId"] = "signup.second"
        data["tests"].append(second)
        self.inputs = self.inputs_for(json.dumps(data))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)

    def test_two_cases_have_distinct_stable_trace_refs(self):
        suite = json.loads(self.suite_json())
        other = {**suite["tests"][0], "testId": "signup.second"}
        suite["tests"].append(other)
        self.inputs = self.inputs_for(json.dumps(suite))
        report = json.loads(self.report_json())
        report["tests"].append({**report["tests"][0], "testId": "signup.second"})
        report.update(total=2, passed=2)
        stdout = json.dumps(report)
        self.report = parse_browser_report(stdout, 0)
        record = self.publish(stdout=stdout)
        self.assertEqual(len(record.trace_refs), 2)
        self.assertTrue(record.trace_refs[1].endswith("browser-trace-0001.json"))
        self.assertEqual(self.store.read_trace(self.binding, record.trace_refs[1])["testId"], "signup.second")

    def test_stderr_credentials_redacted_before_hashes(self):
        record = self.publish(stderr="api_key=fixture-private-key\nAuthorization: Bearer private-token\n")
        self.assertNotIn("fixture-private-key", record.stderr)
        self.assertNotIn("private-token", record.stderr)
        self.assertEqual(record.stderr_sha256, sha256(record.stderr.encode("utf-8")).hexdigest())

    def test_publication_and_reads_require_qa(self):
        record = self.publish()
        for role in (AgentRole.DEVELOPER, AgentRole.PLANNER, AgentRole.SECURITY):
            binding = replace(self.binding, role=role, agent_role=role)
            self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.store.read_report, binding, record.report_ref)
            self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.store.read_trace, binding, record.trace_refs[0])
            self.binding = binding
            self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)

    def test_completed_run_historical_report_and_trace_need_no_active_qa_step(self):
        record = self.publish()
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())
        self.assertEqual(self.store.read_trace(self.binding, record.trace_refs[0])["outcome"], "PASS")

    def test_missing_snapshot_qa_grant_blocks_publication_and_history_reads(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(self.source.artifact_id),))
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.publish, execution_id=uuid4())
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.read_trace, self.binding, record.trace_refs[0])
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_read_source_grant_check_is_in_same_transaction(self):
        record = self.publish()
        with patch.object(BrowserTestOutputStore, "_source", wraps=BrowserTestOutputStore._source) as source_check:
            with patch.object(self.repository, "_transaction", wraps=self.repository._transaction) as transaction:
                self.store.read_report(self.binding, record.report_ref)
        self.assertEqual(transaction.call_count, 1)
        self.assertEqual(source_check.call_args.args[1].role, AgentRole.QA)

    def test_nonrunning_qa_step_or_cancelled_run_denied(self):
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)
        self.mutate_step(status=WorkflowStepStatus.RUNNING)
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="cancel fixture")
        self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)

    def test_wrong_attempt_version_requirements_or_source_input_denied(self):
        for changes in ({"attempt": 1}, {"code_version": 2}, {"requirement_ids": [uuid4()]}, {"input_artifact_ids": []}):
            self.mutate_step(**changes)
            self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)
            self.mutate_step()

    def test_multiple_running_qa_steps_denied(self):
        self.insert_step(WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.QA, status=WorkflowStepStatus.RUNNING,
            code_version=1, requirement_ids=[self.source.requirement_ids[0]], input_artifact_ids=[self.source.artifact_id]))
        self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)

    def test_wrong_workspace_binding_denied(self):
        self.binding = replace(self.binding, workspace_id=uuid4())
        self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)

    def test_protected_suite_ref_and_exact_bytes_verified(self):
        self.protected()
        record = self.publish()
        self.assertEqual(record.suite["kind"], "PROTECTED")
        self.inputs = self.inputs_for(self.suite_json(test_id="weakened.criteria"))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, execution_id=uuid4())

    def test_protected_ref_must_match_frozen_run_configuration(self):
        self.protected(reference="https://criteria.example.invalid/another/v1")
        self.assert_code("BROWSER_TEST_CONTEXT_DENIED", self.publish)

    def test_forged_input_digest_rechecked(self):
        for name in ("inputs_sha256", "runner_sha256", "contract_sha256", "test_files_sha256", "host_configuration_sha256"):
            original = getattr(self.inputs, name)
            object.__setattr__(self.inputs, name, "a" * 64)
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)
            object.__setattr__(self.inputs, name, original)

    def test_configuration_changes_cannot_reuse_old_host_inputs(self):
        self.configuration = self.configuration_for(self.suite, service_argv=("/usr/local/bin/python", "/snapshot/different.py"))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)

    def test_host_selected_suite_must_be_in_configuration(self):
        self.configuration = self.configuration_for(BrowserTestSuite(name="another", kind="QA_TESTS"))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)

    def test_wrong_playwright_version_or_suite_name_rejected(self):
        for changes in ({"version": "1.59.0"}, {"suite_name": "another"}):
            stdout = self.report_json(**changes)
            self.report = parse_browser_report(stdout, 0)
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, stdout=stdout)

    def test_result_identity_manifest_exit_and_report_rechecked(self):
        for changes in ({"run_id": uuid4()}, {"source_artifact_id": uuid4()}, {"tool_name": "run_build"},
                        {"profile_name": "another"}, {"container_id": "bad"}, {"duration_ms": True}, {"execution_id": uuid1()},
                        {"exit_code": 2}, {"exit_code": True}, {"stdout": "{}"}):
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, **changes)

    def test_report_argument_must_equal_actual_output(self):
        self.report = parse_browser_report(self.report_json(outcome="FAIL"), 1)
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)

    def test_profile_requires_fixed_runner_argv_and_tool_identity(self):
        for changes in ({"argv": ("/usr/local/bin/python", "-c", "unsafe")}, {"tool_name": "run_unit_tests"}, {"name": "another"}):
            original = self.profile
            self.profile = replace(original, **changes)
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)
            self.profile = original

    def test_profile_timeout_can_narrow_but_not_expand(self):
        self.profile = replace(self.profile, limits=replace(self.profile.limits, timeout_seconds=30))
        self.publish()
        self.profile = replace(self.profile, limits=replace(self.profile.limits, timeout_seconds=61))
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, execution_id=uuid4())

    def test_non_timeout_profile_limits_must_match_host_policy(self):
        for changes in ({"cpus": 2}, {"memory_bytes": 1024 * 1024 * 1024}, {"max_stderr_bytes": 2 * 1024 * 1024}):
            original = self.profile
            self.profile = replace(original, limits=replace(original.limits, **changes))
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish)
            self.profile = original

    def test_exact_digest_and_repository_digest_semantics(self):
        self.assert_code("BROWSER_TEST_RESULT_INVALID", self.publish, image_id="sha256:" + "e" * 64)
        self.configuration = self.configuration_for(self.suite, image_reference="registry.example.invalid/browser@sha256:" + "d" * 64)
        self.profile = self.profile_for(self.configuration, self.suite)
        self.inputs = self.inputs_for(self.suite_json())
        record = self.publish(image_id="sha256:" + "e" * 64)
        self.assertEqual(record.image_id, "sha256:" + "e" * 64)

    def test_output_limits_checked_before_publication(self):
        self.assert_code("BROWSER_TEST_OUTPUT_LIMIT", self.publish, stderr="x" * (MAX_BROWSER_OUTPUT_BYTES + 1))
        self.assert_code("BROWSER_TEST_OUTPUT_LIMIT", self.publish, stderr="x" * (self.profile.limits.max_stderr_bytes + 1))

    def test_sql_update_delete_and_replace_are_denied(self):
        self.publish()
        with self.repository._connection() as connection:
            for statement in ("UPDATE browser_test_execution_records SET stderr=x''", "DELETE FROM browser_test_execution_records",
                              "INSERT OR REPLACE INTO browser_test_execution_records SELECT * FROM browser_test_execution_records"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)

    def test_execution_and_manifest_namespace_collisions(self):
        record = self.publish()
        for identity in (self.source.artifact_id, self.run_configuration.artifact_id, record.execution_manifest_id, record.execution_id):
            self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish, execution_id=identity)
            with patch("mcp_tools.tools.browser_store.uuid4", return_value=identity):
                self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish, execution_id=uuid4())

    def test_manifest_cannot_equal_execution_and_duplicate_execution_denied(self):
        self.publish()
        self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish)
        with patch("mcp_tools.tools.browser_store.uuid4", return_value=self.result.execution_id):
            self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish)

    def test_build_namespace_collision_verified(self):
        self.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        self.mutate_step(self.developer, status=WorkflowStepStatus.RUNNING)
        developer = replace(self.binding, role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER)
        profile = ExecutionProfile(name="fixture-build", tool_name="run_build", argv=("/usr/local/bin/compiler",), image_reference="sha256:" + "d" * 64)
        result = replace(self.result, execution_id=uuid4(), profile_name=profile.name, tool_name="run_build", stdout="compiled")
        build = BuildOutputStore(self.repository).publish(developer, self.source, result, profile=profile)
        self.mutate_run(status=WorkflowStatus.VALIDATING)
        self.mutate_step(self.developer, status=WorkflowStepStatus.SUCCEEDED)
        self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish, execution_id=build.execution_manifest_id)

    def test_unit_namespace_collision_verified(self):
        scope = UnitTestScope(name="qa-unit", kind="QA_TESTS")
        profile = ExecutionProfile(name=scope.name, tool_name="run_unit_tests", image_reference="sha256:" + "d" * 64,
            argv=("/usr/local/bin/python", "-I", "-B", "/inputs/_unit_runner.py", "--kind", scope.kind,
                  "--directory", scope.source_directory, "--pattern", scope.pattern))
        files = {"_unit_runner.py": b"# Host fixture\n", "tests/test_fixture.py": b"# QA fixture, not executed\n"}
        inputs = UnitTestInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=sha256(files["_unit_runner.py"]).hexdigest(),
            test_files_sha256=_files_hash({"tests/test_fixture.py": files["tests/test_fixture.py"]}))
        stdout = json.dumps({"format": "unittest-v1", "total": 1, "passed": 1, "failed": 0, "skipped": 0,
            "tests": [{"testId": "fixture.test", "outcome": "PASS"}]})
        result = replace(self.result, profile_name=scope.name, tool_name="run_unit_tests", stdout=stdout)
        unit = UnitTestOutputStore(self.repository).publish(self.binding, self.source, result,
            profile=profile, scope=scope, inputs=inputs, report=parse_unit_report(stdout, 0))
        self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish, execution_id=unit.execution_manifest_id)
        with patch("mcp_tools.tools.browser_store.uuid4", return_value=unit.execution_id):
            self.assert_code("BROWSER_TEST_RECORD_CONFLICT", self.publish, execution_id=uuid4())

    def test_source_actual_blob_hash_is_verified_on_publish_and_read(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            row = connection.execute("SELECT content FROM artifact_contents WHERE artifact_id=?", (str(self.source.artifact_id),)).fetchone()
            content = row[0]
            replacement = b"x" + content[1:]
            connection.execute("UPDATE artifact_contents SET content=? WHERE artifact_id=?", (sqlite3.Binary(replacement), str(self.source.artifact_id)))
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.publish, execution_id=uuid4())
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_historical_run_workspace_payload_identity_is_verified(self):
        record = self.publish()
        self.mutate_run(workspace_id=uuid4())
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_selected_host_policy_changes_fail_even_with_recomputed_host_policy_hash(self):
        record = self.publish()
        data = self.metadata(record)
        data["hostConfiguration"]["runner"]["service_argv"][-1] = "/snapshot/changed.py"
        data["hostPolicySha256"] = sha256(canonical(data["hostConfiguration"]).encode("utf-8")).hexdigest()
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_publication_grant_check_and_receipt_write_share_one_transaction(self):
        with patch.object(BrowserTestOutputStore, "_source", wraps=BrowserTestOutputStore._source) as source_check:
            with patch.object(self.repository, "_transaction", wraps=self.repository._transaction) as transaction:
                self.publish()
        self.assertEqual(transaction.call_count, 1)
        self.assertGreaterEqual(source_check.call_count, 2)

    def test_strict_uuid_and_not_found_errors(self):
        for invalid in (None, True, 5, uuid1(), "not-UUID"):
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.store.get, invalid, uuid4())
            self.assert_code("BROWSER_TEST_RESULT_INVALID", self.store.get, self.run.run_id, invalid)
        self.assert_code("BROWSER_TEST_RECORD_NOT_FOUND", self.store.get, self.run.run_id, uuid4())

    def test_references_never_resolve_host_or_external_paths(self):
        record = self.publish()
        for read, ref in ((self.store.read_report, record.report_ref), (self.store.read_trace, record.trace_refs[0])):
            for invalid in (None, "/etc/passwd", "file:///tmp/report.json", "https://example.invalid/report.json",
                            ref + "?token=x", ref + "#fragment", ref.upper(), ref.replace(".json", "%2ejson")):
                self.assert_code("BROWSER_TEST_RESULT_INVALID", read, self.binding, invalid)
        self.assert_code("BROWSER_TEST_RECORD_NOT_FOUND", self.store.read_trace, self.binding,
                         f"artifact://{record.execution_manifest_id}/browser-trace-0001.json")

    def test_corrupt_metadata_and_blob_hashes_rejected(self):
        record = self.publish()
        self.corrupt(record, metadata_sha256="a" * 64)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_each_blob_is_hash_checked(self):
        record = self.publish()
        for name in ("report", "traces", "stdout", "stderr"):
            with self.repository._connection() as connection:
                original = connection.execute(f"SELECT {name} FROM browser_test_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()[0]
            self.corrupt(record, **{name: sqlite3.Binary(b"{}")})
            self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)
            self.corrupt(record, **{name: sqlite3.Binary(original)})

    def test_unknown_metadata_and_input_hash_rejected_even_with_new_record_hash(self):
        record = self.publish()
        data = self.metadata(record)
        data["modelSaysPASS"] = True
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)
        del data["modelSaysPASS"]
        data["inputs"]["inputsSha256"] = "a" * 64
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_selected_suite_inventory_or_host_policy_hash_corruption_rejected(self):
        record = self.publish()
        data = self.metadata(record)
        data["suite"]["tests"][0]["actions"][-1] = "assert_url"
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)
        data = self.metadata(record)
        data["suite"]["tests"][0]["actions"][-1] = "assert_visible"
        data["hostPolicySha256"] = "a" * 64
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_trace_cannot_be_changed_even_with_blob_and_metadata_hashes_updated(self):
        record = self.publish()
        trace = [self.store.read_trace(self.binding, record.trace_refs[0])]
        trace[0]["screenshot"] = "private bytes"
        blob = canonical(trace).encode("utf-8")
        data = self.metadata(record)
        data["tracesSizeBytes"] = len(blob)
        data["tracesSha256"] = sha256(blob).hexdigest()
        self.corrupt(record, traces=sqlite3.Binary(blob))
        self.replace_metadata(record, data)
        self.assert_code("BROWSER_TEST_RESULT_INTEGRITY_ERROR", self.store.read_trace, self.binding, record.trace_refs[0])

    def test_storage_errors_and_repr_hide_host_source_and_sql(self):
        record = self.publish(stderr="private stderr")
        for value in (self.directory.as_posix(), "private stderr", "/inputs/_browser_runner.py", self.source.repository_id):
            self.assertNotIn(value, repr(record))
        for unknown in ("private SQL", [], None):
            self.assertEqual(str(BrowserStoreError(unknown)), "BROWSER_TEST_STORAGE_ERROR")
        with patch.object(self.repository, "_transaction", side_effect=RuntimeError("private SQLite path")):
            self.assert_code("BROWSER_TEST_STORAGE_ERROR", self.publish, execution_id=uuid4())
