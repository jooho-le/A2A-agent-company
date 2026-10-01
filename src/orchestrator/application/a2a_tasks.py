"""Submit A2A Tasks, link them to workflow state, and poll to an interruption."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from math import isfinite
from time import monotonic
from typing import TypeAlias
from a2a.types import Task, TaskState

from orchestrator.a2a import A2AAgentClient, A2AProjectContractError, A2AWorkflowMetadata
from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    SnapshotHandoff,
    TraceEvent,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.domain.models import utc_now


class A2ATaskProtocolError(A2AProjectContractError):
    """Raised when an Agent response changes task identity or breaks its contract."""


class TaskRunDisposition(str, Enum):
    """Agent-task outcome; never a product-level Workflow Verdict."""

    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    AGENT_FAILED = "AGENT_FAILED"
    CANCELED = "CANCELED"
    WAITING_INPUT = "WAITING_INPUT"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    POLLING_TIMEOUT = "POLLING_TIMEOUT"


@dataclass(frozen=True)
class TaskPollingPolicy:
    """Bounded polling policy; defaults are operational MVP defaults, not A2A rules."""

    interval_seconds: float = 1.0
    timeout_seconds: float = 300.0

    def __post_init__(self) -> None:
        if (
            isinstance(self.interval_seconds, bool)
            or not isinstance(self.interval_seconds, (int, float))
            or not isfinite(self.interval_seconds)
            or self.interval_seconds <= 0
        ):
            raise ValueError("interval_seconds must be finite and positive")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not isfinite(self.timeout_seconds)
            or self.timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")


@dataclass(frozen=True)
class A2ATaskRunResult:
    """Latest state and trace observations for one Agent Task execution."""

    step: WorkflowStep
    agent_context: AgentContext
    task: Task
    disposition: TaskRunDisposition
    events: tuple[TraceEvent, ...]
    poll_count: int
    duration_ms: int


TaskUpdateObserver: TypeAlias = Callable[
    [WorkflowStep, AgentContext, TraceEvent], Awaitable[None]
]
Sleep: TypeAlias = Callable[[float], Awaitable[None]]
Clock: TypeAlias = Callable[[], float]


class A2ATaskRunner:
    """Connect A2A Task responses to Step/Context snapshots and Trace events.

    An optional observer is called for each committed logical update. A storage
    adapter can persist the Step, AgentContext, and TraceEvent atomically there;
    this service itself deliberately does not claim DB durability.
    """

    def __init__(
        self,
        client: A2AAgentClient,
        *,
        policy: TaskPollingPolicy | None = None,
        sleep: Sleep = asyncio.sleep,
        clock: Clock = monotonic,
    ) -> None:
        self._client = client
        self._policy = policy or TaskPollingPolicy()
        self._sleep = sleep
        self._clock = clock

    async def submit_and_wait(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        *,
        agent_id: str,
        payload: Mapping[str, object],
        agent_context: AgentContext | None = None,
        observer: TaskUpdateObserver | None = None,
    ) -> A2ATaskRunResult:
        """Send one new Task and poll until terminal, interrupted, or timed out."""
        context = self._prepare_new_step(run, step, agent_id, agent_context)
        metadata = _metadata_for_step(run, step)
        sent = _make_trace_event(
            run,
            step,
            context,
            event_type="A2A_MESSAGE_SENT",
            duration_ms=None,
        )
        await _notify(observer, step, context, sent)
        task = await self._client.send_task(
            payload,
            metadata,
            context_id=context.agent_context_id,
        )
        started_at = self._clock()
        step, context, disposition = _apply_task(
            step,
            context,
            task,
            previous_state=None,
        )
        received = _make_trace_event(
            run,
            step,
            context,
            event_type="A2A_TASK_RECEIVED",
            duration_ms=None,
        )
        events = [sent, received]
        await _notify(observer, step, context, received)
        return await self._poll_from_current(
            run,
            step,
            context,
            task,
            disposition,
            started_at=started_at,
            events=events,
            poll_count=0,
            observer=observer,
        )

    async def submit_snapshot_and_wait(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        handoff: SnapshotHandoff,
        *,
        agent_id: str,
        recipient: AgentRole,
        request_text: str,
        agent_context: AgentContext | None = None,
        observer: TaskUpdateObserver | None = None,
    ) -> A2ATaskRunResult:
        """Send a frozen QA/Security Snapshot to the matching Agent and poll it."""
        context = self._prepare_new_step(run, step, agent_id, agent_context)
        if recipient != step.agent_role:
            raise A2ATaskProtocolError("Snapshot recipient must match WorkflowStep role")
        if handoff.run_id != run.run_id:
            raise A2ATaskProtocolError("Snapshot and WorkflowRun IDs do not match")
        if step.code_version != handoff.execution_manifest.code_version:
            raise A2ATaskProtocolError("Snapshot codeVersion does not match WorkflowStep")
        if handoff.project_artifact_id not in step.input_artifact_ids:
            raise A2ATaskProtocolError(
                "WorkflowStep must reference the handed-off project Artifact"
            )

        sent = _make_trace_event(
            run,
            step,
            context,
            event_type="A2A_MESSAGE_SENT",
            duration_ms=None,
        )
        await _notify(observer, step, context, sent)
        task = await self._client.send_snapshot_handoff(
            handoff,
            recipient,
            request_text,
            workflow_step_id=step.workflow_step_id,
            scenario_id=run.scenario_id,
            context_id=context.agent_context_id,
            attempt=step.attempt,
            requirement_ids=tuple(step.requirement_ids) or None,
        )
        started_at = self._clock()
        step, context, disposition = _apply_task(
            step,
            context,
            task,
            previous_state=None,
        )
        received = _make_trace_event(
            run,
            step,
            context,
            event_type="A2A_TASK_RECEIVED",
            duration_ms=None,
        )
        events = [sent, received]
        await _notify(observer, step, context, received)
        return await self._poll_from_current(
            run,
            step,
            context,
            task,
            disposition,
            started_at=started_at,
            events=events,
            poll_count=0,
            observer=observer,
        )

    async def resume_polling(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        *,
        agent_id: str,
        agent_context: AgentContext,
        observer: TaskUpdateObserver | None = None,
    ) -> A2ATaskRunResult:
        """Resume observation after process restart or a prior polling timeout."""
        context = self._validate_existing_step(run, step, agent_id, agent_context)
        if step.a2a_task_id is None:
            raise A2ATaskProtocolError("WorkflowStep has no server-issued Task ID")
        if step.a2a_task_state not in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING):
            raise A2ATaskProtocolError("Only a nonterminal WorkflowStep can be polled")

        started_at = self._clock()
        task = await self._client.get_task(step.a2a_task_id)
        if task.id != step.a2a_task_id:
            raise A2ATaskProtocolError("GET Task response ID does not match the requested ID")
        previous_step = step
        previous_state = step.a2a_task_state
        step, context, disposition = _apply_task(
            step,
            context,
            task,
            previous_state=previous_state,
        )
        events: list[TraceEvent] = []
        await self._record_update(
            run,
            step,
            context,
            previous_step=previous_step,
            events=events,
            started_at=started_at,
            observer=observer,
        )
        return await self._poll_from_current(
            run,
            step,
            context,
            task,
            disposition,
            started_at=started_at,
            events=events,
            poll_count=1,
            observer=observer,
        )

    async def continue_after_input(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        *,
        agent_id: str,
        payload: Mapping[str, object],
        agent_context: AgentContext,
        observer: TaskUpdateObserver | None = None,
    ) -> A2ATaskRunResult:
        """Send user-provided input to the same interrupted Task and resume polling."""
        context = _make_or_check_context(run, step, agent_id, agent_context)
        if run.run_id != step.run_id:
            raise A2ATaskProtocolError("WorkflowRun and WorkflowStep IDs do not match")
        if (
            step.status != WorkflowStepStatus.WAITING_INPUT
            or step.a2a_task_state != A2ATaskState.INPUT_REQUIRED
            or step.a2a_task_id is None
        ):
            raise A2ATaskProtocolError(
                "Only a Task in INPUT_REQUIRED state can continue with user input"
            )
        if context.latest_a2a_task_id != step.a2a_task_id:
            raise A2ATaskProtocolError("AgentContext does not reference this WorkflowStep Task")
        if context.agent_context_id != step.agent_context_id:
            raise A2ATaskProtocolError("WorkflowStep and AgentContext IDs do not match")

        continued_step_data = step.model_dump(mode="python")
        continued_step_data.update(
            attempt=step.attempt + 1,
            status=WorkflowStepStatus.RUNNING,
            updated_at=utc_now(),
        )
        continued_step = WorkflowStep.model_validate(continued_step_data)
        sent = _make_trace_event(
            run,
            continued_step,
            context,
            event_type="A2A_MESSAGE_SENT",
            duration_ms=None,
        )
        await _notify(observer, continued_step, context, sent)
        task = await self._client.continue_task(
            step.a2a_task_id,
            payload,
            _metadata_for_step(run, continued_step),
            context_id=context.agent_context_id,
        )
        if task.id != step.a2a_task_id:
            raise A2ATaskProtocolError("Continuation response changed the Agent Task ID")

        started_at = self._clock()
        request_step = continued_step
        continued_step, context, disposition = _apply_task(
            continued_step,
            context,
            task,
            previous_state=step.a2a_task_state,
        )
        events = [sent]
        await self._record_update(
            run,
            continued_step,
            context,
            previous_step=request_step,
            events=events,
            started_at=started_at,
            observer=observer,
        )
        return await self._poll_from_current(
            run,
            continued_step,
            context,
            task,
            disposition,
            started_at=started_at,
            events=events,
            poll_count=0,
            observer=observer,
        )

    def _prepare_new_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        agent_id: str,
        agent_context: AgentContext | None,
    ) -> AgentContext:
        if run.run_id != step.run_id:
            raise A2ATaskProtocolError("WorkflowRun and WorkflowStep IDs do not match")
        if step.status not in (
            WorkflowStepStatus.PENDING,
            WorkflowStepStatus.RUNNING,
        ):
            raise A2ATaskProtocolError(
                "A new Agent Task requires a PENDING or preclaimed RUNNING WorkflowStep"
            )
        if step.a2a_task_id is not None or step.a2a_task_state is not None:
            raise A2ATaskProtocolError("A WorkflowStep cannot be assigned a second Task")
        context = _make_or_check_context(run, step, agent_id, agent_context)
        if (
            step.agent_context_id is not None
            and step.agent_context_id != context.agent_context_id
        ):
            raise A2ATaskProtocolError("WorkflowStep and AgentContext IDs do not match")
        return context

    def _validate_existing_step(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        agent_id: str,
        agent_context: AgentContext,
    ) -> AgentContext:
        if run.run_id != step.run_id:
            raise A2ATaskProtocolError("WorkflowRun and WorkflowStep IDs do not match")
        if step.status != WorkflowStepStatus.RUNNING:
            raise A2ATaskProtocolError("Only a RUNNING WorkflowStep can resume polling")
        context = _make_or_check_context(run, step, agent_id, agent_context)
        if context.latest_a2a_task_id != step.a2a_task_id:
            raise A2ATaskProtocolError("AgentContext does not reference this WorkflowStep Task")
        if context.agent_context_id != step.agent_context_id:
            raise A2ATaskProtocolError("WorkflowStep and AgentContext IDs do not match")
        return context

    async def _poll_from_current(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        context: AgentContext,
        task: Task,
        disposition: TaskRunDisposition,
        *,
        started_at: float,
        events: list[TraceEvent],
        poll_count: int,
        observer: TaskUpdateObserver | None,
    ) -> A2ATaskRunResult:
        deadline = started_at + self._policy.timeout_seconds
        while disposition == TaskRunDisposition.IN_PROGRESS:
            remaining = deadline - self._clock()
            if remaining <= 0:
                return await self._timed_out(
                    run,
                    step,
                    context,
                    task,
                    events,
                    poll_count,
                    started_at,
                    observer,
                )
            await self._sleep(min(self._policy.interval_seconds, remaining))
            if self._clock() >= deadline:
                return await self._timed_out(
                    run,
                    step,
                    context,
                    task,
                    events,
                    poll_count,
                    started_at,
                    observer,
                )

            previous_step = step
            previous_state = step.a2a_task_state
            assert step.a2a_task_id is not None
            task = await self._client.get_task(step.a2a_task_id)
            poll_count += 1
            if task.id != step.a2a_task_id:
                raise A2ATaskProtocolError(
                    "GET Task response ID does not match the requested ID"
                )
            step, context, disposition = _apply_task(
                step,
                context,
                task,
                previous_state=previous_state,
            )
            await self._record_update(
                run,
                step,
                context,
                previous_step=previous_step,
                events=events,
                started_at=started_at,
                observer=observer,
            )

        return A2ATaskRunResult(
            step=step,
            agent_context=context,
            task=task,
            disposition=disposition,
            events=tuple(events),
            poll_count=poll_count,
            duration_ms=_elapsed_ms(started_at, self._clock()),
        )

    async def _record_update(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        context: AgentContext,
        *,
        previous_step: WorkflowStep,
        events: list[TraceEvent],
        started_at: float,
        observer: TaskUpdateObserver | None,
    ) -> None:
        if previous_step == step:
            return
        state_changed = previous_step.a2a_task_state != step.a2a_task_state
        event = _make_trace_event(
            run,
            step,
            context,
            event_type=("A2A_TASK_STATE_CHANGED" if state_changed else "A2A_TASK_UPDATED"),
            duration_ms=_elapsed_ms(started_at, self._clock()),
        )
        events.append(event)
        await _notify(observer, step, context, event)

    async def _timed_out(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        context: AgentContext,
        task: Task,
        events: list[TraceEvent],
        poll_count: int,
        started_at: float,
        observer: TaskUpdateObserver | None,
    ) -> A2ATaskRunResult:
        duration_ms = _elapsed_ms(started_at, self._clock())
        event = _make_trace_event(
            run,
            step,
            context,
            event_type="A2A_POLL_TIMED_OUT",
            duration_ms=duration_ms,
        )
        events.append(event)
        await _notify(observer, step, context, event)
        return A2ATaskRunResult(
            step=step,
            agent_context=context,
            task=task,
            disposition=TaskRunDisposition.POLLING_TIMEOUT,
            events=tuple(events),
            poll_count=poll_count,
            duration_ms=duration_ms,
        )


def _metadata_for_step(run: WorkflowRun, step: WorkflowStep) -> A2AWorkflowMetadata:
    return A2AWorkflowMetadata(
        run_id=run.run_id,
        workflow_step_id=step.workflow_step_id,
        scenario_id=run.scenario_id,
        attempt=step.attempt,
        requirement_ids=tuple(step.requirement_ids) or None,
        code_version=step.code_version,
        project_artifact_ids=tuple(step.input_artifact_ids) or None,
    )


def _make_or_check_context(
    run: WorkflowRun,
    step: WorkflowStep,
    agent_id: str,
    agent_context: AgentContext | None,
) -> AgentContext:
    if not agent_id.strip():
        raise A2ATaskProtocolError("agent_id must not be blank")
    if agent_context is None:
        return AgentContext(run_id=run.run_id, agent_id=agent_id)
    if agent_context.run_id != run.run_id or agent_context.agent_id != agent_id:
        raise A2ATaskProtocolError("AgentContext belongs to a different Run or Agent")
    if (
        step.agent_context_id is not None
        and agent_context.agent_context_id is not None
        and step.agent_context_id != agent_context.agent_context_id
    ):
        raise A2ATaskProtocolError("WorkflowStep cannot use another Agent Context")
    return agent_context


def _apply_task(
    step: WorkflowStep,
    context: AgentContext,
    task: Task,
    *,
    previous_state: A2ATaskState | None,
) -> tuple[WorkflowStep, AgentContext, TaskRunDisposition]:
    if not task.id.strip():
        raise A2ATaskProtocolError("Agent returned a Task without a server Task ID")
    if step.a2a_task_id is not None and step.a2a_task_id != task.id:
        raise A2ATaskProtocolError("A WorkflowStep cannot change its A2A Task ID")

    state = _get_task_state(task)
    if previous_state is not None and state == A2ATaskState.UNSPECIFIED:
        disposition = TaskRunDisposition.PROTOCOL_ERROR
    else:
        disposition = _disposition_for_state(state)

    task_context_id = task.context_id if task.context_id.strip() else None
    if (
        task_context_id is not None
        and context.agent_context_id is not None
        and task_context_id != context.agent_context_id
    ):
        raise A2ATaskProtocolError("A2A Task changed its Agent Context ID")
    effective_context_id = task_context_id or context.agent_context_id

    artifact_ids = [artifact.artifact_id for artifact in task.artifacts]
    if any(not artifact_id.strip() for artifact_id in artifact_ids):
        raise A2ATaskProtocolError("A2A Task returned an Artifact without an ID")
    if len(artifact_ids) != len(set(artifact_ids)):
        raise A2ATaskProtocolError("A2A Task returned duplicate Artifact IDs")
    artifact_ids = list(dict.fromkeys([*step.a2a_artifact_ids, *artifact_ids]))

    changes = {
        "status": _step_status_for_state(state),
        "a2a_task_id": task.id,
        "a2a_task_state": state,
        "agent_context_id": effective_context_id,
        "a2a_artifact_ids": artifact_ids,
    }
    changed = any(getattr(step, key) != value for key, value in changes.items())
    updated_step_data = step.model_dump(mode="python")
    updated_step_data.update(changes)
    if changed:
        updated_step_data["updated_at"] = utc_now()
    updated_step = WorkflowStep.model_validate(updated_step_data)

    updated_context_data = context.model_dump(mode="python")
    updated_context_data.update(
        agent_context_id=effective_context_id,
        latest_a2a_task_id=task.id,
    )
    updated_context = AgentContext.model_validate(updated_context_data)
    return updated_step, updated_context, disposition


def _get_task_state(task: Task) -> A2ATaskState:
    try:
        state_name = TaskState.Name(task.status.state)
        return A2ATaskState(state_name)
    except (ValueError, TypeError) as exc:
        raise A2ATaskProtocolError(
            f"Agent returned an unknown A2A Task state: {task.status.state}"
        ) from exc


def _step_status_for_state(state: A2ATaskState) -> WorkflowStepStatus:
    if state in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING):
        return WorkflowStepStatus.RUNNING
    if state == A2ATaskState.COMPLETED:
        return WorkflowStepStatus.SUCCEEDED
    if state in (
        A2ATaskState.FAILED,
        A2ATaskState.REJECTED,
        A2ATaskState.UNSPECIFIED,
    ):
        return WorkflowStepStatus.FAILED
    if state == A2ATaskState.CANCELED:
        return WorkflowStepStatus.CANCELED
    return WorkflowStepStatus.WAITING_INPUT


def _disposition_for_state(state: A2ATaskState) -> TaskRunDisposition:
    if state in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING):
        return TaskRunDisposition.IN_PROGRESS
    if state == A2ATaskState.COMPLETED:
        return TaskRunDisposition.COMPLETED
    if state == A2ATaskState.FAILED:
        return TaskRunDisposition.AGENT_FAILED
    if state == A2ATaskState.CANCELED:
        return TaskRunDisposition.CANCELED
    if state == A2ATaskState.INPUT_REQUIRED:
        return TaskRunDisposition.WAITING_INPUT
    if state in (A2ATaskState.AUTH_REQUIRED, A2ATaskState.REJECTED):
        return TaskRunDisposition.HUMAN_REVIEW
    return TaskRunDisposition.PROTOCOL_ERROR


def _make_trace_event(
    run: WorkflowRun,
    step: WorkflowStep,
    context: AgentContext,
    *,
    event_type: str,
    duration_ms: int | None,
) -> TraceEvent:
    return TraceEvent(
        run_id=run.run_id,
        workflow_step_id=step.workflow_step_id,
        a2a_task_id=step.a2a_task_id,
        agent_context_id=context.agent_context_id,
        event_type=event_type,
        actor="ORCHESTRATOR",
        attempt=step.attempt,
        requirement_ids=step.requirement_ids,
        input_artifact_ids=step.input_artifact_ids,
        code_version=step.code_version,
        a2a_task_state=step.a2a_task_state,
        workflow_state=run.status,
        duration_ms=duration_ms,
    )


async def _notify(
    observer: TaskUpdateObserver | None,
    step: WorkflowStep,
    context: AgentContext,
    event: TraceEvent,
) -> None:
    if observer is not None:
        await observer(step, context, event)


def _elapsed_ms(started_at: float, now: float) -> int:
    return max(0, round((now - started_at) * 1000))
