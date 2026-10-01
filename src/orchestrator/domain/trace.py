"""Project Trace events emitted by the Orchestrator lifecycle."""

from datetime import datetime
from uuid import uuid4

from pydantic import ConfigDict, Field, UUID4, field_validator

from orchestrator.domain.models import DomainModel, utc_now
from orchestrator.domain.states import A2ATaskState, WorkflowStatus


class TraceEvent(DomainModel):
    """One append-only event conforming to the project Trace Event contract."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    event_id: UUID4 = Field(default_factory=uuid4, alias="eventId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 | None = Field(default=None, alias="workflowStepId")
    a2a_task_id: str | None = Field(default=None, alias="a2aTaskId")
    agent_context_id: str | None = Field(default=None, alias="agentContextId")
    event_type: str = Field(alias="eventType", min_length=1)
    occurred_at: datetime = Field(default_factory=utc_now, alias="occurredAt")
    actor: str = Field(min_length=1)
    attempt: int = Field(ge=0, strict=True)
    requirement_ids: list[UUID4] = Field(default_factory=list, alias="requirementIds")
    input_artifact_ids: list[UUID4] = Field(
        default_factory=list, alias="inputArtifactIds"
    )
    output_artifact_ids: list[UUID4] = Field(
        default_factory=list, alias="outputArtifactIds"
    )
    issue_id: UUID4 | None = Field(default=None, alias="issueId")
    code_version: int | None = Field(default=None, ge=1, strict=True, alias="codeVersion")
    snapshot_sha256: str | None = Field(default=None, alias="snapshotSha256")
    a2a_task_state: A2ATaskState | None = Field(default=None, alias="a2aTaskState")
    workflow_state: WorkflowStatus | None = Field(default=None, alias="workflowState")
    duration_ms: int | None = Field(default=None, ge=0, strict=True, alias="durationMs")

    @field_validator("a2a_task_id", "agent_context_id")
    @classmethod
    def opaque_reference_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("opaque A2A references must not be blank")
        return value

    @field_validator("event_type", "actor")
    @classmethod
    def labels_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("event_type and actor must not be blank")
        return value

    @field_validator("occurred_at")
    @classmethod
    def timestamp_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @field_validator("requirement_ids", "input_artifact_ids", "output_artifact_ids")
    @classmethod
    def project_id_lists_must_be_unique(
        cls, values: list[UUID4]
    ) -> list[UUID4]:
        if len(values) != len(set(values)):
            raise ValueError("Trace project ID lists must not contain duplicates")
        return values

    def to_trace_json(self) -> dict[str, object]:
        """Serialize using the Trace Store's camelCase field names."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)
