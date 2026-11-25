"""제품 JWT → 소유권·멱등 접수 원장 → 인증된 Host. 생성 POST는 자동 재전송하지 않습니다."""

import json
import os
from hashlib import sha256
from time import sleep
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, UUID4
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database import get_db
from approved_configuration import public_approved_configuration
from host_reads import HOST_KEY, HostReadClient, HostRunNotFound, HostRejected, HostUnavailable, get_host_read_client
from host_reads import HostAuthenticationFailed, HostResponse, require_host_reads_enabled
from models import Project, ProjectRun, RetiredSubmissionKey, RunSubmission, User
from project_scenarios import ScenarioRegistryUnavailable, is_supported_project_scenario
from routers.auth import read_current_user
from routers.projects import owned_project, project_storage

router = APIRouter(prefix="/api/v1", tags=["office-work"])
SUBMISSION_LOOKUP_RETRY_DELAYS = (2, 4, 8)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return sha256(canonical(value).encode("utf-8")).hexdigest()


def unavailable(*, not_accepted=False):
    headers = {"X-Submission-Status": "NOT_ACCEPTED"} if not_accepted else None
    return HTTPException(503, "실행 서버가 준비되지 않았습니다. 서버 연결과 승인 설정을 확인해 주세요.", headers=headers)


def supported(scenario_id):
    try:
        valid = is_supported_project_scenario(scenario_id)
    except ScenarioRegistryUnavailable:
        raise HostUnavailable() from None
    if not valid:
        raise HTTPException(422, "등록된 서비스 또는 실험 시나리오를 확인해 주세요.")


def approval(client, scenario_id):
    supported(scenario_id)
    require_host_reads_enabled()
    result = client.exchange("GET", "/api/v1/host/approved-configuration?" +
                             urlencode({"scenarioId": str(scenario_id)}))
    return public_approved_configuration(result, scenario_id)


