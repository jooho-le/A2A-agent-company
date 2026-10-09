"""Real SDK HTTP/SQLite/Git/MCP QA boundaries; fake LLM and Docker only.

The initial Source comes through the real Developer executor and Host
candidate registration. Fixture tests are never executed on the Host.
This is not a real provider/container or SDK stdio execution claim.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import unittest

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx

from agents.api.validation import parse_workflow_metadata
from agents.core.config import AgentSettings
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.main import create_app
from agents.roles.outputs import validate_completed_role_output
from agents.runtime.qa import QAAgentExecutor, QAExecutorConfigurationError
from agents.runtime.qa_context import SQLiteQAContextLoader
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.qa_test_store import QATestInputStore
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.test_reports import TestReportTools
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.validation_output import parse_validation_output
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.domain.validation_artifacts import ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime

import test_developer_agent as developer_fixture
from test_llm_runtime import FakeProvider, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker


class QAAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = developer_fixture.DeveloperAgentTests("test_executor_constructor_is_inert_and_has_safe_repr")
        fixture.setUp()
        # Run these on this test's asyncio runner. The borrowed TestCase was
        # not run by unittest and has no runner/outcome of its own.
        for cleanup, args, kwargs in fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        fixture._cleanups.clear()
        self.fixture = fixture
        wire, _ = await fixture.run_to(fixture.ready_provider(), "TASK_STATE_COMPLETED")
        output = fixture.parse_completed(wire)
        developer = WorkflowStep.model_validate({**fixture.step.model_dump(),
            "status": WorkflowStepStatus.SUCCEEDED, "a2a_task_state": A2ATaskState.COMPLETED,
            "a2a_task_id": wire["id"], "agent_context_id": wire["contextId"],
            "a2a_artifact_ids": [item["artifactId"] for item in wire["artifacts"]]})
        self.repository, self.registry, self.artifacts = fixture.repository, fixture.registry, fixture.artifacts
        self._update_step(developer)
        self.run, _, validations = self.repository.record_developer_candidate(
            fixture.run.run_id, developer.workflow_step_id,
            source=output.source, change_report=output.change_report, build_report=output.build_report,
            validation_agents_configured=True,
            validation_requirement_ids={role: fixture.scenario.requirement_ids_for(validator)
                for role, validator in ((AgentRole.QA, RequirementValidator.QA),
                                         (AgentRole.SECURITY, RequirementValidator.SECURITY))})
        self.step = next(step for step in validations if step.agent_role is AgentRole.QA)
        self.source, self.configuration, self.budget = output.source, fixture.configuration, fixture.budget
        self.metadata = A2AWorkflowMetadata(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=0, code_version=1,
            requirement_ids=tuple(self.step.requirement_ids), project_artifact_ids=(self.source.artifact_id,))
        self.loader = SQLiteQAContextLoader(self.repository, lambda _: self.budget)
        self.agent_database = fixture.directory / "qa.sqlite3"
        self.binding = MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA,
            run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.frozen_source = FrozenSourceSelection(project_artifact_id=self.source.artifact_id,
                                                   snapshot_sha256=self.source.snapshot_sha256)
        self.docker = FakeDocker()
        self.sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=self.docker)
        self.unit_store, self.browser_store = UnitTestOutputStore(self.repository), BrowserTestOutputStore(self.repository)
        self.tool_store = ToolExecutionStore(self.repository)
        self.case_ids = [f"test_signup.Signup.test_req_{index}" for index in range(1, len(self.step.requirement_ids) + 1)]
        self.tool, self.selector = "run_unit_tests", "qa-unit"
        self.set_unit_report()
        self.configure()
        self.sessions, self.context_calls, self.service_calls, self.usages = [], [], [], []

    def _update_step(self, step):
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def configure(self, *, browser=False, max_call_seconds=5):
        limits = SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5)
        unit_config = None if browser else UnitTestConfiguration(scopes=(UnitTestScope(name="qa-unit", kind="QA_TESTS"),),
                                                                 limits=limits)
        browser_config = None if not browser else BrowserTestConfiguration(
            suites=(BrowserTestSuite(name="qa-browser", kind="QA_TESTS"),),
            service_argv=("/usr/local/bin/python", "-I", "-B", "/snapshot/signup.py"),
            playwright_version="1.60.0", startup_timeout_seconds=.5, action_timeout_ms=100,
            limits=limits)
        self.mcp_configuration = MCPChildConfiguration(binding=self.binding, database_path=self.repository.database_path,
            workspace_root=self.registry.base_path, frozen_source=self.frozen_source,
            unit_test_configuration=unit_config, browser_test_configuration=browser_config,
            max_call_seconds=max_call_seconds)
        self.rebuild_dispatcher()

    def rebuild_dispatcher(self):
        files = FileTools(self.artifacts, frozen_source=self.frozen_source)
        unit = UnitTestTools(self.artifacts, self.sandbox, self.unit_store,
            configuration=self.mcp_configuration.unit_test_configuration,
            max_call_seconds=self.mcp_configuration.max_call_seconds)
        browser = BrowserTestTools(self.artifacts, self.sandbox, self.browser_store,
            configuration=self.mcp_configuration.browser_test_configuration,
            max_call_seconds=self.mcp_configuration.max_call_seconds)
        self.dispatcher = MCPDispatcher(self.binding, self.registry,
            handlers={**files.handlers(AgentRole.QA), **unit.handlers(AgentRole.QA),
                      **browser.handlers(AgentRole.QA), **TestReportTools(unit, browser).handlers(AgentRole.QA)},
            max_call_seconds=self.mcp_configuration.max_call_seconds)

    def set_unit_report(self, outcomes=None, *, reported_ids=None):
        ids = self.case_ids if reported_ids is None else reported_ids
        outcomes = ["PASS"] * len(ids) if outcomes is None else outcomes
        tests = [{"testId": identifier, "outcome": outcome} for identifier, outcome in zip(ids, outcomes)]
        self.report = {"format": "unittest-v1", "total": len(tests),
            "passed": outcomes.count("PASS"), "failed": outcomes.count("FAIL"), "skipped": outcomes.count("SKIP"),
            "tests": tests}
        self.docker.exit_code = int("FAIL" in outcomes)
        self.docker.start_result = CLIResult(returncode=self.docker.exit_code,
            stdout=json.dumps(self.report).encode(), stderr=b"")

    def use_browser(self, *, fail=False):
        self.tool, self.selector = "run_browser_tests", "qa-browser"
        self.case_ids = [f"signup.req{index}" for index in range(1, len(self.step.requirement_ids) + 1)]
        self.suite_data = {"format": "browser-suite-v1", "tests": [
            {"testId": identifier, "steps": [{"action": "goto", "path": "/"},
                                            {"action": "assert_visible", "selector": "#signup"}]}
            for identifier in self.case_ids]}
        self.report = {"format": "browser-v1", "suiteName": self.selector,
            "playwrightVersion": "1.60.0", "browserVersion": "140.0.7339.0",
            "total": len(self.case_ids), "passed": len(self.case_ids) - int(fail), "failed": int(fail),
            "tests": [{"testId": identifier, "outcome": "FAIL" if fail and index == 0 else "PASS",
                "steps": [{"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 2},
                          {"index": 2, "action": "assert_visible",
                           "outcome": "FAIL" if fail and index == 0 else "PASS", "durationMs": 2}]}
                for index, identifier in enumerate(self.case_ids)]}
        if fail:
            self.report["tests"][0]["details"] = "ASSERTION_FAILED"
        self.docker.exit_code = int(fail)
        self.docker.start_result = CLIResult(returncode=int(fail), stdout=json.dumps(self.report).encode(), stderr=b"")
        self.configure(browser=True)

    def payload(self):
        from agents.runtime.qa_context import QAExecutionContext
        return QAExecutionContext(metadata=self.metadata, configuration=self.configuration, budget=self.budget,
            request_text=self.run.request_text, requirement_artifact=self.repository.get_planning_artifact(self.run.run_id),
            source=self.source).initial_payload

    def wire(self, *, payload=None, metadata=None, task_id=None, context_id=None):
        return MessageToDict(build_send_message_request(self.payload() if payload is None else payload,
            self.metadata if metadata is None else metadata, task_id=task_id, context_id=context_id))

    def context_factory(self, context):
        self.context_calls.append(parse_workflow_metadata(context.metadata))
        return self.loader(context)

    @asynccontextmanager
    async def peer_client(self, configuration):
        self.assertEqual(configuration, self.mcp_configuration)
        session = DispatcherSession(self.dispatcher)
        self.sessions.append(session)
        client = BoundMCPClient(configuration=configuration, _client=type("SDKPeerAdapter", (), {"session": session})())
        try:
            yield client
        finally:
            client._state.active = False

    def services_factory(self, execution):
        self.service_calls.append(execution.metadata)
        return QARuntimeServices(self.repository, self.registry, self.artifacts,
                                mcp_configuration=self.mcp_configuration, client_factory=self.peer_client)

    def executor(self, selected_provider, **changes):
        values = {"provider": selected_provider, "context_factory": self.context_factory,
                  "services_factory": self.services_factory, "usage_sink": self.usages.append}
        values.update(changes)
        return QAAgentExecutor(**values)

    @asynccontextmanager
    async def client_for(self, executor):
        settings = AgentSettings(role="QA", database_path=self.agent_database, log_level="CRITICAL", _env_file=None)
        app = create_app(settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                yield app, client

    async def send(self, client, body=None):
        response = await client.post("/message:send", json=self.wire() if body is None else body,
                                     headers=self.fixture.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    async def poll(self, client, task_id, state):
        return await self.fixture.poll(client, task_id, state)

    async def run_to(self, provider, state, *, body=None, executor=None):
        async with self.client_for(self.executor(provider) if executor is None else executor) as (app, client):
            first = await self.send(client, body)
            task = await self.poll(client, first["id"], state)
            revisions = [MessageToDict(item) for item in await app.state.task_store.revisions(task["id"])]
            return task, revisions

    def cases(self):
        return [{"toolName": self.tool, "selector": self.selector, "testId": identifier,
                 "requirementId": str(requirement), "title": f"QA 독립 검증 {index}",
                 "expectedResult": "동결 Acceptance Criteria를 충족한다."}
                for index, (identifier, requirement) in enumerate(zip(self.case_ids, self.step.requirement_ids), 1)]

    def draft_response(self, *, kind="READY", cases=None, questions=None, **extra):
        return text_response(json.dumps({"kind": kind, "cases": self.cases() if kind == "READY" and cases is None
                                        else [] if cases is None else cases,
                                        "questions": [] if questions is None else questions, **extra}, ensure_ascii=False))

    def write_response(self, *, path=None, content=None, call_id="qa-write-1"):
        if path is None:
            path = "outputs/qa/tests/browser/suite.json" if self.tool == "run_browser_tests" else "outputs/qa/tests/test_signup.py"
        if content is None:
            content = json.dumps(self.suite_data) if self.tool == "run_browser_tests" else "# inert QA fixture; FakeDocker never executes this\n"
        return tool_response(ToolCall(call_id=call_id, name="write_test_file",
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "path": path, "content": content})))

    def ready_provider(self):
        return FakeProvider(self.write_response(), self.draft_response())

    def parse_completed(self, wire):
        task = ParseDict(wire, Task())
        step = WorkflowStep.model_validate({**self.step.model_dump(), "status": WorkflowStepStatus.SUCCEEDED,
            "attempt": self.metadata.attempt, "a2a_task_id": task.id, "agent_context_id": task.context_id,
            "a2a_task_state": A2ATaskState.COMPLETED})
        result = parse_validation_output(task, run=self.run, step=step, source=self.source)
        self.assertEqual(result, validate_completed_role_output(AgentRole.QA, task=task, run=self.run,
                                                                step=step, source=self.source))
        return result

    def tool_calls(self):
        return [(name, args) for session in self.sessions for name, args, _options in session.calls]

    def status_code(self, task):
        return task["status"]["message"]["parts"][0]["data"]["code"]

    async def test_executor_constructor_inert_safe_repr_and_bad_capabilities(self):
        provider = self.ready_provider()
        self.assertEqual(repr(self.executor(provider)), "QAAgentExecutor()")
        self.assertFalse(self.agent_database.exists())
        self.assertEqual((provider.requests, provider.configurations, self.docker.calls), ([], [], []))
        for changes in ({"provider": None}, {"context_factory": None}, {"services_factory": None}, {"usage_sink": "untrusted"}):
            with self.subTest(changes=changes), self.assertRaises(QAExecutorConfigurationError):
                self.executor(provider, **changes)

    async def test_initial_qa_real_sdk_mcp_receipts_report_same_snapshot(self):
        provider = self.ready_provider()
        before = self.repository.get_run(self.run.run_id)
        artifacts_before = self.repository.list_project_artifacts(self.run.run_id)
        task, revisions = await self.run_to(provider, "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertEqual([artifact["name"] for artifact in task["artifacts"]], ["qa-report.json"])
        self.assertEqual(task["metadata"], self.metadata.to_a2a_json())
        self.assertIn("TASK_STATE_WORKING", [item["status"]["state"] for item in revisions])
        self.assertTrue(report.passed)
        self.assertEqual(set(report.requirement_ids), set(self.step.requirement_ids))
        self.assertEqual(report.execution_manifest, self.source.execution_manifest())
        self.assertEqual((report.code_version, report.artifact_version, report.previous_artifact_id), (1, 1, None))
        for result in report.tests:
            self.assertIs(result.outcome, ValidationOutcome.PASS)
            self.assertIs(result.tool_evidence.outcome, ToolExecutionOutcome.PASS)
            self.assertEqual(self.tool_store.get(self.binding, result.tool_evidence.execution_id).to_tool_evidence(),
                             result.tool_evidence)
        self.assertEqual([name for name, _ in self.tool_calls()], ["write_test_file", "run_unit_tests"])
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual(self.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), artifacts_before)
        self.assertIsNone(before.verdict)
        self.assertEqual(provider.configurations[0][1].name, "qa_decision")
        self.assertEqual({tool.name for tool in provider.requests[0].tools},
                         {"read_project_file", "write_test_file", "read_test_report"})

    async def test_actual_failure_is_completed_qa_fail_not_infrastructure_retry(self):
        self.set_unit_report(["FAIL", *["PASS"] * (len(self.case_ids) - 1)])
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertFalse(report.passed)
        self.assertIs(report.tests[0].outcome, ValidationOutcome.FAIL)
        self.assertEqual(report.tests[0].tool_evidence.retries_used, 0)
        self.assertIs(report.tests[0].tool_evidence.outcome, ToolExecutionOutcome.PASS)
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    async def test_skipped_case_is_unverified_without_fabricated_tool_evidence(self):
        self.set_unit_report(["SKIP", *["PASS"] * (len(self.case_ids) - 1)])
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertIs(report.tests[0].outcome, ValidationOutcome.UNVERIFIED)
        self.assertIsNone(report.tests[0].tool_evidence)
        self.assertEqual(report.tests[0].actual_result, "RUNNER_REPORTED_SKIP")
        self.assertTrue(report.has_unverified)

    async def test_missing_planned_case_cannot_turn_passed_counts_into_coverage(self):
        self.set_unit_report(reported_ids=self.case_ids[:-1])
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertIs(report.tests[-1].outcome, ValidationOutcome.UNVERIFIED)
        self.assertIsNone(report.tests[-1].tool_evidence)
        self.assertEqual(report.tests[-1].actual_result, "PLANNED_CASE_NOT_REPORTED")

    async def test_unmapped_actual_case_aborts_report_instead_of_ignoring_failure(self):
        self.set_unit_report([*["PASS"] * len(self.case_ids), "FAIL"], reported_ids=[*self.case_ids, "hidden.extra.failure"])
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "QA_REPORT_BINDING_INVALID")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_real_browser_receipt_is_bound_to_same_source_and_case_outcomes(self):
        self.use_browser(fail=True)
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertIs(report.tests[0].outcome, ValidationOutcome.FAIL)
        self.assertEqual(report.tests[0].tool_evidence.tool_name, "run_browser_tests")
        self.assertEqual(report.execution_manifest, self.source.execution_manifest())
        self.assertEqual([name for name, _ in self.tool_calls()], ["write_test_file", "run_browser_tests"])
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_no_test_file_causes_failed_task_without_fake_infrastructure_report(self):
        task, _ = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "QA_TEST_INPUT_INVALID")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_bad_runner_output_never_publishes_fake_report(self):
        self.docker.start_result = CLIResult(returncode=0, stdout=b"not-json", stderr=b"")
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "QA_TEST_UNVERIFIED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_model_cannot_submit_outcomes_as_truth(self):
        cases = self.cases()
        cases[0]["outcome"] = "PASS"
        task, _ = await self.run_to(FakeProvider(self.draft_response(cases=cases)), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_initial_handoff_source_write_or_metadata_tampering_rejected_before_model(self):
        payload = self.payload()
        payload["snapshot"]["sourceAccess"] = "WRITE"
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(self.status_code(task), "QA_INPUT_INVALID")
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.service_calls, [])

    async def test_forged_requirement_subset_context_rejected_before_model(self):
        provider = self.ready_provider()
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(
            metadata=self.metadata.model_copy(update={"requirement_ids": self.metadata.requirement_ids[:-1]})))
        self.assertEqual(self.status_code(task), "QA_CONTEXT_DENIED")
        self.assertEqual(provider.requests, [])

    async def test_out_of_scope_rejected_with_no_tests_or_registry_updates(self):
        task, _ = await self.run_to(FakeProvider(self.draft_response(kind="REJECTED")), "TASK_STATE_REJECTED")
        self.assertEqual(self.status_code(task), "QA_OUT_OF_SCOPE")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_model_refusal_and_auth_are_distinct_safe_states(self):
        for code, state in ((LLMErrorCode.AUTH, "TASK_STATE_AUTH_REQUIRED"),
                            (LLMErrorCode.REFUSAL, "TASK_STATE_REJECTED")):
            with self.subTest(code=code):
                task, _ = await self.run_to(FakeProvider(LLMRuntimeError(code)), state)
                self.assertEqual(self.status_code(task), code.value)
                self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_unknown_provider_exception_is_safe_failed_state(self):
        task, _ = await self.run_to(FakeProvider(RuntimeError("password=private-host-secret")), "TASK_STATE_FAILED")
        self.assertNotIn("private-host-secret", json.dumps(task))
        self.assertFalse(task.get("artifacts"))

    async def test_input_required_resume_preserves_task_source_budget_and_attempt(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["화면 위치를 알려주세요."]),
                                self.write_response(), self.draft_response())
        deadline, previous_model_calls = self.budget.deadline_monotonic, self.budget.model_calls
        async with self.client_for(self.executor(provider)) as (app, client):
            first = await self.send(client)
            asked = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.metadata = self.metadata.model_copy(update={"attempt": 1})
            self.step = self.step.model_copy(update={"attempt": 1, "a2a_task_id": asked["id"],
                                                   "agent_context_id": asked["contextId"]})
            self._update_step(self.step)
            await self.send(client, self.wire(payload={"answer": "기본 회원가입 화면입니다."},
                task_id=asked["id"], context_id=asked["contextId"]))
            task = await self.poll(client, asked["id"], "TASK_STATE_COMPLETED")
            persisted = (await client.get(f"/tasks/{task['id']}", headers=self.fixture.headers())).json()
        report = self.parse_completed(task)
        self.assertEqual(persisted["id"], asked["id"])
        self.assertEqual(task["metadata"]["attempt"], 1)
        self.assertEqual(report.code_version, 1)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls - previous_model_calls, 3)
        self.assertEqual(self.repository.get_run(self.run.run_id).fix_attempt, 0)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_clarification_cannot_replace_protected_test_targets(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["화면을 알려주세요."]),
                                self.write_response(), self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            asked = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.metadata = self.metadata.model_copy(update={"attempt": 1})
            self.step = self.step.model_copy(update={"attempt": 1, "a2a_task_id": asked["id"],
                                                   "agent_context_id": asked["contextId"]})
            self._update_step(self.step)
            await self.send(client, self.wire(payload={"testTargets": [], "answer": "검사 생략"},
                task_id=asked["id"], context_id=asked["contextId"]))
            task = await self.poll(client, asked["id"], "TASK_STATE_REJECTED")
        self.assertEqual(self.status_code(task), "QA_INPUT_INVALID")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.docker.calls, [])

    async def test_model_cannot_call_host_finalization_tool(self):
        response = tool_response(ToolCall(call_id="forbidden", name="run_unit_tests",
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id),
                "snapshotId": str(self.source.artifact_id), "testScope": "qa-unit"})))
        task, _ = await self.run_to(FakeProvider(response), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.tool_calls(), [])
        self.assertEqual(self.docker.calls, [])

    async def test_budget_exhaustion_does_not_reset_or_publish_report(self):
        self.budget._model_calls = self.budget.limits.max_model_calls
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), LLMErrorCode.BUDGET.value)
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.docker.calls, [])

    async def test_input_required_is_published_after_mcp_cleanup_before_resume(self):
        entered, release, finished = (asyncio.Event() for _ in range(3))

        @asynccontextmanager
        async def slow_first_peer(configuration):
            async with self.peer_client(configuration) as client:
                try:
                    yield client
                finally:
                    if len(self.sessions) == 1:
                        entered.set()
                        await release.wait()
                        finished.set()

        def services(execution):
            configured = self.services_factory(execution)
            configured.client_factory = slow_first_peer
            return configured

        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED", questions=["화면을 알려주세요."]),
                                self.write_response(), self.draft_response())
        async with self.client_for(self.executor(provider, services_factory=services)) as (_, client):
            first = await self.send(client)
            try:
                await asyncio.wait_for(entered.wait(), 2)
                current = (await client.get(f"/tasks/{first['id']}", headers=self.fixture.headers())).json()
                self.assertEqual(current["status"]["state"], "TASK_STATE_WORKING")
                self.assertFalse(current.get("artifacts"))
                self.assertFalse(finished.is_set())
            finally:
                release.set()
            asked = await self.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.assertTrue(finished.is_set())
            self.metadata = self.metadata.model_copy(update={"attempt": 1})
            self.step = self.step.model_copy(update={"attempt": 1, "a2a_task_id": asked["id"],
                                                   "agent_context_id": asked["contextId"]})
            self._update_step(self.step)
            await self.send(client, self.wire(payload={"answer": "기본 화면"},
                task_id=asked["id"], context_id=asked["contextId"]))
            completed = await self.poll(client, asked["id"], "TASK_STATE_COMPLETED")
        self.assertEqual(self.parse_completed(completed).code_version, 1)
        self.assertEqual(len(self.sessions), 2)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_completed_task_repeat_is_inert(self):
        provider = self.ready_provider()
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            completed = await self.poll(client, first["id"], "TASK_STATE_COMPLETED")
            before = (len(provider.requests), len(self.context_calls), len(self.docker.commands("start")))
            response = await client.post("/message:send", json=self.wire(payload={"answer": "재실행"},
                task_id=completed["id"], context_id=completed["contextId"]), headers=self.fixture.headers())
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["status"], "FAILED_PRECONDITION")
        self.assertEqual((len(provider.requests), len(self.context_calls), len(self.docker.commands("start"))), before)

    async def test_source_write_tool_not_offered_and_never_executed(self):
        response = tool_response(ToolCall(call_id="source-override", name="write_source_file",
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "path": "source/signup.py",
                                       "content": "# bypass QA checks"})))
        before = (self.fixture.source / "signup.py").read_bytes()
        task, _ = await self.run_to(FakeProvider(response), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual((self.fixture.source / "signup.py").read_bytes(), before)
        self.assertEqual(self.tool_calls(), [])

    async def test_frozen_source_reads_never_read_developer_working_copy(self):
        (self.fixture.source / "signup.py").write_text("# mutable tampered working copy\n", encoding="utf-8")
        read = tool_response(ToolCall(call_id="read-frozen", name="read_project_file",
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "path": "source/signup.py"})))
        provider = FakeProvider(read, self.write_response(), self.draft_response())
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        self.parse_completed(task)
        prompt = provider.requests[1].input_items_json
        self.assertNotIn("mutable tampered", prompt)
        self.assertIn("generated-candidate", prompt)

    async def test_captured_test_bytes_are_immutable_and_survive_scratch_changes(self):
        task, _ = await self.run_to(self.ready_provider(), "TASK_STATE_COMPLETED")
        self.parse_completed(task)
        with self.repository._connection() as connection:
            rows = connection.execute("SELECT capture_id FROM qa_test_input_captures").fetchall()
        self.assertEqual(len(rows), 1)
        inputs = QATestInputStore(self.repository)
        captured = inputs.get(self.binding, rows[0]["capture_id"])
        (self.fixture.root / "outputs/qa/tests/test_signup.py").write_text("# changed later\n", encoding="utf-8")
        self.assertEqual(inputs.get(self.binding, rows[0]["capture_id"]).files, captured.files)
        self.assertIn("tests/test_signup.py", captured.files)

    async def test_cancel_during_container_start_cleans_up_and_no_report(self):
        self.docker.block_start = True
        async with self.client_for(self.executor(self.ready_provider())) as (_, client):
            first = await self.send(client)
            await asyncio.wait_for(self.docker.start_entered.wait(), 3)
            response = await client.post(f"/tasks/{first['id']}:cancel", json={}, headers=self.fixture.headers())
            self.assertEqual(response.status_code, 200, response.text)
            task = await self.poll(client, first["id"], "TASK_STATE_CANCELED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)


if __name__ == "__main__":
    unittest.main()
