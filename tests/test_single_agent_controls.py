"""Single controls and durable restart through actual SDK ASGI/storage.

No cloud or Docker daemon. Host-owned inert Source and measured adapters only.
"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
import json
import unittest
from uuid import UUID
from unittest.mock import patch

import httpx
from a2a.server.context import ServerCallContext
from a2a.types import Role, TaskState
from pydantic import SecretStr
from google.protobuf.json_format import MessageToDict

from agents.core.identity import SingleAgentIdentity
from agents.llm.budget import LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.single.platform import create_single_platform
from orchestrator.a2a.client import A2AAgentClient
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.domain import AgentRole, A2ATaskState, FinalVerdict, WorkflowStatus, WorkflowStepStatus
from test_llm_runtime import FakeProvider, text_response
import test_single_agent_fix_pipeline as fix_fixture


class _Controls(fix_fixture._SingleFixHarness):
    def __init__(self, owner, **options):
        options.setdefault("qa_mode", "pass")
        super().__init__(owner, **options)
        self.selected = self.provider()
        self.token = None
        self.http_events = []
        self.platform = None
        self.prepare = self.f.prepare_workspace
        self.limits = LLMLimits(max_model_calls=self.cap, max_tool_calls=80,
            model_timeout_seconds=10, max_output_tokens=4096)

    def interrupt(self, role, *, fix=False, auth=False, times=1):
        if auth:
            decision = LLMRuntimeError(LLMErrorCode.AUTH)
        else:
            data = {"kind": "INPUT_REQUIRED", "questions": ["동결 기준 그대로 이어서 진행할까요?"]}
            if role is AgentRole.PLANNER:
                data["implementationPlan"] = []
                data["unapprovedAssumptions"] = []
            elif role is AgentRole.DEVELOPER:
                data["summary"] = ""
            elif role is AgentRole.QA:
                data["cases"] = []
            else:
                data.update(requirementReviews=[], findingReviews=[])
                if self.base.measured_security:
                    data["testCases"] = []
            decision = text_response(json.dumps(data, ensure_ascii=False))
        position = 7 if fix else {AgentRole.PLANNER: 0, AgentRole.DEVELOPER: 1,
                                  AgentRole.QA: 3, AgentRole.SECURITY: 5}[role]
        self.selected.script[position:position] = [decision] * times

    def run_id(self):
        with self.f.repository._connection() as connection:
            row = connection.execute("SELECT run_id FROM workflow_runs ORDER BY rowid LIMIT 1").fetchone()
        return None if row is None else UUID(row[0])

    def step(self, run_id, role, *, code=None):
        return next(step for step in self.f.repository.list_steps(run_id) if step.agent_role is role
                    and (code is None or step.code_version == code))

    def budget(self, run_id):
        return self.platform.budgets.resolve(self.f.repository.get_run_configuration(run_id))

    async def sdk_task(self, step):
        stored = await self.platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT].state.task_store.get(
            step.a2a_task_id, ServerCallContext(state={}))
        return stored.task

    @asynccontextmanager
    async def running(self):
        selected = self.f.settings[AgentRole.DEVELOPER].model_copy(update={"role": SingleAgentIdentity.SINGLE_AGENT,
            "database_path": self.f.directory / "single.sqlite3", "llm_limits": self.limits,
            "bearer_token": self.token})
        values = {role.value.lower() + "_agent_url": selected.agent_base_url for role in AgentRole}
        values.update({role.value.lower() + "_bearer_token": self.token for role in AgentRole})
        settings = self.f.orchestrator_settings.model_copy(update=values)
        clients = []

        async def record(request):
            self.http_events.append((request.method, request.url.path))

        def peer(url):
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.platform.agent_apps[SingleAgentIdentity.SINGLE_AGENT]),
                base_url=url, headers={"Authorization": f"Bearer {self.token.get_secret_value()}"} if self.token else {},
                event_hooks={"request": [record]})
            clients.append(client)
            return A2AAgentClient(url, httpx_client=client)

        self.platform = create_single_platform(repository=self.f.repository, workspace_registry=self.f.registry,
            artifact_store=self.f.artifacts, orchestrator_settings=settings, agent_settings=selected,
            provider=self.selected, limits=self.limits, prepare_workspace=self.prepare,
            developer_services_factory=lambda execution: self.base.services(AgentRole.DEVELOPER, execution),
            qa_services_factory=lambda execution: self.base.services(AgentRole.QA, execution),
            security_services_factory=lambda execution: self.base.services(AgentRole.SECURITY, execution),
            a2a_client_factory=peer)
        try:
            async with AsyncExitStack() as stack:
                for app in (*self.platform.agent_apps.values(), self.platform.orchestrator_app):
                    await stack.enter_async_context(app.router.lifespan_context(app))
                client = await stack.enter_async_context(httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.platform.orchestrator_app), base_url="http://127.0.0.1:8000"))
                yield client
        finally:
            for client in clients:
                await client.aclose()
            await self.platform.aclose()


class SingleAgentControlsTests(unittest.IsolatedAsyncioTestCase):
    async def test_unapproved_planner_assumption_stops_before_developer_and_preserves_baseline(self):
        h = _Controls(self)
        condition = "동아리 사람만 가입할 수 있다는 제한을 추정했다."
        h.selected.script.insert(0, text_response(json.dumps({"kind": "INPUT_REQUIRED", "implementationPlan": [],
            "questions": ["별도의 가입 제한도 필요한가요?"], "unapprovedAssumptions": [condition]}, ensure_ascii=False)))
        async with h.running() as client:
            run_id = await self.start(h, client)
            run = h.f.repository.get_run(run_id)
            self.assertIs(run.status, WorkflowStatus.WAITING_INPUT)
            self.assertIsNone(run.verdict)
            self.assertEqual(len(h.f.repository.list_steps(run_id)), 1)
            task = await h.sdk_task(h.step(run_id, AgentRole.PLANNER))
            data = MessageToDict(task.status.message.parts[0].data)
            self.assertEqual(data["unapprovedAssumptions"], [condition])
            self.assertEqual(data["assumptionApprovalStatus"], "UNAPPROVED")
            self.assertFalse(task.artifacts)
            self.assertEqual(h.budget(run_id).model_calls, 1)

    async def start(self, h, client):
        response = await asyncio.wait_for(client.post("/api/v1/runs", json=h.f.submission()), 40)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()["run"]["runId"])

    async def answer(self, h, client, run_id, step, *, auth=False):
        body = {"workflowStepId": str(step.workflow_step_id)}
        if not auth:
            body["inputData"] = {"answer": "동결 요구사항·환경·예산 그대로 진행해 주세요."}
        response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/resume", json=body), 40)
        self.assertEqual(response.status_code, 200, response.text)
        return h.f.repository.get_run(run_id)

    def assert_identity(self, before, after):
        self.assertEqual((before.workflow_step_id, before.a2a_task_id, before.agent_context_id, before.code_version),
                         (after.workflow_step_id, after.a2a_task_id, after.agent_context_id, after.code_version))
        self.assertEqual(after.attempt, before.attempt + 1)
        self.assertIs(after.status, WorkflowStepStatus.SUCCEEDED)

    async def test_planner_input_retains_task_context_frozen_config_and_budget(self):
        h = _Controls(self)
        h.interrupt(AgentRole.PLANNER)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            budget, config = h.budget(run_id), h.f.repository.get_run_configuration(run_id)
            deadline = budget.snapshot.deadline_utc
            self.assertIs(h.f.repository.get_run(run_id).status, WorkflowStatus.WAITING_INPUT)
            run = await self.answer(h, client, run_id, before)
            self.assertEqual((run.status, run.verdict, run.fix_attempt), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 0))
            self.assert_identity(before, h.step(run_id, AgentRole.PLANNER))
            self.assertIs(h.budget(run_id), budget)
            self.assertEqual((budget.model_calls, budget.snapshot.deadline_utc), (8, deadline))
            self.assertEqual(h.f.repository.get_run_configuration(run_id), config)
            task = await h.sdk_task(h.step(run_id, AgentRole.PLANNER))
            self.assertEqual(len([message for message in task.history if message.role == Role.ROLE_USER]), 2)
            entries, count = h.platform.telemetry_store.list_usage(run_id)
            self.assertEqual(count, 8)
            self.assertEqual({entry["role"] for entry in entries}, {"SINGLE_AGENT"})

    async def test_repeated_qa_input_never_dispatches_security_until_same_task_completes(self):
        h = _Controls(self)
        h.interrupt(AgentRole.QA, times=2)
        async with h.running() as client:
            run_id = await self.start(h, client)
            qa = h.step(run_id, AgentRole.QA)
            self.assertIsNone(h.step(run_id, AgentRole.SECURITY).a2a_task_id)
            run = await self.answer(h, client, run_id, qa)
            self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
            again = h.step(run_id, AgentRole.QA)
            self.assertEqual(again.a2a_task_id, qa.a2a_task_id)
            self.assertEqual(again.attempt, 1)
            self.assertIsNone(h.step(run_id, AgentRole.SECURITY).a2a_task_id)
            run = await self.answer(h, client, run_id, again)
            self.assertIs(run.verdict, FinalVerdict.SUCCESS)
            self.assert_identity(again, h.step(run_id, AgentRole.QA))
            self.assertEqual(h.step(run_id, AgentRole.SECURITY).agent_context_id, qa.agent_context_id)
            self.assertEqual(h.budget(run_id).model_calls, 9)

    async def test_security_input_observes_completed_qa_and_keeps_same_candidate(self):
        h = _Controls(self)
        h.interrupt(AgentRole.SECURITY)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before, qa = h.step(run_id, AgentRole.SECURITY), h.step(run_id, AgentRole.QA)
            sends = h.http_events.count(("POST", "/message:send"))
            run = await self.answer(h, client, run_id, before)
            self.assertIs(run.verdict, FinalVerdict.SUCCESS)
            self.assert_identity(before, h.step(run_id, AgentRole.SECURITY))
            observed = h.step(run_id, AgentRole.QA)
            self.assertEqual((observed.a2a_task_id, observed.agent_context_id, observed.attempt, observed.a2a_artifact_ids),
                             (qa.a2a_task_id, qa.agent_context_id, qa.attempt, qa.a2a_artifact_ids))
            self.assertEqual(h.http_events.count(("POST", "/message:send")), sends + 1)
            self.assertEqual(len([message for message in (await h.sdk_task(observed)).history if message.role == Role.ROLE_USER]), 1)
            self.assertEqual(len([item for item in h.f.repository.list_project_artifacts(run_id) if item.artifact_type == "SOURCE"]), 1)

    async def test_fix_input_resumes_existing_fix_instead_of_creating_another_cycle(self):
        h = _Controls(self, qa_mode="initial-failure")
        h.interrupt(AgentRole.DEVELOPER, fix=True)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.DEVELOPER, code=2)
            deadline = h.budget(run_id).snapshot.deadline_utc
            run = await self.answer(h, client, run_id, before)
            self.assertEqual((run.verdict, run.fix_attempt, run.code_version), (FinalVerdict.SUCCESS, 1, 2))
            self.assert_identity(before, h.step(run_id, AgentRole.DEVELOPER, code=2))
            self.assertEqual(len(h.fix_inputs), 1)
            self.assertEqual(h.budget(run_id).snapshot.deadline_utc, deadline)

    async def test_auth_is_configured_out_of_band_and_unknown_usage_is_not_zero(self):
        h = _Controls(self)
        h.token = SecretStr("single-controls-inert-credential")
        h.interrupt(AgentRole.PLANNER, auth=True)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            self.assertIs(before.a2a_task_state, A2ATaskState.AUTH_REQUIRED)
            run = await self.answer(h, client, run_id, before, auth=True)
            self.assertIs(run.verdict, FinalVerdict.SUCCESS)
            self.assert_identity(before, h.step(run_id, AgentRole.PLANNER))
            summary = h.platform.telemetry_store.usage_summary(run_id)
            self.assertIsNone(summary["totalTokens"])
            self.assertFalse(summary["usageComplete"])
            self.assertIsNone(summary["costUsd"])
            self.assertNotIn(h.token.get_secret_value(), h.selected.requests[1].input_items_json)

    async def test_clean_restart_restores_original_deadline_usage_and_agent_context(self):
        h = _Controls(self)
        h.interrupt(AgentRole.PLANNER)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            old = h.budget(run_id)
            snapshot = old.snapshot
        h.selected = FakeProvider(*h.selected.script)
        async with h.running() as client:
            restored = h.budget(run_id)
            self.assertIsNot(restored, old)
            self.assertEqual((restored.model_calls, restored.tool_calls, restored.total_tokens),
                             (snapshot.model_calls, snapshot.tool_calls, snapshot.known_total_tokens))
            self.assertEqual(restored.snapshot.deadline_utc, snapshot.deadline_utc)
            # Recovery observes the known INPUT_REQUIRED Task; it does not replay.
            sends = len([event for event in h.http_events if event == ("POST", "/message:send")])
            result = await client.post(f"/api/v1/runs/{run_id}/recover", json={})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(len([event for event in h.http_events if event == ("POST", "/message:send")]), sends)
            run = await self.answer(h, client, run_id, before)
            self.assertIs(run.verdict, FinalVerdict.SUCCESS)
            self.assert_identity(before, h.step(run_id, AgentRole.PLANNER))
            self.assertEqual(restored.snapshot.deadline_utc, snapshot.deadline_utc)
            self.assertEqual(len(h.f.preparations), 1)

    async def test_budget_exhaustion_rejects_answer_before_attempt_or_send(self):
        h = _Controls(self, cap=1)
        h.interrupt(AgentRole.PLANNER)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            events = len(h.http_events)
            response = await client.post(f"/api/v1/runs/{run_id}/resume", json={"inputData": {"answer": "진행"}})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(h.step(run_id, AgentRole.PLANNER), before)
            self.assertEqual(len(h.http_events), events)
            self.assertEqual(h.budget(run_id).model_calls, 1)

    async def test_restart_recovers_qa_wait_without_sending_unsent_security(self):
        h = _Controls(self)
        h.interrupt(AgentRole.QA)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.QA)
            deadline = h.budget(run_id).snapshot.deadline_utc
        h.selected = FakeProvider(*h.selected.script)
        async with h.running() as client:
            sends = h.http_events.count(("POST", "/message:send"))
            response = await client.post(f"/api/v1/runs/{run_id}/recover", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(h.http_events.count(("POST", "/message:send")), sends)
            self.assertIsNone(h.step(run_id, AgentRole.SECURITY).a2a_task_id)
            self.assertEqual(h.step(run_id, AgentRole.QA).attempt, before.attempt)
            run = await self.answer(h, client, run_id, before)
            self.assertIs(run.verdict, FinalVerdict.SUCCESS)
            self.assert_identity(before, h.step(run_id, AgentRole.QA))
            self.assertEqual(h.budget(run_id).snapshot.deadline_utc, deadline)
            self.assertEqual(len(h.f.preparations), 1)

    async def test_completed_reports_are_recovered_by_get_at_model_cap_without_reexecution(self):
        h = _Controls(self, cap=7)
        async def pause(dispatcher, run, *_args, **_kwargs):
            dispatcher._repository.transition_run_and_record(run.run_id, WorkflowStatus.HUMAN_REVIEW,
                additional_event_types=("FIXTURE_INTERRUPTED_BEFORE_INGEST",))
        async with h.running() as client:
            with patch.object(PlannerRunDispatcher, "consume_validation_results", pause):
                run_id = await self.start(h, client)
            self.assertEqual(h.budget(run_id).model_calls, 7)
            self.assertTrue(all(step.a2a_task_state is A2ATaskState.COMPLETED
                for step in h.f.repository.list_steps(run_id)))
        h.selected = FakeProvider()
        async with h.running() as client:
            sends = h.http_events.count(("POST", "/message:send"))
            response = await client.post(f"/api/v1/runs/{run_id}/recover", json={})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIs(h.f.repository.get_run(run_id).verdict, FinalVerdict.SUCCESS)
            self.assertEqual(h.http_events.count(("POST", "/message:send")), sends)
            self.assertFalse(h.selected.requests)
            self.assertEqual(h.platform.telemetry_store.load_budget(run_id).model_calls, 7)
            self.assertEqual(len(h.f.preparations), 1)

    async def test_unconfigured_single_continuation_guard_still_rejects_before_send(self):
        h = _Controls(self)
        h.interrupt(AgentRole.PLANNER)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            h.platform.orchestrator_app.state.single_continuation_ready = False
            response = await client.post(f"/api/v1/runs/{run_id}/resume", json={"inputData": {"answer": "진행"}})
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(h.step(run_id, AgentRole.PLANNER), before)
            self.assertEqual(len(h.selected.requests), 1)

    async def test_wrong_dispatcher_cannot_cancel_single_through_role_endpoints(self):
        h = _Controls(self)
        h.interrupt(AgentRole.PLANNER)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.PLANNER)
            requests = len(h.http_events)
            h.platform.dispatcher._single_agent = False
            response = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "USER_CANCELLED"})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(len(h.http_events), requests)
            self.assertEqual(h.step(run_id, AgentRole.PLANNER), before)
            self.assertIs(h.f.repository.get_run(run_id).status, WorkflowStatus.WAITING_INPUT)

    async def test_waiting_task_cancel_is_confirmed_and_preserves_single_mapping(self):
        h = _Controls(self)
        h.interrupt(AgentRole.QA)
        async with h.running() as client:
            run_id = await self.start(h, client)
            before = h.step(run_id, AgentRole.QA)
            response = await client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "USER_CANCELLED"})
            self.assertEqual(response.status_code, 200, response.text)
            run = h.f.repository.get_run(run_id)
            self.assertEqual((run.status, run.verdict), (WorkflowStatus.ABORTED, None))
            self.assertEqual([item.agent_id for item in h.f.repository.list_agent_contexts(run_id)], ["single_agent"])
            self.assertIsNone(h.step(run_id, AgentRole.SECURITY).a2a_task_id)
            self.assertEqual((await h.sdk_task(before)).status.state, TaskState.TASK_STATE_CANCELED)

    async def test_working_model_is_drained_before_aborted_is_recorded(self):
        h = _Controls(self)
        entered, drained = asyncio.Event(), asyncio.Event()
        async def slow(_request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()
        h.selected.script.insert(0, slow)
        async with h.running() as client:
            job = asyncio.create_task(client.post("/api/v1/runs", json=h.f.submission()))
            try:
                await asyncio.wait_for(entered.wait(), 5)
                run_id = h.run_id()
                for _ in range(100):
                    if h.step(run_id, AgentRole.PLANNER).a2a_task_id:
                        break
                    await asyncio.sleep(.02)
                response = await asyncio.wait_for(client.post(f"/api/v1/runs/{run_id}/cancel", json={"reason": "USER_CANCELLED"}), 8)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(drained.is_set())
                self.assertIs(h.f.repository.get_run(run_id).status, WorkflowStatus.ABORTED)
                self.assertFalse(h.f.repository.list_project_artifacts(run_id))
                self.assertEqual((await asyncio.wait_for(job, 5)).status_code, 201)
            finally:
                if not job.done():
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
