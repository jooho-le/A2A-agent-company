"""Validated project-workflow transitions for a single Run."""

from types import MappingProxyType
from typing import Mapping

from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS
from orchestrator.domain.models import WorkflowRun, utc_now
from orchestrator.domain.states import FinalVerdict, WorkflowStatus


class TransitionError(ValueError):
    """Raised when a requested workflow transition violates the contract."""


ALLOWED_TRANSITIONS: Mapping[WorkflowStatus, frozenset[WorkflowStatus]] = (
    MappingProxyType(
        {
            WorkflowStatus.RECEIVED: frozenset(
                {WorkflowStatus.PLANNING, WorkflowStatus.ABORTED}
            ),
            WorkflowStatus.PLANNING: frozenset(
                {
                    WorkflowStatus.IMPLEMENTING,
                    WorkflowStatus.WAITING_INPUT,
                    WorkflowStatus.HUMAN_REVIEW,
                    WorkflowStatus.ABORTED,
                }
            ),
            WorkflowStatus.WAITING_INPUT: frozenset({WorkflowStatus.ABORTED}),
            WorkflowStatus.IMPLEMENTING: frozenset(
                {
                    WorkflowStatus.SNAPSHOT_READY,
                    WorkflowStatus.HUMAN_REVIEW,
                    WorkflowStatus.ABORTED,
                }
            ),
            WorkflowStatus.SNAPSHOT_READY: frozenset(
                {
                    WorkflowStatus.VALIDATING,
                    WorkflowStatus.FIX_REQUIRED,
                    WorkflowStatus.HUMAN_REVIEW,
                }
            ),
            WorkflowStatus.VALIDATING: frozenset(
                {
                    WorkflowStatus.FINISHED,
                    WorkflowStatus.FIX_REQUIRED,
                    WorkflowStatus.HUMAN_REVIEW,
                }
            ),
            WorkflowStatus.FIX_REQUIRED: frozenset(
                {WorkflowStatus.FIXING, WorkflowStatus.HUMAN_REVIEW}
            ),
            WorkflowStatus.FIXING: frozenset(
                {
                    WorkflowStatus.REVALIDATING,
                    WorkflowStatus.HUMAN_REVIEW,
                    WorkflowStatus.FINISHED,
                }
            ),
            WorkflowStatus.REVALIDATING: frozenset(
                {
                    WorkflowStatus.FINISHED,
                    WorkflowStatus.FIX_REQUIRED,
                    WorkflowStatus.HUMAN_REVIEW,
                }
            ),
            WorkflowStatus.HUMAN_REVIEW: frozenset(
                {WorkflowStatus.FINISHED, WorkflowStatus.ABORTED}
            ),
            WorkflowStatus.FINISHED: frozenset(),
            WorkflowStatus.ABORTED: frozenset(),
        }
    )
)

_TERMINAL_STATES = frozenset({WorkflowStatus.FINISHED, WorkflowStatus.ABORTED})
_PAUSED_STATES = frozenset({WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW})


def transition_run(
    run: WorkflowRun,
    target: WorkflowStatus,
    *,
    verdict: FinalVerdict | None = None,
    termination_reason: str | None = None,
) -> WorkflowRun:
    """Return a validated copy of ``run`` in ``target``.

    An explicit cancellation is allowed from every non-terminal state to
    implement the project's ``POST /runs/{runId}/cancel`` contract, even where
    the normal workflow transition table has no ``ABORTED`` edge.
    """
    try:
        target = WorkflowStatus(target)
        if verdict is not None:
            verdict = FinalVerdict(verdict)
    except ValueError as exc:
        raise TransitionError(f"unknown target state or verdict: {exc}") from exc

    if run.status in _TERMINAL_STATES:
        raise TransitionError(f"terminal state {run.status.value} cannot transition")
    if target == run.status:
        raise TransitionError("self-transitions are not allowed")

    is_resume = run.status in _PAUSED_STATES and target == run.resume_state
    is_cancel_exception = target == WorkflowStatus.ABORTED
    if not (
        target in ALLOWED_TRANSITIONS[run.status]
        or is_resume
        or is_cancel_exception
    ):
        raise TransitionError(f"transition {run.status.value} -> {target.value} is not allowed")

    if target == WorkflowStatus.FIX_REQUIRED and run.fix_attempt >= MAX_CODE_FIX_ATTEMPTS:
        raise TransitionError(
            "code-fix limit reached; finish with FAIL or move to HUMAN_REVIEW"
        )
    starts_fix = target == WorkflowStatus.FIXING and not is_resume
    if starts_fix and run.fix_attempt >= MAX_CODE_FIX_ATTEMPTS:
        raise TransitionError("maximum code-fix attempts already reached")

    if target == WorkflowStatus.FINISHED and verdict is None:
        raise TransitionError("FINISHED requires a final verdict")
    if target != WorkflowStatus.FINISHED and verdict is not None:
        if not (target == WorkflowStatus.HUMAN_REVIEW and verdict == FinalVerdict.HUMAN_REVIEW):
            raise TransitionError("verdict is only accepted at FINISHED (or HUMAN_REVIEW)")
    if target == WorkflowStatus.ABORTED:
        if termination_reason is None or not termination_reason.strip():
            raise TransitionError("ABORTED requires a non-empty termination_reason")
        if verdict is not None:
            raise TransitionError("ABORTED must not have a final verdict")
    elif termination_reason is not None:
        raise TransitionError("termination_reason is only valid for ABORTED")

    next_resume_state: WorkflowStatus | None = None
    if target in _PAUSED_STATES:
        next_resume_state = run.status
    elif run.status in _PAUSED_STATES and is_resume:
        next_resume_state = None

    values = run.model_dump()
    values.update(
        status=target,
        resume_state=next_resume_state,
        verdict=verdict,
        termination_reason=termination_reason,
        fix_attempt=run.fix_attempt + int(starts_fix),
        updated_at=utc_now(),
    )
    return WorkflowRun.model_validate(values)
