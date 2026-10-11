"""Real Planner LLMEngine/A2A/SQLite boundaries with an injected FakeProvider.

No cloud model, MCP child, Docker, product code or product verdict is executed.
"""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx
from jsonschema import Draft202012Validator, FormatChecker

from agents.api.validation import parse_workflow_metadata
from agents.core.config import AgentSettings
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.main import create_app
from agents.roles.outputs import validate_completed_role_output
from agents.runtime.planner import PlannerAgentExecutor, PlannerExecutorConfigurationError
from agents.runtime.planner_context import PlannerExecutionContext, SQLitePlannerContextLoader
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.planner_output import parse_planner_output
from orchestrator.domain import A2ATaskState, AgentRole, SCN_001_ID, SCENARIO_REGISTRY, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact
from orchestrator.infrastructure import SQLiteWorkflowRepository
from test_llm_runtime import FakeProvider, text_response, tool_response


class PlannerAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clean_environment = patch.dict(os.environ, {}, clear=True)
        clean_environment.start()
        self.addCleanup(clean_environment.stop)
        temporary = TemporaryDirectory(prefix="a2a-planner-agent-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "planner.sqlite3"
        self.scenario = SCENARIO_REGISTRY[SCN_001_ID]
        self.run_id, self.workspace_id, self.step_id, self.artifact_id = (uuid4() for _ in range(4))
        self.request_text = "회원가입 기능을 구현할 작업으로 나눠줘."
        self.model = ModelConfiguration(provider="fake", model_id="fake-model", temperature=0, seed=17)
        self.configuration = RunConfigurationArtifact(run_id=self.run_id, workspace_id=self.workspace_id,
            scenario_id=SCN_001_ID, configuration=RunConfiguration(model=self.model,
                limits=ExecutionLimits(runtime_budget_ms=10000)))
        self.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_model_calls=4, max_tool_calls=0,
            model_timeout_seconds=1, max_output_tokens=4096))
        self.metadata = A2AWorkflowMetadata(run_id=self.run_id, workflow_step_id=self.step_id,
            scenario_id=SCN_001_ID, attempt=0, requirement_ids=self.scenario.requirement_ids)
        self.context_calls, self.usages = [], []

    def context_factory(self, context):
        metadata = parse_workflow_metadata(context.metadata)
        self.context_calls.append(metadata)
        return self.host_context(metadata)

    def host_context(self, metadata=None, **changes):
        values = dict(metadata=metadata or self.metadata, configuration=self.configuration, budget=self.budget,
            request_text=self.request_text, project_artifact_id=self.artifact_id, artifact_version=1)
        values.update(changes)
        return PlannerExecutionContext(**values)

    def executor(self, selected_provider, **changes):
        values = dict(provider=selected_provider, context_factory=self.context_factory, usage_sink=self.usages.append)
        values.update(changes)
        return PlannerAgentExecutor(**values)

    @staticmethod
    def headers():
        return {"A2A-Version": "1.0", "Content-Type": "application/a2a+json"}

    @asynccontextmanager
    async def client_for(self, executor=None):
        settings = AgentSettings(role="PLANNER", database_path=self.database, log_level="CRITICAL", _env_file=None)
        app = create_app(settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                yield app, client

    def payload(self):
        return {"request": self.request_text, "workspaceId": str(self.workspace_id),
            "runConfiguration": self.configuration.to_artifact_json(), "scenarioContract": self.configuration.scenario_contract}

    def wire(self, *, payload=None, metadata=None, task_id=None, context_id=None):
        return MessageToDict(build_send_message_request(self.payload() if payload is None else payload,
            metadata or self.metadata, task_id=task_id, context_id=context_id))

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
                await asyncio.sleep(0.002)
        return await asyncio.wait_for(wait(), timeout=3)

    def draft(self, *, kind="PLAN", tasks=None, questions=None):
        if tasks is None:
            tasks = [{"taskId": "TASK-001", "title": "회원가입 API 및 입력 규칙 구현",
                "description": "보호된 모든 요구사항과 수용 기준을 변경하지 않고 구현한다.",
                "requirementIds": [str(value) for value in self.scenario.requirement_ids], "dependsOn": []}] if kind == "PLAN" else []
        return {"kind": kind, "implementationPlan": tasks, "questions": [] if questions is None else questions}

    def response(self, draft=None):
        return text_response(json.dumps(self.draft() if draft is None else draft, ensure_ascii=False))

    async def run_to(self, provider, state, *, body=None, executor=None):
        async with self.client_for(self.executor(provider) if executor is None else executor) as (app, client):
            submitted = await self.send(client, body)
            completed = await self.poll(client, submitted["id"], state)
            return completed, [MessageToDict(item) for item in await app.state.task_store.revisions(completed["id"])]

    def parse_completed(self, wire, metadata=None):
        metadata = metadata or self.metadata
        task = ParseDict(wire, Task())
        run = WorkflowRun(run_id=self.run_id, workspace_id=self.workspace_id, scenario_id=SCN_001_ID,
            request_text=self.request_text, status=WorkflowStatus.PLANNING)
        step = WorkflowStep(workflow_step_id=self.step_id, run_id=self.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.SUCCEEDED, attempt=metadata.attempt,
            a2a_task_state=A2ATaskState.COMPLETED, a2a_task_id=task.id,
            agent_context_id=task.context_id, requirement_ids=list(metadata.requirement_ids or ()))
        parsed = parse_planner_output(task, run_id=self.run_id, workflow_step_id=self.step_id)
        validated = validate_completed_role_output(AgentRole.PLANNER, task=task, run=run, step=step,
            scenario=self.host_context(metadata).scenario)
        self.assertEqual(parsed, validated)
        return parsed

    async def assert_draft_rejected(self, draft):
        provider = FakeProvider(self.response(draft))
        task, revisions = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(any(row.get("artifacts") for row in revisions))
        return task

    async def test_constructor_is_inert_and_repr_safe(self):
        provider = FakeProvider(self.response())
        executor = self.executor(provider)
        self.assertEqual(repr(executor), "PlannerAgentExecutor()")
        self.assertEqual(provider.requests, [])
        self.assertEqual(provider.configurations, [])
        self.assertEqual(self.context_calls, [])
        self.assertFalse(self.database.exists())

    async def test_constructor_refuses_missing_host_capabilities(self):
        for changes in ({"provider": None}, {"context_factory": None}, {"usage_sink": "untrusted-selector"}):
            with self.subTest(changes=changes), self.assertRaises(PlannerExecutorConfigurationError) as error:
                self.executor(FakeProvider(self.response()), **changes)
            self.assertEqual(str(error.exception), "PLANNER_EXECUTOR_CONFIGURATION_INVALID")

    async def test_default_server_remains_bootstrap_even_with_model_settings(self):
        settings = AgentSettings(role="PLANNER", database_path=self.database, log_level="CRITICAL", _env_file=None,
            llm_provider="fake", llm_model_id="fake-model", llm_temperature=0)
        app = create_app(settings)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                health = await client.get("/health")
                self.assertFalse(health.json()["executionReady"])
                card = await client.get("/.well-known/agent-card.json")
                self.assertEqual(card.json().get("skills", []), [])
                first = await self.send(client)
                rejected = await self.poll(client, first["id"], "TASK_STATE_REJECTED")
                self.assertEqual(rejected["status"]["message"]["parts"][0]["data"]["code"], "AGENT_RUNTIME_NOT_CONFIGURED")
                self.assertFalse(rejected.get("artifacts"))

    async def test_explicit_planner_executor_advertises_only_implemented_planning(self):
        provider = FakeProvider(self.response())
        async with self.client_for(self.executor(provider)) as (_, client):
            health = await client.get("/health")
            self.assertTrue(health.json()["executionReady"])
            card = await client.get("/.well-known/agent-card.json")
            self.assertEqual([skill["id"] for skill in card.json()["skills"]], ["protected-task-planning"])
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.context_calls, [])

    async def test_planner_executor_cannot_be_injected_into_other_role_server(self):
        provider = FakeProvider(self.response())
        executor = self.executor(provider)
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                settings = AgentSettings(role=role, database_path=self.database, log_level="CRITICAL", _env_file=None)
                with self.assertRaisesRegex(ValueError, "^AGENT_EXECUTOR_ROLE_MISMATCH$"):
                    create_app(settings, executor=executor)
        self.assertFalse(self.database.exists())
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.context_calls, [])

    async def test_host_sqlite_loader_through_real_agent_returns_parseable_artifact_without_owning_run_verdict(self):
        repository = SQLiteWorkflowRepository(self.directory / "orchestrator.sqlite3")
        run = WorkflowRun(run_id=self.run_id, workspace_id=self.workspace_id, scenario_id=SCN_001_ID,
            request_text=self.request_text)
        step = WorkflowStep(workflow_step_id=self.step_id, run_id=self.run_id, agent_role=AgentRole.PLANNER)
        repository.create_run(run, (step,), (), run_configuration=self.configuration)
        claimed_run, claimed_step = repository.claim_planner_dispatch(run.run_id)
        metadata = A2AWorkflowMetadata(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
            scenario_id=SCN_001_ID, attempt=0, requirement_ids=None)
        budgets_resolved = []
        def resolve_budget(configuration):
            budgets_resolved.append(configuration)
            return self.budget
        loader = SQLitePlannerContextLoader(repository, resolve_budget)
        provider = FakeProvider(self.response())
        executor = self.executor(provider, context_factory=loader)
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED", executor=executor,
            body=self.wire(metadata=metadata))
        parsed = self.parse_completed(task, metadata)
        self.assertEqual({item.requirement_id for item in parsed.plan.requirements}, set(self.scenario.requirement_ids))
        self.assertEqual(budgets_resolved, [self.configuration])
        self.assertEqual(self.budget.model_calls, 1)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(repository.get_run(run.run_id), claimed_run)
        self.assertEqual(repository.list_steps(run.run_id), [claimed_step])
        self.assertEqual(repository.get_run(run.run_id).status, WorkflowStatus.PLANNING)
        self.assertIsNone(repository.get_planning_artifact(run.run_id))

    async def test_real_engine_http_task_artifact_passes_orchestrator_parsers(self):
        provider = FakeProvider(self.response())
        task, revisions = await self.run_to(provider, "TASK_STATE_COMPLETED")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(len(task["artifacts"]), 1)
        self.assertEqual(task["artifacts"][0]["name"], "requirements.json")
        self.assertEqual(task["metadata"], self.metadata.to_a2a_json())
        self.assertIn("TASK_STATE_WORKING", [item["status"]["state"] for item in revisions])
        parsed = self.parse_completed(task)
        self.assertEqual(parsed.project_artifact_id, self.artifact_id)
        self.assertEqual(parsed.artifact_version, 1)
        self.assertEqual({item.requirement_id for item in parsed.plan.requirements}, set(self.scenario.requirement_ids))
        self.assertEqual(provider.requests[0].tools, ())
        self.assertEqual(provider.requests[0].model, self.model)
        self.assertEqual(self.budget.tool_calls, 0)
        self.assertEqual(len(self.usages), 1)
        self.assertEqual(self.usages[0].role, AgentRole.PLANNER)

    async def test_host_requirements_are_assembled_not_model_generated(self):
        provider = FakeProvider(self.response())
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        payload = task["artifacts"][0]["parts"][0]["data"]
        self.assertEqual(payload["requirements"], [{key: row[key] for key in
            ("requirementId", "key", "description", "acceptanceCriteria")} for row in self.configuration.scenario_contract["requirements"]])
        self.assertNotIn("requirements", self.draft())
        self.assertNotIn("projectArtifactId", self.draft())

    async def test_draft_schema_is_openai_strict_but_final_artifact_is_project_schema(self):
        provider = FakeProvider(self.response())
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        provider.configurations[0][1].schema.require_openai_strict()
        schema_path = Path(__file__).resolve().parents[1] / "schemas" / "project" / "planner_output.schema.json"
        schema = json.loads(schema_path.read_text())
        Draft202012Validator(schema, format_checker=FormatChecker()).validate(task["artifacts"][0]["parts"][0]["data"])

    async def test_prompt_keeps_instructions_and_host_input_separate(self):
        provider = FakeProvider(self.response())
        await self.run_to(provider, "TASK_STATE_COMPLETED")
        request = provider.requests[0]
        envelope = json.loads(json.loads(request.input_items_json)[0]["content"])
        self.assertEqual(envelope["metadata"], self.metadata.to_a2a_json())
        self.assertEqual(envelope["taskInput"]["request"], self.request_text)
        self.assertEqual(envelope["taskInput"]["scenarioContract"], self.configuration.scenario_contract)
        self.assertEqual(envelope["taskInput"]["clarifications"], [])
        self.assertNotIn(self.request_text, request.system_prompt)

    async def test_input_required_returns_no_complete_artifact(self):
        provider = FakeProvider(self.response(self.draft(kind="INPUT_REQUIRED", questions=["필요한 회원가입 화면 종류를 설명해 주세요."])))
        task, revisions = await self.run_to(provider, "TASK_STATE_INPUT_REQUIRED")
        self.assertFalse(task.get("artifacts"))
        message = task["status"]["message"]["parts"][0]["data"]
        self.assertEqual(message["code"], "PLANNER_INPUT_REQUIRED")
        self.assertEqual(len(message["questions"]), 1)
        self.assertFalse(any(row.get("artifacts") for row in revisions))

    async def test_rejected_has_static_reason_and_no_artifact(self):
        task, _ = await self.run_to(FakeProvider(self.response(self.draft(kind="REJECTED"))), "TASK_STATE_REJECTED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "PLANNER_OUT_OF_SCOPE"})

    async def test_clarification_resume_recovers_original_host_baseline_and_shared_budget(self):
        provider = FakeProvider(self.response(self.draft(kind="INPUT_REQUIRED", questions=["어떤 화면을 원하나요?"])), self.response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            continued_metadata = self.metadata.model_copy(update={"attempt": 1})
            second = await self.send(client, self.wire(payload={"answer": "기본 회원가입 화면으로 만들어 주세요."},
                metadata=continued_metadata, task_id=waiting["id"], context_id=waiting["contextId"]))
            completed = await self.poll(client, second["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(first["id"], completed["id"])
        self.assertEqual(first["contextId"], completed["contextId"])
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(self.budget.model_calls, 2)
        envelope = json.loads(json.loads(provider.requests[1].input_items_json)[0]["content"])
        self.assertEqual(envelope["taskInput"]["scenarioContract"], self.configuration.scenario_contract)
        self.assertEqual(envelope["taskInput"]["clarifications"], [{"answer": "기본 회원가입 화면으로 만들어 주세요."}])
        self.assertEqual(self.parse_completed(completed, continued_metadata).project_artifact_id, self.artifact_id)

    async def test_clarification_cannot_overwrite_protected_baseline(self):
        provider = FakeProvider(self.response(self.draft(kind="INPUT_REQUIRED", questions=["어떤 화면을 원하나요?"])))
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            second = await self.send(client, self.wire(payload={"answer": "그대로", "scenarioContract": {}},
                metadata=self.metadata.model_copy(update={"attempt": 1}), task_id=waiting["id"], context_id=waiting["contextId"]))
            rejected = await self.poll(client, second["id"], "TASK_STATE_REJECTED")
        self.assertFalse(rejected.get("artifacts"))
        self.assertEqual(len(provider.requests), 1)

    async def test_async_host_context_factory_is_supported(self):
        provider = FakeProvider(self.response())
        async def factory(context):
            await asyncio.sleep(0)
            return self.context_factory(context)
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED", executor=self.executor(provider, context_factory=factory))
        self.parse_completed(task)

    async def test_sync_factory_returning_awaitable_is_supported(self):
        provider = FakeProvider(self.response())
        def factory(context):
            async def load():
                return self.context_factory(context)
            return load()
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED", executor=self.executor(provider, context_factory=factory))
        self.parse_completed(task)

    async def test_lost_response_message_replay_does_not_reinvoke_provider(self):
        provider = FakeProvider(self.response())
        body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            completed = await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, completed)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(len(self.context_calls), 1)

    async def test_completed_task_restart_replay_preserves_artifact_without_llm_call(self):
        body = self.wire(); provider = FakeProvider(self.response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            completed = await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
        fresh_provider = FakeProvider()
        async with self.client_for(self.executor(fresh_provider)) as (_, client):
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, completed)
        self.assertEqual(fresh_provider.requests, [])
        self.parse_completed(repeated)

    async def test_concurrent_duplicate_messages_have_one_llm_invocation(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def wait(request):
            started.set()
            await release.wait()
            return self.response()
        provider = FakeProvider(wait); body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            messages = await asyncio.gather(*(self.send(client, deepcopy(body)) for _ in range(4)))
            await asyncio.wait_for(started.wait(), 1)
            self.assertEqual(len({task["id"] for task in messages}), 1)
            self.assertEqual(len(provider.requests), 1)
            release.set()
            await self.poll(client, messages[0]["id"], "TASK_STATE_COMPLETED")

    async def test_model_auth_error_is_out_of_band_and_does_not_request_credentials(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.AUTH))
        task, _ = await self.run_to(provider, "TASK_STATE_AUTH_REQUIRED")
        message = task["status"]["message"]["parts"][0]["data"]
        self.assertEqual(message, {"code": "LLM_AUTH_REQUIRED"})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(provider.requests), 1)

    async def test_provider_timeout_fails_without_retry_or_artifact(self):
        provider = FakeProvider(asyncio.TimeoutError("DUMMY_UNLABELLED_PROVIDER_SECRET"))
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "LLM_EXECUTION_TIMEOUT"})
        self.assertFalse(task.get("artifacts")); self.assertEqual(len(provider.requests), 1)

    async def test_real_async_timeout_obeys_shared_budget(self):
        stopped = asyncio.Event()
        async def wait(request):
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        self.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_tool_calls=0, model_timeout_seconds=.01))
        provider = FakeProvider(wait)
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertTrue(stopped.is_set())
        self.assertFalse(task.get("artifacts")); self.assertEqual(len(provider.requests), 1)

    async def test_provider_exception_body_is_not_in_task_revision_or_database(self):
        secret = "DUMMY_UNLABELLED_PROVIDER_SECRET"
        task, revisions = await self.run_to(FakeProvider(ValueError(secret)), "TASK_STATE_FAILED")
        self.assertNotIn(secret, json.dumps(task)); self.assertNotIn(secret, json.dumps(revisions))
        for file in self.directory.iterdir():
            if file.is_file():
                self.assertNotIn(secret.encode(), file.read_bytes())

    async def test_provider_refusal_rejects_without_completed_artifact(self):
        task, _ = await self.run_to(FakeProvider(text_response(refused=True)), "TASK_STATE_REJECTED")
        self.assertFalse(task.get("artifacts"))

    async def test_provider_incomplete_response_is_not_a_plan(self):
        task, _ = await self.run_to(FakeProvider(text_response(status="incomplete")), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))

    async def test_unknown_model_usage_with_token_cap_cannot_complete(self):
        self.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_tool_calls=0, max_total_tokens=1000))
        provider = FakeProvider(text_response(json.dumps(self.draft()), usage=None))
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertIsNone(self.budget.total_tokens)
        self.assertFalse(task.get("artifacts"))

    async def test_shared_model_call_limit_is_not_reset_at_clarification_resume(self):
        self.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_model_calls=1, max_tool_calls=0))
        provider = FakeProvider(self.response(self.draft(kind="INPUT_REQUIRED", questions=["어떤 화면인가요?"])), self.response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            waiting = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            resumed = await self.send(client, self.wire(payload={"answer": "기본"},
                metadata=self.metadata.model_copy(update={"attempt": 1}), task_id=waiting["id"], context_id=waiting["contextId"]))
            failed = await self.poll(client, resumed["id"], "TASK_STATE_FAILED")
        self.assertEqual(len(provider.requests), 1)
        self.assertFalse(failed.get("artifacts"))

    async def test_host_metadata_binding_mismatch_is_rejected_before_llm(self):
        provider = FakeProvider(self.response())
        wrong = self.metadata.model_copy(update={"workflow_step_id": uuid4()})
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", executor=self.executor(provider,
            context_factory=lambda context: self.host_context(wrong)))
        self.assertFalse(task.get("artifacts")); self.assertEqual(provider.requests, [])

    async def test_host_context_failure_is_safe_rejection_before_llm(self):
        secret = "DUMMY_UNLABELLED_CONTEXT_SECRET"
        def denied(context):
            raise ValueError(secret)
        provider = FakeProvider(self.response())
        task, revisions = await self.run_to(provider, "TASK_STATE_REJECTED", executor=self.executor(provider, context_factory=denied))
        self.assertNotIn(secret, json.dumps(revisions)); self.assertNotIn(secret, json.dumps(task))
        self.assertEqual(provider.requests, [])

    async def test_host_factory_budget_error_is_failed_not_misconfiguration_rejection(self):
        def exhausted(context):
            raise LLMRuntimeError(LLMErrorCode.BUDGET)
        provider = FakeProvider(self.response())
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED", executor=self.executor(provider, context_factory=exhausted))
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": LLMErrorCode.BUDGET.value})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(provider.requests, [])

    async def test_input_workspace_override_is_rejected_before_llm(self):
        payload = self.payload(); payload["workspaceId"] = str(uuid4())
        provider = FakeProvider(self.response())
        await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(provider.requests, [])

    async def test_input_protected_scenario_override_is_rejected_before_llm(self):
        payload = self.payload(); payload["scenarioContract"]["requirements"][0]["acceptanceCriteria"] = ["Skip validation"]
        provider = FakeProvider(self.response())
        await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(provider.requests, [])

    async def test_input_frozen_model_override_is_rejected_before_llm(self):
        payload = self.payload(); payload["runConfiguration"]["configuration"]["model"]["modelId"] = "other-model"
        provider = FakeProvider(self.response())
        await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(provider.requests, [])

    async def test_input_boolean_is_not_artifact_version_one(self):
        payload = self.payload()
        self.assertEqual(payload["runConfiguration"]["artifactVersion"], 1)
        payload["runConfiguration"]["artifactVersion"] = True
        provider = FakeProvider(self.response())
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "PLANNER_INPUT_INVALID"})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(provider.requests, [])

    async def test_input_boolean_is_not_protected_password_parallelism_one(self):
        payload = self.payload()
        policy = payload["scenarioContract"]["securityPolicy"]["passwordHashPolicy"]
        self.assertEqual(policy["parallelism"], 1)
        policy["parallelism"] = True
        provider = FakeProvider(self.response())
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(task["status"]["message"]["parts"][0]["data"], {"code": "PLANNER_INPUT_INVALID"})
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(provider.requests, [])

    async def test_multiple_json_parts_are_not_merged_into_authority(self):
        body = self.wire(); body["message"]["parts"].append({"data": {"role": "DEVELOPER"}, "mediaType": "application/json"})
        provider = FakeProvider(self.response())
        await self.run_to(provider, "TASK_STATE_REJECTED", body=body)
        self.assertEqual(provider.requests, [])

    async def test_extra_model_generated_requirements_are_not_accepted(self):
        draft = self.draft(); draft["requirements"] = []
        await self.assert_draft_rejected(draft)

    async def test_model_cannot_supply_project_ids_versions_or_execution_proof(self):
        for field, value in (("projectArtifactId", str(uuid4())), ("artifactVersion", 99), ("toolEvidence", {}), ("verdict", "SUCCESS")):
            with self.subTest(field=field):
                draft = self.draft(); draft[field] = value
                await self.assert_draft_rejected(draft)

    async def test_unknown_requirement_uuid_in_model_plan_rejected(self):
        draft = self.draft(); draft["implementationPlan"][0]["requirementIds"].append(str(uuid4()))
        await self.assert_draft_rejected(draft)

    async def test_incomplete_requirement_coverage_rejected(self):
        draft = self.draft(); draft["implementationPlan"][0]["requirementIds"].pop()
        await self.assert_draft_rejected(draft)

    async def test_duplicate_task_keys_rejected(self):
        draft = self.draft(); draft["implementationPlan"].append(deepcopy(draft["implementationPlan"][0]))
        await self.assert_draft_rejected(draft)

    async def test_self_or_unknown_dependency_rejected(self):
        for dependency in ("TASK-001", "TASK-UNKNOWN"):
            with self.subTest(dependency=dependency):
                draft = self.draft(); draft["implementationPlan"][0]["dependsOn"] = [dependency]
                await self.assert_draft_rejected(draft)

    async def test_cyclic_dependency_rejected(self):
        draft = self.draft()
        task = deepcopy(draft["implementationPlan"][0]); task["taskId"] = "TASK-002"; task["dependsOn"] = ["TASK-001"]
        draft["implementationPlan"][0]["dependsOn"] = ["TASK-002"]; draft["implementationPlan"].append(task)
        await self.assert_draft_rejected(draft)

    async def test_blank_titles_and_duplicate_requirement_ids_rejected(self):
        for change in ("title", "requirementIds"):
            with self.subTest(change=change):
                draft = self.draft(); task = draft["implementationPlan"][0]
                task[change] = " " if change == "title" else task[change] + task[change][:1]
                await self.assert_draft_rejected(draft)

    async def test_plan_questions_and_empty_input_questions_rejected(self):
        for draft in (self.draft(questions=["unnecessary question"]), self.draft(kind="INPUT_REQUIRED"),
            self.draft(kind="REJECTED", questions=["Model refusal prose must not be stored"])):
            await self.assert_draft_rejected(draft)

    async def test_markdown_duplicate_keys_nonfinite_and_unknown_kind_rejected(self):
        for text in ("```json\n" + json.dumps(self.draft()) + "\n```", '{"kind":"PLAN","kind":"REJECTED"}',
            '{"kind":NaN}', json.dumps({**self.draft(), "kind": "SUCCESS"})):
            with self.subTest(text=text):
                provider = FakeProvider(text_response(text))
                task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
                self.assertFalse(task.get("artifacts")); self.assertEqual(len(provider.requests), 1)

    async def test_planner_tool_request_is_denied_without_mcp_execution(self):
        call = ToolCall("untrusted-call", "write_source_file", json.dumps({"workspaceId": str(self.workspace_id), "path": "source/main.py", "content": "do not execute"}))
        provider = FakeProvider(tool_response(call))
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts")); self.assertEqual(provider.requests[0].tools, ())
        self.assertEqual(self.budget.tool_calls, 0)

    async def test_cancel_while_provider_waits_stops_worker_without_artifact(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def wait(request):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        provider = FakeProvider(wait); body = self.wire()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            await asyncio.wait_for(started.wait(), 1)
            canceled = await client.post(f"/tasks/{first['id']}:cancel", json={}, headers=self.headers())
            self.assertEqual(canceled.status_code, 200, canceled.text)
            task = await self.poll(client, first["id"], "TASK_STATE_CANCELED")
            replay = await self.send(client, deepcopy(body))
            self.assertEqual(replay, task)
        self.assertTrue(stopped.is_set()); self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(provider.requests), 1)

    async def test_shutdown_of_active_planner_does_not_resume_llm_on_restart(self):
        started, stopped = asyncio.Event(), asyncio.Event()
        async def wait(request):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                stopped.set()
        body = self.wire(); provider = FakeProvider(wait)
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client, body)
            await asyncio.wait_for(started.wait(), 1)
        self.assertTrue(stopped.is_set())
        fresh_provider = FakeProvider()
        async with self.client_for(self.executor(fresh_provider)) as (_, client):
            failed = await self.poll(client, first["id"], "TASK_STATE_FAILED")
            repeated = await self.send(client, deepcopy(body))
        self.assertEqual(repeated, failed); self.assertFalse(failed.get("artifacts"))
        self.assertEqual(fresh_provider.requests, [])


if __name__ == "__main__":
    unittest.main()
