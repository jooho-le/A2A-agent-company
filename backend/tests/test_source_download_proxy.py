"""실제 ZIP 생성/네트워크 없이 고정 ZIP 바이트와 임시 DB로 다운로드 중계만 검증."""

import asyncio
import hashlib
import io
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from starlette.datastructures import Headers

import test_host_reads
import test_projects_api
import host_reads


class SourceDownloadProxyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_host_reads.HostReadAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.artifact_id = str(uuid4())
        self.path = self.fixture.path(f"/artifacts/{self.artifact_id}/download")
        # 빈 ZIP의 고정 EOCD, 생성 결과가 아닌 테스트 전용 HTTP fixture.
        self.content = b"PK\x05\x06" + b"\0" * 18
        self.headers = {
            "Content-Type": "application/zip", "Content-Length": str(len(self.content)),
            "X-Archive-Sha256": hashlib.sha256(self.content).hexdigest(),
            "X-Snapshot-Sha256": "a" * 64, "X-Source-Artifact-Id": self.artifact_id,
            "X-Code-Version": "2", "Content-Disposition": 'attachment; filename="../../private.db"',
        }
        self.mock_archive()

    def mock_archive(self):
        self.fixture.response.status = 200
        self.fixture.response.getheader.side_effect = lambda key, default=None: self.headers.get(key, default)
        self.fixture.response.read.side_effect = io.BytesIO(self.content).read

    def request(self, *, path=None, user=1):
        async def invoke():
            headers = [(b"authorization", f"Bearer {self.fixture.fixture.tokens[user]}".encode())] if user else []
            url, messages = urlsplit(path or self.path), []
            delivered = False

            async def receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": b"", "more_body": False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            await test_projects_api.main.app({"type": "http", "asgi": {"version": "3.0"},
                "http_version": "1.1", "method": "GET", "scheme": "http", "path": url.path,
                "raw_path": url.path.encode(), "query_string": url.query.encode(), "root_path": "",
                "headers": headers, "client": ("127.0.0.1", 12345), "server": ("testserver", 80)}, receive, send)
            start = next(message for message in messages if message["type"] == "http.response.start")
            body = b"".join(message.get("body", b"") for message in messages if message["type"] == "http.response.body")
            return SimpleNamespace(status_code=start["status"], headers=Headers(raw=start["headers"]),
                                   content=body, text=body.decode("utf-8", errors="replace"))
        return asyncio.run(invoke())

    def test_owned_source_bytes_headers_and_service_auth(self):
        opened = []
        temporary_file = host_reads.tempfile.TemporaryFile

        def track_file(*args, **kwargs):
            file = temporary_file(*args, **kwargs)
            opened.append(file)
            return file

        with patch.object(host_reads.tempfile, "TemporaryFile", side_effect=track_file) as files:
            response = self.request()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.content)
        self.assertEqual(response.headers["content-disposition"],
                         f'attachment; filename="source-{self.fixture.run_id}-v2.zip"')
        for header in ("X-Archive-Sha256", "X-Snapshot-Sha256", "X-Source-Artifact-Id", "X-Code-Version"):
            self.assertEqual(response.headers[header], self.headers[header])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual(response.headers["content-type"], "application/zip")
        self.fixture.connection.request.assert_called_once_with("GET", self.path, headers={
            "Accept": "application/zip", "Authorization": "Bearer mock-host-service-token-for-tests-only"})
        files.assert_called_once()
        self.assertTrue(opened[0].closed)
        self.fixture.connection.close.assert_called()
        self.fixture.response.close.assert_called()

    def test_jwt_ownership_invalid_artifact_and_missing_token(self):
        self.assertEqual(self.request(user=None).status_code, 401)
        self.assertEqual(self.request(user=2).status_code, 404)
        self.assertEqual(self.request(path=self.path.replace(self.artifact_id, "invalid")).status_code, 422)
        self.assertEqual(self.request(path=self.path + "?path=/private/file").status_code, 422)
        with patch.dict(os.environ, {"A2A_HOST_API_TOKEN": ""}):
            self.assertEqual(self.request().status_code, 503)
        with patch.dict(os.environ, {"A2A_HOST_READS_ENABLED": "false"}):
            self.assertEqual(self.request().status_code, 503)
        self.fixture.constructor.assert_not_called()
        # Host는 동일 Run의 SOURCE가 아니거나 Snapshot이 없으면 404를 반환합니다.
        self.fixture.response.status = 404
        self.fixture.response.read.reset_mock()
        response = self.request()
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("content-disposition", response.headers)
        self.fixture.response.read.assert_not_called()

    def test_upstream_errors_and_malformed_zip_never_become_downloads(self):
        for upstream, expected in ((401, 502), (409, 409), (413, 413), (503, 503), (302, 503)):
            self.fixture.response.status = upstream
            response = self.request()
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("content-disposition", response.headers)
            self.assertNotIn("mock-host-service-token", response.text)
        self.mock_archive()
        self.fixture.connection.request.side_effect = TimeoutError("private upstream details")
        self.assertEqual(self.request().status_code, 503)
        self.fixture.connection.request.side_effect = None
        self.mock_archive()
        self.fixture.response.read.side_effect = io.BytesIO(self.content[:-1]).read
        self.assertEqual(self.request().status_code, 503)
        for header, bad in (("Content-Type", "application/json"), ("X-Source-Artifact-Id", str(uuid4())),
                            ("X-Archive-Sha256", "b" * 64), ("Content-Length", "20971521"),
                            ("X-Code-Version", "../../private")):
            with patch.dict(self.headers, {header: bad}):
                self.mock_archive()
                self.assertEqual(self.request().status_code, 503)
        for bad in (self.content[:-1], b"not a ZIP - private data"):
            self.content = bad
            self.headers["Content-Length"] = str(len(bad))
            self.headers["X-Archive-Sha256"] = hashlib.sha256(bad).hexdigest()
            self.mock_archive()
            self.assertEqual(self.request().status_code, 503)
        self.mock_archive()
        self.fixture.response.read.side_effect = TimeoutError("private stream details")
        with patch.object(host_reads.tempfile, "TemporaryFile", return_value=io.BytesIO()) as files:
            self.assertEqual(self.request().status_code, 503)
            self.assertTrue(files.return_value.closed)


if __name__ == "__main__":
    unittest.main()
