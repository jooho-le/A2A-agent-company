"""제품 서버 전용 Host 전송. 브라우저 JWT와 Host 자격증명은 분리합니다."""

import http.client
import json
import os
import re
from contextlib import closing
from urllib.parse import urlencode, urlsplit
from uuid import UUID


HOST_KEY = "local-multi"
# 실제 Host의 HostServiceAuthenticationMiddleware 계약(83번)이 구현되었습니다.
HOST_SERVICE_AUTH_VERIFIED = True
TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RESOURCES = frozenset(("", "steps", "events", "issues", "artifacts", "configuration"))


class HostUnavailable(Exception):
    def __init__(self):
        super().__init__("Host 조회를 사용할 수 없습니다.")


class HostRunNotFound(Exception):
    pass


class HostRejected(Exception):
    def __init__(self, status):
        self.status = status


class HostReadClient:
    def get(self, run_id, resource="", *, limit=100, offset=0):
        # 승인 플래그/토큰을 클라이언트 요청으로 받지 않습니다.
        if not HOST_SERVICE_AUTH_VERIFIED:
            raise HostUnavailable()
        try:
            return self._get(run_id, resource, limit=limit, offset=offset)
        except HostRunNotFound:
            raise
        except Exception:
            # 원본 HTTP 오류·본문·설정 URL·인증 헤더를 로그/응답에 싣지 않습니다.
            raise HostUnavailable() from None

    def _get(self, run_id, resource, *, limit, offset):
        run_id = str(UUID(str(run_id)))
        if resource not in RESOURCES or not 1 <= limit <= 500 or offset < 0:
            raise HostUnavailable()
        path = f"/api/v1/runs/{run_id}" + (f"/{resource}" if resource else "")
        if resource == "events":
            path += "?" + urlencode({"limit": limit, "offset": offset})
        payload = self.exchange("GET", path)
        if payload.get("runId") != run_id:
            raise HostUnavailable()
        if resource and resource != "configuration" and not isinstance(payload.get(resource), list):
            raise HostUnavailable()
        return payload

    def exchange(self, method, path, *, body=None, key=None):
        if not HOST_SERVICE_AUTH_VERIFIED:
            raise HostUnavailable()
        try:
            return self._exchange(method, path, body=body, key=key)
        except (HostRunNotFound, HostRejected):
            raise
        except Exception:
            raise HostUnavailable() from None

    def _exchange(self, method, path, *, body, key):
        token = os.environ.get("A2A_HOST_API_TOKEN", "")
        if not token or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
            raise HostUnavailable()
        base = os.environ.get("A2A_HOST_BASE_URL", "http://127.0.0.1:8002")
        url = urlsplit(base)
        if (url.scheme not in ("http", "https") or not url.hostname
                or url.username is not None or url.password is not None
                or url.path not in ("", "/") or url.query or url.fragment
                or any(ord(char) <= 32 for char in base)):
            raise HostUnavailable()
        # 평문 서비스 토큰은 로컬 연결에만 허용합니다.
        if url.scheme == "http" and url.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise HostUnavailable()
        connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        # http.client는 환경 프록시·리다이렉트를 따라가지 않습니다.
        with closing(connection_type(url.hostname, url.port, timeout=TIMEOUT_SECONDS)) as connection:
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            options = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
                options["body"] = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if key is not None:
                headers["Idempotency-Key"] = str(UUID(str(key)))
            connection.request(method, path, headers=headers, **options)
            with closing(connection.getresponse()) as response:
                if response.status == 404:
                    raise HostRunNotFound()
                if 400 <= response.status < 500 and response.status not in (401, 403):
                    raise HostRejected(response.status)
                if response.status not in (200, 201):
                    raise HostUnavailable()
                if response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise HostUnavailable()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise HostUnavailable()
                payload = json.loads(body)
        if not isinstance(payload, dict):
            raise HostUnavailable()
        # Host가 서비스 토큰을 잘못 반사하더라도 브라우저로 전달하지 않습니다.
        if token in json.dumps(payload, ensure_ascii=False):
            raise HostUnavailable()
        return payload


def get_host_read_client():
    return HostReadClient()
