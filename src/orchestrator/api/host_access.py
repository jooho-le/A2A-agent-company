"""Server-to-server Host authentication, before routing and body validation."""

from collections.abc import Mapping
import re
from secrets import compare_digest

from pydantic import SecretStr
from fastapi import HTTPException, Request
from starlette.responses import JSONResponse


class HostAPIAccessError(ValueError):
    def __init__(self):
        super().__init__("HOST_API_TOKEN_INVALID")


def require_host_service_mode(request: Request):
    """Do not expose new server-only capabilities via the open dev Bootstrap."""
    if not request.app.state.host_api_auth_required:
        raise HTTPException(status_code=503, detail="HOST_API_NOT_CONFIGURED",
                            headers={"Cache-Control": "no-store"})


def install_host_api_documentation(app):
    """Describe the real Host boundary without putting its credential in OpenAPI."""
    original_openapi = app.openapi

    def host_openapi():
        schema = original_openapi()
        components = schema.setdefault("components", {})
        components.setdefault("securitySchemes", {})["HostServiceBearer"] = {
            "type": "http", "scheme": "bearer",
            "description": "Server-only Host credential, separate from product JWT and Agent tokens.",
        }
        prefix = app.state.settings.api_prefix.rstrip("/")
        for path, operations in schema.get("paths", {}).items():
            if path != prefix and not path.startswith(prefix + "/"):
                continue
            for method, operation in operations.items():
                if method not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                    continue
                operation["security"] = [{"HostServiceBearer": []}]
                operation.setdefault("responses", {}).setdefault("401", {"description": "Host service authentication required"})
                if path == prefix + "/runs" and method == "post":
                    for parameter in operation.get("parameters", []):
                        if parameter.get("in") == "header" and parameter.get("name", "").lower() == "idempotency-key":
                            parameter["required"] = True
                            parameter["schema"] = {"type": "string", "format": "uuid",
                                "description": "One internal UUIDv4, not a list of keys"}
        create = components.get("schemas", {}).get("CreateRunRequest", {})
        if create and "configuration" not in create.setdefault("required", []):
            create["required"].append("configuration")
        return schema

    app.openapi = host_openapi


def host_token_from_environment(environment: Mapping, *, other_credentials=()) -> SecretStr:
    """Read only the dedicated binding; never fall back to model/role tokens."""
    value = environment.get("A2A_HOST_API_TOKEN")
    if (type(value) is not str or not 32 <= len(value) <= 8192
            or re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", value) is None):
        raise HostAPIAccessError()
    if any(value == credential.get_secret_value() for credential in other_credentials
           if credential is not None):
        raise HostAPIAccessError()
    return SecretStr(value)


class HostServiceAuthenticationMiddleware:
    """Pure ASGI middleware: no request-body buffering or background Task owner."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            state = scope["app"].state
            prefix = state.settings.api_prefix.rstrip("/")
            path = scope["path"]
            if state.host_api_auth_required and (path == prefix or path.startswith(prefix + "/")):
                token = state.host_api_token
                if not isinstance(token, SecretStr):
                    response = JSONResponse({"detail": "HOST_API_NOT_CONFIGURED"}, status_code=503,
                                            headers={"Cache-Control": "no-store"})
                    return await response(scope, receive, send)
                headers = [value for key, value in scope["headers"] if key.lower() == b"authorization"]
                authenticated = False
                if len(headers) == 1:
                    scheme, separator, credential = headers[0].partition(b" ")
                    authenticated = (scheme.lower() == b"bearer" and separator == b" "
                        and compare_digest(credential, token.get_secret_value().encode("ascii")))
                if not authenticated:
                    response = JSONResponse({"detail": "HOST_API_UNAUTHORIZED"}, status_code=401,
                        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"})
                    return await response(scope, receive, send)
        await self.app(scope, receive, send)
