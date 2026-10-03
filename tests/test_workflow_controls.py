import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.a2a import A2AAgentRegistry
from orchestrator.a2a.registry import AgentNotConfiguredError
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlService, WorkflowControlConflict
from orchestrator.domain import (
    A2ATaskState, AgentRole, SCN_001_ID, TraceEvent,
    WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus,
)
from orchestrator.infrastructure import RunDispatchConflict, SQLiteWorkflowRepository


class ControlClient:
    def __init__(self, *, state="TASK_STATE_COMPLETED", cancel_state="TASK_STATE_CANCELED"):
        self.state, self.cancel_state = state, cancel_state
        self.sent, self.continued, self.polled, self.canceled = [], [], [], []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def resolve_agent_card(self):
        pass

    def task(self, task_id, state):
        return ParseDict({"id": task_id, "contextId": "planner/context", "status": {"state": state}}, Task())

    async def send_task(self, payload, metadata, *, context_id=None):
        self.sent.append(payload)
        return self.task("planner-task", self.state)

    async def continue_task(self, task_id, payload, metadata, *, context_id=None):
        self.continued.append((task_id, payload, context_id))
        return self.task(task_id, self.state)

    async def get_task(self, task_id):
        self.polled.append(task_id)
        return self.task(task_id, self.state)

    async def cancel_task(self, task_id):
        self.canceled.append(task_id)
        return self.task(task_id, self.cancel_state)


class WorkflowControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(Path(self.directory.name) / "controls.sqlite3")
        self.registry = A2AAgentRegistry({role: "https://agent.example.test" for role in AgentRole})
        self.client = ControlClient()
        self.dispatcher = PlannerRunDispatcher(self.repository, self.registry, client_factory=lambda _: self.client)
        self.service = WorkflowControlService(self.repository, self.registry, self.dispatcher, client_factory=lambda _: self.client)

    def tearDown(self):
        self.directory.cleanup()

    def create(self, *, paused=False, state=A2ATaskState.WORKING, sent=True, task_id="planner-task", step_status=WorkflowStepStatus.RUNNING):
        run = WorkflowRun(
            scenario_id=SCN_001_ID, request_text="회원가입 구현",
            status=WorkflowStatus.HUMAN_REVIEW if paused else WorkflowStatus.PLANNING,
            resume_state=WorkflowStatus.PLANNING if paused else None,
        )
        step = WorkflowStep(
            run_id=run.run_id, agent_role=AgentRole.PLANNER,
            status=step_status, a2a_task_id=task_id, a2a_task_state=state if task_id else None,
            agent_context_id="planner/context" if task_id else None,
        )
        events = (TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                            event_type="A2A_MESSAGE_SENT", actor="Orchestrator", attempt=0),) if sent else ()
        self.repository.create_run(run, (step,), events)
        return run, step

    async def test_recovery_polls_known_task_without_duplicate_send(self):
        run, step = self.create()
        updated = await self.service.resume(run.run_id, recover=True)
        self.assertEqual(self.client.polled, [step.a2a_task_id])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.client.continued, [])
        # Task completed but no Requirement Artifact: never infer success.
        self.assertEqual(updated.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(updated.fix_attempt, 0)

    async def test_input_resume_continues_existing_task_and_context(self):
        run, step = self.create(paused=True, state=A2ATaskState.INPUT_REQUIRED, step_status=WorkflowStepStatus.WAITING_INPUT)
        await self.service.resume(run.run_id, step_id=step.workflow_step_id, input_data={"scope": "회원가입"})
        self.assertEqual(self.client.continued, [(step.a2a_task_id, {"scope": "회원가입"}, "planner/context")])
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.repository.list_steps(run.run_id)[0].attempt, 1)

    async def test_uncertain_continuation_uses_get_only(self):
        run, step = self.create(paused=True, state=A2ATaskState.INPUT_REQUIRED)
        await self.service.resume(run.run_id, recover=True)
        self.assertEqual(self.client.polled, [step.a2a_task_id])
        self.assertEqual(self.client.continued, [])

    async def test_uncertain_initial_send_cannot_be_replayed_or_aborted(self):
        run, _ = self.create(paused=True, task_id=None)
        with self.assertRaises(WorkflowControlConflict):
            await self.service.resume(run.run_id, recover=True)
        with self.assertRaises(WorkflowControlConflict):
            await self.service.cancel(run.run_id, "cancel")
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.HUMAN_REVIEW)

    async def test_proven_unsent_planner_can_be_recovered(self):
        run, _ = self.create(task_id=None, sent=False)
        await self.service.resume(run.run_id, recover=True)
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0]["workspaceId"], str(run.workspace_id))
        self.assertEqual(self.client.sent[0]["scenarioContract"]["scenarioId"], str(SCN_001_ID))

    async def test_cancel_requires_remote_canceled_confirmation(self):
        run, step = self.create(paused=True, state=A2ATaskState.AUTH_REQUIRED, step_status=WorkflowStepStatus.WAITING_INPUT)
        updated = await self.service.cancel(run.run_id, "USER_CANCELLED")
        self.assertEqual(self.client.canceled, [step.a2a_task_id])
        self.assertEqual(updated.status, WorkflowStatus.ABORTED)
        self.assertEqual(self.repository.list_steps(run.run_id)[0].a2a_task_state, A2ATaskState.CANCELED)

    async def test_failed_remote_cancel_does_not_abort(self):
        self.client.cancel_state = "TASK_STATE_WORKING"
        run, _ = self.create()
        with self.assertRaises(WorkflowControlConflict):
            await self.service.cancel(run.run_id, "cancel")
        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.PLANNING)

    async def test_auth_configuration_and_live_control_lease_are_required(self):
        run, step = self.create(paused=True, state=A2ATaskState.AUTH_REQUIRED, step_status=WorkflowStepStatus.WAITING_INPUT)
        with self.assertRaises(WorkflowControlConflict):
            await self.service.resume(run.run_id)
        token = self.repository.acquire_control(run.run_id)
        try:
            with self.assertRaises(RunDispatchConflict):
                await self.service.resume(run.run_id)
        finally:
            self.repository.release_control(run.run_id, token)
        self.service.authenticated_roles = frozenset({AgentRole.PLANNER})
        await self.service.resume(run.run_id)
        self.assertEqual(self.client.continued[0][0], step.a2a_task_id)

    def create_validation_tasks(self):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="cancel validation", status=WorkflowStatus.VALIDATING, code_version=1)
        steps = tuple(WorkflowStep(
            run_id=run.run_id, agent_role=role, code_version=1,
            status=WorkflowStepStatus.RUNNING, a2a_task_state=A2ATaskState.WORKING,
            a2a_task_id=role.value.lower() + "-task", agent_context_id="planner/context",
        ) for role in (AgentRole.QA, AgentRole.SECURITY))
        self.repository.create_run(run, steps, ())
        return run, steps

    async def test_cancel_preflights_all_endpoints_before_any_remote_side_effect(self):
        run, _ = self.create_validation_tasks()
        self.service.registry = A2AAgentRegistry({AgentRole.QA: "https://qa.example.test", AgentRole.SECURITY: None})
        with self.assertRaises(AgentNotConfiguredError):
            await self.service.cancel(run.run_id, "cancel")
        self.assertEqual(self.client.canceled, [])
        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.VALIDATING)

    async def test_partial_remote_cancellation_is_durable_before_second_agent_failure(self):
        run, _ = self.create_validation_tasks()
        successful_cancel = self.client.cancel_task

        async def fail_security(task_id):
            if task_id == "security-task":
                raise ConnectionError("temporary remote failure")
            return await successful_cancel(task_id)

        self.client.cancel_task = fail_security
        with self.assertRaises(ConnectionError):
            await self.service.cancel(run.run_id, "cancel")
        steps = self.repository.list_steps(run.run_id)
        self.assertEqual(next(s for s in steps if s.agent_role == AgentRole.QA).a2a_task_state, A2ATaskState.CANCELED)
        self.assertEqual(next(s for s in steps if s.agent_role == AgentRole.SECURITY).a2a_task_state, A2ATaskState.WORKING)
        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.VALIDATING)
        self.client.cancel_task = successful_cancel
        self.assertEqual((await self.service.cancel(run.run_id, "cancel")).status, WorkflowStatus.ABORTED)
        self.assertEqual(self.client.canceled, ["qa-task", "security-task"])
