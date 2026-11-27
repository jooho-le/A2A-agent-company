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
    get_workspace_registry,
    get_artifact_store,
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
from orchestrator.api.submissions import get_submission_identity, replay_submission, SubmissionIdentity
from orchestrator.domain.submissions import RunSubmissionRecord, SubmissionKeyConflict
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
from orchestrator.workspaces.policy import LAYOUT_VERSION, WorkspaceAccessError, WorkspaceErrorCode
from orchestrator.workspaces.registry import WorkspaceRegistry


router = APIRouter(prefix="/runs", tags=["runs"])
Repository = Annotated[SQLiteWorkflowRepository, Depends(get_workflow_repository)]
Dispatcher = Annotated[
    PlannerRunDispatcher | None,
    Depends(get_run_dispatcher),
]
Controls = Annotated[WorkflowControlService, Depends(get_workflow_controls)]
Workspaces = Annotated[WorkspaceRegistry, Depends(get_workspace_registry)]


@router.post(
    "",
    response_model=RunSubmissionResponse,
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": RunSubmissionResponse, "description": "Replay of a persisted creation receipt"},
               409: {"description": "Idempotency key already belongs to a different request"}},
)
def create_run(
    body: CreateRunRequest,
    request: Request,
    response: Response,
    background_tasks: BackgroundTasks,
    submission: Annotated[SubmissionIdentity | None, Depends(get_submission_identity)],
    repository: Repository,
    dispatcher: Dispatcher,
) -> RunSubmissionResponse:
    """Create a durable Run and its initial pending Planner Step."""
    if submission is not None:
        existing = repository.get_submission(submission.internal_key)
        if existing is not None:
            try:
                return replay_submission(existing, submission, request, response)
            except SubmissionKeyConflict:
                raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_CONFLICT") from None
    if get_scenario(body.scenario_id) is None:
        raise HTTPException(status_code=422, detail="scenarioId is not registered; see GET /api/v1/scenarios")
    # Only an explicitly composed runtime can admit Single; default stays Multi.
    supported = getattr(dispatcher, "supports_architecture", None)
    if body.configuration.architecture == "SINGLE_AGENT" and (not callable(supported) or not supported("SINGLE_AGENT")):
        raise HTTPException(status_code=503, detail="SINGLE_AGENT_RUNTIME_NOT_CONFIGURED")
    if callable(supported) and not supported(body.configuration.architecture):
        raise HTTPException(status_code=422, detail="RUNTIME_ARCHITECTURE_MISMATCH")
    validator = request.app.state.submission_validator
    if validator is not None:
        try:
            validator(body.configuration)
        except Exception:
            raise HTTPException(status_code=422, detail="OWNED_AGENT_CONFIGURATION_INVALID") from None
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
        **_service_baseline(run, body.configuration),
    )
    continuation = None
    from orchestrator.infrastructure.service_continuations import (
        capture_continuation, ContinuationConflict, requires_previous_work)
    from orchestrator.domain.scenario_registry import SERVICE_WEB_ID
    if body.continuation_of is not None:
        if body.scenario_id != SERVICE_WEB_ID:
            raise HTTPException(422, detail='CONTINUATION_SERVICE_ONLY')
        source_configuration = getattr(request.app.state, 'service_source_configuration', None)
        if source_configuration is None:
            raise HTTPException(503, detail='CONTINUATION_NOT_CONFIGURED')
        try:
            continuation = capture_continuation(repository, get_workspace_registry(request), get_artifact_store(request),
                source_configuration, parent_id=body.continuation_of, run_id=run.run_id,
                instruction=body.request_text, configuration=body.configuration)
            configuration = configuration.model_copy(update={'frozen_scenario_contract_json':continuation.contract_json})
        except ContinuationConflict as error:
            raise HTTPException(409, detail=error.code) from None
        except Exception:
            raise HTTPException(409, detail='CONTINUATION_SOURCE_UNAVAILABLE') from None
    elif body.scenario_id == SERVICE_WEB_ID and requires_previous_work(body.request_text):
        raise HTTPException(422, detail='CONTINUATION_TARGET_REQUIRED')
    workspace = WorkspaceRecord(
        workspace_id=run.workspace_id, run_id=run.run_id,
        root_path=str((Path(request.app.state.settings.workspace_root) / str(run.workspace_id)).resolve()),
    )
    dispatch_status = "SCHEDULED" if dispatcher is not None else "NOT_CONFIGURED"
    submitted = RunSubmissionResponse(
        continuation_of=body.continuation_of,
        run=RunStatusResponse.from_run(run),
        first_step=WorkflowStepResponse.from_step(step),
        dispatch_status=dispatch_status,
    )
    record = None if submission is None else RunSubmissionRecord(
        internal_key=submission.internal_key, request_sha256=submission.request_sha256,
        run_id=run.run_id, first_step_id=step.workflow_step_id, created_at=run.created_at,
        response_json=submitted.model_dump_json(by_alias=True),
    )
    try:
        result = repository.create_run(run, (step,), events, run_configuration=configuration,
                                       workspace=workspace, **({"submission": record} if record else {}),
                                       **({'continuation':continuation} if continuation is not None else {}))
        if result is not None and not result[1]:
            return replay_submission(result[0], submission, request, response)
    except SubmissionKeyConflict:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_KEY_CONFLICT") from None
    except ContinuationConflict as error:
        raise HTTPException(409, detail=error.code) from None
    if dispatcher is not None:
        background_tasks.add_task(dispatcher.dispatch_planner, run.run_id)
    if submission is not None:
        response.headers["Idempotency-Replayed"] = "false"
        response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = str(
        request.url_for("get_run", run_id=str(run.run_id))
    )
    return submitted


