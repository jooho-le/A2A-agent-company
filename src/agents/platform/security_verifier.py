"""Approved, request-bound Host analysis; never a default product validator.

An explicitly approved independent assessor consumes immutable measured bytes.
Its approval is a trust decision, not a signature or proof that its reasoning
is correct. No model-selected callback, project execution or I/O fallback is
provided here. Missing criteria/evidence never become a synthetic PASS.
"""

import asyncio
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field, replace
from hashlib import sha256
import inspect
import json
import re
import sqlite3
from types import MappingProxyType
from urllib.parse import quote
from uuid import RFC_4122, UUID, uuid4

from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, parse_json
from agents.platform.composition import _host_call
from agents.platform.host_services import _model_json
from agents.platform.security_approval import ApprovedSecurityAssessor
from agents.platform.security_proof_store import SecurityProofReceipt, SecurityProofReceiptStore
from agents.roles.security_contract import SecurityDecision, validate_security_decision
from agents.runtime.security_services import SecurityMeasuredBundle, SecuritySemanticProof
from mcp_tools.execution_policy import arguments_sha256
from mcp_tools.execution_store import ToolExecutionStore, _digest
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.snapshots import FrozenSourceSelection, SnapshotReader
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class SecurityVerifierError(ValueError):
    code = "SECURITY_SEMANTIC_VERIFICATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _uuid(value):
    return type(value) is UUID and value.version == 4 and value.variant == RFC_4122


def _sha(value):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _assessment_hash(request_id, request_sha256, approval_sha256, requirements,
                     findings, evidence_refs, evidence_sha256):
    return _hash({"requestId": str(request_id), "requestSha256": request_sha256,
        "approvalSha256": approval_sha256,
        "requirements": [[str(key), value] for key, value in requirements],
        "findings": [list(pair) for pair in findings], "evidenceRefs": list(evidence_refs),
        "evidenceSha256": evidence_sha256})


def _decision_data(decision):
    def references(review):
        return [{"path": item.path, "startLine": item.start_line, "endLine": item.end_line}
                for item in review.references]
    return {"kind": decision.kind, "questions": list(decision.questions),
        "requirementReviews": [{"requirementId": str(review.requirement_id),
            "proposedOutcome": review.proposed_outcome, "rationale": review.rationale,
            "references": references(review)} for review in decision.requirement_reviews],
        "findingReviews": [{"findingId": review.finding_id,
            "proposedDisposition": review.proposed_disposition, "rationale": review.rationale,
            "references": references(review)} for review in decision.finding_reviews]}


@dataclass(frozen=True, kw_only=True, repr=False)
class SecurityAssessmentRequest:
    """Issued only by the adapter, with detached data and a fresh challenge."""

    request_id: UUID
    request_sha256: str
    approval_sha256: str
    run_id: UUID
    workspace_id: UUID
    workflow_step_id: UUID
    source_artifact_id: UUID
    snapshot_sha256: str
    requirement_ids: tuple
    finding_ids: tuple
    permitted_evidence_refs: tuple
    _payload_json: str = field(repr=False)
    _source_files: Mapping = field(repr=False)

    def __repr__(self):
        return "SecurityAssessmentRequest()"

    @property
    def source_files(self):
        return MappingProxyType(dict(self._source_files))

    def _get(self, name):
        return parse_json(self._payload_json, max_bytes=8 * 1024 * 1024)[name]

    @property
    def criteria(self):
        return self._get("criteria")

    @property
    def scenario_contract(self):
        return self._get("scenarioContract")

    @property
    def decision(self):
        return self._get("modelProposal")

    @property
    def scans(self):
        return self._get("scans")

    @property
    def execution_manifest(self):
        return self._get("executionManifest")


