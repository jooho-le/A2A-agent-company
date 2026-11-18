"""임시 SQLite와 ASGI에서 실제 제품 라우트/소유권/접수 복구를 검증합니다."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch
from copy import deepcopy
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

import test_projects_api as fixtures
import main
import host_reads
from approved_configuration import _host_contract
from host_reads import HostResponse, HostRunNotFound, HostUnavailable, get_host_read_client
from models import Project, ProjectRun, RunSubmission
from project_scenarios import ScenarioRegistryUnavailable
from routers.run_submissions import digest, unavailable


class HostSubmissionContractTests(unittest.TestCase):
    """실제 제품 전송기를 Mock HTTP 응답과 연결합니다. 실제 네트워크/DB는 사용하지 않습니다."""

    def setUp(self):
        self.fixture = fixtures.ProjectAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        waiting = patch("routers.run_submissions.sleep")
        self.sleep = waiting.start()
        self.addCleanup(waiting.stop)
        self.request = self.fixture.request
        environment = patch.dict(os.environ, {
            "A2A_HOST_READS_ENABLED": "true", "A2A_HOST_BASE_URL": "http://127.0.0.1:8002",
            "A2A_HOST_API_TOKEN": "mock-host-service-token-for-tests-only",
        })
        environment.start()
        self.addCleanup(environment.stop)
        self.connection = MagicMock()
        self.connection.getresponse.side_effect = self.http_response
        for name, options in (("HTTPConnection", {"return_value": self.connection}),
                              ("HTTPSConnection", {"side_effect": AssertionError("Unexpected HTTPS")})):
            mock = patch.object(host_reads.http.client, name, **options)
            mock.start()
            self.addCleanup(mock.stop)
        self.configuration = Host(self.fixture.scenario_id).configuration
        self.body = {"projectName": "Mock 접수", "scenarioId": self.fixture.scenario_id,
                     "configurationId": digest(self.configuration), "requestText": "테스트용 요청"}
        self.upstream_status = 201
        self.replayed = "false"
        self.error = {"detail": "IDEMPOTENCY_KEY_CONFLICT"}
        self.run_id = str(uuid4())
        self.recovery = None

    def run_receipt(self):
        return {"run": {"runId": self.run_id, "scenarioId": self.fixture.scenario_id, "status": "RECEIVED"},
                "firstStep": {"runId": self.run_id, "workflowStepId": str(uuid4())}, "dispatchStatus": "SCHEDULED"}

    def http_response(self):
        method, path = self.connection.request.call_args.args
        response = MagicMock()
        response.status = self.upstream_status if method == "POST" else 200
        headers = {"Content-Type": "application/json", "Idempotency-Replayed": self.replayed}
        response.getheader.side_effect = lambda name, default=None: headers.get(name, default)
        if method == "POST":
            payload = self.error if self.upstream_status == 409 else self.run_receipt()
        elif path.startswith("/api/v1/submissions/"):
            payload = self.recovery
        else:
            self.assertTrue(path.startswith("/api/v1/host/approved-configuration?"))
            payload = {"scenarioId": self.fixture.scenario_id, "configurationId": digest(self.configuration),
                       "configuration": self.configuration}
        response.read.return_value = json.dumps(payload).encode()
        return response

    def post(self, key):
        return self.request("POST", "/api/v1/runs", self.body,
                            extra_headers=[(b"idempotency-key", key.encode())])

    def rows(self, cls):
        with Session(self.fixture.engine) as db:
            return db.scalars(select(cls)).all()

    def test_new_201_and_replayed_200_keep_receipt_and_unique_link(self):
        first_key = str(uuid4())
        status, first = self.post(first_key)
        self.assertEqual((status, first["submissionStatus"]), (201, "CREATED"))
        self.assertTrue({"run", "firstStep", "dispatchStatus"}.issubset(first))
        # Host에서 이미 존재하는 Run을 반환하면 기존 프로젝트 연결을 재사용합니다.
        self.body["projectId"] = first["projectId"]
        self.upstream_status, self.replayed = 200, "true"
        status, reused = self.post(str(uuid4()))
        self.assertEqual((status, reused["run"]["runId"]), (200, first["run"]["runId"]))
        self.assertEqual(len(self.rows(ProjectRun)), 1)
        self.assertEqual(len(self.rows(RunSubmission)), 2)
        self.replayed = None
        status, uncertain = self.post(str(uuid4()))
        self.assertEqual((status, uncertain["submissionStatus"]), (202, "UNKNOWN"))
        self.assertEqual(len(self.rows(ProjectRun)), 1)

    def test_only_confirmed_409_is_rejected_and_transport_failure_stays_unknown(self):
        self.upstream_status = 409
        key = str(uuid4())
        status, rejected = self.post(key)
        self.assertEqual((status, rejected["submissionStatus"], rejected["detail"]),
                         (409, "REJECTED", "IDEMPOTENCY_KEY_CONFLICT"))
        before = self.connection.request.call_count
        self.assertEqual(self.post(key), (409, rejected))
        self.assertEqual(self.connection.request.call_count, before)
        self.error = {"detail": "unrecognized private upstream error"}
        status, uncertain = self.post(str(uuid4()))
        self.assertEqual((status, uncertain["submissionStatus"]), (202, "UNKNOWN"))
        self.assertNotIn("private", json.dumps(uncertain))
        for failure in (TimeoutError("private timeout"), ConnectionResetError("private reset")):
            def fail_post(method, _path, **_options):
                if method == "POST":
                    raise failure
            self.connection.request.side_effect = fail_post
            status, uncertain = self.post(str(uuid4()))
            self.assertEqual((status, uncertain["submissionStatus"]), (202, "UNKNOWN"))
        self.assertEqual(len(self.rows(ProjectRun)), 0)
        self.assertEqual(sorted(row.state for row in self.rows(RunSubmission)), ["REJECTED", "UNKNOWN", "UNKNOWN", "UNKNOWN"])

    def test_recovery_identifiers_must_agree_before_link_or_created(self):
        _, known = self.post(str(uuid4()))
        known_link = self.rows(ProjectRun)[0]
        self.run_id = str(uuid4())
        def fail_post(method, _path, **_options):
            if method == "POST":
                raise TimeoutError()
        self.connection.request.side_effect = fail_post
        key = str(uuid4())
        self.assertEqual(self.post(key)[0], 202)
        self.connection.request.side_effect = None
        row = next(item for item in self.rows(RunSubmission) if item.client_key == key)
        original = self.run_receipt()
        valid = {"internalKey": row.internal_key, "requestSha256": row.host_request_sha256,
                 "status": "CREATED", "runId": self.run_id, "originalResponse": original, "run": original["run"]}
        invalid = [None, {**valid, "internalKey": str(uuid4())}, {**valid, "requestSha256": "0" * 64},
                   {**valid, "runId": str(uuid4())}, {k: v for k, v in valid.items() if k != "runId"},
                   {**valid, "run": {"runId": str(uuid4())}}]
        wrong_original = deepcopy(valid)
        wrong_original["originalResponse"]["run"]["runId"] = str(uuid4())
        invalid.append(wrong_original)
        wrong_step = deepcopy(valid)
        wrong_step["originalResponse"]["firstStep"]["runId"] = str(uuid4())
        invalid.append(wrong_step)
        for self.recovery in invalid:
            status, data = self.request("GET", "/api/v1/submissions/" + key)
            self.assertEqual((status, data["submissionStatus"]), (200, "UNKNOWN"))
            self.assertNotIn("run", data)
            saved = next(item for item in self.rows(RunSubmission) if item.client_key == key)
            self.assertEqual((saved.state, saved.response_json), ("UNKNOWN", None))
            self.assertEqual([(link.id, link.project_id, link.run_id) for link in self.rows(ProjectRun)],
                             [(known_link.id, known_link.project_id, known["run"]["runId"])])
        self.recovery = valid
        self.assertEqual(self.request("GET", "/api/v1/submissions/" + key)[1]["submissionStatus"], "CREATED")
        self.assertEqual(len(self.rows(ProjectRun)), 2)
        self.assertEqual(sum(call.args[0] == "POST" for call in self.connection.request.call_args_list), 2)


class Host:
    def __init__(self, scenario_id):
        self.scenario_id = scenario_id
        self.calls = []
        self.responses = {}
        self.timeout = False
        self.offline = False
        schema, _ = _host_contract()
        self.configuration = schema.model_validate({
            "architecture": "MULTI_AGENT",
            "model": {"provider": "openai", "modelId": "test-only", "temperature": 0.0},
        }).model_dump(mode="json", by_alias=True)

    def exchange(self, method, path, *, body=None, key=None, include_status=False):
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
            self.responses[key] = {"internalKey": key, "status": "CREATED", "requestSha256": digest(body),
                                   "runId": result["run"]["runId"], "run": result["run"], "originalResponse": result}
            if self.timeout:
                raise HostUnavailable()
            return HostResponse(201, result, "false") if include_status else result
        key = path.rsplit("/", 1)[-1]
        if key not in self.responses:
            raise HostRunNotFound()
        return self.responses[key]


class OfficeServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectAPITests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        waiting = patch("routers.run_submissions.sleep")
        self.sleep = waiting.start()
        self.addCleanup(waiting.stop)
        environment = patch.dict(os.environ, {
            "A2A_HOST_READS_ENABLED": "true", "A2A_HOST_API_TOKEN": "mock-host-service-token-for-tests-only",
        })
        environment.start()
        self.addCleanup(environment.stop)
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

    @patch.dict(os.environ, {"A2A_HOST_READS_ENABLED": "true"})
    def test_jwt_precedes_host_and_only_registered_approval_is_returned(self):
        # 승인 GET은 Host가 직렬화한 전체 공개 스키마(null·기본값 포함)를 요구합니다.
        schema, _ = _host_contract()
        self.host.configuration = schema.model_validate({
            **self.host.configuration, "model": {**self.host.configuration["model"], "temperature": 0.0},
        }).model_dump(mode="json", by_alias=True)
        self.assertEqual(self.request("GET", "/api/v1/scenarios", user=None)[0], 401)
        self.assertEqual(self.host.calls, [])
        status, result = self.request("GET", "/api/v1/scenarios")
        self.assertEqual(status, 200)
        self.assertEqual(result["scenarios"], [{"scenarioId": self.fixture.scenario_id, "name": "회원가입"}])
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
        self.assertEqual(sent[0][2]["configuration"], self.host.configuration)
        self.assertNotEqual(sent[0][3], self.key)
        self.assertEqual(len(self.rows(ProjectRun)), 1)
        self.assertEqual(self.rows(Project)[0].name, self.body["projectName"])
        self.assertEqual(self.post({**self.body, "requestText": "다른 요청"})[0], 409)
        before = len(self.host.calls)
        self.assertEqual(self.request("GET", "/api/v1/submissions/" + self.key, user=2)[0], 404)
        self.assertEqual(len(self.host.calls), before)
        self.assertEqual(self.fixture.legacy_snapshot(), self.fixture.legacy_before)

    def test_post_preflight_blocks_disabled_host_and_missing_token(self):
        for setting in ({"A2A_HOST_READS_ENABLED": "false"}, {"A2A_HOST_READS_ENABLED": ""},
                        {"A2A_HOST_API_TOKEN": ""}, {"A2A_HOST_API_TOKEN": "short-invalid-token"}):
            with self.subTest(setting=list(setting)), patch.dict(os.environ, setting):
                self.assertEqual(self.post()[0], 503)
                self.assertEqual(self.host.calls, [])
                self.assertEqual(self.rows(RunSubmission), [])
                self.assertEqual(self.rows(Project), [])
                self.assertEqual(self.rows(ProjectRun), [])

    def test_post_preflight_blocks_changed_and_unsafe_configuration(self):
        self.assertEqual(self.post({**self.body, "configurationId": "0" * 64}),
                         (409, {"detail": "CONFIGURATION_CHANGED"}))
        self.host.calls.clear()
        # 요청 계약 밖의 Configuration은 승인 조회 전 거부합니다.
        self.assertEqual(self.post({**self.body, "configuration": self.host.configuration})[0], 422)
        self.assertEqual(self.host.calls, [])
        safe = deepcopy(self.host.configuration)
        for value in ("sk-test-only-private-value", "/srv/private/runtime.json", "Bearer mock-role-token"):
            self.host.configuration = deepcopy(safe)
            self.host.configuration["model"]["modelRevision"] = value
            # 해시까지 일치해도 안전하지 않은 설정은 거부해야 합니다.
            self.assertEqual(self.post({**self.body, "configurationId": digest(self.host.configuration)})[0], 503)
        self.host.configuration = deepcopy(safe)
        self.host.configuration["apiKey"] = "test-only-private-value"
        self.assertEqual(self.post({**self.body, "configurationId": digest(self.host.configuration)})[0], 503)
        for invalid in (None, {"scenarioId": self.fixture.scenario_id, "configurationId": "0" * 64,
                               "configuration": safe}):
            with patch.object(self.host, "exchange", return_value=invalid):
                self.assertEqual(self.post()[0], 503)
        self.assertFalse(any(call[0] == "POST" for call in self.host.calls))
        self.assertEqual(self.rows(RunSubmission), [])
        self.assertEqual(self.rows(Project), [])
        self.assertEqual(self.rows(ProjectRun), [])

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


class SubmissionRecoveryRetryTests(unittest.TestCase):
    def setUp(self):
        # 기존 임시 DB fixture만 재사용하며 Run POST 없이 접수 기록을 준비합니다.
        self.fixture = OfficeServiceTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request, self.host, self.sleep = self.fixture.request, self.fixture.host, self.fixture.sleep
        self.internal_key, self.client_key, self.run_id = (str(uuid4()) for _ in range(3))
        host_body = {"scenarioId": self.fixture.fixture.scenario_id, "requestText": "Mock 복구 요청",
                     "configuration": self.host.configuration}
        self.request_hash = digest(host_body)
        with Session(self.fixture.fixture.engine) as db:
            project = Project(owner_user_id=1, name="Mock 복구", scenario_id=host_body["scenarioId"])
            db.add(project)
            db.flush()
            self.project_id = project.project_id
            db.add(RunSubmission(internal_key=self.internal_key, owner_user_id=1, client_key=self.client_key,
                project_id=self.project_id, request_sha256="a" * 64, host_request_sha256=self.request_hash,
                host_request_json=json.dumps(host_body), configuration_id=digest(self.host.configuration), state="UNKNOWN"))
            db.commit()
        original = {"run": {"runId": self.run_id, "scenarioId": host_body["scenarioId"]},
                    "firstStep": {"runId": self.run_id}, "dispatchStatus": "SCHEDULED"}
        self.valid = {"status": "CREATED", "internalKey": self.internal_key, "requestSha256": self.request_hash,
                      "runId": self.run_id, "originalResponse": original, "run": original["run"]}
        self.path = "/api/v1/submissions/" + self.client_key
        self.upstream_path = "/api/v1/submissions/" + self.internal_key

    def assert_state(self, state, links):
        records = self.fixture.rows(RunSubmission)
        self.assertEqual(len(records), 1)
        row = records[0]
        self.assertEqual((row.state, row.internal_key, row.client_key, row.host_request_sha256, row.request_sha256),
                         (state, self.internal_key, self.client_key, self.request_hash, "a" * 64))
        self.assertEqual(digest(json.loads(row.host_request_json)), self.request_hash)
        self.assertEqual(len(self.fixture.rows(Project)), 1)
        self.assertEqual(len(self.fixture.rows(ProjectRun)), links)
        if state == "UNKNOWN":
            self.assertIsNone(row.response_json)

    def test_unknown_retries_same_get_and_safely_links_existing_run(self):
        with patch.object(self.host, "exchange", side_effect=[HostRunNotFound(), HostUnavailable(), self.valid]) as exchange:
            status, data = self.request("GET", self.path)
            self.assertEqual((status, data["submissionStatus"], data["run"]["runId"]), (200, "CREATED", self.run_id))
            self.assertEqual([call.args for call in exchange.call_args_list], [("GET", self.upstream_path)] * 3)
            self.assertEqual([call.args for call in self.sleep.call_args_list], [(2,), (4,)])
            self.assert_state("CREATED", 1)
            self.assertEqual(self.request("GET", self.path), (200, data))
            self.assertEqual(exchange.call_count, 3)
            self.assertEqual(self.fixture.rows(ProjectRun)[0].run_id, self.run_id)

    def test_retry_limit_preserves_unknown_without_post_or_new_keys(self):
        for failure in (HostRunNotFound(), HostUnavailable()):
            self.sleep.reset_mock()
            with patch.object(self.host, "exchange", side_effect=failure) as exchange:
                status, data = self.request("GET", self.path)
                self.assertEqual((status, data["submissionStatus"]), (200, "UNKNOWN"))
                self.assertNotIn("run", data)
                self.assertEqual([call.args for call in exchange.call_args_list], [("GET", self.upstream_path)] * 4)
                self.assertEqual([call.args for call in self.sleep.call_args_list], [(2,), (4,), (8,)])
                self.assert_state("UNKNOWN", 0)

    def test_auth_rejected_and_mismatched_run_never_restore(self):
        with patch.object(self.host, "exchange", return_value=self.valid) as exchange:
            self.assertEqual(self.request("GET", self.path, user=None)[0], 401)
            self.assertEqual(self.request("GET", self.path, user=2)[0], 404)
            for setting in ({"A2A_HOST_READS_ENABLED": "false"}, {"A2A_HOST_API_TOKEN": ""}):
                with patch.dict(os.environ, setting):
                    self.assertEqual(self.request("GET", self.path)[0], 503)
            exchange.assert_not_called()
            self.sleep.assert_not_called()
        with patch.object(self.host, "exchange", side_effect=host_reads.HostAuthenticationFailed()) as exchange:
            self.assertEqual(self.request("GET", self.path), (502, {"detail": "Host 서비스 인증에 실패했습니다."}))
            exchange.assert_called_once_with("GET", self.upstream_path)
            self.sleep.assert_not_called()
        self.assert_state("UNKNOWN", 0)
        with patch.object(self.host, "exchange", return_value={**self.valid, "runId": str(uuid4())}) as exchange:
            self.assertEqual(self.request("GET", self.path)[1]["submissionStatus"], "UNKNOWN")
            exchange.assert_called_once_with("GET", self.upstream_path)
            self.sleep.assert_not_called()
        self.assert_state("UNKNOWN", 0)
        # 대기 도중 비활성으로 바뀌어도 추가 Host GET은 보내지 않습니다.
        with patch.dict(os.environ), patch.object(self.host, "exchange", side_effect=HostRunNotFound()) as exchange:
            self.sleep.side_effect = lambda _delay: os.environ.__setitem__("A2A_HOST_READS_ENABLED", "false")
            self.assertEqual(self.request("GET", self.path)[0], 503)
            exchange.assert_called_once_with("GET", self.upstream_path)
        self.sleep.side_effect = None
        self.sleep.reset_mock()
        with Session(self.fixture.fixture.engine) as db:
            row = db.get(RunSubmission, self.internal_key)
            row.state = "REJECTED"
            row.response_json = json.dumps({"detail": "IDEMPOTENCY_KEY_CONFLICT"})
            db.commit()
        with patch.object(self.host, "exchange") as exchange:
            self.assertEqual(self.request("GET", self.path)[1]["submissionStatus"], "REJECTED")
            exchange.assert_not_called()
            self.sleep.assert_not_called()
        self.assert_state("REJECTED", 0)
