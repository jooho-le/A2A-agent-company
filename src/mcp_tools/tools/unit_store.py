"""Private, immutable Unit Test receipts; never QA verdicts or Project Artifacts.

All files, profiles and report bytes are verified before the receipt is stored.
The Host-selected Source and active role step are checked again in the same
SQLite transaction. A returned reference cannot be a Working Copy path or URI
to an external service. Historical receipts remain readable after completion.
"""

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, replace
from hashlib import sha256
import json
import re
import sqlite3
from types import MappingProxyType
from uuid import UUID, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import _copy_profile
from mcp_tools.tools.unit_config import UnitTestScope, _copy_scope, MAX_UNIT_FILES, MAX_UNIT_FILE_BYTES, MAX_UNIT_TOTAL_BYTES
from mcp_tools.tools.unit_inputs import UnitTestInputs, _RUNNER_PATH, _test_path
from mcp_tools.tools.unit_report import UnitTestReport, parse_unit_report
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore, _canonical
from orchestrator.core.security import redact_text
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, ExecutionManifest
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits, SandboxResult
from orchestrator.sandbox.materialization import _check_names
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


MAX_UNIT_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_METADATA_BYTES = 512 * 1024
_MAX_REPORT_BYTES = 1024 * 1024
_CODES = frozenset({
    "UNIT_TEST_STORAGE_ERROR", "UNIT_TEST_RECORD_CONFLICT", "UNIT_TEST_RESULT_INVALID",
    "UNIT_TEST_RESULT_INTEGRITY_ERROR", "UNIT_TEST_CONTEXT_DENIED", "UNIT_TEST_OUTPUT_LIMIT",
    "UNIT_TEST_RECORD_NOT_FOUND",
})
_METADATA_KEYS = frozenset({
    "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId",
    "sourceArtifactId", "executionManifest", "role", "profileName", "toolName", "imageId",
    "containerId", "exitCode", "durationMs", "stdoutSha256", "stderrSha256",
    "stdoutSizeBytes", "stderrSizeBytes", "executionProfile", "scope", "inputs",
    "reportSha256", "reportSizeBytes",
})


