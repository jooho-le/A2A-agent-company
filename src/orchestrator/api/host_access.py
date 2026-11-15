"""Server-to-server Host authentication, before routing and body validation."""

from collections.abc import Mapping
import re
from secrets import compare_digest

from pydantic import SecretStr
from starlette.responses import JSONResponse


class HostAPIAccessError(ValueError):
    def __init__(self):
        super().__init__("HOST_API_TOKEN_INVALID")


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
