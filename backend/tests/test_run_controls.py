"""Product ownership and Host service credentials remain on the server."""
import json
import os
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

import main
import test_projects_api as fixtures
import test_host_reads
from host_reads import HostRejected, HostUnavailable, get_host_read_client
from models import ProjectRun


class Host:
    def __init__(self, run_id):
        self.run_id = run_id; self.calls = []; self.failure = None
    def exchange(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if self.failure: raise self.failure
        if path.endswith('/interaction'):
            return {"runId": self.run_id, "revision": "a" * 64, "requests": [], "canCancel": True}
        return {"runId": self.run_id, "commandId": kwargs.get('key') or path.rsplit('/', 1)[-1],
                "action": "CANCEL", "status": "QUEUED"}


class ProductControlTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"A2A_HOST_READS_ENABLED": "true",
                                              "A2A_HOST_API_TOKEN": "mock-host-service-token-for-tests-only"})
        environment.start()
        self.addCleanup(environment.stop)
        self.fixture = fixtures.ProjectAPITests(methodName='runTest'); self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp(); self.request = self.fixture.request
        mine, other = self.fixture.create(), self.fixture.create(user=2)
        self.run_id, self.other_id = str(uuid4()), str(uuid4())
        with Session(self.fixture.engine) as db:
            db.add_all([ProjectRun(project_id=mine['projectId'], run_id=self.run_id, configuration_id='test'),
                        ProjectRun(project_id=other['projectId'], run_id=self.other_id, configuration_id='test')]); db.commit()
        self.host = Host(self.run_id)
        main.app.dependency_overrides[get_host_read_client] = lambda: self.host
        self.addCleanup(main.app.dependency_overrides.pop, get_host_read_client)
        self.key = str(uuid4()); self.path = '/api/v1/runs/' + self.run_id
        self.body = {"action": "CANCEL", "revision": "a" * 64}

    def post(self, **kwargs):
        return self.request('POST', self.path + '/commands', kwargs.pop('body', self.body),
            extra_headers=[(b'idempotency-key', self.key.encode())], **kwargs)

    def test_owner_can_read_questions_and_submit_one_whitelisted_command(self):
        self.assertEqual(self.request('GET', self.path + '/interaction')[0], 200)
        self.assertEqual(self.post()[0], 202)
        self.assertEqual(self.host.calls[-1], ('POST', self.path + '/commands', {
            'body': {**self.body, 'workflowStepId': None, 'clarification': None},
            'key': self.key, 'accepted_statuses': (200, 202)}))
        self.assertEqual(self.request('GET', self.path + '/commands/' + self.key)[0], 200)

    def test_auth_owner_and_route_boundaries_precede_host_io(self):
        for suffix in ('/interaction', '/commands/' + self.key):
            self.assertEqual(self.request('GET', self.path + suffix, user=None, token='')[0], 401)
            self.assertEqual(self.request('GET', self.path + suffix, user=2)[0], 404)
            self.assertEqual(self.request('GET', '/api/v1/runs/' + self.other_id + suffix)[0], 404)
        self.assertEqual(self.post(user=2)[0], 404)
        self.assertEqual(self.post(user=None, token='')[0], 401)
        self.assertEqual(self.post(body={**self.body, 'configuration': {}})[0], 422)
        self.assertEqual(self.request('POST', self.path + '/commands', self.body)[0], 422)
        self.assertEqual(self.request('GET', self.path + '/interaction?url=other')[0], 422)
        self.assertEqual(self.host.calls, [])

    def test_unknown_transport_or_state_change_is_not_reported_as_success(self):
        self.host.failure = HostUnavailable()
        self.assertEqual(self.post()[0], 503)
        self.assertEqual(len(self.host.calls), 1)
        self.host.failure = HostRejected(409)
        self.assertEqual(self.post()[0], 409)
        self.assertEqual(len(self.host.calls), 2)  # explicit calls; no automatic retry


class HostActivationPolicyTests(unittest.TestCase):
    """기존 임시 DB·Mock HTTP fixture만 재사용하며 실제 Host 요청은 하지 않습니다."""

    def setUp(self):
        self.fixture = test_host_reads.HostReadAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.key = str(uuid4())
        self.base = f"/api/v1/runs/{self.fixture.run_id}"
        self.body = {"action": "CANCEL", "revision": "a" * 64}
        self.routes = (
            ("GET", "/api/v1/scenarios", None),
            ("GET", self.base + "/interaction", None),
            ("POST", self.base + "/commands", self.body),
            ("GET", self.base + "/commands/" + self.key, None),
        )

    def request(self, route, **kwargs):
        method, path, body = route
        return self.fixture.request(method, path, body,
            extra_headers=[(b"idempotency-key", self.key.encode())] if method == "POST" else [], **kwargs)

    def test_disabled_host_blocks_all_routes_before_transport(self):
        for enabled in (None, "", "false", "1"):
            with patch.dict(os.environ):
                os.environ.pop("A2A_HOST_READS_ENABLED", None)
                if enabled is not None:
                    os.environ["A2A_HOST_READS_ENABLED"] = enabled
                for route in self.routes:
                    with self.subTest(route=route[:2], enabled=enabled):
                        self.assertEqual(self.request(route)[0], 503)
        self.fixture.constructor.assert_not_called()
        self.fixture.connection.request.assert_not_called()

    def test_missing_or_malformed_token_blocks_all_routes(self):
        for token in (None, "", "short", "x" * 8193, "x" * 32 + "\r\n", "한" * 32):
            with patch.dict(os.environ):
                os.environ.pop("A2A_HOST_API_TOKEN", None)
                if token is not None:
                    os.environ["A2A_HOST_API_TOKEN"] = token
                for route in self.routes:
                    self.assertEqual(self.request(route)[0], 503)
        self.fixture.constructor.assert_not_called()
        self.fixture.connection.request.assert_not_called()

    def test_authorized_forwarding_ownership_and_upstream_auth_failure(self):
        for route in self.routes:
            self.assertEqual(self.request(route, user=None, token="")[0], 401)
        for route in self.routes[1:]:
            self.assertEqual(self.request(route, user=2)[0], 404)
        self.fixture.constructor.assert_not_called()
        scenario = {"scenarioId": self.fixture.fixture.scenario_id, "key": "SCN-001"}
        for route in self.routes:
            method, path, _ = route
            payload = ({"scenarios": [scenario]} if path.endswith("/scenarios") else
                {"runId": self.fixture.run_id, "requests": [], "revision": "a" * 64, "canCancel": True}
                if path.endswith("/interaction") else
                {"runId": self.fixture.run_id, "commandId": self.key, "action": "CANCEL", "status": "QUEUED"})
            self.fixture.mock_json(payload)
            self.assertEqual(self.request(route), (202 if method == "POST" else 200, payload))
            call = self.fixture.connection.request.call_args
            self.assertEqual(call.args, (method, path))
            self.assertEqual(call.kwargs["headers"]["Authorization"],
                             "Bearer mock-host-service-token-for-tests-only")
            if method == "POST":
                self.assertEqual(call.kwargs["headers"]["Idempotency-Key"], self.key)
                self.assertEqual(json.loads(call.kwargs["body"]),
                                 {**self.body, "workflowStepId": None, "clarification": None})
        # 명시적인 Host 401은 제품 JWT 오류(401)나 준비 오류(503)와 구분합니다.
        self.fixture.response.status = 401
        self.fixture.response.read.reset_mock()
        for route in self.routes:
            self.assertEqual(self.request(route), (502, {"detail": "Host 서비스 인증에 실패했습니다."}))
        self.fixture.response.read.assert_not_called()
