"""Project delivery receipts from trusted admission storage, never LLM claims."""

import hashlib
import json
from typing import Annotated, Literal
from uuid import UUID

from a2a.types import SendMessageRequest, Task
from google.protobuf.json_format import MessageToDict
from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, UUID4, field_validator

from orchestrator.core.security import redact_data
from orchestrator.domain.contract_validation import require_json_integer
from orchestrator.domain.json_fingerprint import json_fingerprint


class DeliveryReceiptError(ValueError):
    def __init__(self):
        super().__init__("DELIVERY_RECEIPT_INVALID")


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_version: Annotated[Literal[1], BeforeValidator(require_json_integer)] = Field(alias="schemaVersion")
    message_id: str = Field(alias="messageId")
    receiver_role: Literal["PLANNER", "DEVELOPER", "QA", "SECURITY", "SINGLE_AGENT"] = Field(alias="receiverRole")
    task_id: str = Field(alias="taskId", repr=False)
    context_id: str = Field(alias="contextId", repr=False)
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    scenario_id: UUID4 = Field(alias="scenarioId")
    attempt: int = Field(ge=0, strict=True)
    code_version: int | None = Field(default=None, alias="codeVersion", ge=1, strict=True)
    request_sha256: str = Field(alias="requestSha256", pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(alias="payloadSha256", pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(alias="metadataSha256", pattern=r"^[0-9a-f]{64}$")
    received_at: AwareDatetime = Field(alias="receivedAt")

    @field_validator("message_id")
    @classmethod
    def message_uuid(cls, value):
        if UUID(value).version != 4:
            raise ValueError("Message ID must be UUIDv4")
        return value

    @field_validator("task_id", "context_id")
    @classmethod
    def opaque_identity(cls, value):
        if not value.strip() or len(value.encode("utf-8")) > 4096:
            raise ValueError("A nonblank opaque ID is required")
        return value


def request_sha256(request):
    """Keep the existing admission-store fingerprint algorithm unchanged."""
    if type(request) is not SendMessageRequest:
        raise DeliveryReceiptError()
    data = json.dumps(redact_data(MessageToDict(request)), ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > 1_048_576:
        raise DeliveryReceiptError()
    return hashlib.sha256(data).hexdigest()


def build_delivery_receipt(request, *, task_id, context_id, receiver_role, received_at):
    invalid = False
    try:
        if type(request) is not SendMessageRequest:
            raise ValueError
        metadata = MessageToDict(request.metadata)
        message = request.message
        if (not message.parts or any(part.WhichOneof("content") != "data"
                or part.media_type != "application/json" for part in message.parts)
                or message.task_id and message.task_id != task_id
                or message.context_id and message.context_id != context_id):
            raise ValueError
        receipt = DeliveryReceipt(
            schemaVersion=1, messageId=message.message_id, receiverRole=receiver_role,
            taskId=task_id, contextId=context_id, runId=metadata["runId"],
            workflowStepId=metadata["workflowStepId"], scenarioId=metadata["scenarioId"],
            attempt=require_json_integer(metadata["attempt"]),
            codeVersion=None if "codeVersion" not in metadata else require_json_integer(metadata["codeVersion"]),
            requestSha256=request_sha256(request),
            payloadSha256=json_fingerprint(redact_data([MessageToDict(part) for part in message.parts])),
            metadataSha256=json_fingerprint(redact_data(metadata)), receivedAt=received_at)
    except Exception:
        invalid = True
    if invalid:
        raise DeliveryReceiptError() from None
    return receipt


def verify_delivery(request, task, receipt, *, receiver_role):
    """Compare sender request and observed Task with an authenticated receipt.

    No receipt means UNVERIFIED. Hashes identify accepted sanitized JSON, not
    authenticated signatures, LLM input fidelity, execution, or product success.
    Return fixed field names only; never echo mismatched private values.
    """
    if receipt is None:
        return {"status": "UNVERIFIED", "differences": ["RECEIPT_NOT_OBSERVED"]}
    invalid = False
    try:
        if type(task) is not Task or type(receipt) is not DeliveryReceipt:
            raise ValueError
        receipt = DeliveryReceipt.model_validate(receipt.model_dump(mode="python", by_alias=True))
        expected = build_delivery_receipt(request, task_id=task.id, context_id=task.context_id,
            receiver_role=receiver_role, received_at=receipt.received_at)
        compared = ("message_id", "receiver_role", "task_id", "context_id", "run_id",
                    "workflow_step_id", "scenario_id", "attempt", "code_version",
                    "request_sha256", "payload_sha256", "metadata_sha256")
        differences = [DeliveryReceipt.model_fields[key].alias for key in compared
                       if getattr(receipt, key) != getattr(expected, key)]
        result = {"status": "FAIL" if differences else "PASS", "differences": differences,
            "hashes": {name: {"sent": getattr(expected, name), "received": getattr(receipt, name)}
                       for name in ("request_sha256", "payload_sha256", "metadata_sha256")}}
    except Exception:
        invalid = True
    if invalid:
        return {"status": "FAIL", "differences": ["RECEIPT_CONTRACT_INVALID"]}
    return result
