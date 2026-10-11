"""Private immutable Run prices and actual-usage cost receipts.

The caller owns its SQLite connection and transaction. Construction and reads
never create a database or table, and historical quotes use only the already
frozen descriptor, not today's approval file. No model-supplied amount enters
this ledger.
"""

from hashlib import sha256
import json
import re
import sqlite3
from uuid import RFC_4122, UUID

from pydantic import BaseModel, ConfigDict, Field, UUID4

from agents.llm.budget import BudgetSnapshot
from agents.llm.contracts import LLMErrorCode, TokenUsage, UsageRecord, parse_json
from agents.platform.pricing import (
    ApprovedPriceBook, quote_frozen_usage, validate_frozen_price_book,
)
from orchestrator.core.security import redact_text
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import ModelConfiguration, RunConfigurationArtifact
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.trace import TraceEvent


_POLICY = "agent_run_pricing"
_RECEIPTS = "agent_llm_cost_receipts"
_MAX_JSON = 1_048_576
_OUTCOMES = {"completed", "incomplete", "failed", "refused", "canceled"} | {item.value for item in LLMErrorCode}
_RESULT_KEYS = frozenset({"runId", "sequence", "usageEventId", "usageSha256", "priceBookSha256",
                          "costUsd", "pricingEntryId", "costStatus", "calculationVersion"})


class PricingStoreError(ValueError):
    code = "RUN_PRICING_UNAVAILABLE"

    def __init__(self):
        super().__init__(self.code)


class _UsageDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, hide_input_in_errors=True)

    sequence: int = Field(ge=1, strict=True)
    role: AgentRole
    requested_model: dict = Field(alias="requestedModel")
    reported_model_id: str | None = Field(default=None, alias="reportedModelId", max_length=256)
    reported_service_tier: str | None = Field(default=None, alias="reportedServiceTier", max_length=256)
    billing_context: str | None = Field(default=None, alias="billingContext", max_length=256)
    outcome: str
    duration_ms: int = Field(ge=0, strict=True, alias="durationMs")
    usage_known: bool = Field(alias="usageKnown", strict=True)
    input_tokens: int | None = Field(default=None, ge=0, strict=True, alias="inputTokens")
    output_tokens: int | None = Field(default=None, ge=0, strict=True, alias="outputTokens")
    total_tokens: int | None = Field(default=None, ge=0, strict=True, alias="totalTokens")
    cached_input_tokens: int | None = Field(default=None, ge=0, strict=True, alias="cachedInputTokens")
    reasoning_output_tokens: int | None = Field(default=None, ge=0, strict=True, alias="reasoningOutputTokens")
    cache_write_input_tokens: int | None = Field(default=None, ge=0, strict=True, alias="cacheWriteInputTokens")
    cost_usd: None = Field(default=None, alias="costUsd")


class _Binding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True, hide_input_in_errors=True)

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


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _uuid(value):
    return type(value) is UUID and value.version == 4 and value.variant == RFC_4122


def _arguments(connection, run_id, sequence=None, *, write=False):
    if (type(connection) is not sqlite3.Connection or connection.row_factory is not sqlite3.Row
            or not _uuid(run_id) or write and not connection.in_transaction
            or sequence is not None and (type(sequence) is not int or sequence < 1)):
        raise ValueError


def _has_table(connection, name):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _run(connection, run_id):
    row = connection.execute("SELECT run_id,status,payload_json FROM workflow_runs WHERE run_id=?", (str(run_id),)).fetchone()
    if row is None:
        raise ValueError
    raw = parse_json(row["payload_json"], max_bytes=_MAX_JSON)
    for key in ("fix_attempt", "code_version"):
        if raw.get(key) is not None and type(raw[key]) is not int:
            raise ValueError
    run = WorkflowRun.model_validate(raw)
    if row["run_id"] != str(run_id) or run.run_id != run_id or row["status"] != run.status.value:
        raise ValueError
    return run


