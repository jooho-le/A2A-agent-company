"""제품 서버 전용 Host 전송. 브라우저 JWT와 Host 자격증명은 분리합니다."""

import http.client
import hashlib
import tempfile
import time
import zipfile
import json
import os
import re
from contextlib import closing
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit
from uuid import UUID


HOST_KEY = "local-multi"
# 83번 인증 코드의 구현 여부입니다. 실제 GET 연결 준비 여부는 별도로 확인합니다.
HOST_SERVICE_AUTH_VERIFIED = True
TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_SOURCE_ZIP_BYTES = 20 * 1024 * 1024
SOURCE_CHUNK_BYTES = 64 * 1024
RESOURCES = frozenset(("", "steps", "events", "issues", "artifacts", "configuration"))


class HostUnavailable(Exception):
    def __init__(self):
        super().__init__("Host 조회를 사용할 수 없습니다.")


class HostAuthenticationFailed(HostUnavailable):
    """Host 401. 제품 사용자 JWT 오류와 구분하되 다른 호출부에서는 안전하게 차단합니다."""


class HostRunNotFound(Exception):
    pass


class HostRejected(Exception):
    def __init__(self, status, code=None):
        self.status = status
        self.code = code


@dataclass(frozen=True, repr=False)
class HostResponse:
    status: int
    payload: dict
    replayed: str | None


def _reject_nonfinite_json(_value):
    raise ValueError("Non-finite JSON value")


def require_host_reads_enabled():
    if not HOST_SERVICE_AUTH_VERIFIED or os.environ.get("A2A_HOST_READS_ENABLED") != "true":
        raise HostUnavailable()
    host_service_token()


def host_service_token():
    token = os.environ.get("A2A_HOST_API_TOKEN", "")
    if not 32 <= len(token) <= 8192 or not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", token):
        raise HostUnavailable()
    return token


def host_connection():
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
    # 환경 프록시와 리다이렉트를 사용하지 않습니다.
    return connection_type(url.hostname, url.port, timeout=TIMEOUT_SECONDS)


@dataclass(repr=False)
class HostSourceArchive:
    file: object
    headers: dict

    def close(self):
        self.file.close()

    def chunks(self):
        try:
            while chunk := self.file.read(SOURCE_CHUNK_BYTES):
                yield chunk
        finally:
            self.close()


