"""Actual owned SDK HTTP controls, with synthetic model/container/proof.

The Orchestrator, four executors, MCP Dispatcher, private stores, Git and
receipts are real. Fixture SUCCESS exercises provenance and control flow,
not real signup correctness or semantic security proof. No generated source
or tests execute on the Host; restart persistence belongs to a later stage.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
import json
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx
from a2a.server.context import ServerCallContext
from a2a.types import Role, TaskState
from pydantic import SecretStr

import test_owned_agent_fix_pipeline as fix_fixture
import test_owned_agent_pipeline as pipeline_fixture
from agents.llm.budget import LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.composition import create_platform
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.domain import (
    A2ATaskState, AgentRole, FinalVerdict, WorkflowStatus, WorkflowStepStatus,
)
from test_llm_runtime import text_response


def _input_required(role):
    decision = {"kind": "INPUT_REQUIRED", "questions": ["동결 기준을 바꾸지 않고 작업을 계속할까요?"]}
    if role is AgentRole.PLANNER:
        decision["implementationPlan"] = []
    elif role is AgentRole.DEVELOPER:
        decision["summary"] = ""
    elif role is AgentRole.QA:
        decision["cases"] = []
    else:
        decision.update(requirementReviews=[], findingReviews=[])
    return text_response(json.dumps(decision, ensure_ascii=False))


class _ControlHarness(fix_fixture._FixHarness):
    def __init__(self, owner, **options):
        options.setdefault("qa_mode", "pass")
        super().__init__(owner, **options)
        self.selected_providers = self.providers()
        self.http_events = []
        self.prepare = self.base.prepare_workspace

    def interrupt(self, role, *, fix=False, auth=False):
        action = LLMRuntimeError(LLMErrorCode.AUTH) if auth else _input_required(role)
        self.selected_providers[role].script.insert(2 if fix else 0, action)

    def configure_auth(self, role):
        # This is an inert, Host-configured fixture credential, never an answer.
        token = SecretStr("owned-controls-inert-role-token")
        self.base.settings[role] = self.base.settings[role].model_copy(update={"bearer_token": token})
        self.base.orchestrator_settings = self.base.orchestrator_settings.model_copy(update={
            role.value.lower() + "_bearer_token": token})

    def a2a_client(self, url):
        role = next(role for role, settings in self.base.settings.items() if settings.agent_base_url == url)
        token = self.base.settings[role].bearer_token

        async def record(request):
            self.http_events.append((role, request.method, request.url.path))

        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.base.platform.agent_apps[role]),
            base_url=url, trust_env=False, event_hooks={"request": [record]}, headers=(
                {"Authorization": "Bearer " + token.get_secret_value()} if token is not None else None))
        self.base.http_clients.append(client)
        return A2AAgentClient(url, httpx_client=client)

    @asynccontextmanager
    async def running(self):
        # A slow fake model is cancellable before this explicit Host timeout.
        limits = LLMLimits(max_model_calls=self.model_cap, max_tool_calls=40,
            model_timeout_seconds=10, max_output_tokens=4096)
        settings = {role: selected.model_copy(update={"llm_limits": limits})
                    for role, selected in self.base.settings.items()}
        self.base.platform = create_platform(repository=self.base.repository,
            workspace_registry=self.base.registry, artifact_store=self.base.artifacts,
            orchestrator_settings=self.base.orchestrator_settings, agent_settings=settings,
            providers=self.selected_providers, prepare_workspace=self.prepare, limits=limits,
            developer_services_factory=lambda execution: self.base.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: self.base.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: self.base.services(AgentRole.SECURITY, execution),
            a2a_client_factory=self.a2a_client)
        try:
            async with AsyncExitStack() as stack:
                for app in self.base.platform.agent_apps.values():
                    await stack.enter_async_context(app.router.lifespan_context(app))
                app = self.base.platform.orchestrator_app
                await stack.enter_async_context(app.router.lifespan_context(app))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                            base_url="http://127.0.0.1:8000") as client:
                    yield client
        finally:
            for client in self.base.http_clients:
                await client.aclose()
            await self.base.platform.aclose()

    def run_id(self):
        with self.base.repository._connection() as connection:
            row = connection.execute("SELECT run_id FROM workflow_runs ORDER BY rowid LIMIT 1").fetchone()
        return None if row is None else UUID(row[0])

    def budget(self, run_id):
        return self.base.platform.budgets.resolve(self.base.repository.get_run_configuration(run_id))

    async def sdk_task(self, step):
        stored = await self.base.platform.agent_apps[step.agent_role].state.task_store.get(
            step.a2a_task_id, ServerCallContext(state={}))
        return stored.task


class OwnedAgentControlsTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, harness, client):
        response = await asyncio.wait_for(client.post("/api/v1/runs", json=harness.base.submission()), 40)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["run"]["runId"])

    def steps(self, harness, run_id):
        return harness.base.repository.list_steps(run_id)

    def role_step(self, harness, run_id, role, *, code=None):
        return next(step for step in self.steps(harness, run_id)
                    if step.agent_role is role and (code is None or step.code_version == code))

    def assert_continued(self, before, after):
        self.assertEqual((after.workflow_step_id, after.a2a_task_id, after.agent_context_id),
                         (before.workflow_step_id, before.a2a_task_id, before.agent_context_id))
        self.assertEqual(after.attempt, before.attempt + 1)
        self.assertEqual(after.input_artifact_ids, before.input_artifact_ids)
        self.assertEqual(after.code_version, before.code_version)
        self.assertIs(after.status, WorkflowStepStatus.SUCCEEDED)

    async def resume(self, harness, client, run_id, step, *, answer=True):
        body = {"workflowStepId": str(step.workflow_step_id)}
        if answer:
            body["inputData"] = {"answer": "동결 기준과 실행환경을 그대로 유지하고 진행해 주세요."}
        response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/resume", json=body), 40)
        self.assertEqual(response.status_code, 200, response.text)
        return harness.base.repository.get_run(run_id)

    def assert_shared_budget(self, harness, budget, deadline):
        self.assertTrue(all(value is budget for value in harness.budgets))
        self.assertEqual(budget.deadline_monotonic, deadline)
        self.assertEqual(budget.model_calls, sum(len(provider.requests)
            for provider in harness.selected_providers.values()))

    async def test_planner_input_resume_preserves_task_context_step_frozen_config_and_fix_cycle(self):
        harness = _ControlHarness(self)
        harness.interrupt(AgentRole.PLANNER)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            paused = harness.base.repository.get_run(run_id)
            self.assertIs(paused.status, WorkflowStatus.WAITING_INPUT)
            self.assertEqual((paused.fix_attempt, paused.code_version), (0, None))
            before = self.role_step(harness, run_id, AgentRole.PLANNER)
            self.assertIs(before.a2a_task_state, A2ATaskState.INPUT_REQUIRED)
            self.assertFalse(harness.base.repository.list_project_artifacts(run_id))
            configuration = harness.base.repository.get_run_configuration(run_id)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            run = await self.resume(harness, client, run_id, before)
            self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                             (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 0, 1))
            after = self.role_step(harness, run_id, AgentRole.PLANNER)
            self.assert_continued(before, after)
            self.assertEqual(harness.base.repository.get_run_configuration(run_id), configuration)
            task = await harness.sdk_task(after)
            self.assertEqual(task.status.state, TaskState.TASK_STATE_COMPLETED)
            self.assertEqual(len([message for message in task.history if message.role == Role.ROLE_USER]), 2)
            self.assertEqual(pipeline_fixture._task_input(harness.selected_providers[AgentRole.PLANNER].requests[1])
                             ["clarifications"], [{"answer": "동결 기준과 실행환경을 그대로 유지하고 진행해 주세요."}])
            self.assert_shared_budget(harness, budget, deadline)
            self.assertEqual(budget.model_calls, 8)

    async def assert_targeted_validation_resume(self, role, *, revalidation=False):
        harness = _ControlHarness(self, qa_mode="initial-failure" if revalidation else "pass")
        harness.interrupt(role, fix=revalidation)
        peer_role = AgentRole.SECURITY if role is AgentRole.QA else AgentRole.QA
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            paused = harness.base.repository.get_run(run_id)
            self.assertIs(paused.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertIs(paused.resume_state, WorkflowStatus.REVALIDATING if revalidation else WorkflowStatus.VALIDATING)
            code = 2 if revalidation else 1
            before = self.role_step(harness, run_id, role, code=code)
            peer = self.role_step(harness, run_id, peer_role, code=code)
            self.assertIs(before.status, WorkflowStepStatus.WAITING_INPUT)
            self.assertIs(peer.status, WorkflowStepStatus.SUCCEEDED)
            source = next(item for item in harness.base.repository.list_project_artifacts(run_id)
                          if item.artifact_type == "SOURCE" and item.code_version == code)
            peer_posts = len([event for event in harness.http_events
                             if event[0] is peer_role and event[1:] == ("POST", "/message:send")])
            peer_model_calls = len(harness.selected_providers[peer_role].requests)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            run = await self.resume(harness, client, run_id, before)
            self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                             (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, int(revalidation), code))
            self.assert_continued(before, self.role_step(harness, run_id, role, code=code))
            observed_peer = self.role_step(harness, run_id, peer_role, code=code)
            self.assertEqual((observed_peer.workflow_step_id, observed_peer.a2a_task_id,
                observed_peer.agent_context_id, observed_peer.attempt, observed_peer.a2a_artifact_ids,
                observed_peer.input_artifact_ids, observed_peer.status),
                (peer.workflow_step_id, peer.a2a_task_id, peer.agent_context_id, peer.attempt,
                 peer.a2a_artifact_ids, peer.input_artifact_ids, peer.status))
            self.assertEqual(len(harness.selected_providers[peer_role].requests), peer_model_calls)
            self.assertEqual(len([event for event in harness.http_events
                if event[0] is peer_role and event[1:] == ("POST", "/message:send")]), peer_posts)
            reports = [item for item in harness.base.repository.list_project_artifacts(run_id)
                       if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"} and item.code_version == code]
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(report.execution_manifest == source.execution_manifest() for report in reports))
            self.assert_shared_budget(harness, budget, deadline)
            self.assertEqual(budget.model_calls, 14 if revalidation else 8)

    async def test_targeted_qa_input_preserves_completed_security_and_same_snapshot(self):
        await self.assert_targeted_validation_resume(AgentRole.QA)

    async def test_targeted_security_input_preserves_completed_qa_and_same_snapshot(self):
        await self.assert_targeted_validation_resume(AgentRole.SECURITY)

    async def test_targeted_qa_revalidation_resume_preserves_completed_peer_new_source_and_fix_cycle(self):
        await self.assert_targeted_validation_resume(AgentRole.QA, revalidation=True)

    async def test_fix_developer_input_resume_does_not_create_a_new_fix_cycle_or_budget(self):
        harness = _ControlHarness(self, qa_mode="initial-failure")
        harness.interrupt(AgentRole.DEVELOPER, fix=True)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            paused = harness.base.repository.get_run(run_id)
            self.assertEqual((paused.status, paused.resume_state, paused.fix_attempt, paused.code_version),
                             (WorkflowStatus.HUMAN_REVIEW, WorkflowStatus.FIXING, 1, 1))
            before = self.role_step(harness, run_id, AgentRole.DEVELOPER, code=2)
            self.assertIs(before.a2a_task_state, A2ATaskState.INPUT_REQUIRED)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            run = await self.resume(harness, client, run_id, before)
            self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                             (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
            self.assert_continued(before, self.role_step(harness, run_id, AgentRole.DEVELOPER, code=2))
            sources = [item for item in harness.base.repository.list_project_artifacts(run_id) if item.artifact_type == "SOURCE"]
            self.assertEqual(len(sources), 2)
            self.assertEqual(len(self.steps(harness, run_id)), 7)
            self.assertEqual(harness.fix_inputs[0]["fixRequest"]["attempt"], 1)
            self.assert_shared_budget(harness, budget, deadline)
            self.assertEqual(budget.model_calls, 14)

    async def test_llm_auth_resume_uses_out_of_band_role_token_and_neutral_task_data(self):
        harness = _ControlHarness(self)
        harness.configure_auth(AgentRole.QA)
        harness.interrupt(AgentRole.QA, auth=True)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = self.role_step(harness, run_id, AgentRole.QA)
            self.assertIs(before.a2a_task_state, A2ATaskState.AUTH_REQUIRED)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            run = await self.resume(harness, client, run_id, before, answer=False)
            self.assertEqual((run.status, run.verdict), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))
            self.assert_continued(before, self.role_step(harness, run_id, AgentRole.QA))
            task_input = pipeline_fixture._task_input(harness.selected_providers[AgentRole.QA].requests[1])
            self.assertEqual(task_input["clarifications"], [{"authenticationConfigured": True}])
            self.assertNotIn("owned-controls-inert-role-token", json.dumps(task_input))
            self.assert_shared_budget(harness, budget, deadline)

    async def test_interrupted_task_cancel_requires_remote_confirmation_and_no_success(self):
        harness = _ControlHarness(self)
        harness.interrupt(AgentRole.PLANNER)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = self.role_step(harness, run_id, AgentRole.PLANNER)
            response = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "USER_CANCELLED"})
            self.assertEqual(response.status_code, 200, response.text)
            run = harness.base.repository.get_run(run_id)
            self.assertEqual((run.status, run.verdict, run.termination_reason),
                             (WorkflowStatus.ABORTED, None, "USER_CANCELLED"))
            after = self.role_step(harness, run_id, AgentRole.PLANNER)
            self.assertEqual((after.a2a_task_id, after.agent_context_id), (before.a2a_task_id, before.agent_context_id))
            self.assertIs(after.a2a_task_state, A2ATaskState.CANCELED)
            self.assertEqual((await harness.sdk_task(after)).status.state, TaskState.TASK_STATE_CANCELED)
            self.assertEqual(harness.budget(run_id).model_calls, 1)
            self.assertFalse(harness.base.repository.list_project_artifacts(run_id))

    async def wait_until(self, predicate, timeout=6):
        async def poll():
            while True:
                value = predicate()
                if value:
                    return value
                await asyncio.sleep(.02)
        return await asyncio.wait_for(poll(), timeout)

    async def test_remote_working_cancel_drains_model_before_aborted_state(self):
        harness = _ControlHarness(self)
        entered, drained = asyncio.Event(), asyncio.Event()

        async def slow(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        harness.selected_providers[AgentRole.PLANNER].script.insert(0, slow)
        async with harness.running() as client:
            submission = asyncio.create_task(client.post("/api/v1/runs", json=harness.base.submission()))
            try:
                await asyncio.wait_for(entered.wait(), 6)
                run_id = harness.run_id()
                before = await self.wait_until(lambda: next((step for step in self.steps(harness, run_id)
                    if step.a2a_task_id and step.a2a_task_state is A2ATaskState.WORKING), None))
                self.assertIs(harness.base.repository.get_run(run_id).status, WorkflowStatus.PLANNING)
                response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/cancel",
                    json={"reason": "USER_CANCELLED"}), 6)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(drained.is_set())
                run = harness.base.repository.get_run(run_id)
                self.assertEqual((run.status, run.verdict), (WorkflowStatus.ABORTED, None))
                after = self.role_step(harness, run_id, AgentRole.PLANNER)
                self.assertEqual(after.a2a_task_id, before.a2a_task_id)
                self.assertEqual((await harness.sdk_task(after)).status.state, TaskState.TASK_STATE_CANCELED)
                self.assertFalse(harness.base.repository.list_project_artifacts(run_id))
                response = await asyncio.wait_for(submission, 6)
                self.assertEqual(response.status_code, 201)
            finally:
                if not submission.done():
                    submission.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await submission

    async def test_unsent_preparation_cancel_stops_before_any_agent_http_or_model(self):
        harness = _ControlHarness(self)
        entered, drained = asyncio.Event(), asyncio.Event()

        async def prepare(_run_id):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        harness.prepare = prepare
        async with harness.running() as client:
            submission = asyncio.create_task(client.post("/api/v1/runs", json=harness.base.submission()))
            try:
                await asyncio.wait_for(entered.wait(), 6)
                run_id = harness.run_id()
                response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/cancel",
                    json={"reason": "USER_CANCELLED"}), 6)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(drained.is_set())
                run = harness.base.repository.get_run(run_id)
                self.assertEqual((run.status, run.verdict), (WorkflowStatus.ABORTED, None))
                self.assertFalse(harness.http_events)
                self.assertTrue(all(not provider.requests for provider in harness.selected_providers.values()))
                step = self.role_step(harness, run_id, AgentRole.PLANNER)
                self.assertIs(step.status, WorkflowStepStatus.CANCELED)
                self.assertIsNone(step.a2a_task_id)
                self.assertEqual(harness.budget(run_id).model_calls, 0)
                response = await asyncio.wait_for(submission, 6)
                self.assertEqual(response.status_code, 201)
            finally:
                if not submission.done():
                    submission.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await submission

    async def assert_resume_budget_denied(self, *, expired):
        harness = _ControlHarness(self, model_cap=40 if expired else 1)
        harness.interrupt(AgentRole.PLANNER)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = harness.base.repository.get_run(run_id)
            step = self.role_step(harness, run_id, AgentRole.PLANNER)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            events, total = harness.base.repository.list_events(run_id, limit=500, offset=0)
            http_events = list(harness.http_events)
            context = patch("agents.llm.budget.monotonic", return_value=deadline + 1) if expired else nullcontext()
            with context:
                response = await client.post(f"/api/v1/runs/{run_id}/resume", json={
                    "workflowStepId": str(step.workflow_step_id), "inputData": {"answer": "진행해 주세요."}})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(harness.base.repository.get_run(run_id), before)
            self.assertEqual(self.role_step(harness, run_id, AgentRole.PLANNER), step)
            self.assertEqual(harness.base.repository.list_events(run_id, limit=500, offset=0), (events, total))
            self.assertEqual(harness.http_events, http_events)
            self.assertIs(harness.budget(run_id), budget)
            self.assertEqual((budget.deadline_monotonic, budget.model_calls), (deadline, 1))

    async def test_expired_deadline_rejects_resume_before_mutating_attempt_task_or_run(self):
        await self.assert_resume_budget_denied(expired=True)

    async def test_exhausted_model_cap_rejects_resume_without_budget_reset(self):
        await self.assert_resume_budget_denied(expired=False)

    async def test_human_review_with_unverified_security_cannot_be_approved_as_success(self):
        harness = _ControlHarness(self, proof=False)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = harness.base.repository.get_run(run_id)
            self.assertIs(before.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertNotEqual(before.verdict, FinalVerdict.SUCCESS)
            budget = harness.budget(run_id)
            calls = budget.model_calls
            artifacts = harness.base.repository.list_project_artifacts(run_id)
            response = await client.post(f"/api/v1/runs/{run_id}/resume", json={})
            self.assertEqual(response.status_code, 409, response.text)
            after = harness.base.repository.get_run(run_id)
            self.assertIs(after.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertNotEqual(after.verdict, FinalVerdict.SUCCESS)
            self.assertEqual((after.code_version, after.fix_attempt), (1, 0))
            self.assertEqual(harness.base.repository.list_project_artifacts(run_id), artifacts)
            self.assertEqual(budget.model_calls, calls)
            self.assertTrue(next(item for item in artifacts if item.artifact_type == "SECURITY_REPORT").has_unverified)

    async def test_resumed_working_task_cancel_drains_owner_without_new_task_or_budget(self):
        harness = _ControlHarness(self)
        harness.interrupt(AgentRole.PLANNER)
        entered, drained = asyncio.Event(), asyncio.Event()

        async def slow(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        harness.selected_providers[AgentRole.PLANNER].script.insert(1, slow)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = self.role_step(harness, run_id, AgentRole.PLANNER)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            continuation = asyncio.create_task(client.post(f"/api/v1/runs/{run_id}/resume", json={
                "workflowStepId": str(before.workflow_step_id), "inputData": {"answer": "진행해 주세요."}}))
            try:
                await asyncio.wait_for(entered.wait(), 6)
                working = await self.wait_until(lambda: next((step for step in self.steps(harness, run_id)
                    if step.workflow_step_id == before.workflow_step_id
                    and step.a2a_task_state is A2ATaskState.WORKING), None))
                self.assertEqual((working.a2a_task_id, working.agent_context_id, working.attempt),
                                 (before.a2a_task_id, before.agent_context_id, before.attempt + 1))
                canceled = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/cancel",
                    json={"reason": "USER_CANCELLED"}), 6)
                self.assertEqual(canceled.status_code, 200, canceled.text)
                self.assertTrue(drained.is_set())
                resumed = await asyncio.wait_for(continuation, 6)
                self.assertEqual(resumed.status_code, 409, resumed.text)
                after = self.role_step(harness, run_id, AgentRole.PLANNER)
                self.assertEqual((after.a2a_task_id, after.agent_context_id, after.attempt),
                                 (before.a2a_task_id, before.agent_context_id, before.attempt + 1))
                self.assertEqual((await harness.sdk_task(after)).status.state, TaskState.TASK_STATE_CANCELED)
                run = harness.base.repository.get_run(run_id)
                self.assertEqual((run.status, run.verdict, run.fix_attempt), (WorkflowStatus.ABORTED, None, 0))
                self.assertEqual((budget.deadline_monotonic, budget.model_calls), (deadline, 2))
                self.assertIs(harness.budget(run_id), budget)
                self.assertEqual(len(self.steps(harness, run_id)), 1)
                self.assertFalse(harness.base.repository.list_project_artifacts(run_id))
            finally:
                if not continuation.done():
                    continuation.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await continuation

    async def test_recover_unsent_validators_uses_canonical_handoff_same_source_and_old_budget(self):
        harness = _ControlHarness(self)

        async def stop_before_validation(*_arguments, **_keywords):
            # Simulate interruption after the real Source/Build registration,
            # before either validation Agent has received a message.
            return None

        async with harness.running() as client:
            with patch.object(harness.base.platform.dispatcher, "_dispatch_validation_agents", stop_before_validation):
                run_id = await self.submit(harness, client)
            before = harness.base.repository.get_run(run_id)
            self.assertIs(before.status, WorkflowStatus.VALIDATING)
            validators = [step for step in self.steps(harness, run_id)
                          if step.agent_role in {AgentRole.QA, AgentRole.SECURITY}]
            self.assertEqual(len(validators), 2)
            self.assertTrue(all(step.a2a_task_id is None for step in validators))
            artifacts = harness.base.repository.list_project_artifacts(run_id)
            source = next(item for item in artifacts if item.artifact_type == "SOURCE")
            configuration = harness.base.repository.get_run_configuration(run_id)
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            self.assertEqual(budget.model_calls, 3)
            response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/recover", json={}), 20)
            self.assertEqual(response.status_code, 200, response.text)
            run = harness.base.repository.get_run(run_id)
            self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                             (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 0, 1))
            updated = harness.base.repository.list_project_artifacts(run_id)
            self.assertEqual([item for item in updated if item.artifact_type == "SOURCE"], [source])
            reports = [item for item in updated if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}]
            self.assertEqual(len(reports), 2)
            self.assertTrue(all(item.execution_manifest == source.execution_manifest() for item in reports))
            for initial in validators:
                step = self.role_step(harness, run_id, initial.agent_role)
                self.assertEqual((step.workflow_step_id, step.input_artifact_ids, step.attempt),
                                 (initial.workflow_step_id, [source.artifact_id], 0))
                self.assertIs(step.status, WorkflowStepStatus.SUCCEEDED)
                self.assertTrue(step.a2a_task_id and step.agent_context_id)
            self.assertEqual(harness.base.repository.get_run_configuration(run_id), configuration)
            self.assert_shared_budget(harness, budget, deadline)
            self.assertEqual(budget.model_calls, 7)

    async def assert_completed_report_observation(self, *, failed, available=False):
        harness = _ControlHarness(self, model_cap=40 if available else 7,
                                  qa_mode="initial-failure" if failed else "pass")

        async def stop_before_consume(*_arguments, **_keywords):
            # SDK Tasks and private receipts are genuinely complete. Only the
            # Orchestrator's final project-report ingestion is interrupted.
            return None

        async with harness.running() as client:
            with patch.object(harness.base.platform.dispatcher, "consume_validation_results", stop_before_consume):
                run_id = await self.submit(harness, client)
            self.assertIs(harness.base.repository.get_run(run_id).status, WorkflowStatus.VALIDATING)
            initial_steps = self.steps(harness, run_id)
            self.assertEqual(len(initial_steps), 4)
            self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in initial_steps))
            self.assertFalse(any(item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}
                for item in harness.base.repository.list_project_artifacts(run_id)))
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            self.assertEqual(budget.model_calls, 7)
            if not available:
                self.assertEqual(budget.model_calls, budget.limits.max_model_calls)
            http_count = len(harness.http_events)
            requests = {role: len(provider.requests) for role, provider in harness.selected_providers.items()}
            response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/recover", json={}), 10)
            self.assertEqual(response.status_code, 200, response.text)
            after = harness.base.repository.get_run(run_id)
            self.assertEqual((after.status, after.verdict),
                (WorkflowStatus.FIX_REQUIRED, None) if failed else (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))
            self.assertEqual((after.fix_attempt, after.code_version), (0, 1))
            recovered_steps = self.steps(harness, run_id)
            self.assertEqual(len(recovered_steps), 4)
            for previous, current in zip(initial_steps, recovered_steps, strict=True):
                self.assertEqual((current.workflow_step_id, current.a2a_task_id, current.agent_context_id,
                                  current.attempt, current.input_artifact_ids),
                                 (previous.workflow_step_id, previous.a2a_task_id, previous.agent_context_id,
                                  previous.attempt, previous.input_artifact_ids))
            self.assertTrue(all(event[1] == "GET" for event in harness.http_events[http_count:]))
            self.assertEqual({role: len(provider.requests) for role, provider in harness.selected_providers.items()}, requests)
            self.assertIs(harness.budget(run_id), budget)
            self.assert_shared_budget(harness, budget, deadline)
            reports = [item for item in harness.base.repository.list_project_artifacts(run_id)
                       if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}]
            self.assertEqual(len(reports), 2)
            source = next(item for item in harness.base.repository.list_project_artifacts(run_id)
                          if item.artifact_type == "SOURCE")
            self.assertTrue(all(item.execution_manifest == source.execution_manifest() for item in reports))
            if failed:
                self.assertTrue(harness.base.repository.list_issue_records(run_id))
                old_http = list(harness.http_events)
                explicit = await client.post(f"/api/v1/runs/{run_id}/resume", json={})
                if not available:
                    self.assertEqual(explicit.status_code, 409, explicit.text)
                    self.assertIn("CONTROL_BUDGET_UNAVAILABLE", explicit.json()["detail"])
                    self.assertEqual(harness.base.repository.get_run(run_id), after)
                    self.assertEqual(harness.http_events, old_http)
                    self.assertEqual(len(self.steps(harness, run_id)), 4)
                    self.assertEqual((budget.deadline_monotonic, budget.model_calls), (deadline, 7))
                else:
                    self.assertEqual(explicit.status_code, 200, explicit.text)
                    completed = harness.base.repository.get_run(run_id)
                    self.assertEqual((completed.status, completed.verdict, completed.fix_attempt, completed.code_version),
                                     (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
                    self.assertEqual(len(self.steps(harness, run_id)), 7)
                    updated = harness.base.repository.list_project_artifacts(run_id)
                    sources = sorted([item for item in updated if item.artifact_type == "SOURCE"],
                                     key=lambda item: item.code_version)
                    self.assertEqual([item.code_version for item in sources], [1, 2])
                    self.assertEqual(sources[0], source)
                    self.assertEqual(sources[1].previous_artifact_id, source.artifact_id)
                    revised_reports = [item for item in updated
                        if item.artifact_type in {"BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"}
                        and item.code_version == 2]
                    self.assertEqual(len(revised_reports), 3)
                    self.assertTrue(all(item.execution_manifest == sources[1].execution_manifest()
                                        for item in revised_reports))
                    self.assertEqual(len(harness.fix_inputs), 1)
                    self.assertEqual(harness.fix_inputs[0]["fixRequest"]["attempt"], 1)
                    self.assertIs(harness.budget(run_id), budget)
                    self.assert_shared_budget(harness, budget, deadline)
                    self.assertEqual(budget.model_calls, 13)

    async def test_completed_reports_recover_at_model_cap_by_get_without_new_execution(self):
        await self.assert_completed_report_observation(failed=False)

    async def test_failed_report_observation_at_cap_records_issue_without_auto_fix_or_reset(self):
        await self.assert_completed_report_observation(failed=True)

    async def test_failed_report_observation_needs_explicit_fix_resume_with_existing_budget(self):
        await self.assert_completed_report_observation(failed=True, available=True)

    async def test_duplicate_input_for_selected_completed_qa_is_get_only_at_model_cap(self):
        harness = _ControlHarness(self, model_cap=7)
        harness.interrupt(AgentRole.QA)
        harness.interrupt(AgentRole.SECURITY)
        async with harness.running() as client:
            run_id = await self.submit(harness, client)
            before = self.role_step(harness, run_id, AgentRole.QA)
            self.assertIs(before.a2a_task_state, A2ATaskState.INPUT_REQUIRED)
            body = {"workflowStepId": str(before.workflow_step_id),
                    "inputData": {"answer": "동결 기준을 유지하고 QA를 진행해 주세요."}}
            first = await client.post(f"/api/v1/runs/{run_id}/resume", json=body)
            self.assertEqual(first.status_code, 200, first.text)
            qa = self.role_step(harness, run_id, AgentRole.QA)
            security = self.role_step(harness, run_id, AgentRole.SECURITY)
            self.assert_continued(before, qa)
            self.assertIs(qa.a2a_task_state, A2ATaskState.COMPLETED)
            self.assertIs(security.a2a_task_state, A2ATaskState.INPUT_REQUIRED)
            self.assertIs(security.status, WorkflowStepStatus.WAITING_INPUT)
            run = harness.base.repository.get_run(run_id)
            self.assertEqual((run.status, run.resume_state, run.code_version, run.fix_attempt),
                (WorkflowStatus.HUMAN_REVIEW, WorkflowStatus.VALIDATING, 1, 0))
            budget = harness.budget(run_id)
            deadline = budget.deadline_monotonic
            self.assertEqual(budget.model_calls, budget.limits.max_model_calls)
            qa_task, security_task = await harness.sdk_task(qa), await harness.sdk_task(security)
            calls = {role: len(provider.requests) for role, provider in harness.selected_providers.items()}
            http_count = len(harness.http_events)
            artifacts = harness.base.repository.list_project_artifacts(run_id)

            def forbid_execution_preflight(_run_id):
                self.fail("A duplicate answer for completed QA must not request new execution budget")

            with patch.object(harness.base.platform.orchestrator_app.state, "control_preflight", forbid_execution_preflight):
                duplicate = await client.post(f"/api/v1/runs/{run_id}/resume", json=body)
            self.assertEqual(duplicate.status_code, 200, duplicate.text)
            after_qa = self.role_step(harness, run_id, AgentRole.QA)
            after_security = self.role_step(harness, run_id, AgentRole.SECURITY)
            self.assertEqual(after_qa, qa)
            self.assertEqual(after_security, security)
            self.assertEqual(await harness.sdk_task(after_qa), qa_task)
            self.assertEqual(await harness.sdk_task(after_security), security_task)
            self.assertTrue(all(event[1] == "GET" for event in harness.http_events[http_count:]))
            self.assertEqual({role: len(provider.requests) for role, provider in harness.selected_providers.items()}, calls)
            self.assertEqual(harness.base.repository.list_project_artifacts(run_id), artifacts)
            self.assertEqual((budget.model_calls, budget.deadline_monotonic), (7, deadline))
            self.assertIs(harness.budget(run_id), budget)
            after = harness.base.repository.get_run(run_id)
            self.assertEqual((after.status, after.resume_state, after.code_version, after.fix_attempt),
                             (run.status, run.resume_state, run.code_version, run.fix_attempt))
            self.assertNotEqual(after.verdict, FinalVerdict.SUCCESS)