@dataclass(frozen=True, kw_only=True, repr=False)
class SecurityAssessment:
    """Independent Host result, not a model result with authority attached.

    Evidence JSON contains independently produced observations. Only its hash
    is persisted. Neither an evidence URI nor scanner success establishes the
    validity of the assessor's semantic analysis; that is the approved Host
    implementation's responsibility.
    """

    request_id: UUID
    request_sha256: str
    approval_sha256: str
    requirement_outcomes: tuple = ()
    finding_dispositions: tuple = ()
    evidence_refs: tuple = ()
    evidence_json: str = field(default="{}", repr=False)

    def __repr__(self):
        return "SecurityAssessment()"

    def __post_init__(self):
        try:
            if (not _uuid(self.request_id) or not _sha(self.request_sha256) or not _sha(self.approval_sha256)
                    or type(self.requirement_outcomes) is not tuple or len(self.requirement_outcomes) > 256
                    or type(self.finding_dispositions) is not tuple or len(self.finding_dispositions) > 1000
                    or type(self.evidence_refs) is not tuple or len(self.evidence_refs) > 256
                    or len(set(self.evidence_refs)) != len(self.evidence_refs)
                    or type(self.evidence_json) is not str):
                raise ValueError
            for pairs, key_check, values in (
                (self.requirement_outcomes, _uuid, {"PASS", "FAIL", "UNVERIFIED"}),
                (self.finding_dispositions, lambda value: type(value) is str and 0 < len(value.encode("utf-8")) <= 512,
                 {"CONFIRMED", "FALSE_POSITIVE", "SUSPECTED", "UNVERIFIED"}),
            ):
                for pair in pairs:
                    if (type(pair) is not tuple or len(pair) != 2 or not key_check(pair[0])
                            or type(pair[1]) is not str or pair[1] not in values):
                        raise ValueError
                if len(dict(pairs)) != len(pairs):
                    raise ValueError
            for ref in self.evidence_refs:
                if type(ref) is not str or not 0 < len(ref.encode("utf-8")) <= 4096 or not ref.startswith("artifact://"):
                    raise ValueError
            observations = parse_json(self.evidence_json, max_bytes=65536)
            if type(observations) is not dict:
                raise ValueError
            sanitize_content({"observations": observations, "evidenceRefs": list(self.evidence_refs),
                              "findingIds": [key for key, _value in self.finding_dispositions]}, reject_secrets=True)
            certified = any(value != "UNVERIFIED" for _key, value in self.requirement_outcomes) or any(
                value in {"CONFIRMED", "FALSE_POSITIVE"} for _key, value in self.finding_dispositions)
            if certified and (not observations or not self.evidence_refs):
                raise ValueError
            object.__setattr__(self, "evidence_json", _json(observations))
        except Exception:
            raise SecurityVerifierError() from None


