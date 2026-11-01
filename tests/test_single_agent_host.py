"""Explicit Single operator path through existing Host approval and shutdown.

Only private Git fixtures and fake providers/transports; no external API,
listener, Docker daemon, or generated Source is executed.
"""

from contextlib import AsyncExitStack, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
from uuid import UUID

import httpx

from agents.core.identity import SingleAgentIdentity
from agents.platform.configuration import (load_runtime_configuration, resolve_runtime_configuration,
    RuntimeConfigurationError)
from agents.platform import host as host_module, run_host as cli, check_configuration
from agents.platform.host import HostFactoryError, approved_run_configuration
import test_owned_host as fixtures
import test_host_services as services_fixture
import test_tool_configuration as tool_fixture
from agents.platform.tool_configuration import decode_tool_configuration
from orchestrator.a2a.client import A2AAgentClient
from test_llm_runtime import FakeProvider, text_response


class _SingleFixture(fixtures._HostFixture):
    def setUp(self):
        super().setUp()
        self.runtime["agents"] = {"SINGLE_AGENT": {"port": 8105,
            "databasePath": "../.data/agents/single_agent.sqlite3", "bearerTokenEnv": "A2A_PLATFORM_SINGLE_AGENT_TOKEN"}}
        self.environment = {key: value for key, value in self.environment.items() if key.endswith("API_KEY")}
        self.environment["A2A_PLATFORM_SINGLE_AGENT_TOKEN"] = "single-fixture-bearer-not-a-real-key"
        self.write()

    def arguments(self, **overrides):
        overrides.setdefault("architecture", "SINGLE_AGENT")
        return super().arguments(**overrides)


class SingleHostConfigurationTests(_SingleFixture, unittest.TestCase):
    def test_single_inspection_is_offline_and_requires_one_store_and_endpoint(self):
        with patch.object(host_module, "SQLiteWorkflowRepository", side_effect=AssertionError("read-only")):
            plan = self.inspect()
        self.assertEqual(plan.approved_configuration.architecture, "SINGLE_AGENT")
        self.assertEqual(set(plan.layout.agent_database_paths), {SingleAgentIdentity.SINGLE_AGENT})
        self.assertFalse((self.directory / ".data").exists())
        resolved = resolve_runtime_configuration(plan.runtime, base_directory=self.base, environ=self.environment)
        self.assertEqual(set(resolved.agent_settings), {SingleAgentIdentity.SINGLE_AGENT})
        for role in fixtures.AgentRole:
            self.assertEqual(getattr(resolved.orchestrator_settings, f"{role.value.lower()}_agent_url"), "http://127.0.0.1:8105")
            self.assertEqual(getattr(resolved.orchestrator_settings, f"{role.value.lower()}_bearer_token").get_secret_value(),
                self.environment["A2A_PLATFORM_SINGLE_AGENT_TOKEN"])

    def test_explicit_architecture_and_closed_identity_schema(self):
        with self.assertRaises(RuntimeConfigurationError):
            load_runtime_configuration(self.runtime_file)  # default stays Multi
        for change in ("missing", "mixed", "ports", "production", "unknown"):
            original = deepcopy(self.runtime)
            if change == "missing":
                self.runtime["agents"] = {}
            elif change == "mixed":
                self.runtime["agents"]["QA"] = deepcopy(self.runtime["agents"]["SINGLE_AGENT"])
            elif change == "ports":
                self.runtime["agents"]["SINGLE_AGENT"]["port"] = self.runtime["orchestrator"]["port"]
            elif change == "production":
                self.runtime["environment"] = "production"
                self.runtime["agents"]["SINGLE_AGENT"]["bearerTokenEnv"] = None
            else:
                self.runtime["agents"]["SINGLE_AGENT"]["arbitrary"] = True
            self.write()
            with self.subTest(change=change), self.assertRaises(HostFactoryError):
                self.inspect()
            self.assertFalse((self.directory / ".data").exists())
            self.runtime = original
        self.write()

    def test_missing_single_credentials_and_storage_aliases_fail_before_creation(self):
        plan = self.inspect()
        for key in tuple(self.environment):
            environment = dict(self.environment)
            environment.pop(key)
            with self.subTest(key=key), self.assertRaises(RuntimeConfigurationError):
                resolve_runtime_configuration(plan.runtime, base_directory=self.base, environ=environment)
        self.runtime["agents"]["SINGLE_AGENT"]["databasePath"] = self.runtime["orchestrator"]["databasePath"]
        self.write()
        with self.assertRaises(HostFactoryError):
            self.inspect()
        self.assertFalse((self.directory / ".data").exists())

    def test_policy_pins_baseline_tools_protected_cases_and_approved_model(self):
        plan = self.inspect()
        wrong = plan.approved_configuration.model_copy(update={"architecture": "MULTI_AGENT"})
        with self.assertRaises(HostFactoryError):
            plan.validate_configuration(wrong)
        self.protected()
        protected = self.inspect(protected_cases_file=self.cases_file)
        self.assertNotEqual(plan.policy_sha256, protected.policy_sha256)
        self.cases["cases"] = []
        self.write()
        with self.assertRaises(HostFactoryError):
            self.inspect(protected_cases_file=self.cases_file)


