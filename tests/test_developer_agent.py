"""Opt-in Developer HTTP/LLM/MCP/Git/SQLite flow with fake provider/daemon.

The PeerAdapter calls the actual MCP dispatcher and Build/Sandbox/receipt
boundaries; it is not an SDK stdio transport or actual Docker execution. Real
Git commands operate only on trusted inert fixtures in private temp Workspaces.
Generated product code is never executed on the Host or claimed container-tested.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx

from agents.api.validation import parse_workflow_metadata
from agents.core.config import AgentSettings
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.main import create_app
from agents.roles.outputs import validate_completed_role_output
from agents.roles.planner_contract import validate_planner_decision
from agents.runtime.developer import DeveloperAgentExecutor, DeveloperExecutorConfigurationError
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.developer_services import DeveloperRuntimeServices
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import BuildConfiguration
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.files import FileTools
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.developer_output import parse_developer_output
from orchestrator.application.dispatch import _DEVELOPER_OUTPUT_CONTRACT
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import (
    A2ATaskState, AgentRole, SCN_001_ID, SCENARIO_REGISTRY,
    WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import (
    ExecutionBaseline, ExecutionLimits, ModelConfiguration, RunConfiguration,
    RunConfigurationArtifact,
)
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import CLIResult, ExecutionProfile, SandboxError, SandboxErrorCode, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry
from test_llm_runtime import FakeProvider, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker, IMAGE_ID


class DeveloperAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean_environment = patch.dict(os.environ, {}, clear=True)
        clean_environment.start()
        self.addCleanup(clean_environment.stop)
        temporary = TemporaryDirectory(prefix="a2a-developer-agent-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.agent_database = self.directory / "developer.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.directory / "orchestrator.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.artifacts = ArtifactStore(self.repository, self.registry)
        self.scenario = SCENARIO_REGISTRY[SCN_001_ID]
        self.model = ModelConfiguration(provider="fake", model_id="fake-model", temperature=0, seed=17)
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="보호된 회원가입 요구사항을 구현해줘.",
                               status=WorkflowStatus.PLANNING)
        self.planner_step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.SUCCEEDED, a2a_task_state=A2ATaskState.COMPLETED,
            a2a_task_id="planner-opaque-task", agent_context_id="planner-opaque-context",
            a2a_artifact_ids=["planner-opaque-artifact"])
        self.requirement_artifact_id = uuid4()
        self.plan = validate_planner_decision({
            "kind": "PLAN", "questions": [], "implementationPlan": [{
                "taskId": "TASK-001", "title": "회원가입 구현", "description": "보호된 모든 기준을 구현한다.",
                "requirementIds": [str(value) for value in self.scenario.requirement_ids], "dependsOn": [],
            }],
        }, self.scenario).plan
        self.lock_bytes = b"trusted-inert-fixture==1.0\n"
        self.configuration = RunConfigurationArtifact(run_id=self.run.run_id,
            workspace_id=self.run.workspace_id, scenario_id=SCN_001_ID,
            configuration=RunConfiguration(model=self.model, limits=ExecutionLimits(runtime_budget_ms=15000),
                environment=ExecutionBaseline(container_image_digest=IMAGE_ID,
                    dependency_lock_hash="sha256:" + hashlib.sha256(self.lock_bytes).hexdigest(),
                    hardware_profile="isolated-fake-daemon-fixture")))
        self.root = self.registry.base_path / str(self.run.workspace_id)
        workspace = WorkspaceRecord(workspace_id=self.run.workspace_id, run_id=self.run.run_id,
                                    root_path=str(self.root))
        self.repository.create_run(self.run, (self.planner_step,), (),
            run_configuration=self.configuration, workspace=workspace)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)
        self.source = self.root / "source"
        (self.source / "requirements.lock").write_bytes(self.lock_bytes)
        self.initial_source = "def signup():\n    return 'trusted-fixture-baseline'\n"
        (self.source / "signup.py").write_text(self.initial_source, encoding="utf-8")
        self.git("init", "--object-format=sha1")
        self.git("add", "requirements.lock", "signup.py")
        self.git("commit", "-m", "trusted inert fixture baseline")
        self.baseline_commit = self.git("rev-parse", "HEAD").strip()
        self.run, self.step = self.repository.create_developer_step_from_plan(
            self.run.run_id, self.planner_step.workflow_step_id,
            a2a_artifact_id="planner-opaque-artifact", requirement_ids=self.scenario.requirement_ids,
            project_artifact_id=self.requirement_artifact_id, developer_configured=True,
            requirement_payload=self.plan.model_dump(mode="json", by_alias=True))
        self.metadata = A2AWorkflowMetadata(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=SCN_001_ID, attempt=0, requirement_ids=self.scenario.requirement_ids,
            code_version=1, project_artifact_ids=(self.requirement_artifact_id,))
        self.budget = ExecutionBudget(runtime_budget_ms=15000, limits=LLMLimits(max_model_calls=8,
            max_tool_calls=10, model_timeout_seconds=2, max_output_tokens=4096))
        self.loader = SQLiteDeveloperContextLoader(self.repository, lambda _configuration: self.budget)
        self.docker = FakeDocker()
        self.sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=self.docker)
        self.outputs = BuildOutputStore(self.repository)
        self.tool_store = ToolExecutionStore(self.repository)
        self.binding = MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
            run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        # This fixed build.py argv is inspected by FakeDocker only. No such
        # script exists or is executed on the Host by this test adapter.
        self.profile = ExecutionProfile(name="fixture-build", tool_name="run_build",
            argv=("/usr/local/bin/python", "-I", "-B", "build.py"),
            limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5))
        self.mcp_configuration = MCPChildConfiguration(binding=self.binding,
            database_path=self.repository.database_path, workspace_root=self.registry.base_path,
            build_configuration=BuildConfiguration(profile=self.profile), max_call_seconds=5)
        files = FileTools(self.artifacts)
        build = BuildTools(self.artifacts, self.sandbox, self.outputs,
            configuration=self.mcp_configuration.build_configuration, max_call_seconds=5)
        self.dispatcher = MCPDispatcher(self.binding, self.registry,
            handlers={**files.handlers(AgentRole.DEVELOPER), **build.handlers(AgentRole.DEVELOPER)},
            max_call_seconds=5)
        self.sessions, self.context_calls, self.service_calls, self.usages = [], [], [], []

    def git(self, *arguments):
        result = subprocess.run([
            "git", "-c", "user.name=Developer Test Fixture", "-c", "user.email=fixture@example.invalid",
            "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments,
        ], cwd=self.source, env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_TERMINAL_PROMPT": "0"},
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        return result.stdout

    def context_factory(self, context):
        self.context_calls.append(parse_workflow_metadata(context.metadata))
        return self.loader(context)

    @asynccontextmanager
    async def peer_client(self, configuration):
        self.assertEqual(configuration.binding, self.binding)
        session = DispatcherSession(self.dispatcher)
        self.sessions.append(session)
        sdk = type("SDKPeerAdapter", (), {"session": session})()
        client = BoundMCPClient(configuration=configuration, _client=sdk)
        try:
            yield client
        finally:
            client._state.active = False

    def services_factory(self, execution):
        self.service_calls.append(execution.metadata)
        return DeveloperRuntimeServices(self.repository, self.registry, self.artifacts,
            mcp_configuration=self.mcp_configuration, baseline_commit_hash=self.baseline_commit,
            repository_id="developer-agent-fixture", lock_path="requirements.lock", client_factory=self.peer_client)

    def executor(self, selected_provider, **changes):
        values = {"provider": selected_provider, "context_factory": self.context_factory,
            "services_factory": self.services_factory, "usage_sink": self.usages.append}
        values.update(changes)
        return DeveloperAgentExecutor(**values)

    def payload(self):
        artifact = self.repository.get_planning_artifact(self.run.run_id)
        return {"workspaceId": str(self.run.workspace_id), "scenario": self.configuration.scenario_contract,
            "runConfiguration": self.configuration.to_artifact_json(),
            "plan": self.plan.model_dump(mode="json", by_alias=True),
            "sourceArtifact": {"a2aArtifactId": artifact.a2a_artifact_id,
                "projectArtifactId": str(artifact.artifact_id), "artifactVersion": artifact.artifact_version},
            "outputContract": deepcopy(_DEVELOPER_OUTPUT_CONTRACT)}

    def wire(self, *, payload=None, metadata=None, task_id=None, context_id=None):
        return MessageToDict(build_send_message_request(self.payload() if payload is None else payload,
            self.metadata if metadata is None else metadata, task_id=task_id, context_id=context_id))

    @staticmethod
    def headers():
        return {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}

    @asynccontextmanager
    async def client_for(self, executor=None):
        settings = AgentSettings(role="DEVELOPER", database_path=self.agent_database, log_level="CRITICAL", _env_file=None)
        app = create_app(settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                yield app, client

    async def send(self, client, body=None):
        response = await client.post("/message:send", json=self.wire() if body is None else body, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    async def poll(self, client, task_id, state):
        async def wait():
            while True:
                response = await client.get(f"/tasks/{task_id}", headers=self.headers())
                self.assertEqual(response.status_code, 200, response.text)
                task = response.json()
                if task["status"]["state"] == state:
                    return task
                if task["status"]["state"] in {"TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED"}:
                    self.fail(f"Expected {state}; got {task['status']['state']} with stable data {task['status'].get('message')}")
                await asyncio.sleep(.002)
        return await asyncio.wait_for(wait(), timeout=5)

    def draft_response(self, *, kind="READY", summary=None, questions=None, **extra):
        return text_response(json.dumps({"kind": kind,
            "summary": ("회원가입 구현 내용을 작성했습니다." if kind == "READY" else "") if summary is None else summary,
            "questions": [] if questions is None else questions, **extra}, ensure_ascii=False))

    def write_response(self, *, content="def signup():\n    return 'generated-candidate'\n", path="source/signup.py", call_id="model-write-001", **extra):
        return tool_response(ToolCall(call_id=call_id, name="write_source_file",
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "path": path, "content": content, **extra})))

    def ready_provider(self, **changes):
        return FakeProvider(self.write_response(**changes), self.draft_response())

    async def run_to(self, provider, state, *, body=None, executor=None):
        async with self.client_for(self.executor(provider) if executor is None else executor) as (app, client):
            first = await self.send(client, body)
            task = await self.poll(client, first["id"], state)
            revisions = [MessageToDict(item) for item in await app.state.task_store.revisions(task["id"])]
            return task, revisions

    def parse_completed(self, wire, metadata=None):
        metadata = self.metadata if metadata is None else metadata
        task = ParseDict(wire, Task())
        step = WorkflowStep.model_validate({**self.step.model_dump(), "status": WorkflowStepStatus.SUCCEEDED,
            "attempt": metadata.attempt, "a2a_task_id": task.id, "agent_context_id": task.context_id,
            "a2a_task_state": A2ATaskState.COMPLETED})
        parsed = parse_developer_output(task, run=self.run, step=step)
        validated = validate_completed_role_output(AgentRole.DEVELOPER, task=task, run=self.run, step=step)
        self.assertEqual(parsed, validated)
        return parsed

    def tool_calls(self):
        return [(name, arguments) for session in self.sessions for name, arguments, _options in session.calls]

    def status_code(self, task):
        return task["status"]["message"]["parts"][0]["data"]["code"]

    def update_step_for_resume(self, metadata, task):
        current = self.repository.list_steps(self.run.run_id)
        current = next(step for step in current if step.workflow_step_id == self.step.workflow_step_id)
        self.step = WorkflowStep.model_validate({**current.model_dump(), "attempt": metadata.attempt,
            "a2a_task_id": task["id"], "agent_context_id": task["contextId"], "status": WorkflowStepStatus.RUNNING})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (self.step.status.value, self.step.model_dump_json(), str(self.step.workflow_step_id)))

    async def test_executor_constructor_is_inert_and_has_safe_repr(self):
        provider = self.ready_provider()
        executor = self.executor(provider)
        self.assertEqual(repr(executor), "DeveloperAgentExecutor()")
        self.assertEqual(provider.requests, [])
        self.assertEqual(provider.configurations, [])
        self.assertEqual(self.context_calls, [])
        self.assertEqual(self.service_calls, [])
        self.assertEqual(self.docker.calls, [])
        self.assertFalse(self.agent_database.exists())

    async def test_executor_constructor_rejects_missing_host_capabilities(self):
        for changes in ({"provider": None}, {"context_factory": None}, {"services_factory": None}, {"usage_sink": "model-selector"}):
            with self.subTest(changes=changes), self.assertRaises(DeveloperExecutorConfigurationError) as caught:
                self.executor(self.ready_provider(), **changes)
            self.assertEqual(str(caught.exception), "DEVELOPER_EXECUTOR_CONFIGURATION_INVALID")

    async def test_default_developer_server_remains_bootstrap(self):
        async with self.client_for() as (_, client):
            health = await client.get("/health")
            self.assertFalse(health.json()["executionReady"])
            card = await client.get("/.well-known/agent-card.json")
            self.assertEqual(card.json().get("skills", []), [])
            first = await self.send(client)
            task = await self.poll(client, first["id"], "TASK_STATE_REJECTED")
        self.assertEqual(self.status_code(task), "AGENT_RUNTIME_NOT_CONFIGURED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_explicit_developer_executor_advertises_only_implemented_role(self):
        provider = self.ready_provider()
        async with self.client_for(self.executor(provider)) as (_, client):
            health = await client.get("/health")
            self.assertTrue(health.json()["executionReady"])
            card = await client.get("/.well-known/agent-card.json")
            self.assertEqual(len(card.json()["skills"]), 1)
            self.assertEqual(card.json()["skills"][0]["id"], "measured-initial-implementation")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.context_calls, [])
        self.assertEqual(self.docker.calls, [])

    async def test_developer_executor_cannot_be_injected_into_another_role(self):
        for role in (AgentRole.PLANNER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role), self.assertRaisesRegex(ValueError, "^AGENT_EXECUTOR_ROLE_MISMATCH$"):
                create_app(AgentSettings(role=role, database_path=self.agent_database, _env_file=None),
                           executor=self.executor(self.ready_provider()))

    async def test_real_engine_mcp_git_snapshot_build_receipt_http_artifacts_match(self):
        provider = self.ready_provider()
        before = self.repository.get_run(self.run.run_id)
        task, revisions = await self.run_to(provider, "TASK_STATE_COMPLETED")
        output = self.parse_completed(task)
        self.assertEqual({artifact["name"] for artifact in task["artifacts"]},
            {"source-snapshot.json", "change-report.json", "build-report.json"})
        self.assertEqual(len(task["artifacts"]), 3)
        self.assertEqual(task["metadata"], self.metadata.to_a2a_json())
        self.assertIn("TASK_STATE_WORKING", [row["status"]["state"] for row in revisions])
        self.assertEqual([(item.path, item.action) for item in output.change_report.changes], [("source/signup.py", "MODIFIED")])
        self.assertEqual(output.source.code_version, 1)
        self.assertEqual(output.source.artifact_version, 1)
        self.assertIsNone(output.source.previous_artifact_id)
        self.assertNotEqual(output.source.commit_hash, self.baseline_commit)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.baseline_commit)
        source = self.artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).read(output.source.artifact_id)
        with tarfile.open(fileobj=io.BytesIO(source.content), mode="r:") as archive:
            self.assertEqual(archive.extractfile("signup.py").read(), b"def signup():\n    return 'generated-candidate'\n")
            self.assertEqual(archive.extractfile("requirements.lock").read(), self.lock_bytes)
        self.assertEqual(output.build_report.exit_code, 0)
        self.assertEqual(output.build_report.execution_outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(output.build_report.tool_evidence.outcome, ToolExecutionOutcome.PASS)
        receipt = self.outputs.get(self.run.run_id, output.build_report.execution_manifest_id)
        self.assertEqual(receipt.source_artifact_id, output.source.artifact_id)
        self.assertEqual(receipt.execution_manifest, output.source.execution_manifest())
        evidence = self.tool_store.get(self.binding, output.build_report.tool_evidence.execution_id)
        self.assertEqual(evidence.to_tool_evidence(), output.build_report.tool_evidence)
        self.assertEqual([name for name, _arguments in self.tool_calls()], ["write_source_file", "run_build"])
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual(self.repository.get_run(self.run.run_id), before)
        self.assertIsNone(before.verdict)

    async def test_product_nonzero_build_completes_with_failed_build_not_tool_retry(self):
        self.docker.exit_code = 2
        self.docker.start_result = CLIResult(returncode=2, stdout=b"", stderr=b"SyntaxError: inert fixture\n")
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        output = self.parse_completed(task)
        self.assertEqual(output.build_report.exit_code, 2)
        self.assertEqual(output.build_report.execution_outcome, ToolExecutionOutcome.FAIL)
        self.assertEqual(output.build_report.failure_kind, "PRODUCT")
        self.assertEqual(output.build_report.tool_evidence.outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(output.build_report.tool_evidence.retries_used, 0)
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    async def test_host_approved_build_timeout_narrowing_preserves_receipt_identity(self):
        self.profile = replace(self.profile, limits=replace(self.profile.limits, timeout_seconds=60))
        self.mcp_configuration = replace(self.mcp_configuration,
            build_configuration=BuildConfiguration(profile=self.profile))
        files = FileTools(self.artifacts)
        build = BuildTools(self.artifacts, self.sandbox, self.outputs,
            configuration=self.mcp_configuration.build_configuration, max_call_seconds=5)
        self.dispatcher = MCPDispatcher(self.binding, self.registry,
            handlers={**files.handlers(AgentRole.DEVELOPER), **build.handlers(AgentRole.DEVELOPER)},
            max_call_seconds=5)
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        output = self.parse_completed(task)
        receipt = self.outputs.get(self.run.run_id, output.build_report.execution_manifest_id)
        self.assertEqual(receipt.execution_profile.limits.timeout_seconds, 3)
        self.assertEqual(self.profile.limits.timeout_seconds, 60)
        self.assertEqual(receipt.execution_profile.argv, self.profile.argv)
        self.assertEqual(receipt.execution_profile.image_reference, self.profile.image_reference)

    async def test_draft_is_provider_strict_and_model_only_has_read_write_tools(self):
        provider = self.ready_provider()
        await self.run_to(provider, "TASK_STATE_COMPLETED")
        provider.configurations[0][1].schema.require_openai_strict()
        self.assertEqual(provider.configurations[0][1].name, "developer_decision")
        self.assertEqual({tool.name for tool in provider.requests[0].tools}, {"read_project_file", "write_source_file"})
        self.assertEqual(provider.requests[0].model, self.model)
        self.assertEqual(self.budget.model_calls, 2)
        self.assertEqual(self.budget.tool_calls, 2)
        self.assertEqual([usage.role for usage in self.usages], [AgentRole.DEVELOPER] * 2)

    async def test_actual_added_file_change_is_not_guessed_by_model(self):
        task, _ = await self.run_to(self.ready_provider(path="source/new_feature.py", content="def new_feature():\n    return True\n"), "TASK_STATE_COMPLETED")
        output = self.parse_completed(task)
        self.assertEqual([(item.path, item.action) for item in output.change_report.changes], [("source/new_feature.py", "ADDED")])

    async def test_ready_without_actual_changes_fails_without_completed_artifacts(self):
        task, revisions = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "DEVELOPER_CHECKPOINT_NO_CHANGES")
        self.assertFalse(task.get("artifacts"))
        self.assertFalse(any(row.get("artifacts") for row in revisions))
        self.assertEqual(self.docker.calls, [])

    async def test_same_content_write_cannot_create_fake_change_report(self):
        task, _ = await self.run_to(self.ready_provider(content=self.initial_source), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "DEVELOPER_CHECKPOINT_NO_CHANGES")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file"])
        self.assertEqual(self.docker.calls, [])

    async def test_provider_failure_is_safe_without_fake_artifacts(self):
        private = "DUMMY_UNLABELLED_DEVELOPER_PROVIDER_SECRET"
        task, revisions = await self.run_to(FakeProvider(ValueError(private)), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_PROVIDER_ERROR")
        self.assertFalse(task.get("artifacts"))
        self.assertNotIn(private, json.dumps(task))
        self.assertNotIn(private, json.dumps(revisions))
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_submit_fabricated_evidence_or_snapshot(self):
        provider = FakeProvider(self.draft_response(toolEvidence={"outcome": "PASS"}, snapshotId=str(uuid4())))
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual(self.docker.calls, [])

    async def test_inconsistent_draft_has_safe_contract_error_without_artifacts(self):
        provider = FakeProvider(self.draft_response(summary=""))
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "DEVELOPER_OUTPUT_INVALID")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])

    async def test_model_rejection_has_static_reason_without_artifacts(self):
        task, _ = await self.run_to(FakeProvider(self.draft_response(kind="REJECTED")), "TASK_STATE_REJECTED")
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "DEVELOPER_OUT_OF_SCOPE"})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_entire_tool_batch_is_checked_before_first_source_write(self):
        first = ToolCall(call_id="batch-valid-write", name="write_source_file", arguments_json=json.dumps({
            "workspaceId": str(self.run.workspace_id), "path": "source/signup.py", "content": "must not be written\n"}))
        second = ToolCall(call_id="batch-forbidden-security", name="run_security_scan", arguments_json=json.dumps({
            "workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4()), "scannerProfile": "untrusted"}))
        task, _ = await self.run_to(FakeProvider(tool_response(first, second)), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_TOOL_POLICY_VIOLATION")
        self.assertEqual((self.source / "signup.py").read_text(encoding="utf-8"), self.initial_source)
        self.assertEqual(self.tool_calls(), [])
        self.assertFalse(task.get("artifacts"))

    async def test_credential_literal_source_is_refused_before_mcp_write(self):
        task, revisions = await self.run_to(self.ready_provider(content="password='private-test-password'\n"), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_TOOL_POLICY_VIOLATION")
        self.assertEqual((self.source / "signup.py").read_text(encoding="utf-8"), self.initial_source)
        self.assertEqual(self.tool_calls(), [])
        self.assertNotIn("private-test-password", json.dumps(task))
        self.assertNotIn("private-test-password", json.dumps(revisions))

    async def test_ordinary_password_variable_code_is_preserved_byte_for_byte(self):
        source_text = "def signup(request):\n    password = request.password\n    return password\n"
        task, _ = await self.run_to(self.ready_provider(content=source_text), "TASK_STATE_COMPLETED")
        output = self.parse_completed(task)
        stored = self.artifacts.bind(self.run.run_id, role=AgentRole.DEVELOPER).read(output.source.artifact_id)
        with tarfile.open(fileobj=io.BytesIO(stored.content), mode="r:") as archive:
            self.assertEqual(archive.extractfile("signup.py").read(), source_text.encode("utf-8"))
        self.assertNotIn(source_text, json.dumps(task))

    async def test_shared_model_limit_does_not_allow_retry_after_side_effect(self):
        self.budget = ExecutionBudget(runtime_budget_ms=15000,
            limits=LLMLimits(max_model_calls=1, max_tool_calls=10, model_timeout_seconds=2))
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file"])
        self.assertIn("generated-candidate", (self.source / "signup.py").read_text(encoding="utf-8"))
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_call_build_with_invented_snapshot(self):
        call = ToolCall(call_id="fake-build", name="run_build", arguments_json=json.dumps({
            "workspaceId": str(self.run.workspace_id), "snapshotId": str(uuid4())}))
        task, _ = await self.run_to(FakeProvider(tool_response(call)), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "LLM_TOOL_POLICY_VIOLATION")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_modify_protected_qa_files(self):
        task, _ = await self.run_to(self.ready_provider(path="outputs/qa/tests/hidden.py"), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertFalse((self.root / "outputs/qa/tests/hidden.py").exists())
        self.assertEqual(self.docker.calls, [])

    async def test_host_baseline_dirty_before_model_is_rejected_before_execution(self):
        (self.source / "signup.py").write_text("unrelated operator edit\n", encoding="utf-8")
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
        self.assertEqual(provider.requests, [])
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])

    async def test_input_required_has_no_completed_artifacts_or_host_build(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["어떤 화면을 만들까요?"]))
        task, revisions = await self.run_to(provider, "TASK_STATE_INPUT_REQUIRED")
        self.assertEqual(self.status_code(task), "DEVELOPER_INPUT_REQUIRED")
        self.assertFalse(task.get("artifacts"))
        self.assertFalse(any(row.get("artifacts") for row in revisions))
        self.assertEqual(self.docker.calls, [])

    async def test_auth_required_never_asks_for_credentials_in_message(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH))
        task, _ = await self.run_to(provider, "TASK_STATE_AUTH_REQUIRED")
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "LLM_AUTH_REQUIRED"})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])

    async def test_clean_input_resume_keeps_task_codeversion_and_shared_budget(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["화면 종류를 알려주세요."]),
            self.write_response(call_id="resume-write"), self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "기본 화면"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            completed = await self.poll(client, second["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(completed["id"], first["id"])
        self.assertEqual(completed["contextId"], first["contextId"])
        output = self.parse_completed(completed, metadata)
        self.assertEqual(output.source.code_version, 1)
        self.assertEqual(self.repository.get_run(self.run.run_id).fix_attempt, 0)
        self.assertEqual(self.budget.model_calls, 3)
        self.assertEqual(self.budget.tool_calls, 2)
        envelope = json.loads(json.loads(provider.requests[1].input_items_json)[0]["content"])
        self.assertEqual(envelope["taskInput"]["clarifications"], [{"answer": "기본 화면"}])
        self.assertEqual(envelope["taskInput"]["plan"], self.plan.model_dump(mode="json", by_alias=True))

    async def test_input_required_is_not_published_before_peer_cleanup_and_resume_is_safe(self):
        cleanup_entered, release_cleanup, cleanup_finished = (asyncio.Event() for _ in range(3))

        @asynccontextmanager
        async def slow_first_peer(configuration):
            async with self.peer_client(configuration) as client:
                try:
                    yield client
                finally:
                    if len(self.sessions) == 1:
                        cleanup_entered.set()
                        await release_cleanup.wait()
                        cleanup_finished.set()

        def services(execution):
            configured = self.services_factory(execution)
            configured.client_factory = slow_first_peer
            return configured

        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["화면 종류를 알려주세요."]),
            self.write_response(call_id="cleanup-resume-write"), self.draft_response())
        async with self.client_for(self.executor(provider, services_factory=services)) as (_, client):
            first = await self.send(client)
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 2)
                response = await client.get(f"/tasks/{first['id']}", headers=self.headers())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"]["state"], "TASK_STATE_WORKING")
                self.assertFalse(cleanup_finished.is_set())
                self.assertFalse(response.json().get("artifacts"))
                self.assertEqual(len(provider.requests), 1)
            finally:
                # An assertion must never leave SDK lifespan waiting on the
                # artificial cleanup barrier during fixture teardown.
                release_cleanup.set()
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.assertTrue(cleanup_finished.is_set())
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "기본 화면"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            completed = await self.poll(client, second["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(completed["id"], first["id"])
        self.assertEqual(completed["contextId"], first["contextId"])
        self.assertEqual(self.parse_completed(completed, metadata).source.code_version, 1)
        self.assertEqual(len(provider.requests), 3)
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(self.budget.model_calls, 3)
        self.assertEqual(self.budget.tool_calls, 2)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_rejected_decision_is_published_only_after_peer_cleanup(self):
        cleanup_entered, release_cleanup, cleanup_finished = (asyncio.Event() for _ in range(3))

        @asynccontextmanager
        async def slow_peer(configuration):
            async with self.peer_client(configuration) as client:
                try:
                    yield client
                finally:
                    cleanup_entered.set()
                    await release_cleanup.wait()
                    cleanup_finished.set()

        def services(execution):
            configured = self.services_factory(execution)
            configured.client_factory = slow_peer
            return configured

        provider = FakeProvider(self.draft_response(kind="REJECTED"))
        async with self.client_for(self.executor(provider, services_factory=services)) as (_, client):
            first = await self.send(client)
            try:
                await asyncio.wait_for(cleanup_entered.wait(), 2)
                response = await client.get(f"/tasks/{first['id']}", headers=self.headers())
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["status"]["state"], "TASK_STATE_WORKING")
                self.assertFalse(cleanup_finished.is_set())
                self.assertFalse(response.json().get("artifacts"))
            finally:
                release_cleanup.set()
            rejected = await self.poll(client, first["id"], "TASK_STATE_REJECTED")
        self.assertTrue(cleanup_finished.is_set())
        self.assertEqual(self.status_code(rejected), "DEVELOPER_OUT_OF_SCOPE")
        self.assertFalse(rejected.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_clean_auth_resume_does_not_generate_new_task_or_reset_budget(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH), self.write_response(call_id="auth-resume-write"), self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_AUTH_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "운영자가 별도 인증 설정을 확인했습니다."}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            completed = await self.poll(client, second["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(completed["id"], first["id"])
        self.assertEqual(completed["contextId"], first["contextId"])
        self.assertEqual(self.parse_completed(completed, metadata).source.code_version, 1)
        self.assertEqual(self.budget.model_calls, 3)

    async def test_resume_after_write_is_not_silently_restored_or_repeated(self):
        provider = FakeProvider(self.write_response(), self.draft_response(kind="INPUT_REQUIRED", questions=["추가 설명이 필요한가요?"]),
            self.write_response(call_id="must-not-run"), self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "진행"}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            failed = await self.poll(client, second["id"], "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(failed), "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file"])
        self.assertIn("generated-candidate", (self.source / "signup.py").read_text(encoding="utf-8"))
        self.assertFalse(failed.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_clarification_cannot_replace_protected_plan(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["추가 설명?"]))
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            metadata = self.metadata.model_copy(update={"attempt": 1})
            self.update_step_for_resume(metadata, waiting)
            second = await self.send(client, self.wire(payload={"answer": "진행", "plan": {}}, metadata=metadata,
                task_id=waiting["id"], context_id=waiting["contextId"]))
            rejected = await self.poll(client, second["id"], "TASK_STATE_REJECTED")
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(rejected.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_initial_boolean_artifact_version_is_not_accepted_as_one(self):
        body = self.wire()
        body["message"]["parts"][0]["data"]["sourceArtifact"]["artifactVersion"] = True
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=body)
        self.assertEqual(provider.requests, [])
        self.assertFalse(task.get("artifacts"))

    async def test_initial_workspace_override_is_rejected_before_model_or_tools(self):
        data = self.payload()
        data["workspaceId"] = str(uuid4())
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=data))
        self.assertEqual(provider.requests, [])
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])

    async def test_host_factory_error_is_safe_rejection(self):
        def fail(_context):
            raise ValueError("DUMMY_PRIVATE_HOST_CONTEXT")
        provider = self.ready_provider()
        task, revisions = await self.run_to(provider, "TASK_STATE_REJECTED",
            executor=self.executor(provider, context_factory=fail))
        self.assertNotIn("DUMMY_PRIVATE_HOST_CONTEXT", json.dumps(task))
        self.assertNotIn("DUMMY_PRIVATE_HOST_CONTEXT", json.dumps(revisions))
        self.assertEqual(provider.requests, [])

    async def test_host_factory_budget_error_is_failed_not_rejected(self):
        def fail(_context):
            raise LLMRuntimeError(LLMErrorCode.BUDGET)
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED", executor=self.executor(provider, context_factory=fail))
        self.assertEqual(self.status_code(task), "LLM_BUDGET_EXHAUSTED")
        self.assertEqual(provider.requests, [])

    async def test_services_factory_failure_is_safe_before_model_or_tools(self):
        def fail(_execution):
            raise ValueError("DUMMY_PRIVATE_HOST_SERVICE")
        provider = self.ready_provider()
        task, revisions = await self.run_to(provider, "TASK_STATE_REJECTED",
            executor=self.executor(provider, services_factory=fail))
        self.assertEqual(self.status_code(task), "DEVELOPER_SERVICES_DENIED")
        self.assertNotIn("DUMMY_PRIVATE_HOST_SERVICE", json.dumps(task))
        self.assertNotIn("DUMMY_PRIVATE_HOST_SERVICE", json.dumps(revisions))
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.tool_calls(), [])

    async def test_infrastructure_build_failure_does_not_invent_complete_receipt(self):
        self.docker.start_error = SandboxError(SandboxErrorCode.EXECUTION)
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file", "run_build"])

    async def test_terminal_replay_does_not_write_freeze_or_build_twice(self):
        provider = self.ready_provider()
        body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            completed = await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, completed)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(self.service_calls), 1)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_completed_task_restart_replay_reads_durable_artifacts_without_model(self):
        provider = self.ready_provider()
        body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            completed = await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
        fresh_provider = FakeProvider()
        async with self.client_for(self.executor(fresh_provider)) as (_, client):
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, completed)
        self.assertEqual(fresh_provider.requests, [])
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.parse_completed(repeated)

    async def test_cancel_while_model_waits_stops_worker_without_completed_artifacts(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def wait(_request):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        provider = FakeProvider(wait)
        body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            await asyncio.wait_for(started.wait(), 2)
            canceled = await client.post(f"/tasks/{first['id']}:cancel", json={}, headers=self.headers())
            self.assertEqual(canceled.status_code, 200, canceled.text)
            task = await self.poll(client, first["id"], "TASK_STATE_CANCELED")
            self.assertEqual(await self.send(client, deepcopy(body)), task)
        self.assertTrue(stopped.is_set())
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.docker.calls, [])

    async def test_cancel_during_host_build_cleans_owned_fake_container_without_artifact(self):
        self.docker.block_start = True
        provider = self.ready_provider()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            await asyncio.wait_for(self.docker.start_entered.wait(), 3)
            canceled = await client.post(f"/tasks/{first['id']}:cancel", json={}, headers=self.headers())
            self.assertEqual(canceled.status_code, 200, canceled.text)
            task = await self.poll(client, first["id"], "TASK_STATE_CANCELED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual([name for name, _args in self.tool_calls()], ["write_source_file", "run_build"])


if __name__ == "__main__":
    unittest.main()
