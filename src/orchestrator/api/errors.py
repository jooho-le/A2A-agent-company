"""Validation errors expose contract locations, never the submitted values."""

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse

from orchestrator.core.security import redact_data


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError,
) -> JSONResponse:
    # Do not serialize input, body, ctx or the original exception message.
    # Even recognized password/API-key fields bypass normal model validators
    # when the surrounding request is invalid.
    details = [
        {
            "type": error.get("type", "request_validation_error"),
            "loc": redact_data(list(error.get("loc", ()))),
            "msg": "Invalid request input; see the API contract for this field",
        }
        for error in exc.errors()
    ]
    return JSONResponse(status_code=422, content={"detail": details})
