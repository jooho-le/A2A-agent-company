"""Authenticated ZIP download of one registered Source version, not live files."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import UUID4
from starlette.responses import StreamingResponse

from orchestrator.api.dependencies import get_artifact_store, get_workflow_repository
from orchestrator.api.host_access import require_host_service_mode
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.artifacts.source_zip import build_source_zip
from orchestrator.infrastructure import SQLiteWorkflowRepository


router = APIRouter(prefix="/runs", tags=["artifacts"], dependencies=[Depends(require_host_service_mode)])


@router.get("/{run_id}/artifacts/{artifact_id}/download", response_class=StreamingResponse,
    responses={200: {"content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
        "headers": {
            "X-Snapshot-Sha256": {"schema": {"type": "string"}, "description": "Original Source tar SHA-256"},
            "X-Archive-Sha256": {"schema": {"type": "string"}, "description": "Downloaded ZIP SHA-256"},
            "X-Source-Artifact-Id": {"schema": {"type": "string", "format": "uuid"}},
            "X-Code-Version": {"schema": {"type": "integer"}},
        }},
        404: {"description": "Registered Source or preserved Snapshot not found in this Run"},
        409: {"description": "Immutable Source integrity or safe content policy rejected"},
        413: {"description": "Approved Source or archive size limit exceeded"},
        503: {"description": "Source storage or rejection recording unavailable"}})
def download_source(run_id: UUID4, artifact_id: UUID4, request: Request,
    store: Annotated[ArtifactStore, Depends(get_artifact_store)],
    repository: Annotated[SQLiteWorkflowRepository, Depends(get_workflow_repository)]):
    if request.query_params:
        raise HTTPException(status_code=422, detail="SOURCE_DOWNLOAD_INPUT_INVALID",
                            headers={"Cache-Control": "no-store"})
    try:
        result = build_source_zip(store, run_id, artifact_id)
    except ArtifactAccessError as error:
        if error.code in {ArtifactErrorCode.INTEGRITY, ArtifactErrorCode.PATH_DENIED, ArtifactErrorCode.TOO_LARGE}:
            try:
                repository.record_source_download_rejection(run_id, artifact_id)
            except Exception:
                raise HTTPException(status_code=503, detail="SOURCE_DOWNLOAD_UNAVAILABLE",
                                    headers={"Cache-Control": "no-store"}) from None
        if error.code in {ArtifactErrorCode.NOT_FOUND, ArtifactErrorCode.INVALID, ArtifactErrorCode.DENIED}:
            code, detail = 404, "SOURCE_NOT_FOUND"
        elif error.code == ArtifactErrorCode.TOO_LARGE:
            code, detail = 413, "SOURCE_DOWNLOAD_TOO_LARGE"
        elif error.code in {ArtifactErrorCode.INTEGRITY, ArtifactErrorCode.PATH_DENIED}:
            code, detail = 409, "SOURCE_DOWNLOAD_REJECTED"
        else:
            code, detail = 503, "SOURCE_DOWNLOAD_UNAVAILABLE"
        raise HTTPException(status_code=code, detail=detail, headers={"Cache-Control": "no-store"}) from None

    def chunks():
        for offset in range(0, len(result.content), 64 * 1024):
            yield result.content[offset:offset + 64 * 1024]

    return StreamingResponse(chunks(), media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{result.filename}"',
        "Content-Length": str(len(result.content)), "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff", "X-Snapshot-Sha256": result.snapshot_sha256,
        "X-Archive-Sha256": result.archive_sha256, "X-Source-Artifact-Id": str(artifact_id),
        "X-Code-Version": str(result.code_version),
    })
