"""Real five-listener HTTP control lifecycle; no paid LLM or real Docker."""

import asyncio
import socket
import unittest
from uuid import UUID

import httpx

import test_owned_agent_controls as controls_fixture
import test_owned_platform_tcp as tcp_fixture
from agents.llm.budget import LLMLimits
from agents.platform.composition import create_platform
from agents.platform.runner import run_platform
from orchestrator.domain import A2ATaskState, AgentRole, WorkflowStatus


class OwnedControlsTCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_http_resumed_task_can_cancel_without_new_task_or_budget(self):
        harness = controls_fixture._ControlHarness(self)
        harness.interrupt(AgentRole.PLANNER)
        entered, drained = asyncio.Event(), asyncio.Event()

        async def slow(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(.02)
                drained.set()

        harness.selected_providers[AgentRole.PLANNER].script[1] = slow
        fixture = harness.base
        ports = tcp_fixture._unused_loopback_ports()
        limits = LLMLimits(max_model_calls=40, max_tool_calls=40,
                           model_timeout_seconds=10, max_output_tokens=4096)
        roles = {role: settings.model_copy(update={"port": port, "llm_limits": limits})
                 for (role, settings), port in zip(fixture.settings.items(), ports[1:], strict=True)}
        settings = fixture.orchestrator_settings.model_copy(update={
            role.value.lower() + "_agent_url": selected.agent_base_url for role, selected in roles.items()})
        platform = create_platform(repository=fixture.repository, workspace_registry=fixture.registry,
            artifact_store=fixture.artifacts, orchestrator_settings=settings, agent_settings=roles,
            providers=harness.selected_providers, prepare_workspace=fixture.prepare_workspace, limits=limits,
            developer_services_factory=lambda execution: fixture.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: fixture.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: fixture.services(AgentRole.SECURITY, execution),
            orchestrator_port=ports[0])
        runner = asyncio.create_task(run_platform(platform, startup_timeout_seconds=10, shutdown_timeout_seconds=10))
        resumption = None

        async def wait_until(predicate):
            while not predicate():
                await asyncio.sleep(.02)

        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}", trust_env=False, timeout=10) as client:
                async def ready():
                    while True:
                        if runner.done():
                            await runner
                            self.fail("Platform stopped before readiness")
                        try:
                            if (await client.get("/health")).status_code == 200:
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.02)
                await asyncio.wait_for(ready(), 10)
                response = await client.post("/api/v1/runs", json=fixture.submission())
                self.assertEqual(response.status_code, 201, response.text)
                run_id = UUID(response.json()["run"]["runId"])
                await asyncio.wait_for(wait_until(lambda: fixture.repository.get_run(run_id).status is WorkflowStatus.WAITING_INPUT), 10)
                before = fixture.repository.list_steps(run_id)[0]
                budget = platform.budgets.resolve(fixture.repository.get_run_configuration(run_id))
                deadline = budget.deadline_monotonic
                resumption = asyncio.create_task(client.post(f"/api/v1/runs/{run_id}/resume", json={
                    "workflowStepId": str(before.workflow_step_id), "inputData": {"answer": "Keep frozen scope"}}))
                await asyncio.wait_for(entered.wait(), 10)
                await asyncio.wait_for(wait_until(lambda: fixture.repository.list_steps(run_id)[0].a2a_task_state is A2ATaskState.WORKING), 6)
                canceled = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "USER_CANCELLED"})
                self.assertEqual(canceled.status_code, 200, canceled.text)
                self.assertEqual((canceled.json()["status"], canceled.json()["verdict"]), ("ABORTED", None))
                self.assertTrue(drained.is_set())
                self.assertEqual((await resumption).status_code, 409)
                after = fixture.repository.list_steps(run_id)[0]
                self.assertEqual((after.workflow_step_id, after.a2a_task_id, after.agent_context_id),
                                 (before.workflow_step_id, before.a2a_task_id, before.agent_context_id))
                self.assertEqual(after.attempt, 1)
                self.assertIs(after.a2a_task_state, A2ATaskState.CANCELED)
                self.assertEqual(len(fixture.repository.list_steps(run_id)), 1)
                self.assertFalse(fixture.repository.list_project_artifacts(run_id))
                self.assertIs(platform.budgets.resolve(fixture.repository.get_run_configuration(run_id)), budget)
                self.assertEqual((budget.deadline_monotonic, budget.model_calls), (deadline, 2))
        finally:
            if resumption is not None and not resumption.done():
                resumption.cancel()
                await asyncio.gather(resumption, return_exceptions=True)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        for port in ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
