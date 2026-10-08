"""Private, immutable byte attachments to the existing Workflow repository.

This adapter is deliberately not an Agent-facing API. A trusted service binds
the role and Run, freezes Git candidates, and adopts completed report metadata.
Construction performs no I/O; explicit storage operations install their schema.
"""

from hashlib import sha256
import json
import sqlite3
from uuid import RFC_4122, UUID

from orchestrator.artifacts.contracts import (
    MAX_CONTENT_BYTES, ArtifactAccessError, ArtifactErrorCode, StoredContent,
)
from orchestrator.domain.developer_artifacts import BuildReportArtifact, ChangeReportArtifact
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import QAReportArtifact, SecurityReportArtifact
from orchestrator.infrastructure.sqlite_workflows import _sanitize_storage_value


_MODELS = {
    "SOURCE": CodeSnapshotArtifact,
    "REQUIREMENT": RequirementArtifact,
    "CHANGE_REPORT": ChangeReportArtifact,
    "BUILD_REPORT": BuildReportArtifact,
    "QA_REPORT": QAReportArtifact,
    "SECURITY_REPORT": SecurityReportArtifact,
}
_SOURCE_GRANTS = frozenset((AgentRole.QA, AgentRole.SECURITY))
_MAX_METADATA_BYTES = 1024 * 1024
_ROW_COLUMNS = (
    "artifact_id", "run_id", "workflow_step_id", "artifact_type", "code_version",
    "metadata_json", "metadata_sha256", "content_sha256", "size_bytes", "media_type", "content",
)


def _uuid(value):
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError
        parsed = UUID(value) if isinstance(value, str) else value
        if parsed.version != 4 or parsed.variant != RFC_4122:
            raise ValueError
        return parsed
    except (TypeError, ValueError, AttributeError):
        raise ArtifactAccessError(ArtifactErrorCode.INVALID) from None


