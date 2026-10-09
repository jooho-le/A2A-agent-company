"""Single owned Host's append-only accounting beside canonical Run Trace.

No provider body, prompt, source, Tool argument or stdout is accepted. This is
not a distributed worker lease or a pricing calculator.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from agents.llm.budget import BudgetSnapshot
from agents.llm.contracts import UsageRecord
from orchestrator.core.security import redact_data, redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.domain.trace import TraceEvent


class TelemetryStoreError(ValueError):
    """Stable storage reason; never attach submitted accounting or SQL bodies."""

    def __init__(self):
        super().__init__("AGENT_TELEMETRY_UNAVAILABLE")


class TelemetryBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True,
                              hide_input_in_errors=True)

    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str = Field(alias="a2aTaskId", min_length=1)
    agent_context_id: str = Field(alias="agentContextId", min_length=1)
    role: AgentRole
    attempt: int = Field(ge=0, strict=True)
    code_version: int | None = Field(default=None, ge=1, strict=True, alias="codeVersion")
    requirement_ids: tuple[UUID4, ...] = Field(default=(), alias="requirementIds")
    input_artifact_ids: tuple[UUID4, ...] = Field(default=(), alias="inputArtifactIds")
    snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$", alias="snapshotSha256")

    @field_validator("a2a_task_id", "agent_context_id")
    @classmethod
    def nonblank_opaque_reference(cls, value):
        if not value.strip():
            raise ValueError("blank opaque reference")
        return value

    @field_validator("requirement_ids", "input_artifact_ids")
    @classmethod
    def unique_ids(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("duplicate project reference")
        return value


class UsageDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True,
                              hide_input_in_errors=True)

    sequence: int = Field(ge=1, strict=True)
    role: AgentRole
    requested_model: dict = Field(alias="requestedModel")
    reported_model_id: str | None = Field(default=None, alias="reportedModelId", max_length=256)
    outcome: Literal["completed", "incomplete", "failed", "refused", "canceled",
                     "LLM_CONFIGURATION_ERROR", "LLM_PROVIDER_ERROR", "LLM_AUTH_REQUIRED",
                     "LLM_RESPONSE_INVALID", "LLM_REFUSED", "LLM_RESPONSE_INCOMPLETE",
                     "LLM_SCHEMA_INVALID", "LLM_BUDGET_EXHAUSTED", "LLM_EXECUTION_TIMEOUT",
                     "LLM_TOOL_POLICY_VIOLATION", "LLM_TOOL_EXECUTION_FAILED"]
    duration_ms: int = Field(ge=0, strict=True, alias="durationMs")
    usage_known: bool = Field(alias="usageKnown", strict=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True, alias="inputTokens")
    output_tokens: int | None = Field(default=None, ge=0, strict=True, alias="outputTokens")
    total_tokens: int | None = Field(default=None, ge=0, strict=True, alias="totalTokens")
    cached_input_tokens: int | None = Field(default=None, ge=0, strict=True, alias="cachedInputTokens")
    reasoning_output_tokens: int | None = Field(default=None, ge=0, strict=True, alias="reasoningOutputTokens")
    cost_usd: None = Field(default=None, alias="costUsd")


class EventDetail(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True,
                              hide_input_in_errors=True)

    sequence: int | None = Field(default=None, ge=1, strict=True)
    requested_model: dict | None = Field(default=None, alias="requestedModel")
    tool_name: str | None = Field(default=None, alias="toolName", pattern=r"^[a-z][a-z0-9_]{0,63}$")
    logical_call_id: str | None = Field(default=None, alias="logicalCallId", pattern=r"^[0-9a-f-]{36}$")
    tool_attempt: int | None = Field(default=None, alias="toolAttempt", ge=0, le=2, strict=True)
    attempt_id: str | None = Field(default=None, alias="attemptId", pattern=r"^[0-9a-f-]{36}$")
    status: Literal["STARTED", "FINISHED"] | None = None
    outcome: Literal["PASS", "FAIL", "UNVERIFIED"] | None = None
    error_kind: str | None = Field(default=None, alias="errorKind", pattern=r"^[A-Z][A-Z_]{0,63}$")
    retry_decision: Literal["RETRY", "DO_NOT_RETRY", "INSPECT_STATE"] | None = Field(default=None, alias="retryDecision")
    delivery_state: Literal["NOT_SENT", "REPLIED", "UNKNOWN"] | None = Field(default=None, alias="deliveryState")
    result_unknown: bool | None = Field(default=None, alias="resultUnknown", strict=True)
    execution_id: str | None = Field(default=None, alias="executionId", pattern=r"^[0-9a-f-]{36}$")
    execution_manifest_id: UUID4 | None = Field(default=None, alias="executionManifestId")
    evidence_ref: str | None = Field(default=None, alias="evidenceRef", max_length=512)
    input_sha256: str | None = Field(default=None, alias="inputSha256", pattern=r"^[0-9a-f]{64}$")
    configuration_sha256: str | None = Field(default=None, alias="configurationSha256", pattern=r"^[0-9a-f]{64}$")
    workflow_attempt: int | None = Field(default=None, alias="workflowAttempt", ge=0, strict=True)

    @field_validator("error_kind")
    @classmethod
    def stable_error_only(cls, value):
        if value is not None:
            from mcp_tools.runtime import MCPExecutionError
            from orchestrator.domain.retry_policy import ToolErrorKind
            allowed = {item.value for item in MCPExecutionError} | {item.value for item in ToolErrorKind}
            if value not in allowed | {"UNKNOWN_ERROR", "CANCELLED"}:
                raise ValueError("unrecognized stable Tool error")
        return value


def _safe_model(model):
    from orchestrator.domain.run_configuration import ModelConfiguration
    validated = ModelConfiguration.model_validate(model)
    result = validated.model_dump(mode="json", by_alias=True)
    for field in ("provider", "modelId", "modelRevision"):
        value = result.get(field)
        if value is not None:
            clean = redact_text(value)
            result[field] = clean if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", clean) else "[REDACTED]"
    return result


def _json(value):
    return json.dumps(redact_data(value), ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


class AgentTelemetryStore:
    """SQLite append-only accounting, opt-in through the owned Host composition."""

    def __init__(self, database_path: str | Path, *, clock: Callable[[], datetime] | None = None):
        self._database_path = Path(database_path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        try:
            with self._connection() as connection:
                # The existing repository owns WAL setup and the Run/Trace schema.
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS agent_budget_ledger (
                        run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
                        revision INTEGER NOT NULL,
                        payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
                    );
                    CREATE TABLE IF NOT EXISTS agent_runtime_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        trace_event_id TEXT NOT NULL UNIQUE REFERENCES trace_events(event_id),
                        run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                        workflow_step_id TEXT REFERENCES workflow_steps(workflow_step_id),
                        kind TEXT NOT NULL CHECK(kind IN ('LLM','MCP')),
                        idempotency_key TEXT NOT NULL,
                        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                        UNIQUE(run_id, kind, idempotency_key)
                    );
                    CREATE INDEX IF NOT EXISTS agent_telemetry_by_run
                        ON agent_runtime_events(run_id,sequence);
                    CREATE TRIGGER IF NOT EXISTS agent_telemetry_no_update
                        BEFORE UPDATE ON agent_runtime_events
                        BEGIN SELECT RAISE(ABORT,'accounting is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS agent_telemetry_no_delete
                        BEFORE DELETE ON agent_runtime_events
                        BEGIN SELECT RAISE(ABORT,'accounting is append-only'); END;
                    CREATE TRIGGER IF NOT EXISTS agent_budget_no_delete
                        BEFORE DELETE ON agent_budget_ledger
                        BEGIN SELECT RAISE(ABORT,'budget ledger cannot be reset'); END;
                """)
        except Exception:
            raise TelemetryStoreError() from None

    def __repr__(self):
        return "AgentTelemetryStore()"

    @property
    def database_path(self):
        return self._database_path

    def now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise TelemetryStoreError()
        return value.astimezone(timezone.utc)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def admit_budget(self, snapshot: BudgetSnapshot) -> BudgetSnapshot:
        try:
            if type(snapshot) is not BudgetSnapshot or snapshot.revision != 0:
                raise ValueError()
            with self._transaction() as connection:
                row = connection.execute("SELECT payload_json FROM agent_budget_ledger WHERE run_id=?",
                                         (str(snapshot.run_id),)).fetchone()
                if row is not None:
                    existing = self._snapshot(row)
                    if (existing.configuration_fingerprint != snapshot.configuration_fingerprint
                            or existing.limits != snapshot.limits
                            or existing.created_at_utc != snapshot.created_at_utc
                            or existing.deadline_utc != snapshot.deadline_utc):
                        raise ValueError()
                    return existing
                if (snapshot.model_calls or snapshot.tool_calls or snapshot.known_total_tokens
                        or not snapshot.usage_complete or snapshot.invalidated):
                    raise ValueError()
                now = self.now()
                if now < snapshot.created_at_utc or now >= snapshot.deadline_utc:
                    raise ValueError()
                saved = BudgetSnapshot.model_validate({**snapshot.model_dump(), "last_observed_utc": now})
                connection.execute("INSERT INTO agent_budget_ledger(run_id,revision,payload_json) VALUES(?,?,?)",
                                   (str(saved.run_id), 0, saved.model_dump_json()))
                return saved
        except Exception:
            raise TelemetryStoreError() from None

    def _snapshot(self, row, *, validate_clock=True) -> BudgetSnapshot:
        snapshot = BudgetSnapshot.model_validate_json(row["payload_json"])
        if "revision" in row.keys() and row["revision"] != snapshot.revision:
            raise TelemetryStoreError()
        if validate_clock and self.now() < snapshot.last_observed_utc:
            raise TelemetryStoreError()
        return snapshot

    def load_budget(self, run_id: UUID, *, validate_clock=True) -> BudgetSnapshot | None:
        try:
            with self._connection() as connection:
                row = connection.execute("SELECT revision,payload_json FROM agent_budget_ledger WHERE run_id=?",
                                         (str(run_id),)).fetchone()
            return self._snapshot(row, validate_clock=validate_clock) if row is not None else None
        except Exception:
            raise TelemetryStoreError() from None

    def save_budget(self, snapshot: BudgetSnapshot, expected_revision: int) -> BudgetSnapshot:
        try:
            if type(snapshot) is not BudgetSnapshot or type(expected_revision) is not int:
                raise ValueError()
            with self._transaction() as connection:
                row = connection.execute("SELECT revision,payload_json FROM agent_budget_ledger WHERE run_id=?",
                                         (str(snapshot.run_id),)).fetchone()
                if row is None:
                    raise ValueError()
                previous = self._snapshot(row)
                if (previous.revision != expected_revision or snapshot.revision != expected_revision
                        or previous.configuration_fingerprint != snapshot.configuration_fingerprint
                        or previous.limits != snapshot.limits
                        or previous.created_at_utc != snapshot.created_at_utc
                        or previous.deadline_utc != snapshot.deadline_utc
                        or snapshot.model_calls < previous.model_calls
                        or snapshot.tool_calls < previous.tool_calls
                        or snapshot.known_total_tokens < previous.known_total_tokens
                        or (not previous.usage_complete and snapshot.usage_complete)
                        or (previous.invalidated and not snapshot.invalidated)
                        or not set(previous.accounted_tool_sequences).issubset(snapshot.accounted_tool_sequences)
                        or any(snapshot.accounted_usage.get(sequence, object()) != usage
                               for sequence, usage in previous.accounted_usage.items())):
                    raise ValueError()
                saved = BudgetSnapshot.model_validate({**snapshot.model_dump(),
                    "last_observed_utc": self.now(), "revision": previous.revision + 1})
                cursor = connection.execute("UPDATE agent_budget_ledger SET revision=?,payload_json=? WHERE run_id=? AND revision=?",
                    (saved.revision, saved.model_dump_json(), str(saved.run_id), previous.revision))
                if cursor.rowcount != 1:
                    raise ValueError()
                return saved
        except Exception:
            raise TelemetryStoreError() from None

    def append_usage(self, binding: dict | TelemetryBinding, record: UsageRecord) -> dict:
        try:
            bound = binding if type(binding) is TelemetryBinding else TelemetryBinding.model_validate(binding)
            if type(record) is not UsageRecord or record.role != bound.role or record.cost_usd is not None:
                raise ValueError()
            usage = record.usage
            detail = UsageDetail(
                sequence=record.sequence, role=record.role,
                requestedModel=_safe_model(record.requested_model),
                reportedModelId=(redact_text(record.reported_model_id)
                    if isinstance(record.reported_model_id, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", redact_text(record.reported_model_id)) else None),
                outcome=record.outcome,
                durationMs=record.duration_ms, usageKnown=usage is not None,
                inputTokens=usage.input_tokens if usage else None,
                outputTokens=usage.output_tokens if usage else None,
                totalTokens=usage.total_tokens if usage else None,
                cachedInputTokens=usage.cached_input_tokens if usage else None,
                reasoningOutputTokens=usage.reasoning_output_tokens if usage else None,
            )
            event = TraceEvent(runId=bound.run_id, workflowStepId=bound.workflow_step_id,
                a2aTaskId=bound.a2a_task_id, agentContextId=bound.agent_context_id,
                eventType="LLM_CALL_FINISHED", occurredAt=self.now(), actor=bound.role.value,
                attempt=bound.attempt, requirementIds=list(bound.requirement_ids),
                inputArtifactIds=list(bound.input_artifact_ids), codeVersion=bound.code_version,
                snapshotSha256=bound.snapshot_sha256, durationMs=record.duration_ms)
            return self._append(event, bound.model_dump(mode="json", by_alias=True),
                                detail.model_dump(mode="json", by_alias=True), "LLM", "usage:" + str(record.sequence), "USAGE")
        except Exception:
            raise TelemetryStoreError() from None

    def append_event(self, event: TraceEvent, *, kind: Literal["LLM", "MCP"],
                     detail: dict, idempotency_key: str) -> dict:
        try:
            if type(event) is not TraceEvent or kind not in ("LLM", "MCP"):
                raise ValueError()
            if (not isinstance(idempotency_key, str) or not idempotency_key
                    or len(idempotency_key) > 256 or redact_text(idempotency_key) != idempotency_key):
                raise ValueError()
            typed = EventDetail.model_validate(detail)
            if kind == "LLM" and (typed.sequence is None or typed.requested_model is None
                    or set(detail) - {"sequence", "requestedModel"}
                    or event.event_type != "LLM_MODEL_CALLED"):
                raise ValueError()
            if kind == "MCP" and (typed.sequence is not None or typed.requested_model is not None
                    or typed.workflow_attempt is None or typed.tool_attempt != event.attempt):
                raise ValueError()
            if kind == "MCP" and (event.event_type not in {"MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"}
                    or typed.tool_name is None or typed.logical_call_id is None or typed.attempt_id is None):
                raise ValueError()
            safe = typed.model_dump(mode="json", by_alias=True, exclude_none=True)
            if typed.requested_model is not None:
                safe["requestedModel"] = _safe_model(typed.requested_model)
            if typed.evidence_ref is not None and not re.fullmatch(
                    r"(?:artifact://[0-9a-f-]{36}/[A-Za-z0-9._/-]+|mcp-journal://[0-9a-f-]{36})", typed.evidence_ref):
                raise ValueError()
            return self._append(event, event.to_trace_json(), safe, kind, idempotency_key, "EVENT")
        except Exception:
            raise TelemetryStoreError() from None

    def _append(self, event, binding, detail, kind, key, record_type):
        payload_json = _json({"binding": binding, "detail": detail, "recordType": record_type})
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT trace_event_id,payload_json FROM agent_runtime_events WHERE run_id=? AND kind=? AND idempotency_key=?",
                (str(event.run_id), kind, key)).fetchone()
            if existing is not None:
                previous = json.loads(existing["payload_json"])
                # Event timestamp and random eventId are not an accounting identity.
                if record_type == "EVENT":
                    previous["binding"].pop("eventId", None)
                    previous["binding"].pop("occurredAt", None)
                    current = json.loads(payload_json)
                    current["binding"].pop("eventId", None)
                    current["binding"].pop("occurredAt", None)
                    identical = _json(previous) == _json(current)
                else:
                    identical = existing["payload_json"] == payload_json
                if not identical:
                    raise TelemetryStoreError()
                return {"eventId": existing["trace_event_id"], "kind": kind, **previous["detail"]}
            self._verify_binding(connection, event, detail, kind, record_type)
            connection.execute(
                "INSERT INTO trace_events(event_id,run_id,occurred_at,event_type,payload_json) VALUES(?,?,?,?,?)",
                (str(event.event_id), str(event.run_id), event.occurred_at.isoformat(), event.event_type,
                 _json(event.to_trace_json())))
            connection.execute(
                "INSERT INTO agent_runtime_events(trace_event_id,run_id,workflow_step_id,kind,idempotency_key,payload_json) VALUES(?,?,?,?,?,?)",
                (str(event.event_id), str(event.run_id), str(event.workflow_step_id) if event.workflow_step_id else None,
                 kind, key, payload_json))
        return {"eventId": str(event.event_id), "kind": kind, **json.loads(payload_json)["detail"]}

    def _verify_binding(self, connection, event, detail, kind, record_type):
        from orchestrator.domain.models import WorkflowStep
        row = connection.execute("SELECT payload_json FROM workflow_steps WHERE workflow_step_id=? AND run_id=?",
                                 (str(event.workflow_step_id), str(event.run_id))).fetchone()
        if row is None:
            raise TelemetryStoreError()
        step = WorkflowStep.model_validate_json(row[0])
        attempt = detail.get("workflowAttempt") if kind == "MCP" else event.attempt
        if (event.actor != step.agent_role.value or step.attempt != attempt
                or step.code_version != event.code_version
                or step.requirement_ids != event.requirement_ids
                or step.input_artifact_ids != event.input_artifact_ids
                or not event.a2a_task_id or not event.agent_context_id
                or step.a2a_task_id not in (None, event.a2a_task_id)
                or step.agent_context_id not in (None, event.agent_context_id)):
            raise TelemetryStoreError()
        if record_type == "USAGE":
            row = connection.execute("SELECT payload_json FROM agent_budget_ledger WHERE run_id=?",
                                     (str(event.run_id),)).fetchone()
            if row is not None:
                snapshot = BudgetSnapshot.model_validate_json(row[0])
                sequence = detail["sequence"]
                if sequence not in snapshot.accounted_usage:
                    raise TelemetryStoreError()
                expected = snapshot.accounted_usage[sequence]
                if ((expected is None) != (not detail["usageKnown"])
                        or (expected is not None and any(detail[key] != value for key, value in (
                            ("inputTokens", expected.input_tokens), ("outputTokens", expected.output_tokens),
                            ("totalTokens", expected.total_tokens), ("cachedInputTokens", expected.cached_input_tokens),
                            ("reasoningOutputTokens", expected.reasoning_output_tokens))))):
                    raise TelemetryStoreError()
        if kind == "LLM":
            row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?",
                                     (str(event.run_id),)).fetchone()
            if row is None:
                raise TelemetryStoreError()
            from orchestrator.domain.run_configuration import RunConfigurationArtifact
            frozen = RunConfigurationArtifact.model_validate_json(row[0]).configuration.model
            if frozen is None or _safe_model(frozen) != detail["requestedModel"]:
                raise TelemetryStoreError()

    def audit_budget(self, snapshot: BudgetSnapshot) -> None:
        """Execution-only recovery guard; incomplete accounting stays readable."""
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT json_extract(payload_json,'$.detail') FROM agent_runtime_events WHERE run_id=? AND json_extract(payload_json,'$.recordType')='USAGE'",
                    (str(snapshot.run_id),)).fetchall()
            details = [UsageDetail.model_validate_json(row[0]) for row in rows]
            if {detail.sequence for detail in details} != set(snapshot.accounted_usage):
                raise ValueError()
            for detail in details:
                usage = snapshot.accounted_usage[detail.sequence]
                if ((usage is None) != (not detail.usage_known)
                        or usage is not None and (usage.input_tokens != detail.input_tokens
                            or usage.output_tokens != detail.output_tokens or usage.total_tokens != detail.total_tokens
                            or usage.cached_input_tokens != detail.cached_input_tokens
                            or usage.reasoning_output_tokens != detail.reasoning_output_tokens)):
                    raise ValueError()
        except Exception:
            raise TelemetryStoreError() from None

    def list_usage(self, run_id: UUID, *, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        return self._list(run_id, usage_only=True, limit=limit, offset=offset)

    def list_events(self, run_id: UUID, *, limit: int = 100, offset: int = 0) -> tuple[list[dict], int]:
        return self._list(run_id, usage_only=False, limit=limit, offset=offset)

    def _list(self, run_id, *, usage_only, limit, offset):
        try:
            if type(limit) is not int or not 1 <= limit <= 500 or type(offset) is not int or offset < 0:
                raise ValueError()
            clause = "run_id=?" + (" AND json_extract(payload_json,'$.recordType')='USAGE'" if usage_only else "")
            values = (str(run_id),)
            with self._connection() as connection:
                total = connection.execute(f"SELECT COUNT(*) FROM agent_runtime_events WHERE {clause}", values).fetchone()[0]
                rows = connection.execute(
                    f"SELECT trace_event_id,kind,payload_json FROM agent_runtime_events WHERE {clause} ORDER BY sequence LIMIT ? OFFSET ?",
                    (*values, limit, offset)).fetchall()
            return ([{"eventId": row["trace_event_id"], "kind": row["kind"],
                      **json.loads(row["payload_json"]), **json.loads(row["payload_json"])["detail"]} for row in rows], total)
        except Exception:
            raise TelemetryStoreError() from None

    def usage_summary(self, run_id: UUID) -> dict:
        try:
            with self._connection() as connection:
                rows = connection.execute(
                    "SELECT json_extract(payload_json,'$.detail') FROM agent_runtime_events WHERE run_id=? AND json_extract(payload_json,'$.recordType')='USAGE' ORDER BY sequence",
                    (str(run_id),)).fetchall()
            details = [UsageDetail.model_validate_json(row[0]) for row in rows]
            known = sum(detail.total_tokens for detail in details if detail.total_tokens is not None)
            complete = all(detail.usage_known for detail in details)
            snapshot = self.load_budget(run_id, validate_clock=False)
            missing = []
            if snapshot is not None:
                # Reserved calls with no receipt are not zero-token calls.
                complete = complete and snapshot.usage_complete and not snapshot.pending_model_sequences
                known = max(known, snapshot.known_total_tokens)
                missing = sorted(set(snapshot.accounted_usage) - {detail.sequence for detail in details})
            return {"runId": str(run_id), "modelCalls": snapshot.model_calls if snapshot else len(details),
                    "toolCalls": snapshot.tool_calls if snapshot else None,
                    "recordedModelCalls": len(details), "usageComplete": complete,
                    "knownTotalTokens": known, "totalTokens": known if complete else None,
                    "costUsd": None, "pendingModelSequences": list(snapshot.pending_model_sequences) if snapshot else [],
                    "pendingToolSequences": list(snapshot.pending_tool_sequences) if snapshot else [],
                    "missingUsageSequences": missing,
                    "executionBlocked": bool(snapshot and (snapshot.invalidated or not snapshot.usage_complete
                        or snapshot.pending_model_sequences or snapshot.pending_tool_sequences or missing))}
        except Exception:
            raise TelemetryStoreError() from None
