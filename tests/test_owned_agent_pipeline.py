"""Owned Stage 34 composition: actual four executors over official SDK HTTP.

Provider output and Docker CLI are synthetic. The MCP adapter calls the real
dispatcher, Sandbox, private receipt and Artifact boundaries, not SDK stdio.
Git/SQLite/archive provenance is real and confined to private temporary inert
fixtures; neither generated product code nor fixture tests execute on the Host.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx

from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from agents.llm.contracts import ToolCall
from agents.platform.composition import create_platform
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import BuildConfiguration
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import SecurityScanConfiguration, SecurityScannerProfile
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.test_reports import TestReportTools
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.core.config import Settings
from orchestrator.domain import (
    A2ATaskState, AgentRole, SCN_001_ID, SCENARIO_REGISTRY,
    WorkflowStatus, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, ExecutionLimits, ModelConfiguration, RunConfiguration,
)
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.validation_artifacts import ValidationOutcome
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import CLIResult, ExecutionProfile, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry
from test_llm_runtime import FakeProvider, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker, IMAGE_ID


_SCANNER_REF = "https://scanner.example.invalid/owned-pipeline/v1"


def _task_input(request):
    return json.loads(json.loads(request.input_items_json)[0]["content"])["taskInput"]


def _workspace_id(request):
    return _task_input(request)["workspaceId"]


class OwnedAgentPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean_environment = patch.dict(os.environ, {}, clear=True)
        clean_environment.start()
        self.addCleanup(clean_environment.stop)
        directory = TemporaryDirectory(prefix="a2a-owned-agent-pipeline-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.artifacts = ArtifactStore(self.repository, self.registry)
        self.scenario = SCENARIO_REGISTRY[SCN_001_ID]
        self.seed = self.directory / "trusted-seed"
        self.seed.mkdir()
        self.lock_bytes = b"trusted-inert-fixture==1.0\n"
        (self.seed / "requirements.lock").write_bytes(self.lock_bytes)
        (self.seed / "signup.py").write_text(
            "def signup():\n    return 'trusted-inert-baseline'\n", encoding="utf-8")
        self.git("init", "--object-format=sha1")
        self.git("add", "requirements.lock", "signup.py")
        self.git("commit", "-m", "trusted inert owned pipeline fixture")
        self.baseline_commit = self.git("rev-parse", "HEAD").strip()
        self.baseline_snapshot = GitSnapshotBuilder(self.seed).build(self.baseline_commit, "requirements.lock")
        self.model = ModelConfiguration(provider="fake", model_id="fake-model", temperature=0, seed=17)
        self.configuration = RunConfiguration(model=self.model,
            starting_commit_hash=self.baseline_commit,
            starting_snapshot_sha256=self.baseline_snapshot.snapshot_sha256,
            limits=ExecutionLimits(runtime_budget_ms=30000),
            environment=ExecutionBaseline(container_image_digest=IMAGE_ID,
                dependency_lock_hash="sha256:" + hashlib.sha256(self.lock_bytes).hexdigest(),
                hardware_profile="isolated-fake-owned-pipeline"), scanner_profile_ref=_SCANNER_REF)
        self.settings = {role: AgentSettings(role=role,
            database_path=self.directory / (role.value.lower() + "-tasks.sqlite3"),
            llm_provider="fake", llm_model_id="fake-model", llm_temperature=0, llm_seed=17,
            log_level="CRITICAL", _env_file=None) for role in AgentRole}
        self.orchestrator_settings = Settings(database_path=str(self.repository.database_path),
            workspace_root=str(self.registry.base_path), log_level="CRITICAL", _env_file=None,
            **{role.value.lower() + "_agent_url": settings.agent_base_url
               for role, settings in self.settings.items()})
        self.service_contexts, self.sessions, self.dockers = [], [], {}
        self.preparations, self.http_requests, self.http_clients = [], [], []
        self.qa_failure = False
        self.platform = None

    def git(self, *arguments):
        result = subprocess.run([
            "git", "-c", "user.name=Owned Pipeline Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments,
        ], cwd=self.seed, env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout

    def prepare_workspace(self, run_id):
        self.preparations.append(run_id)
        run = self.repository.get_run(run_id)
        record = self.registry.provision(run.workspace_id, run_id=run_id)
        # Trusted baseline bytes only. No generated source command is invoked.
        shutil.copytree(self.seed, Path(record.root_path) / "source", dirs_exist_ok=True)

    def providers(self):
        planner = text_response(json.dumps({"kind": "PLAN", "questions": [], "implementationPlan": [{
            "taskId": "TASK-001", "title": "동결 회원가입 기준 구현", "description": "모든 보호된 기준을 구현한다.",
            "requirementIds": [str(value) for value in self.scenario.requirement_ids], "dependsOn": [],
        }]}, ensure_ascii=False))

        def developer_write(request):
            return tool_response(ToolCall(call_id="developer-write", name="write_source_file",
                arguments_json=json.dumps({"workspaceId": _workspace_id(request), "path": "source/signup.py",
                    "content": "def signup():\n    return 'generated-inert-candidate'\n"})))

        def qa_write(request):
            return tool_response(ToolCall(call_id="qa-write", name="write_test_file",
                arguments_json=json.dumps({"workspaceId": _workspace_id(request),
                    "path": "outputs/qa/tests/test_signup.py", "content": "# inert fixture; not executed on Host\n"})))

        def qa_ready(request):
            requirements = self.scenario.requirement_ids_for(RequirementValidator.QA)
            return text_response(json.dumps({"kind": "READY", "questions": [], "cases": [{
                "toolName": "run_unit_tests", "selector": "qa-unit",
                "testId": f"test_signup.Signup.test_req_{index}", "requirementId": str(requirement),
                "title": f"독립 QA 기준 {index}", "expectedResult": "동결 기준을 충족한다.",
            } for index, requirement in enumerate(requirements, 1)]}, ensure_ascii=False))

        def security_ready(request):
            measured = _task_input(request)["measuredSecurity"]
            return text_response(json.dumps({"kind": "READY", "questions": [],
                "requirementReviews": [{"requirementId": str(requirement), "proposedOutcome": "UNVERIFIED",
                    "rationale": "정적 경고 부재만으로 보호된 기준을 증명할 수 없습니다.", "references": []}
                    for requirement in self.scenario.requirement_ids_for(RequirementValidator.SECURITY)],
                "findingReviews": [{"findingId": finding["findingId"], "proposedDisposition": "SUSPECTED",
                    "rationale": "독립 검증이 필요합니다.", "references": []}
                    for profile in measured["profiles"] for finding in profile["findings"]]}, ensure_ascii=False))

        return {AgentRole.PLANNER: FakeProvider(planner),
            AgentRole.DEVELOPER: FakeProvider(developer_write,
                text_response(json.dumps({"kind": "READY", "summary": "동결 기준 구현 후보입니다.", "questions": []}, ensure_ascii=False))),
            AgentRole.QA: FakeProvider(qa_write, qa_ready), AgentRole.SECURITY: FakeProvider(security_ready)}

    @asynccontextmanager
    async def peer_client(self, configuration):
        role = configuration.binding.role
        docker = FakeDocker()
        self.dockers.setdefault(role, []).append(docker)
        sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=docker)
        files = FileTools(self.artifacts, frozen_source=configuration.frozen_source)
        handlers = files.handlers(role)
        if role is AgentRole.DEVELOPER:
            build = BuildTools(self.artifacts, sandbox, BuildOutputStore(self.repository),
                configuration=configuration.build_configuration, max_call_seconds=configuration.max_call_seconds)
            handlers = {**handlers, **build.handlers(role)}
        elif role is AgentRole.QA:
            case_ids = [f"test_signup.Signup.test_req_{index}" for index in range(
                1, len(self.scenario.requirement_ids_for(RequirementValidator.QA)) + 1)]
            failed = int(self.qa_failure)
            docker.exit_code = failed
            docker.start_result = CLIResult(returncode=failed, stdout=json.dumps({"format": "unittest-v1",
                "total": len(case_ids), "passed": len(case_ids) - failed, "failed": failed, "skipped": 0,
                "tests": [{"testId": identity, "outcome": "FAIL" if self.qa_failure and index == 0 else "PASS"}
                          for index, identity in enumerate(case_ids)]}).encode(), stderr=b"")
            unit = UnitTestTools(self.artifacts, sandbox, UnitTestOutputStore(self.repository),
                configuration=configuration.unit_test_configuration, max_call_seconds=configuration.max_call_seconds)
            browser = BrowserTestTools(self.artifacts, sandbox, BrowserTestOutputStore(self.repository),
                configuration=None, max_call_seconds=configuration.max_call_seconds)
            handlers = {**handlers, **unit.handlers(role), **browser.handlers(role),
                **TestReportTools(unit, browser).handlers(role)}
        else:
            profile = configuration.security_scan_configuration.profiles[0]
            docker.start_result = CLIResult(returncode=0, stdout=json.dumps({"format": "bandit-v1",
                "profileName": profile.name, "scanner": "bandit", "scannerVersion": profile.scanner_version,
                "ruleIds": sorted(profile.rule_ids), "profileRef": profile.profile_ref,
                "scannedFiles": ["signup.py"], "findings": []}).encode(), stderr=b"")
            scans = SecurityScanTools(self.artifacts, sandbox, SecurityScanOutputStore(self.repository),
                configuration=configuration.security_scan_configuration, max_call_seconds=configuration.max_call_seconds)
            handlers = {**handlers, **scans.handlers(role)}
        dispatcher = MCPDispatcher(configuration.binding, self.registry, handlers=handlers,
            max_call_seconds=configuration.max_call_seconds)
        session = DispatcherSession(dispatcher)
        self.sessions.append((role, session))
        client = BoundMCPClient(configuration=configuration, _client=type("SDKPeerAdapter", (), {"session": session})())
        try:
            yield client
        finally:
            client._state.active = False

    def services(self, role, execution):
        self.service_contexts.append((role, execution))
        binding = MCPBinding(role=role, agent_role=role, run_id=execution.metadata.run_id,
            workspace_id=execution.configuration.workspace_id)
        values = {"binding": binding, "database_path": self.repository.database_path,
            "workspace_root": self.registry.base_path, "max_call_seconds": 5}
        limits = SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5)
        if role is AgentRole.DEVELOPER:
            values["build_configuration"] = BuildConfiguration(profile=ExecutionProfile(name="fixture-build",
                tool_name="run_build", argv=("/usr/local/bin/python", "-I", "-B", "build.py"), limits=limits,
                image_reference=IMAGE_ID))
            return DeveloperRuntimeServices(self.repository, self.registry, self.artifacts,
                mcp_configuration=MCPChildConfiguration(**values), baseline_commit_hash=self.baseline_commit,
                repository_id="owned-pipeline-fixture", lock_path="requirements.lock", client_factory=self.peer_client)
        values["frozen_source"] = FrozenSourceSelection(project_artifact_id=execution.source.artifact_id,
            snapshot_sha256=execution.source.snapshot_sha256)
        if role is AgentRole.QA:
            values["unit_test_configuration"] = UnitTestConfiguration(
                scopes=(UnitTestScope(name="qa-unit", kind="QA_TESTS"),), limits=limits, image_reference=IMAGE_ID)
            return QARuntimeServices(self.repository, self.registry, self.artifacts,
                mcp_configuration=MCPChildConfiguration(**values), client_factory=self.peer_client)
        values["security_scan_configuration"] = SecurityScanConfiguration(
            profiles=(SecurityScannerProfile(name="python-security", scanner_version="1.8.6",
                rule_ids=("B101", "B307"), profile_ref=_SCANNER_REF),), limits=limits, image_reference=IMAGE_ID)
        return SecurityRuntimeServices(self.repository, self.registry, self.artifacts,
            mcp_configuration=MCPChildConfiguration(**values), client_factory=self.peer_client)

    def a2a_client(self, url):
        self.http_requests.append(url)
        role = next(role for role, settings in self.settings.items() if settings.agent_base_url == url)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.platform.agent_apps[role]), base_url=url)
        self.http_clients.append(client)
        return A2AAgentClient(url, httpx_client=client)

    @asynccontextmanager
    async def running_platform(self, providers=None, *, model_cap=8, prepare=None):
        providers = self.providers() if providers is None else providers
        limits = LLMLimits(max_model_calls=model_cap, max_tool_calls=12,
            model_timeout_seconds=2, max_output_tokens=4096)
        self.platform = create_platform(repository=self.repository, workspace_registry=self.registry,
            artifact_store=self.artifacts, orchestrator_settings=self.orchestrator_settings,
            agent_settings={role: settings.model_copy(update={"llm_limits": limits})
                            for role, settings in self.settings.items()}, providers=providers,
            developer_services_factory=lambda execution: self.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: self.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: self.services(AgentRole.SECURITY, execution),
            prepare_workspace=self.prepare_workspace if prepare is None else prepare,
            limits=limits, a2a_client_factory=self.a2a_client)
        try:
            async with AsyncExitStack() as stack:
                for app in self.platform.agent_apps.values():
                    await stack.enter_async_context(app.router.lifespan_context(app))
                app = self.platform.orchestrator_app
                await stack.enter_async_context(app.router.lifespan_context(app))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://127.0.0.1:8000") as client:
                    yield client, providers
        finally:
            for client in self.http_clients:
                await client.aclose()
            await self.platform.aclose()

    def submission(self):
        return {"scenarioId": str(SCN_001_ID), "requestText": "보호된 회원가입 요구사항을 구현해줘.",
            "configuration": self.configuration.model_dump(mode="json", by_alias=True)}

    async def test_initial_cycle_actual_four_agents_share_source_budget_and_provenance(self):
        async with self.running_platform() as (client, providers):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=self.submission()), timeout=20)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["dispatchStatus"], "SCHEDULED")
            run_id = UUID(response.json()["run"]["runId"])
            run = self.repository.get_run(run_id)
            steps = self.repository.list_steps(run_id)
            self.assertEqual({step.agent_role for step in steps}, set(AgentRole))
            self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in steps), steps)
            self.assertTrue(all(step.a2a_task_state is A2ATaskState.COMPLETED for step in steps))
            self.assertTrue(all(step.a2a_task_id and step.agent_context_id for step in steps))
            self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
            self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertNotEqual(getattr(run.verdict, "value", None), "SUCCESS")
            artifacts = self.repository.list_project_artifacts(run_id)
            source = next(item for item in artifacts if item.artifact_type == "SOURCE")
            qa = next(item for item in artifacts if item.artifact_type == "QA_REPORT")
            security = next(item for item in artifacts if item.artifact_type == "SECURITY_REPORT")
            build = next(item for item in artifacts if item.artifact_type == "BUILD_REPORT")
            steps_by_role = {step.agent_role: step for step in steps}
            requirement = self.repository.get_planning_artifact(run_id)
            self.assertEqual(requirement.a2a_task_id, steps_by_role[AgentRole.PLANNER].a2a_task_id)
            self.assertEqual(steps_by_role[AgentRole.DEVELOPER].input_artifact_ids, [requirement.artifact_id])
            for role, artifact in ((AgentRole.DEVELOPER, source), (AgentRole.QA, qa),
                                   (AgentRole.SECURITY, security)):
                step = steps_by_role[role]
                self.assertEqual((artifact.run_id, artifact.workflow_step_id, artifact.a2a_task_id),
                    (run_id, step.workflow_step_id, step.a2a_task_id))
                self.assertIn(artifact.a2a_artifact_id, step.a2a_artifact_ids)
                self.assertIn(artifact.artifact_id, step.output_artifact_ids)
                if role in (AgentRole.QA, AgentRole.SECURITY):
                    self.assertEqual(step.input_artifact_ids, [source.artifact_id])
            self.artifacts.verify_candidate(source)
            self.assertEqual((qa.execution_manifest.project_artifact_id,
                security.execution_manifest.project_artifact_id, build.source_artifact_id),
                (source.artifact_id,) * 3)
            self.assertEqual(qa.execution_manifest, source.execution_manifest())
            self.assertEqual(security.execution_manifest, source.execution_manifest())
            self.assertTrue(qa.passed)
            self.assertTrue(security.has_unverified)
            self.assertTrue(all(item.outcome is ValidationOutcome.UNVERIFIED for item in security.requirement_results))
            self.assertIs(build.execution_outcome, ToolExecutionOutcome.PASS)
            self.assertTrue(all(item.tool_evidence is not None for item in qa.tests))
            self.assertEqual(self.preparations, [run_id])
            configuration = self.repository.get_run_configuration(run_id)
            budget = self.platform.budgets.resolve(configuration)
            self.assertTrue(all(execution.budget is budget for _role, execution in self.service_contexts))
            self.assertEqual(budget.model_calls, 6)
            self.assertEqual({role: len(provider.requests) for role, provider in providers.items()},
                {AgentRole.PLANNER: 1, AgentRole.DEVELOPER: 2, AgentRole.QA: 2, AgentRole.SECURITY: 1})
            self.assertEqual({url for url in self.http_requests}, {settings.agent_base_url for settings in self.settings.values()})
            tools = {role: [name for session_role, session in self.sessions if session_role is role
                           for name, _arguments, _options in session.calls] for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY)}
            self.assertEqual(tools, {AgentRole.DEVELOPER: ["write_source_file", "run_build"],
                AgentRole.QA: ["write_test_file", "run_unit_tests"], AgentRole.SECURITY: ["run_security_scan"]})
            events, _total = self.repository.list_events(run_id, limit=500, offset=0)
            self.assertEqual({event.workflow_step_id for event in events if event.event_type == "A2A_MESSAGE_SENT"},
                {step.workflow_step_id for step in steps})
            self.assertTrue(any(event.event_type == "MCP_TOOL_FINISHED" for event in events))

    async def test_missing_runtime_configuration_is_422_before_run_or_model(self):
        async with self.running_platform() as (client, providers):
            response = await client.post("/api/v1/runs", json={"scenarioId": str(SCN_001_ID),
                "requestText": "설정 없는 요청은 실행하면 안 됩니다."})
            self.assertEqual(response.status_code, 422, response.text)
            with self.repository._connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0], 0)
            self.assertFalse(self.preparations)
            self.assertFalse(self.http_requests)
            self.assertFalse(self.service_contexts)
            self.assertTrue(all(not provider.requests for provider in providers.values()))

    async def test_preparation_failure_stops_before_agent_http_and_budget_spending(self):
        def failed_preparation(run_id):
            self.preparations.append(run_id)
            raise ValueError("private fixture diagnostic must not become public")

        async with self.running_platform(prepare=failed_preparation) as (client, providers):
            response = await client.post("/api/v1/runs", json=self.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()["run"]["runId"])
            run = self.repository.get_run(run_id)
            self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertEqual(self.preparations, [run_id])
            self.assertFalse(self.http_requests)
            self.assertFalse(self.service_contexts)
            self.assertTrue(all(not provider.requests for provider in providers.values()))
            budget = self.platform.budgets.resolve(self.repository.get_run_configuration(run_id))
            self.assertEqual((budget.model_calls, budget.tool_calls, budget.total_tokens), (0, 0, 0))
            self.assertFalse(self.repository.list_project_artifacts(run_id))

    async def test_model_call_cap_is_shared_across_all_roles_without_reset(self):
        async with self.running_platform(model_cap=3) as (client, providers):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=self.submission()), timeout=20)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()["run"]["runId"])
            run = self.repository.get_run(run_id)
            self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertEqual((run.code_version, run.fix_attempt), (1, 0))
            self.assertEqual(sum(len(provider.requests) for provider in providers.values()), 3)
            self.assertEqual({role: len(provider.requests) for role, provider in providers.items()},
                {AgentRole.PLANNER: 1, AgentRole.DEVELOPER: 2, AgentRole.QA: 0, AgentRole.SECURITY: 0})
            budget = self.platform.budgets.resolve(self.repository.get_run_configuration(run_id))
            self.assertEqual(budget.model_calls, 3)
            self.assertTrue(all(execution.budget is budget for _role, execution in self.service_contexts))
            self.assertEqual(len(self.repository.list_steps(run_id)), 4)
            artifacts = self.repository.list_project_artifacts(run_id)
            self.assertEqual(len([item for item in artifacts if item.artifact_type == "SOURCE"]), 1)
            self.assertFalse([item for item in artifacts if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}])

    async def test_measured_qa_failure_and_unverified_security_keep_issue_without_starting_fix(self):
        self.qa_failure = True
        async with self.running_platform() as (client, _providers):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=self.submission()), timeout=20)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()["run"]["runId"])
            run = self.repository.get_run(run_id)
            self.assertEqual((run.status, run.fix_attempt, run.code_version),
                (WorkflowStatus.HUMAN_REVIEW, 0, 1))
            steps = self.repository.list_steps(run_id)
            self.assertEqual(len(steps), 4)
            self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in steps))
            artifacts = self.repository.list_project_artifacts(run_id)
            source = next(item for item in artifacts if item.artifact_type == "SOURCE")
            qa = next(item for item in artifacts if item.artifact_type == "QA_REPORT")
            security = next(item for item in artifacts if item.artifact_type == "SECURITY_REPORT")
            failed = [item for item in qa.tests if item.outcome is ValidationOutcome.FAIL]
            self.assertEqual(len(failed), 1)
            self.assertIsNotNone(failed[0].tool_evidence)
            self.assertFalse(qa.passed)
            self.assertTrue(security.has_unverified)
            self.assertEqual(qa.execution_manifest, source.execution_manifest())
            self.assertEqual(security.execution_manifest, source.execution_manifest())
            # Inspect the actual independently persisted Unit receipt, not
            # the model's proposal or a hand-written QA Artifact fixture.
            with self.repository._connection() as connection:
                records = connection.execute(
                    "SELECT execution_manifest_id FROM unit_test_execution_records WHERE run_id=?",
                    (str(run_id),)).fetchall()
            self.assertEqual(len(records), 1)
            receipt = UnitTestOutputStore(self.repository).get(run_id, UUID(records[0]["execution_manifest_id"]))
            self.assertEqual((receipt.exit_code, receipt.report.failed, receipt.source_artifact_id),
                (1, 1, source.artifact_id))
            self.assertEqual(receipt.workflow_step_id, qa.workflow_step_id)
            issues = self.repository.list_issue_records(run_id)
            self.assertEqual(len(issues), 1)
            issue = issues[0]
            self.assertEqual((issue.reporter, issue.category, issue.source_artifact_id, issue.report_artifact_id),
                (AgentRole.QA, "QA_ASSERTION_FAILURE", source.artifact_id, qa.artifact_id))
            self.assertEqual(issue.requirement_ids, [failed[0].requirement_id])
            self.assertEqual(issue.reference_id, failed[0].test_id)
            self.assertEqual(len([item for item in artifacts if item.artifact_type == "SOURCE"]), 1)
