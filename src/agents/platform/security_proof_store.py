"""Private immutable receipts from an explicitly trusted semantic assessor.

These records bind independently assessed outcomes to a registered frozen
Source. They are not Agent/Tool output schemas, public Artifacts, signatures,
or proof that an assessor is honest. Only hashes and bounded registry pointers
are persisted; Source bytes, Host paths and assessment prose are not copied.
"""

from contextlib import closing
from dataclasses import dataclass, field, replace
from hashlib import sha256
import json
import re
import sqlite3
from urllib.parse import parse_qsl, quote, unquote, urlsplit
from uuid import RFC_4122, UUID

from agents.llm.contracts import parse_json
from agents.runtime.qa_context import validation_cycle
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.core.security import redact_text
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import RequirementValidator, ScenarioDefinition
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.workspaces.policy import relative_parts


_TABLE = "security_semantic_proof_receipts"
_MAX_METADATA_BYTES = 1_048_576
_OUTCOMES = frozenset({"PASS", "FAIL", "UNVERIFIED"})
_DISPOSITIONS = frozenset({"CONFIRMED", "FALSE_POSITIVE", "SUSPECTED", "UNVERIFIED"})
_UUID_FIELDS = ("receipt_id", "request_id", "run_id", "workspace_id", "workflow_step_id", "source_artifact_id")
_HASH_FIELDS = ("snapshot_sha256", "approval_sha256", "request_sha256", "assessment_sha256", "evidence_sha256")
_FIELDS = frozenset((*_UUID_FIELDS, *_HASH_FIELDS, "code_version", "attempt",
                     "requirement_outcomes", "finding_dispositions", "evidence_refs"))
_FINDING_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,511}\Z", re.ASCII)


class SecurityProofReceiptError(ValueError):
    """Only a stable reason; no submitted IDs, paths or underlying exceptions."""

    code = "SECURITY_PROOF_RECEIPT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _uuid(value):
    if type(value) is not UUID or value.version != 4 or value.variant != RFC_4122:
        raise ValueError
    return value


def _digest(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError


def _reference(value):
    """Registry syntax only; the assessor adapter authenticates membership.

    Source anchors may use the existing percent-encoded path and line-range
    convention. No registry fetching, Host path conversion or path I/O occurs.
    """
    if (type(value) is not str or not value or len(value.encode("utf-8")) > 4096
            or not value.startswith("artifact://")
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value)
            or redact_text(value) != value):
        raise ValueError
    parsed = urlsplit(value)
    authority = UUID(parsed.netloc)
    _uuid(authority)
    if (parsed.scheme != "artifact" or parsed.netloc != str(authority) or parsed.query
            or not parsed.path.startswith("/") or parsed.path.startswith("//")):
        raise ValueError
    logical = unquote(parsed.path[1:], encoding="utf-8", errors="strict")
    relative_parts(logical)
    if redact_text(logical) != logical or quote(logical, safe="/") != parsed.path[1:]:
        raise ValueError
    if parsed.fragment:
        if logical != "source.tar":
            raise ValueError
        items = parse_qsl(parsed.fragment, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=3)
        names = tuple(name for name, _value in items)
        if names not in (("path",), ("path", "startLine", "endLine")):
            raise ValueError
        path = items[0][1]
        relative_parts(path)
        if redact_text(path) != path:
            raise ValueError
        encoded = parsed.fragment.split("&", 1)[0][len("path="):]
        if encoded not in {quote(path, safe="/"), quote(path, safe="")}:
            raise ValueError
        if len(items) == 3:
            numbers = tuple(value for _name, value in items[1:])
            if (any(re.fullmatch(r"[1-9][0-9]{0,7}", value) is None for value in numbers)
                    or int(numbers[0]) > int(numbers[1])
                    or parsed.fragment != "path=" + encoded + "&startLine=" + numbers[0] + "&endLine=" + numbers[1]):
                raise ValueError