class TrustedSecurityProofVerifier:
    """One approved independent analysis capability, bound to Host storage."""

    def __init__(self, repository, approved_assessor, *, artifact_store):
        if (type(repository) is not SQLiteWorkflowRepository or type(approved_assessor) is not ApprovedSecurityAssessor
                or type(artifact_store) is not ArtifactStore or artifact_store._repository is not repository):
            raise SecurityVerifierError()
        self._repository, self._approval, self._artifacts = repository, approved_assessor, artifact_store
        self._receipts = SecurityProofReceiptStore(repository)

    def __repr__(self):
        return "TrustedSecurityProofVerifier()"

    @property
    def descriptor(self):
        return self._approval.descriptor

    def verify_integrity(self):
        try:
            self._approval.verify_integrity()
        except Exception:
            raise SecurityVerifierError() from None

    def _request(self, execution, decision, measured, request_id):
        try:
            self.verify_integrity()
            metadata, frozen, source = execution.metadata, execution.configuration, execution.source
            if (not _uuid(request_id) or type(metadata) is not A2AWorkflowMetadata
                    or type(frozen) is not RunConfigurationArtifact or type(source) is not CodeSnapshotArtifact
                    or type(decision) is not SecurityDecision or decision.kind != "READY"
                    or type(measured) is not SecurityMeasuredBundle):
                raise ValueError
            for model in (metadata, frozen, source):
                copied = type(model).model_validate_json(_model_json(model))
                if _model_json(copied) != _model_json(model):
                    raise ValueError
            if (frozen.scenario_contract_sha256 != self._approval.scenario_contract_sha256
                    or measured.run_id != metadata.run_id or measured.workflow_step_id != metadata.workflow_step_id
                    or measured.source_artifact_id != source.artifact_id or measured.snapshot_sha256 != source.snapshot_sha256
                    or metadata.run_id != frozen.run_id or metadata.scenario_id != frozen.scenario_id
                    or source.run_id != frozen.run_id or source.code_version != metadata.code_version
                    or metadata.project_artifact_ids != (source.artifact_id,) or not metadata.requirement_ids
                    or type(measured.scans) is not tuple or not measured.scans):
                raise ValueError
            binding = MCPBinding(role=AgentRole.SECURITY, agent_role=AgentRole.SECURITY,
                run_id=frozen.run_id, workspace_id=frozen.workspace_id)
            self._artifacts.verify_candidate(source)
            staged, files = SnapshotReader(self._artifacts)._load(binding,
                FrozenSourceSelection(project_artifact_id=source.artifact_id, snapshot_sha256=source.snapshot_sha256))
            if (staged.execution_manifest() != source.execution_manifest()
                    or not isinstance(measured.source_files, Mapping) or dict(measured.source_files) != files):
                raise ValueError
            uri = self._repository.database_path.absolute().as_uri() + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=10.0)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA query_only=ON")
                connection.execute("BEGIN")
                run_row = connection.execute("SELECT * FROM workflow_runs WHERE run_id=?", (str(frozen.run_id),)).fetchone()
                cfg_row = connection.execute("SELECT payload_json FROM run_configurations WHERE run_id=?", (str(frozen.run_id),)).fetchone()
                if run_row is None or cfg_row is None:
                    raise ValueError
                run = WorkflowRun.model_validate_json(run_row["payload_json"])
                stored_frozen = RunConfigurationArtifact.model_validate_json(cfg_row[0])
                steps = [WorkflowStep.model_validate_json(row[0]) for row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id=?", (str(run.run_id),))]
                active = [step for step in steps if step.agent_role is AgentRole.SECURITY and step.status is WorkflowStepStatus.RUNNING]
                if (run.run_id != frozen.run_id or run.workspace_id != frozen.workspace_id
                        or run.scenario_id != frozen.scenario_id or run_row["status"] != run.status.value
                        or run.status not in {WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING}
                        or run.code_version != source.code_version or run.fix_attempt + 1 != source.code_version
                        or _model_json(stored_frozen) != _model_json(frozen) or len(active) != 1
                        or active[0].workflow_step_id != metadata.workflow_step_id
                        or active[0].attempt != metadata.attempt or active[0].code_version != source.code_version
                        or active[0].input_artifact_ids != [source.artifact_id]
                        or tuple(active[0].requirement_ids) != metadata.requirement_ids):
                    raise ValueError
                registered = connection.execute("SELECT payload_json FROM project_artifacts WHERE artifact_id=? AND run_id=? AND artifact_type='SOURCE'",
                    (str(source.artifact_id), str(run.run_id))).fetchone()
                if registered is None or _model_json(CodeSnapshotArtifact.model_validate_json(registered[0])) != _model_json(source):
                    raise ValueError
                tools, scans, fingerprints, finding_items = ToolExecutionStore(self._repository), [], [], []
                for expected_record, expected_receipt, expected_evidence in measured.scans:
                    record = tools._load(connection, binding, expected_record.logical_call_id)
                    row = connection.execute("SELECT * FROM security_scan_execution_records WHERE execution_manifest_id=? AND run_id=?",
                        (str(expected_receipt.execution_manifest_id), str(run.run_id))).fetchone()
                    if row is None:
                        raise ValueError
                    receipt = SecurityScanOutputStore._decode(connection, row)
                    evidence = record.to_tool_evidence()
                    if (record != expected_record or receipt != expected_receipt or evidence != expected_evidence
                            or record.workflow_step_id != metadata.workflow_step_id
                            or record.source_artifact_id != source.artifact_id or record.tool_name != "run_security_scan"
                            or record.role is not AgentRole.SECURITY or evidence.outcome is not ToolExecutionOutcome.PASS
                            or record.execution_manifest != source.execution_manifest()
                            or record.input_sha256 != arguments_sha256("run_security_scan", {
                                "workspaceId": str(frozen.workspace_id), "snapshotId": str(source.artifact_id), "scannerProfile": receipt.profile_name})
                            or record.attempts[-1].output_sha256 != _digest(receipt.tool_output())
                            or receipt.workflow_step_id != metadata.workflow_step_id
                            or receipt.source_artifact_id != source.artifact_id
                            or receipt.workspace_id != frozen.workspace_id
                            or receipt.execution_manifest != source.execution_manifest()):
                        raise ValueError
                    scans.append({"journal": record.to_dict(), "receiptId": str(receipt.execution_manifest_id),
                        "metadataSha256": receipt.metadata_sha256, "reportSha256": receipt.report_sha256,
                        "reportRef": receipt.report_ref, "report": receipt.report.to_dict()})
                    fingerprints.append(receipt.execution_manifest_id)
                    finding_items.extend((receipt.profile_name, item, receipt) for item in receipt.report.findings)
                if len(set(fingerprints)) != len(fingerprints) or len(finding_items) != len(measured.findings):
                    raise ValueError
                for (identity, selector, finding, receipt), actual in zip(measured.findings, finding_items):
                    if (type(identity) is not str or (selector, finding, receipt) != actual):
                        raise ValueError
                proposal = _decision_data(decision)
                checked = validate_security_decision(proposal, metadata.requirement_ids, measured.finding_ids, tuple(files))
                if _json(_decision_data(checked)) != _json(proposal):
                    raise ValueError
            source_refs = [f"artifact://{source.artifact_id}/source.tar#path=" + quote(path, safe="/") for path in sorted(files)]
            refs = tuple((*source_refs, *(scan["journal"]["evidenceRef"] for scan in scans), *(scan["reportRef"] for scan in scans)))
            payload = {"schemaVersion": 1, "requestId": str(request_id), "approvalSha256": self._approval.approval_sha256,
                "approval": self.descriptor, "criteria": self._approval.criteria,
                "runId": str(frozen.run_id), "workspaceId": str(frozen.workspace_id), "workflowStepId": str(metadata.workflow_step_id),
                "attempt": metadata.attempt, "fixAttempt": run.fix_attempt,
                "sourceArtifactId": str(source.artifact_id), "codeVersion": source.code_version,
                "snapshotSha256": source.snapshot_sha256, "executionManifest": source.execution_manifest().model_dump(mode="json", by_alias=True),
                "configurationSha256": _hash(parse_json(_model_json(frozen))), "scenarioContract": frozen.scenario_contract,
                "modelProposal": proposal, "scans": scans, "findingIds": list(measured.finding_ids),
                "sourceInventory": [{"path": name, "sha256": sha256(content).hexdigest(), "sizeBytes": len(content)} for name, content in sorted(files.items())],
                "permittedEvidenceRefs": list(refs)}
            sanitize_content(payload, reject_secrets=True)
            raw = _json(payload)
            if len(raw.encode("utf-8")) > 8 * 1024 * 1024:
                raise ValueError
            return SecurityAssessmentRequest(request_id=request_id, request_sha256=sha256(raw.encode("utf-8")).hexdigest(),
                approval_sha256=self._approval.approval_sha256, run_id=frozen.run_id, workspace_id=frozen.workspace_id,
                workflow_step_id=metadata.workflow_step_id, source_artifact_id=source.artifact_id,
                snapshot_sha256=source.snapshot_sha256, requirement_ids=metadata.requirement_ids,
                finding_ids=measured.finding_ids, permitted_evidence_refs=refs,
                _payload_json=raw, _source_files=MappingProxyType(dict(files)))
        except SecurityVerifierError:
            raise
        except Exception:
            raise SecurityVerifierError() from None

    async def __call__(self, execution, decision, measured):
        execution.budget.check()
        request = await _host_call(lambda _argument: self._request(execution, decision, measured, uuid4()), None)
        execution.budget.check()
        assessor = self._approval.assess
        try:
            deadline = asyncio.timeout(execution.budget.remaining_seconds())
            async with deadline:
                response = await _host_call(assessor, request)
            execution.budget.check()
            if response is None:
                return None
            if type(response) is not SecurityAssessment:
                raise SecurityVerifierError()
            result = replace(response)
            if (result != response or result.request_id != request.request_id or result.request_sha256 != request.request_sha256
                    or result.approval_sha256 != request.approval_sha256
                    or not set(dict(result.requirement_outcomes)) <= set(request.requirement_ids)
                    or not set(dict(result.finding_dispositions)) <= set(request.finding_ids)
                    or not set(result.evidence_refs) <= set(request.permitted_evidence_refs)):
                raise SecurityVerifierError()
            # Recheck approval and all measured inputs after trusted analysis.
            current = await _host_call(lambda _argument: self._request(execution, decision, measured, request.request_id), None)
            if current.request_sha256 != request.request_sha256:
                raise SecurityVerifierError()
            receipt = SecurityProofReceipt(receipt_id=uuid4(), request_id=request.request_id,
                run_id=request.run_id, workspace_id=request.workspace_id, workflow_step_id=request.workflow_step_id,
                source_artifact_id=request.source_artifact_id, code_version=execution.source.code_version,
                attempt=execution.metadata.attempt, snapshot_sha256=request.snapshot_sha256,
                approval_sha256=request.approval_sha256, request_sha256=request.request_sha256,
                assessment_sha256=_assessment_hash(result.request_id, result.request_sha256,
                    result.approval_sha256, result.requirement_outcomes, result.finding_dispositions,
                    result.evidence_refs, sha256(result.evidence_json.encode("utf-8")).hexdigest()),
                requirement_outcomes=result.requirement_outcomes, finding_dispositions=result.finding_dispositions,
                evidence_refs=result.evidence_refs, evidence_sha256=sha256(result.evidence_json.encode("utf-8")).hexdigest())
            execution.budget.check()
            await _host_call(self._receipts.publish, receipt)
            saved = await _host_call(lambda _argument: self._receipts.get(request.run_id, receipt.receipt_id), None)
            if saved != receipt:
                raise SecurityVerifierError()
            execution.budget.check()
            return SecuritySemanticProof(run_id=request.run_id, workflow_step_id=request.workflow_step_id,
                source_artifact_id=request.source_artifact_id, snapshot_sha256=request.snapshot_sha256,
                requirement_outcomes=result.requirement_outcomes, finding_dispositions=result.finding_dispositions,
                verification_receipt_id=receipt.receipt_id, approval_sha256=request.approval_sha256,
                request_sha256=request.request_sha256)
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError:
            raise
        except TimeoutError:
            if deadline.expired():
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise SecurityVerifierError() from None
        except Exception:
            raise SecurityVerifierError() from None

    def verify_receipt(self, proof, execution, decision, measured):
        try:
            if type(proof) is not SecuritySemanticProof or proof.verification_receipt_id is None:
                raise ValueError
            proof.__post_init__()
            saved = self._receipts.get(execution.metadata.run_id, proof.verification_receipt_id)
            request = self._request(execution, decision, measured, saved.request_id)
            if (saved.run_id != proof.run_id or saved.workflow_step_id != proof.workflow_step_id
                    or saved.source_artifact_id != proof.source_artifact_id or saved.snapshot_sha256 != proof.snapshot_sha256
                    or saved.requirement_outcomes != proof.requirement_outcomes
                    or saved.finding_dispositions != proof.finding_dispositions
                    or saved.request_sha256 != proof.request_sha256 or saved.approval_sha256 != proof.approval_sha256
                    or saved.request_sha256 != request.request_sha256 or saved.approval_sha256 != request.approval_sha256
                    or not set(saved.evidence_refs) <= set(request.permitted_evidence_refs)
                    or saved.assessment_sha256 != _assessment_hash(saved.request_id, saved.request_sha256,
                        saved.approval_sha256, saved.requirement_outcomes, saved.finding_dispositions,
                        saved.evidence_refs, saved.evidence_sha256)):
                raise ValueError
        except Exception:
            raise SecurityVerifierError() from None