class UnitTestStoreError(RuntimeError):
    """Expose only a stable code, never SQL, Source or Host paths."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _CODES else "UNIT_TEST_STORAGE_ERROR"
        super().__init__(self.code)


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID") from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _output(value):
    if not isinstance(value, str):
        raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
    try:
        if len(value.encode("utf-8")) > MAX_UNIT_OUTPUT_BYTES:
            raise UnitTestStoreError("UNIT_TEST_OUTPUT_LIMIT")
        safe = redact_text(value)
        encoded = safe.encode("utf-8")
        if len(encoded) > MAX_UNIT_OUTPUT_BYTES:
            raise UnitTestStoreError("UNIT_TEST_OUTPUT_LIMIT")
        return encoded
    except UnicodeError:
        raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID") from None


@dataclass(frozen=True, kw_only=True)
class UnitTestExecutionRecord:
    execution_manifest_id: UUID
    execution_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    execution_manifest: ExecutionManifest = field(repr=False)
    execution_profile: ExecutionProfile = field(repr=False)
    role: AgentRole
    profile_name: str
    scope: UnitTestScope = field(repr=False)
    inputs: Mapping = field(repr=False)
    report: UnitTestReport = field(repr=False)
    image_id: str
    container_id: str
    exit_code: int
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    stdout_sha256: str
    stderr_sha256: str
    report_sha256: str
    metadata_sha256: str

    @property
    def tool_name(self):
        return "run_unit_tests"

    @property
    def report_ref(self):
        # This is a private execution receipt, not a fabricated A2A Artifact.
        return f"artifact://{self.execution_manifest_id}/unit-test-report.json"

    def tool_output(self) -> dict:
        return {
            "total": self.report.total, "passed": self.report.passed,
            "failed": self.report.failed, "skipped": self.report.skipped,
            "reportRef": self.report_ref, "executionManifestId": str(self.execution_manifest_id),
        }


class UnitTestOutputStore:
    """Inert private Host capability for durable per-execution receipts."""

    def __init__(self, repository: SQLiteWorkflowRepository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        self._repository = repository

    def __repr__(self):
        return "UnitTestOutputStore()"

    @staticmethod
    def _ensure_schema(connection):
        for statement in (
            """CREATE TABLE IF NOT EXISTS unit_test_execution_records (
                execution_manifest_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
                metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256)=64),
                report BLOB NOT NULL CHECK (typeof(report)='blob' AND length(report)<=1048576),
                stdout BLOB NOT NULL CHECK (typeof(stdout)='blob' AND length(stdout)<=4194304),
                stderr BLOB NOT NULL CHECK (typeof(stderr)='blob' AND length(stderr)<=4194304),
                CHECK (execution_manifest_id != execution_id)
            )""",
            """CREATE INDEX IF NOT EXISTS unit_test_records_by_run
                ON unit_test_execution_records(run_id,execution_manifest_id)""",
            """CREATE TRIGGER IF NOT EXISTS unit_test_records_no_update
                BEFORE UPDATE ON unit_test_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Unit Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS unit_test_records_no_delete
                BEFORE DELETE ON unit_test_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Unit Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS unit_test_records_no_replace
                BEFORE INSERT ON unit_test_execution_records
                WHEN EXISTS (SELECT 1 FROM unit_test_execution_records
                    WHERE execution_manifest_id IN (NEW.execution_manifest_id,NEW.execution_id)
                        OR execution_id IN (NEW.execution_manifest_id,NEW.execution_id))
                BEGIN SELECT RAISE(ABORT,'immutable Unit Test execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS unit_test_records_ownership
                BEFORE INSERT ON unit_test_execution_records
                WHEN NOT EXISTS (SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id)
                    OR NOT EXISTS (SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                    OR NOT EXISTS (SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.source_artifact_id
                        AND run_id=NEW.run_id AND artifact_type='SOURCE')
                BEGIN SELECT RAISE(ABORT,'Unit Test ownership mismatch'); END""",
        ):
            connection.execute(statement)

    @staticmethod
    def _source(connection, binding, source):
        if not isinstance(source, CodeSnapshotArtifact) or source.run_id != binding.run_id:
            raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
        SQLiteArtifactContentStore._ensure_schema(connection)
        row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                 (str(source.artifact_id), str(binding.run_id))).fetchone()
        if row is None:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR")
        try:
            stored = SQLiteArtifactContentStore._decode(connection, row)
            expected, expected_json = _canonical(source)
            if (not isinstance(stored.metadata, CodeSnapshotArtifact)
                    or stored.metadata != expected or row["metadata_json"] != expected_json
                    or source.created_by is not AgentRole.DEVELOPER
                    or stored.media_type != "application/x-tar"
                    or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"):
                raise ValueError
            if binding.role is AgentRole.QA and connection.execute(
                "SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role='QA' AND access='READ_ONLY'",
                (str(source.artifact_id),),
            ).fetchone() is None:
                raise ValueError
            return stored
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _context(connection, binding, source, scope):
        if (not isinstance(binding, MCPBinding) or binding.role not in {AgentRole.DEVELOPER, AgentRole.QA}
                or binding.agent_role is not binding.role):
            raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
        if not isinstance(source, CodeSnapshotArtifact):
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        rows = [connection.execute(query, (str(identity),)).fetchone() for query, identity in (
            ("SELECT * FROM workflow_runs WHERE run_id=?", binding.run_id),
            ("SELECT * FROM workspaces WHERE workspace_id=?", binding.workspace_id),
            ("SELECT payload_json FROM run_configurations WHERE run_id=?", binding.run_id),
        )]
        if any(row is None for row in rows):
            raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
        try:
            run = WorkflowRun.model_validate_json(rows[0]["payload_json"])
            workspace = WorkspaceRecord.model_validate_json(rows[1]["payload_json"])
            configuration = RunConfigurationArtifact.model_validate_json(rows[2]["payload_json"])
            environment = configuration.configuration.environment
            allowed = {AgentRole.DEVELOPER: {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING},
                       AgentRole.QA: {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING}}
            if (run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                    or rows[0]["status"] != run.status.value or run.status not in allowed[binding.role]
                    or workspace.workspace_id != binding.workspace_id or workspace.run_id != binding.run_id
                    or rows[1]["run_id"] != str(binding.run_id)
                    or configuration.run_id != binding.run_id or configuration.workspace_id != binding.workspace_id
                    or configuration.scenario_id != run.scenario_id or environment is None
                    or environment.network_policy != "DENY" or source.code_version != run.fix_attempt + 1
                    or source.container_image_digest != environment.container_image_digest
                    or source.dependency_lock_hash != environment.dependency_lock_hash):
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            active = []
            for row in connection.execute("SELECT * FROM workflow_steps WHERE run_id=?", (str(binding.run_id),)):
                step = WorkflowStep.model_validate_json(row["payload_json"])
                if (str(step.workflow_step_id) != row["workflow_step_id"] or step.run_id != run.run_id
                        or step.status.value != row["status"]):
                    raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR")
                if (step.agent_role is binding.role and step.status is WorkflowStepStatus.RUNNING
                        and step.attempt == run.fix_attempt):
                    active.append(step)
            if len(active) != 1:
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            step = active[0]
            if (not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or step.code_version is not None and step.code_version != source.code_version):
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            if binding.role is AgentRole.DEVELOPER:
                if (step.workflow_step_id != source.workflow_step_id
                        or set(step.requirement_ids) != set(source.requirement_ids)
                        or source.a2a_task_id is not None and source.a2a_task_id != step.a2a_task_id):
                    raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            elif source.artifact_id not in step.input_artifact_ids:
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            if scope.kind == "PROTECTED" and (binding.role is not AgentRole.QA
                    or scope.protected_suite_ref != configuration.configuration.protected_test_suite_ref):
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            return step, UnitTestOutputStore._source(connection, binding, source)
        except UnitTestStoreError:
            raise
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _scope_payload(scope):
        validated = _copy_scope(scope)
        return {
            "name": validated.name, "kind": validated.kind, "pattern": validated.pattern,
            "source_directory": validated.source_directory,
            "protected_files": None if validated.protected_files is None else dict(validated.protected_files),
            "protected_suite_ref": validated.protected_suite_ref,
        }

    @staticmethod
    def _profile(profile):
        if type(profile) is not ExecutionProfile or profile.tool_name != "run_unit_tests":
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        # The existing Host path/credential/shell policy is identical; only the
        # fixed Tool identity differs. This does not execute or select a command.
        return replace(_copy_profile(replace(profile, tool_name="run_build")), tool_name="run_unit_tests")

    @staticmethod
    def _inputs_payload(inputs, scope):
        if type(inputs) is not UnitTestInputs:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        verified = UnitTestInputs(files=inputs.files, inputs_sha256=inputs.inputs_sha256,
                                  runner_sha256=inputs.runner_sha256, test_files_sha256=inputs.test_files_sha256)
        tests = {path: content for path, content in verified.files.items() if path != _RUNNER_PATH}
        if scope.kind == "SNAPSHOT" and tests:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        if scope.kind == "PROTECTED" and tests != {path: value.encode("utf-8") for path, value in scope.protected_files.items()}:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        payload = {
            "inputsSha256": verified.inputs_sha256, "runnerSha256": verified.runner_sha256,
            "testFilesSha256": verified.test_files_sha256,
            "files": [{"path": path, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)}
                      for path, content in verified.files.items()],
        }
        UnitTestOutputStore._verify_inputs_payload(payload, scope)
        return payload

    @staticmethod
    def _verify_inputs_payload(payload, scope):
        if type(payload) is not dict or set(payload) != {"inputsSha256", "runnerSha256", "testFilesSha256", "files"}:
            raise ValueError
        if (type(payload["files"]) is not list or not 1 <= len(payload["files"]) <= MAX_UNIT_FILES + 1
                or any(type(payload[name]) is not str or re.fullmatch(r"[0-9a-f]{64}", payload[name]) is None
                       for name in ("inputsSha256", "runnerSha256", "testFilesSha256"))):
            raise ValueError
        canonical = []
        for row in payload["files"]:
            if (type(row) is not dict or set(row) != {"path", "sha256", "sizeBytes"}
                    or type(row["sha256"]) is not str or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                    or type(row["sizeBytes"]) is not int or not 0 <= row["sizeBytes"] <= MAX_UNIT_FILE_BYTES):
                raise ValueError
            if row["path"] != _RUNNER_PATH:
                _test_path(row["path"])
            canonical.append({"path": row["path"], "sha256": row["sha256"], "sizeBytes": row["sizeBytes"]})
        names = [entry["path"] for entry in canonical]
        if (names != sorted(names) or len(set(names)) != len(names) or _RUNNER_PATH not in names
                or sum(entry["sizeBytes"] for entry in canonical) > MAX_UNIT_TOTAL_BYTES):
            raise ValueError
        _check_names(names)
        def digest(entries):
            return sha256(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
        tests = [entry for entry in canonical if entry["path"] != _RUNNER_PATH]
        runner = next(entry for entry in canonical if entry["path"] == _RUNNER_PATH)
        if sum(entry["sizeBytes"] for entry in tests) > MAX_UNIT_TOTAL_BYTES:
            raise ValueError
        if (payload["inputsSha256"] != digest(canonical) or payload["testFilesSha256"] != digest(tests)
                or payload["runnerSha256"] != runner["sha256"]):
            raise ValueError
        if scope.kind == "SNAPSHOT" and tests:
            raise ValueError
        if scope.kind == "PROTECTED":
            expected = [{"path": path, "sha256": sha256(content.encode("utf-8")).hexdigest(),
                         "sizeBytes": len(content.encode("utf-8"))}
                        for path, content in scope.protected_files.items()]
            if tests != expected:
                raise ValueError

    @staticmethod
    def _result_metadata(binding, source, result, manifest_id, step_id, profile, scope, inputs_payload, report):
        if (not isinstance(binding, MCPBinding) or binding.role not in {AgentRole.DEVELOPER, AgentRole.QA}
                or binding.agent_role is not binding.role or type(result) is not SandboxResult
                or not isinstance(source, CodeSnapshotArtifact) or type(report) is not UnitTestReport):
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        try:
            execution_id, manifest_id, step_id = _uuid(result.execution_id), _uuid(manifest_id), _uuid(step_id)
            if execution_id == manifest_id:
                raise UnitTestStoreError("UNIT_TEST_RECORD_CONFLICT")
            scope = _copy_scope(scope)
            profile = UnitTestOutputStore._profile(profile)
            UnitTestOutputStore._verify_inputs_payload(inputs_payload, scope)
            manifest = ExecutionManifest.model_validate(result.execution_manifest.model_dump(mode="json", by_alias=True))
            reference = profile.image_reference
            if reference is None:
                image_matches = result.image_id == manifest.container_image_digest
            elif reference.startswith("sha256:"):
                image_matches = reference == manifest.container_image_digest == result.image_id
            else:
                image_matches = reference.rsplit("@", 1)[-1] == manifest.container_image_digest
            if (binding.role not in scope.roles or result.run_id != binding.run_id
                    or result.source_artifact_id != source.artifact_id or result.tool_name != "run_unit_tests"
                    or source.run_id != binding.run_id or manifest != source.execution_manifest()
                    or profile.name != scope.name or result.profile_name != scope.name
                    or not image_matches or type(result.image_id) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None
                    or type(result.container_id) is not str or re.fullmatch(r"[0-9a-f]{64}", result.container_id) is None
                    or type(result.duration_ms) is not int or not 0 <= result.duration_ms <= 2**63 - 1):
                raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
            if profile.argv[1:] != ("-I", "-B", "/inputs/_unit_runner.py", "--kind", scope.kind,
                                    "--directory", scope.source_directory, "--pattern", scope.pattern):
                raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
            parsed = parse_unit_report(result.stdout, result.exit_code)
            if report != parsed:
                raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
            report_bytes = _json(parsed.to_dict()).encode("utf-8")
            # Parsing sanitizes details before serialization. Applying a generic
            # raw-JSON regex would corrupt the document, so store the verified
            # canonical report as stdout instead of rewriting JSON strings.
            stdout = report_bytes
            stderr = _output(result.stderr)
            if len(result.stdout.encode("utf-8")) > MAX_UNIT_OUTPUT_BYTES or len(report_bytes) > _MAX_REPORT_BYTES:
                raise UnitTestStoreError("UNIT_TEST_OUTPUT_LIMIT")
            metadata = {
                "executionManifestId": str(manifest_id), "executionId": str(execution_id),
                "runId": str(binding.run_id), "workspaceId": str(binding.workspace_id),
                "workflowStepId": str(step_id), "sourceArtifactId": str(source.artifact_id),
                "executionManifest": manifest.model_dump(mode="json", by_alias=True), "role": binding.role.value,
                "executionProfile": {"name": profile.name, "toolName": profile.tool_name,
                                     "argv": list(profile.argv), "limits": asdict(profile.limits),
                                     "imageReference": profile.image_reference},
                "profileName": result.profile_name, "toolName": result.tool_name,
                "imageId": result.image_id, "containerId": result.container_id,
                "exitCode": result.exit_code, "durationMs": result.duration_ms,
                "stdoutSha256": sha256(stdout).hexdigest(), "stderrSha256": sha256(stderr).hexdigest(),
                "stdoutSizeBytes": len(stdout), "stderrSizeBytes": len(stderr),
                "scope": UnitTestOutputStore._scope_payload(scope), "inputs": inputs_payload,
                "reportSha256": sha256(report_bytes).hexdigest(), "reportSizeBytes": len(report_bytes),
            }
            raw = _json(metadata)
            if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
            return metadata, raw, report_bytes, stdout, stderr
        except UnitTestStoreError:
            raise
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID") from None

    @staticmethod
    def _collision(connection, identities):
        for identity in identities:
            for query in (
                "SELECT 1 FROM artifact_contents WHERE artifact_id=?",
                "SELECT 1 FROM project_artifacts WHERE artifact_id=?",
                "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?",
                "SELECT 1 FROM unit_test_execution_records WHERE execution_manifest_id=? OR execution_id=?",
            ):
                args = (identity, identity) if " OR execution_id" in query else (identity,)
                if connection.execute(query, args).fetchone() is not None:
                    raise UnitTestStoreError("UNIT_TEST_RECORD_CONFLICT")
            for table in ("build_execution_records", "browser_test_execution_records"):
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
                    continue
                if connection.execute(f"SELECT 1 FROM {table} WHERE execution_manifest_id=? OR execution_id=?",
                                      (identity, identity)).fetchone() is not None:
                    raise UnitTestStoreError("UNIT_TEST_RECORD_CONFLICT")

    def publish(self, binding: MCPBinding, source: CodeSnapshotArtifact, result: SandboxResult,
                *, profile: ExecutionProfile, scope: UnitTestScope, inputs: UnitTestInputs,
                report: UnitTestReport) -> UnitTestExecutionRecord:
        try:
            if (not isinstance(binding, MCPBinding) or binding.role not in {AgentRole.DEVELOPER, AgentRole.QA}
                    or binding.agent_role is not binding.role):
                raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
            try:
                scope = _copy_scope(scope)
                inputs_payload = self._inputs_payload(inputs, scope)
            except Exception:
                raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID") from None
            manifest_id = _uuid(uuid4())
            with self._repository._transaction() as connection:
                step, _stored = self._context(connection, binding, source, scope)
                metadata, raw, report_bytes, stdout, stderr = self._result_metadata(
                    binding, source, result, manifest_id, step.workflow_step_id, profile, scope, inputs_payload, report)
                self._ensure_schema(connection)
                self._collision(connection, (str(manifest_id), metadata["executionId"]))
                digest = sha256(raw.encode("utf-8")).hexdigest()
                connection.execute(
                    "INSERT INTO unit_test_execution_records(execution_manifest_id,execution_id,run_id,workspace_id,workflow_step_id,"
                    "source_artifact_id,metadata_json,metadata_sha256,report,stdout,stderr) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (str(manifest_id), metadata["executionId"], str(binding.run_id), str(binding.workspace_id),
                     str(step.workflow_step_id), str(source.artifact_id), raw, digest,
                     sqlite3.Binary(report_bytes), sqlite3.Binary(stdout), sqlite3.Binary(stderr)),
                )
                row = connection.execute("SELECT * FROM unit_test_execution_records WHERE execution_manifest_id=?", (str(manifest_id),)).fetchone()
                return self._decode(connection, row)
        except UnitTestStoreError:
            raise
        except sqlite3.IntegrityError:
            raise UnitTestStoreError("UNIT_TEST_RECORD_CONFLICT") from None
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_STORAGE_ERROR") from None

    @staticmethod
    def _decode(connection, row):
        try:
            raw = row["metadata_json"]
            if type(raw) is not str or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            metadata = json.loads(raw)
            if (type(metadata) is not dict or frozenset(metadata) != _METADATA_KEYS
                    or _json(metadata) != raw or sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"]):
                raise ValueError
            identities = {key: _uuid(metadata[key]) for key in (
                "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId")}
            for key, column in (("executionManifestId", "execution_manifest_id"), ("executionId", "execution_id"),
                                ("runId", "run_id"), ("workspaceId", "workspace_id"),
                                ("workflowStepId", "workflow_step_id"), ("sourceArtifactId", "source_artifact_id")):
                if metadata[key] != row[column] or str(identities[key]) != row[column]:
                    raise ValueError
            role = AgentRole(metadata["role"])
            binding = MCPBinding(role=role, agent_role=role, run_id=identities["runId"], workspace_id=identities["workspaceId"])
            scope_data = metadata["scope"]
            if (type(scope_data) is not dict or set(scope_data) != {"name", "kind", "pattern", "source_directory", "protected_files", "protected_suite_ref"}):
                raise ValueError
            scope = UnitTestScope(**scope_data)
            profile_data = metadata["executionProfile"]
            if (type(profile_data) is not dict or set(profile_data) != {"name", "toolName", "argv", "limits", "imageReference"}
                    or type(profile_data["argv"]) is not list or type(profile_data["limits"]) is not dict):
                raise ValueError
            profile = ExecutionProfile(name=profile_data["name"], tool_name=profile_data["toolName"],
                                       argv=tuple(profile_data["argv"]), limits=SandboxLimits(**profile_data["limits"]),
                                       image_reference=profile_data["imageReference"])
            source_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                            (row["source_artifact_id"], row["run_id"])).fetchone()
            if source_row is None:
                raise ValueError
            source = SQLiteArtifactContentStore._decode(connection, source_row).metadata
            if not isinstance(source, CodeSnapshotArtifact):
                raise ValueError
            UnitTestOutputStore._source(connection, binding, source)
            workspace_row = connection.execute("SELECT run_id,payload_json FROM workspaces WHERE workspace_id=?", (row["workspace_id"],)).fetchone()
            step_row = connection.execute("SELECT run_id,payload_json FROM workflow_steps WHERE workflow_step_id=?", (row["workflow_step_id"],)).fetchone()
            if workspace_row is None or step_row is None:
                raise ValueError
            workspace = WorkspaceRecord.model_validate_json(workspace_row["payload_json"])
            step = WorkflowStep.model_validate_json(step_row["payload_json"])
            if (workspace_row["run_id"] != row["run_id"] or str(workspace.run_id) != row["run_id"]
                    or str(workspace.workspace_id) != row["workspace_id"]
                    or step_row["run_id"] != row["run_id"] or str(step.run_id) != row["run_id"]
                    or str(step.workflow_step_id) != row["workflow_step_id"] or step.agent_role is not role):
                raise ValueError
            if (step.attempt != source.code_version - 1 or not step.requirement_ids
                    or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or step.code_version is not None and step.code_version != source.code_version):
                raise ValueError
            if role is AgentRole.DEVELOPER:
                if (step.workflow_step_id != source.workflow_step_id
                        or set(step.requirement_ids) != set(source.requirement_ids)):
                    raise ValueError
            elif source.artifact_id not in step.input_artifact_ids:
                raise ValueError
            streams = []
            for name in ("stdout", "stderr", "report"):
                content = row[name]
                maximum = _MAX_REPORT_BYTES if name == "report" else MAX_UNIT_OUTPUT_BYTES
                if (type(content) is not bytes or len(content) > maximum
                        or type(metadata[name + "SizeBytes"]) is not int or metadata[name + "SizeBytes"] != len(content)
                        or metadata[name + "Sha256"] != sha256(content).hexdigest()):
                    raise ValueError
                text = content.decode("utf-8")
                if name == "stderr" and redact_text(text) != text:
                    raise ValueError
                streams.append(text)
            report = parse_unit_report(streams[2], metadata["exitCode"])
            if _json(report.to_dict()) != streams[2] or streams[0] != streams[2]:
                raise ValueError
            result = SandboxResult(
                execution_id=identities["executionId"], run_id=identities["runId"], source_artifact_id=identities["sourceArtifactId"],
                profile_name=metadata["profileName"], tool_name=metadata["toolName"],
                execution_manifest=ExecutionManifest.model_validate(metadata["executionManifest"]),
                image_id=metadata["imageId"], container_id=metadata["containerId"], exit_code=metadata["exitCode"],
                duration_ms=metadata["durationMs"], stdout=streams[0], stderr=streams[1],
            )
            _metadata, expected, _report, _stdout, _stderr = UnitTestOutputStore._result_metadata(
                binding, source, result, identities["executionManifestId"], identities["workflowStepId"],
                profile, scope, metadata["inputs"], report)
            if raw != expected:
                raise ValueError
            if scope.kind == "PROTECTED":
                config_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (row["run_id"],)).fetchone()
                configuration = RunConfigurationArtifact.model_validate_json(config_row["payload_json"])
                if scope.protected_suite_ref != configuration.configuration.protected_test_suite_ref:
                    raise ValueError
            def freeze(value):
                if type(value) is dict:
                    return MappingProxyType({key: freeze(item) for key, item in value.items()})
                if type(value) is list:
                    return tuple(freeze(item) for item in value)
                return value
            return UnitTestExecutionRecord(
                execution_manifest_id=identities["executionManifestId"], execution_id=identities["executionId"],
                run_id=identities["runId"], workspace_id=identities["workspaceId"],
                workflow_step_id=identities["workflowStepId"], source_artifact_id=identities["sourceArtifactId"],
                execution_manifest=result.execution_manifest, execution_profile=profile, role=role,
                profile_name=result.profile_name, scope=scope, inputs=freeze(metadata["inputs"]), report=report,
                image_id=result.image_id, container_id=result.container_id, exit_code=result.exit_code,
                duration_ms=result.duration_ms, stdout=streams[0], stderr=streams[1],
                stdout_sha256=metadata["stdoutSha256"], stderr_sha256=metadata["stderrSha256"],
                report_sha256=metadata["reportSha256"], metadata_sha256=row["metadata_sha256"],
            )
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR") from None

    @staticmethod
    def _get(connection, run_id, execution_manifest_id):
        SQLiteArtifactContentStore._ensure_schema(connection)
        UnitTestOutputStore._ensure_schema(connection)
        row = connection.execute("SELECT * FROM unit_test_execution_records WHERE run_id=? AND execution_manifest_id=?",
                                 (str(run_id), str(execution_manifest_id))).fetchone()
        if row is None:
            raise UnitTestStoreError("UNIT_TEST_RECORD_NOT_FOUND")
        return UnitTestOutputStore._decode(connection, row)

    def get(self, run_id, execution_manifest_id) -> UnitTestExecutionRecord:
        run_id, execution_manifest_id = _uuid(run_id), _uuid(execution_manifest_id)
        try:
            with self._repository._transaction() as connection:
                return self._get(connection, run_id, execution_manifest_id)
        except UnitTestStoreError:
            raise
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_STORAGE_ERROR") from None

    def read_report(self, binding: MCPBinding, report_ref) -> dict:
        if (not isinstance(binding, MCPBinding) or binding.role is not AgentRole.QA
                or binding.agent_role is not AgentRole.QA):
            raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
        if type(report_ref) is not str:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        match = re.fullmatch(r"artifact://([0-9a-f-]{36})/unit-test-report\.json", report_ref)
        if match is None:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        manifest_id = _uuid(match[1])
        if str(manifest_id) != match[1]:
            raise UnitTestStoreError("UNIT_TEST_RESULT_INVALID")
        try:
            with self._repository._transaction() as connection:
                record = self._get(connection, binding.run_id, manifest_id)
                if record.workspace_id != binding.workspace_id:
                    raise UnitTestStoreError("UNIT_TEST_CONTEXT_DENIED")
                source_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                                (str(record.source_artifact_id), str(binding.run_id))).fetchone()
                if source_row is None:
                    raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR")
                try:
                    source = SQLiteArtifactContentStore._decode(connection, source_row).metadata
                    # The receipt's publishing role may be Developer. Report
                    # access must still use the current reader's QA grant;
                    # completed Runs require no currently active QA step.
                    self._source(connection, binding, source)
                except Exception:
                    raise UnitTestStoreError("UNIT_TEST_RESULT_INTEGRITY_ERROR") from None
                return record.report.to_dict()
        except UnitTestStoreError:
            raise
        except Exception:
            raise UnitTestStoreError("UNIT_TEST_STORAGE_ERROR") from None
