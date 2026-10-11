"""Real immutable SQLite/Snapshot fixtures; never run generated tests on Host."""

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
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.unit_config import UnitTestScope
from mcp_tools.tools.unit_inputs import UnitTestInputs, _files_hash
from mcp_tools.tools.unit_report import parse_unit_report
from mcp_tools.tools.unit_store import UnitTestOutputStore, UnitTestStoreError, MAX_UNIT_OUTPUT_BYTES
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.states import FinalVerdict
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxResult
from orchestrator.workspaces.registry import WorkspaceRegistry


class UnitTestOutputStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-unit-store-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.base = self.directory / "workspaces"
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.lock = b"local-unit-fixture==1\n"
        self.run, self.step, self.root, self.configuration = self.make_run()
        source_dir = self.root / "source"
        (source_dir / "requirements.lock").write_bytes(self.lock)
        (source_dir / "main.py").write_text("raise RuntimeError('Do not execute this Source')\n", encoding="utf-8")
        for args in (("init", "--object-format=sha1"), ("add", "main.py", "requirements.lock"), ("commit", "-m", "immutable unit fixture")):
            self.git(source_dir, *args)
        commit = self.git(source_dir, "rev-parse", "HEAD").strip()
        artifacts = ArtifactStore(self.repository, self.registry)
        self.source = artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=self.step.workflow_step_id, commit_hash=commit,
            repository_id="unit-store-fixture", lock_path="requirements.lock")
        self.binding = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                                  run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.scope = UnitTestScope(name="snapshot-unit", kind="SNAPSHOT")
        self.profile = self.profile_for(self.scope)
        self.inputs = self.inputs_for({})
        stdout = self.report_json()
        self.report = parse_unit_report(stdout, 0)
        self.result = SandboxResult(execution_id=uuid4(), run_id=self.run.run_id, source_artifact_id=self.source.artifact_id,
                                    profile_name=self.scope.name, tool_name="run_unit_tests", execution_manifest=self.source.execution_manifest(),
                                    image_id="sha256:" + "d" * 64, container_id="c" * 64, exit_code=0,
                                    duration_ms=123, stdout=stdout, stderr="")
        self.store = UnitTestOutputStore(self.repository)

    def make_run(self):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING,
                            code_version=1, requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids))
        configuration = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
            configuration=RunConfiguration(protected_test_suite_ref="https://criteria.example.invalid/signup/v1",
                environment=ExecutionBaseline(container_image_digest="sha256:" + "d" * 64,
                    dependency_lock_hash="sha256:" + sha256(self.lock).hexdigest(), hardware_profile="unit-store-fixture")))
        root = self.base / str(run.workspace_id)
        workspace = WorkspaceRecord(run_id=run.run_id, workspace_id=run.workspace_id, root_path=str(root))
        self.repository.create_run(run, (step,), (), workspace=workspace, run_configuration=configuration)
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        return run, step, root, configuration

    @staticmethod
    def git(directory, *arguments):
        return subprocess.run(["git", "-c", "user.name=Unit Store Test", "-c", "user.email=fixture@example.invalid",
                               "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
                              cwd=directory, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20, text=True).stdout

    @staticmethod
    def profile_for(scope):
        return ExecutionProfile(name=scope.name, tool_name="run_unit_tests",
                                argv=("/usr/local/bin/python", "-I", "-B", "/inputs/_unit_runner.py", "--kind", scope.kind,
                                      "--directory", scope.source_directory, "--pattern", scope.pattern),
                                image_reference="sha256:" + "d" * 64)

    @staticmethod
    def inputs_for(tests):
        runner = b"# Host test runner fixture, not executed.\n"
        files = {"_unit_runner.py": runner, **tests}
        return UnitTestInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=sha256(runner).hexdigest(),
                              test_files_sha256=_files_hash(tests))

    @staticmethod
    def report_json(*, outcome="PASS", details=None):
        case = {"testId": "tests.TestSignup.test_email", "outcome": outcome}
        if details is not None:
            case["details"] = details
        return json.dumps({"format": "unittest-v1", "total": 1, "passed": int(outcome == "PASS"),
                           "failed": int(outcome == "FAIL"), "skipped": int(outcome == "SKIP"), "tests": [case]}, ensure_ascii=False)

    def publish(self, **changes):
        return self.store.publish(self.binding, self.source, replace(self.result, **changes), profile=self.profile,
                                  scope=self.scope, inputs=self.inputs, report=self.report)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(UnitTestStoreError) as raised:
            operation(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def mutate_run(self, **changes):
        run = WorkflowRun.model_validate({**self.run.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (run.status.value, run.model_dump_json(), str(run.run_id)))

    def mutate_step(self, step=None, **changes):
        original = self.step if step is None else step
        step = WorkflowStep.model_validate({**original.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                               (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def qa_context(self, *, kind="QA_TESTS", protected_ref=None):
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.VALIDATING, code_version=1)
        qa = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.QA, status=WorkflowStepStatus.RUNNING,
                          attempt=0, code_version=1, requirement_ids=[self.source.requirement_ids[0]], input_artifact_ids=[self.source.artifact_id])
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES (?,?,?,?,?,?)",
                               (str(qa.workflow_step_id), str(qa.run_id), qa.status.value, qa.created_at.isoformat(), qa.updated_at.isoformat(), qa.model_dump_json()))
        self.binding = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        kwargs = {}
        if kind == "PROTECTED":
            kwargs = {"protected_files": {"tests/test_signup.py": "# protected criteria\n"},
                      "protected_suite_ref": protected_ref or self.configuration.configuration.protected_test_suite_ref}
        self.scope = UnitTestScope(name="qa-unit", kind=kind, **kwargs)
        self.profile = self.profile_for(self.scope)
        self.inputs = self.inputs_for({"tests/test_signup.py": b"# protected criteria\n"} if kind == "PROTECTED" else {"tests/test_signup.py": b"# captured QA tests\n"})
        self.result = replace(self.result, profile_name=self.scope.name)
        return qa

    def corrupt(self, record, **changes):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER unit_test_records_no_update")
            for name, value in changes.items():
                self.assertIn(name, {"metadata_json", "metadata_sha256", "stdout", "stderr", "report", "execution_id", "workspace_id"})
                connection.execute(f"UPDATE unit_test_execution_records SET {name}=? WHERE execution_manifest_id=?", (value, str(record.execution_manifest_id)))

    def metadata(self, record):
        with self.repository._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM unit_test_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()
        return json.loads(row[0])

    def replace_metadata(self, record, data):
        raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.corrupt(record, metadata_json=raw, metadata_sha256=sha256(raw.encode()).hexdigest())

    def test_constructor_is_inert(self):
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("inert")):
            store = UnitTestOutputStore(self.repository)
        self.assertEqual(repr(store), "UnitTestOutputStore()")
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='unit_test_execution_records'").fetchone())

    def test_non_repository_rejected(self):
        self.assert_code("UNIT_TEST_RESULT_INVALID", UnitTestOutputStore, object())

    def test_immutable_receipt_roundtrip_and_closed_tool_output(self):
        record = self.publish()
        self.assertEqual(record.execution_manifest_id.version, 4)
        self.assertNotEqual(record.execution_manifest_id, self.result.execution_id)
        self.assertEqual(record.execution_id, self.result.execution_id)
        self.assertEqual(record.workflow_step_id, self.step.workflow_step_id)
        self.assertEqual(record.execution_manifest, self.source.execution_manifest())
        self.assertEqual(record.execution_profile, self.profile)
        self.assertEqual(record.report, self.report)
        self.assertEqual(record.inputs["inputsSha256"], self.inputs.inputs_sha256)
        self.assertEqual(record.tool_name, "run_unit_tests")
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(record.tool_output(), {"total": 1, "passed": 1, "failed": 0, "skipped": 0,
                                              "reportRef": record.report_ref, "executionManifestId": str(record.execution_manifest_id)})
        with self.assertRaises(FrozenInstanceError):
            record.exit_code = 1
        with self.assertRaises(TypeError):
            record.inputs["inputsSha256"] = "a" * 64
        with self.assertRaises(TypeError):
            record.inputs["files"][0]["path"] = "wrong"

    def test_receipt_does_not_create_project_artifact_or_verdict(self):
        self.publish()
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.IMPLEMENTING)
        self.assertIsNone(run.verdict)

    def test_assertion_fail_is_successfully_published(self):
        stdout = self.report_json(outcome="FAIL", details="assertion did not match")
        self.report = parse_unit_report(stdout, 1)
        record = self.publish(stdout=stdout, exit_code=1)
        self.assertEqual(record.tool_output()["failed"], 1)
        self.assertEqual(record.exit_code, 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_skip_is_preserved_not_counted_as_pass(self):
        stdout = self.report_json(outcome="SKIP")
        self.report = parse_unit_report(stdout, 0)
        record = self.publish(stdout=stdout)
        self.assertEqual((record.report.passed, record.report.skipped), (0, 1))

    def test_sensitive_details_and_stderr_are_sanitized_before_hashes(self):
        stdout = self.report_json(details="password='never-store-this'\nAuthorization: Bearer private.jwt.token")
        self.report = parse_unit_report(stdout, 0)
        record = self.publish(stdout=stdout, stderr="api_key=never-store-api\n")
        for secret in ("never-store-this", "private.jwt.token", "never-store-api"):
            self.assertNotIn(secret, record.stdout + record.stderr + json.dumps(record.report.to_dict()))
        self.assertEqual(record.stdout_sha256, sha256(record.stdout.encode()).hexdigest())
        self.assertEqual(record.report_sha256, sha256(record.stdout.encode()).hexdigest())

    def test_repr_and_unknown_errors_do_not_leak_source_or_sql(self):
        record = self.publish(stderr="private output")
        for secret in (self.directory.as_posix(), "private output", self.source.repository_id, "/inputs/_unit_runner.py"):
            self.assertNotIn(secret, repr(record))
        self.assertEqual(str(UnitTestStoreError("private SQL")), "UNIT_TEST_STORAGE_ERROR")
        self.assertEqual(str(UnitTestStoreError([])), "UNIT_TEST_STORAGE_ERROR")

    def test_qa_read_report_is_same_run_and_workspace_only(self):
        record = self.publish()
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.assertEqual(self.store.read_report(qa, record.report_ref), self.report.to_dict())
        wrong = replace(qa, workspace_id=uuid4())
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.store.read_report, wrong, record.report_ref)
        wrong = replace(qa, run_id=uuid4())
        self.assert_code("UNIT_TEST_RECORD_NOT_FOUND", self.store.read_report, wrong, record.report_ref)

    def test_read_report_rejects_non_qa_roles(self):
        record = self.publish()
        for role in (AgentRole.DEVELOPER, AgentRole.PLANNER, AgentRole.SECURITY):
            binding = MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
            self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.store.read_report, binding, record.report_ref)

    def test_read_developer_receipt_explicitly_rechecks_reader_qa_grant(self):
        record = self.publish()
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        with patch.object(UnitTestOutputStore, "_source", wraps=UnitTestOutputStore._source) as source_check:
            with patch.object(self.repository, "_transaction", wraps=self.repository._transaction) as transaction:
                report = self.store.read_report(qa, record.report_ref)
        self.assertEqual(report, self.report.to_dict())
        self.assertIn(AgentRole.QA, [call.args[1].role for call in source_check.call_args_list])
        self.assertEqual(transaction.call_count, 1)

    def test_read_developer_receipt_rejects_missing_qa_source_grant(self):
        record = self.publish()
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(self.source.artifact_id),))
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.read_report, qa, record.report_ref)

    def test_completed_developer_receipt_remains_qa_readable_without_active_step(self):
        record = self.publish()
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.assertEqual(self.store.read_report(qa, record.report_ref), self.report.to_dict())

    def test_input_manifest_total_budget_includes_host_runner(self):
        scope = UnitTestScope(name="qa-unit", kind="QA_TESTS")
        files = [{"path": "_unit_runner.py", "sha256": "a" * 64, "sizeBytes": 1}]
        files.extend({"path": f"tests/test_{index:02}.py", "sha256": "b" * 64, "sizeBytes": 256 * 1024} for index in range(64))
        def payload(rows):
            def digest(values):
                return sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            return {"inputsSha256": digest(rows), "runnerSha256": rows[0]["sha256"],
                    "testFilesSha256": digest(rows[1:]), "files": rows}
        with self.assertRaises(ValueError):
            UnitTestOutputStore._verify_inputs_payload(payload(files), scope)
        files[1]["sizeBytes"] -= 1
        UnitTestOutputStore._verify_inputs_payload(payload(files), scope)

    def test_report_uris_are_never_fetched_or_resolved(self):
        record = self.publish()
        qa = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        for invalid in (None, "/etc/passwd", "file:///tmp/report.json", "https://example.invalid/report.json", record.report_ref + "?token=x",
                        record.report_ref + "#fragment", record.report_ref.replace("unit-test-report", "../unit-test-report"),
                        record.report_ref.upper(), record.report_ref.replace(".json", "%2ejson")):
            self.assert_code("UNIT_TEST_RESULT_INVALID", self.store.read_report, qa, invalid)

    def test_duplicate_execution_and_manifest_collisions(self):
        record = self.publish()
        self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish)
        with patch("mcp_tools.tools.unit_store.uuid4", return_value=record.execution_manifest_id):
            self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish, execution_id=uuid4())

    def test_manifest_source_config_or_same_execution_collision(self):
        for identity in (self.source.artifact_id, self.configuration.artifact_id, self.result.execution_id):
            with patch("mcp_tools.tools.unit_store.uuid4", return_value=identity):
                self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish)

    def test_execution_uuid_source_and_manifest_namespace_collision(self):
        self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish, execution_id=self.source.artifact_id)
        record = self.publish()
        self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish, execution_id=record.execution_manifest_id)

    def test_same_source_multiple_real_executions_have_distinct_receipts(self):
        first = self.publish()
        second = self.publish(execution_id=uuid4(), container_id="b" * 64)
        self.assertNotEqual(first.execution_manifest_id, second.execution_manifest_id)
        self.assertEqual(first.source_artifact_id, second.source_artifact_id)

    def test_sql_immutability_triggers(self):
        self.publish()
        with self.repository._connection() as connection:
            for query in ("UPDATE unit_test_execution_records SET stderr=x''", "DELETE FROM unit_test_execution_records",
                          "INSERT OR REPLACE INTO unit_test_execution_records SELECT * FROM unit_test_execution_records"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(query)

    def test_uuid_strictness_and_missing_records(self):
        record = self.publish()
        for invalid in (None, True, 123, uuid1(), "not-UUID"):
            self.assert_code("UNIT_TEST_RESULT_INVALID", self.store.get, invalid, record.execution_manifest_id)
            self.assert_code("UNIT_TEST_RESULT_INVALID", self.store.get, self.run.run_id, invalid)
        self.assert_code("UNIT_TEST_RECORD_NOT_FOUND", self.store.get, self.run.run_id, uuid4())

    def test_historical_finished_receipts_remain_readable(self):
        record = self.publish()
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)

    def test_cancelled_run_cannot_publish(self):
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="cancel fixture")
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_nonrunning_step_cannot_publish(self):
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_wrong_attempt_or_code_version_cannot_publish(self):
        self.mutate_step(attempt=1)
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_wrong_requirements_cannot_publish(self):
        self.mutate_step(requirement_ids=[uuid4()])
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_workspace_binding_cannot_publish_other_root(self):
        self.binding = replace(self.binding, workspace_id=uuid4())
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_qa_receipt_uses_qa_step_not_source_producer_step(self):
        qa = self.qa_context()
        record = self.publish()
        self.assertEqual(record.workflow_step_id, qa.workflow_step_id)
        self.assertNotEqual(record.workflow_step_id, self.source.workflow_step_id)
        self.assertEqual(record.role, AgentRole.QA)
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())

    def test_qa_continuation_attempt_independent_of_code_revision(self):
        qa = self.qa_context()
        self.mutate_step(qa, attempt=1)
        record = self.publish()
        self.assertEqual(record.execution_manifest.code_version, 1)
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())

    def test_qa_without_exact_source_input_is_denied(self):
        qa = self.qa_context()
        self.mutate_step(qa, input_artifact_ids=[])
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_missing_snapshot_grant_is_integrity_error(self):
        self.qa_context()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE role='QA'")
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.publish)

    def test_protected_criteria_publishes_matching_scope_and_input_hashes(self):
        self.qa_context(kind="PROTECTED")
        record = self.publish()
        self.assertEqual(record.scope.kind, "PROTECTED")
        self.assertEqual(record.inputs["testFilesSha256"], self.inputs.test_files_sha256)
        self.assertEqual(record.scope.protected_suite_ref, self.configuration.configuration.protected_test_suite_ref)

    def test_protected_ref_must_match_run_configuration(self):
        self.qa_context(kind="PROTECTED", protected_ref="https://criteria.example.invalid/another/v1")
        self.assert_code("UNIT_TEST_CONTEXT_DENIED", self.publish)

    def test_protected_input_bytes_cannot_be_changed_under_same_reference(self):
        self.qa_context(kind="PROTECTED")
        self.inputs = self.inputs_for({"tests/test_signup.py": b"# weakened criteria\n"})
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_snapshot_scope_cannot_mount_arbitrary_extra_tests(self):
        self.inputs = self.inputs_for({"tests/test_signup.py": b"# shadow tests\n"})
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_forged_input_digest_is_revalidated(self):
        object.__setattr__(self.inputs, "inputs_sha256", "a" * 64)
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_scope_role_is_enforced_at_publication(self):
        self.scope = UnitTestScope(name="qa-unit", kind="QA_TESTS")
        self.profile = self.profile_for(self.scope)
        self.result = replace(self.result, profile_name=self.scope.name)
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_invalid_runner_results_are_not_fabricated_into_counts(self):
        for changes in ({"exit_code": 2}, {"stdout": "not JSON"}, {"stdout": self.report_json(outcome="FAIL")}, {"exit_code": True}):
            self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish, **changes)

    def test_report_argument_must_match_actual_output(self):
        self.report = parse_unit_report(self.report_json(outcome="SKIP"), 0)
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_wrong_result_identity_or_manifest_is_rejected(self):
        for changes in ({"run_id": uuid4()}, {"source_artifact_id": uuid4()}, {"profile_name": "another"},
                        {"tool_name": "run_build"}, {"container_id": "bad"}, {"duration_ms": True}, {"execution_id": uuid1()}):
            self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish, **changes)

    def test_image_config_digest_is_exact_not_sha_shaped(self):
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish, image_id="sha256:" + "e" * 64)

    def test_repository_digest_can_have_distinct_config_id(self):
        self.profile = replace(self.profile, image_reference="registry.example.invalid/unit@sha256:" + "d" * 64)
        record = self.publish(image_id="sha256:" + "e" * 64)
        self.assertEqual(record.image_id, "sha256:" + "e" * 64)

    def test_wrong_profile_argv_cannot_be_recorded(self):
        self.profile = replace(self.profile, argv=("/usr/local/bin/python", "-c", "do anything"))
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_profile_tool_identity_cannot_be_forged(self):
        self.profile = replace(self.profile, tool_name="run_build")
        self.assert_code("UNIT_TEST_RESULT_INVALID", self.publish)

    def test_output_bounds_are_checked_before_publication(self):
        self.assert_code("UNIT_TEST_OUTPUT_LIMIT", self.publish, stderr="x" * (MAX_UNIT_OUTPUT_BYTES + 1))

    def test_storage_failure_exposes_no_sql_or_path(self):
        with patch.object(self.repository, "_transaction", side_effect=RuntimeError("private SQLite path")):
            self.assert_code("UNIT_TEST_STORAGE_ERROR", self.publish)

    def test_hash_corruption_is_rejected(self):
        record = self.publish()
        self.corrupt(record, metadata_sha256="a" * 64)
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_report_blob_corruption_is_rejected(self):
        record = self.publish()
        self.corrupt(record, report=sqlite3.Binary(b"{}"))
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_stdout_blob_corruption_is_rejected(self):
        record = self.publish()
        self.corrupt(record, stdout=sqlite3.Binary(b"{}"))
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_metadata_unknown_field_even_with_recomputed_hash_is_rejected(self):
        record = self.publish()
        metadata = self.metadata(record)
        metadata["modelSaysPASS"] = True
        self.replace_metadata(record, metadata)
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_metadata_input_hash_even_with_recomputed_record_hash_is_rejected(self):
        record = self.publish()
        metadata = self.metadata(record)
        metadata["inputs"]["inputsSha256"] = "a" * 64
        self.replace_metadata(record, metadata)
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_metadata_profile_scope_mismatch_even_with_hash_is_rejected(self):
        record = self.publish()
        metadata = self.metadata(record)
        metadata["scope"]["pattern"] = "other_*.py"
        self.replace_metadata(record, metadata)
        self.assert_code("UNIT_TEST_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_build_namespace_collision_is_rejected(self):
        build_profile = ExecutionProfile(name="fixture-build", tool_name="run_build", argv=("/usr/local/bin/compiler",), image_reference="sha256:" + "d" * 64)
        build_result = replace(self.result, execution_id=uuid4(), profile_name=build_profile.name, tool_name="run_build", stdout="compiled")
        build = BuildOutputStore(self.repository).publish(self.binding, self.source, build_result, profile=build_profile)
        self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish, execution_id=build.execution_manifest_id)
        with patch("mcp_tools.tools.unit_store.uuid4", return_value=build.execution_id):
            self.assert_code("UNIT_TEST_RECORD_CONFLICT", self.publish)
