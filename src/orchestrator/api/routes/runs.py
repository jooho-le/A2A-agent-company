"""Run submission, status, step, trace, and cancellation endpoints."""

from typing import Annotated
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)

from orchestrator.api.dependencies import (
    get_run_dispatcher,
    get_workflow_repository,
)
from orchestrator.api.schemas.runs import (
    CancelRunRequest,
    CreateRunRequest,
    RunEventsResponse,
    RunStatusResponse,
    RunStepsResponse,
    RunSubmissionResponse,
    WorkflowStepResponse,
)
from orchestrator.domain import (
    AgentRole,
    TraceEvent,
    WorkflowRun,
    WorkflowStep,
    WorkflowStatus,
)
from orchestrator.application import PlannerRunDispatcher
from orchestrator.infrastructure import (
    ActiveAgentTaskError,
    RunNotFoundError,
    SQLiteWorkflowRepository,
)


router = APIRouter(prefix="/runs", tags=["runs"])
Repository = Annotated[SQLiteWorkflowRepository, Depends(get_workflow_repository)]
Dispatcher = Annotated[
    PlannerRunDispatcher | None,
    Depends(get_run_dispatcher),
]


@router.post(
    "",
    response_model=RunSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_run(
    body: CreateRunRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    repository: Repository,
    dispatcher: Dispatcher,
) -> RunSubmissionResponse:
    """Create a durable Run and its initial pending Planner Step."""
    run = WorkflowRun(
        scenario_id=body.scenario_id,
        request_text=body.request_text,
        status=WorkflowStatus.RECEIVED,
    )
    step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
    events = (
        TraceEvent(
            run_id=run.run_id,
            event_type="RUN_STARTED",
            actor="Orchestrator",
            attempt=0,
            workflow_state=run.status,
        ),
        TraceEvent(
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            event_type="WORKFLOW_STEP_CREATED",
            actor="Orchestrator",
            attempt=step.attempt,
            workflow_state=run.status,
        ),
    )
    repository.create_run(run, (step,), events)
    dispatch_status = "NOT_CONFIGURED"
    if dispatcher is not None:
        background_tasks.add_task(dispatcher.dispatch_planner, run.run_id)
        dispatch_status = "SCHEDULED"
    response.headers["Location"] = str(
        request.url_for("get_run", run_id=str(run.run_id))
    )
    return RunSubmissionResponse(
        run=RunStatusResponse.from_run(run),
        first_step=WorkflowStepResponse.from_step(step),
        dispatch_status=dispatch_status,
    )


@router.get("/{run_id}", response_model=RunStatusResponse)
def get_run(run_id: UUID, repository: Repository) -> RunStatusResponse:
    run = _get_run_or_404(repository, run_id)
    return RunStatusResponse.from_run(run)


@router.get("/{run_id}/steps", response_model=RunStepsResponse)
def get_run_steps(run_id: UUID, repository: Repository) -> RunStepsResponse:
    _get_run_or_404(repository, run_id)
    return RunStepsResponse(
        run_id=run_id,
        steps=[
            WorkflowStepResponse.from_step(step)
            for step in repository.list_steps(run_id)
        ],
    )


@router.get("/{run_id}/events", response_model=RunEventsResponse)
def get_run_events(
    run_id: UUID,
    repository: Repository,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> RunEventsResponse:
    _get_run_or_404(repository, run_id)
    events, total = repository.list_events(run_id, limit=limit, offset=offset)
    return RunEventsResponse(
        run_id=run_id,
        events=events,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/{run_id}/cancel", response_model=RunStatusResponse)
def cancel_run(
    run_id: UUID,
    body: CancelRunRequest,
    repository: Repository,
) -> RunStatusResponse:
    try:
        run = repository.cancel_run(run_id, body.reason)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except ActiveAgentTaskError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunStatusResponse.from_run(run)


def _get_run_or_404(repository: SQLiteWorkflowRepository, run_id: UUID) -> WorkflowRun:
    run = repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