def _initial(connection, run):
    if run.status is not WorkflowStatus.PLANNING or run.fix_attempt != 0 or run.code_version is not None:
        raise ValueError
    rows = connection.execute("SELECT * FROM workflow_steps WHERE run_id=?", (str(run.run_id),)).fetchall()
    if len(rows) != 1:
        raise ValueError
    raw = parse_json(rows[0]["payload_json"], max_bytes=_MAX_JSON)
    if type(raw.get("attempt")) is not int or raw.get("code_version") is not None:
        raise ValueError
    step = WorkflowStep.model_validate(raw)
    if (step.run_id != run.run_id or rows[0]["workflow_step_id"] != str(step.workflow_step_id)
            or rows[0]["status"] != step.status.value or step.agent_role is not AgentRole.PLANNER
            or step.status is not WorkflowStepStatus.RUNNING or step.attempt != 0
            or step.requirement_ids or step.input_artifact_ids or step.output_artifact_ids or step.a2a_artifact_ids):
        raise ValueError
    if (_has_table(connection, "agent_runtime_events") and connection.execute(
            "SELECT 1 FROM agent_runtime_events WHERE run_id=? AND kind='LLM' LIMIT 1", (str(run.run_id),)).fetchone() is not None):
        raise ValueError
    if connection.execute("SELECT 1 FROM trace_events WHERE run_id=? AND event_type IN ('LLM_MODEL_CALLED','LLM_CALL_FINISHED') LIMIT 1",
                          (str(run.run_id),)).fetchone() is not None:
        raise ValueError
    if _has_table(connection, "agent_budget_ledger"):
        row = connection.execute("SELECT payload_json FROM agent_budget_ledger WHERE run_id=?", (str(run.run_id),)).fetchone()
        if row is not None:
            budget = BudgetSnapshot.model_validate_json(row[0])
            if (budget.run_id != run.run_id or budget.model_calls not in (0, 1) or budget.accounted_usage
                    or budget.pending_model_sequences != (() if budget.model_calls == 0 else (1,))
                    or budget.tool_calls or not budget.usage_complete or budget.invalidated):
                raise ValueError


