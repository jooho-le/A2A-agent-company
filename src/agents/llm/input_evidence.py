"""Hash-only observations at the provider boundary; never persist prompt or reasoning text."""
from hashlib import sha256
import json

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator
from orchestrator.a2a.requests import A2AWorkflowMetadata


# Closed keys the Host itself adds to a task input for a follow-up model call in the same step.
HOST_FOLLOW_UP_KEYS = ('testRunFeedback', 'semanticReviewFeedback')


def json_sha256(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


class ModelInputEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    sequence: int = Field(strict=True, ge=1)
    requiredTool: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    metadataSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    taskInputSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    initialInputSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    systemSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    inputItemsSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    # Codex review 2026-11-18: fingerprints of the tool definitions and response schema exactly
    # as sent (workspace pin and Host test-ID catalog included). Optional for older records.
    toolsSha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    outputSchemaSha256: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')
    # Codex review 2026-11-18: taskInputSha256/initialInputSha256 always describe the ORIGINAL task
    # input. A Host follow-up in the same step (in-step test fix, one semantic rewrite) is recorded
    # here by its closed key and hash only, so evaluation can tell it apart from an input change.
    hostFollowUpSha256: dict[str, str] | None = None

    @field_validator('hostFollowUpSha256')
    @classmethod
    def _closed_follow_ups(cls, value):
        if value is not None and (not value or not set(value) <= set(HOST_FOLLOW_UP_KEYS) or any(
                type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest) for digest in value.values())):
            raise ValueError('HOST_FOLLOW_UP_INVALID')
        return value


def tools_sha256(tools):
    return json_sha256([{'name': t.name, 'description': t.description, 'inputSchema': t.input_schema.to_dict(),
                         'outputSchema': t.output_schema.to_dict()} for t in tools])


def output_schema_sha256(output):
    return json_sha256({'name': output.name, 'schema': output.schema.to_dict()})


def observe_input(sequence, request):
    """Observe the exact first user item retained in every continuation request."""
    items = json.loads(request.input_items_json)
    initial = json.loads(items[0]['content'])
    metadata = initial.get('metadata') or (initial.get('trusted') or {}).get('metadata')
    normalized = A2AWorkflowMetadata.model_validate(metadata).to_a2a_json()
    task = initial['taskInput']
    follow_ups = {key: json_sha256(task[key]) for key in HOST_FOLLOW_UP_KEYS
                  if isinstance(task, dict) and key in task}
    original = {k: v for k, v in task.items() if k not in follow_ups} if follow_ups else task
    return ModelInputEvidence(
        sequence=sequence, requiredTool=request.required_tool, metadataSha256=json_sha256(normalized),
        taskInputSha256=json_sha256(original),
        initialInputSha256=json_sha256({**initial, 'taskInput': original} if follow_ups else initial),
        hostFollowUpSha256=follow_ups or None,
        systemSha256=sha256(request.system_prompt.encode('utf-8')).hexdigest(),
        inputItemsSha256=sha256(request.input_items_json.encode('utf-8')).hexdigest(),
        toolsSha256=tools_sha256(request.tools), outputSchemaSha256=output_schema_sha256(request.output),
    ).model_dump()