@dataclass(frozen=True, kw_only=True, repr=False)
class SecurityProofReceipt:
    receipt_id: UUID = field(repr=False)
    request_id: UUID = field(repr=False)
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)
    workflow_step_id: UUID = field(repr=False)
    source_artifact_id: UUID = field(repr=False)
    code_version: int
    attempt: int
    snapshot_sha256: str = field(repr=False)
    approval_sha256: str = field(repr=False)
    request_sha256: str = field(repr=False)
    assessment_sha256: str = field(repr=False)
    evidence_sha256: str = field(repr=False)
    requirement_outcomes: tuple = field(default=(), repr=False)
    finding_dispositions: tuple = field(default=(), repr=False)
    evidence_refs: tuple = field(default=(), repr=False)

    def __repr__(self):
        return "SecurityProofReceipt()"

    def __post_init__(self):
        invalid = False
        try:
            for name in _UUID_FIELDS:
                _uuid(getattr(self, name))
            for name in _HASH_FIELDS:
                _digest(getattr(self, name))
            if (type(self.code_version) is not int or self.code_version < 1
                    or type(self.attempt) is not int or self.attempt < 0
                    or type(self.requirement_outcomes) is not tuple or len(self.requirement_outcomes) > 256
                    or type(self.finding_dispositions) is not tuple or len(self.finding_dispositions) > 1000
                    or type(self.evidence_refs) is not tuple or len(self.evidence_refs) > 256):
                raise ValueError
            for pair in self.requirement_outcomes:
                if (type(pair) is not tuple or len(pair) != 2 or type(pair[1]) is not str
                        or pair[1] not in _OUTCOMES):
                    raise ValueError
                _uuid(pair[0])
            for pair in self.finding_dispositions:
                if (type(pair) is not tuple or len(pair) != 2 or type(pair[0]) is not str
                        or _FINDING_ID.fullmatch(pair[0]) is None or redact_text(pair[0]) != pair[0]
                        or type(pair[1]) is not str or pair[1] not in _DISPOSITIONS):
                    raise ValueError
            if (len(dict(self.requirement_outcomes)) != len(self.requirement_outcomes)
                    or len(dict(self.finding_dispositions)) != len(self.finding_dispositions)
                    or len(set(self.evidence_refs)) != len(self.evidence_refs)):
                raise ValueError
            for reference in self.evidence_refs:
                _reference(reference)
            if sum(len(reference.encode("utf-8")) for reference in self.evidence_refs) > 256 * 1024:
                raise ValueError
            _json(_payload(self))
        except Exception:
            invalid = True
        if invalid:
            raise SecurityProofReceiptError() from None

    @property
    def receipt_ref(self):
        return f"artifact://{self.receipt_id}/security-semantic-proof.json"


def _payload(record):
    result = {name: str(getattr(record, name)) for name in _UUID_FIELDS}
    result.update({name: getattr(record, name) for name in _HASH_FIELDS})
    result.update(schemaVersion=1, code_version=record.code_version, attempt=record.attempt,
        requirement_outcomes=[[str(identifier), outcome] for identifier, outcome in record.requirement_outcomes],
        finding_dispositions=[list(pair) for pair in record.finding_dispositions],
        evidence_refs=list(record.evidence_refs))
    return result


