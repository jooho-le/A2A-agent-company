"""One budget per Run, with opt-in owned-Host durable accounting.

The legacy default cannot recover after restart. Durable recovery validates
the original UTC deadline, immutable inputs and all recorded model receipts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import RLock
from uuid import RFC_4122, UUID

from agents.llm.budget import BudgetSnapshot, ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.telemetry_store import AgentTelemetryStore, TelemetryStoreError
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
    counters, deadline and unknown-token state. Only an explicitly supplied
    telemetry ledger permits checked recovery; this is not distributed leasing.
    """

    def __init__(self, repository, *, limits, telemetry_store=None):
        if not isinstance(repository, SQLiteWorkflowRepository) or type(limits) is not LLMLimits:
            raise RunBudgetError()
        self._repository = repository
        if telemetry_store is not None and (type(telemetry_store) is not AgentTelemetryStore
                or telemetry_store.database_path.resolve() != repository.database_path.resolve()):
            raise RunBudgetError()
        self._telemetry_store = telemetry_store
        self._limits = LLMLimits.model_validate(limits.model_dump())
        self._started_at = telemetry_store.now() if telemetry_store is not None else datetime.now(timezone.utc)
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
            if self._telemetry_store is not None:
                restored = self._restore(configuration)
                if restored is not None:
                    return restored
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
            now = self._telemetry_store.now() if self._telemetry_store is not None else datetime.now(timezone.utc)
            elapsed = (now - run.created_at).total_seconds()
            if elapsed < 0:
                raise RunBudgetError()
            remaining_ms = int(configuration.configuration.limits.runtime_budget_ms - elapsed * 1000)
            if remaining_ms <= 0:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            if self._telemetry_store is None:
                budget = ExecutionBudget(runtime_budget_ms=remaining_ms, limits=self._limits)
            else:
                try:
                    snapshot = self._telemetry_store.admit_budget(BudgetSnapshot(
                        run_id=run.run_id, configuration_fingerprint=fingerprint, limits=self._limits,
                        created_at_utc=run.created_at,
                        deadline_utc=run.created_at + timedelta(milliseconds=configuration.configuration.limits.runtime_budget_ms),
                        last_observed_utc=self._telemetry_store.now()))
                    budget = self._budget(snapshot)
                except (TelemetryStoreError, ValueError):
                    raise RunBudgetError() from None
            self._admissions[run_id] = _Admission(fingerprint, budget)
            return budget

    def resolve(self, configuration):
        with self._guard:
            fingerprint = _fingerprint(configuration)
            existing = self._admissions.get(configuration.run_id)
            if existing is None and self._telemetry_store is not None:
                restored = self._restore(configuration)
                if restored is not None:
                    return restored
            if existing is None or existing.fingerprint != fingerprint:
                raise RunBudgetError()
            existing.budget.check()
            return existing.budget

    def _budget(self, snapshot):
        return ExecutionBudget(runtime_budget_ms=1, limits=self._limits, snapshot=snapshot,
            persistence=self._telemetry_store.save_budget, utc_clock=self._telemetry_store.now)

    def _restore(self, configuration):
        try:
            snapshot = self._telemetry_store.load_budget(configuration.run_id)
            if snapshot is None:
                return None
            run = self._repository.get_run(configuration.run_id)
            runtime = configuration.configuration.limits.runtime_budget_ms
            if (run is None or run.status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED)
                    or runtime is None or configuration.configuration.model is None
                    or snapshot.configuration_fingerprint != _fingerprint(configuration)
                    or snapshot.limits != self._limits
                    or snapshot.created_at_utc != run.created_at
                    or snapshot.deadline_utc != run.created_at + timedelta(milliseconds=runtime)
                    or snapshot.pending_model_sequences or snapshot.pending_tool_sequences
                    or not snapshot.usage_complete or snapshot.invalidated):
                raise RunBudgetError()
            self._telemetry_store.audit_budget(snapshot)
            # Persist the observation so a later restart cannot move UTC backward.
            snapshot = self._telemetry_store.save_budget(snapshot, snapshot.revision)
            budget = self._budget(snapshot)
            budget.check()
            self._admissions[configuration.run_id] = _Admission(_fingerprint(configuration), budget)
            return budget
        except TelemetryStoreError:
            raise RunBudgetError() from None
