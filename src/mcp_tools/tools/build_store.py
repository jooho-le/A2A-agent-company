"""Immutable Host-only Build execution records, not product report Artifacts.

The Tool returns references only after both output streams and their exact
Snapshot/environment manifest are committed together. Construction is inert;
publication rechecks authoritative workflow state inside the same transaction.
No generated code, output URI, or model-selected path is executed or fetched.
"""

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import re
import sqlite3
from uuid import UUID, uuid4

from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import _copy_profile
from orchestrator.artifacts.contracts import ArtifactAccessError
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore, _canonical
from orchestrator.core.security import redact_text
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, ExecutionManifest
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxLimits, SandboxResult
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


MAX_BUILD_OUTPUT_BYTES = 4 * 1024 * 1024
# The Host configuration permits a complete 16 KiB policy; reserve room for
# its Source manifest and receipt fields so a valid policy is not rejected
# only after executing the build.
_MAX_METADATA_BYTES = 64 * 1024
_CODES = frozenset({
    "BUILD_STORAGE_ERROR", "BUILD_RECORD_CONFLICT", "BUILD_RESULT_INVALID",
    "BUILD_RESULT_INTEGRITY_ERROR", "BUILD_CONTEXT_DENIED", "BUILD_OUTPUT_LIMIT",
    "BUILD_RECORD_NOT_FOUND",
})
_METADATA_KEYS = frozenset({
    "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId",
    "sourceArtifactId", "executionManifest", "profileName", "toolName", "imageId",
    "containerId", "exitCode", "durationMs", "stdoutSha256", "stderrSha256",
    "stdoutSizeBytes", "stderrSizeBytes", "executionProfile",
})