def _json(payload):
    raw = json.dumps(payload, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise ValueError
    return raw


def _parse_uuid(value):
    if type(value) is not str:
        raise ValueError
    parsed = UUID(value)
    _uuid(parsed)
    if str(parsed) != value:
        raise ValueError
    return parsed


def _decode(raw):
    data = parse_json(raw, max_bytes=_MAX_METADATA_BYTES)
    if (type(data) is not dict or set(data) != _FIELDS | {"schemaVersion"}
            or type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1
            or any(type(data[name]) is not list for name in (
                "requirement_outcomes", "finding_dispositions", "evidence_refs"))):
        raise ValueError
    requirement_pairs, finding_pairs = data["requirement_outcomes"], data["finding_dispositions"]
    if any(type(pair) is not list or len(pair) != 2 for pair in (*requirement_pairs, *finding_pairs)):
        raise ValueError
    fields = {name: _parse_uuid(data[name]) for name in _UUID_FIELDS}
    fields.update({name: data[name] for name in (*_HASH_FIELDS, "code_version", "attempt")})
    fields.update(requirement_outcomes=tuple((_parse_uuid(pair[0]), pair[1]) for pair in requirement_pairs),
        finding_dispositions=tuple(tuple(pair) for pair in finding_pairs), evidence_refs=tuple(data["evidence_refs"]))
    receipt = SecurityProofReceipt(**fields)
    if _json(_payload(receipt)) != raw:
        raise ValueError
    return receipt


def _model(raw, model):
    data = parse_json(raw, max_bytes=_MAX_METADATA_BYTES)
    if model in (WorkflowRun, WorkflowStep):
        # These mutable workflow models predate strict JSON counters. A
        # malformed stored bool/float must not be coerced into this subject.
        counters = ("fix_attempt", "code_version") if model is WorkflowRun else ("attempt", "code_version")
        if type(data) is not dict:
            raise ValueError
        for name in counters:
            if (name not in data or (data[name] is None and name != "code_version")
                    or data[name] is not None and type(data[name]) is not int):
                raise ValueError
    return model.model_validate(data)


class SecurityProofReceiptStore:
    """One shared Workflow repository; construction does not read or write."""

    def __init__(self, repository):
        if type(repository) is not SQLiteWorkflowRepository:
            raise SecurityProofReceiptError()
        self._repository = repository

    def __repr__(self):
        return "SecurityProofReceiptStore()"

    @staticmethod
    def _schema(connection):
        connection.execute("""CREATE TABLE IF NOT EXISTS security_semantic_proof_receipts (
            receipt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE,
            run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
            workspace_id TEXT NOT NULL REFERENCES workspaces(workspace_id),
            workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
            source_artifact_id TEXT NOT NULL REFERENCES artifact_contents(artifact_id),
            code_version INTEGER NOT NULL CHECK(typeof(code_version)='integer' AND code_version>0),
            attempt INTEGER NOT NULL CHECK(typeof(attempt)='integer' AND attempt>=0),
            metadata_json TEXT NOT NULL CHECK(json_valid(metadata_json) AND length(metadata_json)<=1048576),
            metadata_sha256 TEXT NOT NULL CHECK(typeof(metadata_sha256)='text' AND length(metadata_sha256)=64
                AND metadata_sha256 NOT GLOB '*[^0-9a-f]*')
        )""")
        connection.execute("CREATE INDEX IF NOT EXISTS security_proof_receipts_by_run "
                           "ON security_semantic_proof_receipts(run_id,receipt_id)")
        for operation in ("UPDATE", "DELETE"):
            connection.execute(f"CREATE TRIGGER IF NOT EXISTS security_proof_receipts_no_{operation.lower()} "
                f"BEFORE {operation} ON security_semantic_proof_receipts "
                "BEGIN SELECT RAISE(ABORT,'immutable Security proof receipt'); END")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS security_proof_receipts_no_replace
            BEFORE INSERT ON security_semantic_proof_receipts
            WHEN EXISTS(SELECT 1 FROM security_semantic_proof_receipts
                WHERE receipt_id=NEW.receipt_id OR request_id=NEW.request_id)
            BEGIN SELECT RAISE(ABORT,'immutable Security proof receipt'); END""")
        connection.execute("""CREATE TRIGGER IF NOT EXISTS security_proof_receipts_ownership
            BEFORE INSERT ON security_semantic_proof_receipts
            WHEN NOT EXISTS(SELECT 1 FROM workspaces WHERE workspace_id=NEW.workspace_id AND run_id=NEW.run_id)
                OR NOT EXISTS(SELECT 1 FROM workflow_steps WHERE workflow_step_id=NEW.workflow_step_id AND run_id=NEW.run_id)
                OR NOT EXISTS(SELECT 1 FROM project_artifacts WHERE artifact_id=NEW.source_artifact_id
                    AND run_id=NEW.run_id AND artifact_type='SOURCE')
            BEGIN SELECT RAISE(ABORT,'Security proof ownership mismatch'); END""")

    @staticmethod
    def _subject(connection, receipt, *, active):
        run_row = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (str(receipt.run_id),)).fetchone()
        config_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(receipt.run_id),)).fetchone()
        workspace_row = connection.execute("SELECT * FROM workspaces WHERE workspace_id=?", (str(receipt.workspace_id),)).fetchone()
        if run_row is None or config_row is None or workspace_row is None:
            raise ValueError
        run = _model(run_row["payload_json"], WorkflowRun)
        config = _model(config_row[0], RunConfigurationArtifact)
        workspace = _model(workspace_row["payload_json"], WorkspaceRecord)
        if (run_row["run_id"] != str(run.run_id) or run.run_id != receipt.run_id
                or run_row["status"] != run.status.value or run.workspace_id != receipt.workspace_id
                or config.run_id != run.run_id or config.workspace_id != run.workspace_id
                or config.scenario_id != run.scenario_id or workspace.workspace_id != receipt.workspace_id
                or workspace_row["workspace_id"] != str(receipt.workspace_id)
                or workspace_row["run_id"] != str(receipt.run_id) or workspace.run_id != receipt.run_id):
            raise ValueError
        scenario = ScenarioDefinition.from_contract(config.scenario_contract)
        steps = []
        for row in connection.execute("SELECT * FROM workflow_steps WHERE run_id=? LIMIT 1025", (str(receipt.run_id),)):
            step = _model(row["payload_json"], WorkflowStep)
            if (row["workflow_step_id"] != str(step.workflow_step_id) or row["run_id"] != str(receipt.run_id)
                    or step.run_id != receipt.run_id or row["status"] != step.status.value):
                raise ValueError
            steps.append(step)
        if len(steps) > 1024:
            raise ValueError
        selected = [step for step in steps if step.workflow_step_id == receipt.workflow_step_id]
        if len(selected) != 1:
            raise ValueError
        step = selected[0]
        source_row = connection.execute("SELECT * FROM project_artifacts WHERE artifact_id=? AND run_id=?",
            (str(receipt.source_artifact_id), str(receipt.run_id))).fetchone()
        if source_row is None or source_row["artifact_type"] != "SOURCE":
            raise ValueError
        source = _model(source_row["payload_json"], CodeSnapshotArtifact)
        allowed = set(scenario.requirement_ids_for(RequirementValidator.SECURITY))
        if (source.artifact_id != receipt.source_artifact_id or source.run_id != receipt.run_id
                or source_row["artifact_version"] != source.artifact_version
                or source.code_version != receipt.code_version or source.artifact_version != receipt.code_version
                or source.snapshot_sha256 != receipt.snapshot_sha256 or step.agent_role is not AgentRole.SECURITY
                or step.code_version != receipt.code_version or not step.requirement_ids
                or not set(step.requirement_ids) <= allowed
                or not set(step.requirement_ids) <= set(source.requirement_ids)
                or not set(dict(receipt.requirement_outcomes)) <= set(step.requirement_ids)
                or step.input_artifact_ids != [source.artifact_id]):
            raise ValueError
        environment = config.configuration.environment
        if (environment is None or environment.network_policy != "DENY"
                or config.configuration.scanner_profile_ref is None
                or source.container_image_digest != environment.container_image_digest
                or source.dependency_lock_hash != environment.dependency_lock_hash):
            raise ValueError
        stored_row = connection.execute("SELECT * FROM artifact_contents WHERE artifact_id=? AND run_id=?",
            (str(receipt.source_artifact_id), str(receipt.run_id))).fetchone()
        if stored_row is None:
            raise ValueError
        stored = SQLiteArtifactContentStore._decode(connection, stored_row)
        excluded = {"a2a_task_id", "a2a_artifact_id"}
        if (type(stored.metadata) is not CodeSnapshotArtifact
                or stored.metadata.model_dump(mode="json", by_alias=True, exclude=excluded)
                != source.model_dump(mode="json", by_alias=True, exclude=excluded)
                or source.artifact_uri != f"artifact://{source.artifact_id}/source.tar"):
            raise ValueError
        if active:
            validation_cycle(run)
            security = [item for item in steps if item.agent_role is AgentRole.SECURITY
                        and item.status is WorkflowStepStatus.RUNNING]
            if (run.code_version != receipt.code_version or len(security) != 1
                    or security[0].workflow_step_id != step.workflow_step_id
                    or step.status is not WorkflowStepStatus.RUNNING or step.attempt != receipt.attempt):
                raise ValueError

    @staticmethod
    def _row(connection, row):
        raw = row["metadata_json"]
        if type(raw) is not str:
            raise ValueError
        _digest(row["metadata_sha256"])
        if sha256(raw.encode("utf-8")).hexdigest() != row["metadata_sha256"]:
            raise ValueError
        receipt = _decode(raw)
        for name in _UUID_FIELDS:
            if row[name] != str(getattr(receipt, name)):
                raise ValueError
        if row["code_version"] != receipt.code_version or row["attempt"] != receipt.attempt:
            raise ValueError
        SecurityProofReceiptStore._subject(connection, receipt, active=False)
        return receipt

    def publish(self, receipt):
        """Persist once only in the current Security validation scope.

        An idempotent match still requires a currently valid scope. Completion
        does not authorize new evidence; historical reads remain available.
        """
        try:
            if type(receipt) is not SecurityProofReceipt:
                raise ValueError
            checked = replace(receipt)
            raw = _json(_payload(checked))
            with self._repository._transaction() as connection:
                self._subject(connection, checked, active=True)
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)).fetchone():
                    matches = connection.execute("SELECT * FROM security_semantic_proof_receipts "
                        "WHERE receipt_id=? OR request_id=?", (str(checked.receipt_id), str(checked.request_id))).fetchall()
                    if matches:
                        if len(matches) != 1 or self._row(connection, matches[0]) != checked:
                            raise ValueError
                        return checked
                self._schema(connection)
                connection.execute("INSERT INTO security_semantic_proof_receipts "
                    "(receipt_id,request_id,run_id,workspace_id,workflow_step_id,source_artifact_id,"
                    "code_version,attempt,metadata_json,metadata_sha256) VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        *(str(getattr(checked, name)) for name in _UUID_FIELDS), checked.code_version,
                        checked.attempt, raw, sha256(raw.encode("utf-8")).hexdigest()))
                return checked
        except Exception:
            pass
        raise SecurityProofReceiptError() from None

    def get(self, run_id, receipt_id):
        """Read-only lookup, including terminal Runs; never create a database."""
        try:
            _uuid(run_id)
            _uuid(receipt_id)
            uri = self._repository.database_path.absolute().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                row = connection.execute("SELECT * FROM security_semantic_proof_receipts "
                    "WHERE run_id=? AND receipt_id=?", (str(run_id), str(receipt_id))).fetchone()
                if row is None:
                    raise ValueError
                return self._row(connection, row)
        except Exception:
            pass
        raise SecurityProofReceiptError() from None
