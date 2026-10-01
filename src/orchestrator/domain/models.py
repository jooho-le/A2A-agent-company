from datetime import datetime, timezone
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator

from orchestrator.domain.states import (
    A2ATaskState,
    AgentRole,
    FinalVerdict,
    WorkflowStatus,
    WorkflowStepStatus,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class WorkflowRun(DomainModel):
    """One Orchestrator execution for a single user request."""

    run_id: UUID4 = Field(default_factory=uuid4)
    scenario_id: UUID4
    request_text: str = Field(min_length=1)
    status: WorkflowStatus = WorkflowStatus.RECEIVED
    verdict: FinalVerdict | None = None
    code_version: int | None = Field(default=None, ge=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("request_text")
    @classmethod
    def request_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_text must not be blank")
        return value


class WorkflowStep(DomainModel):
    """One logical agent operation within a Run."""

    workflow_step_id: UUID4 = Field(default_factory=uuid4)
    run_id: UUID4
    agent_role: AgentRole
    status: WorkflowStepStatus = WorkflowStepStatus.PENDING
    attempt: int = Field(default=0, ge=0)
    a2a_task_id: str | None = Field(default=None, min_length=1)
    a2a_task_state: A2ATaskState | None = None
    agent_context_id: str | None = Field(default=None, min_length=1)
    code_version: int | None = Field(default=None, ge=1)
    input_artifact_ids: list[UUID4] = Field(default_factory=list)
    output_artifact_ids: list[UUID4] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
