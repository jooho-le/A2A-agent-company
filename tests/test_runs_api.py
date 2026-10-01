import asyncio
import tempfile
import unittest
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from orchestrator.domain import (
    A2ATaskState,
    AgentRole,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.core.config import Settings
from orchestrator.main import create_app


class RecordingDispatcher:
    def __init__(self) -> None:
        self.run_ids = []

    async def dispatch_planner(self, run_id) -> None:
        self.run_ids.append(run_id)


class RunsAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "api.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)
        self.app = create_app(
            self.repository,
            settings=Settings(_env_file=None, database_path=str(self.database_path)),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        async def make_request() -> httpx.Response:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(make_request())

    def test_submit_persists_run_initial_planner_step_and_start_events(self) -> None:
        response = self.request(
            "POST",
            "/api/v1/runs",
            json={"scenarioId": str(uuid4()), "requestText": "회원가입 기능 구현"},
        )

        self.assertEqual(response.status_code, 201)
        body = response.json()
        run_id = body["run"]["runId"]
        self.assertEqual(body["run"]["status"], "RECEIVED")
        self.assertNotIn("requestText", body["run"])
        self.assertEqual(body["dispatchStatus"], "NOT_CONFIGURED")
        self.assertEqual(body["firstStep"]["agentRole"], "PLANNER")
        self.assertEqual(body["firstStep"]["status"], "PENDING")
        self.assertEqual(response.headers["location"], f"http://testserver/api/v1/runs/{run_id}")
        self.assertIsNone(self.repository.get_run(uuid4()))
        self.assertEqual(self.repository.get_run(UUID(run_id)).status, WorkflowStatus.RECEIVED)

        event_response = self.request("GET", f"/api/v1/runs/{run_id}/events")
        self.assertEqual(event_response.status_code, 200)
        self.assertEqual(
            [event["eventType"] for event in event_response.json()["events"]],
            ["RUN_STARTED", "WORKFLOW_STEP_CREATED"],
        )

    def test_configured_planner_is_scheduled_after_run_creation(self) -> None:
        dispatcher = RecordingDispatcher()
        self.app = create_app(
            self.repository,
            dispatcher=dispatcher,  # type: ignore[arg-type]
            settings=Settings(_env_file=None, database_path=str(self.database_path)),
        )

        response = self.request(
            "POST",
            "/api/v1/runs",
            json={"scenarioId": str(uuid4()), "requestText": "Plan this request"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["dispatchStatus"], "SCHEDULED")
        self.assertEqual(dispatcher.run_ids, [UUID(response.json()["run"]["runId"])])

    def test_status_steps_and_event_pagination(self) -> None:
        created = self.request(
            "POST",
            "/api/v1/runs",
            json={"scenarioId": str(uuid4()), "requestText": "Add tests"},
        ).json()
        run_id = created["run"]["runId"]

        status_response = self.request("GET", f"/api/v1/runs/{run_id}")
        steps_response = self.request("GET", f"/api/v1/runs/{run_id}/steps")
        events_response = self.request("GET", f"/api/v1/runs/{run_id}/events?limit=1&offset=1")

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["scenarioId"], created["run"]["scenarioId"])
        self.assertEqual(steps_response.json()["steps"][0]["workflowStepId"], created["firstStep"]["workflowStepId"])
        self.assertEqual(events_response.json()["total"], 2)
        self.assertEqual(events_response.json()["events"][0]["eventType"], "WORKFLOW_STEP_CREATED")

    def test_invalid_create_and_missing_run_return_client_errors(self) -> None:
        invalid = self.request("POST", "/api/v1/runs", json={"scenarioId": "not-a-uuid", "requestText": " "})
        missing = self.request("GET", f"/api/v1/runs/{uuid4()}")
        invalid_cancel = self.request(
            "POST",
            f"/api/v1/runs/{uuid4()}/cancel",
            json={"reason": "  "},
        )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(invalid_cancel.status_code, 422)

    def test_cancel_pending_run_records_aborted_status(self) -> None:
        created = self.request(
            "POST",
            "/api/v1/runs",
            json={"scenarioId": str(uuid4()), "requestText": "Cancel me"},
        ).json()
        run_id = created["run"]["runId"]

        canceled = self.request(
            "POST",
            f"/api/v1/runs/{run_id}/cancel",
            json={"reason": "USER_CANCELLED"},
        )

        self.assertEqual(canceled.status_code, 200)
        self.assertEqual(canceled.json()["status"], "ABORTED")
        self.assertIsNone(canceled.json()["verdict"])
        self.assertEqual(canceled.json()["terminationReason"], "USER_CANCELLED")

    def test_cancel_does_not_claim_remote_active_task_was_canceled(self) -> None:
        run = WorkflowRun(
            scenario_id=uuid4(),
            request_text="Active task",
            status=WorkflowStatus.IMPLEMENTING,
        )
        step = WorkflowStep(
            run_id=run.run_id,
            agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING,
            a2a_task_id="developer-task",
            a2a_task_state=A2ATaskState.WORKING,
        )
        self.repository.create_run(run, (step,), ())

        response = self.request(
            "POST",
            f"/api/v1/runs/{run.run_id}/cancel",
            json={"reason": "USER_CANCELLED"},
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.IMPLEMENTING)


if __name__ == "__main__":
    unittest.main()
