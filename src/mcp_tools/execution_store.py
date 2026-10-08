"""Host-owned, append-only logical MCP calls and bounded attempt evidence.

Only hashes and verified private receipt identities enter this ledger. It does
not persist arguments, product files, tool output bodies, or peer error text.
An unfinished attempt is deliberately not replayable after a crash.
"""

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from hashlib import sha256
import re
import sqlite3
from uuid import UUID, uuid4

from mcp_tools.core.catalog import get_tool_contract, _canonical_json, _parse_json
from mcp_tools.core.policy import ROLE_TOOL_NAMES
from mcp_tools.runtime import MCPBinding, MCPExecutionError
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.unit_store import UnitTestOutputStore
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.security_store import SecurityScanOutputStore
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep, utc_now
from orchestrator.domain.retry_policy import RetryDecision, ToolErrorKind
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact, ExecutionManifest
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolAttemptEvidence, ToolExecutionEvidence, ToolExecutionOutcome
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import workspace_uuid


_CODES = frozenset({"TOOL_EVIDENCE_STORAGE_ERROR", "TOOL_EVIDENCE_INVALID",
    "TOOL_EVIDENCE_CONTEXT_DENIED", "TOOL_EVIDENCE_CONFLICT", "TOOL_EVIDENCE_INTEGRITY_ERROR",
    "TOOL_EVIDENCE_NOT_FOUND", "TOOL_EVIDENCE_RETRY_DENIED", "TOOL_EVIDENCE_INCOMPLETE"})
_ERRORS = frozenset(item.value for item in ToolErrorKind) | frozenset(item.value for item in MCPExecutionError) | {
    "UNKNOWN_ERROR", "CANCELLED"}
_WRITE_TOOLS = frozenset({"write_source_file", "write_test_file", "apply_patch"})
_EXECUTIONS = {
    "run_build": (BuildOutputStore, "build_execution_records"),
    "run_unit_tests": (UnitTestOutputStore, "unit_test_execution_records"),
    "run_browser_tests": (BrowserTestOutputStore, "browser_test_execution_records"),
    "run_security_scan": (SecurityScanOutputStore, "security_scan_execution_records"),
}
_CALL_KEYS = frozenset({"logicalCallId", "runId", "workspaceId", "workflowStepId", "role", "toolName",
    "inputSha256", "configurationSha256", "selectorSha256", "runConfigurationSha256", "stepIdentity", "sourceArtifactId",
    "executionManifest", "createdAt"})
_START_KEYS = frozenset({"logicalCallId", "attempt", "attemptId", "startedAt", "receiptRowidFloor"})
_FINISH_KEYS = frozenset({"logicalCallId", "attempt", "attemptId", "outcome", "errorKind", "retrySafe",
    "retryDecision", "durationMs", "deliveryState", "resultUnknown", "productFailureKind", "outputSha256",
    "receipt", "finishedAt"})


class ToolStoreError(RuntimeError):
    """A stable safe code, without submitted values, output or SQLite text."""
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "TOOL_EVIDENCE_STORAGE_ERROR"
        super().__init__(self.code)


# Explicit compatibility name for the Host execution adapter.
ToolEvidenceStoreError = ToolStoreError


def _uuid(value):
    try:
        return workspace_uuid(value)
    except Exception:
        raise ToolStoreError("TOOL_EVIDENCE_INVALID") from None


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ToolStoreError("TOOL_EVIDENCE_INVALID")
    return value


def _time(value):
    if type(value) is not str or len(value) > 40:
        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
    try:
        instant = datetime.fromisoformat(value)
        if instant.tzinfo is None or instant.utcoffset() != timezone.utc.utcoffset(instant) or instant.isoformat() != value:
            raise ValueError
        return instant
    except Exception:
        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR") from None


def _json(value):
    return _canonical_json(value)


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _enum(value, enumeration):
    try:
        return enumeration(value)
    except Exception:
        raise ToolStoreError("TOOL_EVIDENCE_INVALID") from None


def _binding(value):
    if not isinstance(value, MCPBinding) or value.role is not value.agent_role:
        raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
    return value


@dataclass(frozen=True, kw_only=True)
class ToolAttemptToken:
    logical_call_id: UUID
    attempt: int
    attempt_id: UUID

    @property
    def evidence_ref(self):
        return f"artifact://{self.attempt_id}/tool-attempt.json"


