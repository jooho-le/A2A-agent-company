"""Host composition checks; no live provider, listener, MCP or Docker calls."""

import asyncio
from dataclasses import FrozenInstanceError
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.composition import (
    OwnedAgentPlatform, PlatformConfigurationError, PlatformEndpoint,
    PlatformPreparationError, create_platform,
)
from agents.runtime.developer import DeveloperAgentExecutor
from agents.runtime.planner import PlannerAgentExecutor
from agents.runtime.qa import QAAgentExecutor
from agents.runtime.security import SecurityAgentExecutor
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.core.config import Settings
from orchestrator.domain import (
    AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, ExecutionLimits, ModelConfiguration, RunConfiguration,
    RunConfigurationArtifact,
)
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


class _Provider:
    name = "fake"

    def __init__(self):
        self.validated = []
        self.closed = 0

    def validate_configuration(self, model, output):
        self.validated.append((model, output))

    async def complete(self, request):
        raise AssertionError("No model invocation in composition tests")

    async def aclose(self):
        self.closed += 1


class OwnedCompositionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean = patch.dict(os.environ, {}, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        temporary = TemporaryDirectory(prefix="a2a-owned-composition-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "orchestrator.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.artifacts = ArtifactStore(self.repository, self.registry)
        self.limits = LLMLimits(max_model_calls=16, max_tool_calls=32)
        self.model = ModelConfiguration(provider="fake", model_id="fake-model",
            model_revision="frozen-revision", temperature=0, seed=17)
        self.roles = {role: AgentSettings(role=role, environment="test",
            database_path=self.directory / f"{role.value.lower()}.sqlite3",
            bearer_token=SecretStr(f"owned-private-{role.value.lower()}"),
            llm_provider=self.model.provider, llm_model_id=self.model.model_id,
            llm_model_revision=self.model.model_revision,
            llm_temperature=self.model.temperature, llm_seed=self.model.seed,
            llm_limits=self.limits) for role in AgentRole}
        self.providers = {role: _Provider() for role in AgentRole}
        values = {"database_path": str(self.repository.database_path),
            "workspace_root": str(self.registry.base_path), "environment": "test", "log_level": "CRITICAL"}
        for role, selected in self.roles.items():
            values[f"{role.value.lower()}_agent_url"] = selected.agent_base_url
            values[f"{role.value.lower()}_bearer_token"] = selected.bearer_token
        self.settings = Settings(**values)
        self.prepared = []

    def arguments(self):
        return {
            "repository": self.repository, "workspace_registry": self.registry,
            "artifact_store": self.artifacts, "orchestrator_settings": self.settings,
            "agent_settings": self.roles, "providers": self.providers,
            "developer_services_factory": lambda _execution: None,
            "qa_services_factory": lambda _execution: None,
            "security_services_factory": lambda _execution: None,
            "prepare_workspace": self.prepared.append, "limits": self.limits,
        }

    def platform(self, **changes):
        return create_platform(**(self.arguments() | changes))

    def configuration(self, **changes):
        values = {
            "model": self.model, "limits": ExecutionLimits(runtime_budget_ms=30_000),
            "environment": ExecutionBaseline(container_image_digest="sha256:" + "a" * 64,
                dependency_lock_hash="sha256:" + "b" * 64, hardware_profile="fixed-host"),
            "starting_commit_hash": "c" * 40,
            "scanner_profile_ref": "artifact://frozen-scanner/profile.json",
        }
        return RunConfiguration(**(values | changes))

    def create_claimed_run(self, platform, *, configuration=None):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 요구사항 구현",
            status=WorkflowStatus.RECEIVED)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        frozen = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
            workspace_id=run.workspace_id, configuration=configuration or self.configuration())
        workspace = WorkspaceRecord(workspace_id=run.workspace_id, run_id=run.run_id,
            root_path=str(self.registry.base_path / str(run.workspace_id)))
        self.repository.create_run(run, (step,), (), run_configuration=frozen, workspace=workspace)
        self.repository.claim_planner_dispatch(run.run_id)
        return run, frozen

    async def test_construction_is_inert_and_readiness_is_actual(self):
        platform = self.platform()
        self.assertIsInstance(platform, OwnedAgentPlatform)
        self.assertEqual(self.prepared, [])
        self.assertFalse(self.registry.base_path.exists())
        self.assertTrue(all(not selected.task_database_path.exists() for selected in self.roles.values()))
        self.assertTrue(all(not provider.validated for provider in self.providers.values()))
        self.assertEqual(len(platform.endpoints), 5)
        self.assertEqual(set(platform.agent_apps), set(AgentRole))
        self.assertIs(platform.orchestrator_app.state.workflow_repository, self.repository)
        self.assertIs(platform.orchestrator_app.state.workspace_registry, self.registry)
        self.assertFalse(platform.dispatcher._allow_fix_dispatch)
        for role, app in platform.agent_apps.items():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://unit") as client:
                response = await client.get("/health")
            self.assertEqual(response.json(), {"status": "ok", "role": role.value, "executionReady": True})
        await platform.aclose()

    async def test_every_actual_context_uses_same_registry_resolver(self):
        platform = self.platform()
        types = {AgentRole.PLANNER: PlannerAgentExecutor, AgentRole.DEVELOPER: DeveloperAgentExecutor,
            AgentRole.QA: QAAgentExecutor, AgentRole.SECURITY: SecurityAgentExecutor}
        for role, app in platform.agent_apps.items():
            executor = app.state.request_handler.agent_executor._executor
            self.assertIs(type(executor), types[role])
            self.assertIs(executor._context_factory._budget_resolver.__self__, platform.budgets)
        await platform.aclose()

    async def test_readonly_mapping_and_secret_safe_representations(self):
        platform = self.platform()
        with self.assertRaises(TypeError):
            platform.agent_apps[AgentRole.PLANNER] = platform.orchestrator_app
        with self.assertRaises(FrozenInstanceError):
            platform.endpoints = ()
        with self.assertRaises(FrozenInstanceError):
            platform.endpoints[0].port = 42
        self.assertEqual(repr(platform), "OwnedAgentPlatform()")
        for token in (selected.bearer_token.get_secret_value() for selected in self.roles.values()):
            self.assertNotIn(token, repr(platform.endpoints))
        await platform.aclose()

    async def test_valid_submission_checks_all_providers_without_calls(self):
        platform = self.platform()
        self.assertIsNone(platform.orchestrator_app.state.submission_validator(self.configuration()))
        for provider in self.providers.values():
            self.assertEqual(len(provider.validated), 1)
            self.assertEqual(provider.validated[0][0], self.model)
            provider.validated[0][1].schema.require_openai_strict()
        await platform.aclose()

    async def test_missing_frozen_runtime_inputs_are_rejected(self):
        platform = self.platform()
        invalid = [RunConfiguration(), self.configuration(model=None), self.configuration(environment=None),
            self.configuration(starting_commit_hash=None), self.configuration(scanner_profile_ref=None),
            self.configuration(limits=ExecutionLimits())]
        for configuration in invalid:
            with self.subTest(configuration=configuration.model_dump(exclude={"model"})):
                with self.assertRaisesRegex(PlatformConfigurationError, "^OWNED_AGENT_CONFIGURATION_INVALID$"):
                    platform.orchestrator_app.state.submission_validator(configuration)
        self.assertFalse(any(provider.validated for provider in self.providers.values()))
        await platform.aclose()

    async def test_network_allowlist_and_model_switch_are_rejected(self):
        platform = self.platform()
        invalid = [self.configuration(environment=ExecutionBaseline(
            container_image_digest="sha256:" + "a" * 64, dependency_lock_hash="sha256:" + "b" * 64,
            hardware_profile="fixed-host", network_policy="ALLOWLIST", allowed_hosts=("example.test",))),
            self.configuration(model=self.model.model_copy(update={"model_id": "different-model"}))]
        for configuration in invalid:
            with self.assertRaises(PlatformConfigurationError):
                platform.orchestrator_app.state.submission_validator(configuration)
        await platform.aclose()

    async def test_provider_denial_does_not_leak_credentials(self):
        platform = self.platform()
        def deny(_model, _output):
            raise RuntimeError("owned-private-qa provider body")
        self.providers[AgentRole.QA].validate_configuration = deny
        with self.assertRaisesRegex(PlatformConfigurationError, "^OWNED_AGENT_CONFIGURATION_INVALID$") as caught:
            platform.orchestrator_app.state.submission_validator(self.configuration())
        self.assertIsNone(caught.exception.__cause__)
        self.assertNotIn("owned-private", str(caught.exception))
        await platform.aclose()

    async def test_async_provider_configuration_validator_is_denied(self):
        platform = self.platform()
        async def invalid(_model, _output):
            raise AssertionError("Must not invoke a validator asynchronously")
        self.providers[AgentRole.QA].validate_configuration = invalid
        with self.assertRaises(PlatformConfigurationError):
            platform.orchestrator_app.state.submission_validator(self.configuration())
        await platform.aclose()

    async def test_each_role_and_provider_mapping_is_exact(self):
        for keyword in ("agent_settings", "providers"):
            mapping = dict(self.arguments()[keyword])
            mapping.pop(AgentRole.QA)
            with self.assertRaises(PlatformConfigurationError):
                self.platform(**{keyword: mapping})
            mapping = {role.value: value for role, value in self.arguments()[keyword].items()}
            with self.assertRaises(PlatformConfigurationError):
                self.platform(**{keyword: mapping})

    async def test_wrong_settings_role_provider_and_host_limits_are_rejected(self):
        for change in ({"role": AgentRole.DEVELOPER}, {"llm_provider": "other"},
                {"llm_model_id": "other"}, {"llm_limits": LLMLimits()}):
            mapping = self.roles | {AgentRole.QA: self.roles[AgentRole.QA].model_copy(update=change)}
            with self.subTest(change=change), self.assertRaises(PlatformConfigurationError):
                self.platform(agent_settings=mapping)

    async def test_missing_model_temperature_or_bad_provider_is_rejected(self):
        mapping = self.roles | {AgentRole.QA: self.roles[AgentRole.QA].model_copy(update={"llm_temperature": None})}
        with self.assertRaises(PlatformConfigurationError):
            self.platform(agent_settings=mapping)
        with self.assertRaises(PlatformConfigurationError):
            self.platform(providers=self.providers | {AgentRole.QA: object()})

    async def test_exact_urls_and_tokens_are_required(self):
        for update in ({"qa_agent_url": None}, {"qa_agent_url": self.roles[AgentRole.QA].agent_base_url + "/"},
                {"qa_agent_url": "http://owned-private-qa@127.0.0.1:8103"},
                {"qa_bearer_token": SecretStr("mismatch")}, {"qa_bearer_token": None}):
            with self.subTest(update_keys=tuple(update)), self.assertRaises(PlatformConfigurationError):
                self.platform(orchestrator_settings=self.settings.model_copy(update=update))

    async def test_distinct_loopback_role_ports_and_orchestrator_port(self):
        changes = [{"host": "localhost", "port": self.roles[AgentRole.PLANNER].listen_port}, {"port": 8000}]
        for change in changes:
            roles = self.roles | {AgentRole.QA: self.roles[AgentRole.QA].model_copy(update=change)}
            settings = self.settings.model_copy(update={"qa_agent_url": roles[AgentRole.QA].agent_base_url})
            with self.assertRaises(PlatformConfigurationError):
                self.platform(agent_settings=roles, orchestrator_settings=settings)

    async def test_unique_database_paths_include_orchestrator_and_aliases(self):
        for target in (self.repository.database_path, self.roles[AgentRole.PLANNER].task_database_path,
                self.directory / "uncreated" / ".." / "planner.sqlite3"):
            roles = self.roles | {AgentRole.QA: self.roles[AgentRole.QA].model_copy(update={"database_path": target})}
            with self.assertRaises(PlatformConfigurationError):
                self.platform(agent_settings=roles)

    async def test_capability_repository_identity_and_settings_paths_are_exact(self):
        other = SQLiteWorkflowRepository(self.directory / "other.sqlite3")
        for changes in ({"workspace_registry": WorkspaceRegistry(other, self.directory / "workspaces")},
                {"artifact_store": ArtifactStore(other, self.registry)},
                {"orchestrator_settings": self.settings.model_copy(update={"database_path": str(other.database_path)})},
                {"orchestrator_settings": self.settings.model_copy(update={"workspace_root": str(self.directory / "other-workspaces")})}):
            with self.assertRaises(PlatformConfigurationError):
                self.platform(**changes)

    async def test_trusted_factories_and_loopback_launcher_are_required(self):
        for field in ("developer_services_factory", "qa_services_factory", "security_services_factory", "prepare_workspace"):
            with self.assertRaises(PlatformConfigurationError):
                self.platform(**{field: None})
        for changes in ({"orchestrator_host": "0.0.0.0"}, {"orchestrator_port": True},
                {"orchestrator_port": 0}, {"a2a_client_factory": False}):
            with self.assertRaises(PlatformConfigurationError):
                self.platform(**changes)

    async def test_injected_a2a_factory_and_endpoint_bindings_are_retained(self):
        factory = lambda _url: None
        platform = self.platform(a2a_client_factory=factory, orchestrator_host="::1", orchestrator_port=9200)
        self.assertIs(platform.dispatcher._client_factory, factory)
        self.assertEqual(platform.endpoints[0].name, "orchestrator")
        self.assertEqual(platform.endpoints[0].host, "::1")
        self.assertEqual(platform.endpoints[0].port, 9200)
        self.assertIs(platform.endpoints[0].app, platform.orchestrator_app)
        for endpoint, role in zip(platform.endpoints[1:], AgentRole):
            self.assertEqual(endpoint.name, role.value.lower())
            self.assertIs(endpoint.app, platform.agent_apps[role])
        await platform.aclose()

    async def test_invalid_platform_endpoint_uses_stable_error(self):
        platform = self.platform()
        for changes in ({"name": "unknown"}, {"host": "0.0.0.0"}, {"port": False}, {"app": None}):
            with self.assertRaises(PlatformConfigurationError):
                PlatformEndpoint(**({"name": "orchestrator", "host": "127.0.0.1", "port": 8000,
                    "app": platform.orchestrator_app} | changes))
        await platform.aclose()

    async def test_prepare_runs_only_after_claim_without_implicit_provisioning(self):
        platform = self.platform()
        run, frozen = self.create_claimed_run(platform)
        await platform.dispatcher._before_dispatch(run.run_id)
        budget = platform.budgets.resolve(frozen)
        self.assertEqual(self.prepared, [run.run_id])
        self.assertFalse(self.registry.base_path.exists())
        budget.reserve_model_call()
        self.assertIs(platform.budgets.resolve(frozen), budget)
        self.assertEqual(budget.model_calls, 1)
        await platform.aclose()

    async def test_unknown_run_never_invokes_preparation(self):
        platform = self.platform()
        with self.assertRaisesRegex(PlatformPreparationError, "^OWNED_AGENT_PREPARATION_FAILED$"):
            await platform.dispatcher._before_dispatch(uuid4())
        self.assertEqual(self.prepared, [])
        await platform.aclose()

    async def test_direct_invalid_run_configuration_never_prepares(self):
        platform = self.platform()
        run, _frozen = self.create_claimed_run(platform, configuration=self.configuration(scanner_profile_ref=None))
        with self.assertRaises(PlatformPreparationError):
            await platform.dispatcher._before_dispatch(run.run_id)
        self.assertEqual(self.prepared, [])
        await platform.aclose()

    async def test_preparation_failure_is_credential_safe(self):
        def fail(_run_id):
            raise RuntimeError("owned-private-qa sensitive path")
        platform = self.platform(prepare_workspace=fail)
        run, _frozen = self.create_claimed_run(platform)
        with self.assertRaisesRegex(PlatformPreparationError, "^OWNED_AGENT_PREPARATION_FAILED$"):
            await platform.dispatcher._before_dispatch(run.run_id)
        await platform.aclose()

    async def test_preparation_deadline_preserves_shared_budget(self):
        async def slow(_run_id):
            await asyncio.sleep(.1)
        platform = self.platform(prepare_workspace=slow)
        run, _frozen = self.create_claimed_run(platform,
            configuration=self.configuration(limits=ExecutionLimits(runtime_budget_ms=40)))
        with self.assertRaises(LLMRuntimeError) as caught:
            await platform.dispatcher._before_dispatch(run.run_id)
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        await platform.aclose()

    async def test_cancelled_sync_preparation_is_drained(self):
        entered, release, finished = threading.Event(), threading.Event(), threading.Event()
        def worker(_run_id):
            entered.set()
            release.wait(2)
            finished.set()
        platform = self.platform(prepare_workspace=worker)
        run, _frozen = self.create_claimed_run(platform)
        task = asyncio.create_task(platform.dispatcher._before_dispatch(run.run_id))
        self.assertTrue(await asyncio.to_thread(entered.wait, 1))
        task.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(finished.is_set())
        await platform.aclose()

    async def test_actual_services_type_is_not_replaceable_with_stub(self):
        platform = self.platform()
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            executor = platform.agent_apps[role].state.request_handler.agent_executor._executor
            with self.assertRaises(PlatformConfigurationError):
                await executor._services_factory(None)
        await platform.aclose()

    async def test_provider_close_is_once_for_shared_provider_and_concurrent_calls(self):
        provider = _Provider()
        platform = self.platform(providers={role: provider for role in AgentRole})
        await asyncio.gather(platform.aclose(), platform.aclose())
        await platform.aclose()
        self.assertEqual(provider.closed, 1)
        with self.assertRaises(PlatformConfigurationError):
            platform.orchestrator_app.state.submission_validator(self.configuration())
        with self.assertRaises(PlatformPreparationError):
            await platform.dispatcher._before_dispatch(uuid4())

    async def test_close_failure_attempts_all_providers_and_hides_message(self):
        async def fail():
            raise RuntimeError("owned-private-qa raw close body")
        self.providers[AgentRole.QA].aclose = fail
        platform = self.platform()
        with self.assertRaisesRegex(PlatformConfigurationError, "^OWNED_AGENT_CONFIGURATION_INVALID$"):
            await platform.aclose()
        self.assertEqual(self.providers[AgentRole.PLANNER].closed, 1)
        self.assertEqual(self.providers[AgentRole.DEVELOPER].closed, 1)
        self.assertEqual(self.providers[AgentRole.SECURITY].closed, 1)

    async def test_cancelled_close_drains_all_providers_once(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow():
            self.providers[AgentRole.PLANNER].closed += 1
            entered.set()
            await release.wait()
        self.providers[AgentRole.PLANNER].aclose = slow
        platform = self.platform()
        closing = asyncio.create_task(platform.aclose())
        await entered.wait()
        closing.cancel()
        await asyncio.sleep(.01)
        self.assertFalse(closing.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        await platform.aclose()
        self.assertTrue(all(provider.closed == 1 for provider in self.providers.values()))

    async def test_invalid_api_submission_creates_no_run_or_workspace(self):
        platform = self.platform()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=platform.orchestrator_app),
                base_url="http://unit") as client:
            response = await client.post("/api/v1/runs", json={"scenarioId": str(SCN_001_ID),
                "requestText": "회원가입 구현", "configuration": {}})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"detail": "OWNED_AGENT_CONFIGURATION_INVALID"})
        with self.repository._connection() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0], 0)
        self.assertFalse(self.registry.base_path.exists())
        self.assertEqual(self.prepared, [])
        await platform.aclose()


if __name__ == "__main__":
    unittest.main()