def _canonical(artifact):
    """Use the repository's existing redaction policy without rewriting IDs."""
    if type(artifact) not in _MODELS.values():
        raise ArtifactAccessError(ArtifactErrorCode.INVALID)
    try:
        original = artifact.model_dump(mode="json", by_alias=True)
        safe = _sanitize_storage_value(original)
        if isinstance(artifact, CodeSnapshotArtifact) and safe != original:
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        metadata = type(artifact).model_validate(safe)
        raw = json.dumps(
            metadata.model_dump(mode="json", by_alias=True), ensure_ascii=False,
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        return metadata, raw
    except ArtifactAccessError:
        raise
    except Exception:
        raise ArtifactAccessError(ArtifactErrorCode.INVALID) from None


def canonical_report_content(artifact) -> bytes:
    """The only accepted report bytes are its redacted registry JSON document."""
    metadata, raw = _canonical(artifact)
    if isinstance(metadata, CodeSnapshotArtifact):
        raise ArtifactAccessError(ArtifactErrorCode.INVALID)
    return raw.encode("utf-8")


class SQLiteArtifactContentStore:
    """Atomic BLOB publication and grants; no URI fetching or filesystem writes."""

    def __init__(self, repository):
        self._repository = repository

    def put(self, artifact, content: bytes, media_type: str, grants: tuple[AgentRole, ...] = ()) -> StoredContent:
        metadata, raw = _canonical(artifact)
        if not isinstance(content, bytes):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        if len(content) > MAX_CONTENT_BYTES:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        if not isinstance(grants, tuple) or any(not isinstance(role, AgentRole) for role in grants):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        source = isinstance(metadata, CodeSnapshotArtifact)
        if (source and (len(grants) != 2 or frozenset(grants) != _SOURCE_GRANTS)) or (not source and grants):
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        expected_type = "application/x-tar" if source else "application/json"
        if media_type != expected_type:
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        digest = sha256(content).hexdigest()
        if source and metadata.snapshot_sha256 != digest:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        if not source and content != raw.encode("utf-8"):
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        metadata_hash = sha256(raw.encode("utf-8")).hexdigest()
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                self._validate_context(connection, metadata, staging=source)
                existing = connection.execute(
                    "SELECT * FROM artifact_contents WHERE artifact_id=?", (str(metadata.artifact_id),),
                ).fetchone()
                if existing is not None:
                    stored = self._decode(connection, existing)
                    if (stored.metadata != metadata or stored.content != content
                        or stored.media_type != media_type
                        or existing["metadata_json"] != raw):
                        raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
                    return stored
                if connection.execute(
                    "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?",
                    (str(metadata.artifact_id),),
                ).fetchone() is not None:
                    raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
                if source and connection.execute(
                    "SELECT 1 FROM project_artifacts WHERE artifact_id=?", (str(metadata.artifact_id),),
                ).fetchone() is not None:
                    raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
                self._validate_lineage(connection, metadata)
                values = (
                    str(metadata.artifact_id), str(metadata.run_id), str(metadata.workflow_step_id),
                    metadata.artifact_type, getattr(metadata, "code_version", None), raw, metadata_hash,
                    digest, len(content), media_type, sqlite3.Binary(content),
                )
                connection.execute(
                    "INSERT INTO artifact_contents(" + ",".join(_ROW_COLUMNS) + ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                for role in sorted(grants, key=lambda role: role.value):
                    connection.execute(
                        "INSERT INTO snapshot_read_grants(artifact_id,role,access) VALUES (?,?,'READ_ONLY')",
                        (str(metadata.artifact_id), role.value),
                    )
                return StoredContent(
                    metadata=metadata, content=content, media_type=media_type,
                    content_sha256=digest, size_bytes=len(content),
                )
        except ArtifactAccessError:
            raise
        except sqlite3.IntegrityError:
            raise ArtifactAccessError(ArtifactErrorCode.CONFLICT) from None
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None

    def get(self, run_id, artifact_id) -> StoredContent:
        run_id, artifact_id = _uuid(run_id), _uuid(artifact_id)
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT * FROM artifact_contents WHERE run_id=? AND artifact_id=?",
                    (str(run_id), str(artifact_id)),
                ).fetchone()
                if row is None:
                    raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
                return self._decode(connection, row)
        except ArtifactAccessError:
            raise
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None

    def has_grant(self, run_id, artifact_id, role: AgentRole) -> bool:
        run_id, artifact_id = _uuid(run_id), _uuid(artifact_id)
        if not isinstance(role, AgentRole):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                row = connection.execute(
                    "SELECT * FROM artifact_contents WHERE run_id=? AND artifact_id=?",
                    (str(run_id), str(artifact_id)),
                ).fetchone()
                if row is None:
                    raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
                self._decode(connection, row)
                return connection.execute(
                    "SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role=? AND access='READ_ONLY'",
                    (str(artifact_id), role.value),
                ).fetchone() is not None
        except ArtifactAccessError:
            raise
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None

    def latest_source(self, run_id) -> CodeSnapshotArtifact | None:
        run_id = _uuid(run_id)
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                if connection.execute("SELECT 1 FROM workflow_runs WHERE run_id=?", (str(run_id),)).fetchone() is None:
                    raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
                row = connection.execute(
                    "SELECT * FROM artifact_contents WHERE run_id=? AND artifact_type='SOURCE' ORDER BY code_version DESC LIMIT 1",
                    (str(run_id),),
                ).fetchone()
                return self._decode(connection, row).metadata if row is not None else None
        except ArtifactAccessError:
            raise
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None

    @staticmethod
    def _ensure_schema(connection):
        # Individual execute calls retain the caller's transaction; executescript
        # would implicitly commit and break atomic publication of bytes + grants.
        statements = (
            """CREATE TABLE IF NOT EXISTS artifact_contents (
                artifact_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                artifact_type TEXT NOT NULL CHECK (artifact_type IN ('SOURCE','REQUIREMENT','CHANGE_REPORT','BUILD_REPORT','QA_REPORT','SECURITY_REPORT')),
                code_version INTEGER,
                metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
                metadata_sha256 TEXT NOT NULL CHECK (length(metadata_sha256)=64),
                content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
                size_bytes INTEGER NOT NULL CHECK (size_bytes>=0 AND size_bytes<=20971520),
                media_type TEXT NOT NULL,
                content BLOB NOT NULL CHECK (typeof(content)='blob' AND length(content)=size_bytes),
                CHECK (artifact_type!='SOURCE' OR (code_version BETWEEN 1 AND 4 AND media_type='application/x-tar'))
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS artifact_contents_source_versions
                ON artifact_contents(run_id,code_version) WHERE artifact_type='SOURCE'""",
            """CREATE TABLE IF NOT EXISTS snapshot_read_grants (
                artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
                role TEXT NOT NULL CHECK (role IN ('QA','SECURITY')),
                access TEXT NOT NULL CHECK (access='READ_ONLY'),
                PRIMARY KEY (artifact_id,role)
            )""",
            """CREATE TRIGGER IF NOT EXISTS artifact_contents_no_update BEFORE UPDATE ON artifact_contents
                BEGIN SELECT RAISE(ABORT,'immutable Artifact content'); END""",
            """CREATE TRIGGER IF NOT EXISTS artifact_contents_no_delete BEFORE DELETE ON artifact_contents
                BEGIN SELECT RAISE(ABORT,'immutable Artifact content'); END""",
            """CREATE TRIGGER IF NOT EXISTS artifact_contents_no_replace BEFORE INSERT ON artifact_contents
                WHEN EXISTS (SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.artifact_id
                    OR (NEW.artifact_type='SOURCE' AND artifact_type='SOURCE' AND run_id=NEW.run_id AND code_version=NEW.code_version))
                BEGIN SELECT RAISE(ABORT,'immutable Artifact content'); END""",
            """CREATE TRIGGER IF NOT EXISTS snapshot_read_grants_no_update BEFORE UPDATE ON snapshot_read_grants
                BEGIN SELECT RAISE(ABORT,'immutable Snapshot grant'); END""",
            """CREATE TRIGGER IF NOT EXISTS snapshot_read_grants_no_delete BEFORE DELETE ON snapshot_read_grants
                BEGIN SELECT RAISE(ABORT,'immutable Snapshot grant'); END""",
            """CREATE TRIGGER IF NOT EXISTS snapshot_read_grants_no_replace BEFORE INSERT ON snapshot_read_grants
                WHEN EXISTS (SELECT 1 FROM snapshot_read_grants WHERE artifact_id=NEW.artifact_id AND role=NEW.role)
                BEGIN SELECT RAISE(ABORT,'immutable Snapshot grant'); END""",
            """CREATE TRIGGER IF NOT EXISTS artifact_contents_step_ownership BEFORE INSERT ON artifact_contents
                WHEN NOT EXISTS (SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                BEGIN SELECT RAISE(ABORT,'Artifact ownership mismatch'); END""",
            """CREATE TRIGGER IF NOT EXISTS snapshot_read_grants_source_only BEFORE INSERT ON snapshot_read_grants
                WHEN NOT EXISTS (SELECT 1 FROM artifact_contents WHERE artifact_id=NEW.artifact_id AND artifact_type='SOURCE')
                BEGIN SELECT RAISE(ABORT,'Snapshot grant requires Source'); END""",
        )
        for statement in statements:
            connection.execute(statement)

    @staticmethod
    def _validate_context(connection, metadata, *, staging=False):
        run_row = connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (str(metadata.run_id),)).fetchone()
        step_row = connection.execute("SELECT run_id,payload_json FROM workflow_steps WHERE workflow_step_id=?", (str(metadata.workflow_step_id),)).fetchone()
        if run_row is None or step_row is None:
            raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
        try:
            run = WorkflowRun.model_validate_json(run_row["payload_json"])
            step = WorkflowStep.model_validate_json(step_row["payload_json"])
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
        if (run.run_id != metadata.run_id or step.run_id != metadata.run_id
            or step_row["run_id"] != str(metadata.run_id)
            or step.workflow_step_id != metadata.workflow_step_id or step.agent_role != metadata.created_by):
            raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        if staging:
            if (run.status not in (WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING)
                or step.status not in (WorkflowStepStatus.RUNNING, WorkflowStepStatus.SUCCEEDED)
                or step.attempt != run.fix_attempt or metadata.code_version != run.fix_attempt + 1
                or set(metadata.requirement_ids) != set(step.requirement_ids)
                or (step.code_version is not None and step.code_version != metadata.code_version)
                or (metadata.a2a_task_id is not None and metadata.a2a_task_id != step.a2a_task_id)):
                raise ArtifactAccessError(ArtifactErrorCode.DENIED)
        if isinstance(metadata, CodeSnapshotArtifact):
            baseline_row = connection.execute(
                "SELECT payload_json FROM run_configurations WHERE run_id=?", (str(metadata.run_id),),
            ).fetchone()
            if baseline_row is None:
                raise ArtifactAccessError(ArtifactErrorCode.CONFIGURATION)
            try:
                baseline = RunConfigurationArtifact.model_validate_json(baseline_row["payload_json"])
                environment = baseline.configuration.environment
            except Exception:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
            if environment is None:
                raise ArtifactAccessError(ArtifactErrorCode.CONFIGURATION)
            if (baseline.run_id != metadata.run_id or baseline.workspace_id != run.workspace_id
                or baseline.scenario_id != run.scenario_id
                or metadata.container_image_digest != environment.container_image_digest
                or metadata.dependency_lock_hash != environment.dependency_lock_hash):
                raise ArtifactAccessError(ArtifactErrorCode.DENIED if staging else ArtifactErrorCode.INTEGRITY)
        if not isinstance(metadata, CodeSnapshotArtifact):
            registered = connection.execute(
                "SELECT run_id,artifact_type,payload_json FROM project_artifacts WHERE artifact_id=?", (str(metadata.artifact_id),),
            ).fetchone()
            if registered is None:
                raise ArtifactAccessError(ArtifactErrorCode.NOT_FOUND)
            try:
                authoritative = type(metadata).model_validate_json(registered["payload_json"])
                _, expected = _canonical(authoritative)
                _, supplied = _canonical(metadata)
            except ArtifactAccessError:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
            except Exception:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
            if (registered["run_id"] != str(metadata.run_id)
                or registered["artifact_type"] != metadata.artifact_type or expected != supplied):
                raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)

    @staticmethod
    def _validate_lineage(connection, metadata):
        if not isinstance(metadata, CodeSnapshotArtifact):
            return
        previous = connection.execute(
            "SELECT * FROM artifact_contents WHERE run_id=? AND artifact_type='SOURCE' ORDER BY code_version DESC LIMIT 1",
            (str(metadata.run_id),),
        ).fetchone()
        if previous is None:
            if metadata.artifact_version != 1 or metadata.code_version != 1 or metadata.previous_artifact_id is not None:
                raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)
            return
        stored_previous = SQLiteArtifactContentStore._decode(connection, previous).metadata
        if (metadata.previous_artifact_id != stored_previous.artifact_id
            or metadata.artifact_version != stored_previous.artifact_version + 1
            or metadata.code_version != stored_previous.code_version + 1
            or metadata.repository_id != stored_previous.repository_id
            or metadata.git_object_format != stored_previous.git_object_format):
            raise ArtifactAccessError(ArtifactErrorCode.CONFLICT)

    @staticmethod
    def _decode(connection, row) -> StoredContent:
        try:
            raw = row["metadata_json"]
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
                raise ValueError
            if sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"]:
                raise ValueError
            model_type = _MODELS[row["artifact_type"]]
            metadata = model_type.model_validate_json(raw)
            safe, canonical = _canonical(metadata)
            if safe != metadata or canonical != raw:
                raise ValueError
            content = row["content"]
            if not isinstance(content, bytes) or len(content) > MAX_CONTENT_BYTES:
                raise ValueError
            digest = sha256(content).hexdigest()
            if (str(metadata.artifact_id) != row["artifact_id"] or str(metadata.run_id) != row["run_id"]
                or str(metadata.workflow_step_id) != row["workflow_step_id"]
                or metadata.artifact_type != row["artifact_type"]
                or getattr(metadata, "code_version", None) != row["code_version"]
                or len(content) != row["size_bytes"] or digest != row["content_sha256"]):
                raise ValueError
            source = isinstance(metadata, CodeSnapshotArtifact)
            if row["media_type"] != ("application/x-tar" if source else "application/json"):
                raise ValueError
            if source and digest != metadata.snapshot_sha256:
                raise ValueError
            if not source and content != raw.encode("utf-8"):
                raise ValueError
            grants = connection.execute("SELECT role,access FROM snapshot_read_grants WHERE artifact_id=?", (row["artifact_id"],)).fetchall()
            if ({(grant["role"], grant["access"]) for grant in grants}
                != ({("QA", "READ_ONLY"), ("SECURITY", "READ_ONLY")} if source else set())):
                raise ValueError
        except ArtifactAccessError:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
        except Exception:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
        SQLiteArtifactContentStore._validate_context(connection, metadata)
        return StoredContent(
            metadata=metadata, content=content, media_type=row["media_type"],
            content_sha256=digest, size_bytes=len(content),
        )