@dataclass(frozen=True, kw_only=True)
class ToolAttemptRecord:
    logical_call_id: UUID
    attempt: int
    attempt_id: UUID
    status: str
    started_at: str
    receipt_rowid_floor: int | None = None
    finished_at: str | None = None
    outcome: ToolExecutionOutcome | None = None
    error_kind: str | None = None
    retry_safe: bool = False
    retry_decision: RetryDecision = RetryDecision.DO_NOT_RETRY
    duration_ms: int = 0
    delivery_state: str | None = None
    result_unknown: bool = False
    product_failure_kind: str | None = None
    output_sha256: str | None = None
    execution_id: UUID | None = None
    execution_manifest_id: UUID | None = None
    receipt_metadata_sha256: str | None = None
    receipt_refs: tuple[str, ...] = ()

    @property
    def evidence_ref(self):
        return f"artifact://{self.attempt_id}/tool-attempt.json"

    def to_dict(self):
        return {"logicalCallId": str(self.logical_call_id), "attempt": self.attempt,
            "attemptId": str(self.attempt_id), "evidenceRef": self.evidence_ref, "status": self.status,
            "startedAt": self.started_at, "finishedAt": self.finished_at,
            "receiptRowidFloor": self.receipt_rowid_floor,
            "outcome": None if self.outcome is None else self.outcome.value, "errorKind": self.error_kind,
            "retrySafe": self.retry_safe, "retryDecision": self.retry_decision.value,
            "durationMs": self.duration_ms, "deliveryState": self.delivery_state,
            "resultUnknown": self.result_unknown, "productFailureKind": self.product_failure_kind,
            "outputSha256": self.output_sha256,
            "executionId": None if self.execution_id is None else str(self.execution_id),
            "executionManifestId": None if self.execution_manifest_id is None else str(self.execution_manifest_id),
            "receiptMetadataSha256": self.receipt_metadata_sha256, "receiptRefs": list(self.receipt_refs)}


@dataclass(frozen=True, kw_only=True)
class ToolCallRecord:
    logical_call_id: UUID
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    role: AgentRole
    tool_name: str
    input_sha256: str
    configuration_sha256: str
    selector_sha256: str | None
    source_artifact_id: UUID | None = field(repr=False)
    execution_manifest: ExecutionManifest | None = field(repr=False)
    attempts: tuple[ToolAttemptRecord, ...]

    @property
    def evidence_ref(self):
        return f"artifact://{self.logical_call_id}/tool-execution-evidence.json"

    def to_dict(self):
        return {"logicalCallId": str(self.logical_call_id), "runId": str(self.run_id),
            "workspaceId": str(self.workspace_id), "workflowStepId": str(self.workflow_step_id),
            "role": self.role.value, "toolName": self.tool_name, "inputSha256": self.input_sha256,
            "configurationSha256": self.configuration_sha256,
            "selectorSha256": self.selector_sha256,
            "sourceArtifactId": None if self.source_artifact_id is None else str(self.source_artifact_id),
            "executionManifest": None if self.execution_manifest is None else self.execution_manifest.model_dump(mode="json", by_alias=True),
            "evidenceRef": self.evidence_ref, "attempts": [item.to_dict() for item in self.attempts]}

    def to_tool_evidence(self):
        if self.execution_manifest is None or not self.attempts or any(item.status != "FINISHED" for item in self.attempts):
            raise ToolStoreError("TOOL_EVIDENCE_INCOMPLETE")
        return ToolExecutionEvidence(toolName=self.tool_name, executionId=self.logical_call_id,
            executionManifest=self.execution_manifest, evidenceRef=self.evidence_ref,
            attempts=tuple(ToolAttemptEvidence(attempt=item.attempt, outcome=item.outcome,
                evidenceRef=item.evidence_ref, errorKind=item.error_kind, retrySafe=item.retry_safe,
                durationMs=item.duration_ms) for item in self.attempts))