class SingleHostCreationTests(_SingleFixture, unittest.IsolatedAsyncioTestCase):
    async def test_approved_file_submission_prepares_source_and_calls_only_one_real_a2a_service(self):
        provider = FakeProvider(text_response("{}"))  # invalid Draft must never mean product success
        provider.name = "openai"
        clients, platform = [], None

        def client_factory(url):
            self.assertEqual(url, "http://127.0.0.1:8105")
            client = httpx.AsyncClient(transport=httpx.ASGITransport(
                app=platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT]), base_url=url)
            clients.append(client)
            return A2AAgentClient(url, httpx_client=client,
                headers={"Authorization": "Bearer " + self.environment["A2A_PLATFORM_SINGLE_AGENT_TOKEN"]})

        platform = await self.host(provider_factory=lambda settings: provider, a2a_client_factory=client_factory)
        self.addAsyncCleanup(platform.aclose)
        try:
            async with AsyncExitStack() as stack:
                for app in (*platform.agent_apps.values(), platform.orchestrator_app):
                    await stack.enter_async_context(app.router.lifespan_context(app))
                client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=platform.orchestrator_app), base_url="http://127.0.0.1:8000"))
                response = await client.post("/api/v1/runs", json={"scenarioId": str(fixtures.SCN_001_ID),
                    "requestText": "승인한 회원가입 기준을 구현해 주세요.",
                    "configuration": approved_run_configuration(platform).model_dump(mode="json", by_alias=True)})
                self.assertEqual(response.status_code, 201, response.text)
                run_id = response.json()["run"]["runId"]
                repository = platform.orchestrator_app.state.workflow_repository
                run = repository.get_run(UUID(run_id))
                self.assertNotEqual(getattr(run.verdict, "value", None), "SUCCESS")
                workspace = repository.get_workspace(run.workspace_id)
                self.assertEqual((Path(workspace.root_path) / "source/signup.py").read_bytes(), (self.seed / "signup.py").read_bytes())
                self.assertEqual(len(provider.requests), 1)
                self.assertEqual([item.agent_id for item in repository.list_agent_contexts(run.run_id)], ["single_agent"])
        finally:
            for client in clients:
                await client.aclose()

    async def test_creation_uses_one_provider_two_apps_and_once_only_cleanup(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(self.providers[0].role, SingleAgentIdentity.SINGLE_AGENT)
        self.assertEqual({item.name for item in platform.endpoints}, {"orchestrator", "single_agent"})
        self.assertEqual(set(platform.agent_apps), {SingleAgentIdentity.SINGLE_AGENT})
        self.assertEqual(approved_run_configuration(platform).architecture, "SINGLE_AGENT")
        self.assertEqual(self.providers[0].completed, 0)
        async with AsyncExitStack() as stack:
            for app in (*platform.agent_apps.values(), platform.orchestrator_app):
                await stack.enter_async_context(app.router.lifespan_context(app))
            client = await stack.enter_async_context(httpx.AsyncClient(
                transport=httpx.ASGITransport(app=platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT]),
                base_url="http://127.0.0.1:8105"))
            response = await client.get("/health")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["executionReady"])
            self.assertFalse((self.directory / ".data/workspaces").exists())
        await platform.aclose()
        await platform.aclose()
        self.assertEqual(self.providers[0].closed, 1)

    async def test_wrong_submission_mode_rejected_before_source_or_model_calls(self):
        platform = await self.host()
        self.addAsyncCleanup(platform.aclose)
        config = approved_run_configuration(platform).model_copy(update={"architecture": "MULTI_AGENT"})
        with self.assertRaises(HostFactoryError):
            platform.orchestrator_app.state.submission_validator(config)
        self.assertEqual(self.providers[0].completed, 0)
        self.assertFalse((self.directory / ".data/workspaces").exists())

    async def test_failed_single_composition_closes_sole_allocated_provider(self):
        with patch.object(host_module, "create_single_platform", side_effect=ValueError("inert")):
            with self.assertRaises(HostFactoryError):
                await self.host()
        self.assertEqual(len(self.providers), 1)
        self.assertEqual(self.providers[0].closed, 1)


