"""Actual A2A/SQLite/Git/MCP boundaries; provider and Docker are fixtures only.

No generated Source or scanner is executed on the Host. These checks do not
prove real model, Bandit/container, or signup security-policy compliance.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx

from agents.core.config import AgentSettings
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.main import create_app
from agents.roles.outputs import validate_completed_role_output
from agents.runtime.security import SecurityAgentExecutor, SecurityExecutorConfigurationError
from agents.runtime.security_context import SecurityExecutionContext, SQLiteSecurityContextLoader
from agents.runtime.security_services import SecurityRuntimeServices, SecuritySemanticProof
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.execution_store import ToolExecutionStore
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.tools.files import FileTools
from mcp_tools.tools.security import SecurityScanTools
from mcp_tools.tools.security_config import SecurityScanConfiguration, SecurityScannerProfile
from mcp_tools.tools.security_store import SecurityScanOutputStore
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.a2a import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.validation_output import parse_validation_output
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.run_configuration import RunConfiguration
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import FindingDisposition, ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime

import test_developer_agent as developer_fixture
from test_llm_runtime import FakeProvider, text_response, tool_response
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker, IMAGE_ID


SCANNER_REF = "https://scanner.example.invalid/bandit/v1"


class SecurityAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fixture = developer_fixture.DeveloperAgentTests("test_executor_constructor_is_inert_and_has_safe_repr")
        # Configure the immutable Run before its creation, never rewrite it.
        with patch("test_developer_agent.RunConfiguration", side_effect=lambda **values:
                   RunConfiguration(scanner_profile_ref=SCANNER_REF, **values)):
            fixture.setUp()
        for cleanup, args, kwargs in fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        fixture._cleanups.clear()
        self.fixture = fixture
        wire, _ = await fixture.run_to(fixture.ready_provider(), "TASK_STATE_COMPLETED")
        output = fixture.parse_completed(wire)
        self.repository, self.registry, self.artifacts = fixture.repository, fixture.registry, fixture.artifacts
        developer = WorkflowStep.model_validate({**fixture.step.model_dump(),
            "status": WorkflowStepStatus.SUCCEEDED, "a2a_task_state": A2ATaskState.COMPLETED,
            "a2a_task_id": wire["id"], "agent_context_id": wire["contextId"],
            "a2a_artifact_ids": [item["artifactId"] for item in wire["artifacts"]]})
        self._update_step(developer)
        self.run, _, validations = self.repository.record_developer_candidate(
            fixture.run.run_id, developer.workflow_step_id,
            source=output.source, change_report=output.change_report, build_report=output.build_report,
            validation_agents_configured=True,
            validation_requirement_ids={role: fixture.scenario.requirement_ids_for(validator)
                for role, validator in ((AgentRole.QA, RequirementValidator.QA),
                                        (AgentRole.SECURITY, RequirementValidator.SECURITY))})
        self.step = next(step for step in validations if step.agent_role is AgentRole.SECURITY)
        self.source, self.configuration, self.budget = output.source, fixture.configuration, fixture.budget
        self.metadata = A2AWorkflowMetadata(run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=0, code_version=1,
            requirement_ids=tuple(self.step.requirement_ids), project_artifact_ids=(self.source.artifact_id,))
        self.loader = SQLiteSecurityContextLoader(self.repository, lambda _: self.budget)
        self.agent_database = fixture.directory / "security.sqlite3"
        self.binding = MCPBinding(role=AgentRole.SECURITY, agent_role=AgentRole.SECURITY,
            run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        self.docker = FakeDocker()
        self.sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=self.docker)
        self.scan_store, self.tool_store = SecurityScanOutputStore(self.repository), ToolExecutionStore(self.repository)
        self.scanner = SecurityScannerProfile(name="python-security", scanner_version="1.8.6",
                                              rule_ids=("B101", "B307"), profile_ref=SCANNER_REF)
        self.scan_configuration = SecurityScanConfiguration(profiles=(self.scanner,), image_reference=IMAGE_ID,
            limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5))
        self.mcp_configuration = MCPChildConfiguration(binding=self.binding,
            database_path=self.repository.database_path, workspace_root=self.registry.base_path,
            frozen_source=FrozenSourceSelection(project_artifact_id=self.source.artifact_id,
                                               snapshot_sha256=self.source.snapshot_sha256),
            security_scan_configuration=self.scan_configuration, max_call_seconds=5)
        files = FileTools(self.artifacts, frozen_source=self.mcp_configuration.frozen_source)
        scans = SecurityScanTools(self.artifacts, self.sandbox, self.scan_store,
                                 configuration=self.scan_configuration, max_call_seconds=5)
        self.dispatcher = MCPDispatcher(self.binding, self.registry,
            handlers={**files.handlers(AgentRole.SECURITY), **scans.handlers(AgentRole.SECURITY)}, max_call_seconds=5)
        self.sessions, self.usages = [], []
        self.set_scan_report()

    def _update_step(self, step):
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def set_scan_report(self, *, findings=False):
        self.scan_report = {"format": "bandit-v1", "profileName": self.scanner.name, "scanner": "bandit",
            "scannerVersion": self.scanner.scanner_version, "ruleIds": sorted(self.scanner.rule_ids),
            "profileRef": self.scanner.profile_ref, "scannedFiles": ["signup.py"], "findings": [
                {"ruleId": "B101", "testName": "assert_used", "path": "signup.py", "line": 1, "column": 0,
                 "severity": "HIGH", "confidence": "HIGH", "status": "SUSPECTED"}] if findings else []}
        self.docker.exit_code = int(findings)
        self.docker.start_result = CLIResult(returncode=int(findings), stdout=json.dumps(self.scan_report).encode(), stderr=b"")

    def payload(self):
        return SecurityExecutionContext(metadata=self.metadata, configuration=self.configuration, budget=self.budget,
            request_text=self.run.request_text, requirement_artifact=self.repository.get_planning_artifact(self.run.run_id),
            source=self.source).initial_payload

    def wire(self, *, payload=None, metadata=None, task_id=None, context_id=None):
        return MessageToDict(build_send_message_request(self.payload() if payload is None else payload,
            self.metadata if metadata is None else metadata, task_id=task_id, context_id=context_id))

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
        return SecurityRuntimeServices(self.repository, self.registry, self.artifacts,
            mcp_configuration=self.mcp_configuration, client_factory=self.peer_client)

    def executor(self, selected_provider, **changes):
        values = {"provider": selected_provider, "context_factory": self.loader,
                  "services_factory": self.services_factory, "usage_sink": self.usages.append}
        values.update(changes)
        return SecurityAgentExecutor(**values)

    @asynccontextmanager
    async def client_for(self, executor):
        settings = AgentSettings(role="SECURITY", database_path=self.agent_database, log_level="CRITICAL", _env_file=None)
        app = create_app(settings, executor=executor)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=settings.agent_base_url) as client:
                yield app, client

    async def send(self, client, body=None):
        response = await client.post("/message:send", json=self.wire() if body is None else body,
                                     headers=self.fixture.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["task"]

    async def run_to(self, provider, state, *, body=None, executor=None):
        async with self.client_for(self.executor(provider) if executor is None else executor) as (app, client):
            first = await self.send(client, body)
            task = await self.fixture.poll(client, first["id"], state)
            revisions = [MessageToDict(item) for item in await app.state.task_store.revisions(task["id"])]
            return task, revisions

    @staticmethod
    def input_data(request):
        return json.loads(json.loads(request.input_items_json)[0]["content"])["taskInput"]

    def draft(self, request=None, *, kind="READY", references=None, outcome="UNVERIFIED", disposition="SUSPECTED"):
        measured = {} if request is None else self.input_data(request)["measuredSecurity"]
        rows = [finding for profile in measured.get("profiles", []) for finding in profile["findings"]]
        return {"kind": kind, "questions": ["보안 기준의 추가 설명이 필요합니다."] if kind == "INPUT_REQUIRED" else [],
            "requirementReviews": [{"requirementId": str(value), "proposedOutcome": outcome,
                "rationale": "동결 요구사항은 정적 검사 결과만으로 입증할 수 없습니다.",
                "references": [] if references is None else references} for value in self.metadata.requirement_ids] if kind == "READY" else [],
            "findingReviews": [{"findingId": row["findingId"], "proposedDisposition": disposition,
                "rationale": "스캐너 경고는 독립된 근거 검증을 요구합니다.",
                "references": [] if references is None else references} for row in rows] if kind == "READY" else []}

    def draft_response(self, **changes):
        return lambda request: text_response(json.dumps(self.draft(request, **changes), ensure_ascii=False))

    def read_response(self, *, call_id="source-read", path="source/signup.py", name="read_project_file"):
        return tool_response(ToolCall(call_id=call_id, name=name,
            arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "path": path})))

    def parse_completed(self, wire):
        task = ParseDict(wire, Task())
        step = WorkflowStep.model_validate({**self.step.model_dump(), "status": WorkflowStepStatus.SUCCEEDED,
            "attempt": self.metadata.attempt, "a2a_task_id": task.id, "agent_context_id": task.context_id,
            "a2a_task_state": A2ATaskState.COMPLETED})
        report = parse_validation_output(task, run=self.run, step=step, source=self.source)
        self.assertEqual(report, validate_completed_role_output(AgentRole.SECURITY, task=task,
            run=self.run, step=step, source=self.source))
        return report

    def calls(self):
        return [name for session in self.sessions for name, _args, _options in session.calls]

    @staticmethod
    def status_code(task):
        return task["status"]["message"]["parts"][0]["data"]["code"]

    async def test_constructor_inert_safe_repr_and_bad_capabilities(self):
        provider = FakeProvider(self.draft_response())
        self.assertEqual(repr(self.executor(provider)), "SecurityAgentExecutor()")
        self.assertFalse(self.agent_database.exists())
        self.assertEqual(provider.requests, [])
        self.assertEqual(self.docker.calls, [])
        for changes in ({"provider": None}, {"context_factory": None}, {"services_factory": None}, {"usage_sink": "untrusted"}):
            with self.subTest(changes=changes), self.assertRaises(SecurityExecutorConfigurationError):
                self.executor(provider, **changes)

    async def test_zero_warnings_completed_report_is_not_security_pass(self):
        provider = FakeProvider(self.draft_response())
        before = self.repository.get_run(self.run.run_id)
        before_artifacts = self.repository.list_project_artifacts(self.run.run_id)
        task, revisions = await self.run_to(provider, "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertEqual([artifact["name"] for artifact in task["artifacts"]], ["security-report.json"])
        self.assertEqual((report.code_version, report.artifact_version, report.previous_artifact_id), (1, 1, None))
        self.assertEqual(report.execution_manifest, self.source.execution_manifest())
        self.assertEqual(report.requirement_ids, tuple(self.step.requirement_ids))
        self.assertTrue(report.has_unverified)
        self.assertFalse(report.passed)
        self.assertFalse(report.findings)
        self.assertTrue(all(item.outcome is ValidationOutcome.UNVERIFIED and item.tool_evidence is None
                            for item in report.requirement_results))
        self.assertIn("TASK_STATE_WORKING", [item["status"]["state"] for item in revisions])
        self.assertEqual(self.calls(), ["run_security_scan"])
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual(self.repository.get_run(self.run.run_id), before)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), before_artifacts)
        self.assertEqual({tool.name for tool in provider.requests[0].tools}, {"read_project_file", "read_security_report"})

    async def test_warning_is_preserved_as_suspected_without_product_retry(self):
        self.set_scan_report(findings=True)
        task, _ = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertEqual(len(report.findings), 1)
        self.assertIs(report.findings[0].disposition, FindingDisposition.SUSPECTED)
        self.assertEqual(report.findings[0].rule_id, "B101")
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    async def test_model_proposals_with_real_reads_are_not_host_proof(self):
        self.set_scan_report(findings=True)
        references = [{"path": "signup.py", "startLine": 1, "endLine": 1}]
        provider = FakeProvider(self.read_response(), self.draft_response(references=references,
                                outcome="PASS", disposition="FALSE_POSITIVE"))
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertTrue(report.has_unverified)
        self.assertIs(report.findings[0].disposition, FindingDisposition.SUSPECTED)
        self.assertEqual(self.calls(), ["run_security_scan", "read_project_file"])

    async def test_frozen_source_reads_do_not_use_mutable_working_copy(self):
        (self.fixture.source / "signup.py").write_text("# mutable tampered working copy\n", encoding="utf-8")
        provider = FakeProvider(self.read_response(), self.draft_response(
            references=[{"path": "signup.py", "startLine": 1, "endLine": 1}]))
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED")
        self.parse_completed(task)
        self.assertNotIn("mutable tampered", provider.requests[1].input_items_json)
        self.assertIn("generated-candidate", provider.requests[1].input_items_json)

    async def test_bad_scanner_output_failed_task_no_fabricated_artifact(self):
        self.docker.start_result = CLIResult(returncode=0, stdout=b"not-json", stderr=b"")
        provider = FakeProvider(self.draft_response())
        task, _ = await self.run_to(provider, "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), "SECURITY_SCAN_UNVERIFIED")
        self.assertFalse(task.get("artifacts"))
        self.assertFalse(provider.requests)

    async def test_forged_handoff_rejected_before_model_or_scanner(self):
        payload = self.payload()
        payload["snapshot"]["sourceAccess"] = "WRITE"
        provider = FakeProvider(self.draft_response())
        task, _ = await self.run_to(provider, "TASK_STATE_REJECTED", body=self.wire(payload=payload))
        self.assertEqual(self.status_code(task), "SECURITY_INPUT_INVALID")
        self.assertFalse(provider.requests)
        self.assertFalse(self.docker.calls)

    async def test_wrong_requirement_subset_rejected_before_model(self):
        task, _ = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_REJECTED",
            body=self.wire(metadata=self.metadata.model_copy(update={"requirement_ids": self.metadata.requirement_ids[:-1]})))
        self.assertEqual(self.status_code(task), "SECURITY_CONTEXT_DENIED")
        self.assertFalse(self.docker.calls)

    async def test_forbidden_write_or_scan_tool_not_model_capability(self):
        for name in ("write_source_file", "run_security_scan"):
            with self.subTest(name=name):
                response = tool_response(ToolCall(call_id=name, name=name, arguments_json="{}"))
                task, _ = await self.run_to(FakeProvider(response), "TASK_STATE_FAILED")
                self.assertEqual(self.status_code(task), LLMErrorCode.TOOL_POLICY.value)
                self.assertFalse(task.get("artifacts"))
        self.assertEqual(self.calls(), ["run_security_scan", "run_security_scan"])

    async def test_unread_source_reference_cannot_be_accepted(self):
        task, _ = await self.run_to(FakeProvider(self.draft_response(
            references=[{"path": "signup.py", "startLine": 1, "endLine": 1}], outcome="PASS")), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))

    async def test_omitted_scanner_warning_never_publishes_clean_report(self):
        self.set_scan_report(findings=True)
        def omit(request):
            data = self.draft(request)
            data["findingReviews"] = []
            return text_response(json.dumps(data))
        task, _ = await self.run_to(FakeProvider(omit), "TASK_STATE_FAILED")
        self.assertFalse(task.get("artifacts"))

    async def test_input_required_resume_keeps_task_source_budget_not_fix_cycle(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED"), self.draft_response())
        deadline, calls = self.budget.deadline_monotonic, self.budget.model_calls
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            asked = await self.fixture.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.metadata = self.metadata.model_copy(update={"attempt": 1})
            self.step = self.step.model_copy(update={"attempt": 1, "a2a_task_id": asked["id"],
                                                   "agent_context_id": asked["contextId"]})
            self._update_step(self.step)
            await self.send(client, self.wire(payload={"answer": "동결 기준을 그대로 사용하세요."},
                task_id=asked["id"], context_id=asked["contextId"]))
            task = await self.fixture.poll(client, asked["id"], "TASK_STATE_COMPLETED")
        report = self.parse_completed(task)
        self.assertEqual(task["id"], asked["id"])
        self.assertEqual(task["metadata"]["attempt"], 1)
        self.assertEqual(report.code_version, 1)
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assertEqual(self.budget.model_calls - calls, 2)
        self.assertEqual(self.repository.get_run(self.run.run_id).fix_attempt, 0)
        self.assertEqual(len(self.docker.commands("start")), 2)

    async def test_clarification_cannot_remove_scanner_or_change_policy(self):
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED"), self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            asked = await self.fixture.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.metadata = self.metadata.model_copy(update={"attempt": 1})
            self.step = self.step.model_copy(update={"attempt": 1, "a2a_task_id": asked["id"],
                                                   "agent_context_id": asked["contextId"]})
            self._update_step(self.step)
            await self.send(client, self.wire(payload={"scannerProfiles": [], "answer": "검사를 생략"},
                task_id=asked["id"], context_id=asked["contextId"]))
            task = await self.fixture.poll(client, asked["id"], "TASK_STATE_REJECTED")
        self.assertEqual(self.status_code(task), "SECURITY_INPUT_INVALID")
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(len(self.docker.commands("start")), 1)

    async def test_provider_auth_refusal_and_unknown_error_use_safe_states(self):
        for error, state, code in ((LLMRuntimeError(LLMErrorCode.AUTH), "TASK_STATE_AUTH_REQUIRED", LLMErrorCode.AUTH.value),
                                   (LLMRuntimeError(LLMErrorCode.REFUSAL), "TASK_STATE_REJECTED", LLMErrorCode.REFUSAL.value),
                                   (RuntimeError("password=private-host-secret"), "TASK_STATE_FAILED", "LLM_PROVIDER_ERROR")):
            with self.subTest(state=state):
                task, _ = await self.run_to(FakeProvider(error), state)
                self.assertEqual(self.status_code(task), code)
                self.assertFalse(task.get("artifacts"))
                self.assertNotIn("private-host-secret", json.dumps(task))

    async def test_tool_budget_exhaustion_never_resets_or_fakes_report(self):
        self.budget._tool_calls = self.budget.limits.max_tool_calls
        task, _ = await self.run_to(FakeProvider(self.draft_response()), "TASK_STATE_FAILED")
        self.assertEqual(self.status_code(task), LLMErrorCode.BUDGET.value)
        self.assertFalse(task.get("artifacts"))
        self.assertFalse(self.docker.calls)

    async def test_terminal_task_repeat_has_no_side_effect(self):
        provider = FakeProvider(self.draft_response())
        async with self.client_for(self.executor(provider)) as (_, client):
            first = await self.send(client)
            completed = await self.fixture.poll(client, first["id"], "TASK_STATE_COMPLETED")
            before = (len(provider.requests), len(self.docker.calls))
            response = await client.post("/message:send", json=self.wire(payload={"answer": "재실행"},
                task_id=completed["id"], context_id=completed["contextId"]), headers=self.fixture.headers())
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual((len(provider.requests), len(self.docker.calls)), before)

    async def test_cancel_scanner_start_cleans_up_no_report(self):
        self.docker.block_start = True
        async with self.client_for(self.executor(FakeProvider(self.draft_response()))) as (_, client):
            first = await self.send(client)
            await asyncio.wait_for(self.docker.start_entered.wait(), 3)
            response = await client.post(f"/tasks/{first['id']}:cancel", json={}, headers=self.fixture.headers())
            self.assertEqual(response.status_code, 200, response.text)
            task = await self.fixture.poll(client, first["id"], "TASK_STATE_CANCELED")
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_read_actual_scan_report_is_offered_and_bound(self):
        def read_report(request):
            report_ref = self.input_data(request)["measuredSecurity"]["profiles"][0]["reportRef"]
            return tool_response(ToolCall(call_id="read-scan-receipt", name="read_security_report",
                arguments_json=json.dumps({"workspaceId": str(self.run.workspace_id), "reportRef": report_ref})))
        task, _ = await self.run_to(FakeProvider(read_report, self.draft_response()), "TASK_STATE_COMPLETED")
        self.parse_completed(task)
        self.assertEqual(self.calls(), ["run_security_scan", "read_security_report"])

    async def test_fixture_host_proof_binds_fail_and_confirmed_without_final_verdict(self):
        # This explicit verifier tests provenance only. It is NOT an actual
        # signup validator or proof that this fixture Source is vulnerable.
        self.set_scan_report(findings=True)
        def fixture_verifier(execution, decision, measured):
            self.assertIn(b"generated-candidate", measured.source_files["signup.py"])
            self.assertEqual(measured.source_artifact_id, self.source.artifact_id)
            self.assertEqual(len(decision.finding_reviews), 1)
            return SecuritySemanticProof(run_id=execution.metadata.run_id,
                workflow_step_id=execution.metadata.workflow_step_id, source_artifact_id=measured.source_artifact_id,
                snapshot_sha256=measured.snapshot_sha256,
                requirement_outcomes=((self.metadata.requirement_ids[0], "FAIL"),),
                finding_dispositions=((measured.finding_ids[0], "CONFIRMED"),))
        def services(execution):
            return SecurityRuntimeServices(self.repository, self.registry, self.artifacts,
                mcp_configuration=self.mcp_configuration, client_factory=self.peer_client, proof_verifier=fixture_verifier)
        references = [{"path": "signup.py", "startLine": 1, "endLine": 1}]
        provider = FakeProvider(self.read_response(), self.draft_response(
            references=references, outcome="FAIL", disposition="CONFIRMED"))
        task, _ = await self.run_to(provider, "TASK_STATE_COMPLETED", executor=self.executor(provider, services_factory=services))
        report = self.parse_completed(task)
        self.assertIs(report.requirement_results[0].outcome, ValidationOutcome.FAIL)
        evidence = report.requirement_results[0].tool_evidence
        self.assertEqual(evidence.tool_name, "run_security_scan")
        self.assertEqual(self.tool_store.get(self.binding, evidence.execution_id).to_tool_evidence(), evidence)
        self.assertIs(report.findings[0].disposition, FindingDisposition.CONFIRMED)
        self.assertEqual(len(self.docker.commands("start")), 1)
        self.assertIsNone(self.repository.get_run(self.run.run_id).verdict)

    async def test_input_required_waits_for_transport_cleanup(self):
        entered, release, finished = (asyncio.Event() for _ in range(3))
        @asynccontextmanager
        async def slow_peer(configuration):
            async with self.peer_client(configuration) as client:
                try:
                    yield client
                finally:
                    entered.set()
                    await release.wait()
                    finished.set()
        def services(execution):
            return SecurityRuntimeServices(self.repository, self.registry, self.artifacts,
                mcp_configuration=self.mcp_configuration, client_factory=slow_peer)
        provider = FakeProvider(self.draft_response(kind="INPUT_REQUIRED"))
        async with self.client_for(self.executor(provider, services_factory=services)) as (_, client):
            first = await self.send(client)
            try:
                await asyncio.wait_for(entered.wait(), 3)
                current = (await client.get(f"/tasks/{first['id']}", headers=self.fixture.headers())).json()
                self.assertEqual(current["status"]["state"], "TASK_STATE_WORKING")
                self.assertFalse(current.get("artifacts"))
            finally:
                release.set()
            task = await self.fixture.poll(client, first["id"], "TASK_STATE_INPUT_REQUIRED")
            self.assertTrue(finished.is_set())
            self.assertFalse(task.get("artifacts"))


if __name__ == "__main__":
    unittest.main()
