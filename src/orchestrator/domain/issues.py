"""Append-only, traceable issues that can be handed back to Developer."""

from datetime import datetime
from typing import Literal
from uuid import uuid4

from pydantic import ConfigDict, Field, UUID4, field_validator, model_validator

from orchestrator.domain.models import DomainModel, utc_now
from orchestrator.domain.states import AgentRole


class ProtectedReceipt(DomainModel):
    """Host measurement available before the validator produced an A2A report."""
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)
    receipt_id: UUID4 = Field(alias="receiptId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    test_id: str = Field(min_length=1, max_length=1024, alias="testId")
    test_version: str = Field(pattern=r"^(?:practice|inputs):[0-9a-f]{64}$", alias="testVersion")
    observations_json: str = Field(max_length=65536, alias="observationsJson")

    @field_validator("observations_json")
    @classmethod
    def measured_failure(cls, value):
        import json
        from orchestrator.domain.validation_feedback import observation_json
        rows = json.loads(value)
        canonical = observation_json(rows)
        if not any(row['kind'] == 'assertion' and row['status'] == 'FAIL' for row in rows):
            raise ValueError("A protected product failure requires an observed assertion failure")
        return canonical


class IssueRecord(DomainModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True
    )

    issue_id: UUID4 = Field(default_factory=uuid4, alias="issueId")
    run_id: UUID4 = Field(alias="runId")
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    code_version: int = Field(ge=1, alias="codeVersion")
    source_artifact_id: UUID4 = Field(alias="sourceArtifactId")
    report_artifact_id: UUID4 | None = Field(default=None, alias="reportArtifactId")
    protected_receipt: ProtectedReceipt | None = Field(default=None, alias="protectedReceipt", exclude_if=lambda v: v is None)
    requirement_ids: list[UUID4] = Field(min_length=1, alias="requirementIds")
    reporter: AgentRole
    category: str = Field(min_length=1)
    reference_id: str = Field(min_length=1, alias="referenceId")
    severity: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    consecutive_repeat_count: int = Field(ge=0, alias="consecutiveRepeatCount")
    stop_repeat_count: int | None = Field(default=None, ge=0, alias="stopRepeatCount", exclude_if=lambda v: v is None)
    measured_progress: Literal["PARTIAL_IMPROVEMENT", "NO_PROGRESS", "CHANGED", "NOT_COMPARABLE"] | None = Field(
        default=None, alias="measuredProgress", exclude_if=lambda v: v is None)

    expected_result: str | None = Field(default=None, alias="expectedResult")
    actual_result: str | None = Field(default=None, alias="actualResult")
    evidence_refs: tuple[str, ...] = Field(default=(), alias="evidenceRefs")
    normalized_location: str = Field(default="unknown", min_length=1, alias="normalizedLocation")
    cause_status: Literal["SUSPECTED", "CONFIRMED", "UNKNOWN"] = Field(
        default="UNKNOWN", alias="causeStatus"
    )
    suspected_cause: str | None = Field(default=None, alias="suspectedCause")
    fixed_by: AgentRole | None = Field(default=None, alias="fixedBy")
    fix_workflow_step_id: UUID4 | None = Field(default=None, alias="fixWorkflowStepId")
    previous_code_version: int | None = Field(default=None, ge=1, alias="previousCodeVersion")
    new_code_version: int | None = Field(default=None, ge=1, alias="newCodeVersion")
    revalidation_result: Literal["PASS", "FAIL", "UNVERIFIED"] | None = Field(
        default=None, alias="revalidationResult"
    )
    created_at: datetime = Field(default_factory=utc_now, alias="createdAt")

    @model_validator(mode="after")
    def receipt_origin(self):
        if self.protected_receipt is not None and (self.report_artifact_id is not None
                or len(self.requirement_ids) != 1 or self.reporter not in (AgentRole.QA, AgentRole.SECURITY)
                or self.category != ("QA_ASSERTION_FAILURE" if self.reporter == AgentRole.QA else "SECURITY_REQUIREMENT_FAILURE")
                or not self.evidence_refs):
            raise ValueError("Invalid independent protected receipt")
        return self

    @property
    def repeats_for_stop(self) -> int:
        return self.consecutive_repeat_count if self.stop_repeat_count is None else self.stop_repeat_count

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
