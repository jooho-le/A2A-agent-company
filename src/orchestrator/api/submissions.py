"""Validate the internal key and fingerprint the original JSON, before redaction."""

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Annotated
from uuid import UUID

from fastapi import Header, HTTPException, Request

from orchestrator.api.schemas.runs import RunSubmissionResponse
from orchestrator.domain.submissions import SubmissionKeyConflict


@dataclass(frozen=True)
class SubmissionIdentity:
    internal_key: UUID
    request_sha256: str


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


async def get_submission_identity(request: Request,
    keys: Annotated[list[str] | None, Header(alias="Idempotency-Key")] = None):
    if not keys:
        if request.app.state.require_idempotency_key:
            raise HTTPException(status_code=422, detail="IDEMPOTENCY_KEY_REQUIRED")
        return None
    try:
        if len(keys) != 1:
            raise ValueError
        key = UUID(keys[0])
        if key.version != 4 or str(key) != keys[0].lower():
            raise ValueError
        # Hash only; the original text/credentials are never stored in a receipt.
        raw = json.loads(await request.body(), object_pairs_hook=_unique_object)
        canonical = json.dumps(raw, ensure_ascii=False, sort_keys=True,
                               separators=(",", ":"), allow_nan=False).encode("utf-8")
    except Exception:
        raise HTTPException(status_code=422, detail="IDEMPOTENCY_REQUEST_INVALID") from None
    return SubmissionIdentity(key, sha256(canonical).hexdigest())


def replay_submission(record, identity, request, response):
    if record.request_sha256 != identity.request_sha256:
        raise SubmissionKeyConflict()
    response.status_code = 200
    response.headers["Idempotency-Replayed"] = "true"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Location"] = str(request.url_for("get_run", run_id=str(record.run_id)))
    return RunSubmissionResponse.model_validate_json(record.response_json)
