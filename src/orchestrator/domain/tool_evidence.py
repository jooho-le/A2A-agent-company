"""Structured MCP execution provenance; retries belong to one logical call."""

from enum import Enum

from pydantic import Field, UUID4, field_validator, model_validator

from orchestrator.domain.snapshot_handoff import (
    ExecutionManifest,
    ImmutableDomainModel,
    _validate_artifact_uri,
)


class ToolExecutionOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class ToolAttemptEvidence(ImmutableDomainModel):
    attempt: int = Field(ge=0, le=2, strict=True)
    outcome: ToolExecutionOutcome
    evidence_ref: str = Field(alias="evidenceRef", min_length=1)
    error_kind: str | None = Field(default=None, alias="errorKind")
    retry_safe: bool = Field(default=False, alias="retrySafe", strict=True)
    duration_ms: int = Field(default=0, alias="durationMs", ge=0, strict=True)

    @field_validator("attempt", "duration_ms", mode="before")
    @classmethod
    def numeric_values_are_integral(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value).is_integer():
            raise ValueError("Tool attempt and duration must be integers")
        return int(value)

    @field_validator("evidence_ref")
    @classmethod
    def evidence_is_a_nonlocal_reference(cls, value: str) -> str:
        return _validate_artifact_uri(value.strip())

    @property
    def can_retry(self) -> bool:
        return self.outcome == ToolExecutionOutcome.UNVERIFIED and (
            self.error_kind in {"PROCESS_STARTUP_FAILURE", "RESOURCE_BUSY"}
            or self.retry_safe and self.error_kind in {
                "TIMEOUT", "TOOL_TIMEOUT", "MCP_TRANSPORT_INTERRUPTED"
            }
        )


class ToolExecutionEvidence(ImmutableDomainModel):
    tool_name: str = Field(alias="toolName", min_length=1)
    execution_id: UUID4 = Field(alias="executionId")
    execution_manifest: ExecutionManifest = Field(alias="executionManifest")
    evidence_ref: str = Field(alias="evidenceRef", min_length=1)
    attempts: tuple[ToolAttemptEvidence, ...] = Field(min_length=1, max_length=3)

    @field_validator("evidence_ref")
    @classmethod
    def evidence_is_a_nonlocal_reference(cls, value: str) -> str:
        return ToolAttemptEvidence.evidence_is_a_nonlocal_reference(value)

    @model_validator(mode="after")
    def validate_attempt_chain(self) -> "ToolExecutionEvidence":
        if [item.attempt for item in self.attempts] != list(range(len(self.attempts))):
            raise ValueError("Tool retry evidence must include the initial attempt and every retry")
        if any(not item.can_retry for item in self.attempts[:-1]):
            raise ValueError("Tool retries must follow a safe, retryable infrastructure failure")
        return self

    @property
    def outcome(self) -> ToolExecutionOutcome:
        return self.attempts[-1].outcome

    @property
    def retries_used(self) -> int:
        return len(self.attempts) - 1

    @property
    def retries_exhausted(self) -> bool:
        return self.outcome == ToolExecutionOutcome.UNVERIFIED and self.retries_used == 2
