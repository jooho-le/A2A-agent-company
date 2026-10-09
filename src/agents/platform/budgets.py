"""One budget per newly admitted Run in this Host process, never per role.

Restart recovery intentionally fails closed. Reading frozen limits cannot
reconstruct already spent usage or justify a new deadline.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from threading import RLock
from uuid import RFC_4122, UUID

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository


class RunBudgetError(ValueError):
    code = "RUN_BUDGET_NOT_ADMITTED"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True)
class _Admission:
    fingerprint: str
    budget: ExecutionBudget


def _fingerprint(configuration):
    if type(configuration) is not RunConfigurationArtifact:
        raise RunBudgetError()
    return sha256(configuration.model_dump_json(by_alias=True).encode("utf-8")).hexdigest()


class RunBudgetRegistry:
    """Orchestrator admits; Context loaders only resolve the existing object.

    Run creation must follow this registry's startup and its first Planner
    dispatch must be claimed, without any previous A2A identity. Queue and
    workspace preparation count from Run.created_at. Continuation preserves
    counters, deadline and unknown-token state. This is not durable recovery.
    """

    def __init__(self, repository, *, limits):
        if not isinstance(repository, SQLiteWorkflowRepository) or type(limits) is not LLMLimits:
            raise RunBudgetError()
        self._repository = repository
        self._limits = LLMLimits.model_validate(limits.model_dump())
        self._started_at = datetime.now(timezone.utc)
        self._admissions = {}
        self._guard = RLock()

    def __repr__(self):
        return "RunBudgetRegistry()"

    def admit(self, run_id):
        if type(run_id) is not UUID or run_id.version != 4 or run_id.variant != RFC_4122:
            raise RunBudgetError()
        with self._guard:
            configuration = self._repository.get_run_configuration(run_id)
            fingerprint = _fingerprint(configuration)
            existing = self._admissions.get(run_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise RunBudgetError()
                existing.budget.check()
                return existing.budget
            run = self._repository.get_run(run_id)
            steps = self._repository.list_steps(run_id)
            if (run is None or run.status is not WorkflowStatus.PLANNING
                    or run.fix_attempt != 0 or run.code_version is not None
                    or run.created_at.tzinfo is None or run.created_at < self._started_at
                    or len(steps) != 1 or steps[0].agent_role is not AgentRole.PLANNER
                    or steps[0].status is not WorkflowStepStatus.RUNNING
                    or steps[0].attempt != 0 or steps[0].a2a_task_id is not None
                    or steps[0].agent_context_id is not None or steps[0].a2a_task_state is not None
                    or steps[0].input_artifact_ids or steps[0].output_artifact_ids
                    or steps[0].a2a_artifact_ids
                    or configuration.workspace_id != run.workspace_id
                    or configuration.scenario_id != run.scenario_id
                    or configuration.configuration.model is None
                    or configuration.configuration.limits.runtime_budget_ms is None):
                raise RunBudgetError()
            elapsed = (datetime.now(timezone.utc) - run.created_at).total_seconds()
            if elapsed < 0:
                raise RunBudgetError()
            remaining_ms = int(configuration.configuration.limits.runtime_budget_ms - elapsed * 1000)
            if remaining_ms <= 0:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            budget = ExecutionBudget(runtime_budget_ms=remaining_ms, limits=self._limits)
            self._admissions[run_id] = _Admission(fingerprint, budget)
            return budget

    def resolve(self, configuration):
        with self._guard:
            fingerprint = _fingerprint(configuration)
            existing = self._admissions.get(configuration.run_id)
            if existing is None or existing.fingerprint != fingerprint:
                raise RunBudgetError()
            existing.budget.check()
            return existing.budget
