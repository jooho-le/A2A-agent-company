"""Hash-only observations at the provider boundary; never persist prompt or reasoning text."""
from hashlib import sha256
import json

from pydantic import BaseModel, ConfigDict, Field
from orchestrator.a2a.requests import A2AWorkflowMetadata


def json_sha256(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'),
                             allow_nan=False).encode('utf-8')).hexdigest()


class ModelInputEvidence(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, hide_input_in_errors=True)
    sequence: int = Field(strict=True, ge=1)
    metadataSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    taskInputSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    initialInputSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    systemSha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    inputItemsSha256: str = Field(pattern=r'^[0-9a-f]{64}$')


def observe_input(sequence, request):
    """Observe the exact first user item retained in every continuation request."""
    items = json.loads(request.input_items_json)
    initial = json.loads(items[0]['content'])
    metadata = initial.get('metadata') or (initial.get('trusted') or {}).get('metadata')
    normalized = A2AWorkflowMetadata.model_validate(metadata).to_a2a_json()
    return ModelInputEvidence(
        sequence=sequence, metadataSha256=json_sha256(normalized),
        taskInputSha256=json_sha256(initial['taskInput']), initialInputSha256=json_sha256(initial),
        systemSha256=sha256(request.system_prompt.encode('utf-8')).hexdigest(),
        inputItemsSha256=sha256(request.input_items_json.encode('utf-8')).hexdigest(),
    ).model_dump()
