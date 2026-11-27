"""미접수 키 정리·늦은 POST 차단을 임시 DB/Mock Host에서 검증합니다."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread, main_thread
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

import test_office_service as office
from models import Project, RetiredSubmissionKey, RunSubmission
from routers import run_submissions


class SubmissionRetirementTests(unittest.TestCase):
    def setUp(self):
        self.fixture = office.OfficeServiceTests(methodName="runTest")
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.key = self.fixture.key
        self.path = "/api/v1/submissions/" + self.key + "/retire"

    def retire(self, user=1):
        return self.fixture.request("POST", self.path, {}, user=user)

    def test_missing_lookup_does_not_unlock_until_explicit_retirement(self):
        self.assertEqual(self.fixture.request("GET", self.path.removesuffix("/retire"))[0], 404)
        self.assertEqual(self.fixture.rows(RetiredSubmissionKey), [])
        expected = (200, {"requestId": self.key, "outcome": "RETIRED"})
        self.assertEqual(self.retire(), expected)
        self.assertEqual(self.retire(), expected)
        self.assertEqual(len(self.fixture.rows(RetiredSubmissionKey)), 1)
        self.assertEqual(self.fixture.host.calls, [])
        self.assertEqual(self.fixture.rows(Project), [])
        self.assertEqual(self.fixture.post(), (409, {"detail": "SUBMISSION_KEY_RETIRED"}))
        self.assertEqual(self.fixture.host.calls, [])
        self.assertEqual(self.fixture.post(key=str(uuid4()))[0], 201)
        self.assertEqual(len([call for call in self.fixture.host.calls if call[0] == "POST"]), 1)

    def test_created_and_unknown_receipts_are_preserved(self):
        for timeout in (False, True):
            key = str(uuid4())
            self.fixture.host.timeout = timeout
            _, receipt = self.fixture.post(key=key)
            before = len(self.fixture.host.calls)
            status, result = self.fixture.request("POST", "/api/v1/submissions/" + key + "/retire", {})
            self.assertEqual(status, 200)
            self.assertEqual(result, {"requestId": key, "outcome": "EXISTING", "submission": receipt})
            self.assertEqual(self.fixture.rows(RetiredSubmissionKey), [])
            self.assertEqual(len(self.fixture.host.calls), before)

    def test_retired_keys_are_scoped_to_authenticated_owner(self):
        self.assertEqual(self.retire(user=None)[0], 401)
        self.assertEqual(self.fixture.rows(RetiredSubmissionKey), [])
        self.assertEqual(self.retire()[1]["outcome"], "RETIRED")
        # Same random key used by another account must not share or expose receipts.
        _, created = self.fixture.post(user=2)
        self.assertEqual(created["submissionStatus"], "CREATED")
        self.assertEqual(self.retire()[1]["outcome"], "RETIRED")
        self.assertEqual(self.fixture.post(user=2)[1]["run"]["runId"], created["run"]["runId"])
        self.assertEqual(self.fixture.request("POST", self.path, {"force": True})[0], 422)

    def test_retirement_waits_for_in_flight_receipt_transaction(self):
        project = self.fixture.fixture.create()
        entered = Event()
        original = run_submissions.lock_submission_keys

        def observed_lock(db):
            if current_thread() is not main_thread():
                entered.set()
            return original(db)

        with ThreadPoolExecutor(max_workers=1) as pool, Session(self.fixture.fixture.engine) as db:
            original(db)
            with patch.object(run_submissions, "lock_submission_keys", observed_lock):
                future = pool.submit(self.retire)
                try:
                    self.assertTrue(entered.wait(2), "retirement reached the write-lock boundary")
                    self.assertFalse(future.done(), "retirement must wait for the in-flight writer")
                    db.add(RunSubmission(owner_user_id=1, client_key=self.key, project_id=project["projectId"],
                        request_sha256="a" * 64, host_request_sha256="b" * 64,
                        host_request_json="{}", configuration_id="c" * 64, state="PENDING"))
                    db.commit()
                finally:
                    db.rollback()
                status, result = future.result(timeout=5)
        self.assertEqual((status, result["outcome"], result["submission"]["submissionStatus"]),
                         (200, "EXISTING", "PENDING"))
        self.assertEqual(self.fixture.rows(RetiredSubmissionKey), [])
        self.assertEqual(self.fixture.host.calls, [])
