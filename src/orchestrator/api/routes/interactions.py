"""Owner-mediated user interaction; Host service authentication is mandatory."""
import asyncio
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, Response
from pydantic import Field, UUID4, model_validator

from orchestrator.api.dependencies import get_workflow_controls, get_workflow_repository
from orchestrator.api.host_access import require_host_service_mode
from orchestrator.api.schemas.runs import APIModel
from orchestrator.application.control_input import validate_control_input
from orchestrator.application.run_interactions import interaction_revision, read_interactions
from orchestrator.domain import WorkflowStatus
from orchestrator.infrastructure.run_commands import CommandConflict, RunCommandStore

router = APIRouter(prefix="/runs", tags=["user-interaction"], dependencies=[Depends(require_host_service_mode)])


class UserCommand(APIModel):
    action: Literal["CANCEL", "RESUME", "ANNOTATE"]
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_step_id: UUID4 | None = Field(default=None, alias="workflowStepId")
    clarification: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def validate_action(self):
        if self.action == "CANCEL":
            if self.workflow_step_id is not None or self.clarification is not None:
                raise ValueError("CANCEL takes no clarification or Step")
        elif self.action == 'ANNOTATE':
            if self.workflow_step_id is not None or not self.clarification or not self.clarification.strip() or len(self.clarification.strip()) > 4000:
                raise ValueError('Enter a supplementary explanation without selecting a Step')
        elif self.workflow_step_id is None or not self.clarification or not self.clarification.strip():
            raise ValueError("Choose one waiting Step and enter a clarification")
        if self.clarification is not None:
            self.clarification = self.clarification.strip()
            validate_control_input({"clarification": self.clarification})
        return self


def command_store(request, repository):
    with request.app.state.repository_lock:
        store = getattr(request.app.state, "user_command_store", None)
        if store is None:
            store = RunCommandStore(repository.database_path)
            request.app.state.user_command_store = store
    return store


def require_run(repository, run_id):
    run = repository.get_run(run_id)
    if run is None:
        raise HTTPException(404, "RUN_NOT_FOUND")
    return run


@router.get("/{run_id}/interaction")
async def get_interaction(run_id: UUID, response: Response, request: Request,
                          repository=Depends(get_workflow_repository), controls=Depends(get_workflow_controls)):
    require_run(repository, run_id)
    response.headers["Cache-Control"] = "no-store"
    return await read_interactions(repository, controls, run_id,
        expected_policy_sha256=getattr(request.app.state, 'host_policy_sha256', None))


async def execute_command(store, command_id, run_id, body, repository, controls):
    if not store.claim(command_id):
        return
    try:
        run = require_run(repository, run_id)
        if body.action == "RESUME" and interaction_revision(run, repository.list_steps(run_id)) != body.revision:
            store.finish(command_id, "REJECTED", error_code="STATE_CHANGED")
            return
        if body.action == "CANCEL":
            result = await controls.cancel(run_id, "사용자가 작업 중단을 요청했습니다.")
        else:
            result = await controls.resume(run_id, step_id=body.workflow_step_id,
                                           input_data={"clarification": body.clarification})
        store.finish(command_id, "SUCCEEDED", result_status=result.status.value)
    except asyncio.CancelledError:
        store.finish(command_id, "UNKNOWN", error_code="COMMAND_INTERRUPTED")
        raise
    except Exception:
        # Remote effects may already exist. Never claim rejection or retry them.
        store.finish(command_id, "UNKNOWN", error_code="CONTROL_REQUIRES_REVIEW")


@router.post("/{run_id}/commands", status_code=202)
async def submit_command(run_id: UUID, body: UserCommand, request: Request, response: Response,
                         background: BackgroundTasks,
                         command_id: Annotated[UUID4, Header(alias="Idempotency-Key")],
                         repository=Depends(get_workflow_repository), controls=Depends(get_workflow_controls)):
    run = require_run(repository, run_id)
    store = command_store(request, repository)
    payload = body.model_dump(mode="json", by_alias=True)
    old = store.get(run_id, command_id)
    if old is not None:
        if old["fingerprint"] != store.fingerprint(payload):
            raise HTTPException(409, "COMMAND_KEY_CONFLICT")
        response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return store.public(old)
    if run.status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED):
        raise HTTPException(409, "RUN_ALREADY_ENDED")
    if body.action == 'ANNOTATE':
        from orchestrator.domain.service_design import service_policy
        from orchestrator.infrastructure.run_feedback import admit_feedback
        configuration = repository.get_run_configuration(run_id)
        if configuration is None or not service_policy(configuration.scenario_contract):
            raise HTTPException(409, 'SERVICE_FEEDBACK_NOT_SUPPORTED')
        interaction = await read_interactions(repository, controls, run_id,
            expected_policy_sha256=getattr(request.app.state, 'host_policy_sha256', None))
        if not interaction['canAnnotate']:
            raise HTTPException(409, 'FEEDBACK_NOT_ACTIONABLE')
        try:
            row = admit_feedback(store, run_id, command_id, payload)
        except CommandConflict as error:
            raise HTTPException(409, str(error)) from None
        response.headers['Cache-Control'] = 'no-store'
        return store.public(row)
    if body.action == "RESUME" and interaction_revision(run, repository.list_steps(run_id)) != body.revision:
        raise HTTPException(409, "STATE_CHANGED")
    if body.action == "RESUME":
        if run.status not in (WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW):
            raise HTTPException(409, "RUN_NOT_WAITING")
        interaction = await read_interactions(repository, controls, run_id,
            expected_policy_sha256=getattr(request.app.state, 'host_policy_sha256', None))
        target = next((row for row in interaction["requests"] if row["workflowStepId"] == str(body.workflow_step_id)), None)
        if interaction["revision"] != body.revision or target is None or target["replyMode"] != "ANSWER":
            raise HTTPException(409, "QUESTION_NOT_ACTIONABLE")
    try:
        row, created = store.admit(run_id, command_id, payload)
    except CommandConflict as error:
        raise HTTPException(409, str(error)) from None
    if created:
        background.add_task(execute_command, store, command_id, run_id, body, repository, controls)
    else:
        response.status_code = 200
    response.headers["Cache-Control"] = "no-store"
    return store.public(row)


@router.get("/{run_id}/commands/{command_id}")
def get_command(run_id: UUID, command_id: UUID4, request: Request, response: Response,
                repository=Depends(get_workflow_repository)):
    require_run(repository, run_id)
    row = command_store(request, repository).get(run_id, command_id)
    if row is None:
        raise HTTPException(404, "COMMAND_NOT_FOUND")
    response.headers["Cache-Control"] = "no-store"
    return RunCommandStore.public(row)
