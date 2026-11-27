"""Selected QA/Security continuations preserve the other interrupted Task."""

import tempfile
import unittest
from pathlib import Path

from a2a.types import Task, TaskState

from orchestrator.a2a import A2AAgentRegistry
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.workflow_controls import (
    WorkflowControlConflict,
    WorkflowControlService,
)
from orchestrator.domain import (
    A2ATaskState,
    AgentRole,
    FinalVerdict,
    SCN_001_ID,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.infrastructure import RunDispatchConflict, SQLiteWorkflowRepository

from test_dispatch import FakePlannerClient


def copy_task(task):
    copied = Task()
    copied.CopyFrom(task)
    return copied


class InterruptedValidationClient(FakePlannerClient):
    """Real report fixtures with two independently interrupted remote Tasks."""

    def __init__(self, states=None, **kwargs):
        super().__init__("TASK_STATE_COMPLETED", **kwargs)
        self.states = states or {
            AgentRole.QA: TaskState.TASK_STATE_INPUT_REQUIRED,
            AgentRole.SECURITY: TaskState.TASK_STATE_INPUT_REQUIRED,
        }
        self.completed_tasks = {}
        self.current_tasks = {}
        self.continued = []
        self.polled = []
        self.lose_response_for = set()
        self.fail_get_for = set()

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        completed = await super().send_snapshot_handoff(
            handoff, recipient, request_text, **kwargs,
        )
        self.completed_tasks[completed.id] = copy_task(completed)
        interrupted = copy_task(completed)
        interrupted.status.state = self.states[recipient]
        interrupted.ClearField("artifacts")
        self.current_tasks[completed.id] = interrupted
        return copy_task(interrupted)

    async def continue_task(self, task_id, payload, metadata, *, context_id=None):
        current = self.current_tasks[task_id]
        if current.status.state not in (
            TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED,
        ):
            raise AssertionError("A terminal Task must not be continued again")
        self.continued.append((task_id, dict(payload), metadata, context_id))
        self.current_tasks[task_id] = copy_task(self.completed_tasks[task_id])
        if task_id in self.lose_response_for:
            self.lose_response_for.remove(task_id)
            raise ConnectionError("The remote continuation applied, but its response was lost")
        return copy_task(self.current_tasks[task_id])

    async def get_task(self, task_id):
        self.polled.append(task_id)
        if task_id in self.fail_get_for:
            self.fail_get_for.remove(task_id)
            raise ConnectionError("Temporary GET failure")
        return copy_task(self.current_tasks[task_id])


class SelectedValidationResumeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "selected-resume.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)
        self.registry = A2AAgentRegistry({
            role: f"http://{role.value.lower()}.test" for role in AgentRole
        })
        self.run = WorkflowRun(
            scenario_id=SCN_001_ID, request_text="회원가입 기능을 구현해줘.",
        )
        planner = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(self.run, (planner,), (
            TraceEvent(
                run_id=self.run.run_id, event_type="RUN_STARTED",
                actor="Orchestrator", attempt=0, workflow_state=self.run.status,
            ),
        ))

    def tearDown(self):
        self.directory.cleanup()

    async def start_interrupted(self, states=None, **kwargs):
        self.client = InterruptedValidationClient(states, **kwargs)
        dispatcher = PlannerRunDispatcher(
            self.repository, self.registry, client_factory=lambda _: self.client,
        )
        await dispatcher.dispatch_planner(self.run.run_id)
        self.assert_pending_validation()
        self.assertIsNotNone(self.repository.get_planner_plan(self.run.run_id))
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(len(self.client.handoffs), 2)
        return self.validator_steps()

    def controller_after_restart(self, *, authenticated_roles=frozenset()):
        self.repository = SQLiteWorkflowRepository(self.database_path)
        dispatcher = PlannerRunDispatcher(
            self.repository, self.registry, client_factory=lambda _: self.client,
        )
        return WorkflowControlService(
            self.repository, self.registry, dispatcher,
            client_factory=lambda _: self.client,
            authenticated_roles=authenticated_roles,
        )

    def validator_steps(self):
        run = self.repository.get_run(self.run.run_id)
        return {
            step.agent_role: step for step in self.repository.list_steps(self.run.run_id)
            if step.agent_role in (AgentRole.QA, AgentRole.SECURITY)
            and step.code_version == run.code_version
        }

    def reports(self):
        return [
            artifact for artifact in self.repository.list_project_artifacts(self.run.run_id)
            if artifact.artifact_type in ("QA_REPORT", "SECURITY_REPORT")
        ]

    def assert_pending_validation(self):
        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)
        self.assertIn(run.verdict, (None, FinalVerdict.HUMAN_REVIEW))
        self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
        self.assertEqual(self.reports(), [])
        events, _ = self.repository.list_events(self.run.run_id, limit=500, offset=0)
        self.assertFalse(any(event.event_type == "RUN_FINISHED" for event in events))

    async def assert_sequential_resume(self, first_role):
        original = await self.start_interrupted()
        other_role = AgentRole.SECURITY if first_role == AgentRole.QA else AgentRole.QA
        first, other = original[first_role], original[other_role]
        controller = self.controller_after_restart()

        await controller.resume(
            self.run.run_id, step_id=first.workflow_step_id,
            input_data={"approvedScope": "가입 기능 요구사항 그대로 검증"},
        )

        self.assert_pending_validation()
        progressed = self.validator_steps()
        self.assertEqual(progressed[first_role].status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(progressed[first_role].a2a_task_state, A2ATaskState.COMPLETED)
        self.assertEqual(progressed[first_role].attempt, 1)
        self.assertEqual(progressed[other_role].status, WorkflowStepStatus.WAITING_INPUT)
        self.assertEqual(progressed[other_role].a2a_task_state, A2ATaskState.INPUT_REQUIRED)
        self.assertEqual(progressed[other_role].attempt, 0)
        self.assertEqual([call[0] for call in self.client.continued], [first.a2a_task_id])
        self.assertEqual(self.client.polled, [other.a2a_task_id])
        self.assertEqual((len(self.client.sent), len(self.client.handoffs)), (2, 2))

        # A fresh process must reuse the already completed Task and the same
        # immutable candidate, not create a second QA/Security execution.
        controller = self.controller_after_restart()
        finished = await controller.resume(
            self.run.run_id, step_id=other.workflow_step_id,
            input_data={"approvedScope": "동일 Snapshot 검증 계속"},
        )

        self.assertEqual(finished.status, WorkflowStatus.FINISHED)
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)
        self.assertIsNone(finished.resume_state)
        self.assertEqual((finished.fix_attempt, finished.code_version), (0, 1))
        self.assertEqual([call[0] for call in self.client.continued], [first.a2a_task_id, other.a2a_task_id])
        self.assertEqual((len(self.client.sent), len(self.client.handoffs)), (2, 2))
        for role, step in self.validator_steps().items():
            self.assertEqual(step.workflow_step_id, original[role].workflow_step_id)
            self.assertEqual(step.a2a_task_id, original[role].a2a_task_id)
            self.assertEqual(step.agent_context_id, original[role].agent_context_id)
            self.assertEqual(step.attempt, 1)
            self.assertEqual(step.a2a_task_state, A2ATaskState.COMPLETED)
        self.assertEqual(len(self.reports()), 2)
        manifests = [report.execution_manifest for report in self.reports()]
        self.assertEqual(manifests[0], manifests[1])
        self.assertEqual({report.a2a_task_id for report in self.reports()}, {
            first.a2a_task_id, other.a2a_task_id,
        })
        for task_id, _, metadata, context_id in self.client.continued:
            step = next(step for step in original.values() if step.a2a_task_id == task_id)
            self.assertEqual(context_id, step.agent_context_id)
            self.assertEqual(metadata.workflow_step_id, step.workflow_step_id)
            self.assertEqual(metadata.attempt, 1)

    async def test_qa_then_security_selected_resume_finishes_same_candidate(self):
        await self.assert_sequential_resume(AgentRole.QA)

    async def test_security_then_qa_selected_resume_finishes_same_candidate(self):
        await self.assert_sequential_resume(AgentRole.SECURITY)

    async def test_broadcast_input_is_rejected_before_any_remote_side_effect(self):
        await self.start_interrupted()
        with self.assertRaises(WorkflowControlConflict):
            await self.controller_after_restart().resume(
                self.run.run_id, input_data={"approvedScope": "do not broadcast"},
            )
        self.assertEqual(self.client.continued, [])
        self.assertEqual(self.client.polled, [])
        self.assert_pending_validation()

    async def test_read_only_recovery_observes_both_input_waits_without_continuation(self):
        steps = await self.start_interrupted()
        await self.controller_after_restart().resume(self.run.run_id, recover=True)
        self.assertEqual(self.client.continued, [])
        self.assertEqual(set(self.client.polled), {step.a2a_task_id for step in steps.values()})
        self.assertTrue(all(step.attempt == 0 for step in self.validator_steps().values()))
        self.assert_pending_validation()

    async def test_unselected_auth_wait_does_not_require_auth_or_receive_input(self):
        original = await self.start_interrupted({
            AgentRole.QA: TaskState.TASK_STATE_INPUT_REQUIRED,
            AgentRole.SECURITY: TaskState.TASK_STATE_AUTH_REQUIRED,
        })
        qa, security = original[AgentRole.QA], original[AgentRole.SECURITY]
        await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
        )
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id])
        self.assertEqual(self.validator_steps()[AgentRole.SECURITY].a2a_task_state, A2ATaskState.AUTH_REQUIRED)
        self.assert_pending_validation()

        with self.assertRaises(WorkflowControlConflict):
            await self.controller_after_restart().resume(
                self.run.run_id, step_id=security.workflow_step_id,
            )
        self.assertEqual(len(self.client.continued), 1)
        finished = await self.controller_after_restart(
            authenticated_roles=frozenset({AgentRole.SECURITY}),
        ).resume(self.run.run_id, step_id=security.workflow_step_id)
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id, security.a2a_task_id])

    async def test_multiple_auth_waits_require_selection_even_if_both_are_configured(self):
        steps = await self.start_interrupted({
            role: TaskState.TASK_STATE_AUTH_REQUIRED
            for role in (AgentRole.QA, AgentRole.SECURITY)
        })
        controller = self.controller_after_restart(
            authenticated_roles=frozenset({AgentRole.QA, AgentRole.SECURITY}),
        )
        with self.assertRaises(WorkflowControlConflict):
            await controller.resume(self.run.run_id)
        self.assertEqual(self.client.continued, [])
        await controller.resume(self.run.run_id, step_id=steps[AgentRole.QA].workflow_step_id)
        self.assertEqual([call[0] for call in self.client.continued], [steps[AgentRole.QA].a2a_task_id])
        self.assert_pending_validation()

    async def test_control_lease_prevents_selected_remote_continuation(self):
        steps = await self.start_interrupted()
        controller = self.controller_after_restart()
        token = self.repository.acquire_control(self.run.run_id)
        try:
            with self.assertRaises(RunDispatchConflict):
                await controller.resume(
                    self.run.run_id, step_id=steps[AgentRole.QA].workflow_step_id,
                    input_data={"approved": True},
                )
        finally:
            self.repository.release_control(self.run.run_id, token)
        self.assertEqual(self.client.continued, [])
        self.assertEqual(self.client.polled, [])

    async def test_lost_continuation_response_is_reconciled_by_get_without_replay(self):
        steps = await self.start_interrupted()
        qa, security = steps[AgentRole.QA], steps[AgentRole.SECURITY]
        self.client.lose_response_for.add(qa.a2a_task_id)
        with self.assertRaises(WorkflowControlConflict):
            await self.controller_after_restart().resume(
                self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
            )
        uncertain = self.validator_steps()[AgentRole.QA]
        self.assertEqual(uncertain.status, WorkflowStepStatus.RUNNING)
        self.assertEqual(uncertain.attempt, 1)
        self.assert_pending_validation()

        await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
        )
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id])
        self.assertEqual(self.validator_steps()[AgentRole.QA].a2a_task_state, A2ATaskState.COMPLETED)
        self.assert_pending_validation()
        finished = await self.controller_after_restart().resume(
            self.run.run_id, step_id=security.workflow_step_id, input_data={"approved": True},
        )
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)
        self.assertEqual(len(self.client.continued), 2)

    async def test_completed_selected_task_is_observed_not_continued_again(self):
        steps = await self.start_interrupted()
        qa = steps[AgentRole.QA]
        await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
        )
        await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
        )
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id])
        self.assertEqual(self.validator_steps()[AgentRole.QA].attempt, 1)
        self.assertEqual(self.validator_steps()[AgentRole.SECURITY].attempt, 0)
        self.assert_pending_validation()

    async def test_failure_observing_other_task_preserves_selected_completion(self):
        steps = await self.start_interrupted()
        qa, security = steps[AgentRole.QA], steps[AgentRole.SECURITY]
        self.client.fail_get_for.add(security.a2a_task_id)
        with self.assertRaises(WorkflowControlConflict):
            await self.controller_after_restart().resume(
                self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
            )
        self.assertEqual(self.validator_steps()[AgentRole.QA].a2a_task_state, A2ATaskState.COMPLETED)
        self.assert_pending_validation()
        finished = await self.controller_after_restart().resume(
            self.run.run_id, step_id=security.workflow_step_id, input_data={"approved": True},
        )
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id, security.a2a_task_id])

    async def test_fix_revalidation_resumes_selected_tasks_without_another_fix_attempt(self):
        initial = await self.start_interrupted(
            qa_outcome="FAIL", qa_outcome_after_fix="PASS",
        )
        await self.controller_after_restart().resume(
            self.run.run_id, step_id=initial[AgentRole.QA].workflow_step_id,
            input_data={"approved": True},
        )
        paused = await self.controller_after_restart().resume(
            self.run.run_id, step_id=initial[AgentRole.SECURITY].workflow_step_id,
            input_data={"approved": True},
        )
        self.assertEqual(paused.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(paused.resume_state, WorkflowStatus.REVALIDATING)
        self.assertEqual((paused.fix_attempt, paused.code_version), (1, 2))
        self.assertEqual(len(self.reports()), 2)
        corrected = self.validator_steps()
        qa, security = corrected[AgentRole.QA], corrected[AgentRole.SECURITY]
        self.assertTrue(all(step.code_version == 2 for step in corrected.values()))
        self.assertEqual(qa.agent_context_id, initial[AgentRole.QA].agent_context_id)
        self.assertEqual(security.agent_context_id, initial[AgentRole.SECURITY].agent_context_id)

        paused = await self.controller_after_restart().resume(
            self.run.run_id, step_id=security.workflow_step_id,
            input_data={"approved": True},
        )
        self.assertEqual(paused.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(paused.resume_state, WorkflowStatus.REVALIDATING)
        self.assertEqual((paused.fix_attempt, paused.code_version), (1, 2))
        self.assertEqual(len(self.reports()), 2)
        self.assertEqual(self.validator_steps()[AgentRole.QA].attempt, 0)

        finished = await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id,
            input_data={"approved": True},
        )
        self.assertEqual(finished.status, WorkflowStatus.FINISHED)
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)
        self.assertEqual((finished.fix_attempt, finished.code_version), (1, 2))
        self.assertEqual((len(self.client.sent), len(self.client.handoffs)), (3, 4))
        self.assertEqual([call[0] for call in self.client.continued], [
            initial[AgentRole.QA].a2a_task_id, initial[AgentRole.SECURITY].a2a_task_id,
            security.a2a_task_id, qa.a2a_task_id,
        ])
        corrected_reports = [report for report in self.reports() if report.code_version == 2]
        self.assertEqual(len(corrected_reports), 2)
        self.assertEqual(corrected_reports[0].execution_manifest, corrected_reports[1].execution_manifest)
        self.assertEqual({report.a2a_task_id for report in corrected_reports}, {
            qa.a2a_task_id, security.a2a_task_id,
        })

    async def test_observed_remote_failure_does_not_finalize_or_restart_other_task(self):
        steps = await self.start_interrupted()
        qa, security = steps[AgentRole.QA], steps[AgentRole.SECURITY]
        self.client.current_tasks[security.a2a_task_id].status.state = TaskState.TASK_STATE_FAILED
        await self.controller_after_restart().resume(
            self.run.run_id, step_id=qa.workflow_step_id, input_data={"approved": True},
        )
        self.assert_pending_validation()
        self.assertEqual(self.validator_steps()[AgentRole.QA].a2a_task_state, A2ATaskState.COMPLETED)
        self.assertEqual(self.validator_steps()[AgentRole.SECURITY].a2a_task_state, A2ATaskState.FAILED)
        with self.assertRaises(WorkflowControlConflict):
            await self.controller_after_restart().resume(
                self.run.run_id, step_id=security.workflow_step_id,
                input_data={"approved": True},
            )
        self.assertEqual([call[0] for call in self.client.continued], [qa.a2a_task_id])

    async def test_read_only_auth_recovery_does_not_continue_even_configured_roles(self):
        steps = await self.start_interrupted({
            role: TaskState.TASK_STATE_AUTH_REQUIRED
            for role in (AgentRole.QA, AgentRole.SECURITY)
        })
        await self.controller_after_restart(
            authenticated_roles=frozenset({AgentRole.QA, AgentRole.SECURITY}),
        ).resume(self.run.run_id, recover=True)
        self.assertEqual(self.client.continued, [])
        self.assertEqual(set(self.client.polled), {step.a2a_task_id for step in steps.values()})
        self.assertTrue(all(step.attempt == 0 for step in self.validator_steps().values()))
        self.assert_pending_validation()
