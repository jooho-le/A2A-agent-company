"""Real Git/SQLite Build output storage; never execute product code or Docker."""

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
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError, MAX_BUILD_OUTPUT_BYTES
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import (
    AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import ExecutionBaseline, RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits, SandboxResult
from orchestrator.workspaces.registry import WorkspaceRegistry


class BuildOutputStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-build-store-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.base = self.directory / "workspaces"
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.lock = b"local-fixture==1\n"
        self.run, self.step, self.root, self.configuration = self.make_run()
        source_dir = self.root / "source"
        (source_dir / "requirements.lock").write_bytes(self.lock)
        (source_dir / "main.py").write_text("raise RuntimeError('Do not execute this Source')\n", encoding="utf-8")
        for args in (("init", "--object-format=sha1"), ("add", "main.py", "requirements.lock"), ("commit", "-m", "immutable fixture")):
            self.git(source_dir, *args)
        commit = self.git(source_dir, "rev-parse", "HEAD").strip()
        artifacts = ArtifactStore(self.repository, self.registry)
        self.source = artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=self.step.workflow_step_id, commit_hash=commit,
            repository_id="build-store-fixture", lock_path="requirements.lock",
        )
        self.binding = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                                  run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
                                        argv=("/usr/local/bin/fixture", "--compile"),
                                        limits=SandboxLimits(timeout_seconds=17.0, control_timeout_seconds=2.0),
                                        image_reference="sha256:" + "d" * 64)
        self.result = SandboxResult(
            execution_id=uuid4(), run_id=self.run.run_id, source_artifact_id=self.source.artifact_id,
            profile_name=self.profile.name, tool_name="run_build", execution_manifest=self.source.execution_manifest(),
            image_id="sha256:" + "d" * 64, container_id="c" * 64, exit_code=0,
            duration_ms=123, stdout="정상 Build 출력\n", stderr="",
        )
        self.store = BuildOutputStore(self.repository)

    def make_run(self):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입", status=WorkflowStatus.IMPLEMENTING)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING,
                            code_version=1, requirement_ids=list(SCENARIO_REGISTRY[SCN_001_ID].requirement_ids))
        configuration = RunConfigurationArtifact(
            run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
            configuration=RunConfiguration(environment=ExecutionBaseline(
                container_image_digest="sha256:" + "d" * 64,
                dependency_lock_hash="sha256:" + sha256(self.lock).hexdigest(), hardware_profile="storage-fixture")),
        )
        root = self.base / str(run.workspace_id)
        workspace = WorkspaceRecord(run_id=run.run_id, workspace_id=run.workspace_id, root_path=str(root))
        self.repository.create_run(run, (step,), (), workspace=workspace, run_configuration=configuration)
        self.registry.provision(run.workspace_id, run_id=run.run_id)
        return run, step, root, configuration

    @staticmethod
    def git(directory, *arguments):
        return subprocess.run(
            ["git", "-c", "user.name=Build Test", "-c", "user.email=fixture@example.invalid",
             "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
            cwd=directory, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=20, text=True,
        ).stdout

    def publish(self, **changes):
        return self.store.publish(self.binding, self.source, replace(self.result, **changes), profile=self.profile)

    def assert_code(self, code, operation, *args, **kwargs):
        with self.assertRaises(BuildStoreError) as raised:
            operation(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        self.assertEqual(str(raised.exception), code)

    def mutate_run(self, **changes):
        run = WorkflowRun.model_validate({**self.run.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                               (run.status.value, run.model_dump_json(), str(run.run_id)))

    def mutate_step(self, **changes):
        step = WorkflowStep.model_validate({**self.step.model_dump(), **changes})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                               (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def mutate_configuration(self, configuration):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?",
                               (configuration.model_dump_json(), str(self.run.run_id)))

    def corrupt_record(self, manifest_id, column, value):
        allowed = {"metadata_json", "metadata_sha256", "stdout", "stderr", "workspace_id", "run_id", "execution_id"}
        self.assertIn(column, allowed)
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER build_execution_records_no_update")
            connection.execute(f"UPDATE build_execution_records SET {column}=? WHERE execution_manifest_id=?", (value, str(manifest_id)))

    def test_constructor_performs_no_io_or_schema_install(self):
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("must stay inert")):
            store = BuildOutputStore(self.repository)
        self.assertEqual(repr(store), "BuildOutputStore()")
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='build_execution_records'").fetchone())

    def test_constructor_rejects_non_repository(self):
        self.assert_code("BUILD_RESULT_INVALID", BuildOutputStore, object())

    def test_real_immutable_record_outputs_and_manifest(self):
        record = self.publish()
        self.assertEqual(record.execution_manifest_id.version, 4)
        self.assertNotEqual(record.execution_manifest_id, self.result.execution_id)
        self.assertEqual(record.execution_id, self.result.execution_id)
        self.assertEqual(record.execution_manifest, self.source.execution_manifest())
        self.assertEqual(record.execution_profile, self.profile)
        self.assertEqual(record.image_id, self.result.image_id)
        self.assertEqual(record.container_id, self.result.container_id)
        self.assertEqual(record.stdout_sha256, sha256(self.result.stdout.encode()).hexdigest())
        self.assertEqual(record.stderr_sha256, sha256(b"").hexdigest())
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        self.assertEqual(self.store.read_output(self.run.run_id, record.stdout_ref), self.result.stdout)
        self.assertEqual(self.store.read_output(self.run.run_id, record.stderr_ref), "")
        self.assertEqual(record.tool_output(), {
            "exitCode": 0, "stdoutRef": record.stdout_ref, "stderrRef": record.stderr_ref,
            "durationMs": 123, "executionManifestId": str(record.execution_manifest_id),
        })
        with self.assertRaises(FrozenInstanceError):
            record.exit_code = 12

    def test_record_is_not_a_project_artifact_and_does_not_set_verdict(self):
        record = self.publish()
        self.assertFalse(record.stdout_ref.startswith("artifact://"))
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        self.assertEqual(self.repository.get_run(self.run.run_id).status, WorkflowStatus.IMPLEMENTING)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_nonzero_build_is_stored_without_product_verdict(self):
        record = self.publish(exit_code=2, stderr="Syntax error\n")
        self.assertEqual(record.tool_output()["exitCode"], 2)
        self.assertEqual(self.store.read_output(self.run.run_id, record.stderr_ref), "Syntax error\n")
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    def test_output_is_sanitized_before_publication_and_checksum(self):
        raw = "password='never-store-this'\nAuthorization: Bearer private.jwt.token\n"
        record = self.publish(stdout=raw, stderr="api_key=private-api-value\n")
        self.assertNotIn("never-store-this", record.stdout)
        self.assertNotIn("private.jwt.token", record.stdout)
        self.assertNotIn("private-api-value", record.stderr)
        self.assertIn("[REDACTED]", record.stdout)
        self.assertEqual(record.stdout_sha256, sha256(record.stdout.encode()).hexdigest())
        with self.repository._connection() as connection:
            row = connection.execute("SELECT * FROM build_execution_records").fetchone()
        self.assertNotIn(b"never-store-this", row["stdout"])
        self.assertEqual(row["metadata_sha256"], sha256(row["metadata_json"].encode()).hexdigest())

    def test_sensitive_strings_and_host_paths_are_not_in_repr_or_error(self):
        record = self.publish(stdout="private Source output", stderr="private error output")
        for secret in (self.directory.as_posix(), "private Source output", "private error output", "--compile", self.source.repository_id):
            self.assertNotIn(secret, repr(record))
            self.assertNotIn(secret, repr(self.store))
        self.assertEqual(str(BuildStoreError("private error output")), "BUILD_STORAGE_ERROR")
        self.assertEqual(str(BuildStoreError([])), "BUILD_STORAGE_ERROR")

    def test_duplicate_sandbox_execution_is_not_published_again(self):
        first = self.publish()
        self.assert_code("BUILD_RECORD_CONFLICT", self.publish)
        self.assertEqual(self.store.get(self.run.run_id, first.execution_manifest_id), first)

    def test_multiple_actual_executions_of_same_snapshot_have_separate_ids(self):
        first = self.publish()
        second = self.publish(execution_id=uuid4(), container_id="b" * 64)
        self.assertNotEqual(first.execution_manifest_id, second.execution_manifest_id)
        self.assertEqual(first.source_artifact_id, second.source_artifact_id)

    def test_duplicate_manifest_id_is_rejected(self):
        first = self.publish()
        with patch("mcp_tools.tools.build_store.uuid4", return_value=first.execution_manifest_id):
            self.assert_code("BUILD_RECORD_CONFLICT", self.publish, execution_id=uuid4())

    def test_manifest_id_collision_with_source_id_is_rejected(self):
        with patch("mcp_tools.tools.build_store.uuid4", return_value=self.source.artifact_id):
            self.assert_code("BUILD_RECORD_CONFLICT", self.publish)

    def test_manifest_and_sandbox_execution_id_must_be_distinct(self):
        with patch("mcp_tools.tools.build_store.uuid4", return_value=self.result.execution_id):
            self.assert_code("BUILD_RECORD_CONFLICT", self.publish)

    def test_sql_triggers_forbid_update_delete_and_replace(self):
        self.publish()
        with self.repository._connection() as connection:
            for statement in (
                "UPDATE build_execution_records SET stdout=x''",
                "DELETE FROM build_execution_records",
                "INSERT OR REPLACE INTO build_execution_records SELECT * FROM build_execution_records",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)

    def test_wrong_run_cannot_get_or_read_outputs(self):
        record = self.publish()
        other, _step, _root, _configuration = self.make_run()
        self.assert_code("BUILD_RECORD_NOT_FOUND", self.store.get, other.run_id, record.execution_manifest_id)
        self.assert_code("BUILD_RECORD_NOT_FOUND", self.store.read_output, other.run_id, record.stdout_ref)

    def test_uuid_validation_is_strict(self):
        record = self.publish()
        for invalid in (uuid1(), "not-an-id", True, 123, None):
            self.assert_code("BUILD_RESULT_INVALID", self.store.get, invalid, record.execution_manifest_id)
            self.assert_code("BUILD_RESULT_INVALID", self.store.get, self.run.run_id, invalid)

    def test_invalid_uri_is_never_fetched_or_resolved(self):
        record = self.publish()
        for invalid in (
            record.stdout_ref + "?token=value", record.stdout_ref + "#fragment",
            record.stdout_ref.replace("stdout.txt", "../stdout.txt"),
            record.stdout_ref.replace("stdout.txt", "stdout%2etxt"),
            record.stdout_ref.replace("execution://", "file://"),
            record.stdout_ref.upper(), "/etc/passwd", "https://example.invalid", None,
        ):
            self.assert_code("BUILD_RESULT_INVALID", self.store.read_output, self.run.run_id, invalid)

    def test_unpublished_manifest_is_not_found(self):
        self.assert_code("BUILD_RECORD_NOT_FOUND", self.store.get, self.run.run_id, uuid4())

    def test_role_rejection(self):
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            binding = MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
            self.assert_code("BUILD_CONTEXT_DENIED", self.store.publish, binding, self.source, self.result, profile=self.profile)

    def test_cancelled_run_cannot_publish(self):
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="cancel fixture")
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_cancelled_or_completed_step_cannot_publish(self):
        for status in (WorkflowStepStatus.CANCELED, WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.FAILED, WorkflowStepStatus.WAITING_INPUT):
            self.mutate_step(status=status)
            self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_fix_attempt_change_cannot_publish_old_result(self):
        self.mutate_run(fix_attempt=1, status=WorkflowStatus.FIXING)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_code_version_and_attempt_step_changes_are_denied(self):
        self.mutate_step(code_version=2)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)
        self.mutate_step(code_version=1, attempt=1)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_role_and_requirement_step_changes_are_denied(self):
        self.mutate_step(agent_role=AgentRole.QA)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)
        self.mutate_step(agent_role=AgentRole.DEVELOPER, requirement_ids=[])
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_wrong_workspace_binding_is_denied(self):
        other, _step, _root, _configuration = self.make_run()
        binding = replace(self.binding, workspace_id=other.workspace_id)
        self.assert_code("BUILD_CONTEXT_DENIED", self.store.publish, binding, self.source, self.result, profile=self.profile)

    def test_unstaged_or_forged_source_is_denied(self):
        source = self.source.model_copy(update={"artifact_id": uuid4()})
        result = replace(self.result, source_artifact_id=source.artifact_id, execution_manifest=source.execution_manifest())
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.publish, self.binding, source, result, profile=self.profile)
        source = self.source.model_copy(update={"commit_hash": "a" * 40})
        result = replace(self.result, execution_manifest=source.execution_manifest())
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.publish, self.binding, source, result, profile=self.profile)

    def test_environment_change_is_denied(self):
        environment = self.configuration.configuration.environment.model_copy(update={"dependency_lock_hash": "sha256:" + "b" * 64})
        configuration = self.configuration.model_copy(update={"configuration": self.configuration.configuration.model_copy(update={"environment": environment})})
        self.mutate_configuration(configuration)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_allowlist_network_configuration_is_not_silently_accepted(self):
        environment = self.configuration.configuration.environment.model_copy(update={"network_policy": "ALLOWLIST", "allowed_hosts": ("fixture.invalid",)})
        configuration = self.configuration.model_copy(update={"configuration": self.configuration.configuration.model_copy(update={"environment": environment})})
        self.mutate_configuration(configuration)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_missing_environment_is_denied(self):
        configuration = self.configuration.model_copy(update={"configuration": self.configuration.configuration.model_copy(update={"environment": None})})
        self.mutate_configuration(configuration)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_corrupt_staged_source_bytes_block_publication(self):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET content=?,size_bytes=? WHERE artifact_id=?",
                               (sqlite3.Binary(b"bad"), 3, str(self.source.artifact_id)))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.publish)

    def test_run_cancellation_is_rechecked_inside_publication_transaction(self):
        original = self.store._result_metadata
        def prepare(*args):
            output = original(*args)
            self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="concurrent cancellation")
            return output
        with patch.object(self.store, "_result_metadata", side_effect=prepare):
            self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_invalid_result_fields_have_safe_errors(self):
        for changes in (
            {"execution_id": uuid1()}, {"run_id": uuid4()}, {"source_artifact_id": uuid4()},
            {"tool_name": "run_unit_tests"}, {"profile_name": "unexpected"},
            {"image_id": "mutable:latest"}, {"container_id": "short-id"},
            {"exit_code": True}, {"exit_code": -1}, {"exit_code": 256},
            {"duration_ms": True}, {"duration_ms": -1}, {"stdout": b"not-text"}, {"stdout": "\ud800"},
        ):
            with self.subTest(changes=tuple(changes)):
                self.assert_code("BUILD_RESULT_INVALID", self.publish, **changes)

    def test_result_manifest_must_match_actual_frozen_source(self):
        manifest = self.source.execution_manifest().model_copy(update={"snapshot_sha256": "e" * 64})
        self.assert_code("BUILD_RESULT_INVALID", self.publish, execution_manifest=manifest)

    def test_without_explicit_image_reference_requires_actual_config_id_match(self):
        profile = replace(self.profile, image_reference=None)
        record = self.store.publish(self.binding, self.source, self.result, profile=profile)
        self.assertEqual(record.image_id, self.source.container_image_digest)
        result = replace(self.result, execution_id=uuid4(), image_id="sha256:" + "f" * 64)
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, result, profile=profile)

    def test_explicit_config_reference_and_manifest_and_actual_image_must_match(self):
        result = replace(self.result, image_id="sha256:" + "f" * 64)
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, result, profile=self.profile)
        profile = replace(self.profile, image_reference="sha256:" + "b" * 64)
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, self.result, profile=profile)

    def test_repository_distribution_digest_may_have_different_actual_config_id(self):
        profile = replace(self.profile, image_reference="registry.example.invalid/build@" + self.source.container_image_digest)
        result = replace(self.result, image_id="sha256:" + "f" * 64)
        record = self.store.publish(self.binding, self.source, result, profile=profile)
        self.assertEqual(record.image_id, result.image_id)
        self.assertEqual(self.store.get(self.run.run_id, record.execution_manifest_id), record)
        mismatched = replace(profile, image_reference="registry.example.invalid/build@sha256:" + "b" * 64)
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, result, profile=mismatched)

    def test_decode_rechecks_actual_config_id_even_when_metadata_hash_is_recomputed(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT metadata_json FROM build_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["imageId"] = "sha256:" + "f" * 64
            raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute("DROP TRIGGER build_execution_records_no_update")
            connection.execute("UPDATE build_execution_records SET metadata_json=?,metadata_sha256=? WHERE execution_manifest_id=?",
                               (raw, sha256(raw.encode()).hexdigest(), str(record.execution_manifest_id)))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_new_second_running_developer_step_blocks_publication(self):
        other = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER, status=WorkflowStepStatus.RUNNING,
                             attempt=self.run.fix_attempt, code_version=1, requirement_ids=list(self.step.requirement_ids))
        from orchestrator.infrastructure.sqlite_workflows import _insert_step
        with self.repository._transaction() as connection:
            _insert_step(connection, other)
        self.assert_code("BUILD_CONTEXT_DENIED", self.publish)

    def test_invalid_unicode_source_identity_never_exposes_encoder_error(self):
        source = self.source.model_copy(update={"repository_id": "\ud800"})
        manifest = self.result.execution_manifest.model_copy(update={"repository_id": "\ud800"})
        result = replace(self.result, execution_manifest=manifest)
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, source, result, profile=self.profile)

    def test_secret_profile_arguments_are_rejected_not_silently_changed(self):
        profile = replace(self.profile, argv=("/usr/local/bin/fixture", "password=private-value"))
        self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, self.result, profile=profile)

    def test_separated_secret_flags_and_shell_profiles_are_rejected(self):
        for argv in (("/usr/local/bin/fixture", "--password", "private-value"), ("/bin/sh", "-c", "true")):
            profile = replace(self.profile, argv=argv)
            self.assert_code("BUILD_RESULT_INVALID", self.store.publish, self.binding, self.source, self.result, profile=profile)

    def test_narrowed_profile_limits_are_preserved(self):
        record = self.publish()
        self.assertEqual(record.execution_profile.limits.timeout_seconds, 17.0)
        self.assertEqual(record.execution_profile.limits.control_timeout_seconds, 2.0)
        with self.repository._connection() as connection:
            metadata = json.loads(connection.execute("SELECT metadata_json FROM build_execution_records").fetchone()[0])
        self.assertEqual(metadata["executionProfile"]["argv"], list(self.profile.argv))

    def test_long_valid_host_profile_has_room_for_source_receipt_metadata(self):
        # Host policy JSON has a 16 KiB limit; receipt metadata must leave
        # additional room for the Source manifest, identities and stream hashes.
        profile = replace(self.profile, argv=("/usr/local/bin/fixture", "x" * 8192, "y" * 6700))
        record = self.store.publish(self.binding, self.source, self.result, profile=profile)
        self.assertEqual(record.execution_profile, profile)

    def test_stdout_and_stderr_bounded_independently(self):
        for stream in ("stdout", "stderr"):
            self.assert_code("BUILD_OUTPUT_LIMIT", self.publish, **{stream: "x" * (MAX_BUILD_OUTPUT_BYTES + 1)})

    def test_output_text_corruption_is_detected(self):
        record = self.publish()
        self.corrupt_record(record.execution_manifest_id, "stdout", sqlite3.Binary(b"modified"))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_utf8_corruption_is_detected(self):
        record = self.publish()
        self.corrupt_record(record.execution_manifest_id, "stderr", sqlite3.Binary(b"\xff"))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.read_output, self.run.run_id, record.stderr_ref)

    def test_metadata_hash_and_noncanonical_json_corruption_are_detected(self):
        record = self.publish()
        self.corrupt_record(record.execution_manifest_id, "metadata_sha256", "0" * 64)
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_canonical_metadata_and_rows_are_cross_checked(self):
        record = self.publish()
        self.corrupt_record(record.execution_manifest_id, "execution_id", str(uuid4()))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_decode_rejects_conflated_manifest_and_actual_execution_id(self):
        record = self.publish()
        with self.repository._transaction() as connection:
            row = connection.execute("SELECT metadata_json FROM build_execution_records WHERE execution_manifest_id=?", (str(record.execution_manifest_id),)).fetchone()
            metadata = json.loads(row["metadata_json"])
            metadata["executionId"] = str(record.execution_manifest_id)
            raw = json.dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            connection.execute("DROP TRIGGER build_execution_records_no_update")
            connection.execute("UPDATE build_execution_records SET execution_id=?,metadata_json=?,metadata_sha256=? WHERE execution_manifest_id=?",
                               (str(record.execution_manifest_id), raw, sha256(raw.encode()).hexdigest(), str(record.execution_manifest_id)))
        self.assert_code("BUILD_RESULT_INTEGRITY_ERROR", self.store.get, self.run.run_id, record.execution_manifest_id)

    def test_old_outputs_remain_readable_after_run_is_cancelled(self):
        record = self.publish()
        self.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="later cancellation")
        self.assertEqual(self.store.read_output(self.run.run_id, record.stdout_ref), self.result.stdout)

    def test_publication_failure_rolls_back_streams_and_schema(self):
        with patch.object(self.store, "_decode", side_effect=RuntimeError("private host failure")):
            self.assert_code("BUILD_STORAGE_ERROR", self.publish)
        with self.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='build_execution_records'").fetchone())
