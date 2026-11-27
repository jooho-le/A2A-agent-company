"""Creation lookup is separate from execution status and product ownership."""

from datetime import datetime
from typing import Literal

from pydantic import Field, UUID4

from orchestrator.api.schemas.runs import APIModel, RunStatusResponse, RunSubmissionResponse


class SubmissionRecoveryResponse(APIModel):
    action: Literal["NONE", "OBSERVE", "RECOVER", "REVIEW", "RECONCILE"]
    reason_code: str = Field(alias="reasonCode")
    automatic_retry_allowed: Literal[False] = Field(default=False, alias="automaticRetryAllowed")


class SubmissionLookupResponse(APIModel):
    internal_key: UUID4 = Field(alias="internalKey")
    status: Literal["CREATED"]
    request_sha256: str = Field(alias="requestSha256", pattern=r"^[0-9a-f]{64}$")
    run_id: UUID4 = Field(alias="runId")
    created_at: datetime = Field(alias="createdAt")
    original_response: RunSubmissionResponse = Field(alias="originalResponse")
    run: RunStatusResponse
    recovery: SubmissionRecoveryResponse
