"""Product ownership/idempotency/parent binding, ASGI + Mock Host only."""
from uuid import uuid4
from unittest.mock import patch
import unittest

import test_office_service as fixtures
from host_reads import HostResponse, HostRejected
from models import ProjectRun, RunSubmission


class ContinuationProductTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.OfficeServiceTests('runTest')
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.post, self.host, self.rows = self.fixture.post, self.fixture.host, self.fixture.rows
        _, self.parent = self.post()
        self.body = {**self.fixture.body, 'projectId':self.parent['projectId'],
            'continuationOf': self.parent['run']['runId'], 'requestText':'기록된 오류를 수정하고 재검증해줘.'}

    def host_with_parent(self, method, path, **kw):
        result = self.original(method, path, **kw)
        if method == 'POST':
            payload = {**result.payload, 'continuationOf': kw['body']['continuationOf']}
            self.host.responses[kw['key']]['originalResponse'] = payload
            return HostResponse(result.status, payload, result.replayed)
        return result

    def test_same_project_child_is_linked_once_and_replays_without_new_host_post(self):
        self.original = self.host.exchange
        key = str(uuid4())
        with patch.object(self.host, 'exchange', side_effect=self.host_with_parent):
            status, child = self.post(self.body, key)
            assert (status, child['submissionStatus']) == (201, 'CREATED')
            assert child['continuationOf'] == self.parent['run']['runId']
            assert child['projectId'] == self.parent['projectId']
            count = len(self.host.calls)
            assert self.post(self.body, key) == (200, child)
            assert len(self.host.calls) == count
        links = self.rows(ProjectRun)
        assert len(links) == 2 and {r.project_id for r in links} == {child['projectId']}
        stored = next(r for r in self.rows(RunSubmission) if r.client_key == key)
        assert self.parent['run']['runId'] in stored.host_request_json

    def test_wrong_owner_wrong_project_and_missing_project_stop_before_host_io(self):
        before = len(self.host.calls)
        assert self.post(self.body, str(uuid4()), user=2)[0] == 404
        assert self.post({**self.body, 'continuationOf':str(uuid4())}, str(uuid4()))[0] == 404
        no_project = {k:v for k,v in self.body.items() if k != 'projectId'}
        assert self.post(no_project, str(uuid4()))[0] == 422
        assert len(self.host.calls) == before
        assert len(self.rows(ProjectRun)) == 1

    def test_unbound_created_receipt_is_not_selected_or_linked(self):
        # The ordinary Mock receipt deliberately omits continuationOf.
        status, child = self.post(self.body, str(uuid4()))
        assert (status, child['submissionStatus']) == (202, 'UNKNOWN')
        assert len(self.rows(ProjectRun)) == 1

    def test_confirmed_rejection_is_safe_to_repair_without_an_unknown_request_loop(self):
        self.original = self.host.exchange
        def reject(method, path, **kw):
            if method == 'POST': raise HostRejected(409, 'CONTINUATION_ALREADY_ACTIVE')
            return self.original(method, path, **kw)
        key = str(uuid4())
        with patch.object(self.host, 'exchange', side_effect=reject):
            status, data = self.post(self.body, key)
            assert (status, data['submissionStatus'], data['detail']) == (409, 'REJECTED', 'CONTINUATION_ALREADY_ACTIVE')
            assert self.post(self.body, key) == (status, data)
        assert len(self.rows(ProjectRun)) == 1
