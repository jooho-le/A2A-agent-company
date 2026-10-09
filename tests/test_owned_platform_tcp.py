"""Actual loopback Uvicorn/SDK HTTP; synthetic model/container, real storage.

All listeners, Task databases and inert Git fixtures are private to this test.
Generated code/tests are never executed on the Host; the MCP seam remains the
real Dispatcher adapter rather than an actual stdio child.
"""

import asyncio
from contextlib import ExitStack
import socket
import unittest
from uuid import UUID

import httpx

import test_owned_agent_pipeline as pipeline_fixture
from agents.llm.budget import LLMLimits
from agents.platform.composition import create_platform
from agents.platform.runner import run_platform
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus


def _unused_loopback_ports():
    # Keep five probes simultaneously bound to avoid selecting duplicates.
    # The launcher itself performs the authoritative all-port admission.
    with ExitStack() as stack:
        probes = [stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) for _ in range(5)]
        for probe in probes:
            probe.bind(("127.0.0.1", 0))
        return tuple(probe.getsockname()[1] for probe in probes)


class OwnedPlatformTCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = pipeline_fixture.OwnedAgentPipelineTests()
        self.fixture.setUp()
        for function, arguments, keywords in self.fixture._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.fixture._cleanups.clear()

    async def test_actual_five_servers_run_initial_cycle_and_release_all_listeners(self):
        fixture = self.fixture
        ports = _unused_loopback_ports()
        limits = LLMLimits(max_model_calls=8, max_tool_calls=12, model_timeout_seconds=2, max_output_tokens=4096)
        roles = {role: selected.model_copy(update={"port": port, "llm_limits": limits})
            for (role, selected), port in zip(fixture.settings.items(), ports[1:], strict=True)}
        settings = fixture.orchestrator_settings.model_copy(update={
            role.value.lower() + "_agent_url": selected.agent_base_url for role, selected in roles.items()})
        providers = fixture.providers()
        platform = create_platform(repository=fixture.repository, workspace_registry=fixture.registry,
            artifact_store=fixture.artifacts, orchestrator_settings=settings,
            agent_settings=roles, providers=providers,
            developer_services_factory=lambda execution: fixture.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: fixture.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: fixture.services(AgentRole.SECURITY, execution),
            prepare_workspace=fixture.prepare_workspace, limits=limits, orchestrator_port=ports[0])
        runner = asyncio.create_task(run_platform(platform, startup_timeout_seconds=10, shutdown_timeout_seconds=10))
        try:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{ports[0]}", trust_env=False, timeout=1) as client:
                async def wait_ready():
                    while True:
                        if runner.done():
                            await runner
                            self.fail("Platform exited before readiness")
                        try:
                            response = await client.get("/health")
                            if response.status_code == 200:
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(.02)
                await asyncio.wait_for(wait_ready(), 10)
                for role, selected in roles.items():
                    response = await client.get(selected.agent_base_url + "/health")
                    self.assertEqual(response.json(), {"status": "ok", "role": role.value, "executionReady": True})
                response = await client.post("/api/v1/runs", json=fixture.submission())
                self.assertEqual(response.status_code, 201)
                run_id = UUID(response.json()["run"]["runId"])

                async def wait_completed():
                    while True:
                        response = await client.get(f"/api/v1/runs/{run_id}")
                        self.assertEqual(response.status_code, 200)
                        if response.json()["status"] == "HUMAN_REVIEW":
                            return
                        await asyncio.sleep(.03)
                await asyncio.wait_for(wait_completed(), 20)
                run = fixture.repository.get_run(run_id)
                self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
                self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED
                    for step in fixture.repository.list_steps(run_id)))
                artifacts = fixture.repository.list_project_artifacts(run_id)
                source = next(item for item in artifacts if item.artifact_type == "SOURCE")
                for report in (item for item in artifacts if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}):
                    self.assertEqual(report.execution_manifest, source.execution_manifest())
                configuration = fixture.repository.get_run_configuration(run_id)
                self.assertEqual(platform.budgets.resolve(configuration).model_calls, 6)
                self.assertEqual(sum(len(provider.requests) for provider in providers.values()), 6)
        finally:
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await runner
        for port in ports:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                probe.bind(("127.0.0.1", port))
