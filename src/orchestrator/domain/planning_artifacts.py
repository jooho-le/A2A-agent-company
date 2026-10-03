"""Immutable registry metadata for a protected Planner requirement document."""

from datetime import datetime
from typing import Literal

from pydantic import Field, UUID4, field_validator, model_validator

from orchestrator.domain.models import utc_now
from orchestrator.domain.snapshot_handoff import ImmutableDomainModel, _validate_artifact_uri
from orchestrator.domain.states import AgentRole


class RequirementArtifact(ImmutableDomainModel):
    artifact_id: UUID4 = Field(alias="artifactId")
    artifact_type: Literal["REQUIREMENT"] = Field(default="REQUIREMENT", alias="artifactType")
    artifact_version: int = Field(default=1, ge=1, alias="artifactVersion")
    previous_artifact_id: UUID4 | None = Field(default=None, alias="previousArtifactId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str = Field(alias="a2aTaskId", min_length=1)
    a2a_artifact_id: str = Field(alias="a2aArtifactId", min_length=1)
    created_by: Literal[AgentRole.PLANNER] = Field(default=AgentRole.PLANNER, alias="createdBy")
    requirement_ids: tuple[UUID4, ...] = Field(alias="requirementIds", min_length=1)
    artifact_uri: str = Field(alias="artifactUri", min_length=1)
    payload: dict[str, object]
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")

    @field_validator("artifact_uri")
    @classmethod
    def uri_is_a_registry_reference(cls, value: str) -> str:
        return _validate_artifact_uri(value)

    @field_validator("created_at")
    @classmethod
    def created_at_has_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Requirement Artifact timestamp must include a timezone")
        return value

    @model_validator(mode="after")
    def baseline_is_the_first_immutable_version(self):
        if self.artifact_version != 1 or self.previous_artifact_id is not None:
            raise ValueError("The protected Planner baseline must start at Artifact version 1")
        if len(self.requirement_ids) != len(set(self.requirement_ids)):
            raise ValueError("Requirement Artifact IDs must be unique")
        return self
