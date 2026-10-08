"""Opt-in, read-only Host inputs for Planner execution; no fresh budgets.

The Scenario Registry is deliberately not consulted here: a Run's saved
baseline remains authoritative after registry edits. Loader reads are one
SQLite snapshot. A first dispatch may precede the Orchestrator's Task-ID
observer, but an already bound opaque Task/Context ID cannot be replaced.
"""

from dataclasses import dataclass, field
import math
from uuid import RFC_4122, UUID, uuid4

from a2a.server.agent_execution import RequestContext

from agents.api.validation import parse_workflow_metadata
from agents.llm.budget import ExecutionBudget
from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, parse_json
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.application.planner_output import PlanRequirement
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import ScenarioDefinition, RequirementValidator
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


_MAX_BYTES = 1_048_576
_SCENARIO_FIELDS = frozenset({
    "scenarioId", "scenarioKey", "name", "requirements", "excludedFeatures",
    "securityPolicy", "emailPolicy",
})
_REQUIREMENT_FIELDS = frozenset({
    "requirementId", "key", "category", "description", "acceptanceCriteria", "validators",
})


class PlannerContextError(ValueError):
    """Stable failure only; never retain config, request, path or SQL prose."""

    code = "PLANNER_CONTEXT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _uuid(value):
    if type(value) is not UUID or value.version != 4 or value.variant != RFC_4122:
        raise PlannerContextError()
    return value


def _text(value, *, max_bytes=4096):
    if (type(value) is not str or not value.strip()
            or len(value.encode("utf-8")) > max_bytes
            or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in value)):
        raise PlannerContextError()
    # Validate, never strip or rewrite the frozen baseline.
    return value


def _json(value):
    data = parse_json(value, max_bytes=_MAX_BYTES)
    stack = [(data, 0)]
    visited = 0
    while stack:
        item, depth = stack.pop()
        visited += 1
        if depth > 64 or visited > 65536:
            raise PlannerContextError()
        if type(item) is dict:
            stack.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            stack.extend((child, depth + 1) for child in item)
    return data


def _configuration(data):
    """Read existing frozen bytes before the common model can fill defaults."""
    if type(data) is not dict:
        raise PlannerContextError()
    names = [name for name in ("frozen_scenario_contract_json", "frozenScenarioContractJson") if name in data]
    if len(names) != 1:
        raise PlannerContextError()
    frozen = data[names[0]]
    if type(frozen) is not str or not frozen.strip():
        raise PlannerContextError()
    # Parse bounded, duplicate-free JSON without consulting a mutable Registry.
    _json(frozen)
    return RunConfigurationArtifact.model_validate(data)


def _scenario(configuration):
    """Validate semantic fields without inventing policies or canonical IDs."""
    try:
        data = _json(configuration.frozen_scenario_contract_json)
        if type(data) is not dict or not _SCENARIO_FIELDS <= data.keys():
            raise ValueError
        if data["scenarioId"] != str(configuration.scenario_id):
            raise ValueError
        _text(data["scenarioKey"])
        _text(data["name"])
        requirements = data["requirements"]
        if type(requirements) is not list or not 1 <= len(requirements) <= 128:
            raise ValueError
        ids, keys = set(), set()
        for item in requirements:
            if type(item) is not dict or not _REQUIREMENT_FIELDS <= item.keys():
                raise ValueError
            identity = UUID(item["requirementId"])
            _uuid(identity)
            if str(identity) != item["requirementId"] or identity in ids:
                raise ValueError
            ids.add(identity)
            key = _text(item["key"])
            if key in keys:
                raise ValueError
            keys.add(key)
            _text(item["category"])
            _text(item["description"])
            criteria = item["acceptanceCriteria"]
            validators = item["validators"]
            if type(criteria) is not list or not 1 <= len(criteria) <= 1000:
                raise ValueError
            for criterion in criteria:
                _text(criterion)
            protected = {key: item[key] for key in (
                "requirementId", "key", "description", "acceptanceCriteria",
            )}
            requirement = PlanRequirement.model_validate(protected)
            if requirement.model_dump(mode="json", by_alias=True) != protected:
                # Existing Plan validators strip prose. Reject such a frozen
                # baseline instead of silently changing acceptance criteria.
                raise ValueError
            if (type(validators) is not list or not validators
                    or any(type(value) is not str for value in validators)
                    or len(validators) != len(set(validators))):
                raise ValueError
            for validator in validators:
                RequirementValidator(validator)
        excluded = data["excludedFeatures"]
        if type(excluded) is not list or len(excluded) > 1000:
            raise ValueError
        for feature in excluded:
            _text(feature)
        if type(data["securityPolicy"]) is not dict or type(data["emailPolicy"]) is not dict:
            raise ValueError
        # Known credential literals are refused, not normalized into a weaker
        # baseline. Optional future fields are bounded and checked as well.
        sanitize_content(data, reject_secrets=True)
        return ScenarioDefinition.from_contract(data)
    except Exception:
        raise PlannerContextError() from None