class ToolExecutionStore:
    """Inert Host capability; each mutation is a short BEGIN IMMEDIATE txn."""
    def __init__(self, repository: SQLiteWorkflowRepository):
        if not isinstance(repository, SQLiteWorkflowRepository):
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        self._repository = repository

    def __repr__(self):
        return "ToolExecutionStore()"

    @staticmethod
    def _ensure_schema(connection):
        definitions = {
            "tool_execution_calls": "logical_call_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES workflow_runs(run_id), workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id), workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id)",
            "tool_execution_attempt_starts": "attempt_id TEXT PRIMARY KEY, logical_call_id TEXT NOT NULL REFERENCES tool_execution_calls(logical_call_id), attempt INTEGER NOT NULL CHECK(attempt BETWEEN 0 AND 2)",
            "tool_execution_attempt_finishes": "attempt_id TEXT PRIMARY KEY REFERENCES tool_execution_attempt_starts(attempt_id), logical_call_id TEXT NOT NULL REFERENCES tool_execution_calls(logical_call_id), attempt INTEGER NOT NULL CHECK(attempt BETWEEN 0 AND 2)",
        }
        for table, columns in definitions.items():
            unique = "" if table == "tool_execution_calls" else ", UNIQUE(logical_call_id,attempt)"
            connection.execute(f"CREATE TABLE IF NOT EXISTS {table} ({columns}, payload_json TEXT NOT NULL CHECK(json_valid(payload_json)), payload_sha256 TEXT NOT NULL CHECK(length(payload_sha256)=64){unique})")
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable Tool evidence'); END")
            identity = "logical_call_id" if table == "tool_execution_calls" else "attempt_id"
            alternate = "" if table == "tool_execution_calls" else " OR (logical_call_id=NEW.logical_call_id AND attempt=NEW.attempt)"
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} WHEN EXISTS(SELECT 1 FROM {table} WHERE {identity}=NEW.{identity}{alternate}) BEGIN SELECT RAISE(ABORT,'immutable Tool evidence'); END")
        connection.execute("CREATE TRIGGER IF NOT EXISTS tool_execution_call_ownership BEFORE INSERT ON tool_execution_calls WHEN NOT EXISTS(SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id) OR NOT EXISTS(SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id) BEGIN SELECT RAISE(ABORT,'Tool evidence ownership mismatch'); END")
        connection.execute("CREATE TRIGGER IF NOT EXISTS tool_execution_finish_ownership BEFORE INSERT ON tool_execution_attempt_finishes WHEN NOT EXISTS(SELECT 1 FROM tool_execution_attempt_starts WHERE attempt_id=NEW.attempt_id AND logical_call_id=NEW.logical_call_id AND attempt=NEW.attempt) BEGIN SELECT RAISE(ABORT,'Tool evidence attempt mismatch'); END")
        for key, label in (("executionId", "physical_execution"), ("executionManifestId", "physical_manifest")):
            connection.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS tool_execution_unique_{label} ON tool_execution_attempt_finishes(json_extract(payload_json,'$.receipt.{key}')) WHERE json_extract(payload_json,'$.receipt.{key}') IS NOT NULL")

    @staticmethod
    def _step_identity(step):
        return {"attempt": step.attempt, "codeVersion": step.code_version,
            "requirementIds": [str(value) for value in step.requirement_ids],
            "inputArtifactIds": [str(value) for value in step.input_artifact_ids],
            "a2aTaskIdSha256": None if step.a2a_task_id is None else sha256(step.a2a_task_id.encode("utf-8")).hexdigest()}

    @staticmethod
    def _context(connection, binding, step_id, *, active, call=None, source_id=None):
        _binding(binding)
        rows = [connection.execute(query, (str(identity),)).fetchone() for query, identity in (
            ("SELECT * FROM workflow_runs WHERE run_id=?", binding.run_id),
            ("SELECT * FROM workspaces WHERE workspace_id=?", binding.workspace_id),
            ("SELECT * FROM workflow_steps WHERE workflow_step_id=?", step_id),
            ("SELECT * FROM run_configurations WHERE run_id=?", binding.run_id))]
        if any(row is None for row in rows):
            raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
        try:
            run = WorkflowRun.model_validate_json(rows[0]["payload_json"])
            workspace = WorkspaceRecord.model_validate_json(rows[1]["payload_json"])
            step = WorkflowStep.model_validate_json(rows[2]["payload_json"])
            configuration = RunConfigurationArtifact.model_validate_json(rows[3]["payload_json"])
            if (run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or rows[0]["status"] != run.status.value or workspace.run_id != binding.run_id
                or workspace.workspace_id != binding.workspace_id or rows[1]["run_id"] != str(binding.run_id)
                or step.run_id != binding.run_id or step.workflow_step_id != step_id
                or rows[2]["run_id"] != str(binding.run_id) or rows[2]["status"] != step.status.value
                or step.agent_role is not binding.role or configuration.run_id != binding.run_id
                or configuration.workspace_id != binding.workspace_id or configuration.scenario_id != run.scenario_id):
                raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
            config_hash = _digest(configuration.model_dump(mode="json", by_alias=True))
            if call is not None and (config_hash != call["runConfigurationSha256"]
                or ToolExecutionStore._step_identity(step) != call["stepIdentity"]):
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            if active:
                phases = {AgentRole.PLANNER: {WorkflowStatus.PLANNING},
                    AgentRole.DEVELOPER: {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING},
                    AgentRole.QA: {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING},
                    AgentRole.SECURITY: {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING}}
                if (run.status not in phases[binding.role] or step.status is not WorkflowStepStatus.RUNNING
                        or binding.role is not AgentRole.DEVELOPER and step.attempt != run.fix_attempt):
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                candidates = []
                for row in connection.execute("SELECT * FROM workflow_steps WHERE run_id=?", (str(binding.run_id),)):
                    candidate = WorkflowStep.model_validate_json(row["payload_json"])
                    if (candidate.run_id != binding.run_id or str(candidate.workflow_step_id) != row["workflow_step_id"]
                        or candidate.status.value != row["status"]):
                        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                    if (candidate.agent_role is binding.role and candidate.status is WorkflowStepStatus.RUNNING
                            and (binding.role is AgentRole.DEVELOPER or candidate.attempt == run.fix_attempt)):
                        candidates.append(candidate.workflow_step_id)
                if candidates != [step_id]:
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
            source = None
            if source_id is not None:
                SQLiteArtifactContentStore._ensure_schema(connection)
                row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?", (str(source_id), str(binding.run_id))).fetchone()
                if row is None:
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                if binding.role in {AgentRole.QA, AgentRole.SECURITY} and connection.execute(
                    "SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role=? AND access='READ_ONLY'",
                    (str(source_id), binding.role.value)).fetchone() is None:
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                stored = SQLiteArtifactContentStore._decode(connection, row)
                source = stored.metadata
                if (not isinstance(source, CodeSnapshotArtifact) or source.created_by is not AgentRole.DEVELOPER
                    or stored.media_type != "application/x-tar" or source.artifact_uri != f"artifact://{source_id}/source.tar"
                    or not step.requirement_ids or not set(step.requirement_ids) <= set(source.requirement_ids)
                    or step.code_version is not None and step.code_version != source.code_version):
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                env = configuration.configuration.environment
                if (env is None or env.network_policy != "DENY" or env.container_image_digest != source.container_image_digest
                    or env.dependency_lock_hash != source.dependency_lock_hash):
                    raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                if binding.role is AgentRole.DEVELOPER:
                    if (source.workflow_step_id != step_id or set(step.requirement_ids) != set(source.requirement_ids)
                        or source.a2a_task_id is not None and source.a2a_task_id != step.a2a_task_id):
                        raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                else:
                    if source.artifact_id not in step.input_artifact_ids or connection.execute(
                        "SELECT 1 FROM snapshot_read_grants WHERE artifact_id=? AND role=? AND access='READ_ONLY'",
                        (str(source_id), binding.role.value)).fetchone() is None:
                        raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                if active:
                    latest = connection.execute("SELECT artifact_id FROM artifact_contents WHERE run_id=? AND artifact_type='SOURCE' ORDER BY code_version DESC LIMIT 1", (str(binding.run_id),)).fetchone()
                    if latest is None or latest[0] != str(source_id) or source.code_version != run.fix_attempt + 1:
                        raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
                if call is not None and source.execution_manifest().model_dump(mode="json", by_alias=True) != call["executionManifest"]:
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            return step, source, config_hash
        except ToolStoreError:
            raise
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _collision(connection, identity):
        candidates = [("tool_execution_calls", "logical_call_id=?"), ("tool_execution_attempt_starts", "attempt_id=?"),
            ("artifact_contents", "artifact_id=?"), ("project_artifacts", "artifact_id=?"),
            ("run_configurations", "COALESCE(json_extract(payload_json,'$.artifact_id'),json_extract(payload_json,'$.artifactId'))=?")]
        candidates.extend((table, "execution_manifest_id=? OR execution_id=?") for _, table in _EXECUTIONS.values())
        for table, predicate in candidates:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None:
                if connection.execute(f"SELECT 1 FROM {table} WHERE {predicate}", (str(identity),) * predicate.count("?")).fetchone() is not None:
                    raise ToolStoreError("TOOL_EVIDENCE_CONFLICT")

    @staticmethod
    def _insert(connection, table, columns, payload):
        raw = _json(payload)
        values = tuple(columns.values()) + (raw, sha256(raw.encode("utf-8")).hexdigest())
        connection.execute(f"INSERT INTO {table} ({','.join(columns)},payload_json,payload_sha256) VALUES ({','.join('?' for _ in values)})", values)

    @staticmethod
    def _payload(row, keys):
        raw = row["payload_json"]
        if type(raw) is not str or len(raw.encode("utf-8")) > 65536 or sha256(raw.encode("utf-8")).hexdigest() != row["payload_sha256"]:
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
        data = _parse_json(raw)
        if type(data) is not dict or set(data) != keys or _json(data) != raw:
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
        return data

    def create(self, binding, workflow_step_id, tool_name, input_sha256, *, source_artifact_id=None, configuration_sha256, selector_sha256=None):
        _binding(binding)
        step_id = _uuid(workflow_step_id)
        input_sha256, configuration_sha256 = _hash(input_sha256), _hash(configuration_sha256)
        if type(tool_name) is not str or tool_name not in ROLE_TOOL_NAMES[binding.role]:
            raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
        source_id = None if source_artifact_id is None else _uuid(source_artifact_id)
        if selector_sha256 is not None:
            selector_sha256 = _hash(selector_sha256)
        if tool_name in _EXECUTIONS and source_id is None:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        if tool_name not in _EXECUTIONS and source_id is not None:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        if tool_name not in _EXECUTIONS and selector_sha256 is not None:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                step, source, config_hash = self._context(connection, binding, step_id, active=True, source_id=source_id)
                identity = _uuid(uuid4())
                self._collision(connection, identity)
                payload = {"logicalCallId": str(identity), "runId": str(binding.run_id), "workspaceId": str(binding.workspace_id),
                    "workflowStepId": str(step_id), "role": binding.role.value, "toolName": tool_name,
                    "inputSha256": input_sha256, "configurationSha256": configuration_sha256,
                    "selectorSha256": selector_sha256,
                    "runConfigurationSha256": config_hash, "stepIdentity": self._step_identity(step),
                    "sourceArtifactId": None if source is None else str(source_id),
                    "executionManifest": None if source is None else source.execution_manifest().model_dump(mode="json", by_alias=True),
                    "createdAt": utc_now().isoformat()}
                self._insert(connection, "tool_execution_calls", {"logical_call_id": str(identity), "run_id": str(binding.run_id),
                    "workspace_id": str(binding.workspace_id), "workflow_step_id": str(step_id)}, payload)
                return self._load(connection, binding, identity)
        except ToolStoreError:
            raise
        except sqlite3.IntegrityError:
            raise ToolStoreError("TOOL_EVIDENCE_CONFLICT") from None
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None

    def claim(self, binding, logical_call_id):
        _binding(binding)
        identity = _uuid(logical_call_id)
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                record, payload = self._load(connection, binding, identity, return_payload=True)
                self._context(connection, binding, record.workflow_step_id, active=True, call=payload, source_id=record.source_artifact_id)
                attempt = len(record.attempts)
                if attempt >= 3 or attempt and (record.attempts[-1].status != "FINISHED"
                    or record.attempts[-1].retry_decision is not RetryDecision.RETRY or not record.attempts[-1].retry_safe):
                    raise ToolStoreError("TOOL_EVIDENCE_RETRY_DENIED")
                attempt_id = _uuid(uuid4())
                self._collision(connection, attempt_id)
                floor = None
                if record.tool_name in _EXECUTIONS:
                    table = _EXECUTIONS[record.tool_name][1]
                    floor = 0
                    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None:
                        floor = connection.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table} WHERE run_id=?", (str(binding.run_id),)).fetchone()[0]
                    if type(floor) is not int or not 0 <= floor <= 2**63 - 1:
                        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                data = {"logicalCallId": str(identity), "attempt": attempt, "attemptId": str(attempt_id), "startedAt": utc_now().isoformat(),
                    "receiptRowidFloor": floor}
                self._insert(connection, "tool_execution_attempt_starts", {"attempt_id": str(attempt_id), "logical_call_id": str(identity), "attempt": attempt}, data)
                return ToolAttemptToken(logical_call_id=identity, attempt=attempt, attempt_id=attempt_id)
        except ToolStoreError:
            raise
        except sqlite3.IntegrityError:
            raise ToolStoreError("TOOL_EVIDENCE_CONFLICT") from None
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None

    @staticmethod
    def _receipt(connection, record, manifest_id, floor):
        store, table = _EXECUTIONS[record.tool_name]
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is None:
            raise ToolStoreError("TOOL_EVIDENCE_NOT_FOUND")
        row = connection.execute(f"SELECT rowid AS receipt_rowid,* FROM {table} WHERE run_id=? AND execution_manifest_id=?", (str(record.run_id), str(manifest_id))).fetchone()
        if row is None:
            raise ToolStoreError("TOOL_EVIDENCE_NOT_FOUND")
        try:
            if type(floor) is not int or not 0 <= floor <= 2**63 - 1:
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            if row["receipt_rowid"] <= floor:
                raise ToolStoreError("TOOL_EVIDENCE_CONFLICT")
            receipt = store._decode(connection, row)
            if (receipt.run_id != record.run_id or receipt.workspace_id != record.workspace_id
                or receipt.workflow_step_id != record.workflow_step_id or receipt.source_artifact_id != record.source_artifact_id
                or receipt.execution_manifest != record.execution_manifest or receipt.tool_name != record.tool_name
                or hasattr(receipt, "role") and receipt.role is not record.role):
                raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
            if record.selector_sha256 is None or sha256(receipt.profile_name.encode("utf-8")).hexdigest() != record.selector_sha256:
                raise ToolStoreError("TOOL_EVIDENCE_INVALID")
            refs = (receipt.stdout_ref, receipt.stderr_ref) if record.tool_name == "run_build" else (receipt.report_ref,)
            if record.tool_name == "run_browser_tests":
                refs += tuple(receipt.trace_refs)
            product = None
            if record.tool_name == "run_build" and receipt.exit_code != 0:
                product = "BUILD_CODE_FAILURE"
            elif record.tool_name in {"run_unit_tests", "run_browser_tests"} and receipt.report.failed:
                product = "QA_ASSERTION_FAILURE"
            elif record.tool_name == "run_security_scan" and receipt.report.findings:
                product = "SECURITY_FINDING"
            data = {"executionManifestId": str(receipt.execution_manifest_id), "executionId": str(receipt.execution_id),
                "metadataSha256": receipt.metadata_sha256, "refs": list(refs)}
            for identity in (receipt.execution_manifest_id, receipt.execution_id):
                if connection.execute("SELECT 1 FROM tool_execution_calls WHERE logical_call_id=? UNION ALL SELECT 1 FROM tool_execution_attempt_starts WHERE attempt_id=?", (str(identity), str(identity))).fetchone() is not None:
                    raise ToolStoreError("TOOL_EVIDENCE_CONFLICT")
            return receipt.tool_output(), data, product
        except ToolStoreError:
            raise
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR") from None

    @staticmethod
    def _validate_finish(record, data):
        if (type(data["attempt"]) is not int or not 0 <= data["attempt"] <= 2
            or type(data["durationMs"]) is not int or not 0 <= data["durationMs"] <= 2**63 - 1
            or type(data["retrySafe"]) is not bool or type(data["resultUnknown"]) is not bool
            or data["deliveryState"] not in {"NOT_SENT", "REPLIED", "UNKNOWN"}
            or data["errorKind"] is not None and data["errorKind"] not in _ERRORS):
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        outcome = _enum(data["outcome"], ToolExecutionOutcome)
        decision = _enum(data["retryDecision"], RetryDecision)
        product = data["productFailureKind"]
        if product is not None and product not in {"BUILD_CODE_FAILURE", "QA_ASSERTION_FAILURE", "SECURITY_FINDING"}:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        if outcome is ToolExecutionOutcome.PASS:
            if (data["errorKind"] is not None or data["retrySafe"] or decision is not RetryDecision.DO_NOT_RETRY
                or data["resultUnknown"] or data["deliveryState"] != "REPLIED" or data["outputSha256"] is None):
                raise ToolStoreError("TOOL_EVIDENCE_INVALID")
            _hash(data["outputSha256"])
            if (record.tool_name in _EXECUTIONS) != (data["receipt"] is not None):
                raise ToolStoreError("TOOL_EVIDENCE_INVALID")
            if record.tool_name not in _EXECUTIONS and product is not None:
                raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        else:
            if (outcome is not ToolExecutionOutcome.UNVERIFIED or data["errorKind"] is None
                or product is not None or data["outputSha256"] is not None or data["receipt"] is not None):
                raise ToolStoreError("TOOL_EVIDENCE_INVALID")
            evidence = ToolAttemptEvidence(attempt=data["attempt"], outcome=outcome, evidenceRef=f"artifact://{data['attemptId']}/tool-attempt.json",
                errorKind=data["errorKind"], retrySafe=data["retrySafe"], durationMs=data["durationMs"])
            if decision is RetryDecision.RETRY:
                if (data["attempt"] >= 2 or not data["retrySafe"] or not evidence.can_retry or data["resultUnknown"]):
                    raise ToolStoreError("TOOL_EVIDENCE_RETRY_DENIED")
                if data["errorKind"] in {"PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"} and data["deliveryState"] == "UNKNOWN":
                    raise ToolStoreError("TOOL_EVIDENCE_RETRY_DENIED")
                if record.tool_name in _WRITE_TOOLS and not (data["deliveryState"] == "NOT_SENT"
                    and data["errorKind"] in {"PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"}):
                    raise ToolStoreError("TOOL_EVIDENCE_RETRY_DENIED")
            if data["errorKind"] == "WRITE_RESULT_UNKNOWN" and decision is not RetryDecision.INSPECT_STATE:
                raise ToolStoreError("TOOL_EVIDENCE_RETRY_DENIED")

    def finish(self, binding, token, *, outcome, error_kind=None, retry_safe=False,
        retry_decision=RetryDecision.DO_NOT_RETRY, duration_ms=0, delivery_state="REPLIED",
        result_unknown=False, product_failure_kind=None, output=None):
        _binding(binding)
        if type(token) is not ToolAttemptToken or type(token.attempt) is not int or not 0 <= token.attempt <= 2:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        identity, attempt_id = _uuid(token.logical_call_id), _uuid(token.attempt_id)
        outcome, decision = _enum(outcome, ToolExecutionOutcome), _enum(retry_decision, RetryDecision)
        error_kind = error_kind.value if isinstance(error_kind, (ToolErrorKind, MCPExecutionError)) else error_kind
        product_failure_kind = product_failure_kind.value if isinstance(product_failure_kind, ToolErrorKind) else product_failure_kind
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                record, call = self._load(connection, binding, identity, return_payload=True)
                if (not record.attempts or record.attempts[-1].attempt != token.attempt
                    or record.attempts[-1].attempt_id != attempt_id or record.attempts[-1].status != "STARTED"):
                    raise ToolStoreError("TOOL_EVIDENCE_CONFLICT")
                self._context(connection, binding, record.workflow_step_id, active=outcome is ToolExecutionOutcome.PASS,
                    call=call, source_id=record.source_artifact_id)
                receipt_data = None
                output_hash = None
                if outcome is ToolExecutionOutcome.PASS:
                    if type(output) is not dict:
                        raise ToolStoreError("TOOL_EVIDENCE_INVALID")
                    get_tool_contract(record.tool_name).validate_output(output)
                    output_hash = _digest(output)
                    if record.tool_name in _EXECUTIONS:
                        expected, receipt_data, product = self._receipt(connection, record, _uuid(output.get("executionManifestId")), record.attempts[-1].receipt_rowid_floor)
                        if output != expected or output_hash != _digest(expected):
                            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
                        if product_failure_kind is not None and product_failure_kind != product:
                            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
                        product_failure_kind = product
                elif output is not None:
                    raise ToolStoreError("TOOL_EVIDENCE_INVALID")
                data = {"logicalCallId": str(identity), "attempt": token.attempt, "attemptId": str(attempt_id),
                    "outcome": outcome.value, "errorKind": error_kind, "retrySafe": retry_safe,
                    "retryDecision": decision.value, "durationMs": duration_ms, "deliveryState": delivery_state,
                    "resultUnknown": result_unknown, "productFailureKind": product_failure_kind,
                    "outputSha256": output_hash, "receipt": receipt_data, "finishedAt": utc_now().isoformat()}
                self._validate_finish(record, data)
                self._insert(connection, "tool_execution_attempt_finishes", {"attempt_id": str(attempt_id),
                    "logical_call_id": str(identity), "attempt": token.attempt}, data)
                return self._load(connection, binding, identity)
        except ToolStoreError:
            raise
        except sqlite3.IntegrityError:
            raise ToolStoreError("TOOL_EVIDENCE_CONFLICT") from None
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID") from None

    def _load(self, connection, binding, identity, *, return_payload=False):
        row = connection.execute("SELECT * FROM tool_execution_calls WHERE logical_call_id=?", (str(identity),)).fetchone()
        if row is None:
            raise ToolStoreError("TOOL_EVIDENCE_NOT_FOUND")
        if row["run_id"] != str(binding.run_id) or row["workspace_id"] != str(binding.workspace_id):
            raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
        try:
            data = self._payload(row, _CALL_KEYS)
            ids = {name: _uuid(data[name]) for name in ("logicalCallId", "runId", "workspaceId", "workflowStepId")}
            if (ids["logicalCallId"] != identity or str(ids["runId"]) != row["run_id"]
                or str(ids["workspaceId"]) != row["workspace_id"] or str(ids["workflowStepId"]) != row["workflow_step_id"]
                or data["role"] != binding.role.value or data["toolName"] not in ROLE_TOOL_NAMES[binding.role]):
                raise ToolStoreError("TOOL_EVIDENCE_CONTEXT_DENIED")
            _hash(data["inputSha256"]); _hash(data["configurationSha256"]); _hash(data["runConfigurationSha256"])
            selector = None if data["selectorSha256"] is None else _hash(data["selectorSha256"])
            if data["toolName"] not in _EXECUTIONS and selector is not None:
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            created = _time(data["createdAt"])
            source_id = None if data["sourceArtifactId"] is None else _uuid(data["sourceArtifactId"])
            manifest = None if data["executionManifest"] is None else ExecutionManifest.model_validate(data["executionManifest"])
            if (data["toolName"] in _EXECUTIONS and (source_id is None or manifest is None)
                or data["toolName"] not in _EXECUTIONS and (source_id is not None or manifest is not None)):
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            self._context(connection, binding, ids["workflowStepId"], active=False, call=data, source_id=source_id)
            record = ToolCallRecord(logical_call_id=identity, run_id=ids["runId"], workspace_id=ids["workspaceId"],
                workflow_step_id=ids["workflowStepId"], role=binding.role, tool_name=data["toolName"],
                input_sha256=data["inputSha256"], configuration_sha256=data["configurationSha256"],
                selector_sha256=selector,
                source_artifact_id=source_id, execution_manifest=manifest, attempts=())
            starts = list(connection.execute("SELECT * FROM tool_execution_attempt_starts WHERE logical_call_id=? ORDER BY attempt", (str(identity),)))
            finishes = list(connection.execute("SELECT * FROM tool_execution_attempt_finishes WHERE logical_call_id=? ORDER BY attempt", (str(identity),)))
            if len(starts) > 3 or len(finishes) > len(starts):
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            finished_by_id = {row["attempt_id"]: row for row in finishes}
            attempts = []
            for index, row in enumerate(starts):
                start = self._payload(row, _START_KEYS)
                attempt_id = _uuid(start["attemptId"])
                if (type(start["attempt"]) is not int or start["attempt"] != index or row["attempt"] != index
                    or start["logicalCallId"] != str(identity) or start["attemptId"] != row["attempt_id"]
                    or row["logical_call_id"] != str(identity)):
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                started = _time(start["startedAt"])
                if started < created or attempts and started < _time(attempts[-1].finished_at):
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                floor = start["receiptRowidFloor"]
                if (record.tool_name in _EXECUTIONS and (type(floor) is not int or not 0 <= floor <= 2**63 - 1)
                    or record.tool_name not in _EXECUTIONS and floor is not None):
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                if attempts and (attempts[-1].status != "FINISHED" or attempts[-1].retry_decision is not RetryDecision.RETRY or not attempts[-1].retry_safe):
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                finish_row = finished_by_id.pop(str(attempt_id), None)
                fields = dict(logical_call_id=identity, attempt=index, attempt_id=attempt_id, started_at=start["startedAt"], receipt_rowid_floor=floor)
                if finish_row is None:
                    attempts.append(ToolAttemptRecord(**fields, status="STARTED"))
                    continue
                finish = self._payload(finish_row, _FINISH_KEYS)
                if (finish["logicalCallId"] != str(identity) or finish["attemptId"] != str(attempt_id)
                    or finish["attempt"] != index or finish_row["attempt"] != index
                    or finish_row["logical_call_id"] != str(identity)):
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                self._validate_finish(record, finish)
                if _time(finish["finishedAt"]) < started:
                    raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                receipt = finish["receipt"]
                physical_id = manifest_id = metadata_hash = None
                refs = ()
                if receipt is not None:
                    if type(receipt) is not dict or set(receipt) != {"executionManifestId", "executionId", "metadataSha256", "refs"}:
                        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                    expected, verified, product = self._receipt(connection, record, _uuid(receipt["executionManifestId"]), floor)
                    if receipt != verified or finish["outputSha256"] != _digest(expected) or finish["productFailureKind"] != product:
                        raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                    physical_id, manifest_id, metadata_hash = _uuid(receipt["executionId"]), _uuid(receipt["executionManifestId"]), _hash(receipt["metadataSha256"])
                    for key in ("executionId", "executionManifestId"):
                        count = connection.execute(f"SELECT count(*) FROM tool_execution_attempt_finishes WHERE json_extract(payload_json,'$.receipt.{key}')=?", (receipt[key],)).fetchone()[0]
                        if count != 1:
                            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
                    refs = tuple(receipt["refs"])
                attempts.append(ToolAttemptRecord(**fields, status="FINISHED", finished_at=finish["finishedAt"],
                    outcome=ToolExecutionOutcome(finish["outcome"]), error_kind=finish["errorKind"], retry_safe=finish["retrySafe"],
                    retry_decision=RetryDecision(finish["retryDecision"]), duration_ms=finish["durationMs"],
                    delivery_state=finish["deliveryState"], result_unknown=finish["resultUnknown"],
                    product_failure_kind=finish["productFailureKind"], output_sha256=finish["outputSha256"],
                    execution_id=physical_id, execution_manifest_id=manifest_id, receipt_metadata_sha256=metadata_hash, receipt_refs=refs))
            if finished_by_id:
                raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR")
            record = replace(record, attempts=tuple(attempts))
            return (record, data) if return_payload else record
        except ToolStoreError as exc:
            if exc.code in {"TOOL_EVIDENCE_NOT_FOUND", "TOOL_EVIDENCE_CONTEXT_DENIED", "TOOL_EVIDENCE_INTEGRITY_ERROR"}:
                raise
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR") from None
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_INTEGRITY_ERROR") from None

    def get(self, binding, logical_call_id):
        _binding(binding)
        identity = _uuid(logical_call_id)
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                return self._load(connection, binding, identity)
        except ToolStoreError:
            raise
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None

    def read_evidence(self, binding, artifactref):
        _binding(binding)
        if type(artifactref) is not str:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        match = re.fullmatch(r"artifact://([0-9a-f-]{36})/(tool-execution-evidence|tool-attempt)\.json", artifactref)
        if match is None:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        identity = _uuid(match[1])
        if str(identity) != match[1]:
            raise ToolStoreError("TOOL_EVIDENCE_INVALID")
        if match[2] == "tool-execution-evidence":
            return self.get(binding, identity).to_dict()
        try:
            with self._repository._transaction() as connection:
                self._ensure_schema(connection)
                row = connection.execute("SELECT logical_call_id FROM tool_execution_attempt_starts WHERE attempt_id=?", (str(identity),)).fetchone()
                if row is None:
                    raise ToolStoreError("TOOL_EVIDENCE_NOT_FOUND")
                record = self._load(connection, binding, _uuid(row[0]))
                return next(item.to_dict() for item in record.attempts if item.attempt_id == identity)
        except ToolStoreError:
            raise
        except Exception:
            raise ToolStoreError("TOOL_EVIDENCE_STORAGE_ERROR") from None
