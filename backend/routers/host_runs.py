"""제품 JWT와 프로젝트 소유권을 확인하는 읽기 전용 Host 중계."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
from host_reads import HOST_KEY, HostReadClient, HostRunNotFound, HostUnavailable, get_host_read_client
from models import ProjectRun, User
from routers.auth import read_current_user
from routers.projects import owned_project, project_storage


router = APIRouter(prefix="/api/v1/runs", tags=["host-reads"])


def owned_run(
    runId: UUID,
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    with project_storage(db):
        link = db.scalar(select(ProjectRun).where(
            ProjectRun.host_key == HOST_KEY, ProjectRun.run_id == str(runId),
        ))
        if link is None:
            raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
        # 프로젝트 API와 동일한 404 정책을 사용합니다. 여기까지 Host I/O는 없습니다.
        owned_project(db, link.project_id, current_user.id)
    return runId


def read_host(request, client, run_id, resource="", *, limit=100, offset=0):
    allowed = {"limit", "offset"} if resource == "events" else set()
    if set(request.query_params) - allowed or any(len(request.query_params.getlist(key)) != 1 for key in request.query_params):
        raise HTTPException(status_code=422, detail="지원하지 않는 조회 파라미터입니다.")
    try:
        return client.get(run_id, resource, limit=limit, offset=offset)
    except HostRunNotFound:
        raise HTTPException(status_code=404, detail="Run을 찾을 수 없습니다.") from None
    except HostUnavailable:
        raise HTTPException(status_code=503, detail="Host 조회를 사용할 수 없습니다.") from None


@router.get("/{runId}")
def get_run(request: Request, run_id: UUID = Depends(owned_run), client: HostReadClient = Depends(get_host_read_client)):
    return read_host(request, client, run_id)


@router.get("/{runId}/steps")
def get_steps(request: Request, run_id: UUID = Depends(owned_run), client: HostReadClient = Depends(get_host_read_client)):
    return read_host(request, client, run_id, "steps")


@router.get("/{runId}/events")
def get_events(
    request: Request,
    run_id: UUID = Depends(owned_run),
    client: HostReadClient = Depends(get_host_read_client),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    return read_host(request, client, run_id, "events", limit=limit, offset=offset)


@router.get("/{runId}/issues")
def get_issues(request: Request, run_id: UUID = Depends(owned_run), client: HostReadClient = Depends(get_host_read_client)):
    return read_host(request, client, run_id, "issues")


@router.get("/{runId}/artifacts")
def get_artifacts(request: Request, run_id: UUID = Depends(owned_run), client: HostReadClient = Depends(get_host_read_client)):
    return read_host(request, client, run_id, "artifacts")
