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
    SCN_001_ID,
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
            json={"scenarioId": str(SCN_001_ID), "requestText": "회원가입 기능 구현"},
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
            ["RUN_STARTED", "WORKFLOW_STEP_CREATED", "ARTIFACT_REGISTERED"],
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
            json={"scenarioId": str(SCN_001_ID), "requestText": "Plan this request"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["dispatchStatus"], "SCHEDULED")
        self.assertEqual(dispatcher.run_ids, [UUID(response.json()["run"]["runId"])])

    def test_status_steps_and_event_pagination(self) -> None:
        created = self.request(
            "POST",
            "/api/v1/runs",
            json={"scenarioId": str(SCN_001_ID), "requestText": "Add tests"},
        ).json()
        run_id = created["run"]["runId"]

        status_response = self.request("GET", f"/api/v1/runs/{run_id}")
        steps_response = self.request("GET", f"/api/v1/runs/{run_id}/steps")
        events_response = self.request("GET", f"/api/v1/runs/{run_id}/events?limit=1&offset=1")

        self.assertEqual(status_response.status_code, 200)
        self.assertEqual(status_response.json()["scenarioId"], created["run"]["scenarioId"])
        self.assertEqual(steps_response.json()["steps"][0]["workflowStepId"], created["firstStep"]["workflowStepId"])
        self.assertEqual(events_response.json()["total"], 3)
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
            json={"scenarioId": str(SCN_001_ID), "requestText": "Cancel me"},
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

    def test_discovery_validation_and_secret_safe_run_configuration(self) -> None:
        discovered = self.request("GET", "/api/v1/scenarios").json()["scenarios"]
        self.assertEqual(discovered[0]["scenarioId"], str(SCN_001_ID))
        for payload in (
            {"scenarioId": str(SCN_001_ID), "requestText": "   "},
            {"scenarioId": str(uuid4()), "requestText": "not registered"},
            {"scenarioId": str(SCN_001_ID), "requestText": "experiment", "configuration": {"experimentId": str(uuid4())}},
        ):
            self.assertEqual(self.request("POST", "/api/v1/runs", json=payload).status_code, 422)
        response = self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID), "requestText": "password=sample-credential 개발",
        })
        self.assertEqual(response.status_code, 201)
        run_id = UUID(response.json()["run"]["runId"])
        run = self.repository.get_run(run_id)
        self.assertNotIn("sample-credential", run.request_text)
        workspace = self.request("GET", f"/api/v1/runs/{run_id}/workspace").json()
        self.assertEqual(workspace["workspaceId"], str(run.workspace_id))
        self.assertNotIn("rootPath", workspace)
        artifacts = self.request("GET", f"/api/v1/runs/{run_id}/artifacts").json()["artifacts"]
        self.assertEqual([a["artifactType"] for a in artifacts], ["RUN_CONFIGURATION"])
        config = self.request("GET", f"/api/v1/runs/{run_id}/configuration").json()
        self.assertEqual(config["configuration"]["limits"]["maxFixAttempts"], 3)
        self.assertIsNone(config["configuration"]["model"])
        self.assertEqual(self.request("GET", f"/api/v1/runs/{run_id}/artifacts/{config['artifactId']}").json(), config)
        self.assertEqual(self.request("GET", f"/api/v1/runs/{run_id}/artifacts/{uuid4()}").status_code, 404)
        self.assertEqual(self.request("GET", f"/api/v1/runs/{run_id}/issues").json()["issues"], [])
        self.assertEqual(self.request("GET", f"/api/v1/runs/{run_id}/tool-attempts").json()["attempts"], [])

    def test_resume_recover_require_valid_stage_without_resending(self) -> None:
        created = self.request("POST", "/api/v1/runs", json={
            "scenarioId": str(SCN_001_ID), "requestText": "resume guards",
        }).json()
        run_id = created["run"]["runId"]
        self.assertEqual(self.request("POST", f"/api/v1/runs/{run_id}/resume", json={}).status_code, 409)
        self.assertEqual(self.request("POST", f"/api/v1/runs/{run_id}/recover", json={}).status_code, 409)
        self.assertEqual(self.request("POST", f"/api/v1/runs/{uuid4()}/recover", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
