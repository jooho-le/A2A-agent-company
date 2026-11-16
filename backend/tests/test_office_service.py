"""임시 SQLite와 ASGI에서 실제 제품 라우트/소유권/접수 복구를 검증합니다."""
import json
import unittest
from unittest.mock import patch
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import test_projects_api as fixtures
import main
from host_reads import HostRunNotFound, HostUnavailable, get_host_read_client
from models import Project, ProjectRun, RunSubmission
from project_scenarios import ScenarioRegistryUnavailable
from routers.run_submissions import digest, unavailable


class Host:
    def __init__(self, scenario_id):
        self.scenario_id = scenario_id
        self.calls = []
        self.responses = {}
        self.timeout = False
        self.offline = False
        self.configuration = {"architecture": "MULTI_AGENT", "model": {"provider": "openai", "modelId": "test-only"}}

    def exchange(self, method, path, *, body=None, key=None):
        self.calls.append((method, path, deepcopy(body), key))
        if self.offline:
            raise HostUnavailable()
        if path == "/api/v1/scenarios":
            return {"scenarios": [{"scenarioId": self.scenario_id, "name": "회원가입"}]}
        if path.startswith("/api/v1/host/approved-configuration?"):
            return {"scenarioId": self.scenario_id, "configurationId": digest(self.configuration), "configuration": self.configuration}
        if method == "POST":
            result = {"run": {"runId": str(uuid4()), "scenarioId": body["scenarioId"], "status": "PLANNING"},
                      "firstStep": {}, "dispatchStatus": "SCHEDULED"}
            self.responses[key] = {"internalKey": key, "status": "CREATED", "requestSha256": digest(body), "originalResponse": result}
            if self.timeout:
                raise HostUnavailable()
            return result
        key = path.rsplit("/", 1)[-1]
        if key not in self.responses:
            raise HostRunNotFound()
        return self.responses[key]


class OfficeServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request = self.fixture.request
        self.host = Host(self.fixture.scenario_id)
        main.app.dependency_overrides[get_host_read_client] = lambda: self.host
        self.addCleanup(main.app.dependency_overrides.pop, get_host_read_client)
        self.key = str(uuid4())
        self.body = {"projectName": "실제 업무", "scenarioId": self.fixture.scenario_id,
                     "configurationId": digest(self.host.configuration), "requestText": "이메일로 회원가입하는 기능을 만들어줘."}

    def post(self, body=None, key=None, user=1):
        return self.request("POST", "/api/v1/runs", body or self.body, user=user,
                            extra_headers=[(b"idempotency-key", (key or self.key).encode())])

    def rows(self, cls):
        with Session(self.fixture.engine) as db:
            return db.scalars(select(cls)).all()

    def test_jwt_precedes_host_and_only_registered_approval_is_returned(self):
        self.assertEqual(self.request("GET", "/api/v1/scenarios", user=None)[0], 401)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.request("GET", "/api/v1/scenarios")[0], 200)
        status, result = self.request("GET", "/api/v1/host/approved-configuration?scenarioId=" + self.fixture.scenario_id)
        self.assertEqual(status, 200)
        self.assertEqual(result["configurationId"], digest(result["configuration"]))
        self.assertEqual(self.rows(Project), [])

    def test_create_replay_ownership_and_original_natural_request(self):
        status, result = self.post()
        self.assertEqual(status, 201, result)
        self.assertEqual(result["submissionStatus"], "CREATED")
        self.assertEqual(self.post(), (200, result))
        sent = [call for call in self.host.calls if call[0] == "POST"]
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][2]["requestText"], self.body["requestText"])
        self.assertEqual(set(sent[0][2]), {"scenarioId", "requestText", "configuration"})
        self.assertNotEqual(sent[0][3], self.key)
        self.assertEqual(len(self.rows(ProjectRun)), 1)
        self.assertEqual(self.rows(Project)[0].name, self.body["projectName"])
        self.assertEqual(self.post({**self.body, "requestText": "다른 요청"})[0], 409)
        before = len(self.host.calls)
        self.assertEqual(self.request("GET", "/api/v1/submissions/" + self.key, user=2)[0], 404)
        self.assertEqual(len(self.host.calls), before)
        self.assertEqual(self.fixture.legacy_snapshot(), self.fixture.legacy_before)

    def test_lost_post_response_recovers_without_second_creation(self):
        self.host.timeout = True
        status, uncertain = self.post()
        self.assertEqual((status, uncertain["submissionStatus"]), (202, "UNKNOWN"))
        self.assertEqual(self.post()[1]["submissionStatus"], "UNKNOWN")
        status, recovered = self.request("GET", "/api/v1/submissions/" + self.key)
        self.assertEqual((status, recovered["submissionStatus"]), (200, "CREATED"))
        self.assertEqual(len(self.rows(ProjectRun)), 1)
        self.assertEqual(len([call for call in self.host.calls if call[0] == "POST"]), 1)
        self.assertNotIn("internalKey", recovered)
        self.assertNotIn("configuration", recovered)

    def test_missing_or_wrong_recovery_receipt_stays_unknown(self):
        self.host.timeout = True
        self.post()
        record = self.rows(RunSubmission)[0]
        self.host.responses[record.internal_key]["requestSha256"] = "0" * 64
        self.assertEqual(self.request("GET", "/api/v1/submissions/" + self.key)[1]["submissionStatus"], "UNKNOWN")
        self.host.responses.clear()
        self.assertEqual(self.request("GET", "/api/v1/submissions/" + self.key)[1]["submissionStatus"], "UNKNOWN")
        self.assertEqual(self.rows(ProjectRun), [])
        self.assertEqual(len([call for call in self.host.calls if call[0] == "POST"]), 1)

    def test_foreign_project_and_changed_configuration_never_create_run(self):
        project = self.fixture.create(user=2)
        before = len(self.host.calls)
        self.assertEqual(self.post({**self.body, "projectId": project["projectId"]})[0], 404)
        self.assertEqual(len(self.host.calls), before)
        self.assertEqual(self.post({**self.body, "configurationId": "0" * 64})[0], 409)
        self.assertEqual(self.rows(RunSubmission), [])
        self.host.offline = True
        self.assertEqual(self.post()[0], 503)
        self.assertEqual(len(self.rows(Project)), 1)

    def test_concurrent_duplicate_headers_are_not_accepted(self):
        self.assertEqual(self.request("POST", "/api/v1/runs", self.body,
            extra_headers=[(b"idempotency-key", self.key.encode()), (b"idempotency-key", str(uuid4()).encode())])[0], 422)
        self.assertEqual(self.rows(RunSubmission), [])

    def test_preflight_failure_is_not_accepted_and_can_retry_after_reconnect(self):
        self.host.offline = True
        with patch('routers.run_submissions.unavailable', wraps=unavailable) as error:
            self.assertEqual(self.post()[0], 503)
            error.assert_called_once_with(not_accepted=True)
        self.assertEqual(unavailable(not_accepted=True).headers, {'X-Submission-Status': 'NOT_ACCEPTED'})
        self.assertEqual(self.rows(RunSubmission), [])
        self.assertEqual(self.rows(Project), [])
        self.assertFalse(any(call[0] == 'POST' for call in self.host.calls))
        self.host.offline = False
        self.assertEqual(self.post()[0], 201)
        self.assertEqual(len([call for call in self.host.calls if call[0] == 'POST']), 1)

    def test_malformed_post_receipts_remain_unknown_without_creating_links(self):
        original = self.host.exchange
        for malformed in (None, [], {'run': None}, {'run': {'runId': 123}}, {'run': {'runId': str(uuid4())}}):
            with self.subTest(receipt=malformed):
                def exchange(method, path, **options):
                    if method == 'POST':
                        return malformed
                    return original(method, path, **options)
                with patch.object(self.host, 'exchange', side_effect=exchange):
                    status, result = self.post(key=str(uuid4()))
                self.assertEqual((status, result['submissionStatus']), (202, 'UNKNOWN'))
        self.assertEqual(self.rows(ProjectRun), [])

    def test_malformed_recovery_does_not_turn_an_uncertain_run_into_success(self):
        self.host.timeout = True
        self.post()
        record = self.rows(RunSubmission)[0]
        for malformed in (None, [], {'run': None}, {'run': {'runId': 123}}):
            self.host.responses[record.internal_key]['originalResponse'] = malformed
            status, result = self.request('GET', '/api/v1/submissions/' + self.key)
            self.assertEqual((status, result['submissionStatus']), (200, 'UNKNOWN'))
        self.assertEqual(self.rows(ProjectRun), [])
        self.assertEqual(len([call for call in self.host.calls if call[0] == 'POST']), 1)

    def test_non_finite_configuration_is_unavailable_without_host_post(self):
        self.host.configuration['bad'] = float('nan')
        def exchange(method, path, **_options):
            self.assertEqual(method, 'GET')
            return {'scenarioId': self.fixture.scenario_id, 'configurationId': '0' * 64,
                    'configuration': self.host.configuration}
        with patch.object(self.host, 'exchange', side_effect=exchange):
            self.assertEqual(self.post()[0], 503)
        self.assertEqual(self.rows(RunSubmission), [])

    def test_missing_scenario_registry_blocks_safely_without_host_io(self):
        with patch('routers.run_submissions.is_supported_project_scenario', side_effect=ScenarioRegistryUnavailable('private path')):
            status, data = self.request('GET', '/api/v1/host/approved-configuration?scenarioId=' + self.fixture.scenario_id)
            self.assertEqual(status, 503)
            self.assertNotIn('private path', str(data))
            self.assertEqual(self.post()[0], 503)
        self.assertEqual(self.host.calls, [])
        self.assertEqual(self.rows(RunSubmission), [])
