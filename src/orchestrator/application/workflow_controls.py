"""Explicit recovery and cancellation without replaying uncertain writes."""

from collections.abc import Callable, Mapping
from uuid import UUID

from a2a.types import TaskState

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.a2a_tasks import A2ATaskRunner, A2ATaskRunResult, TaskRunDisposition
from orchestrator.application.dispatch import PlannerRunDispatcher, _DEVELOPER_OUTPUT_CONTRACT
from orchestrator.application.control_input import validate_control_input
from orchestrator.application.validation_request import build_validation_request
from orchestrator.core.security import redact_data
from orchestrator.domain import (
    A2ATaskState, AgentContext, AgentRole, CodeSnapshotArtifact, FinalVerdict,
    SnapshotHandoff, WorkflowRun, WorkflowStatus, WorkflowStep,
    WorkflowStepStatus, TraceEvent,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository


class WorkflowControlConflict(RuntimeError):
    """An operation needs reconciliation or a different explicit user action."""


_FINAL = {WorkflowStatus.FINISHED, WorkflowStatus.ABORTED}
_UNRESOLVED_TASKS = {
    A2ATaskState.SUBMITTED, A2ATaskState.WORKING,
    A2ATaskState.INPUT_REQUIRED, A2ATaskState.AUTH_REQUIRED,
}


class WorkflowControlService:
    def __init__(
        self, repository: SQLiteWorkflowRepository, registry: A2AAgentRegistry,
        dispatcher: PlannerRunDispatcher, *,
        client_factory: Callable[[str], A2AAgentClient] = A2AAgentClient,
        authenticated_roles: frozenset[AgentRole] = frozenset(),
        preflight: Callable[[UUID], None] | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.dispatcher = dispatcher
        self.client_factory = client_factory
        self.authenticated_roles = authenticated_roles
        if preflight is not None and not callable(preflight):
            raise ValueError("CONTROL_PREFLIGHT_INVALID")
        self.preflight = preflight

    async def resume(
        self, run_id: UUID, *, step_id: UUID | None = None,
        input_data: Mapping[str, object] | None = None, recover: bool = False,
    ) -> WorkflowRun:
        """Read known Tasks, or send only a demonstrably unsent Step once."""
        input_data = validate_control_input(input_data)
        if self.dispatcher.is_run_active(run_id):
            raise WorkflowControlConflict("Run is currently being dispatched; recovery would compete with it")
        return await self.dispatcher.control_operation(run_id, lambda: self._resume(
            run_id, step_id=step_id, input_data=input_data, recover=recover,
        ))

    async def _resume(self, run_id, *, step_id, input_data, recover):
        try:
            run = self.repository.get_run(run_id)
            if run is None:
                raise WorkflowControlConflict("Run does not exist")
            if run.status in _FINAL:
                raise WorkflowControlConflict("A finished or aborted Run cannot be resumed")
            if not recover and run.status not in (
                WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW,
                WorkflowStatus.FIX_REQUIRED,
            ):
                raise WorkflowControlConflict("resume requires WAITING_INPUT, HUMAN_REVIEW, or FIX_REQUIRED; use recover for interrupted execution")
            stage = run.resume_state or run.status
            if step_id is not None and input_data is None and stage == WorkflowStatus.FIX_REQUIRED:
                raise WorkflowControlConflict("FIX_REQUIRED has no created fix Step to select")
            steps = self._stage_steps(run, stage, step_id)
            if steps and all(step.output_artifact_ids for step in steps):
                raise WorkflowControlConflict("This stage already registered its evidence; Human Review cannot override it. Cancel or create a separately approved Run")
            validation_stage = stage in (
                WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING,
                WorkflowStatus.SNAPSHOT_READY,
            )
            # Recovery without an explicit target/input is observation, not
            # permission to continue every interrupted validation Task.
            observe_only = recover and step_id is None and input_data is None
            if stage == WorkflowStatus.FIX_REQUIRED and observe_only:
                return run
            if validation_stage and not observe_only and step_id is None and sum(
                step.status == WorkflowStepStatus.WAITING_INPUT
                and step.a2a_task_state in (A2ATaskState.INPUT_REQUIRED, A2ATaskState.AUTH_REQUIRED)
                for step in steps
            ) > 1:
                raise WorkflowControlConflict("Select workflowStepId instead of broadcasting input or authentication to multiple interrupted Tasks")
            continuation_steps = {
                step.workflow_step_id for step in steps
                if not observe_only and step_id in (None, step.workflow_step_id)
            }
            for step in steps:
                self._check_observable(
                    step, input_data if step.workflow_step_id in continuation_steps else None,
                    continue_interrupted=step.workflow_step_id in continuation_steps,
                )
                self.registry.require_base_url(step.agent_role)
            if input_data is not None:
                targets = [step for step in steps if step.workflow_step_id in continuation_steps]
                completed_observation = (
                    validation_stage and step_id is not None and len(targets) == 1
                    and targets[0].a2a_task_state == A2ATaskState.COMPLETED
                )
                if completed_observation:
                    # An explicit retry can arrive after this validator already
                    # completed, while its peer still needs input. It is a read,
                    # never permission to apply this answer to another Task or
                    # to begin a new fix. All input protection was checked first.
                    if any(step.a2a_task_id is None for step in steps):
                        raise WorkflowControlConflict("A completed Task retry cannot authorize unsent validation; use explicit recovery")
                    input_data = None
                    continuation_steps.clear()
                    observe_only = True
                elif len(targets) != 1 or targets[0].a2a_task_state != A2ATaskState.INPUT_REQUIRED:
                    raise WorkflowControlConflict("inputData requires one existing INPUT_REQUIRED Task; authentication is configured out of band")
            needs_execution = stage == WorkflowStatus.FIX_REQUIRED or any(
                step.a2a_task_id is None or (
                    step.workflow_step_id in continuation_steps
                    and step.status == WorkflowStepStatus.WAITING_INPUT
                    and step.a2a_task_state in (A2ATaskState.INPUT_REQUIRED, A2ATaskState.AUTH_REQUIRED)
                ) for step in steps
            )
            if needs_execution and stage != WorkflowStatus.RECEIVED:
                self._check_budget(run_id)
            if stage == WorkflowStatus.RECEIVED:
                # Planner's existing one-shot DB claim owns the initial send.
                await self.dispatcher._dispatch_planner(run_id)
                return self.repository.get_run(run_id)
            plan = None if stage == WorkflowStatus.PLANNING else self.repository.get_planner_plan(run_id)
            if stage != WorkflowStatus.PLANNING and plan is None:
                raise WorkflowControlConflict("The authoritative Planner Artifact is missing; reconcile it before resuming")
            scenario = self.dispatcher._scenario_for_run(run)
            if scenario is None:
                raise WorkflowControlConflict("Scenario is not registered")
            if stage == WorkflowStatus.FIX_REQUIRED:
                self.registry.require_base_url(AgentRole.DEVELOPER)
            defer_resume = observe_only and not needs_execution
            if run.resume_state is not None and not defer_resume:
                run, _ = self.repository.resume_run_with_step(run_id, step_id)
            if stage == WorkflowStatus.FIX_REQUIRED:
                issues = tuple(i for i in self.repository.list_issue_records(run_id) if i.code_version == run.code_version)
                if not issues:
                    raise WorkflowControlConflict("Failed candidate has no recorded Issue; reconcile its reports first")
                await self.dispatcher._dispatch_fix(run, plan, scenario, issues)
            elif validation_stage:
                source = self._source(run)
                handoff = SnapshotHandoff.from_snapshot(source)
                outcomes = []
                for step in steps:
                    outcomes.append(await self._observe_or_send(
                        run, step, input_data if step.workflow_step_id in continuation_steps else None,
                        handoff=handoff, plan=plan,
                        continue_interrupted=step.workflow_step_id in continuation_steps,
                    ))
                interrupted = next((
                    result for result in outcomes
                    if result.disposition != TaskRunDisposition.COMPLETED
                ), None)
                if interrupted is not None:
                    # Observers have already durably preserved each completed
                    # Task. Do not parse incomplete reports or create a product
                    # verdict while the other Task still needs input/auth.
                    self._pause_result(run, interrupted)
                else:
                    if run.resume_state is not None:
                        run, _ = self.repository.resume_run_with_step(run_id, step_id)
                    await self.dispatcher.consume_validation_results(
                        run, tuple(steps), handoff, tuple(outcomes), plan=plan, scenario=scenario,
                        allow_fix_dispatch=not observe_only,
                    )
            else:
                if len(steps) != 1:
                    raise WorkflowControlConflict("Recovery requires exactly one current Planner or Developer Step")
                result = await self._observe_or_send(run, steps[0], input_data, plan=plan,
                    continue_interrupted=steps[0].workflow_step_id in continuation_steps)
                if result.disposition == TaskRunDisposition.COMPLETED:
                    # GET is free to reconcile a known Task. Advancing Planner
                    # or Developer starts new execution and needs the old budget.
                    self._check_budget(run_id)
                    if run.resume_state is not None:
                        run, _ = self.repository.resume_run_with_step(run_id, step_id)
                    if result.step.agent_role == AgentRole.PLANNER:
                        await self.dispatcher._advance_to_developer(run, result.step.workflow_step_id, result.task)
                    else:
                        await self.dispatcher._advance_to_validation(run, result.step, result.task, plan, scenario)
                else:
                    self._pause_result(run, result)
            return self.repository.get_run(run_id)
        except WorkflowControlConflict:
            raise
        except Exception as exc:
            current = self.repository.get_run(run_id)
            if current is not None and current.status not in _FINAL | {WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW, WorkflowStatus.RECEIVED}:
                self.repository.transition_run_and_record(
                    run_id, WorkflowStatus.HUMAN_REVIEW,
                    additional_event_types=("RECOVERY_REQUIRES_REVIEW",),
                )
            raise WorkflowControlConflict(
                "Recovery could not complete; persisted Task IDs must be reconciled before retrying"
            ) from exc

    async def cancel(self, run_id: UUID, reason: str) -> WorkflowRun:
        # No await between this all-target preflight and stopping the child.
        self._cancel_targets(run_id)
        await self.dispatcher.stop_active_dispatch(run_id)
        token = self.repository.acquire_control(run_id)
        try:
            run = self.repository.get_run(run_id)
            if run is None or run.status in _FINAL:
                raise WorkflowControlConflict("Only an active Run can be canceled")
            steps = self._cancel_targets(run_id)
            confirmed: dict[UUID, A2ATaskState] = {}
            for step in steps:
                if step.a2a_task_id is None:
                    if self._message_sent(step):
                        raise WorkflowControlConflict("A sent request has no confirmed Task ID; reconcile remote execution before cancellation")
                    continue
                if step.a2a_task_state not in _UNRESOLVED_TASKS:
                    continue
                url = self.registry.require_base_url(step.agent_role)
                async with self.client_factory(url) as client:
                    task = await client.cancel_task(step.a2a_task_id)
                if task.id != step.a2a_task_id or A2ATaskState(TaskState.Name(task.status.state)) != A2ATaskState.CANCELED:
                    raise WorkflowControlConflict("Remote Agent has not confirmed Task cancellation")
                if step.agent_context_id is not None and task.context_id != step.agent_context_id:
                    raise WorkflowControlConflict("Cancellation response changed the stored Agent Context")
                canceled = WorkflowStep.model_validate({
                    **step.model_dump(), "status": WorkflowStepStatus.CANCELED,
                    "a2a_task_state": A2ATaskState.CANCELED,
                })
                context = AgentContext(
                    run_id=run_id, agent_id=step.agent_role.value.lower(),
                    agent_context_id=step.agent_context_id,
                    latest_a2a_task_id=step.a2a_task_id,
                )
                self.repository.save_task_update(run, canceled, context, TraceEvent(
                    run_id=run_id, workflow_step_id=step.workflow_step_id,
                    a2a_task_id=step.a2a_task_id, agent_context_id=step.agent_context_id,
                    event_type="A2A_TASK_STATE_CHANGED", actor="Orchestrator",
                    attempt=step.attempt, workflow_state=run.status,
                    a2a_task_state=A2ATaskState.CANCELED,
                ))
                confirmed[step.workflow_step_id] = A2ATaskState.CANCELED
            return self.repository.complete_remote_cancellation(run_id, reason, confirmed)
        finally:
            self.repository.release_control(run_id, token)

    def _cancel_targets(self, run_id):
        run = self.repository.get_run(run_id)
        if run is None or run.status in _FINAL:
            raise WorkflowControlConflict("Only an active Run can be canceled")
        steps = self.repository.list_steps(run_id)
        for step in steps:
            if step.a2a_task_id is None and self._message_sent(step):
                raise WorkflowControlConflict("A sent request has no confirmed Task ID; reconcile remote execution before cancellation")
            if step.status == WorkflowStepStatus.RUNNING and step.a2a_task_state in (A2ATaskState.INPUT_REQUIRED, A2ATaskState.AUTH_REQUIRED):
                raise WorkflowControlConflict("Reconcile the uncertain continuation response before cancellation")
            if step.a2a_task_state in _UNRESOLVED_TASKS:
                self.registry.require_base_url(step.agent_role)
        return steps

    def _check_budget(self, run_id):
        if self.preflight is not None:
            try:
                self.preflight(run_id)
            except Exception:
                raise WorkflowControlConflict("CONTROL_BUDGET_UNAVAILABLE: the existing Run budget cannot authorize resumption") from None

    def _stage_steps(self, run: WorkflowRun, stage: WorkflowStatus, step_id: UUID | None) -> list[WorkflowStep]:
        all_steps = self.repository.list_steps(run.run_id)
        if stage == WorkflowStatus.FIX_REQUIRED:
            return []
        if stage in (WorkflowStatus.RECEIVED, WorkflowStatus.PLANNING):
            selected = [s for s in all_steps if s.agent_role == AgentRole.PLANNER]
        elif stage in (WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING):
            selected = [s for s in all_steps if s.agent_role == AgentRole.DEVELOPER and s.code_version == run.fix_attempt + 1]
        else:
            selected = [s for s in all_steps if s.agent_role in (AgentRole.QA, AgentRole.SECURITY) and s.code_version == run.code_version]
        if step_id is not None and not any(s.workflow_step_id == step_id for s in selected):
            raise WorkflowControlConflict("workflowStepId does not belong to the current Run stage")
        if not selected:
            raise WorkflowControlConflict("Current stage has no durable Step to recover")
        if stage in (WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING, WorkflowStatus.SNAPSHOT_READY) and {s.agent_role for s in selected} != {AgentRole.QA, AgentRole.SECURITY}:
            raise WorkflowControlConflict("Validation recovery requires the matching QA and Security Steps")
        return selected

    def _message_sent(self, step: WorkflowStep) -> bool:
        offset = 0
        while True:
            events, total = self.repository.list_events(step.run_id, limit=500, offset=offset)
            if any(e.workflow_step_id == step.workflow_step_id and e.event_type == "A2A_MESSAGE_SENT" for e in events):
                return True
            offset += len(events)
            if offset >= total or not events:
                return False

    def _check_observable(
        self, step: WorkflowStep, input_data: Mapping[str, object] | None,
        *, continue_interrupted: bool = True,
    ) -> None:
        if step.a2a_task_id is None and self._message_sent(step):
            raise WorkflowControlConflict("SendMessage outcome is unknown; automatic replay is forbidden")
        if step.a2a_task_state in (A2ATaskState.FAILED, A2ATaskState.REJECTED, A2ATaskState.CANCELED, A2ATaskState.UNSPECIFIED):
            raise WorkflowControlConflict("Terminal or invalid Tasks cannot be restarted; a new approved Step is required")
        if continue_interrupted and step.a2a_task_state == A2ATaskState.INPUT_REQUIRED and step.status == WorkflowStepStatus.WAITING_INPUT and not input_data:
            raise WorkflowControlConflict("Task requires inputData before it can continue")
        if continue_interrupted and step.a2a_task_state == A2ATaskState.AUTH_REQUIRED and step.status == WorkflowStepStatus.WAITING_INPUT and step.agent_role not in self.authenticated_roles:
            raise WorkflowControlConflict("Configure this Agent's authentication out of band before resuming")

    def _source(self, run: WorkflowRun) -> CodeSnapshotArtifact:
        source = next((a for a in self.repository.list_project_artifacts(run.run_id) if isinstance(a, CodeSnapshotArtifact) and a.code_version == run.code_version), None)
        if source is None:
            raise WorkflowControlConflict("Current immutable Source Snapshot is not registered")
        return source

    async def _observe_or_send(
        self, run, step, input_data, *, handoff=None, plan=None,
        continue_interrupted: bool = True,
    ) -> A2ATaskRunResult:
        role = step.agent_role
        agent_id = role.value.lower()
        saved_context = next((c for c in self.repository.list_agent_contexts(run.run_id) if c.agent_id == agent_id), None)
        context = (AgentContext(run_id=run.run_id, agent_id=agent_id, agent_context_id=step.agent_context_id, latest_a2a_task_id=step.a2a_task_id) if step.a2a_task_id else saved_context)
        url = self.registry.require_base_url(role)
        async with self.client_factory(url) as client:
            await client.resolve_agent_card()
            runner = A2ATaskRunner(client)
            kwargs = {"agent_id": agent_id, "agent_context": context, "observer": self.repository.task_update_observer(run)}
            if step.a2a_task_id is not None:
                if not continue_interrupted:
                    return await runner.resume_polling(run, step, **kwargs)
                # A previous continuation may have been applied remotely. GET first.
                if step.status == WorkflowStepStatus.RUNNING and step.a2a_task_state in (A2ATaskState.INPUT_REQUIRED, A2ATaskState.AUTH_REQUIRED):
                    return await runner.resume_polling(run, step, **kwargs)
                if step.a2a_task_state == A2ATaskState.INPUT_REQUIRED:
                    return await runner.continue_after_input(run, step, payload=redact_data(dict(input_data)), **kwargs)
                if step.a2a_task_state == A2ATaskState.AUTH_REQUIRED:
                    return await runner.continue_after_auth(run, step, payload={"authenticationConfigured": True}, authentication_configured=True, **kwargs)
                return await runner.resume_polling(run, step, **kwargs)
            if handoff is not None:
                request_text = build_validation_request(
                    self.repository.get_run_configuration(run.run_id), plan,
                    self.dispatcher._scenario_for_run(run), step.requirement_ids, role,
                )
                return await runner.submit_snapshot_and_wait(run, step, handoff, recipient=role, request_text=request_text, **kwargs)
            if role == AgentRole.PLANNER:
                payload = {
                    "request": run.request_text, "workspaceId": str(run.workspace_id),
                    "scenarioContract": self.dispatcher._scenario_for_run(run).planner_contract(),
                    "runConfiguration": self.repository.get_run_configuration(run.run_id).to_artifact_json(),
                }
                return await runner.submit_and_wait(run, step, payload=payload, **kwargs)
            planning = self.repository.get_planning_artifact(run.run_id)
            payload = {
                "workspaceId": str(run.workspace_id),
                "plan": plan.model_dump(mode="json", by_alias=True),
                "sourceArtifact": {"a2aArtifactId": planning.a2a_artifact_id, "projectArtifactId": str(planning.artifact_id), "artifactVersion": planning.artifact_version},
                "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
                "scenario": self.dispatcher._scenario_for_run(run).planner_contract(),
                "runConfiguration": self.repository.get_run_configuration(run.run_id).to_artifact_json(),
            }
            if run.status == WorkflowStatus.FIXING:
                issues = tuple(i for i in self.repository.list_issue_records(run.run_id) if i.code_version == run.code_version and i.revalidation_result is None)
                if not issues:
                    raise WorkflowControlConflict("The unsent fix Step has no original Issue records")
                artifacts = tuple(a for a in self.repository.list_project_artifacts(run.run_id) if a.artifact_id in step.input_artifact_ids)
                payload = self.dispatcher.build_fix_payload(run, step, plan, self.dispatcher._scenario_for_run(run), issues, artifacts)
                payload["runConfiguration"] = self.repository.get_run_configuration(run.run_id).to_artifact_json()
            return await runner.submit_and_wait(run, step, payload=payload, **kwargs)

    def _pause_result(self, run: WorkflowRun, result: A2ATaskRunResult) -> None:
        if run.status == WorkflowStatus.HUMAN_REVIEW:
            return
        if run.status == WorkflowStatus.WAITING_INPUT:
            if result.disposition == TaskRunDisposition.WAITING_INPUT:
                return
            run, _ = self.repository.resume_run_with_step(run.run_id)
        if result.disposition == TaskRunDisposition.WAITING_INPUT and run.status == WorkflowStatus.PLANNING:
            target, verdict = WorkflowStatus.WAITING_INPUT, None
        else:
            target, verdict = WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW
        self.repository.transition_run_and_record(run.run_id, target, verdict=verdict, workflow_step_id=result.step.workflow_step_id)
