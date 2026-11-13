"""Host-derived role scopes and generated case expectations, never verdicts.

Only the frozen configuration grants authority. Planner task descriptions and
model-authored expected results cannot introduce another acceptance baseline.
This module performs no I/O, changes no approval, and infers no prose meaning.
"""

from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from agents.llm.contracts import json_text, parse_json
from agents.roles.qa_contract import QACaseBinding
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.requirement_authority import (
    ApprovedRequirement, RequirementAuthority, freeze_requirement_authority,
)
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.states import AgentRole


class RequirementScopeError(ValueError):
    """Stable reason only; private criteria and nested failures are discarded."""

    code = "REQUIREMENT_SCOPE_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class RequirementScope:
    role: AgentRole
    authority: RequirementAuthority = field(repr=False)
    planning_artifact_id: UUID
    planning_payload_sha256: str
    requirements: tuple[ApprovedRequirement, ...] = field(repr=False)
    task_bindings: tuple[tuple[str, tuple[UUID, ...]], ...] = field(repr=False)

    def to_prompt_json(self):
        return {
            "schemaVersion": 1, "role": self.role.value,
            "approvalSource": "HOST_FROZEN_SCENARIO_CONTRACT",
            "planningArtifactId": str(self.planning_artifact_id),
            "planningPayloadSha256": self.planning_payload_sha256,
            "assignedRequirementIds": [str(item.requirement_id) for item in self.requirements],
            "requirements": [{"requirementId": str(item.requirement_id), "key": item.key,
                "description": item.description, "acceptanceCriteria": list(item.acceptance_criteria)}
                for item in self.requirements],
            "taskBindings": [{"taskId": key, "requirementIds": [str(value) for value in values]}
                             for key, values in self.task_bindings],
            "plannerTaskTextCanChangeBaseline": False,
            "priorRoleOutcomeIsEvidence": False,
            "generatedExpectedResultSource": "EXACT_ASSIGNED_ACCEPTANCE_CRITERION",
            "coverageIsNotSuccess": True,
        }


def _assigned_requirements(configuration, metadata, role):
    if (type(role) is not AgentRole or role not in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY)
            or type(metadata) is not A2AWorkflowMetadata):
        raise ValueError
    authority = freeze_requirement_authority(configuration)
    if metadata.run_id != authority.run_id or metadata.scenario_id != authority.scenario_id:
        raise ValueError
    validator = {AgentRole.QA: RequirementValidator.QA,
                 AgentRole.SECURITY: RequirementValidator.SECURITY}.get(role)
    assigned = tuple(item for item in authority.requirements
                     if validator is None or validator in item.validators)
    if not assigned or metadata.requirement_ids != tuple(item.requirement_id for item in assigned):
        raise ValueError
    return authority, assigned


def build_requirement_scope(configuration, artifact, metadata, role) -> RequirementScope:
    """Reconcile the exact Planner copy and assigned IDs against the Host.

    Called after existing Task/history admission. Validation services also
    rederive the frozen role assignment before accepting case expectations.
    No current registry or APPROVED dictionary grants authority here; hashes
    identify data, not approval.
    """
    invalid = False
    try:
        authority, assigned = _assigned_requirements(configuration, metadata, role)
        if type(artifact) is not RequirementArtifact:
            raise ValueError
        if (metadata.run_id != authority.run_id or metadata.scenario_id != authority.scenario_id
                or artifact.run_id != authority.run_id or artifact.workflow_step_id == metadata.workflow_step_id
                or RequirementArtifact.model_validate(parse_json(artifact.model_dump_json())) != artifact):
            raise ValueError
        all_ids = tuple(item.requirement_id for item in authority.requirements)
        plan = PlannerPlan.model_validate(artifact.payload)
        canonical = plan.model_dump(mode="json", by_alias=True)
        if (canonical != artifact.payload or artifact.requirement_ids != all_ids
                or tuple(item.requirement_id for item in plan.requirements) != all_ids):
            raise ValueError
        for row, approved in zip(plan.requirements, authority.requirements):
            if (row.key != approved.key or row.description != approved.description
                    or tuple(row.acceptance_criteria) != approved.acceptance_criteria):
                raise ValueError
        # Validation roles get approved criteria only; Planner task prose is
        # neither a test oracle nor needed to infer their assigned scope.
        tasks = tuple((item.task_id, tuple(item.requirement_ids)) for item in plan.implementation_plan
                      ) if role is AgentRole.DEVELOPER else ()
        result = RequirementScope(role=role, authority=authority,
            planning_artifact_id=artifact.artifact_id,
            planning_payload_sha256=sha256(json_text(canonical).encode("utf-8")).hexdigest(),
            requirements=assigned, task_bindings=tasks)
    except Exception:
        invalid = True
    if invalid:
        raise RequirementScopeError() from None
    return result


def validate_generated_case_expectations(cases, *, configuration, metadata, role):
    """Reject invented/weakened expectations before executing generated tests.

    A test may bind one exact criterion from its assigned Requirement. This
    does not prove the test implements that criterion or covers every clause.
    Protected tests are separately admitted Host policy, not model decisions.
    """
    invalid = False
    try:
        if (type(role) is not AgentRole or role not in (AgentRole.QA, AgentRole.SECURITY)
                or type(cases) is not tuple or not 1 <= len(cases) <= 256):
            raise ValueError
        _authority, assigned = _assigned_requirements(configuration, metadata, role)
        criteria = {item.requirement_id: item.acceptance_criteria for item in assigned}
        for case in cases:
            if (type(case) is not QACaseBinding or case.requirement_id not in criteria
                    or case.expected_result not in criteria[case.requirement_id]):
                raise ValueError
        if {case.requirement_id for case in cases} != set(criteria):
            raise ValueError
    except Exception:
        invalid = True
    if invalid:
        raise RequirementScopeError() from None
