"""Run submission, status, step, trace, and cancellation endpoints."""

from typing import Annotated
from pathlib import Path
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
    get_workflow_controls,
)
from orchestrator.api.schemas.runs import (
    CancelRunRequest,
    CreateRunRequest,
    ResumeRunRequest,
    RunArtifactsResponse,
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
    RunConfigurationArtifact,
    WorkspaceRecord,
    get_scenario,
)
from orchestrator.application import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlConflict, WorkflowControlService
from orchestrator.a2a.registry import AgentNotConfiguredError
from orchestrator.infrastructure import (
    ActiveAgentTaskError,
    RunNotFoundError,
    RunDispatchConflict,
    SQLiteWorkflowRepository,
)


router = APIRouter(prefix="/runs", tags=["runs"])
Repository = Annotated[SQLiteWorkflowRepository, Depends(get_workflow_repository)]
Dispatcher = Annotated[
    PlannerRunDispatcher | None,
    Depends(get_run_dispatcher),
]
Controls = Annotated[WorkflowControlService, Depends(get_workflow_controls)]


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
    if get_scenario(body.scenario_id) is None:
        raise HTTPException(status_code=422, detail="scenarioId is not registered; see GET /api/v1/scenarios")
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
    configuration = RunConfigurationArtifact(
        run_id=run.run_id, scenario_id=run.scenario_id,
        workspace_id=run.workspace_id, configuration=body.configuration,
    )
    workspace = WorkspaceRecord(
        workspace_id=run.workspace_id, run_id=run.run_id,
        root_path=str((Path(request.app.state.settings.workspace_root) / str(run.workspace_id)).resolve()),
    )
    repository.create_run(run, (step,), events, run_configuration=configuration, workspace=workspace)
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
async def cancel_run(
    run_id: UUID,
    body: CancelRunRequest,
    repository: Repository,
    controls: Controls,
) -> RunStatusResponse:
    _get_run_or_404(repository, run_id)
    try:
        run = await controls.cancel(run_id, body.reason)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc
    except (ActiveAgentTaskError, RunDispatchConflict, WorkflowControlConflict, AgentNotConfiguredError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Remote cancellation could not be confirmed; Run was not marked aborted") from exc
    return RunStatusResponse.from_run(run)


@router.post("/{run_id}/resume", response_model=RunStatusResponse)
async def resume_run(run_id: UUID, body: ResumeRunRequest, repository: Repository, controls: Controls):
    _get_run_or_404(repository, run_id)
    try:
        run = await controls.resume(run_id, step_id=body.workflow_step_id, input_data=body.input_data)
    except (WorkflowControlConflict, RunDispatchConflict, AgentNotConfiguredError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunStatusResponse.from_run(run)


@router.post("/{run_id}/recover", response_model=RunStatusResponse)
async def recover_run(run_id: UUID, body: ResumeRunRequest, repository: Repository, controls: Controls):
    _get_run_or_404(repository, run_id)
    try:
        run = await controls.resume(run_id, step_id=body.workflow_step_id, input_data=body.input_data, recover=True)
    except (WorkflowControlConflict, RunDispatchConflict, AgentNotConfiguredError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunStatusResponse.from_run(run)


@router.get("/{run_id}/artifacts", response_model=RunArtifactsResponse)
def get_run_artifacts(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    artifacts = [a.model_dump(mode="json", by_alias=True) for a in repository.list_project_artifacts(run_id)]
    config = repository.get_run_configuration(run_id)
    if config is not None:
        artifacts.insert(0, config.to_artifact_json())
    return RunArtifactsResponse(run_id=run_id, artifacts=artifacts)


@router.get("/{run_id}/artifacts/{artifact_id}")
def get_run_artifact(run_id: UUID, artifact_id: UUID, repository: Repository):
    result = get_run_artifacts(run_id, repository)
    artifact = next((a for a in result.artifacts if a["artifactId"] == str(artifact_id)), None)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found in this Run")
    return artifact


@router.get("/{run_id}/issues")
def get_run_issues(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    return {"runId": str(run_id), "issues": [i.model_dump(mode="json", by_alias=True) for i in repository.list_issue_records(run_id)]}


@router.get("/{run_id}/configuration")
def get_run_configuration(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    return repository.get_run_configuration(run_id).to_artifact_json()


@router.get("/{run_id}/workspace")
def get_run_workspace(run_id: UUID, repository: Repository):
    run = _get_run_or_404(repository, run_id)
    return repository.get_workspace(run.workspace_id).public_contract()


@router.get("/{run_id}/tool-attempts")
def get_run_tool_attempts(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    return {"runId": str(run_id), "attempts": repository.list_tool_attempts(run_id)}


def _get_run_or_404(repository: SQLiteWorkflowRepository, run_id: UUID) -> WorkflowRun:
    run = repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
