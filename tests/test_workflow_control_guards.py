"""Control preflight cannot consume attempts, authority, or an old budget."""

import asyncio
import threading
import unittest

import test_workflow_controls as fixtures
from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMRuntimeError, TokenUsage
from orchestrator.api.schemas.runs import ResumeRunRequest
from orchestrator.application.workflow_controls import WorkflowControlConflict
from orchestrator.domain import A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import ActiveAgentTaskError
from pydantic import ValidationError


class ControlGuardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = fixtures.WorkflowControlTests()
        self.base.setUp()
        self.addCleanup(self.base.tearDown)

    async def test_failed_budget_preflight_keeps_pause_attempt_and_task(self):
        run, step = self.base.create(paused=True, state=A2ATaskState.INPUT_REQUIRED,
                                     step_status=WorkflowStepStatus.WAITING_INPUT)

        def deny(_):
            raise RuntimeError("private provider text must not leak")

        self.base.service.preflight = deny
        with self.assertRaisesRegex(WorkflowControlConflict, "CONTROL_BUDGET_UNAVAILABLE"):
            await self.base.service.resume(run.run_id, input_data={"answer": "original scope"})
        self.assertEqual(self.base.repository.get_run(run.run_id), run)
        self.assertEqual(self.base.repository.list_steps(run.run_id), [step])
        self.assertFalse(self.base.client.continued)

    async def test_input_for_working_or_completed_task_is_not_silently_ignored(self):
        for state in (A2ATaskState.WORKING, A2ATaskState.COMPLETED):
            with self.subTest(state=state):
                run, step = self.base.create(paused=True, state=state,
                    step_status=WorkflowStepStatus.SUCCEEDED if state == A2ATaskState.COMPLETED
                    else WorkflowStepStatus.RUNNING)
                with self.assertRaisesRegex(WorkflowControlConflict, "INPUT_REQUIRED"):
                    await self.base.service.resume(run.run_id, input_data={"answer": "continue"})
                self.assertEqual(self.base.repository.list_steps(run.run_id), [step])
                self.assertFalse(self.base.client.polled)

    async def test_credentials_and_frozen_fields_reject_before_resume_or_message(self):
        run, step = self.base.create(paused=True, state=A2ATaskState.INPUT_REQUIRED,
                                     step_status=WorkflowStepStatus.WAITING_INPUT)
        for data in ({"plan": {}}, {"snapshot": {}}, {"token": "private-value"}, {"answer": "Bearer private-value"}):
            with self.subTest(data=list(data)):
                with self.assertRaises(ValueError):
                    await self.base.service.resume(run.run_id, input_data=data)
                with self.assertRaises(ValidationError):
                    ResumeRunRequest(inputData=data)
        self.assertEqual(self.base.repository.get_run(run.run_id), run)
        self.assertEqual(self.base.repository.list_steps(run.run_id), [step])
        self.assertFalse(self.base.client.continued)

    async def test_auth_uses_noncredential_nonprotected_continuation(self):
        run, _ = self.base.create(paused=True, state=A2ATaskState.AUTH_REQUIRED,
                                 step_status=WorkflowStepStatus.WAITING_INPUT)
        self.base.service.authenticated_roles = frozenset({self.base.repository.list_steps(run.run_id)[0].agent_role})
        await self.base.service.resume(run.run_id)
        self.assertEqual(self.base.client.continued[0][1], {"authenticationConfigured": True})

    async def test_read_only_recovery_observes_interrupted_task_without_budget_or_send(self):
        run, step = self.base.create(paused=True, state=A2ATaskState.INPUT_REQUIRED,
                                     step_status=WorkflowStepStatus.WAITING_INPUT)
        self.base.client.state = "TASK_STATE_INPUT_REQUIRED"

        def forbid(_):
            self.fail("GET-only recovery cannot request a new execution budget")

        self.base.service.preflight = forbid
        result = await self.base.service.resume(run.run_id, recover=True)
        self.assertEqual(result.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(self.base.client.polled, [step.a2a_task_id])
        self.assertFalse(self.base.client.continued)
        self.assertFalse(self.base.client.sent)
        self.assertEqual(self.base.repository.list_steps(run.run_id)[0].attempt, 0)

    async def test_uncertain_continuation_must_be_observed_before_cancel(self):
        run, _ = self.base.create(paused=True, state=A2ATaskState.INPUT_REQUIRED)
        with self.assertRaisesRegex(WorkflowControlConflict, "uncertain continuation"):
            await self.base.service.cancel(run.run_id, "USER_CANCELLED")
        self.assertFalse(self.base.client.canceled)

    async def test_proven_unsent_claimed_step_can_cancel_without_remote_task(self):
        run, step = self.base.create(sent=False, task_id=None)
        result = await self.base.service.cancel(run.run_id, "USER_CANCELLED")
        self.assertEqual(result.status, WorkflowStatus.ABORTED)
        saved = self.base.repository.list_steps(run.run_id)[0]
        self.assertEqual(saved.status, WorkflowStepStatus.CANCELED)
        self.assertIsNone(saved.a2a_task_id)
        self.assertIsNone(saved.a2a_task_state)
        self.assertFalse(self.base.client.canceled)

    async def test_repository_itself_rejects_sent_receipt_without_task_id(self):
        run, _ = self.base.create(sent=True, task_id=None)
        with self.assertRaises(ActiveAgentTaskError):
            self.base.repository.complete_remote_cancellation(run.run_id, "USER_CANCELLED", {})
        self.assertEqual(self.base.repository.get_run(run.run_id), run)

    async def test_task_observer_drains_sqlite_worker_before_releasing_cancel(self):
        run, step = self.base.create()
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        real_save = self.base.repository.save_task_update

        def blocked(*args):
            started.set()
            release.wait(5)
            try:
                return real_save(*args)
            finally:
                finished.set()

        self.base.repository.save_task_update = blocked
        context = fixtures.ControlClient().task(step.a2a_task_id, "TASK_STATE_WORKING")
        from orchestrator.domain import AgentContext, TraceEvent
        observer = self.base.repository.task_update_observer(run)
        call = asyncio.create_task(observer(step, AgentContext(
            run_id=run.run_id, agent_id="planner", agent_context_id=context.context_id,
            latest_a2a_task_id=context.id,
        ), TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                      event_type="A2A_TASK_STATE_CHANGED", actor="Orchestrator", attempt=0)))
        try:
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            call.cancel()
            await asyncio.sleep(0.01)
            call.cancel()
            await asyncio.sleep(0.01)
            self.assertFalse(call.done())
        finally:
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await call
        self.assertTrue(finished.is_set())


class NonConsumingBudgetTests(unittest.TestCase):
    def test_preflight_does_not_reserve_or_reset_and_rejects_exhaustion(self):
        budget = ExecutionBudget(runtime_budget_ms=1000, limits=LLMLimits(max_model_calls=1))
        deadline = budget.deadline_monotonic
        budget.check_model_call()
        budget.check_model_call()
        self.assertEqual(budget.model_calls, 0)
        budget.reserve_model_call()
        with self.assertRaises(LLMRuntimeError):
            budget.check_model_call()
        self.assertEqual(budget.model_calls, 1)
        self.assertEqual(budget.deadline_monotonic, deadline)

    def test_unknown_tokens_and_insufficient_output_cap_stay_blocked(self):
        for usage in (None, TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2)):
            budget = ExecutionBudget(runtime_budget_ms=1000, limits=LLMLimits(max_total_tokens=16))
            budget.account_usage(usage)
            with self.assertRaises(LLMRuntimeError):
                budget.check_model_call()
            self.assertEqual(budget.model_calls, 0)
