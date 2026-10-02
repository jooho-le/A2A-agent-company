"""Append-only, traceable issues that can be handed back to Developer."""

from datetime import datetime
from uuid import uuid4

from pydantic import ConfigDict, Field, UUID4, field_validator

from orchestrator.domain.models import DomainModel, utc_now
from orchestrator.domain.states import AgentRole


class IssueRecord(DomainModel):
    model_config = ConfigDict(
        extra="forbid", validate_assignment=True, populate_by_name=True
    )

    issue_id: UUID4 = Field(default_factory=uuid4, alias="issueId")
    run_id: UUID4 = Field(alias="runId")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_version: int = Field(ge=1, alias="codeVersion")
    source_artifact_id: UUID4 = Field(alias="sourceArtifactId")
    report_artifact_id: UUID4 | None = Field(default=None, alias="reportArtifactId")
    requirement_ids: list[UUID4] = Field(min_length=1, alias="requirementIds")
    reporter: AgentRole
    category: str = Field(min_length=1)
    reference_id: str = Field(min_length=1, alias="referenceId")
    severity: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    consecutive_repeat_count: int = Field(ge=0, alias="consecutiveRepeatCount")
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")

    @field_validator("requirement_ids")
    @classmethod
    def unique_requirements(cls, values: list[UUID4]) -> list[UUID4]:
        if len(values) != len(set(values)):
            raise ValueError("Issue Requirement IDs must be unique")
        return values

    @field_validator("category", "reference_id", "severity", "title", "description")
    @classmethod
    def non_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Issue text fields must not be blank")
        return value

    @field_validator("created_at")
    @classmethod
    def timestamp_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Issue timestamp must include a timezone")
        return value
