"""Bounded model explanations and independently labelled Host routing facts.

An explanation is a model claim, never evidence of a product defect or a policy
decision. Only two mechanically contradicted capability claims and an explicit
in-scope implementation stop may receive one shared-budget correction.
"""
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from orchestrator.core.security import redact_data


POLICY = 'EXPLAINED_DEVELOPMENT_REJECTION_BOUND_FAILURE_V2'
REASON_CODES = ('OUT_OF_SCOPE', 'BUILD_TOOL_UNAVAILABLE', 'REQUIREMENT_NOT_ASSIGNED',
                'IMPLEMENTATION_BLOCKED', 'NO_CHANGE_REQUIRED', 'REQUIREMENTS_CONFLICT', 'POLICY_BLOCKED', 'UNKNOWN')


class RejectionReason(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True, hide_input_in_errors=True)
    reason_code: Literal['OUT_OF_SCOPE', 'BUILD_TOOL_UNAVAILABLE', 'REQUIREMENT_NOT_ASSIGNED',
                         'IMPLEMENTATION_BLOCKED', 'NO_CHANGE_REQUIRED', 'REQUIREMENTS_CONFLICT', 'POLICY_BLOCKED', 'UNKNOWN'] = Field(alias='reasonCode')
    explanation: str = Field(min_length=1, max_length=1024, strict=True, repr=False)
    requirement_ids: tuple[str, ...] = Field(alias='requirementIds', max_length=16)

    @field_validator('explanation')
    @classmethod
    def safe_text(cls, value):
        if (not value.strip() or len(value.encode('utf-8')) > 4096 or redact_data(value) != value
                or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)):
            raise ValueError('invalid rejection explanation')
        return value

    @field_validator('requirement_ids')
    @classmethod
    def canonical_ids(cls, values):
        if len(set(values)) != len(values) or any(str(UUID(v)) != v for v in values):
            raise ValueError('invalid rejection requirement ids')
        return values

    def public(self):
        return self.model_dump(mode='json', by_alias=True)


class BoundFailure(BaseModel):
    """Host-selected evidence only; never accepted in the model output schema."""
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True)
    source_artifact_id: UUID = Field(alias='sourceArtifactId')
    snapshot_sha256: str = Field(alias='snapshotSha256', pattern=r'^[0-9a-f]{64}$')
    issue_ids: tuple[UUID, ...] = Field(alias='issueIds', min_length=1)
    requirement_ids: tuple[UUID, ...] = Field(alias='requirementIds', min_length=1)


class RejectionAssessment(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, populate_by_name=True, hide_input_in_errors=True)
    proposal: RejectionReason
    classification: Literal['HOST_CONTRADICTED', 'MODEL_DECLARED_TECHNICAL_STOP', 'HUMAN_REVIEW']
    basis: Literal['HOST_RUNS_BUILD', 'REQUIREMENTS_ARE_ASSIGNED', 'IN_SCOPE_MODEL_STOP',
                   'UNASSIGNED_REQUIREMENT_REFERENCE', 'POLICY_OR_REQUIREMENTS_DECISION', 'UNCONFIRMED_CLAIM',
                   'UNCHANGED_MEASURED_FAILURE']
    source_changed: bool = Field(alias='sourceChanged', strict=True)
    failure_evidence: BoundFailure | None = Field(default=None, alias='failureEvidence')

    def public(self):
        return self.model_dump(mode='json', by_alias=True)


def rejection_schema():
    # Explicit provider-strict shape; local branch validation requires non-null
    # only for REJECTED. The three other fields retain their original meaning.
    return {'anyOf': [{'type': 'null'}, {'type': 'object', 'additionalProperties': False,
        'required': ['reasonCode', 'explanation', 'requirementIds'], 'properties': {
            'reasonCode': {'type': 'string', 'enum': list(REASON_CODES)},
            'explanation': {'type': 'string', 'minLength': 1, 'maxLength': 1024},
            'requirementIds': {'type': 'array', 'maxItems': 16, 'items': {'type': 'string'}}}}]}


def assess_rejection(reason, *, assigned_ids, source_changed, failure_evidence=None):
    reason = RejectionReason.model_validate(reason)
    assigned = {str(i) for i in assigned_ids}
    referenced = set(reason.requirement_ids)
    classification, basis = 'HUMAN_REVIEW', 'UNCONFIRMED_CLAIM'
    if not referenced.issubset(assigned):
        basis = 'UNASSIGNED_REQUIREMENT_REFERENCE'
    elif reason.reason_code in ('POLICY_BLOCKED', 'REQUIREMENTS_CONFLICT'):
        basis = 'POLICY_OR_REQUIREMENTS_DECISION'
    elif reason.reason_code == 'BUILD_TOOL_UNAVAILABLE':
        classification, basis = 'HOST_CONTRADICTED', 'HOST_RUNS_BUILD'
    elif referenced and reason.reason_code == 'REQUIREMENT_NOT_ASSIGNED':
        classification, basis = 'HOST_CONTRADICTED', 'REQUIREMENTS_ARE_ASSIGNED'
    elif referenced and reason.reason_code == 'IMPLEMENTATION_BLOCKED':
        # This confirms a model-declared inability on an assigned task, not its
        # claimed root cause. Safe engine completion proves Tools have settled.
        classification, basis = 'MODEL_DECLARED_TECHNICAL_STOP', 'IN_SCOPE_MODEL_STOP'
    elif (reason.reason_code == 'NO_CHANGE_REQUIRED' and not source_changed and referenced
          and failure_evidence is not None
          and referenced.intersection(str(i) for i in failure_evidence.requirement_ids)):
        classification, basis = 'HOST_CONTRADICTED', 'UNCHANGED_MEASURED_FAILURE'
    return RejectionAssessment(proposal=reason, classification=classification, basis=basis,
                               sourceChanged=source_changed,
                               failureEvidence=failure_evidence if basis == 'UNCHANGED_MEASURED_FAILURE' else None)


def rejection_check(assigned_ids, *, failure_evidence=None, validate=None):
    assigned = tuple(assigned_ids)

    def check(data, source_changed):
        from agents.roles.developer_contract import validate_developer_decision
        if not isinstance(data, dict) or data.get("kind") != "REJECTED":
            return None
        try:
            decision = (validate or validate_developer_decision)(data)
        except ValueError:
            return None  # Ordinary schema/contract correction owns malformed replies.
        if decision.kind != 'REJECTED':
            return None
        evidence = failure_evidence() if (failure_evidence is not None and not source_changed
                                          and decision.rejection.reason_code == 'NO_CHANGE_REQUIRED') else None
        return assess_rejection(decision.rejection.public(), assigned_ids=assigned, source_changed=source_changed,
                                failure_evidence=evidence)
    return check
