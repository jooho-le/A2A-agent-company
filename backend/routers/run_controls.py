"""JWT + project ownership before any authenticated Host control I/O."""
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

from host_reads import (HostAuthenticationFailed, HostReadClient, HostRejected, HostRunNotFound,
                        HostUnavailable, get_host_read_client, require_host_reads_enabled)
from routers.host_runs import owned_run

router = APIRouter(prefix="/api/v1/runs", tags=["run-controls"])


class UserCommand(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["CANCEL", "RESUME"]
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflowStepId: UUID4 | None = None
    clarification: str | None = Field(default=None, min_length=1, max_length=8000)

    @model_validator(mode="after")
    def check_action(self):
        if self.action == "CANCEL" and (self.workflowStepId is not None or self.clarification is not None):
            raise ValueError("중단 요청에는 추가 입력을 함께 보낼 수 없습니다.")
        if self.action == "RESUME" and (self.workflowStepId is None or not self.clarification):
            raise ValueError("답변할 직원과 내용을 선택해주세요.")
        return self


def exchange(request, response, client, method, path, **kwargs):
    if request.query_params:
        raise HTTPException(422, "지원하지 않는 요청입니다.")
    response.headers["Cache-Control"] = "no-store"
    try:
        require_host_reads_enabled()
        return client.exchange(method, path, **kwargs)
    except HostAuthenticationFailed:
        raise HTTPException(502, "Host 서비스 인증에 실패했습니다.",
                            headers={"Cache-Control": "no-store"}) from None
    except HostRunNotFound:
        raise HTTPException(404, "작업 또는 요청 기록을 찾을 수 없습니다.") from None
    except HostRejected as error:
        status = 409 if error.status == 409 else 422
        raise HTTPException(status, "작업 상태가 바뀌었거나 이 단계에서는 입력을 받을 수 없어요. 최신 상태를 확인해주세요.") from None
    except HostUnavailable:
        raise HTTPException(503, "실행 서버의 처리 결과를 확인할 수 없어요. 요청을 다시 보내지 말고 처리 상태를 확인해주세요.") from None


@router.get("/{runId}/interaction")
def interaction(request: Request, response: Response, run_id: UUID = Depends(owned_run),
                client: HostReadClient = Depends(get_host_read_client)):
    data = exchange(request, response, client, "GET", f"/api/v1/runs/{run_id}/interaction")
    if data.get("runId") != str(run_id) or not isinstance(data.get("requests"), list):
        raise HTTPException(503, "작업 확인 정보를 읽을 수 없습니다.")
    return data


@router.post("/{runId}/commands", status_code=202)
def command(body: UserCommand, request: Request, response: Response,
            command_id: UUID4 = Header(alias="Idempotency-Key"), run_id: UUID = Depends(owned_run),
            client: HostReadClient = Depends(get_host_read_client)):
    data = exchange(request, response, client, "POST", f"/api/v1/runs/{run_id}/commands",
                    body=body.model_dump(mode="json"), key=str(command_id), accepted_statuses=(200, 202))
    if data.get("runId") != str(run_id) or data.get("commandId") != str(command_id):
        raise HTTPException(503, "요청 접수를 확인하지 못했어요. 처리 상태를 확인해주세요.")
    return data


@router.get("/{runId}/commands/{command_id}")
def command_status(command_id: UUID4, request: Request, response: Response,
                   run_id: UUID = Depends(owned_run), client: HostReadClient = Depends(get_host_read_client)):
    data = exchange(request, response, client, "GET", f"/api/v1/runs/{run_id}/commands/{command_id}")
    if data.get("runId") != str(run_id) or data.get("commandId") != str(command_id):
        raise HTTPException(503, "요청 처리 상태를 확인할 수 없습니다.")
    return data
