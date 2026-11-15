"""Lost-response/restart lookup and explicit recovery using private SQLite/ASGI."""

from dataclasses import replace
from hashlib import sha256
import json
import unittest
from unittest.mock import Mock, patch
from uuid import UUID, uuid4

from a2a.types import Task
import test_host_service_api as fixtures
from test_dispatch import FakePlannerClient
from orchestrator.a2a import A2AAgentRegistry
from orchestrator.api.dependencies import get_workflow_controls
from orchestrator.application import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlService
from orchestrator.domain import AgentRole, TraceEvent
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _insert_event


class RecoverableInputClient(FakePlannerClient):
    def __init__(self):
        super().__init__("TASK_STATE_INPUT_REQUIRED")
        self.saved = None
        self.polled = []

    async def send_task(self, *args, **kwargs):
        task = await super().send_task(*args, **kwargs)
        self.saved = Task()
        self.saved.CopyFrom(task)
        return task

    async def get_task(self, task_id):
        self.polled.append(task_id)
        task = Task()
        task.CopyFrom(self.saved)
        return task


class SubmissionLookupTests(unittest.IsolatedAsyncioTestCase):
    setUp = fixtures.HostServiceAPITests.setUp
    make_app = fixtures.HostServiceAPITests.make_app
    request = fixtures.HostServiceAPITests.request
    submit = fixtures.HostServiceAPITests.submit
    count = fixtures.HostServiceAPITests.count

    async def lookup(self, key, **kwargs):
        return await self.request("GET", f"/api/v1/submissions/{key}", **kwargs)

    async def pending(self):
        key = uuid4()
        # Simulate DB commit followed by loss before the process-local callback.
        with patch("starlette.background.BackgroundTasks.add_task"):
            response = await self.submit(key)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(self.dispatcher.run_ids, [])
        return key, response.json()

    def controller(self):
        client = RecoverableInputClient()
        registry = A2AAgentRegistry({AgentRole.PLANNER: "http://planner.test"})
        dispatcher = PlannerRunDispatcher(self.repository, registry, client_factory=lambda _: client)
        controls = WorkflowControlService(self.repository, registry, dispatcher, client_factory=lambda _: client)
        self.app.state.run_dispatcher = dispatcher
        self.app.dependency_overrides[get_workflow_controls] = lambda: controls
        return client

    async def test_lookup_is_authenticated_read_only_and_preserves_original_receipt(self):
        key, original = await self.pending()
        before = {table: self.count(table) for table in ("workflow_runs", "workflow_steps", "run_submissions", "trace_events")}
        response = await self.lookup(key)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "CREATED")
        self.assertEqual(body["internalKey"], str(key))
        self.assertEqual(body["originalResponse"], original)
        self.assertEqual(body["runId"], original["run"]["runId"])
        canonical = json.dumps(self.body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        self.assertEqual(body["requestSha256"], sha256(canonical.encode()).hexdigest())
        self.assertEqual(body["recovery"], {"action": "RECOVER", "reasonCode": "INITIAL_DISPATCH_PENDING",
                                            "automaticRetryAllowed": False})
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertNotIn("requestText", body["run"])
        self.assertNotIn(fixtures.TOKEN, response.text)
        self.assertNotIn(str(self.base), response.text)
        self.assertEqual(before, {table: self.count(table) for table in before})
        self.assertEqual(self.dispatcher.run_ids, [])
        self.assertEqual((await self.lookup(key, headers={})).status_code, 401)

    async def test_restart_lookup_works_without_current_configuration_and_never_reschedules(self):
        key, original = await self.pending()
        restarted = self.make_app(SQLiteWorkflowRepository(self.path))
        restarted.state.approved_configuration_provider = None
        restarted.state.submission_validator = Mock(side_effect=ValueError("private changed settings"))
        response = await self.lookup(key, app=restarted)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["originalResponse"], original)
        restarted.state.submission_validator.assert_not_called()
        self.assertEqual(self.dispatcher.run_ids, [])
        self.assertEqual(self.count("workflow_runs"), 1)

    async def test_current_run_state_and_original_creation_state_are_separate(self):
        key, original = await self.pending()
        self.repository.claim_planner_dispatch(UUID(original["run"]["runId"]))
        response = await self.lookup(key)
        self.assertEqual(response.json()["originalResponse"]["run"]["status"], "RECEIVED")
        self.assertEqual(response.json()["run"]["status"], "PLANNING")
        self.assertEqual(response.json()["recovery"]["action"], "OBSERVE")

    async def test_missing_receipt_is_404_and_corrupt_or_unavailable_receipt_is_safe_503(self):
        missing = await self.lookup(uuid4())
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["detail"], "SUBMISSION_NOT_FOUND")
        self.assertEqual((await self.lookup("not-a-uuid")).status_code, 422)
        key, _ = await self.pending()
        view = self.repository.get_submission_view(key)
        corrupted = json.loads(view.receipt.response_json)
        other_id = str(uuid4())
        corrupted["run"]["runId"] = other_id
        bad = replace(view, receipt=view.receipt.model_copy(update={"response_json": json.dumps(corrupted)}))
        for replacement in (Mock(return_value=bad), Mock(side_effect=RuntimeError(fixtures.TOKEN))):
            with patch.object(self.repository, "get_submission_view", replacement):
                response = await self.lookup(key)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertNotIn(fixtures.TOKEN, response.text)
            self.assertNotIn(other_id, response.text)

    async def test_explicit_recover_starts_pending_same_run_once_with_existing_guard(self):
        key, original = await self.pending()
        client = self.controller()
        run_id = original["run"]["runId"]
        response = await self.request("POST", f"/api/v1/runs/{run_id}/recover", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["runId"], run_id)
        self.assertEqual(response.json()["status"], "WAITING_INPUT")
        self.assertEqual(response.json()["fixAttempt"], 0)
        self.assertEqual(len(client.sent), 1)
        again = await self.request("POST", f"/api/v1/runs/{run_id}/recover", json={})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.polled, ["planner-task-opaque"])
        observed = await self.lookup(key)
        self.assertEqual(observed.json()["status"], "CREATED")
        self.assertEqual(observed.json()["run"]["status"], "WAITING_INPUT")
        self.assertEqual(observed.json()["recovery"]["action"], "REVIEW")
        self.assertEqual(self.count("workflow_runs"), 1)
        self.assertEqual(self.count("workflow_steps"), 1)

    async def test_uncertain_send_is_reported_and_explicit_recovery_cannot_replay_it(self):
        key, original = await self.pending()
        run_id = UUID(original["run"]["runId"])
        run, step = self.repository.claim_planner_dispatch(run_id)
        with self.repository._transaction() as connection:
            _insert_event(connection, TraceEvent(run_id=run_id, workflow_step_id=step.workflow_step_id,
                event_type="A2A_MESSAGE_SENT", actor="Orchestrator", attempt=step.attempt,
                workflow_state=run.status))
        client = self.controller()
        observed = await self.lookup(key)
        self.assertEqual(observed.json()["recovery"]["action"], "RECONCILE")
        response = await self.request("POST", f"/api/v1/runs/{run_id}/recover", json={})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(client.sent, [])
        replay = await self.submit(key)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(client.sent, [])
        self.assertEqual(self.count("workflow_runs"), 1)

    async def test_terminal_run_lookup_does_not_restart_or_reinterpret_its_verdict(self):
        key, original = await self.pending()
        run_id = UUID(original["run"]["runId"])
        self.repository.complete_remote_cancellation(run_id, "USER_CANCELLED", {})
        response = await self.lookup(key)
        self.assertEqual(response.json()["status"], "CREATED")
        self.assertEqual(response.json()["run"]["status"], "ABORTED")
        self.assertIsNone(response.json()["run"]["verdict"])
        self.assertEqual(response.json()["recovery"]["action"], "NONE")
        self.assertEqual(self.dispatcher.run_ids, [])

    async def test_unprotected_bootstrap_cannot_read_server_only_receipts(self):
        key, _ = await self.pending()
        self.app.state.host_api_auth_required = False
        response = await self.lookup(key, headers={})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "HOST_API_NOT_CONFIGURED")


if __name__ == "__main__":
    unittest.main()
