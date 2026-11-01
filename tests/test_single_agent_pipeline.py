"""One real Single A2A server, persisted context, MCP ACLs and Host evidence.

Provider/Docker are deterministic adapters. Git/SQLite/Snapshot/receipts and
the official ASGI A2A transport are real; generated code never runs on Host.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import replace
import json
import time
from uuid import UUID
import unittest

import httpx

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.budget import LLMLimits
from agents.platform.runner import _owned_endpoints
from agents.platform.composition import PlatformConfigurationError
from agents.runtime.security_services import SecurityRuntimeServices
from agents.single.platform import create_single_platform
from agents.single.prompts import build_single_system_prompt
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration, MCPClientError, open_mcp_client
from mcp_tools.execution_runtime import TrackedMCPExecutor
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from agents.single.mcp import open_single_model_client
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.domain import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.sandbox.contracts import CLIResult
from orchestrator.sandbox.runtime import SandboxRuntime
from test_llm_runtime import FakeProvider, ToolCall, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker, IMAGE_ID
import test_owned_agent_pipeline as fixtures
import test_single_agent_mcp as single_mcp_fixtures


class SingleAgentPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixtures.OwnedAgentPipelineTests("runTest")
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.configuration = self.f.configuration.model_copy(update={"architecture": "SINGLE_AGENT"})
        self.model_clients, self.events, self.usage, self.clients = [], [], [], []
        self.measured_security = False

    @asynccontextmanager
    async def peer(self, configuration):
        role = configuration.binding.role
        if role is not SingleAgentIdentity.SINGLE_AGENT and not (role is AgentRole.SECURITY and self.measured_security):
            async with self.f.peer_client(configuration) as client:
                yield client
            return
        if role is SingleAgentIdentity.SINGLE_AGENT:
            phase = configuration.binding.single_phase
            semantic = {SinglePhase.IMPLEMENTING: AgentRole.DEVELOPER, SinglePhase.FIXING: AgentRole.DEVELOPER, SinglePhase.QA: AgentRole.QA,
                        SinglePhase.SECURITY: AgentRole.SECURITY}[phase]
            self.model_clients.append(configuration)
            self.events.append(("model-mcp", phase))
        else:
            phase, semantic = SinglePhase.SECURITY, role
        handlers = FileTools(self.f.artifacts, frozen_source=configuration.frozen_source).handlers(semantic)
        if role is AgentRole.SECURITY:
            docker = FakeDocker()
            sandbox = SandboxRuntime(self.f.repository, self.f.registry, self.f.artifacts, docker=docker)
            scans = SecurityScanTools(self.f.artifacts, sandbox, SecurityScanOutputStore(self.f.repository),
                configuration=configuration.security_scan_configuration, max_call_seconds=configuration.max_call_seconds)
            unit = UnitTestTools(self.f.artifacts, sandbox, UnitTestOutputStore(self.f.repository),
                configuration=configuration.unit_test_configuration, max_call_seconds=configuration.max_call_seconds)
            handlers = {**handlers, **scans.handlers(role), **unit.handlers(role)}

            def result(container):
                if "/inputs/_unit_runner.py" in container["Config"]["Cmd"]:
                    identities = [f"test_security.Security.test_req_{index}" for index in range(
                        1, len(self.f.scenario.requirement_ids_for(RequirementValidator.SECURITY)) + 1)]
                    version = self.f.repository.get_run(configuration.binding.run_id).code_version
                    failure = getattr(self, "security_failures", {}).get(version)
                    docker.exit_code = int(failure is not None)
                    payload = {"format": "unittest-v1", "total": len(identities),
                        "passed": len(identities) - docker.exit_code, "failed": docker.exit_code, "skipped": 0,
                        "tests": [{"testId": identity, "outcome": "FAIL" if index == failure else "PASS"}
                                  for index, identity in enumerate(identities)]}
                else:
                    profile = configuration.security_scan_configuration.profiles[0]
                    payload = {"format": "bandit-v1", "profileName": profile.name, "scanner": "bandit",
                        "scannerVersion": profile.scanner_version, "ruleIds": sorted(profile.rule_ids),
                        "profileRef": profile.profile_ref, "scannedFiles": ["signup.py"], "findings": []}
                docker.start_result = CLIResult(returncode=docker.exit_code, stdout=json.dumps(payload).encode(), stderr=b"")
            docker.start_hook = result
        dispatcher = MCPDispatcher(configuration.binding, self.f.registry, handlers=handlers,
            frozen_snapshot_id=None if configuration.frozen_source is None else configuration.frozen_source.project_artifact_id,
            max_call_seconds=configuration.max_call_seconds)
        session = DispatcherSession(dispatcher)
        client = BoundMCPClient(configuration=configuration, _client=type("SDKPeer", (), {"session": session})())
        try:
            yield client
        finally:
            client._state.active = False

    def services(self, role, execution):
        original = self.f.services(role, execution)
        original.client_factory = self.peer
        if role is not AgentRole.SECURITY or not self.measured_security:
            return original
        config = replace(original.configuration, unit_test_configuration=UnitTestConfiguration(
            scopes=(UnitTestScope(name="security-unit", kind="QA_TESTS", role="SECURITY"),),
            limits=original.configuration.security_scan_configuration.limits, image_reference=IMAGE_ID))
        return SecurityRuntimeServices(self.f.repository, self.f.registry, self.f.artifacts,
            mcp_configuration=config, client_factory=self.peer)

    def provider(self):
        roles = self.f.providers()
        script = [action for role in AgentRole for action in roles[role].script]
        if self.measured_security:
            ready = roles[AgentRole.SECURITY].script[-1]
            def write(request):
                return tool_response(ToolCall(call_id="security-write", name="write_test_file",
                    arguments_json=json.dumps({"workspaceId": fixtures._workspace_id(request),
                        "path": "outputs/security/tests/test_security.py", "content": "# inert Security fixture\n"})))
            def measured_ready(request):
                payload = json.loads(ready(request).output_text)
                payload["testCases"] = [{"selector": "security-unit", "testId": f"test_security.Security.test_req_{index}",
                    "requirementId": str(requirement), "title": f"보안 기준 {index}", "expectedResult": "기준 충족"}
                    for index, requirement in enumerate(self.f.scenario.requirement_ids_for(RequirementValidator.SECURITY), 1)]
                return text_response(json.dumps(payload, ensure_ascii=False))
            script[-1:] = [write, measured_ready]
        return FakeProvider(*script)

    @asynccontextmanager
    async def running(self, *, cap=8, tool_cap=12, provider=None, price_book=None):
        provider = provider or self.provider()
        limits = LLMLimits(max_model_calls=cap, max_tool_calls=tool_cap, model_timeout_seconds=2, max_output_tokens=4096)
        selected = self.f.settings[AgentRole.DEVELOPER].model_copy(update={"role": SingleAgentIdentity.SINGLE_AGENT,
            "database_path": self.f.directory / "single.sqlite3", "llm_limits": limits})
        settings = self.f.orchestrator_settings.model_copy(update={
            role.value.lower() + "_agent_url": selected.agent_base_url for role in AgentRole})

        def a2a(url):
            self.assertEqual(url, selected.agent_base_url)
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT]), base_url=url)
            self.clients.append(client)
            return A2AAgentClient(url, httpx_client=client)

        self.platform = create_single_platform(repository=self.f.repository, workspace_registry=self.f.registry,
            artifact_store=self.f.artifacts, orchestrator_settings=settings, agent_settings=selected, provider=provider,
            developer_services_factory=lambda execution: self.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: self.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: self.services(AgentRole.SECURITY, execution),
            prepare_workspace=self.f.prepare_workspace, limits=limits, a2a_client_factory=a2a, usage_sink=self.usage.append,
            price_book=price_book)
        self.assertEqual({endpoint.name for endpoint in _owned_endpoints(self.platform)}, {"orchestrator", "single_agent"})
        try:
            async with AsyncExitStack() as stack:
                for app in (*self.platform.agent_apps.values(), self.platform.orchestrator_app):
                    await stack.enter_async_context(app.router.lifespan_context(app))
                client = await stack.enter_async_context(httpx.AsyncClient(transport=httpx.ASGITransport(app=self.platform.orchestrator_app),
                    base_url="http://127.0.0.1:8000"))
                yield client, provider
        finally:
            for client in self.clients:
                await client.aclose()
            await self.platform.aclose()

    async def submit(self, client):
        response = await asyncio.wait_for(client.post("/api/v1/runs", json=self.f.submission()), timeout=20)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["run"]["runId"])

    def assert_history(self, run_id, provider):
        steps = self.f.repository.list_steps(run_id)
        self.assertEqual(len(steps), 4, steps)
        self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in steps), steps)
        self.assertEqual(len({step.agent_context_id for step in steps}), 1)
        self.assertEqual(len({step.a2a_task_id for step in steps}), 4)
        mappings = self.f.repository.list_agent_contexts(run_id)
        self.assertEqual([item.agent_id for item in mappings], ["single_agent"])
        self.assertEqual({request.system_prompt for request in provider.requests}, {build_single_system_prompt()})
        payloads = [json.loads(json.loads(request.input_items_json)[0]["content"]) for request in provider.requests]
        self.assertEqual(list(dict.fromkeys(payload["trusted"]["phase"] for payload in payloads)),
                         ["PLANNING", "IMPLEMENTING", "QA", "SECURITY"])
        self.assertEqual({payload["trusted"]["identity"] for payload in payloads}, {"SINGLE_AGENT"})
        self.assertEqual({payload["contextId"] for payload in payloads}, {steps[0].agent_context_id})
        self.assertEqual(len(payloads[-1]["history"]), 4)
        self.assertTrue(all(len(payload["history"]) == index for index, payload in enumerate(
            [next(item for item in payloads if item["trusted"]["phase"] == phase)
             for phase in ("PLANNING", "IMPLEMENTING", "QA", "SECURITY")], 1)))
        names = {artifact["name"] for item in payloads[-1]["history"][:-1] for artifact in item["artifacts"]}
        self.assertTrue({"requirements.json", "source-snapshot.json", "qa-report.json"} <= names, names)
        self.assertEqual([config.binding.single_phase for config in self.model_clients],
                         [SinglePhase.IMPLEMENTING, SinglePhase.QA, SinglePhase.SECURITY])
        self.assertTrue(all(config.binding.role is SingleAgentIdentity.SINGLE_AGENT for config in self.model_clients))
        self.assertTrue(self.usage and all(record.role is SingleAgentIdentity.SINGLE_AGENT for record in self.usage))

    async def test_one_agent_retains_history_and_unproven_security_never_becomes_success(self):
        async with self.running() as (client, provider):
            run_id = await self.submit(client)
            self.assert_history(run_id, provider)
            self.assertIs(self.f.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
            self.assertNotEqual(getattr(self.f.repository.get_run(run_id).verdict, "value", None), "SUCCESS")
            agent = self.platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT]
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent), base_url="http://127.0.0.1:8105") as direct:
                self.assertTrue((await direct.get("/health")).json()["executionReady"])
                self.assertEqual((await direct.get("/.well-known/agent-card.json")).json()["skills"][0]["id"], "single-initial-pipeline")
            for action in ("resume", "recover"):
                response = await client.post(f"/api/v1/runs/{run_id}/{action}", json={})
                self.assertEqual(response.status_code, 409, response.text)  # registered evidence cannot be overridden

    async def test_measured_single_pipeline_reaches_success_on_same_candidate(self):
        self.measured_security = True
        async with self.running() as (client, provider):
            run_id = await self.submit(client)
            self.assert_history(run_id, provider)
            run = self.f.repository.get_run(run_id)
            self.assertIs(run.status, WorkflowStatus.FINISHED)
            self.assertEqual(run.verdict.value, "SUCCESS")
            reports = [item for item in self.f.repository.list_project_artifacts(run_id)
                       if item.artifact_type in {"BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"}]
            self.assertEqual(len(reports), 3)
            self.assertEqual(len({item.execution_manifest.snapshot_sha256 for item in reports}), 1)

    async def test_shared_model_cap_stops_later_phases_without_new_budget(self):
        async with self.running(cap=2) as (client, provider):
            run_id = await self.submit(client)
            self.assertEqual(len(provider.requests), 2)
            self.assertIs(self.f.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
            self.assertEqual([phase for _kind, phase in self.events], [SinglePhase.IMPLEMENTING])

    async def test_wrong_architecture_rejected_before_workspace_or_provider_call(self):
        async with self.running() as (client, provider):
            body = self.f.submission()
            body["configuration"]["architecture"] = "MULTI_AGENT"
            response = await client.post("/api/v1/runs", json=body)
            self.assertEqual(response.status_code, 422)
            self.assertFalse(provider.requests)
            self.assertFalse(self.f.preparations)

    async def test_single_host_rejects_legacy_four_server_routing_before_execution(self):
        selected = self.f.settings[AgentRole.DEVELOPER].model_copy(update={"role": SingleAgentIdentity.SINGLE_AGENT})
        provider = self.provider()
        with self.assertRaises(PlatformConfigurationError):
            create_single_platform(repository=self.f.repository, workspace_registry=self.f.registry,
                artifact_store=self.f.artifacts, orchestrator_settings=self.f.orchestrator_settings,
                agent_settings=selected, provider=provider, limits=selected.llm_limits,
                developer_services_factory=lambda execution: self.services(AgentRole.DEVELOPER, execution),
                qa_services_factory=lambda execution: self.services(AgentRole.QA, execution),
                security_services_factory=lambda execution: self.services(AgentRole.SECURITY, execution),
                prepare_workspace=self.f.prepare_workspace)
        self.assertFalse(provider.requests)
        self.assertFalse(self.f.preparations)
        self.assertFalse(self.f.service_contexts)

    async def test_qa_failure_infrastructure_does_not_launch_security_in_same_context(self):
        provider = self.provider()
        provider.script[4] = text_response("{}")
        async with self.running(provider=provider) as (client, provider):
            run_id = await self.submit(client)
            self.assertIs(self.f.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
            self.assertEqual(len(provider.requests), 5)
            self.assertEqual([phase for _kind, phase in self.events], [SinglePhase.IMPLEMENTING, SinglePhase.QA])


class SingleModelTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_view_uses_single_authority_and_existing_private_journal(self):
        setup = single_mcp_fixtures.SingleMCPFileTests("runTest")
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        f = setup.fixture
        host = MCPChildConfiguration(binding=MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
            run_id=f.run.run_id, workspace_id=f.run.workspace_id), database_path=f.database, workspace_root=f.base)
        async with open_single_model_client(host, SinglePhase.IMPLEMENTING, open_mcp_client) as view:
            self.assertIs(view.principal_configuration.binding.role, SingleAgentIdentity.SINGLE_AGENT)
            tracked = TrackedMCPExecutor(view, ToolExecutionStore(f.repository), workflow_step_id=f.step.workflow_step_id)
            await tracked.invoke("write_source_file", f.arguments(content="# real Single stdio fixture\n"),
                                 deadline_monotonic=time.monotonic() + 10)
            self.assertEqual(f.file.read_text(), "# real Single stdio fixture\n")
            self.assertEqual(len(tracked.logical_call_ids), 1)
            with self.assertRaises(MCPClientError):
                await view.call_tool("write_test_file", f.arguments(path="outputs/qa/tests/test_bypass.py", content="pass\n"))
            wrong = replace(host, binding=replace(host.binding, role=AgentRole.QA, agent_role=AgentRole.QA))
            with self.assertRaises(MCPClientError):
                BoundMCPClient(configuration=wrong, _client=view._client, principal_configuration=view.principal_configuration)
        with self.assertRaises(MCPClientError):
            await view.call_tool("read_project_file", f.arguments())


if __name__ == "__main__":
    unittest.main()
