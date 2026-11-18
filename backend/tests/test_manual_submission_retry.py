"""명시적 수동 재시도만 Mock Host·임시 SQLite에서 검사합니다."""

import json
import os
import unittest
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from threading import Barrier, Event, local
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import Session

import test_office_service as fixtures
from host_reads import HostAuthenticationFailed, HostRejected, HostResponse, HostRunNotFound, HostUnavailable
from models import ProjectRun, RunSubmission
from routers.run_submissions import digest


class ManualSubmissionRetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SubmissionRecoveryRetryTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.request = self.fixture.request
        self.host = self.fixture.host
        self.engine = self.fixture.fixture.fixture.engine
        self.path = self.fixture.path + "/retry"
        self.original = self.row().host_request_json
        enabled = patch.dict(os.environ, {"A2A_HOST_MANUAL_RETRY_ENABLED": "true"})
        enabled.start()
        self.addCleanup(enabled.stop)

    def row(self):
        with Session(self.engine) as db:
            return db.get(RunSubmission, self.fixture.internal_key)

    def reset(self, state="UNKNOWN", response=None):
        # 이 테스트 전용 임시 DB만 초기화합니다.
        with Session(self.engine) as db:
            db.execute(delete(ProjectRun))
            row = db.get(RunSubmission, self.fixture.internal_key)
            row.state, row.response_json, row.manual_retry_claimed_at = state, response, None
            row.host_request_json = self.original
            db.commit()
        self.fixture.sleep.reset_mock()

    def post(self, body=None, **kwargs):
        return self.request("POST", self.path, {} if body is None else body, **kwargs)

    def assert_frozen(self):
        row = self.row()
        self.assertEqual(row.host_request_json, self.original)
        self.assertEqual(row.host_request_sha256, digest(json.loads(self.original)))
        self.assertEqual((row.internal_key, row.client_key, row.request_sha256),
                         (self.fixture.internal_key, self.fixture.client_key, "a" * 64))
        self.assertEqual(self.fixture.fixture.fixture.legacy_snapshot(),
                         self.fixture.fixture.fixture.legacy_before)

    def test_manual_retry_preserves_original_body_key_and_receipt_mapping(self):
        # GET으로 확인되면 POST 없이 복구합니다.
        with patch.object(self.host, "exchange", return_value=self.fixture.valid) as exchange:
            status, data = self.post()
            self.assertEqual((status, data["submissionStatus"]), (200, "CREATED"))
            exchange.assert_called_once_with("GET", self.fixture.upstream_path)
            self.assertIsNone(self.row().manual_retry_claimed_at)
        # 현재 설정이 바뀌었어도 조회/재구성하지 않고 보관된 설정을 그대로 씁니다.
        self.host.configuration = {"changed": "not-approved"}
        for status, replayed in ((201, "false"), (200, "true")):
            with self.subTest(status=status):
                self.reset()
                expected = HostResponse(status, self.fixture.valid["originalResponse"], replayed)
                with patch.object(self.host, "exchange", side_effect=[HostRunNotFound()] * 4 + [expected]) as exchange:
                    result_status, data = self.post()
                    self.assertEqual((result_status, data["submissionStatus"], data["run"]["runId"]),
                                     (status, "CREATED", self.fixture.run_id))
                    self.assertEqual(data["dispatchStatus"], "SCHEDULED")
                    self.assertEqual([call.args for call in exchange.call_args_list[:4]],
                                     [("GET", self.fixture.upstream_path)] * 4)
                    self.assertEqual([call.args for call in self.fixture.sleep.call_args_list], [(2,), (4,), (8,)])
                    self.assertEqual(exchange.call_args.args, ("POST", "/api/v1/runs"))
                    self.assertEqual(exchange.call_args.kwargs,
                                     {"body": json.loads(self.original), "key": self.fixture.internal_key,
                                      "include_status": True})
                    self.assertIsNotNone(self.row().manual_retry_claimed_at)
                    self.assertEqual(self.post()[1]["run"]["runId"], self.fixture.run_id)
                    self.assertEqual(exchange.call_count, 5)
                self.fixture.assert_state("CREATED", 1)
                self.assert_frozen()

    def test_concurrent_duplicate_changed_body_and_terminal_requests_never_resend(self):
        with patch.object(self.host, "exchange") as exchange:
            self.assertEqual(self.post({"requestText": "변경된 업무"})[0], 422)
            self.assertEqual(self.post({"configuration": {}})[0], 422)
            self.assertEqual(self.post(user=None)[0], 401)
            self.assertEqual(self.post(user=2)[0], 404)
            for setting in ({"A2A_HOST_MANUAL_RETRY_ENABLED": "false"},
                            {"A2A_HOST_READS_ENABLED": "false"}, {"A2A_HOST_API_TOKEN": ""}):
                with patch.dict(os.environ, setting):
                    self.assertEqual(self.post()[0], 503)
            for state in ("PENDING", "CREATED", "REJECTED"):
                self.reset(state)
                self.assertEqual(self.post()[1]["submissionStatus"], state)
            self.reset()
            with Session(self.engine) as db:
                row = db.get(RunSubmission, self.fixture.internal_key)
                changed = json.loads(row.host_request_json)
                changed["requestText"] = "변조된 원문"
                row.host_request_json = json.dumps(changed)
                db.commit()
            self.assertEqual(self.post()[0], 503)
            self.assertIsNone(self.row().manual_retry_claimed_at)
            exchange.assert_not_called()
        self.reset()
        started, release = Event(), Event()
        queried, counts = Barrier(2), local()

        def pending_post(method, path, **kwargs):
            if method == "GET":
                counts.lookups = getattr(counts, "lookups", 0) + 1
                if counts.lookups == 4:
                    queried.wait(timeout=5)  # 두 요청 모두 claim 이전 UNKNOWN을 읽게 합니다.
                raise HostRunNotFound()
            started.set()
            if not release.wait(5):
                raise AssertionError("Mock POST was not released")
            raise HostUnavailable()  # 유실 후에도 claim이 유지돼 두 번째 POST가 없어야 합니다.

        with patch.object(self.host, "exchange", side_effect=pending_post) as exchange:
            with ThreadPoolExecutor(max_workers=2) as workers:
                attempts = [workers.submit(self.post), workers.submit(self.post)]
                try:
                    self.assertTrue(started.wait(5))
                    done, _ = wait(attempts, timeout=5, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    duplicate = next(iter(done)).result()
                    self.assertEqual((duplicate[0], duplicate[1]["submissionStatus"]), (202, "UNKNOWN"))
                finally:
                    release.set()
                self.assertEqual([attempt.result(timeout=5)[0] for attempt in attempts], [202, 202])
            # 별도 요청·DB 세션에서도 영속 claim으로 재전송을 차단합니다.
            self.assertEqual(self.post()[0], 202)
            self.assertEqual(sum(call.args[0] == "POST" for call in exchange.call_args_list), 1)
        self.fixture.assert_state("UNKNOWN", 0)
        self.assert_frozen()

    def test_loss_conflicts_and_mismatched_identifiers_remain_safe(self):
        # 잘못된 접수·인증 실패·연결 불명확 상태에서는 POST를 하지 않습니다.
        for failure in (HostUnavailable(), HostAuthenticationFailed(),
                        {**self.fixture.valid, "runId": str(uuid4())},
                        {**self.fixture.valid, "requestSha256": "b" * 64},
                        {**self.fixture.valid, "internalKey": str(uuid4())}):
            self.reset()
            with patch.object(self.host, "exchange", side_effect=[failure]) as exchange:
                status, _ = self.post()
                self.assertEqual(status, 502 if isinstance(failure, HostAuthenticationFailed) else 202)
                exchange.assert_called_once_with("GET", self.fixture.upstream_path)
            self.fixture.assert_state("UNKNOWN", 0)
            self.assertIsNone(self.row().manual_retry_claimed_at)
        mismatch = deepcopy(self.fixture.valid["originalResponse"])
        mismatch["firstStep"]["runId"] = str(uuid4())
        for outcome, status, state in (
            (HostUnavailable(), 202, "UNKNOWN"),
            (HostResponse(200, mismatch, "true"), 202, "UNKNOWN"),
            (HostResponse(201, self.fixture.valid["originalResponse"], None), 202, "UNKNOWN"),
            (HostRejected(409), 202, "UNKNOWN"),
            (HostRejected(409, "IDEMPOTENCY_KEY_CONFLICT"), 409, "REJECTED"),
        ):
            with self.subTest(outcome=type(outcome).__name__, state=state):
                self.reset()
                with patch.object(self.host, "exchange", side_effect=[HostRunNotFound()] * 4 + [outcome]) as exchange:
                    result_status, data = self.post()
                    self.assertEqual((result_status, data["submissionStatus"]), (status, state))
                    self.assertNotIn("run", data)
                    self.assertIsNotNone(self.row().manual_retry_claimed_at)
                    self.post()  # 실패·충돌 후 중복 클릭도 POST를 보내지 않습니다.
                    self.assertEqual(exchange.call_count, 5)
                self.fixture.assert_state(state, 0)
                self.assert_frozen()
        # 재전송 응답이 불명확해도 기존 GET 복구로 같은 Run에 연결할 수 있습니다.
        self.reset()
        with patch.object(self.host, "exchange", side_effect=[HostRunNotFound()] * 4 + [HostUnavailable()]):
            self.assertEqual(self.post()[0], 202)
        with patch.object(self.host, "exchange", return_value=self.fixture.valid) as exchange:
            self.assertEqual(self.request("GET", self.fixture.path)[1]["submissionStatus"], "CREATED")
            exchange.assert_called_once_with("GET", self.fixture.upstream_path)
        self.fixture.assert_state("CREATED", 1)
