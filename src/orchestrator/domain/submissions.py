"""Immutable creation receipt, independent of workflow execution/verdict."""

from datetime import datetime
from dataclasses import dataclass, field
from typing import Literal
from hashlib import sha256

from orchestrator.core.security import redact_text

from pydantic import Field, UUID4

from orchestrator.domain.snapshot_handoff import ImmutableDomainModel
from orchestrator.domain.models import WorkflowRun, WorkflowStep


def measurement_request_sha256(text: str) -> str:
    """Fingerprint the effective public request, independently of role, policy and slot."""
    return sha256(redact_text(text.strip()).encode("utf-8")).hexdigest()


class RunSubmissionRecord(ImmutableDomainModel):
    internal_key: UUID4 = Field(alias="internalKey")
    request_sha256: str = Field(alias="requestSha256", pattern=r"^[0-9a-f]{64}$")
    run_id: UUID4 = Field(alias="runId")
    first_step_id: UUID4 = Field(alias="firstStepId")
    status: Literal["CREATED"] = "CREATED"
    created_at: datetime = Field(alias="createdAt")
    response_json: str = Field(alias="responseJson", repr=False)


class SubmissionKeyConflict(ValueError):
    def __init__(self):
        super().__init__("IDEMPOTENCY_KEY_CONFLICT")


@dataclass(frozen=True)
class SubmissionView:
    """One SQLite read snapshot; never expose the Run's request_text in repr."""

    receipt: RunSubmissionRecord = field(repr=False)
    run: WorkflowRun = field(repr=False)
    first_step: WorkflowStep = field(repr=False)
    uncertain_send: bool
