"""Run API schemas kept separate from the internal domain models."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from orchestrator.domain import (
    A2ATaskState,
    AgentRole,
    FinalVerdict,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CreateRunRequest(APIModel):
    scenario_id: UUID4 = Field(alias="scenarioId")
    request_text: str = Field(alias="requestText", min_length=1)


class CancelRunRequest(APIModel):
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return value


class RunStatusResponse(APIModel):
    run_id: UUID4 = Field(alias="runId")
    scenario_id: UUID4 = Field(alias="scenarioId")
    status: WorkflowStatus
    resume_state: WorkflowStatus | None = Field(alias="resumeState")
    verdict: FinalVerdict | None
    code_version: int | None = Field(alias="codeVersion")
    fix_attempt: int = Field(alias="fixAttempt")
    termination_reason: str | None = Field(alias="terminationReason")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    @classmethod
    def from_run(cls, run: WorkflowRun) -> "RunStatusResponse":
        return cls.model_validate(run.model_dump(exclude={"request_text"}))


class WorkflowStepResponse(APIModel):
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    run_id: UUID4 = Field(alias="runId")
    agent_role: AgentRole = Field(alias="agentRole")
    status: WorkflowStepStatus
    attempt: int
    a2a_task_id: str | None = Field(alias="a2aTaskId")
    a2a_task_state: A2ATaskState | None = Field(alias="a2aTaskState")
    requirement_ids: list[UUID4] = Field(alias="requirementIds")
    code_version: int | None = Field(alias="codeVersion")
    a2a_artifact_ids: list[str] = Field(alias="a2aArtifactIds")
    input_artifact_ids: list[UUID4] = Field(alias="inputArtifactIds")
    output_artifact_ids: list[UUID4] = Field(alias="outputArtifactIds")
    created_at: datetime = Field(alias="createdAt")
    updated_at: datetime = Field(alias="updatedAt")

    @classmethod
    def from_step(cls, step: WorkflowStep) -> "WorkflowStepResponse":
        return cls.model_validate(step.model_dump(exclude={"agent_context_id"}))


class RunSubmissionResponse(APIModel):
    run: RunStatusResponse
    first_step: WorkflowStepResponse = Field(alias="firstStep")
    dispatch_status: Literal["SCHEDULED", "NOT_CONFIGURED"] = Field(
        alias="dispatchStatus"
    )


class RunStepsResponse(APIModel):
    run_id: UUID4 = Field(alias="runId")
    steps: list[WorkflowStepResponse]


class RunEventsResponse(APIModel):
    run_id: UUID4 = Field(alias="runId")
    events: list[TraceEvent]
    total: int
    limit: int
    offset: int