def _schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_run_pricing (
        run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
        price_book_sha256 TEXT NOT NULL,
        descriptor_json TEXT NOT NULL CHECK(json_valid(descriptor_json)),
        metadata_sha256 TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS agent_llm_cost_receipts (
        run_id TEXT NOT NULL REFERENCES agent_run_pricing(run_id),
        model_sequence INTEGER NOT NULL CHECK(typeof(model_sequence)='integer' AND model_sequence>0),
        usage_event_id TEXT NOT NULL UNIQUE REFERENCES trace_events(event_id),
        price_book_sha256 TEXT NOT NULL,
        usage_sha256 TEXT NOT NULL,
        payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
        metadata_sha256 TEXT NOT NULL,
        PRIMARY KEY(run_id,model_sequence)
    )""")
    for table, unique in ((_POLICY, "run_id=NEW.run_id"), (_RECEIPTS, "(run_id=NEW.run_id AND model_sequence=NEW.model_sequence) OR usage_event_id=NEW.usage_event_id")):
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} "
                               "BEGIN SELECT RAISE(ABORT,'immutable pricing'); END")
        connection.execute(f"CREATE TRIGGER IF NOT EXISTS {table}_no_replace BEFORE INSERT ON {table} "
                           f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {unique}) "
                           "BEGIN SELECT RAISE(ABORT,'immutable pricing'); END")


def _policy(connection, run_id):
    _run(connection, run_id)
    row = None if not _has_table(connection, _POLICY) else connection.execute(
        "SELECT * FROM agent_run_pricing WHERE run_id=?", (str(run_id),)).fetchone()
    if row is None:
        if _has_table(connection, _RECEIPTS) and connection.execute(
                "SELECT 1 FROM agent_llm_cost_receipts WHERE run_id=? LIMIT 1", (str(run_id),)).fetchone() is not None:
            raise ValueError
        return None
    descriptor = parse_json(row["descriptor_json"], max_bytes=_MAX_JSON)
    validate_frozen_price_book(descriptor)
    book_hash = _hash(descriptor)
    if (row["run_id"] != str(run_id) or row["descriptor_json"] != _json(descriptor)
            or row["price_book_sha256"] != book_hash
            or row["metadata_sha256"] != _hash({"runId": str(run_id), "descriptor": descriptor, "priceBookSha256": book_hash})):
        raise ValueError
    return {"descriptor": descriptor, "priceBookSha256": book_hash}


def _usage(connection, run_id, sequence):
    if not _has_table(connection, "agent_runtime_events"):
        return None
    rows = connection.execute("SELECT * FROM agent_runtime_events WHERE run_id=? AND kind='LLM' "
                              "AND json_extract(payload_json,'$.recordType')='USAGE' "
                              "AND json_extract(payload_json,'$.detail.sequence')=?", (str(run_id), sequence)).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise ValueError
    row = rows[0]
    payload = parse_json(row["payload_json"], max_bytes=_MAX_JSON)
    if (type(payload) is not dict or set(payload) != {"recordType", "binding", "detail"}
            or payload["recordType"] != "USAGE" or row["idempotency_key"] != "usage:" + str(sequence)):
        raise ValueError
    detail, binding = _UsageDetail.model_validate(payload["detail"]), _Binding.model_validate(payload["binding"])
    if (detail.sequence != sequence or binding.run_id != run_id or detail.role is not binding.role
            or row["workflow_step_id"] != str(binding.workflow_step_id) or detail.outcome not in _OUTCOMES
            or not binding.a2a_task_id.strip() or not binding.agent_context_id.strip()
            or len(set(binding.requirement_ids)) != len(binding.requirement_ids)
            or len(set(binding.input_artifact_ids)) != len(binding.input_artifact_ids)):
        raise ValueError
    for value in (detail.reported_model_id, detail.reported_service_tier, detail.billing_context):
        if value is not None and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", value) is None:
            raise ValueError
    values = (detail.input_tokens, detail.output_tokens, detail.total_tokens,
              detail.cached_input_tokens, detail.reasoning_output_tokens, detail.cache_write_input_tokens)
    if detail.usage_known:
        if any(value is None for value in values[:3]):
            raise ValueError
        usage = TokenUsage(*values[:3], cached_input_tokens=values[3], reasoning_output_tokens=values[4], cache_write_input_tokens=values[5])
    else:
        if any(value is not None for value in values):
            raise ValueError
        usage = None
    model = ModelConfiguration.model_validate(detail.requested_model)
    if _json(model.model_dump(mode="json", by_alias=True)) != _json(detail.requested_model):
        raise ValueError
    configuration_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(run_id),)).fetchone()
    step_row = connection.execute("SELECT payload_json FROM workflow_steps WHERE workflow_step_id=? AND run_id=?",
                                  (str(binding.workflow_step_id), str(run_id))).fetchone()
    if configuration_row is None or step_row is None:
        raise ValueError
    frozen = RunConfigurationArtifact.model_validate_json(configuration_row[0])
    step = WorkflowStep.model_validate_json(step_row[0])
    if (frozen.run_id != run_id or frozen.configuration.model is None
            or step.run_id != run_id or step.workflow_step_id != binding.workflow_step_id or step.agent_role is not binding.role):
        raise ValueError
    selected_model = frozen.configuration.model.model_dump(mode="json", by_alias=True)
    # Match the existing factual telemetry's safe projection, not raw secrets.
    for key in ("provider", "modelId", "modelRevision"):
        if selected_model[key] is not None:
            safe = redact_text(selected_model[key])
            selected_model[key] = safe if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", safe) else "[REDACTED]"
    if _json(selected_model) != _json(detail.requested_model):
        raise ValueError
    if _has_table(connection, "agent_budget_ledger"):
        budget_row = connection.execute("SELECT payload_json FROM agent_budget_ledger WHERE run_id=?", (str(run_id),)).fetchone()
        if budget_row is not None:
            raw_budget = parse_json(budget_row[0], max_bytes=8 * _MAX_JSON)
            budget = BudgetSnapshot.model_validate(raw_budget)
            if budget.run_id != run_id or sequence not in budget.accounted_usage:
                raise ValueError
            original = raw_budget["accounted_usage"][str(sequence)]
            if original is not None:
                # Do not let Pydantic coercion turn corrupted bool/float counts
                # into a seemingly matching observed provider token receipt.
                if any(value is not None and type(value) is not int for value in original.values()):
                    raise ValueError
                original = TokenUsage(**original)
            if original != usage or budget.accounted_usage[sequence] != usage:
                raise ValueError
    record = UsageRecord(sequence=sequence, role=detail.role, requested_model=model,
        reported_model_id=detail.reported_model_id, outcome=detail.outcome,
        duration_ms=detail.duration_ms, usage=usage, reported_service_tier=detail.reported_service_tier,
        billing_context=detail.billing_context)
    trace_row = connection.execute("SELECT * FROM trace_events WHERE event_id=?", (row["trace_event_id"],)).fetchone()
    if trace_row is None:
        raise ValueError
    trace = TraceEvent.model_validate_json(trace_row["payload_json"])
    if (str(trace.event_id) != row["trace_event_id"] or trace_row["run_id"] != str(run_id)
            or trace_row["event_type"] != "LLM_CALL_FINISHED" or trace.event_type != "LLM_CALL_FINISHED"
            or trace.run_id != run_id or trace.workflow_step_id != binding.workflow_step_id
            or trace.actor != binding.role.value or trace.attempt != binding.attempt
            or trace.a2a_task_id != binding.a2a_task_id or trace.agent_context_id != binding.agent_context_id
            or trace.code_version != binding.code_version or tuple(trace.requirement_ids) != binding.requirement_ids
            or tuple(trace.input_artifact_ids) != binding.input_artifact_ids or trace.snapshot_sha256 != binding.snapshot_sha256
            or trace.duration_ms != detail.duration_ms):
        raise ValueError
    # Bind every byte of the actually persisted original usage payload, not a
    # projection, decoded/re-serialized detail or a mutable response object.
    return record, row["trace_event_id"], sha256(row["payload_json"].encode("utf-8")).hexdigest()


def _result(policy, run_id, sequence, usage):
    record, event_id, usage_hash = usage
    quote = quote_frozen_usage(policy["descriptor"], record)
    return {"runId": str(run_id), "sequence": sequence, "usageEventId": event_id, "usageSha256": usage_hash,
            "priceBookSha256": policy["priceBookSha256"], "costUsd": quote.cost_usd,
            "pricingEntryId": quote.entry_id, "costStatus": quote.reason,
            "calculationVersion": policy["descriptor"]["calculationVersion"]}


def _public(policy, result):
    book = policy["descriptor"]["priceBook"]
    return {key: result[key] for key in ("costUsd", "pricingEntryId", "costStatus", "priceBookSha256", "calculationVersion")} | {
        "priceBookId": book["priceBookId"], "priceBookVersion": book["priceBookVersion"]}


def _receipt(connection, run_id, sequence, policy, usage):
    row = None if not _has_table(connection, _RECEIPTS) else connection.execute(
        "SELECT * FROM agent_llm_cost_receipts WHERE run_id=? AND model_sequence=?", (str(run_id), sequence)).fetchone()
    if row is None:
        return None
    if policy is None or usage is None:
        raise ValueError
    result = parse_json(row["payload_json"], max_bytes=_MAX_JSON)
    if (type(result) is not dict or set(result) != _RESULT_KEYS or row["payload_json"] != _json(result)
            or row["metadata_sha256"] != _hash(result) or _json(result) != _json(_result(policy, run_id, sequence, usage))
            or row["run_id"] != result["runId"] or row["model_sequence"] != result["sequence"]
            or row["usage_event_id"] != result["usageEventId"] or row["usage_sha256"] != result["usageSha256"]
            or row["price_book_sha256"] != result["priceBookSha256"]):
        raise ValueError
    return _public(policy, result)


class RunPricingLedger:
    def __repr__(self):
        return "RunPricingLedger()"

    def pin(self, connection, run_id, book):
        try:
            _arguments(connection, run_id, write=True)
            if type(book) is not ApprovedPriceBook:
                raise ValueError
            book.verify_integrity()
            descriptor, book_hash = book.descriptor, book.sha256
            validate_frozen_price_book(descriptor)
            if _hash(descriptor) != book_hash:
                raise ValueError
            existing = _policy(connection, run_id)
            if existing is not None:
                if existing != {"descriptor": descriptor, "priceBookSha256": book_hash}:
                    raise ValueError
                return None
            _initial(connection, _run(connection, run_id))
            _schema(connection)
            connection.execute("INSERT INTO agent_run_pricing(run_id,price_book_sha256,descriptor_json,metadata_sha256) VALUES(?,?,?,?)",
                (str(run_id), book_hash, _json(descriptor), _hash({"runId": str(run_id), "descriptor": descriptor, "priceBookSha256": book_hash})))
            return None
        except Exception:
            pass
        raise PricingStoreError() from None

    def require(self, connection, run_id, book):
        try:
            _arguments(connection, run_id)
            if type(book) is not ApprovedPriceBook:
                raise ValueError
            book.verify_integrity()
            existing = _policy(connection, run_id)
            if existing is None or existing != {"descriptor": book.descriptor, "priceBookSha256": book.sha256}:
                raise ValueError
            return None
        except Exception:
            pass
        raise PricingStoreError() from None

    def policy(self, connection, run_id):
        try:
            _arguments(connection, run_id)
            return _policy(connection, run_id)
        except Exception:
            pass
        raise PricingStoreError() from None

    def publish(self, connection, run_id, sequence):
        try:
            _arguments(connection, run_id, sequence, write=True)
            policy, usage = _policy(connection, run_id), _usage(connection, run_id, sequence)
            if policy is None or usage is None:
                raise ValueError
            saved = _receipt(connection, run_id, sequence, policy, usage)
            if saved is not None:
                return saved
            result = _result(policy, run_id, sequence, usage)
            connection.execute("INSERT INTO agent_llm_cost_receipts(run_id,model_sequence,usage_event_id,price_book_sha256,usage_sha256,payload_json,metadata_sha256) VALUES(?,?,?,?,?,?,?)",
                (str(run_id), sequence, result["usageEventId"], result["priceBookSha256"], result["usageSha256"], _json(result), _hash(result)))
            return _public(policy, result)
        except Exception:
            pass
        raise PricingStoreError() from None

    def get(self, connection, run_id, sequence):
        try:
            _arguments(connection, run_id, sequence)
            policy = _policy(connection, run_id)
            if policy is None:
                return None
            usage = _usage(connection, run_id, sequence)
            saved = _receipt(connection, run_id, sequence, policy, usage)
            if saved is None and usage is not None:
                raise ValueError
            return saved
        except Exception:
            pass
        raise PricingStoreError() from None
