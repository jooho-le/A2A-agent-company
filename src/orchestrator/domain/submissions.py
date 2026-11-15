"""Immutable creation receipt, independent of workflow execution/verdict."""

from datetime import datetime
from typing import Literal

from pydantic import Field, UUID4

from orchestrator.domain.snapshot_handoff import ImmutableDomainModel


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