class BuildStoreError(RuntimeError):
    """Stable safe codes only; never expose Source, output, SQL or Host paths."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _CODES else "BUILD_STORAGE_ERROR"
        super().__init__(self.code)


def _uuid(value):
    try:
        return workspace_uuid(value)
    except WorkspaceAccessError:
        raise BuildStoreError("BUILD_RESULT_INVALID") from None


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _safe_output(value):
    if not isinstance(value, str):
        raise BuildStoreError("BUILD_RESULT_INVALID")
    try:
        original = value.encode("utf-8")
        if len(original) > MAX_BUILD_OUTPUT_BYTES:
            raise BuildStoreError("BUILD_OUTPUT_LIMIT")
        safe = redact_text(value)
        content = safe.encode("utf-8")
        if len(content) > MAX_BUILD_OUTPUT_BYTES:
            raise BuildStoreError("BUILD_OUTPUT_LIMIT")
        return safe, content
    except UnicodeError:
        raise BuildStoreError("BUILD_RESULT_INVALID") from None


@dataclass(frozen=True, kw_only=True)
class BuildExecutionRecord:
    execution_manifest_id: UUID
    execution_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    execution_manifest: ExecutionManifest = field(repr=False)
    execution_profile: ExecutionProfile = field(repr=False)
    profile_name: str
    tool_name: str
    image_id: str
    container_id: str
    exit_code: int
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    stdout_sha256: str
    stderr_sha256: str
    metadata_sha256: str

    @property
    def stdout_ref(self):
        return f"execution://{self.execution_manifest_id}/stdout.txt"

    @property
    def stderr_ref(self):
        return f"execution://{self.execution_manifest_id}/stderr.txt"

    def tool_output(self) -> dict:
        """The existing closed run_build output Schema; no PASS/FAIL verdict."""
        return {
            "exitCode": self.exit_code,
            "stdoutRef": self.stdout_ref,
            "stderrRef": self.stderr_ref,
            "durationMs": self.duration_ms,
            "executionManifestId": str(self.execution_manifest_id),
        }


class BuildOutputStore:
    """A private capability injected by the Host, not a model-selectable store."""

    def __init__(self, repository: SQLiteWorkflowRepository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise BuildStoreError("BUILD_RESULT_INVALID")
        self._repository = repository

    def __repr__(self):
        return "BuildOutputStore()"

    @staticmethod
    def _ensure_schema(connection):
        # Do not use executescript: it implicitly commits the caller's transaction.
        for statement in (
            """CREATE TABLE IF NOT EXISTS build_execution_records (
                execution_manifest_id TEXT PRIMARY KEY,
                execution_id TEXT NOT NULL UNIQUE,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
                metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256)=64),
                stdout BLOB NOT NULL CHECK (typeof(stdout)='blob' AND length(stdout)<=4194304),
                stderr BLOB NOT NULL CHECK (typeof(stderr)='blob' AND length(stderr)<=4194304)
            )""",
            """CREATE INDEX IF NOT EXISTS build_execution_records_by_run
                ON build_execution_records(run_id,execution_manifest_id)""",
            """CREATE TRIGGER IF NOT EXISTS build_execution_records_no_update
                BEFORE UPDATE ON build_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Build execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS build_execution_records_no_delete
                BEFORE DELETE ON build_execution_records
                BEGIN SELECT RAISE(ABORT,'immutable Build execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS build_execution_records_no_replace
                BEFORE INSERT ON build_execution_records
                WHEN EXISTS (SELECT 1 FROM build_execution_records
                    WHERE execution_manifest_id IN (NEW.execution_manifest_id,NEW.execution_id)
                        OR execution_id IN (NEW.execution_manifest_id,NEW.execution_id))
                BEGIN SELECT RAISE(ABORT,'immutable Build execution'); END""",
            """CREATE TRIGGER IF NOT EXISTS build_execution_records_ownership
                BEFORE INSERT ON build_execution_records
                WHEN NOT EXISTS (SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id)
                    OR NOT EXISTS (SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                    OR NOT EXISTS (SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.source_artifact_id
                        AND run_id=NEW.run_id AND workflow_step_id=NEW.workflow_step_id AND artifact_type='SOURCE')
                BEGIN SELECT RAISE(ABORT,'Build ownership mismatch'); END""",
        ):
            connection.execute(statement)

    @staticmethod
    def _context(connection, binding, source):
        """Recheck cancellation, Step mutation and staged bytes at commit time."""
        if (not isinstance(binding, MCPBinding) or binding.role is not AgentRole.DEVELOPER
                or binding.agent_role is not AgentRole.DEVELOPER):
            raise BuildStoreError("BUILD_CONTEXT_DENIED")
        if not isinstance(source, CodeSnapshotArtifact):
            raise BuildStoreError("BUILD_RESULT_INVALID")
        if source.run_id != binding.run_id or source.created_by is not AgentRole.DEVELOPER:
            raise BuildStoreError("BUILD_CONTEXT_DENIED")
        run_row = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (str(binding.run_id),)).fetchone()
        step_row = connection.execute("SELECT * FROM workflow_steps WHERE workflow_step_id=?", (str(source.workflow_step_id),)).fetchone()
        workspace_row = connection.execute("SELECT * FROM workspaces WHERE workspace_id=?", (str(binding.workspace_id),)).fetchone()
        config_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(binding.run_id),)).fetchone()
        if any(row is None for row in (run_row, step_row, workspace_row, config_row)):
            raise BuildStoreError("BUILD_CONTEXT_DENIED")
        try:
            run = WorkflowRun.model_validate_json(run_row["payload_json"])
            step = WorkflowStep.model_validate_json(step_row["payload_json"])
            workspace = WorkspaceRecord.model_validate_json(workspace_row["payload_json"])
            configuration = RunConfigurationArtifact.model_validate_json(config_row["payload_json"])
            environment = configuration.configuration.environment
            if (
                run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or run_row["status"] != run.status.value
                or step_row["status"] != step.status.value
                or step_row["run_id"] != str(binding.run_id)
                or step.run_id != binding.run_id or step.workflow_step_id != source.workflow_step_id
                or step.agent_role is not AgentRole.DEVELOPER
                or run.status not in (WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING)
                or step.status is not WorkflowStepStatus.RUNNING
                or step.attempt != run.fix_attempt or source.code_version != run.fix_attempt + 1
                or (step.code_version is not None and step.code_version != source.code_version)
                or set(step.requirement_ids) != set(source.requirement_ids)
                or (source.a2a_task_id is not None and source.a2a_task_id != step.a2a_task_id)
                or workspace.run_id != binding.run_id or workspace.workspace_id != binding.workspace_id
                or workspace_row["run_id"] != str(binding.run_id)
                or configuration.run_id != binding.run_id or configuration.workspace_id != binding.workspace_id
                or configuration.scenario_id != run.scenario_id or environment is None
                or environment.network_policy != "DENY"
                or source.container_image_digest != environment.container_image_digest
                or source.dependency_lock_hash != environment.dependency_lock_hash
            ):
                raise BuildStoreError("BUILD_CONTEXT_DENIED")
            steps = []
            for row in connection.execute("SELECT workflow_step_id,run_id,status,payload_json FROM workflow_steps WHERE run_id=?", (str(binding.run_id),)):
                candidate = WorkflowStep.model_validate_json(row["payload_json"])
                if (candidate.run_id != binding.run_id or str(candidate.workflow_step_id) != row["workflow_step_id"]
                        or row["status"] != candidate.status.value):
                    raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR")
                if (candidate.agent_role is AgentRole.DEVELOPER and candidate.status is WorkflowStepStatus.RUNNING
                        and candidate.attempt == run.fix_attempt):
                    steps.append(candidate)
            if len(steps) != 1 or steps[0].workflow_step_id != source.workflow_step_id:
                raise BuildStoreError("BUILD_CONTEXT_DENIED")
        except BuildStoreError:
            raise
        except Exception:
            raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR") from None

        SQLiteArtifactContentStore._ensure_schema(connection)
        row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                 (str(source.artifact_id), str(binding.run_id))).fetchone()
        if row is None:
            raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR")
        try:
            stored = SQLiteArtifactContentStore._decode(connection, row)
            expected, expected_json = _canonical(source)
            if (not isinstance(stored.metadata, CodeSnapshotArtifact)
                    or stored.metadata != expected or row["metadata_json"] != expected_json
                    or stored.media_type != "application/x-tar"
                    or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"):
                raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR")
        except ArtifactAccessError:
            raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR") from None
        return stored

    @staticmethod
    def _result_metadata(binding, source, result, manifest_id, profile):
        if not isinstance(result, SandboxResult) or not isinstance(source, CodeSnapshotArtifact):
            raise BuildStoreError("BUILD_RESULT_INVALID")
        try:
            execution_id = _uuid(result.execution_id)
            manifest = ExecutionManifest.model_validate(result.execution_manifest.model_dump(mode="json", by_alias=True))
            if not isinstance(profile, ExecutionProfile):
                raise BuildStoreError("BUILD_RESULT_INVALID")
            # Reconstruct even trusted dataclasses: object.__setattr__ is not
            # an authority to bypass bounded Host profile validation.
            verified_profile = _copy_profile(profile)
            reference = verified_profile.image_reference
            # Docker config IDs and repository distribution digests are not
            # interchangeable. Mirror SandboxRuntime's exact image identity
            # semantics rather than merely accepting a SHA-shaped image ID.
            if reference is None:
                image_matches = result.image_id == manifest.container_image_digest
            elif reference.startswith("sha256:"):
                image_matches = reference == manifest.container_image_digest == result.image_id
            else:
                image_matches = reference.rsplit("@", 1)[-1] == manifest.container_image_digest
            if (
                result.run_id != binding.run_id or result.source_artifact_id != source.artifact_id
                or result.tool_name != "run_build" or manifest != source.execution_manifest()
                or verified_profile.tool_name != result.tool_name or verified_profile.name != result.profile_name
                or not image_matches
                or any(redact_text(argument) != argument for argument in verified_profile.argv)
                or not isinstance(result.profile_name, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", result.profile_name) is None
                or not isinstance(result.image_id, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", result.image_id) is None
                or not isinstance(result.container_id, str) or re.fullmatch(r"[0-9a-f]{64}", result.container_id) is None
                or type(result.exit_code) is not int or not 0 <= result.exit_code <= 255
                or type(result.duration_ms) is not int or not 0 <= result.duration_ms <= 2**63 - 1
            ):
                raise BuildStoreError("BUILD_RESULT_INVALID")
        except BuildStoreError:
            raise
        except Exception:
            raise BuildStoreError("BUILD_RESULT_INVALID") from None
        _stdout, stdout = _safe_output(result.stdout)
        _stderr, stderr = _safe_output(result.stderr)
        metadata = {
            "executionManifestId": str(manifest_id), "executionId": str(execution_id),
            "runId": str(binding.run_id), "workspaceId": str(binding.workspace_id),
            "workflowStepId": str(source.workflow_step_id), "sourceArtifactId": str(source.artifact_id),
            "executionManifest": manifest.model_dump(mode="json", by_alias=True),
            "executionProfile": {"name": profile.name, "toolName": profile.tool_name,
                                 "argv": list(profile.argv), "limits": asdict(profile.limits),
                                 "imageReference": profile.image_reference},
            "profileName": result.profile_name, "toolName": result.tool_name,
            "imageId": result.image_id, "containerId": result.container_id,
            "exitCode": result.exit_code, "durationMs": result.duration_ms,
            "stdoutSha256": sha256(stdout).hexdigest(), "stderrSha256": sha256(stderr).hexdigest(),
            "stdoutSizeBytes": len(stdout), "stderrSizeBytes": len(stderr),
        }
        try:
            raw = _json(metadata)
            if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise BuildStoreError("BUILD_RESULT_INVALID")
        except (TypeError, ValueError, UnicodeError, OverflowError):
            raise BuildStoreError("BUILD_RESULT_INVALID") from None
        return metadata, raw, stdout, stderr

    def publish(
        self, binding: MCPBinding, source: CodeSnapshotArtifact, result: SandboxResult,
        *, profile: ExecutionProfile,
    ) -> BuildExecutionRecord:
        if not isinstance(binding, MCPBinding) or binding.role is not AgentRole.DEVELOPER:
            raise BuildStoreError("BUILD_CONTEXT_DENIED")
        manifest_id = uuid4()
        metadata, raw, stdout, stderr = self._result_metadata(binding, source, result, manifest_id, profile)
        if str(manifest_id) == metadata["executionId"]:
            raise BuildStoreError("BUILD_RECORD_CONFLICT")
        digest = sha256(raw.encode("utf-8")).hexdigest()
        try:
            with self._repository._transaction() as connection:
                self._context(connection, binding, source)
                self._ensure_schema(connection)
                # UUID collisions with other registries are also rejected; this
                # ID denotes an execution record, never a Project Artifact.
                if any(connection.execute(query, (str(manifest_id),)).fetchone() is not None for query in (
                    "SELECT 1 FROM artifact_contents WHERE artifact_id=?",
                    "SELECT 1 FROM project_artifacts WHERE artifact_id=?",
                    "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?",
                )):
                    raise BuildStoreError("BUILD_RECORD_CONFLICT")
                for table in ("unit_test_execution_records", "browser_test_execution_records"):
                    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                        continue
                    for identity in (str(manifest_id), metadata["executionId"]):
                        if connection.execute(
                            f"SELECT 1 FROM {table} WHERE execution_manifest_id=? OR execution_id=?",
                            (identity, identity),
                        ).fetchone():
                            raise BuildStoreError("BUILD_RECORD_CONFLICT")
                connection.execute(
                    "INSERT INTO build_execution_records(execution_manifest_id,execution_id,run_id,workspace_id,workflow_step_id,"
                    "source_artifact_id,metadata_json,metadata_sha256,stdout,stderr) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (str(manifest_id), metadata["executionId"], str(binding.run_id), str(binding.workspace_id),
                     str(source.workflow_step_id), str(source.artifact_id), raw, digest,
                     sqlite3.Binary(stdout), sqlite3.Binary(stderr)),
                )
                row = connection.execute("SELECT * FROM build_execution_records WHERE execution_manifest_id=?", (str(manifest_id),)).fetchone()
                return self._decode(connection, row)
        except BuildStoreError:
            raise
        except sqlite3.IntegrityError:
            raise BuildStoreError("BUILD_RECORD_CONFLICT") from None
        except Exception:
            raise BuildStoreError("BUILD_STORAGE_ERROR") from None

    @staticmethod
    def _decode(connection, row):
        try:
            raw = row["metadata_json"]
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            metadata = json.loads(raw)
            if (not isinstance(metadata, dict) or frozenset(metadata) != _METADATA_KEYS
                    or _json(metadata) != raw or sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"]):
                raise ValueError
            identities = {key: _uuid(metadata[key]) for key in (
                "executionManifestId", "executionId", "runId", "workspaceId", "workflowStepId", "sourceArtifactId",
            )}
            if identities["executionManifestId"] == identities["executionId"]:
                raise ValueError
            for key, column in (("executionManifestId", "execution_manifest_id"), ("executionId", "execution_id"),
                                ("runId", "run_id"), ("workspaceId", "workspace_id"),
                                ("workflowStepId", "workflow_step_id"), ("sourceArtifactId", "source_artifact_id")):
                if str(identities[key]) != row[column] or metadata[key] != row[column]:
                    raise ValueError
            manifest = ExecutionManifest.model_validate(metadata["executionManifest"])
            profile_data = metadata["executionProfile"]
            if (not isinstance(profile_data, dict) or set(profile_data) != {"name", "toolName", "argv", "limits", "imageReference"}
                    or not isinstance(profile_data["argv"], list) or not isinstance(profile_data["limits"], dict)):
                raise ValueError
            profile = ExecutionProfile(name=profile_data["name"], tool_name=profile_data["toolName"],
                                       argv=tuple(profile_data["argv"]), limits=SandboxLimits(**profile_data["limits"]),
                                       image_reference=profile_data["imageReference"])
            source_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
                                            (row["source_artifact_id"], row["run_id"])).fetchone()
            if source_row is None:
                raise ValueError
            source = SQLiteArtifactContentStore._decode(connection, source_row).metadata
            if (not isinstance(source, CodeSnapshotArtifact) or manifest != source.execution_manifest()
                    or str(source.workflow_step_id) != row["workflow_step_id"]):
                raise ValueError
            workspace = connection.execute("SELECT run_id,payload_json FROM workspaces WHERE workspace_id=?", (row["workspace_id"],)).fetchone()
            if workspace is None or workspace["run_id"] != row["run_id"]:
                raise ValueError
            workspace_record = WorkspaceRecord.model_validate_json(workspace["payload_json"])
            if str(workspace_record.workspace_id) != row["workspace_id"] or str(workspace_record.run_id) != row["run_id"]:
                raise ValueError
            streams = []
            for name in ("stdout", "stderr"):
                content = row[name]
                if not isinstance(content, bytes) or len(content) > MAX_BUILD_OUTPUT_BYTES:
                    raise ValueError
                text = content.decode("utf-8")
                if (redact_text(text) != text or type(metadata[name + "SizeBytes"]) is not int
                        or metadata[name + "SizeBytes"] != len(content)
                        or metadata[name + "Sha256"] != sha256(content).hexdigest()):
                    raise ValueError
                streams.append(text)
            result = SandboxResult(
                execution_id=identities["executionId"], run_id=identities["runId"],
                source_artifact_id=identities["sourceArtifactId"], profile_name=metadata["profileName"],
                tool_name=metadata["toolName"], execution_manifest=manifest, image_id=metadata["imageId"],
                container_id=metadata["containerId"], exit_code=metadata["exitCode"], duration_ms=metadata["durationMs"],
                stdout=streams[0], stderr=streams[1],
            )
            binding = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                                 run_id=identities["runId"], workspace_id=identities["workspaceId"])
            _expected, canonical, _stdout, _stderr = BuildOutputStore._result_metadata(binding, source, result, identities["executionManifestId"], profile)
            if canonical != raw:
                raise ValueError
            return BuildExecutionRecord(
                execution_manifest_id=identities["executionManifestId"], execution_id=identities["executionId"],
                run_id=identities["runId"], workspace_id=identities["workspaceId"],
                workflow_step_id=identities["workflowStepId"], source_artifact_id=identities["sourceArtifactId"],
                execution_manifest=manifest, execution_profile=profile,
                profile_name=result.profile_name, tool_name=result.tool_name,
                image_id=result.image_id, container_id=result.container_id, exit_code=result.exit_code,
                duration_ms=result.duration_ms, stdout=streams[0], stderr=streams[1],
                stdout_sha256=metadata["stdoutSha256"], stderr_sha256=metadata["stderrSha256"],
                metadata_sha256=row["metadata_sha256"],
            )
        except Exception:
            raise BuildStoreError("BUILD_RESULT_INTEGRITY_ERROR") from None

    def get(self, run_id, execution_manifest_id) -> BuildExecutionRecord:
        run_id, execution_manifest_id = _uuid(run_id), _uuid(execution_manifest_id)
        try:
            with self._repository._transaction() as connection:
                SQLiteArtifactContentStore._ensure_schema(connection)
                self._ensure_schema(connection)
                row = connection.execute("SELECT * FROM build_execution_records WHERE run_id=? AND execution_manifest_id=?",
                                         (str(run_id), str(execution_manifest_id))).fetchone()
                if row is None:
                    raise BuildStoreError("BUILD_RECORD_NOT_FOUND")
                return self._decode(connection, row)
        except BuildStoreError:
            raise
        except Exception:
            raise BuildStoreError("BUILD_STORAGE_ERROR") from None

    def read_output(self, run_id, ref) -> str:
        if not isinstance(ref, str):
            raise BuildStoreError("BUILD_RESULT_INVALID")
        match = re.fullmatch(r"execution://([0-9a-f-]{36})/(stdout|stderr)\.txt", ref)
        if match is None:
            raise BuildStoreError("BUILD_RESULT_INVALID")
        manifest_id = _uuid(match[1])
        if str(manifest_id) != match[1]:
            raise BuildStoreError("BUILD_RESULT_INVALID")
        record = self.get(run_id, manifest_id)
        return getattr(record, match[2])
