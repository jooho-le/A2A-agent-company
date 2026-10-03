"""A2A SendMessageRequest builders for the project contract."""

import json
from collections.abc import Mapping
from typing import Any
from uuid import UUID, uuid4

from google.protobuf.json_format import ParseDict, ParseError
from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator
from a2a.types import SendMessageRequest

from orchestrator.core.security import redact_data
from orchestrator.domain.snapshot_handoff import SnapshotHandoff
from orchestrator.domain.states import AgentRole


class A2AProjectContractError(ValueError):
    """Raised when project metadata or a handoff payload violates its contract."""


class A2AWorkflowMetadata(BaseModel):
    """Project metadata carried at the SendMessageRequest top level."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    scenario_id: UUID4 = Field(alias="scenarioId")
    attempt: int = Field(ge=0, strict=True)
    requirement_ids: tuple[UUID4, ...] | None = Field(
        default=None, alias="requirementIds"
    )
    code_version: int | None = Field(
        default=None, ge=1, strict=True, alias="codeVersion"
    )
    project_artifact_ids: tuple[UUID4, ...] | None = Field(
        default=None, alias="projectArtifactIds"
    )

    @field_validator("requirement_ids", "project_artifact_ids")
    @classmethod
    def ids_must_be_unique(
        cls, values: tuple[UUID4, ...] | None
    ) -> tuple[UUID4, ...] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("metadata ID lists must not contain duplicates")
        return values

    def to_a2a_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)


def build_send_message_request(
    payload: Mapping[str, object],
    metadata: A2AWorkflowMetadata,
    *,
    context_id: str | None = None,
    task_id: str | None = None,
    message_id: UUID | None = None,
) -> SendMessageRequest:
    """Build an SDK request; omitting task_id asks the Agent to create a new Task."""
    if not payload:
        raise A2AProjectContractError("Agent input payload must not be empty")
    if context_id is not None and not context_id.strip():
        raise A2AProjectContractError("context_id must not be blank")
    if task_id is not None and not task_id.strip():
        raise A2AProjectContractError("task_id must not be blank")

    try:
        json_payload = redact_data(json.loads(json.dumps(dict(payload), allow_nan=False)))
    except (TypeError, ValueError) as exc:
        raise A2AProjectContractError("Agent input payload must be JSON-compatible") from exc

    message_uuid = message_id or uuid4()
    if message_uuid.version != 4:
        raise A2AProjectContractError("message_id must be UUIDv4")

    message: dict[str, object] = {
        "messageId": str(message_uuid),
        "role": "ROLE_USER",
        "parts": [
            {
                "data": json_payload,
                "mediaType": "application/json",
            }
        ],
    }
    if context_id is not None:
        message["contextId"] = context_id
    if task_id is not None:
        message["taskId"] = task_id

    wire_request = {
        "message": message,
        "configuration": {
            "acceptedOutputModes": ["application/json"],
            "returnImmediately": True,
        },
        "metadata": metadata.to_a2a_json(),
    }
    try:
        return ParseDict(wire_request, SendMessageRequest())
    except (ParseError, TypeError, ValueError) as exc:
        raise A2AProjectContractError("Could not encode SendMessageRequest") from exc


def build_snapshot_handoff_data(
    handoff: SnapshotHandoff,
    recipient: AgentRole,
    request_text: str,
) -> dict[str, object]:
    """Create the JSON body delivered to one authorized QA/Security Agent."""
    request_text = request_text.strip()
    if not request_text:
        raise A2AProjectContractError("request_text must not be blank")

    grant = next(
        (grant for grant in handoff.grants if grant.recipient == recipient),
        None,
    )
    if grant is None:
        raise A2AProjectContractError(
            f"no Snapshot read grant exists for {recipient.value}"
        )

    return {
        "request": request_text,
        "snapshot": {
            "projectArtifactId": str(handoff.project_artifact_id),
            "artifactUri": handoff.artifact_uri,
            "sourceAccess": grant.access,
            "executionManifest": handoff.execution_manifest.model_dump(
                mode="json", by_alias=True
            ),
        },
    }


def build_snapshot_handoff_request(
    handoff: SnapshotHandoff,
    recipient: AgentRole,
    request_text: str,
    *,
    workflow_step_id: UUID,
    scenario_id: UUID,
    context_id: str | None = None,
    attempt: int = 0,
    requirement_ids: tuple[UUID4, ...] | None = None,
    message_id: UUID | None = None,
) -> SendMessageRequest:
    """Build a Task request whose metadata and payload identify one frozen candidate."""
    metadata = A2AWorkflowMetadata(
        run_id=handoff.run_id,
        workflow_step_id=workflow_step_id,
        scenario_id=scenario_id,
        attempt=attempt,
        requirement_ids=requirement_ids,
        code_version=handoff.execution_manifest.code_version,
        project_artifact_ids=(handoff.project_artifact_id,),
    )
    payload = build_snapshot_handoff_data(handoff, recipient, request_text)
    return build_send_message_request(
        payload, metadata, context_id=context_id, message_id=message_id
    )
