from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    UUID4,
    field_validator,
    model_validator,
)

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
    fix_attempt: int = Field(default=0, ge=0, le=3)
    termination_reason: str | None = Field(default=None, min_length=1)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("request_text")
    @classmethod
    def request_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("request_text must not be blank")
        return value

    @model_validator(mode="after")
    def validate_outcome(self) -> "WorkflowRun":
        if self.status == WorkflowStatus.FINISHED and self.verdict is None:
            raise ValueError("FINISHED runs must have a final verdict")
        if self.status == WorkflowStatus.ABORTED:
            if self.verdict is not None:
                raise ValueError("ABORTED runs must not have a final verdict")
            if self.termination_reason is None:
                raise ValueError("ABORTED runs must include a termination_reason")
        elif self.termination_reason is not None:
            raise ValueError("termination_reason is only valid for ABORTED runs")

        if self.status == WorkflowStatus.HUMAN_REVIEW:
            if self.verdict not in (None, FinalVerdict.HUMAN_REVIEW):
                raise ValueError("HUMAN_REVIEW runs may only have HUMAN_REVIEW verdict")
        elif self.status not in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED):
            if self.verdict is not None:
                raise ValueError("non-terminal runs must not have a final verdict")
        return self

    def with_outcome(
        self,
        *,
        status: WorkflowStatus,
        verdict: FinalVerdict | None = None,
        termination_reason: str | None = None,
    ) -> "WorkflowRun":
        """Return a validated copy with status-related fields changed atomically."""
        values = self.model_dump()
        values.update(
            status=status,
            verdict=verdict,
            termination_reason=termination_reason,
        )
        return type(self).model_validate(values)


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
    requirement_ids: list[UUID4] = Field(default_factory=list)
    code_version: int | None = Field(default=None, ge=1)
    a2a_artifact_ids: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list
    )
    input_artifact_ids: list[UUID4] = Field(default_factory=list)
    output_artifact_ids: list[UUID4] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("requirement_ids", "input_artifact_ids", "output_artifact_ids")
    @classmethod
    def project_artifact_ids_must_be_unique(cls, values: list[UUID4]) -> list[UUID4]:
        if len(values) != len(set(values)):
            raise ValueError("project ID lists must not contain duplicates")
        return values

    @field_validator("a2a_artifact_ids")
    @classmethod
    def a2a_artifact_ids_must_be_unique(
        cls, values: list[str]
    ) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("A2A artifact IDs must not contain duplicates")
        return values


class AgentContext(DomainModel):
    """Agent-local A2A context mapping scoped to one Run."""

    run_id: UUID4
    agent_id: str = Field(min_length=1)
    agent_context_id: str | None = Field(default=None, min_length=1)
    latest_a2a_task_id: str | None = Field(default=None, min_length=1)