@router.get("/scenarios")
def scenarios(response: Response, user: User = Depends(read_current_user),
              client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    try:
        require_host_reads_enabled()
        result = client.exchange("GET", "/api/v1/scenarios")
        rows = result["scenarios"]
        if not isinstance(rows, list):
            raise ValueError()
        return {"scenarios": [row for row in rows if is_supported_project_scenario(row["scenarioId"])]}
    except HostAuthenticationFailed:
        raise HTTPException(502, "Host 서비스 인증에 실패했습니다.",
                            headers={"Cache-Control": "no-store"}) from None
    except Exception:
        raise unavailable() from None


@router.get("/host/approved-configuration")
def approved_configuration(request: Request, response: Response, scenarioId: UUID4 = Query(),
                           user: User = Depends(read_current_user),
                           client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    try:
        if set(request.query_params) != {"scenarioId"} or len(request.query_params.getlist("scenarioId")) != 1:
            raise HTTPException(422, "지원하지 않는 조회 파라미터입니다.")
        return approval(client, scenarioId)
    except HostAuthenticationFailed:
        raise HTTPException(502, "Host 서비스 인증에 실패했습니다.",
                            headers={"Cache-Control": "no-store"}) from None
    except (HostUnavailable, HostRunNotFound, HostRejected):
        raise HTTPException(503, "승인된 공개 실행 설정을 조회할 수 없습니다.",
                            headers={"Cache-Control": "no-store"}) from None
    except HTTPException as error:
        error.headers = {**(error.headers or {}), "Cache-Control": "no-store"}
        raise


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


def lock_submission_keys(db):
    # SQLite writer lock BEFORE checking a receipt/key. No row is updated.
    # Both submit and retire use this transaction so an in-flight POST cannot
    # create a receipt after retirement has certified that the key is unused.
    if db.get_bind().dialect.name != "sqlite":
        raise unavailable()
    db.execute(update(RetiredSubmissionKey).where(False)
               .values(client_key=RetiredSubmissionKey.client_key)
               .execution_options(synchronize_session=False))


def receipt(row, response=None):
    data = json.loads(row.response_json) if row.response_json else {}
    if response is not None and row.state == "REJECTED" and data.get("detail") == "IDEMPOTENCY_KEY_CONFLICT":
        response.status_code = 409
    return {**data, "submissionStatus": row.state, "requestId": row.client_key, "projectId": row.project_id}


def lock_submission(db, row):
    # SQLite 쓰기 잠금 후 최신 상태를 읽어 동시 GET/POST가 확정 기록을 덮어쓰지 않게 합니다.
    db.execute(update(RunSubmission).where(RunSubmission.internal_key == row.internal_key)
               .values(state=RunSubmission.state).execution_options(synchronize_session=False))
    db.refresh(row)


def link_result(db, row, data):
    lock_submission(db, row)
    if row.state == "REJECTED":
        raise HostUnavailable()
    if not isinstance(data, dict) or not isinstance(data.get("run"), dict):
        raise HostUnavailable()
    run = data.get("run", {})
    original = json.loads(row.host_request_json)
    try:
        if not isinstance(run.get("runId"), str):
            raise ValueError()
        run_id = str(UUID(run["runId"], version=4))
        if (run_id != run["runId"] or run.get("scenarioId") != original["scenarioId"]
                or data.get("dispatchStatus") not in ("SCHEDULED", "NOT_CONFIGURED")):
            raise ValueError()
        first_step = data.get("firstStep")
        if isinstance(first_step, dict) and "runId" in first_step and first_step["runId"] != run_id:
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise HostUnavailable() from None
    if row.response_json:
        saved_run = json.loads(row.response_json).get("run")
        if saved_run is not None and (not isinstance(saved_run, dict) or saved_run.get("runId") != run_id):
            raise HostUnavailable()
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


def recovery_receipt(row, data):
    """어떤 연결/상태도 쓰기 전에 접수와 Run 식별자를 대조합니다."""
    try:
        if (not isinstance(data, dict) or data.get("status") != "CREATED"
                or data.get("internalKey") != row.internal_key
                or data.get("requestSha256") != row.host_request_sha256):
            raise ValueError()
        run_id = data["runId"]
        if not isinstance(run_id, str) or str(UUID(run_id, version=4)) != run_id:
            raise ValueError()
        original = data["originalResponse"]
        if original["run"]["runId"] != run_id:
            raise ValueError()
        # 현재 Run/초기 Step 식별자도 제공됐다면 같은 Run이어야 합니다.
        if "run" in data and data["run"]["runId"] != run_id:
            raise ValueError()
        first_step = original.get("firstStep")
        if isinstance(first_step, dict) and "runId" in first_step and first_step["runId"] != run_id:
            raise ValueError()
        return original
    except (KeyError, TypeError, ValueError, AttributeError):
        raise HostUnavailable() from None


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
        lock_submission_keys(db)
        row = existing(db, user.id, idempotency_key)
        if row:
            if row.request_sha256 != request_hash:
                raise HTTPException(409, "같은 요청 식별자로 다른 업무를 접수할 수 없습니다.")
            return receipt(row, response)
        if db.get(RetiredSubmissionKey, (user.id, str(idempotency_key))) is not None:
            raise HTTPException(409, "SUBMISSION_KEY_RETIRED")
        # 타 사용자 프로젝트는 Host I/O 전에 404입니다.
        project = owned_project(db, payload.projectId, user.id) if payload.projectId else None
        if project and project.scenario_id != str(payload.scenarioId):
            raise HTTPException(422, "프로젝트 시나리오와 요청이 일치하지 않습니다.")
        try:
            approved = approval(client, payload.scenarioId)
        except (HostUnavailable, HostRunNotFound, HostRejected):
            # 아직 접수 원장 생성·Host POST를 하지 않았음을 명시합니다.
            raise unavailable(not_accepted=True) from None
        if payload.configurationId != approved["configurationId"]:
            raise HTTPException(409, "CONFIGURATION_CHANGED")
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
            return receipt(row, response)
        return send_submission(db, row, client, host_body, response)


def send_submission(db, row, client, host_body, response):
    """최초 접수와 명시적 수동 재시도가 공유하는 기존 201/200/409 처리입니다."""
    failure = None
    try:
        upstream = client.exchange("POST", "/api/v1/runs", body=host_body, key=row.internal_key, include_status=True)
        if (not isinstance(upstream, HostResponse)
                or (upstream.status, upstream.replayed) not in ((201, "false"), (200, "true"))):
            raise HostUnavailable()
        result = link_result(db, row, upstream.payload)
        response.status_code = upstream.status
        response.headers["Idempotency-Replayed"] = upstream.replayed
        return result
    except HostRejected as error:
        failure = error
    except (HostUnavailable, HostRunNotFound):
        pass
    lock_submission(db, row)
    if row.state in ("CREATED", "REJECTED"):
        return receipt(row, response)
    if failure and failure.status == 409 and failure.code == "IDEMPOTENCY_KEY_CONFLICT":
        row.state = "REJECTED"
        row.response_json = canonical({"detail": "IDEMPOTENCY_KEY_CONFLICT"})
        db.commit()
        return receipt(row, response)
    row.state = "REJECTED" if failure and failure.status in (400, 404, 405, 413, 415, 422, 429) else "UNKNOWN"
    db.commit()
    response.status_code = 202
    return receipt(row)


def lookup_host_submission(client, internal_key, *, retry_unavailable=True):
    # 최초 GET + 연결 실패/404 뒤 최대 3회 추가 조회. POST/새 키 생성은 하지 않습니다.
    path = f"/api/v1/submissions/{internal_key}"
    delays = iter(SUBMISSION_LOOKUP_RETRY_DELAYS)
    while True:
        try:
            require_host_reads_enabled()
        except HostUnavailable:
            raise unavailable() from None
        try:
            return client.exchange("GET", path)
        except HostAuthenticationFailed:
            raise HTTPException(502, "Host 서비스 인증에 실패했습니다.",
                                headers={"Cache-Control": "no-store"}) from None
        except (HostUnavailable, HostRunNotFound) as error:
            if isinstance(error, HostUnavailable) and not retry_unavailable:
                raise
            delay = next(delays, None)
            if delay is None:
                raise
            sleep(delay)


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
                data = lookup_host_submission(client, row.internal_key)
                # 대기 중 다른 조회가 이미 확정한 접수는 덮어쓰지 않습니다.
                db.refresh(row)
                if row.state not in ("PENDING", "UNKNOWN"):
                    return receipt(row)
                return link_result(db, row, recovery_receipt(row, data))
            except (HostUnavailable, HostRunNotFound, HostRejected, KeyError):
                # 404·503은 미생성을 보장하지 않습니다. POST를 재전송하지 않습니다.
                lock_submission(db, row)
                if row.state in ("PENDING", "UNKNOWN"):
                    row.state = "UNKNOWN"
                    db.commit()
        return receipt(row)


class ManualRetryRequest(BaseModel):
    # 본문·설정·키 교체를 받지 않고 사용자의 명시적 재시도만 나타냅니다.
    model_config = ConfigDict(extra="forbid")


@router.post("/submissions/{requestId}/retire")
def retire_unrecorded_submission(requestId: UUID4, payload: ManualRetryRequest, response: Response,
                                user: User = Depends(read_current_user), db: Session = Depends(get_db)):
    """사용자 클릭으로만 미접수 키를 폐기합니다. Host 호출·기존 Run 취소는 없습니다."""
    response.headers["Cache-Control"] = "no-store"
    with project_storage(db):
        lock_submission_keys(db)
        row = existing(db, user.id, requestId)
        if row is not None:
            owned_project(db, row.project_id, user.id)
            result = {"requestId": str(requestId), "outcome": "EXISTING", "submission": receipt(row)}
            db.rollback()  # Release the writer lock; keep the existing receipt unchanged.
            return result
        identity = (user.id, str(requestId))
        if db.get(RetiredSubmissionKey, identity) is None:
            db.add(RetiredSubmissionKey(owner_user_id=user.id, client_key=str(requestId)))
        db.commit()  # Permanently fence this client key before the browser releases it.
        return {"requestId": str(requestId), "outcome": "RETIRED"}


def require_manual_retry_enabled():
    require_host_reads_enabled()
    # 인증된 create_host와 동일한 영속 접수 DB를 유지함을 운영자가 확인한 뒤 활성화합니다.
    # Host DB 초기화·다른 Host로 전환 시에는 비활성화하여 기존 키 재전송을 막습니다.
    if os.environ.get("A2A_HOST_MANUAL_RETRY_ENABLED") != "true":
        raise HostUnavailable()


def frozen_host_body(row, project):
    """저장된 원문을 검증할 뿐 현재 승인 설정을 조회하거나 기본값으로 재구성하지 않습니다."""
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    try:
        body = json.loads(row.host_request_json, object_pairs_hook=unique_object)
        if (not isinstance(body, dict) or set(body) != {"scenarioId", "requestText", "configuration"}
                or digest(body) != row.host_request_sha256
                or body["scenarioId"] != project.scenario_id
                or not isinstance(body["requestText"], str) or not 1 <= len(body["requestText"]) <= 20000
                or str(UUID(row.internal_key, version=4)) != row.internal_key):
            raise ValueError()
        public_approved_configuration({"scenarioId": body["scenarioId"], "configurationId": row.configuration_id,
                                       "configuration": body["configuration"]}, project.scenario_id)
        return body
    except (ValueError, TypeError, KeyError, HostUnavailable):
        raise HostUnavailable() from None


@router.post("/submissions/{requestId}/retry")
def retry_submission(requestId: UUID4, payload: ManualRetryRequest, response: Response,
                     user: User = Depends(read_current_user), db: Session = Depends(get_db),
                     client: HostReadClient = Depends(get_host_read_client)):
    response.headers["Cache-Control"] = "no-store"
    with project_storage(db):
        row = existing(db, user.id, requestId)
        if row is None:
            raise HTTPException(404, "접수 기록을 찾을 수 없습니다.")
        project = owned_project(db, row.project_id, user.id)
        if row.state != "UNKNOWN":
            return receipt(row, response)
        if row.manual_retry_claimed_at is not None:
            response.status_code = 202
            return receipt(row)
        try:
            require_manual_retry_enabled()
            body = frozen_host_body(row, project)
        except HostUnavailable:
            raise unavailable() from None
        try:
            data = lookup_host_submission(client, row.internal_key, retry_unavailable=False)
            return link_result(db, row, recovery_receipt(row, data))
        except HostRunNotFound:
            # 제한된 GET 뒤에도 없으면 명시적 요청에 한해 동일 접수를 재전송합니다.
            # 404를 실패 확정으로 간주하거나 새 키/본문을 만들지 않습니다.
            pass
        except (HostUnavailable, HostRejected):
            db.refresh(row)
            response.status_code = 202 if row.state == "UNKNOWN" else 200
            return receipt(row, response)
        try:
            require_manual_retry_enabled()
        except HostUnavailable:
            raise unavailable() from None
        # DB의 원자적 조건부 갱신으로 worker/중복 클릭 중 한 요청만 POST 권한을 얻습니다.
        # crash/응답 유실 후에도 claim은 해제하지 않습니다. 이후에는 GET 복구만 허용합니다.
        claimed = db.execute(update(RunSubmission).where(
            RunSubmission.internal_key == row.internal_key, RunSubmission.state == "UNKNOWN",
            RunSubmission.manual_retry_claimed_at.is_(None),
            RunSubmission.host_request_json == row.host_request_json,
            RunSubmission.host_request_sha256 == row.host_request_sha256,
            RunSubmission.request_sha256 == row.request_sha256,
            RunSubmission.configuration_id == row.configuration_id,
            RunSubmission.owner_user_id == user.id, RunSubmission.project_id == project.project_id,
            RunSubmission.client_key == str(requestId),
        ).values(manual_retry_claimed_at=func.now()).execution_options(synchronize_session=False)).rowcount
        db.commit()  # 외부 POST보다 먼저 영속화하며 네트워크 대기 중 DB 잠금을 잡지 않습니다.
        db.refresh(row)
        if claimed != 1 or row.state != "UNKNOWN":
            response.status_code = 202 if row.state == "UNKNOWN" else 200
            return receipt(row, response)
        return send_submission(db, row, client, body, response)