def _service_baseline(run, configuration):
    from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, service_request_contract
    from orchestrator.core.security import redact_data
    import json
    if run.scenario_id != SERVICE_WEB_ID:
        return {}
    if configuration.experiment_id is not None or configuration.protected_test_suite_ref is not None:
        raise HTTPException(status_code=422, detail="SERVICE_REQUEST_REQUIRES_NON_EXPERIMENT_CONFIGURATION")
    try:
        contract = service_request_contract(run.request_text)
    except (ValueError, UnicodeError):
        raise HTTPException(status_code=422, detail="SERVICE_REQUEST_INVALID") from None
    if redact_data(contract) != contract:
        raise HTTPException(status_code=422, detail="SERVICE_REQUEST_CONTAINS_CREDENTIALS")
    return {"frozen_scenario_contract_json": json.dumps(contract, ensure_ascii=False, sort_keys=True, separators=(",", ":"))}


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


def _telemetry_or_503(request):
    store = request.app.state.telemetry_store
    if store is None:
        raise HTTPException(status_code=503, detail="RUNTIME_TELEMETRY_NOT_CONFIGURED")
    return store


@router.get("/{run_id}/usage")
def get_run_usage(run_id: UUID, request: Request, repository: Repository,
                  limit: int = Query(default=100, ge=1, le=500),
                  offset: int = Query(default=0, ge=0)):
    _get_run_or_404(repository, run_id)
    store = _telemetry_or_503(request)
    try:
        records, total = store.list_usage(run_id, limit=limit, offset=offset)
        summary = store.usage_summary(run_id)
    except Exception:
        raise HTTPException(status_code=503, detail="RUNTIME_TELEMETRY_UNAVAILABLE") from None
    return {"runId": str(run_id), "summary": summary, "records": records,
            "total": total, "limit": limit, "offset": offset}


