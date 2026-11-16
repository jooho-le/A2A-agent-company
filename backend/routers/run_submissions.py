"""제품 JWT → 소유권·멱등 접수 원장 → 인증된 Host. 생성 POST는 자동 재전송하지 않습니다."""

import json
from hashlib import sha256
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, UUID4
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from host_reads import HOST_KEY, HostReadClient, HostRunNotFound, HostRejected, HostUnavailable, get_host_read_client
from models import Project, ProjectRun, RunSubmission, User
from project_scenarios import is_supported_project_scenario
from routers.auth import read_current_user
from routers.projects import owned_project, project_storage

router = APIRouter(prefix="/api/v1", tags=["office-work"])


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def unavailable():
    return HTTPException(503, "실행 서버가 준비되지 않았습니다. 서버 연결과 승인 설정을 확인해 주세요.")


def supported(scenario_id):
    if not is_supported_project_scenario(scenario_id):
        raise HTTPException(422, "현재 등록된 회원가입 시나리오만 실행할 수 있습니다.")


def approval(client, scenario_id):
    supported(scenario_id)
    result = client.exchange("GET", "/api/v1/host/approved-configuration?" +
                             urlencode({"scenarioId": str(scenario_id)}))
    configuration = result.get("configuration")
    if (result.get("scenarioId") != str(scenario_id) or not isinstance(configuration, dict)
            or configuration.get("architecture") != "MULTI_AGENT"
            or digest(configuration) != result.get("configurationId")):
        raise HostUnavailable()
    return result


@router.get("/scenarios")
def scenarios(response: Response, user: User = Depends(read_current_user),
              client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    try:
        result = client.exchange("GET", "/api/v1/scenarios")
        rows = result["scenarios"]
        if not isinstance(rows, list):
            raise ValueError()
        return {"scenarios": [row for row in rows if is_supported_project_scenario(row["scenarioId"])]}
    except Exception:
        raise unavailable() from None


@router.get("/host/approved-configuration")
def approved_configuration(response: Response, scenarioId: UUID4 = Query(),
                           user: User = Depends(read_current_user),
                           client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    try:
        return approval(client, scenarioId)
    except (HostUnavailable, HostRunNotFound, HostRejected):
        raise unavailable() from None


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    projectName: str = Field(min_length=1, max_length=60)
    projectId: UUID4 | None = None
    scenarioId: UUID4
    configurationId: str = Field(pattern=r"^[0-9a-f]{64}$")
    requestText: str = Field(min_length=1, max_length=20000)


def existing(db, user_id, key):
    return db.scalar(select(RunSubmission).where(RunSubmission.owner_user_id == user_id,
                                                 RunSubmission.client_key == str(key)))


def receipt(row):
    data = json.loads(row.response_json) if row.response_json else {}
    return {**data, "submissionStatus": row.state, "requestId": row.client_key, "projectId": row.project_id}


def link_result(db, row, data):
    run = data.get("run", {})
    original = json.loads(row.host_request_json)
    try:
        run_id = str(UUID(run["runId"], version=4))
        if (run_id != run["runId"] or run.get("scenarioId") != original["scenarioId"]
                or data.get("dispatchStatus") not in ("SCHEDULED", "NOT_CONFIGURED")):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HostUnavailable() from None
    link = db.scalar(select(ProjectRun).where(ProjectRun.host_key == HOST_KEY, ProjectRun.run_id == run_id))
    if link and link.project_id != row.project_id:
        raise HostUnavailable()
    if not link:
        db.add(ProjectRun(project_id=row.project_id, host_key=HOST_KEY, run_id=run_id,
                          configuration_id=row.configuration_id))
    row.response_json = canonical(data)
    row.state = "CREATED"
    db.commit()
    return receipt(row)


@router.post("/runs")
def submit(payload: RunRequest, request: Request, response: Response,
           idempotency_key: UUID4 = Header(alias="Idempotency-Key"),
           user: User = Depends(read_current_user), db: Session = Depends(get_db),
           client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    if len(request.headers.getlist("idempotency-key")) != 1:
        raise HTTPException(422, "요청 식별자는 한 개여야 합니다.")
    request_hash = digest(payload.model_dump(mode="json"))
    with project_storage(db):
        row = existing(db, user.id, idempotency_key)
        if row:
            if row.request_sha256 != request_hash:
                raise HTTPException(409, "같은 요청 식별자로 다른 업무를 접수할 수 없습니다.")
            return receipt(row)
        # 타 사용자 프로젝트는 Host I/O 전에 404입니다.
        project = owned_project(db, payload.projectId, user.id) if payload.projectId else None
        if project and project.scenario_id != str(payload.scenarioId):
            raise HTTPException(422, "프로젝트 시나리오와 요청이 일치하지 않습니다.")
        try:
            approved = approval(client, payload.scenarioId)
        except (HostUnavailable, HostRunNotFound, HostRejected):
            raise unavailable() from None
        if payload.configurationId != approved["configurationId"]:
            raise HTTPException(409, "승인 설정이 변경되었습니다. 실행 설정을 다시 확인해 주세요.")
        if project is None:
            project = Project(owner_user_id=user.id, name=payload.projectName, scenario_id=str(payload.scenarioId))
            db.add(project)
            db.flush()
        host_body = {"scenarioId": str(payload.scenarioId), "requestText": payload.requestText,
                     "configuration": approved["configuration"]}
        row = RunSubmission(internal_key=str(uuid4()), owner_user_id=user.id, client_key=str(idempotency_key),
            project_id=project.project_id, request_sha256=request_hash, host_request_sha256=digest(host_body),
            host_request_json=canonical(host_body), configuration_id=approved["configurationId"], state="PENDING")
        db.add(row)
        try:
            db.commit()  # 외부 POST보다 먼저 원문·내부 키·프로젝트를 영속화합니다.
        except IntegrityError:
            db.rollback()
            row = existing(db, user.id, idempotency_key)
            if row is None:
                raise unavailable() from None
            if row.request_sha256 != request_hash:
                raise HTTPException(409, "같은 요청 식별자로 다른 업무를 접수할 수 없습니다.")
            return receipt(row)
        try:
            data = client.exchange("POST", "/api/v1/runs", body=host_body, key=row.internal_key)
            result = link_result(db, row, data)
            response.status_code = 201
            return result
        except HostRejected as error:
            row.state = "REJECTED" if error.status in (400, 404, 405, 413, 415, 422, 429) else "UNKNOWN"
        except (HostUnavailable, HostRunNotFound):
            row.state = "UNKNOWN"
        db.commit()
        response.status_code = 202
        return receipt(row)


@router.get("/submissions/{requestId}")
def lookup(requestId: UUID4, response: Response, user: User = Depends(read_current_user),
           db: Session = Depends(get_db), client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    with project_storage(db):
        row = existing(db, user.id, requestId)
        if row is None:
            raise HTTPException(404, "접수 기록을 찾을 수 없습니다.")
        owned_project(db, row.project_id, user.id)
        if row.state in ("PENDING", "UNKNOWN"):
            try:
                data = client.exchange("GET", f"/api/v1/submissions/{row.internal_key}")
                if (data.get("status") != "CREATED" or data.get("internalKey") != row.internal_key
                        or data.get("requestSha256") != row.host_request_sha256):
                    raise HostUnavailable()
                return link_result(db, row, data["originalResponse"])
            except (HostUnavailable, HostRunNotFound, HostRejected, KeyError):
                # 404·503은 미생성을 보장하지 않습니다. POST를 재전송하지 않습니다.
                row.state = "UNKNOWN"
                db.commit()
        return receipt(row)
