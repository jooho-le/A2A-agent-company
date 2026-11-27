"""Read-only, authenticated lookup after an ambiguous Run creation response."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import UUID4

from orchestrator.api.dependencies import get_workflow_repository
from orchestrator.api.host_access import require_host_service_mode
from orchestrator.api.schemas.runs import RunStatusResponse, RunSubmissionResponse
from orchestrator.api.schemas.submissions import SubmissionLookupResponse, SubmissionRecoveryResponse
from orchestrator.domain.states import WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository


router = APIRouter(prefix="/submissions", tags=["submissions"], dependencies=[Depends(require_host_service_mode)])


def _recovery(view):
    if view.run.status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED):
        action, reason = "NONE", "RUN_TERMINAL"
    elif view.uncertain_send:
        action, reason = "RECONCILE", "A2A_SEND_OUTCOME_UNKNOWN"
    elif (view.run.status == WorkflowStatus.RECEIVED
            and view.first_step.status == WorkflowStepStatus.PENDING
            and view.first_step.a2a_task_id is None and view.first_step.a2a_task_state is None):
        action, reason = "RECOVER", "INITIAL_DISPATCH_PENDING"
    elif view.run.status in (WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW):
        action, reason = "REVIEW", "RUN_REQUIRES_REVIEW_OR_INPUT"
    else:
        action, reason = "OBSERVE", "USE_PERSISTED_RUN_STATE"
    return SubmissionRecoveryResponse(action=action, reasonCode=reason)


@router.get("/{internal_key}", response_model=SubmissionLookupResponse,
    responses={404: {"description": "No committed receipt found; not proof the POST failed"},
               503: {"description": "Receipt lookup unavailable or inconsistent"}})
def get_submission(internal_key: UUID4, response: Response,
    repository: Annotated[SQLiteWorkflowRepository, Depends(get_workflow_repository)]):
    response.headers["Cache-Control"] = "no-store"
    try:
        view = repository.get_submission_view(internal_key)
        if view is None:
            raise HTTPException(status_code=404, detail="SUBMISSION_NOT_FOUND",
                                headers={"Cache-Control": "no-store"})
        original = RunSubmissionResponse.model_validate_json(view.receipt.response_json)
        if (original.run.run_id != view.run.run_id or original.first_step.run_id != view.run.run_id
                or original.first_step.workflow_step_id != view.first_step.workflow_step_id
                or original.run.scenario_id != view.run.scenario_id
                or original.run.workspace_id != view.run.workspace_id):
            raise ValueError
        return SubmissionLookupResponse(internalKey=internal_key, status=view.receipt.status,
            requestSha256=view.receipt.request_sha256, runId=view.run.run_id,
            createdAt=view.receipt.created_at, originalResponse=original,
            run=RunStatusResponse.from_run(view.run), recovery=_recovery(view))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=503, detail="SUBMISSION_LOOKUP_UNAVAILABLE",
                            headers={"Cache-Control": "no-store"}) from None
