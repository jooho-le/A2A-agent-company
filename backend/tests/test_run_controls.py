"""Product ownership and Host service credentials remain on the server."""
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy.orm import Session

import main
import test_projects_api as fixtures
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