@dataclass(frozen=True, kw_only=True)
class PlannerExecutionContext:
    metadata: A2AWorkflowMetadata
    configuration: RunConfigurationArtifact = field(repr=False)
    budget: ExecutionBudget = field(repr=False)
    request_text: str = field(repr=False)
    project_artifact_id: UUID = field(default_factory=uuid4)
    artifact_version: int = 1

    def __post_init__(self):
        try:
            if (type(self.metadata) is not A2AWorkflowMetadata
                    or type(self.configuration) is not RunConfigurationArtifact
                    or not isinstance(self.budget, ExecutionBudget)):
                raise ValueError
            metadata = A2AWorkflowMetadata.model_validate(_json(self.metadata.model_dump_json(warnings=False)))
            configuration = _configuration(_json(self.configuration.model_dump_json(warnings=False)))
            if (metadata != self.metadata or configuration != self.configuration
                    or metadata.run_id != configuration.run_id
                    or metadata.scenario_id != configuration.scenario_id
                    or metadata.code_version is not None or metadata.project_artifact_ids):
                raise ValueError
            _uuid(self.project_artifact_id)
            if type(self.artifact_version) is not int or self.artifact_version != 1:
                raise ValueError
            _text(self.request_text, max_bytes=_MAX_BYTES)
            frozen = configuration.configuration
            if frozen.model is None or frozen.limits.runtime_budget_ms is None:
                raise ValueError
            # Checking the caller-owned budget does not reserve usage or reset
            # its deadline. Already consumed model/tool usage remains intact.
            self.budget.check()
            remaining = self.budget.remaining_seconds()
            if (not math.isfinite(remaining)
                    or remaining > frozen.limits.runtime_budget_ms / 1000 + 0.001):
                raise ValueError
            sanitize_content(configuration.model_dump(mode="json"), reject_secrets=True)
            scenario = _scenario(configuration)
            if metadata.requirement_ids and metadata.requirement_ids != scenario.requirement_ids:
                raise ValueError
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise PlannerContextError() from None
        except Exception:
            raise PlannerContextError() from None

    @property
    def scenario(self):
        return _scenario(self.configuration)

    @property
    def model(self):
        return self.configuration.configuration.model


class SQLitePlannerContextLoader:
    """Inert Host capability; existing shared budgets are resolved, not built.

    A continuation must first be admitted by the Orchestrator: its observer
    persists RUNNING and the incremented Step attempt before the A2A send.
    WAITING_INPUT is not a license to start a fresh execution independently.
    """

    def __init__(self, repository, budget_resolver):
        if not isinstance(repository, SQLiteWorkflowRepository) or not callable(budget_resolver):
            raise PlannerContextError()
        self._repository, self._budget_resolver = repository, budget_resolver

    def __repr__(self):
        return "SQLitePlannerContextLoader()"

    def __call__(self, context):
        try:
            if not isinstance(context, RequestContext):
                raise ValueError
            metadata = parse_workflow_metadata(context.metadata)
            _text(context.task_id)
            _text(context.context_id)
            with self._repository._connection() as connection:
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
                run_row = connection.execute(
                    "SELECT status,payload_json FROM workflow_runs WHERE run_id=?", (str(metadata.run_id),),
                ).fetchone()
                step_row = connection.execute(
                    "SELECT status,payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?",
                    (str(metadata.run_id), str(metadata.workflow_step_id)),
                ).fetchone()
                config_row = connection.execute(
                    "SELECT payload_json FROM run_configurations WHERE run_id=?", (str(metadata.run_id),),
                ).fetchone()
                if run_row is None or step_row is None or config_row is None:
                    raise ValueError
                run = WorkflowRun.model_validate(_json(run_row["payload_json"]))
                step = WorkflowStep.model_validate(_json(step_row["payload_json"]))
                configuration = _configuration(_json(config_row["payload_json"]))
                workspace_row = connection.execute(
                    "SELECT workspace_id,run_id,payload_json FROM workspaces WHERE workspace_id=?",
                    (str(run.workspace_id),),
                ).fetchone()
                if workspace_row is None:
                    raise ValueError
                workspace = WorkspaceRecord.model_validate(_json(workspace_row["payload_json"]))
            if (run.run_id != metadata.run_id or run.scenario_id != metadata.scenario_id
                    or run.status is not WorkflowStatus.PLANNING or run_row["status"] != run.status.value
                    or run.fix_attempt != 0 or run.code_version is not None
                    or step.run_id != run.run_id or step.workflow_step_id != metadata.workflow_step_id
                    or step.agent_role is not AgentRole.PLANNER
                    or step.status is not WorkflowStepStatus.RUNNING or step_row["status"] != step.status.value
                    or step.attempt != metadata.attempt or step.code_version is not None
                    or step.input_artifact_ids or step.output_artifact_ids or step.a2a_artifact_ids
                    or tuple(step.requirement_ids) != (metadata.requirement_ids or ())
                    or configuration.run_id != run.run_id or configuration.scenario_id != run.scenario_id
                    or configuration.workspace_id != run.workspace_id
                    or workspace.run_id != run.run_id or workspace.workspace_id != run.workspace_id
                    or workspace_row["workspace_id"] != str(workspace.workspace_id)
                    or workspace_row["run_id"] != str(workspace.run_id)
                    or step.a2a_task_id is not None and step.a2a_task_id != context.task_id
                    or step.agent_context_id is not None and step.agent_context_id != context.context_id):
                raise ValueError
            # The resolver is a trusted Host capability. It must return the
            # existing shared Run budget, never derive one from model input.
            budget = self._budget_resolver(configuration)
            return PlannerExecutionContext(
                metadata=metadata, configuration=configuration, budget=budget,
                request_text=run.request_text,
            )
        except LLMRuntimeError as error:
            if error.code is LLMErrorCode.BUDGET:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            raise PlannerContextError() from None
        except Exception:
            raise PlannerContextError() from None
