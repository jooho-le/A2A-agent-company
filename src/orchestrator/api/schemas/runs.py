"""Run API schemas kept separate from the internal domain models."""

from datetime import datetime
from typing import Any, Literal

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
    RunConfiguration,
    SCN_001_ID,
)
from orchestrator.core.security import redact_data, redact_text


class APIModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class CreateRunRequest(APIModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True,
        json_schema_extra={"examples": [{
            "scenarioId": str(SCN_001_ID),
            "requestText": "이메일과 비밀번호로 회원가입 기능을 만들어줘.",
        }]},
    )
    scenario_id: UUID4 = Field(alias="scenarioId")
    request_text: str = Field(alias="requestText", min_length=1)
    configuration: RunConfiguration = Field(default_factory=RunConfiguration)

    @field_validator("request_text")
    @classmethod
    def nonblank_request(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("requestText must not be blank")
        return redact_text(value)


class ResumeRunRequest(APIModel):
    workflow_step_id: UUID4 | None = Field(default=None, alias="workflowStepId")
    input_data: dict[str, Any] | None = Field(default=None, alias="inputData")

    @field_validator("input_data")
    @classmethod
    def safe_input(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is not None and not value:
            raise ValueError("inputData must not be empty")
        return redact_data(value) if value is not None else None


class CancelRunRequest(APIModel):
    reason: str = Field(min_length=1)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason must not be blank")
        return redact_text(value)


class RunStatusResponse(APIModel):
    run_id: UUID4 = Field(alias="runId")
    scenario_id: UUID4 = Field(alias="scenarioId")
    workspace_id: UUID4 = Field(alias="workspaceId")
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


class RunArtifactsResponse(APIModel):
    run_id: UUID4 = Field(alias="runId")
    artifacts: list[dict[str, Any]]
