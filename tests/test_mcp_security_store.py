"""Real immutable SQLite/Source fixtures; no Bandit or generated-code execution."""

from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

import test_mcp_unit_store as _unit_fixtures
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.security_config import SecurityScannerProfile, SecurityScanConfiguration, security_host_payload
from mcp_tools.tools.security_inputs import SecurityScanInputs
from mcp_tools.tools.security_report import parse_security_report
from mcp_tools.tools.security_store import SecurityScanOutputStore, SecurityStoreError, MAX_SECURITY_OUTPUT_BYTES
from mcp_tools.tools.unit_inputs import _files_hash
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain import AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.states import FinalVerdict
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits
from orchestrator.sandbox.materialization import _canonical_archive


SCANNER_REFERENCE = "https://scanner.example.invalid/bandit/v1"


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


class SecurityScanOutputStoreTests(unittest.TestCase):
    def setUp(self):
        class Fixture(_unit_fixtures.UnitTestOutputStoreTests):
            def make_run(inner):
                run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
                step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING,
                    code_version=1, requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids))
                configuration = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
                    configuration=RunConfiguration(protected_test_suite_ref="https://criteria.example.invalid/signup/v1",
                        scanner_profile_ref=SCANNER_REFERENCE, environment=ExecutionBaseline(container_image_digest="sha256:" + "d" * 64,
                            dependency_lock_hash="sha256:" + sha256(inner.lock).hexdigest(), hardware_profile="security-store-fixture")))
                root = inner.base / str(run.workspace_id)
                workspace = WorkspaceRecord(run_id=run.run_id, workspace_id=run.workspace_id, root_path=str(root))
                inner.repository.create_run(run, (step,), (), workspace=workspace, run_configuration=configuration)
                inner.registry.provision(run.workspace_id, run_id=run.run_id)
                return run, step, root, configuration
        fixture = Fixture("runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.fixture = fixture
        for name in ("repository", "registry", "directory", "base", "root", "source", "run"):
            setattr(self, name, getattr(fixture, name))
        self.developer = fixture.step
        self.run_configuration = fixture.configuration
        self.mutate_step(self.developer, status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.VALIDATING, code_version=1)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.SECURITY, status=WorkflowStepStatus.RUNNING,
            code_version=1, requirement_ids=[self.source.requirement_ids[0]], input_artifact_ids=[self.source.artifact_id])
        self.insert_step(self.step)
        self.binding = MCPBinding(role=AgentRole.SECURITY, agent_role=AgentRole.SECURITY, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.scanner_profile = SecurityScannerProfile(name="python-security", scanner_version="1.8.6", rule_ids=("B101", "B307"), profile_ref=SCANNER_REFERENCE)
        self.configuration = self.configuration_for(self.scanner_profile)
        self.profile = self.profile_for(self.configuration, self.scanner_profile)
        self.inputs = self.inputs_for(self.configuration, self.scanner_profile)
        stdout = self.report_json()
        self.report = parse_security_report(stdout, 0)
        self.result = replace(fixture.result, profile_name=self.scanner_profile.name, tool_name="run_security_scan", stdout=stdout)
        self.store = SecurityScanOutputStore(self.repository)

    @staticmethod
    def configuration_for(scanner_profile, **changes):
        values = dict(profiles=(scanner_profile,), image_reference="sha256:" + "d" * 64)
        values.update(changes)
        return SecurityScanConfiguration(**values)

    @staticmethod
    def profile_for(configuration, scanner_profile):
        return ExecutionProfile(name=scanner_profile.name, tool_name="run_security_scan", limits=configuration.limits,
            argv=(configuration.python_executable, "-I", "-B", "/inputs/_security_runner.py"), image_reference=configuration.image_reference)

    @staticmethod
    def inputs_for(configuration, scanner_profile):
        files = {"_security_runner.py": b"# Host fixture, never executed\n", "_security_contract.py": b"# Host contract fixture\n",
                 "_security_host.json": canonical(security_host_payload(configuration, scanner_profile)).encode("utf-8")}
        return SecurityScanInputs(files=files, inputs_sha256=_files_hash(files), runner_sha256=sha256(files["_security_runner.py"]).hexdigest(),
            contract_sha256=sha256(files["_security_contract.py"]).hexdigest(), host_configuration_sha256=sha256(files["_security_host.json"]).hexdigest())

    @staticmethod
    def finding(**changes):
        value = dict(ruleId="B101", testName="assert_used", path="main.py", line=1, column=0,
                     severity="LOW", confidence="HIGH", status="SUSPECTED")
        value.update(changes)
        return value

    @staticmethod
    def report_json(*, findings=None, **changes):
        values = dict(format="bandit-v1", profileName="python-security", scanner="bandit", scannerVersion="1.8.6",
            ruleIds=["B101", "B307"], profileRef=SCANNER_REFERENCE, scannedFiles=["main.py"], findings=[] if findings is None else findings)
        values.update(changes)
        return json.dumps(values, ensure_ascii=False)

    def publish(self, **changes):
        return self.store.publish(self.binding, self.source, replace(self.result, **changes), profile=self.profile,
            scanner_profile=self.scanner_profile, inputs=self.inputs, report=self.report, configuration=self.configuration)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(SecurityStoreError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(str(caught.exception), code)

    def mutate_run(self, **changes):
        original = self.repository.get_run(self.run.run_id)
        run = WorkflowRun.model_validate({**original.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?", (run.status.value, run.model_dump_json(), str(run.run_id)))

    def mutate_step(self, step=None, **changes):
        original = self.step if step is None else step
        updated = WorkflowStep.model_validate({**original.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (updated.status.value, updated.model_dump_json(), str(updated.workflow_step_id)))

    def insert_step(self, step):
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (str(step.workflow_step_id), str(step.run_id), step.status.value, step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))

    def metadata(self, record):
        with self.repository._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM security_scan_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()
        return json.loads(row[0])

    def corrupt(self, record, **changes):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER IF EXISTS security_scan_records_no_update")
            for column, value in changes.items():
                self.assertIn(column, {"metadata_json", "metadata_sha256", "report", "stdout", "execution_id"})
                connection.execute(f"UPDATE security_scan_execution_records SET {column}=? WHERE execution_manifest_id=?", (value, str(record.execution_manifest_id)))

    def replace_metadata(self, record, data):
        raw = canonical(data)
        self.corrupt(record, metadata_json=raw, metadata_sha256=sha256(raw.encode("utf-8")).hexdigest())

    def replace_frozen_configuration(self, **changes):
        original = self.run_configuration.configuration.model_dump()
        config = RunConfiguration.model_validate({**original, **changes})
        artifact = self.run_configuration.model_copy(update={"configuration": config})
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?", (artifact.model_dump_json(), str(self.run.run_id)))

    def test_constructor_is_inert_and_safe(self):
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("inert")):
            store = SecurityScanOutputStore(self.repository)
        self.assertEqual(repr(store), "SecurityScanOutputStore()")
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='security_scan_execution_records'").fetchone())
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", SecurityScanOutputStore, object())

    def test_receipt_roundtrip_empty_findings_tool_output_and_immutable(self):
        record = self.publish()
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(record.workflow_step_id, self.step.workflow_step_id)
        self.assertEqual(record.tool_output(), {"findings": [], "reportRef": record.report_ref, "executionManifestId": str(record.execution_manifest_id)})
        self.assertEqual(record.report_ref, f"artifact://{record.execution_manifest_id}/security-scan-report.json")
        self.assertEqual(record.inputs["inputsSha256"], self.inputs.inputs_sha256)
        self.assertEqual(record.source_files[0]["path"], "main.py")
        self.assertEqual(record.stderr, "")
        with self.assertRaises(FrozenInstanceError):
            record.exit_code = 1
        with self.assertRaises(TypeError):
            record.inputs["files"][0]["path"] = "other"

    def test_findings_are_suspected_only_no_project_artifact_or_final_verdict(self):
        stdout = self.report_json(findings=[self.finding()])
        self.report = parse_security_report(stdout, 1)
        record = self.publish(stdout=stdout, exit_code=1)
        self.assertEqual(record.tool_output()["findings"][0]["status"], "SUSPECTED")
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_report_read_is_security_same_run_workspace_only(self):
        record = self.publish()
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.store.read_report, replace(self.binding, workspace_id=uuid4()), record.report_ref)
        self.assert_code("SECURITY_SCAN_RECORD_NOT_FOUND", self.store.read_report, replace(self.binding, run_id=uuid4()), record.report_ref)
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.PLANNER):
            self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.store.read_report, replace(self.binding, role=role, agent_role=role), record.report_ref)

    def test_nonsecurity_publication_denied(self):
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.PLANNER):
            self.binding = replace(self.binding, role=role, agent_role=role)
            self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)

    def test_completed_run_historical_reads_require_no_running_step(self):
        record = self.publish()
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_run(status=WorkflowStatus.FINISHED, verdict=FinalVerdict.SUCCESS)
        self.assertEqual(self.store.read_report(self.binding, record.report_ref), self.report.to_dict())
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)

    def test_removed_security_grant_denies_publication_and_history(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='SECURITY'", (str(self.source.artifact_id),))
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.publish, execution_id=uuid4())
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_grant_check_and_record_write_read_each_share_transaction(self):
        with patch.object(SecurityScanOutputStore, "_source", wraps=SecurityScanOutputStore._source) as source_check:
            with patch.object(self.repository, "_transaction", wraps=self.repository._transaction) as transaction:
                record = self.publish()
        self.assertEqual(transaction.call_count, 1)
        self.assertGreaterEqual(source_check.call_count, 2)
        with patch.object(self.repository, "_transaction", wraps=self.repository._transaction) as transaction:
            self.store.read_report(self.binding, record.report_ref)
        self.assertEqual(transaction.call_count, 1)

    def test_cancelled_run_or_nonrunning_step_denies_publication(self):
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)
        self.mutate_step()
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="cancel fixture")
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)

    def test_wrong_code_requirement_and_source_input_denied(self):
        for changes in ({"code_version": 2}, {"requirement_ids": [uuid4()]}, {"input_artifact_ids": []}):
            self.mutate_step(**changes)
            self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)
            self.mutate_step()

    def test_continuation_attempt_does_not_change_code_version_or_erase_old_receipt(self):
        initial = self.publish()
        self.mutate_step(attempt=1)
        continued = self.publish(execution_id=uuid4())
        self.assertEqual(initial.execution_manifest, continued.execution_manifest)
        self.assertEqual(self.store.get(self.run.run_id, initial.execution_manifest_id), initial)
        self.assertEqual(self.store.get(self.run.run_id, continued.execution_manifest_id), continued)

    def test_multiple_running_security_steps_denied(self):
        self.insert_step(WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.SECURITY, status=WorkflowStepStatus.RUNNING,
            code_version=1, requirement_ids=[self.source.requirement_ids[0]], input_artifact_ids=[self.source.artifact_id]))
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)

    def test_wrong_workspace_binding_denied(self):
        self.binding = replace(self.binding, workspace_id=uuid4())
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)

    def test_frozen_scanner_reference_null_or_mismatch_denied(self):
        self.replace_frozen_configuration(scanner_profile_ref=None)
        self.assert_code("SECURITY_SCAN_CONTEXT_DENIED", self.publish)

    def test_frozen_scanner_reference_change_denies_historical_read(self):
        record = self.publish()
        self.replace_frozen_configuration(scanner_profile_ref="https://scanner.example.invalid/other/v1")
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_source_actual_blob_hash_verified(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            content = connection.execute("SELECT content FROM artifact_contents WHERE artifact_id=?", (str(self.source.artifact_id),)).fetchone()[0]
            connection.execute("UPDATE artifact_contents SET content=? WHERE artifact_id=?", (sqlite3.Binary(b"x" + content[1:]), str(self.source.artifact_id)))
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.publish, execution_id=uuid4())
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_inventory_uses_frozen_source_not_current_working_copy(self):
        (self.root / "source" / "added.py").write_text("# Own fixture; not executed\n", encoding="utf-8")
        record = self.publish()
        self.assertEqual(record.report.scanned_files, ("main.py",))

    def test_pure_inventory_all_py_actual_hashes_and_nonempty_requirement(self):
        archive = _canonical_archive((('a.py', b'# Own fixture A\n', 0o644), ('b.py', b'# Own fixture B\n', 0o644), ('readme.md', b'fixture', 0o644)))
        rows, files = self.store.source_inventory(archive)
        self.assertEqual([row["path"] for row in rows], ["a.py", "b.py"])
        self.assertEqual(rows[0]["sha256"], sha256(files["a.py"]).hexdigest())
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.source_inventory, _canonical_archive((('readme.md', b'fixture', 0o644),)))
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.source_inventory, b'not a tar')

    def test_scanned_files_must_exactly_match_all_python_inventory(self):
        for files in (["another.py"], ["main.py", "missing.py"]):
            stdout = self.report_json(scannedFiles=files)
            self.report = parse_security_report(stdout, 0)
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, stdout=stdout)

    def test_findings_line_and_utf8_byte_column_bound_to_actual_source(self):
        for changes in ({"line": 2}, {"column": 10000}):
            stdout = self.report_json(findings=[self.finding(**changes)])
            self.report = parse_security_report(stdout, 1)
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, stdout=stdout, exit_code=1)

    def test_unicode_ast_byte_columns_and_encoding_cookie_are_not_character_counts(self):
        stdout = self.report_json(findings=[self.finding(line=2, column=12)])
        report = parse_security_report(stdout, 1)
        content = b'# coding: latin-1\n' + 'name="ééé"\n'.encode('latin-1')
        self.store._complete(report, self.scanner_profile, {"main.py": content})
        bad = parse_security_report(self.report_json(findings=[self.finding(line=2, column=14)]), 1)
        with self.assertRaises(ValueError):
            self.store._complete(bad, self.scanner_profile, {"main.py": content})

    def test_credential_bearing_source_names_fail_closed_without_rewriting_paths(self):
        archive = _canonical_archive((('password=fixture-secret.py', b'# Own fixture\n', 0o644),))
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.source_inventory, archive)

    def test_unicode_line_separator_in_literal_is_not_a_python_physical_line_break(self):
        content = 'name="\u2028";pass'.encode("utf-8")
        report = parse_security_report(self.report_json(findings=[self.finding(column=11)]), 1)
        self.store._complete(report, self.scanner_profile, {"main.py": content})
        report = parse_security_report(self.report_json(findings=[self.finding(line=2)]), 1)
        with self.assertRaises(ValueError):
            self.store._complete(report, self.scanner_profile, {"main.py": content})

    def test_profile_version_rules_reference_and_name_must_match(self):
        for changes in ({"scannerVersion": "1.8.7"}, {"ruleIds": ["B101"]}, {"profileName": "another"},
                        {"profileRef": "https://scanner.example.invalid/other/v1"}):
            stdout = self.report_json(**changes)
            self.report = parse_security_report(stdout, 0)
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, stdout=stdout)

    def test_inputs_hashes_rechecked_not_trusted_dataclass(self):
        for name in ("inputs_sha256", "runner_sha256", "contract_sha256", "host_configuration_sha256"):
            original = getattr(self.inputs, name)
            object.__setattr__(self.inputs, name, "a" * 64)
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish)
            object.__setattr__(self.inputs, name, original)

    def test_selected_profile_must_be_in_host_configuration(self):
        other = replace(self.scanner_profile, name="other")
        self.configuration = self.configuration_for(other)
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish)

    def test_nonempty_stderr_always_rejected_even_plain_text_or_credentials(self):
        for stderr in ("raw issue text", "unlabeled-private-source-value", "password=private-secret", "[REDACTED]"):
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, stderr=stderr)

    def test_result_identity_exit_and_manifest_rechecked(self):
        for changes in ({"run_id": uuid4()}, {"source_artifact_id": uuid4()}, {"tool_name": "run_build"}, {"profile_name": "other"},
                        {"container_id": "bad"}, {"duration_ms": True}, {"execution_id": uuid1()}, {"exit_code": 2}, {"exit_code": True}, {"stdout": "{}"}):
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, **changes)

    def test_report_argument_must_match_actual_stdout(self):
        self.report = parse_security_report(self.report_json(findings=[self.finding()]), 1)
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish)

    def test_fixed_execution_profile_argv_tool_and_name_checked(self):
        for changes in ({"argv": ("/usr/local/bin/python", "-c", "unsafe")}, {"tool_name": "run_unit_tests"}, {"name": "other"}):
            original = self.profile
            self.profile = replace(original, **changes)
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish)
            self.profile = original

    def test_host_limits_may_narrow_timeout_only(self):
        self.profile = replace(self.profile, limits=replace(self.profile.limits, timeout_seconds=30))
        self.publish()
        self.profile = replace(self.profile, limits=replace(self.profile.limits, timeout_seconds=61))
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, execution_id=uuid4())
        self.profile = replace(self.profile, limits=replace(self.configuration.limits, cpus=2))
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, execution_id=uuid4())

    def test_image_config_and_repository_digest_semantics(self):
        self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.publish, image_id="sha256:" + "e" * 64)
        self.configuration = self.configuration_for(self.scanner_profile, image_reference="registry.example.invalid/security@sha256:" + "d" * 64)
        self.profile = self.profile_for(self.configuration, self.scanner_profile)
        self.inputs = self.inputs_for(self.configuration, self.scanner_profile)
        self.assertEqual(self.publish(image_id="sha256:" + "e" * 64).image_id, "sha256:" + "e" * 64)

    def test_output_limits_preserve_error_not_partial_report(self):
        self.assert_code("SECURITY_SCAN_OUTPUT_LIMIT", self.publish, stderr="x" * (MAX_SECURITY_OUTPUT_BYTES + 1))
        self.profile = replace(self.profile, limits=replace(self.profile.limits, max_stdout_bytes=1))
        self.configuration = self.configuration_for(self.scanner_profile, limits=self.profile.limits)
        self.assert_code("SECURITY_SCAN_OUTPUT_LIMIT", self.publish)

    def test_sql_update_delete_and_replace_denied(self):
        self.publish()
        with self.repository._connection() as connection:
            for sql in ("UPDATE security_scan_execution_records SET stdout=x''", "DELETE FROM security_scan_execution_records",
                        "INSERT OR REPLACE INTO security_scan_execution_records SELECT * FROM security_scan_execution_records"):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(sql)

    def test_source_run_configuration_and_record_namespace_collisions(self):
        record = self.publish()
        for identity in (self.source.artifact_id, self.run_configuration.artifact_id, record.execution_id, record.execution_manifest_id):
            self.assert_code("SECURITY_SCAN_RECORD_CONFLICT", self.publish, execution_id=identity)
            with patch("mcp_tools.tools.security_store.uuid4", return_value=identity):
                self.assert_code("SECURITY_SCAN_RECORD_CONFLICT", self.publish, execution_id=uuid4())

    def test_manifest_cannot_equal_execution_and_duplicate_execution_denied(self):
        self.publish()
        self.assert_code("SECURITY_SCAN_RECORD_CONFLICT", self.publish)
        with patch("mcp_tools.tools.security_store.uuid4", return_value=self.result.execution_id):
            self.assert_code("SECURITY_SCAN_RECORD_CONFLICT", self.publish)

    def test_unit_namespace_collision_verified(self):
        self.mutate_step(status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_step(self.developer, status=WorkflowStepStatus.RUNNING)
        self.mutate_run(status=WorkflowStatus.IMPLEMENTING)
        unit = UnitTestOutputStore(self.repository).publish(self.fixture.binding, self.source, self.fixture.result,
            profile=self.fixture.profile, scope=self.fixture.scope, inputs=self.fixture.inputs, report=self.fixture.report)
        self.mutate_step(self.developer, status=WorkflowStepStatus.SUCCEEDED)
        self.mutate_step()
        self.mutate_run(status=WorkflowStatus.VALIDATING)
        self.assert_code("SECURITY_SCAN_RECORD_CONFLICT", self.publish, execution_id=unit.execution_manifest_id)

    def test_strict_uuid_not_found_and_report_reference_lexical_only(self):
        for invalid in (None, True, 5, uuid1(), "not-UUID"):
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.store.get, invalid, uuid4())
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.store.get, self.run.run_id, invalid)
        self.assert_code("SECURITY_SCAN_RECORD_NOT_FOUND", self.store.get, self.run.run_id, uuid4())
        record = self.publish()
        for reference in (None, "/etc/passwd", "file:///tmp/report.json", "https://example.invalid/report.json", record.report_ref + "?token=x",
                          record.report_ref + "#fragment", record.report_ref.upper(), record.report_ref.replace(".json", "%2ejson")):
            self.assert_code("SECURITY_SCAN_RESULT_INVALID", self.store.read_report, self.binding, reference)

    def test_metadata_and_blob_hashes_checked(self):
        record = self.publish()
        self.corrupt(record, metadata_sha256="a" * 64)
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_report_and_stdout_hash_corruption_rejected(self):
        record = self.publish()
        self.corrupt(record, report=sqlite3.Binary(b"{}"))
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.read_report, self.binding, record.report_ref)

    def test_unknown_metadata_and_input_hash_rejected_after_rehash(self):
        record = self.publish()
        data = self.metadata(record)
        data["confirmedSafe"] = True
        self.replace_metadata(record, data)
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)
        del data["confirmedSafe"]
        data["inputs"]["inputsSha256"] = "a" * 64
        self.replace_metadata(record, data)
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_source_inventory_hash_and_actual_inventory_rechecked(self):
        record = self.publish()
        data = self.metadata(record)
        data["sourceFiles"][0]["sha256"] = "a" * 64
        data["sourceFilesSha256"] = sha256(canonical(data["sourceFiles"]).encode()).hexdigest()
        self.replace_metadata(record, data)
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_host_policy_cannot_change_even_with_policy_and_metadata_rehash(self):
        record = self.publish()
        data = self.metadata(record)
        data["hostConfiguration"]["runner"]["scanner_version"] = "1.8.7"
        data["hostPolicySha256"] = sha256(canonical(data["hostConfiguration"]).encode()).hexdigest()
        self.replace_metadata(record, data)
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_historical_run_workspace_payload_identity_verified(self):
        record = self.publish()
        self.mutate_run(workspace_id=uuid4())
        self.assert_code("SECURITY_SCAN_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_storage_errors_and_repr_hide_source_host_and_sql(self):
        record = self.publish()
        for value in (self.directory.as_posix(), "/inputs/_security_runner.py", self.source.repository_id):
            self.assertNotIn(value, repr(record))
        for unknown in ("private SQL", [], None):
            self.assertEqual(str(SecurityStoreError(unknown)), "SECURITY_SCAN_STORAGE_ERROR")
        with patch.object(self.repository, "_transaction", side_effect=RuntimeError("private SQLite path")):
            self.assert_code("SECURITY_SCAN_STORAGE_ERROR", self.publish, execution_id=uuid4())
