from contextlib import contextmanager

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import UUID4
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import get_db
from models import Project, ProjectRun, User
from project_scenarios import ScenarioRegistryUnavailable, is_supported_project_scenario
from project_schemas import (
    ProjectCreateRequest,
    ProjectDetailResponse,
    ProjectListResponse,
    ProjectResponse,
    ProjectRunResponse,
    ProjectUpdateRequest,
)
from routers.auth import read_current_user


router = APIRouter(prefix="/api/projects", tags=["projects"])


@contextmanager
def project_storage(db):
    try:
        yield
    except SQLAlchemyError as exc:
        db.rollback()
        # 미적용 스키마·DB 장애의 SQL/경로/파라미터를 공개하지 않습니다.
        raise HTTPException(status_code=503, detail="프로젝트 저장소를 사용할 수 없습니다.") from exc


def owned_project(db, project_id, user_id):
    project = db.scalar(select(Project).where(
        Project.project_id == str(project_id), Project.owner_user_id == user_id,
    ))
    if project is None:
        raise HTTPException(status_code=404, detail="프로젝트를 찾을 수 없습니다.")
    return project


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    payload: ProjectCreateRequest,
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    try:
        supported = is_supported_project_scenario(payload.scenario_id)
    except ScenarioRegistryUnavailable as exc:
        raise HTTPException(status_code=503, detail="시나리오 등록 정보를 확인할 수 없습니다.") from exc
    if not supported:
        raise HTTPException(status_code=422, detail="등록된 MVP 회원가입 시나리오만 사용할 수 있습니다.")
    with project_storage(db):
        project = Project(owner_user_id=current_user.id, name=payload.name, scenario_id=str(payload.scenario_id))
        db.add(project)
        db.commit()
        db.refresh(project)
        return ProjectResponse.model_validate(project)


@router.get("", response_model=ProjectListResponse)
def list_projects(current_user: User = Depends(read_current_user), db: Session = Depends(get_db)):
    with project_storage(db):
        projects = db.scalars(select(Project).where(Project.owner_user_id == current_user.id)
                              .order_by(Project.created_at.desc(), Project.project_id.desc())).all()
        return ProjectListResponse(projects=[ProjectResponse.model_validate(project) for project in projects])


@router.get("/{projectId}", response_model=ProjectDetailResponse)
def get_project(projectId: UUID4, current_user: User = Depends(read_current_user), db: Session = Depends(get_db)):
    with project_storage(db):
        project = owned_project(db, projectId, current_user.id)
        runs = db.scalars(select(ProjectRun).where(ProjectRun.project_id == project.project_id)
                         .order_by(ProjectRun.linked_at.desc(), ProjectRun.id.desc())).all()
        return ProjectDetailResponse(**ProjectResponse.model_validate(project).model_dump(),
                                     runs=[ProjectRunResponse.model_validate(run) for run in runs])


@router.patch("/{projectId}", response_model=ProjectResponse)
def update_project(
    projectId: UUID4,
    payload: ProjectUpdateRequest,
    current_user: User = Depends(read_current_user),
    db: Session = Depends(get_db),
):
    with project_storage(db):
        project = owned_project(db, projectId, current_user.id)
        project.name = payload.name
        db.commit()
        db.refresh(project)
        return ProjectResponse.model_validate(project)
