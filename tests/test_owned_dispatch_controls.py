"""Owned capability gates; synthetic Agent client, no product execution."""

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from starlette.requests import Request

import test_dispatch as dispatch_fixture
from orchestrator.a2a.registry import A2AAgentRegistry
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.api.dependencies import get_workflow_controls
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.core.config import Settings
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun, WorkflowStep, WorkflowStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


class OwnedDispatchControlsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-owned-controls-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = SQLiteWorkflowRepository(self.root / "workflow.sqlite3")
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 실행")
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(self.run, (self.step,), ())
        self.registry = A2AAgentRegistry({role: f"http://{role.value.lower()}.test" for role in AgentRole})

    def dispatcher(self, client, **options):
        return PlannerRunDispatcher(self.repository, self.registry, client_factory=lambda _url: client, **options)

    async def test_preparation_runs_after_durable_claim_before_any_a2a_request_once(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")
        prepared = []

        async def prepare(run_id):
            self.assertEqual(self.repository.get_run(run_id).status, WorkflowStatus.PLANNING)
            self.assertFalse(client.card_resolved)
            self.assertFalse(client.sent)
            prepared.append(run_id)

        dispatcher = self.dispatcher(client, before_dispatch=prepare)
        await dispatcher.dispatch_planner(self.run.run_id)
        await dispatcher.dispatch_planner(self.run.run_id)
        self.assertEqual(prepared, [self.run.run_id])
        self.assertTrue(client.sent)

    async def test_preparation_failure_stops_before_http_and_preserves_review_trace(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")

        async def prepare(_run_id):
            raise ValueError("operator-private-credential-and-path")

        await self.dispatcher(client, before_dispatch=prepare).dispatch_planner(self.run.run_id)
        self.assertFalse(client.card_resolved)
        self.assertFalse(client.sent)
        self.assertEqual(self.repository.get_run(self.run.run_id).status, WorkflowStatus.HUMAN_REVIEW)
        events, _ = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        self.assertIn("OWNED_AGENT_PREPARATION_FAILED", {event.event_type for event in events})
        self.assertNotIn("operator-private-credential", str(events))

    async def test_preparation_cancellation_propagates_without_a2a_send(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")

        async def prepare(_run_id):
            raise asyncio.CancelledError()

        dispatcher = self.dispatcher(client, before_dispatch=prepare)
        with self.assertRaises(asyncio.CancelledError):
            await dispatcher.dispatch_planner(self.run.run_id)
        self.assertFalse(client.sent)
        self.assertFalse(dispatcher._active_runs)

    async def test_initial_only_pipeline_keeps_real_qa_failure_at_fix_required(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED", qa_outcome="FAIL")
        await self.dispatcher(client, allow_fix_dispatch=False).dispatch_planner(self.run.run_id)
        run = self.repository.get_run(self.run.run_id)
        self.assertEqual((run.status, run.fix_attempt, run.code_version), (WorkflowStatus.FIX_REQUIRED, 0, 1))
        self.assertIsNone(run.verdict)
        self.assertEqual(len(self.repository.list_steps(run.run_id)), 4)
        self.assertTrue(self.repository.list_issue_records(run.run_id))

    async def test_initial_only_pipeline_keeps_build_failure_without_validation_or_fix(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED", build_exit_code=1)
        await self.dispatcher(client, allow_fix_dispatch=False).dispatch_planner(self.run.run_id)
        run = self.repository.get_run(self.run.run_id)
        self.assertEqual((run.status, run.fix_attempt), (WorkflowStatus.FIX_REQUIRED, 0))
        self.assertTrue(self.repository.list_issue_records(run.run_id))
        self.assertFalse(client.handoffs)

    async def test_submission_rejected_before_persistence_and_scheduling(self):
        seen = []

        def validator(configuration):
            seen.append(configuration)
            raise ValueError("private-operator-setting")

        app = create_app(repository=self.repository,
            settings=Settings(_env_file=None, log_level="CRITICAL", database_path=str(self.repository.database_path),
                workspace_root=str(self.root / "workspaces")), submission_validator=validator)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://orchestrator.test") as client:
            response = await client.post("/api/v1/runs", json={"scenarioId": str(SCN_001_ID), "requestText": "회원가입"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "OWNED_AGENT_CONFIGURATION_INVALID"})
        self.assertEqual(len(seen), 1)
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM workflow_runs").fetchone()[0], 1)

    async def test_owned_http_client_does_not_load_environment_proxy_configuration(self):
        with patch.dict(os.environ, {"HTTP_PROXY": "http://proxy.example.invalid:9999",
            "HTTPS_PROXY": "http://proxy.example.invalid:9999", "ALL_PROXY": "http://proxy.example.invalid:9999",
            "NO_PROXY": ""}), patch("orchestrator.a2a.client.httpx.AsyncClient", wraps=httpx.AsyncClient) as constructor:
            client = A2AAgentClient("http://127.0.0.1:8101", trust_env=False)
            try:
                constructor.assert_called_once_with(timeout=10.0, headers=None, trust_env=False)
                self.assertFalse(client._httpx_client._trust_env)
            finally:
                await client.aclose()

    def test_existing_workflow_controls_share_owned_private_client_factory(self):
        selected = lambda _url: None
        dispatcher = PlannerRunDispatcher(self.repository, self.registry, client_factory=selected)
        app = create_app(repository=self.repository, dispatcher=dispatcher,
            settings=Settings(_env_file=None, log_level="CRITICAL"))
        app.state.agent_client_factory = selected
        request = Request({"type": "http", "app": app})
        with patch("orchestrator.api.dependencies.WorkflowControlService") as service:
            get_workflow_controls(request)
        self.assertIs(service.call_args.kwargs["client_factory"], selected)

    def test_invalid_capability_configuration_is_rejected(self):
        client = dispatch_fixture.FakePlannerClient("TASK_STATE_COMPLETED")
        for options in ({"before_dispatch": "private"}, {"allow_fix_dispatch": 1}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.dispatcher(client, **options)
        with self.assertRaises(ValueError):
            create_app(settings=Settings(_env_file=None), submission_validator="private")
