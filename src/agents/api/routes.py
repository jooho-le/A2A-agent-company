"""Minimal nonstreaming HTTP+JSON binding using official A2A request/response types."""

import json
import logging
import math
import secrets
from collections.abc import Awaitable, Callable

from a2a.server.context import ServerCallContext
from a2a.types import CancelTaskRequest, GetTaskRequest, SendMessageResponse
from a2a.utils.constants import VERSION_HEADER
from a2a.utils.error_handlers import build_rest_error_payload
from a2a.utils.errors import (
    A2AError,
    ContentTypeNotSupportedError,
    InternalError,
    InvalidParamsError,
    TaskNotFoundError,
    UnsupportedOperationError,
    VersionNotSupportedError,
)
from fastapi import APIRouter, Request
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from starlette.responses import JSONResponse

from agents.api.handler import ProjectRequestHandler
from agents.api.task_store import sanitize_task
from agents.api.validation import parse_project_request
from agents.core.config import AgentSettings
from agents.core.contracts import A2A_MEDIA_TYPE, A2A_PROTOCOL_VERSION
from orchestrator.core.security import redact_data


logger = logging.getLogger(__name__)
MAX_REQUEST_BYTES = 1024 * 1024
VERSION_PARAMETER = {
    "in": "header", "name": VERSION_HEADER, "required": True,
    "schema": {"type": "string", "enum": [A2A_PROTOCOL_VERSION]},
    "example": A2A_PROTOCOL_VERSION,
}
SEND_EXAMPLE = {
    "message": {
        "messageId": "a1d87396-6d86-4a59-9a4d-45b5497d3641",
        "role": "ROLE_USER",
        "parts": [{"data": {"request": "회원가입 기능 개발"}, "mediaType": "application/json"}],
    },
    "configuration": {"returnImmediately": True, "acceptedOutputModes": ["application/json"]},
    "metadata": {
        "runId": "d3fa54a7-c5dd-49bb-8c45-f873b227ca3e",
        "workflowStepId": "5a9f60cf-5172-48ae-863d-eeb1ba98d6f6",
        "scenarioId": "f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76",
        "attempt": 0,
    },
}


def protocol_response(body: dict, status_code: int = 200, **kwargs) -> JSONResponse:
    headers = {VERSION_HEADER: A2A_PROTOCOL_VERSION, "Cache-Control": "no-store"}
    headers.update(kwargs.pop("headers", {}))
    return JSONResponse(
        body, status_code=status_code, media_type=A2A_MEDIA_TYPE,
        headers=headers, **kwargs,
    )


def error_response(error: A2AError) -> JSONResponse:
    payload = redact_data(build_rest_error_payload(error))
    return protocol_response(payload, status_code=payload["error"]["code"])


def authentication_response() -> JSONResponse:
    return protocol_response(
        {"error": {
            "code": 401, "status": "UNAUTHENTICATED",
            "message": "Valid bearer authentication is required",
            "details": [{
                "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                "reason": "UNAUTHENTICATED", "domain": "agents.project",
            }],
        }},
        401, headers={"WWW-Authenticate": "Bearer"},
    )


def is_authorized(request: Request, settings: AgentSettings) -> bool:
    if settings.bearer_token is None:
        return True  # Explicit unauthenticated loopback development mode only.
    values = request.headers.getlist("authorization")
    if len(values) != 1:
        return False
    scheme, separator, token = values[0].partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return False
    return secrets.compare_digest(
        token.encode("utf-8"), settings.bearer_token.get_secret_value().encode("utf-8")
    )


def call_context() -> ServerCallContext:
    # The SDK does not need credentials. Do NOT copy Authorization or all
    # HTTP headers into its context, metadata, events, or the Task store.
    return ServerCallContext(state={"headers": {VERSION_HEADER: A2A_PROTOCOL_VERSION}})


