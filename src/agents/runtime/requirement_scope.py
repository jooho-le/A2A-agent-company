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


def signup_observation_plan(contract, scope=None):
    """Break compound public SCN-001 criteria into evidence the assigned roles can observe."""
    if contract.get('scenarioKey') != 'SCN-001':
        return {}
    criteria = {
        'REQ-003': {'QA': ['concurrent signup outcomes and committed canonical row count'],
                    'SECURITY': ['deployed unique key on the canonical email']},
        'REQ-005': {'SECURITY': ['stored credential verifies against the supplied synthetic password',
                                 'observed algorithm, memory, iterations, parallelism and per-account salts meet the approved policy']},
        'REQ-006': {'SECURITY': ['success response', 'input rejection response', 'storage error response',
                                 'request logs', 'storage error request logs']},
    }
    assigned = {str(r.requirement_id) for r in scope.requirements} if scope else None
    plan = {}
    for requirement in contract['requirements']:
        key, identifier = requirement['key'], requirement['requirementId']
        if key not in criteria or (assigned is not None and identifier not in assigned):
            continue
        tasks = [task for task, ids in scope.task_bindings if any(str(r) == identifier for r in ids)] if scope else []
        observations = criteria[key]
        if scope and scope.role.value in ('QA', 'SECURITY'):
            observations = {scope.role.value: observations[scope.role.value]} if scope.role.value in observations else {}
        if observations:
            plan[identifier] = {'observationsByRole': observations}
            if tasks:
                plan[identifier]['implementationTaskIds'] = tasks
    return plan


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

    Each approved criterion must be linked to a generated case from its assigned role.
    Runtime evidence determines whether that case observed the criterion's clauses.
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
        linked = {(case.requirement_id, case.expected_result) for case in cases}
        if any((identifier, criterion) not in linked for identifier, values in criteria.items() for criterion in values):
            raise ValueError
    except Exception:
        invalid = True
    if invalid:
        raise RequirementScopeError() from None


def assigned_acceptance_criteria(configuration, metadata, role) -> tuple[str, ...]:
    """The criteria a generated case of this role may quote; () when the Host cannot tell."""
    try:
        _authority, assigned = _assigned_requirements(configuration, metadata, role)
    except Exception:
        return ()
    return tuple(criterion for item in assigned for criterion in item.acceptance_criteria)


def generated_case_expectation_issues(data, *, field, configuration, metadata, role) -> list[dict]:
    """Where a draft breaks ``validate_generated_case_expectations``, as closed field pointers.

    The same rule, read from the model's JSON before the engine returns, so the
    model gets one correction that names the case (docs/live-api problem P5, live Run 3754980f:
    an expectedResult that differed from its criterion ended the step with no
    chance to fix it). Values are never echoed. ``[]`` means nothing to report or
    the Host cannot judge here; the caller still runs the authoritative check.
    """
    try:
        _authority, assigned = _assigned_requirements(configuration, metadata, role)
    except Exception:
        return []
    rows = data.get(field) if isinstance(data, dict) and isinstance(field, str) else None
    if not isinstance(rows, list):
        return []
    criteria = {str(item.requirement_id): tuple(item.acceptance_criteria) for item in assigned}
    issues = []
    if not 1 <= len(rows) <= 256:
        issues.append({"fieldPath": "/" + field, "reasonCode": "CASE_COUNT_INVALID"})
    covered, linked, invalid_expectations = set(), set(), set()
    for index, row in enumerate(rows[:256]):
        if not isinstance(row, dict):
            continue
        requirement = row.get("requirementId")
        if not isinstance(requirement, str) or requirement not in criteria:
            issues.append({"fieldPath": f"/{field}/{index}/requirementId", "reasonCode": "CASE_REQUIREMENT_NOT_ASSIGNED"})
            continue
        covered.add(requirement)
        if row.get("expectedResult") not in criteria[requirement]:
            invalid_expectations.add(requirement)
            issues.append({"fieldPath": f"/{field}/{index}/expectedResult",
                           "reasonCode": "EXPECTED_RESULT_NOT_ACCEPTANCE_CRITERION"})
        else:
            linked.add((requirement, row['expectedResult']))
    if any((identifier, criterion) not in linked for identifier in covered - invalid_expectations for criterion in criteria[identifier]):
        issues.append({"fieldPath": "/" + field, "reasonCode": "ACCEPTANCE_CRITERION_WITHOUT_CASE"})
    if covered != set(criteria):
        issues.append({"fieldPath": "/" + field, "reasonCode": "ASSIGNED_REQUIREMENT_WITHOUT_CASE"})
    return issues
