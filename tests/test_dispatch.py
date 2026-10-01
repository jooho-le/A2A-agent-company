import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.a2a import A2AAgentRegistry, AgentNotConfiguredError
from orchestrator.application import PlannerRunDispatcher, TaskRunDisposition
from orchestrator.core.config import Settings
from orchestrator.domain import (
    AgentRole,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository


class FakePlannerClient:
    def __init__(self, state: str) -> None:
        self.state = state
        self.sent: list[tuple[dict[str, object], object, str | None]] = []
        self.closed = False
        self.card_resolved = False

    async def __aenter__(self) -> "FakePlannerClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    async def resolve_agent_card(self) -> None:
        self.card_resolved = True

    async def send_task(self, payload, metadata, *, context_id=None):
        self.sent.append((dict(payload), metadata, context_id))
        if isinstance(self.state, Exception):
            raise self.state
        return ParseDict(
            {
                "id": "planner-task-opaque",
                "contextId": "planner-context-opaque",
                "status": {"state": self.state},
            },
            Task(),
        )

    async def get_task(self, task_id: str) -> Task:
        raise AssertionError("terminal/interrupted submission must not be polled")


class PlannerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(
            Path(self.temp_dir.name) / "dispatch.sqlite3"
        )
        self.run = WorkflowRun(
            scenario_id=uuid4(),
            request_text="회원가입 기능을 계획해줘.",
        )
        self.step = WorkflowStep(
            run_id=self.run.run_id,
            agent_role=AgentRole.PLANNER,
        )
        self.repository.create_run(
            self.run,
            (self.step,),
            (
                TraceEvent(
                    run_id=self.run.run_id,
                    event_type="RUN_STARTED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=self.run.status,
                ),
                TraceEvent(
                    run_id=self.run.run_id,
                    workflow_step_id=self.step.workflow_step_id,
                    event_type="WORKFLOW_STEP_CREATED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=self.run.status,
                ),
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def dispatcher_for(self, client: FakePlannerClient) -> PlannerRunDispatcher:
        return PlannerRunDispatcher(
            self.repository,
            A2AAgentRegistry({AgentRole.PLANNER: "http://planner.test"}),
            client_factory=lambda url: client,  # type: ignore[arg-type]
        )

    async def test_completed_planner_task_persists_result_without_finalizing_run(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        step = self.repository.list_steps(self.run.run_id)[0]
        contexts = self.repository.list_agent_contexts(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.PLANNING)
        self.assertIsNone(run.verdict)
        self.assertEqual(step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(step.a2a_task_id, "planner-task-opaque")
        self.assertEqual(contexts[0].agent_id, "planner")
        self.assertEqual(contexts[0].latest_a2a_task_id, step.a2a_task_id)
        self.assertEqual(client.sent[0][0], {"request": self.run.request_text})
        self.assertIsNone(client.sent[0][2])
        self.assertTrue(client.card_resolved)
        self.assertTrue(client.closed)
        self.assertIn("A2A_TASK_RECEIVED", [event.event_type for event in events])
        received = next(event for event in events if event.event_type == "A2A_TASK_RECEIVED")
        self.assertEqual(received.a2a_task_state.value, "TASK_STATE_COMPLETED")

    async def test_planner_input_required_pauses_workflow(self) -> None:
        client = FakePlannerClient("TASK_STATE_INPUT_REQUIRED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        step = self.repository.list_steps(self.run.run_id)[0]
        self.assertEqual(run.status, WorkflowStatus.WAITING_INPUT)
        self.assertEqual(run.resume_state, WorkflowStatus.PLANNING)
        self.assertEqual(step.status, WorkflowStepStatus.WAITING_INPUT)

    async def test_uncertain_send_failure_requires_review_without_retry(self) -> None:
        client = FakePlannerClient(httpx.ConnectError("connection interrupted"))

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        step = self.repository.list_steps(self.run.run_id)[0]
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.PLANNING)
        self.assertIsNone(run.verdict)
        self.assertEqual(step.status, WorkflowStepStatus.RUNNING)
        self.assertIsNone(step.a2a_task_id)
        self.assertEqual(len(client.sent), 1)
        self.assertIn(
            "A2A_DISPATCH_REQUIRES_REVIEW",
            [event.event_type for event in events],
        )


class AgentRegistryTests(unittest.TestCase):
    def test_settings_map_each_agent_role_to_its_own_url(self) -> None:
        registry = A2AAgentRegistry.from_settings(
            Settings(
                planner_agent_url="http://planner.test",
                developer_agent_url="http://developer.test",
                qa_agent_url="http://qa.test",
                security_agent_url="http://security.test",
            )
        )

        self.assertEqual(registry.require_base_url(AgentRole.PLANNER), "http://planner.test")
        self.assertEqual(registry.require_base_url(AgentRole.DEVELOPER), "http://developer.test")
        self.assertEqual(registry.require_base_url(AgentRole.QA), "http://qa.test")
        self.assertEqual(registry.require_base_url(AgentRole.SECURITY), "http://security.test")

    def test_missing_agent_url_is_reported_without_fallback(self) -> None:
        registry = A2AAgentRegistry({AgentRole.PLANNER: None})

        with self.assertRaises(AgentNotConfiguredError):
            registry.require_base_url(AgentRole.PLANNER)
        self.assertIsNone(registry.get_base_url(AgentRole.QA))


if __name__ == "__main__":
    unittest.main()