class SingleHostCLITests(_SingleFixture, unittest.TestCase):
    def argv(self, *extra):
        return ["--architecture", "SINGLE_AGENT", "--runtime", str(self.runtime_file),
            "--tools", str(self.tools_file), "--source", str(self.source_file), *extra]

    def test_default_check_and_secret_check_never_allocate_or_listen(self):
        for extra in ((), ("--check-secrets",)):
            with self.subTest(extra=extra), patch.dict(os.environ, self.environment), \
                    patch.object(cli, "create_host", new_callable=AsyncMock) as created, \
                    patch.object(cli, "run_platform", new_callable=AsyncMock) as runner, \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
                self.assertEqual(cli.main(self.argv(*extra)), 0)
            self.assertFalse(json.loads(output.getvalue())["executionReady"])
            self.assertEqual(errors.getvalue(), "")
            created.assert_not_called()
            runner.assert_not_called()
            self.assertFalse((self.directory / ".data").exists())

    def test_explicit_start_passes_single_mode_and_closes_platform(self):
        platform = type("Owned", (), {"aclose": AsyncMock()})()
        with patch.object(cli, "create_host", new_callable=AsyncMock, return_value=platform) as created, \
                patch.object(cli, "run_platform", new_callable=AsyncMock) as runner, redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(self.argv("--start")), 0)
        self.assertEqual(created.await_args.kwargs["architecture"], "SINGLE_AGENT")
        runner.assert_awaited_once()
        platform.aclose.assert_awaited_once()

    def test_runtime_only_check_accepts_explicit_single_without_secret_disclosure(self):
        with redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(check_configuration.main(["--architecture", "SINGLE_AGENT", "--file", str(self.runtime_file)]), 0)
        self.assertEqual(errors.getvalue(), "")
        self.assertFalse(json.loads(output.getvalue())["executionReady"])
        self.assertNotIn(str(self.directory), output.getvalue())
        self.assertFalse((self.directory / ".data").exists())


class SecurityHostProjectionTests(unittest.TestCase):
    def test_approved_security_self_tests_reach_existing_runtime_for_both_architectures(self):
        base = services_fixture.HostRuntimeServicesTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        data = tool_fixture.configuration_data()
        data["unit"]["scopes"].append({"name": "security-self-tests", "kind": "QA_TESTS", "role": "SECURITY"})
        base.tools = decode_tool_configuration(json.dumps(data))
        configuration = services_fixture.RunConfiguration
        for mode in ("MULTI_AGENT", "SINGLE_AGENT"):
            with self.subTest(architecture=mode), patch.object(services_fixture, "RunConfiguration",
                    side_effect=lambda **values: configuration(architecture=mode, **values)):
                security = base.host().security(base.execution(fixtures.AgentRole.SECURITY))
                self.assertEqual(security.test_selectors, ("security-self-tests",))
                self.assertEqual({scope.role for scope in security.configuration.unit_test_configuration.scopes}, {"SECURITY"})
        self.assertNotIn("security-self-tests", [scope.name for scope in base.tools.unit_configuration_for(fixtures.AgentRole.QA).scopes])

    def test_unsupported_security_protected_case_binding_is_rejected_not_silently_dropped(self):
        base = SingleHostConfigurationTests("runTest")
        base.setUp()
        self.addCleanup(base.doCleanups)
        base.tools["unit"]["scopes"].append({**tool_fixture.protected_unit(), "role": "SECURITY"})
        base.write()
        with self.assertRaises(HostFactoryError):
            base.inspect()


if __name__ == "__main__":
    unittest.main()