async def read_json_body(request: Request) -> object:
    chunks = bytearray()
    async for chunk in request.stream():
        chunks.extend(chunk)
        if len(chunks) > MAX_REQUEST_BYTES:
            raise InvalidParamsError(message="Request body exceeds the configured limit")
    try:
        def reject_constant(value: str) -> None:
            raise ValueError("Nonfinite JSON number")

        def finite_float(value: str) -> float:
            number = float(value)
            if not math.isfinite(number):
                raise ValueError("Nonfinite JSON number")
            return number

        def unique_fields(pairs: list[tuple[str, object]]) -> dict:
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate JSON field")
                result[key] = value
            return result

        return json.loads(
            chunks, parse_constant=reject_constant, parse_float=finite_float,
            object_pairs_hook=unique_fields,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidParamsError(message="Invalid JSON request body") from None


def create_protocol_router(settings: AgentSettings, handler: ProjectRequestHandler) -> APIRouter:
    router = APIRouter(tags=["A2A HTTP+JSON"])

    async def dispatch(
        request: Request, operation: Callable[[], Awaitable[dict]]
    ) -> JSONResponse:
        if not is_authorized(request, settings):
            return authentication_response()
        if request.headers.getlist(VERSION_HEADER) != [A2A_PROTOCOL_VERSION]:
            return error_response(VersionNotSupportedError(message="A2A-Version: 1.0 is required"))
        try:
            if request.method == "POST":
                media_type = request.headers.get("content-type", "").partition(";")[0].strip().lower()
                if media_type != A2A_MEDIA_TYPE:
                    raise ContentTypeNotSupportedError()
            return protocol_response(await operation())
        except A2AError as error:
            return error_response(error)
        except Exception as error:
            # Do not log exception text, body, IDs, or credentials.
            logger.error("Agent HTTP operation failed (error type: %s)", type(error).__name__)
            return error_response(InternalError())

    @router.post(
        "/message:send", summary="Create or continue an Agent Task",
        openapi_extra={
            "parameters": [VERSION_PARAMETER],
            "requestBody": {
                "required": True,
                "description": "Official A2A SendMessageRequest with project workflow metadata",
                "content": {A2A_MEDIA_TYPE: {"schema": {"type": "object"}, "example": SEND_EXAMPLE}},
            },
        },
    )
    async def send_message(request: Request) -> JSONResponse:
        async def operation() -> dict:
            params = parse_project_request(await read_json_body(request))
            task = await handler.on_message_send(params, call_context())
            return MessageToDict(SendMessageResponse(task=sanitize_task(task)))
        return await dispatch(request, operation)

    # Specific suffix routes must precede /tasks/{id}, which also matches a
    # literal ID ending in ':subscribe' in Starlette's routing order.
    @router.api_route("/message:stream", methods=["POST"], include_in_schema=False)
    @router.api_route("/tasks/{id}:subscribe", methods=["GET", "POST"], include_in_schema=False)
    async def unsupported(request: Request, id: str = "") -> JSONResponse:
        async def operation() -> dict:
            raise UnsupportedOperationError()
        return await dispatch(request, operation)

    @router.get(
        "/tasks/{id}", summary="Poll an Agent Task",
        openapi_extra={"parameters": [VERSION_PARAMETER]},
    )
    async def get_task(id: str, request: Request) -> JSONResponse:
        async def operation() -> dict:
            if len(request.query_params.multi_items()) != len(set(request.query_params.keys())):
                raise InvalidParamsError(message="Duplicate query fields are not allowed")
            try:
                params = ParseDict(dict(request.query_params), GetTaskRequest())
            except (ParseError, ValueError, TypeError):
                raise InvalidParamsError(message="Invalid Task query parameters") from None
            params.id = id
            task = await handler.on_get_task(params, call_context())
            if task is None:
                raise TaskNotFoundError()
            return MessageToDict(sanitize_task(task))
        return await dispatch(request, operation)

    @router.post(
        "/tasks/{id}:cancel", summary="Cancel and confirm an Agent Task",
        openapi_extra={
            "parameters": [VERSION_PARAMETER],
            "requestBody": {"content": {A2A_MEDIA_TYPE: {"example": {}}}},
        },
    )
    async def cancel_task(id: str, request: Request) -> JSONResponse:
        async def operation() -> dict:
            task = await handler.on_cancel_task(CancelTaskRequest(id=id), call_context())
            if task is None:
                raise TaskNotFoundError()
            return MessageToDict(sanitize_task(task))
        return await dispatch(request, operation)

    return router
