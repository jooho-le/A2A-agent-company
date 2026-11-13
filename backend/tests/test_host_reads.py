"""네트워크 대신 Mock HTTP 연결과 기존 임시 DB/ASGI fixture만 사용합니다."""

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import host_reads
import test_projects_api as project_fixtures
from models import Project, ProjectRun


class HostReadAPITests(unittest.TestCase):
    def setUp(self):
        # 테스트 메서드를 상속/재실행하지 않고 격리 환경과 ASGI 요청 도우미만 재사용합니다.
        self.fixture = project_fixtures.ProjectAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request = self.fixture.request
        self.run_id = str(uuid4())
        self.other_run_id = str(uuid4())
        self.remote_run_id = str(uuid4())
        with Session(self.fixture.engine) as db:
            projects = [Project(owner_user_id=owner, name="Mock 프로젝트", scenario_id=self.fixture.scenario_id) for owner in (1, 2)]
            db.add_all(projects)
            db.flush()
            db.add_all([
                ProjectRun(project_id=projects[0].project_id, run_id=self.run_id, configuration_id="mock-config"),
                ProjectRun(project_id=projects[1].project_id, run_id=self.other_run_id, configuration_id="mock-config"),
                ProjectRun(project_id=projects[0].project_id, run_id=self.remote_run_id, host_key="other-host", configuration_id="mock-config"),
            ])
            db.commit()
        self.db_before = self.snapshot()
        self.addCleanup(self.assert_db_unchanged)
        self.env = patch.dict(os.environ, {"A2A_HOST_BASE_URL": "http://127.0.0.1:8002", "A2A_HOST_API_TOKEN": "mock-service-token-only"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.connection = MagicMock()
        self.response = self.connection.getresponse.return_value
        self.response.status = 200
        self.response.getheader.return_value = "application/json; charset=utf-8"
        self.constructor_patch = patch.object(host_reads.http.client, "HTTPConnection", return_value=self.connection)
        self.constructor = self.constructor_patch.start()
        self.addCleanup(self.constructor_patch.stop)
        # 테스트에서도 HTTPS가 실제 연결되지 않도록 차단합니다.
        https_patch = patch.object(host_reads.http.client, "HTTPSConnection", side_effect=AssertionError("Unexpected HTTPS"))
        https_patch.start()
        self.addCleanup(https_patch.stop)
        self.auth_patch = patch.object(host_reads, "HOST_SERVICE_AUTH_VERIFIED", True)
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)

    def snapshot(self):
        with self.fixture.engine.connect() as db:
            return [db.exec_driver_sql(f'SELECT * FROM "{table}" ORDER BY 1').all()
                    for table in ("users", "documents", "projects", "project_runs")]

    def assert_db_unchanged(self):
        self.assertEqual(self.snapshot(), self.db_before)

    def path(self, suffix="", run_id=None):
        return f"/api/v1/runs/{run_id or self.run_id}{suffix}"

    def mock_json(self, body):
        self.response.read.return_value = json.dumps(body).encode()

    def test_service_path_alignment_and_access_guards(self):
        # 3단계 B는 이 테스트만 실행합니다. 기존 4개 전체 검증은 반복하지 않습니다.
        base = f"/api/v1/runs/{self.run_id}"
        for suffix, payload in (
            ("", {"runId": self.run_id, "status": "FINISHED", "verdict": "HUMAN_REVIEW"}),
            ("/events?limit=2&offset=0", {"runId": self.run_id, "events": [], "total": 0, "limit": 2, "offset": 0}),
        ):
            self.mock_json(payload)
            self.assertEqual(self.request("GET", base + suffix), (200, payload))
            self.assertEqual(self.connection.request.call_args.args, ("GET", base + suffix))
        self.constructor.reset_mock()
        for suffix in ("", "/events"):
            self.assertEqual(self.request("GET", base + suffix, user=None, token="")[0], 401)
            self.assertEqual(self.request("GET", base + suffix, user=2)[0], 404)
        with patch.object(host_reads, "HOST_SERVICE_AUTH_VERIFIED", False):
            self.assertEqual(self.request("GET", base), (503, {"detail": "Host 조회를 사용할 수 없습니다."}))
        self.assertEqual(self.request("GET", f"/api/runs/{self.run_id}")[0], 404)
        # 생성 경로를 구현하거나 catch-all 프록시로 선점하지 않습니다.
        self.assertEqual(self.request("POST", "/api/v1/runs")[0], 404)
        self.constructor.assert_not_called()

    def test_owner_reads_all_five_contracts_with_service_auth_and_event_order(self):
        events = [{"eventId": str(uuid4()), "runId": self.run_id, "eventType": kind, "actor": "Orchestrator", "attempt": 0}
                  for kind in ("RUN_STARTED", "A2A_MESSAGE_SENT")]
        fixtures = {
            "": {"runId": self.run_id, "status": "FINISHED", "verdict": "HUMAN_REVIEW"},
            "/steps": {"runId": self.run_id, "steps": [{"agentRole": "QA", "status": "FAILED"}]},
            "/events?limit=2&offset=3": {"runId": self.run_id, "events": events, "total": 5, "limit": 2, "offset": 3},
            "/issues": {"runId": self.run_id, "issues": [{"issueId": str(uuid4()), "summary": "Mock 검증 결과"}]},
            "/artifacts": {"runId": self.run_id, "artifacts": [{"artifactId": str(uuid4()), "name": "Mock 보고서"}]},
        }
        for suffix, payload in fixtures.items():
            with self.subTest(suffix=suffix):
                self.mock_json(payload)
                self.assertEqual(self.request("GET", self.path(suffix)), (200, payload))
                self.constructor.assert_called_with("127.0.0.1", 8002, timeout=5)
                self.connection.request.assert_called_with("GET", f"/api/v1/runs/{self.run_id}{suffix}",
                    headers={"Accept": "application/json", "Authorization": "Bearer mock-service-token-only"})
                self.connection.close.assert_called()
                self.response.close.assert_called()
        self.mock_json({"runId": self.run_id, "events": [], "total": 0, "limit": 100, "offset": 0})
        self.assertEqual(self.request("GET", self.path("/events"))[0], 200)
        self.assertEqual(self.connection.request.call_args.args[1], f"/api/v1/runs/{self.run_id}/events?limit=100&offset=0")

    def test_foreign_unknown_and_wrong_host_runs_never_contact_host(self):
        for suffix in ("", "/steps", "/events", "/issues", "/artifacts"):
            for run_id in (self.other_run_id, self.remote_run_id, str(uuid4())):
                self.assertEqual(self.request("GET", self.path(suffix, run_id))[0], 404)
            self.assertEqual(self.request("GET", self.path(suffix), user=2)[0], 404)
        self.constructor.assert_not_called()

    def test_jwt_and_explicit_route_query_boundaries(self):
        for suffix in ("", "/steps", "/events", "/issues", "/artifacts"):
            for token in ("", "invalid"):
                self.assertEqual(self.request("GET", self.path(suffix), user=None, token=token)[0], 401)
        for suffix in ("?url=http://example.test", "?hostKey=other-host", "/events?limit=501", "/events?offset=-1", "/events?offset=0&offset=1"):
            self.assertEqual(self.request("GET", self.path(suffix))[0], 422)
        self.assertEqual(self.request("GET", self.path("/configuration"))[0], 404)
        self.assertEqual(self.request("POST", self.path())[0], 405)
        self.constructor.assert_not_called()

    def test_unverified_auth_missing_token_and_host_errors_fail_safely(self):
        unavailable = (503, {"detail": "Host 조회를 사용할 수 없습니다."})
        with patch.object(host_reads, "HOST_SERVICE_AUTH_VERIFIED", False):
            self.assertEqual(self.request("GET", self.path()), unavailable)
        with patch.dict(os.environ, {"A2A_HOST_API_TOKEN": ""}):
            self.assertEqual(self.request("GET", self.path()), unavailable)
        for url in ("http://example.test", "http://user:password@127.0.0.1:8002", "http://127.0.0.1:8002/private"):
            with patch.dict(os.environ, {"A2A_HOST_BASE_URL": url}):
                self.assertEqual(self.request("GET", self.path()), unavailable)
        self.constructor.assert_not_called()
        for failure in (ConnectionRefusedError("private connection detail"), TimeoutError("mock-service-token-only")):
            self.connection.request.side_effect = failure
            self.assertEqual(self.request("GET", self.path()), unavailable)
            self.connection.close.assert_called()
        self.connection.request.side_effect = None
        for status in (301, 401, 403, 500, 503):
            self.response.status = status
            self.assertEqual(self.request("GET", self.path()), unavailable)
            self.response.close.assert_called()
        self.response.status = 404
        self.assertEqual(self.request("GET", self.path()), (404, {"detail": "Run을 찾을 수 없습니다."}))
        self.response.status = 200
        for body in ({"runId": str(uuid4())}, {"runId": self.run_id, "detail": "mock-service-token-only"}):
            self.mock_json(body)
            self.assertEqual(self.request("GET", self.path()), unavailable)
        self.response.read.return_value = b"not-json private upstream error"
        self.assertEqual(self.request("GET", self.path()), unavailable)
        self.response.read.return_value = b"x" * (host_reads.MAX_RESPONSE_BYTES + 1)
        self.assertEqual(self.request("GET", self.path()), unavailable)
        self.mock_json({"runId": self.run_id, "steps": None})
        self.assertEqual(self.request("GET", self.path("/steps")), unavailable)


if __name__ == "__main__":
    unittest.main()