@router.get("/{run_id}/metrics")
def get_run_metrics(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    from agents.platform.run_records import collect_run_metrics, RunRecordsError
    try:
        return collect_run_metrics(repository, run_id)
    except RunRecordsError:
        raise HTTPException(status_code=503, detail="RUN_METRICS_UNAVAILABLE") from None


@router.get("/{run_id}/package-production")
def get_package_production(run_id: UUID, repository: Repository):
    """Read Host completion progress; querying never creates a result package."""
    _get_run_or_404(repository, run_id)
    from agents.platform.package_records import package_production_status
    try:
        return package_production_status(repository, run_id)
    except Exception:
        raise HTTPException(status_code=503, detail="PACKAGE_STATUS_UNAVAILABLE") from None


@router.get("/{run_id}/boundary-reports")
def get_boundary_reports(run_id: UUID, repository: Repository):
    """Saved held/input-waiting records, separate from final result packages."""
    _get_run_or_404(repository, run_id)
    from agents.platform.package_records import stored_boundary_reports
    return {'runId': str(run_id), 'reports': stored_boundary_reports(repository, run_id)}


@router.get("/{run_id}/telemetry")
def get_run_telemetry(run_id: UUID, request: Request, repository: Repository,
                      limit: int = Query(default=100, ge=1, le=500),
                      offset: int = Query(default=0, ge=0)):
    _get_run_or_404(repository, run_id)
    store = _telemetry_or_503(request)
    try:
        events, total = store.list_events(run_id, limit=limit, offset=offset)
    except Exception:
        raise HTTPException(status_code=503, detail="RUNTIME_TELEMETRY_UNAVAILABLE") from None
    return {"runId": str(run_id), "events": events, "total": total,
            "limit": limit, "offset": offset}


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
async def resume_run(run_id: UUID, body: ResumeRunRequest, request: Request, repository: Repository, controls: Controls):
    _get_run_or_404(repository, run_id)
    _require_supported_continuation(request, repository, run_id)
    try:
        run = await controls.resume(run_id, step_id=body.workflow_step_id, input_data=body.input_data)
    except (WorkflowControlConflict, RunDispatchConflict, AgentNotConfiguredError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunStatusResponse.from_run(run)


@router.post("/{run_id}/recover", response_model=RunStatusResponse)
async def recover_run(run_id: UUID, body: ResumeRunRequest, request: Request, repository: Repository, controls: Controls):
    _get_run_or_404(repository, run_id)
    _require_supported_continuation(request, repository, run_id)
    try:
        run = await controls.resume(run_id, step_id=body.workflow_step_id, input_data=body.input_data, recover=True)
    except (WorkflowControlConflict, RunDispatchConflict, AgentNotConfiguredError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return RunStatusResponse.from_run(run)


def _require_supported_continuation(request, repository, run_id):
    configuration = repository.get_run_configuration(run_id)
    if (configuration is not None and configuration.configuration.architecture == "SINGLE_AGENT"
            and not getattr(request.app.state, "single_continuation_ready", False)):
        raise HTTPException(status_code=503, detail="SINGLE_AGENT_CONTINUATION_NOT_CONFIGURED")


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


@router.post("/{run_id}/workspace/provision")
def provision_run_workspace(run_id: UUID, repository: Repository, registry: Workspaces):
    """Explicitly prepare known server-issued storage; no role or Host path input."""
    run = _get_run_or_404(repository, run_id)
    try:
        workspace = registry.provision(run.workspace_id, run_id=run.run_id)
    except WorkspaceAccessError as error:
        code = 409 if error.code in (
            WorkspaceErrorCode.CONFLICT, WorkspaceErrorCode.PATH_DENIED,
        ) else 503
        raise HTTPException(status_code=code, detail=error.code.value) from None
    return {**workspace.public_contract(), "provisioned": True, "layoutVersion": LAYOUT_VERSION}


@router.get("/{run_id}/tool-attempts")
def get_run_tool_attempts(run_id: UUID, repository: Repository):
    _get_run_or_404(repository, run_id)
    return {"runId": str(run_id), "attempts": repository.list_tool_attempts(run_id)}


def _get_run_or_404(repository: SQLiteWorkflowRepository, run_id: UUID) -> WorkflowRun:
    run = repository.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return run