class HostReadClient:
    def get(self, run_id, resource="", *, limit=100, offset=0):
        # 운영자가 인증된 create_host 목적지를 확인한 뒤 제품 서버에서만 활성화합니다.
        # 기본 Bootstrap으로 fallback하거나 브라우저의 승인 플래그를 받지 않습니다.
        require_host_reads_enabled()
        try:
            return self._get(run_id, resource, limit=limit, offset=offset)
        except (HostRunNotFound, HostAuthenticationFailed):
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

    def download_source(self, run_id, artifact_id):
        require_host_reads_enabled()
        archive = None
        try:
            run_id, artifact_id = str(UUID(str(run_id))), str(UUID(str(artifact_id)))
            if UUID(run_id).version != 4 or UUID(artifact_id).version != 4:
                raise HostRunNotFound()
            token = host_service_token()
            with closing(host_connection()) as connection:
                connection.request("GET", f"/api/v1/runs/{run_id}/artifacts/{artifact_id}/download",
                                   headers={"Accept": "application/zip", "Authorization": f"Bearer {token}"})
                with closing(connection.getresponse()) as response:
                    if response.status == 401:
                        raise HostAuthenticationFailed()
                    if response.status == 404:
                        raise HostRunNotFound()
                    if response.status in (409, 413):
                        raise HostRejected(response.status)
                    if response.status != 200:
                        raise HostUnavailable()
                    version = response.getheader("X-Code-Version", "")
                    length = response.getheader("Content-Length", "")
                    digest = response.getheader("X-Archive-Sha256", "")
                    snapshot = response.getheader("X-Snapshot-Sha256", "")
                    if (response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/zip"
                            or response.getheader("Content-Encoding", "identity") != "identity"
                            or response.getheader("X-Source-Artifact-Id", "") != artifact_id
                            or not re.fullmatch(r"[1-9][0-9]{0,8}", version)
                            or not re.fullmatch(r"[0-9]{1,8}", length)
                            or not 22 <= int(length) <= MAX_SOURCE_ZIP_BYTES
                            or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in (digest, snapshot))
                            or any(token in value for value in (digest, snapshot, artifact_id, run_id))):
                        raise HostUnavailable()
                    # 디스크 임시 파일(자동 삭제)에 제한된 청크로 받고 검증 후에만 200을 반환합니다.
                    # 원본 파일명/경로 및 임의 Host 헤더는 전달하지 않습니다.
                    archive = tempfile.TemporaryFile(mode="w+b")
                    checksum, size, deadline = hashlib.sha256(), 0, time.monotonic() + 30
                    while chunk := response.read(SOURCE_CHUNK_BYTES):
                        size += len(chunk)
                        if size > int(length) or time.monotonic() > deadline:
                            raise HostUnavailable()
                        archive.write(chunk)
                        checksum.update(chunk)
                    archive.seek(0)
                    if (size != int(length) or checksum.hexdigest() != digest
                            or archive.read(4) not in (b"PK\x03\x04", b"PK\x05\x06")
                            or not zipfile.is_zipfile(archive)):
                        raise HostUnavailable()
                    archive.seek(0)
            headers = {
                "Content-Disposition": f'attachment; filename="source-{run_id}-v{version}.zip"',
                "Content-Length": str(size), "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "X-Archive-Sha256": digest, "X-Snapshot-Sha256": snapshot,
                "X-Source-Artifact-Id": artifact_id, "X-Code-Version": version,
            }
            return HostSourceArchive(archive, headers)
        except (HostRunNotFound, HostAuthenticationFailed, HostRejected):
            if archive is not None:
                archive.close()
            raise
        except Exception:
            if archive is not None:
                archive.close()
            raise HostUnavailable() from None

    def exchange(self, method, path, *, body=None, key=None, accepted_statuses=(200, 201), include_status=False):
        if not HOST_SERVICE_AUTH_VERIFIED:
            raise HostUnavailable()
        try:
            return self._exchange(method, path, body=body, key=key, accepted_statuses=accepted_statuses,
                                  include_status=include_status)
        except (HostRunNotFound, HostRejected, HostAuthenticationFailed):
            raise
        except Exception:
            raise HostUnavailable() from None

    def _exchange(self, method, path, *, body, key, accepted_statuses=(200, 201), include_status=False):
        token = host_service_token()
        with closing(host_connection()) as connection:
            headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            options = {}
            if body is not None:
                headers["Content-Type"] = "application/json"
                options["body"] = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if key is not None:
                headers["Idempotency-Key"] = str(UUID(str(key)))
            connection.request(method, path, headers=headers, **options)
            with closing(connection.getresponse()) as response:
                if response.status == 401:
                    raise HostAuthenticationFailed()
                if response.status == 404:
                    raise HostRunNotFound()
                if response.status == 409 and method == "POST" and path == "/api/v1/runs":
                    # 계약상 확인된 코드만 보존합니다. 원본 오류는 반환/기록하지 않습니다.
                    code = None
                    if response.getheader("Content-Type", "").split(";")[0].strip().lower() == "application/json":
                        raw_error = response.read(4097)
                        if len(raw_error) <= 4096:
                            try:
                                error = json.loads(raw_error, parse_constant=_reject_nonfinite_json)
                                if error == {"detail": "IDEMPOTENCY_KEY_CONFLICT"}:
                                    code = "IDEMPOTENCY_KEY_CONFLICT"
                            except (ValueError, UnicodeError):
                                pass
                    raise HostRejected(409, code)
                if 400 <= response.status < 500 and response.status not in (401, 403):
                    raise HostRejected(response.status)
                if response.status not in accepted_statuses:
                    raise HostUnavailable()
                if response.getheader("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                    raise HostUnavailable()
                body = response.read(MAX_RESPONSE_BYTES + 1)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise HostUnavailable()
                payload = json.loads(body, parse_constant=_reject_nonfinite_json)
                status = response.status
                replayed = response.getheader("Idempotency-Replayed") if include_status else None
        if not isinstance(payload, dict):
            raise HostUnavailable()
        # Host가 서비스 토큰을 잘못 반사하더라도 브라우저로 전달하지 않습니다.
        if token in json.dumps(payload, ensure_ascii=False):
            raise HostUnavailable()
        return HostResponse(status, payload, replayed) if include_status else payload


def get_host_read_client():
    return HostReadClient()
