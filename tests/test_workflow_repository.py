import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.infrastructure import (
    ActiveAgentTaskError,
    RunDispatchConflict,
    SQLiteWorkflowRepository,
)


class SQLiteWorkflowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "workflow.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_bundle(self, *, status: WorkflowStatus = WorkflowStatus.RECEIVED):
        run = WorkflowRun(
            scenario_id=uuid4(),
            request_text="Implement a feature",
            status=status,
        )
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        events = (
            TraceEvent(
                run_id=run.run_id,
                event_type="RUN_STARTED",
                actor="Orchestrator",
                attempt=0,
                workflow_state=run.status,
            ),
            TraceEvent(
                run_id=run.run_id,
                workflow_step_id=step.workflow_step_id,
                event_type="WORKFLOW_STEP_CREATED",
                actor="Orchestrator",
                attempt=0,
                workflow_state=run.status,
            ),
        )
        self.repository.create_run(run, (step,), events)
        return run, step, events

    def test_run_step_and_trace_survive_repository_reopen_in_order(self) -> None:
        run, step, events = self.create_bundle()

        reopened = SQLiteWorkflowRepository(self.database_path)

        self.assertEqual(reopened.get_run(run.run_id), run)
        self.assertEqual(reopened.list_steps(run.run_id), [step])
        stored_events, total = reopened.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 2)
        self.assertEqual([event.event_id for event in stored_events], [e.event_id for e in events])

    def test_failed_initial_trace_insert_rolls_back_entire_run_creation(self) -> None:
        run = WorkflowRun(scenario_id=uuid4(), request_text="Atomic create")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        duplicate_event_id = uuid4()
        events = (
            TraceEvent(
                event_id=duplicate_event_id,
                run_id=run.run_id,
                event_type="RUN_STARTED",
                actor="Orchestrator",
                attempt=0,
            ),
            TraceEvent(
                event_id=duplicate_event_id,
                run_id=run.run_id,
                workflow_step_id=step.workflow_step_id,
                event_type="WORKFLOW_STEP_CREATED",
                actor="Orchestrator",
                attempt=0,
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create_run(run, (step,), events)

        self.assertIsNone(self.repository.get_run(run.run_id))
        self.assertEqual(self.repository.list_steps(run.run_id), [])

    def test_planner_dispatch_claim_is_atomic_and_one_shot(self) -> None:
        run, step, _ = self.create_bundle()

        claimed_run, claimed_step = self.repository.claim_planner_dispatch(run.run_id)

        self.assertEqual(claimed_run.status, WorkflowStatus.PLANNING)
        self.assertEqual(claimed_step.workflow_step_id, step.workflow_step_id)
        self.assertEqual(claimed_step.status, WorkflowStepStatus.RUNNING)
        self.assertEqual(self.repository.get_run(run.run_id), claimed_run)
        self.assertEqual(self.repository.list_steps(run.run_id), [claimed_step])
        with self.assertRaises(RunDispatchConflict):
            self.repository.claim_planner_dispatch(run.run_id)

    def test_task_step_context_and_trace_write_atomically(self) -> None:
        run, step, _ = self.create_bundle()
        context_id = "planner-context-opaque"
        task_id = "planner-task-opaque"
        updated_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": task_id,
                "a2a_task_state": A2ATaskState.WORKING,
                "agent_context_id": context_id,
            }
        )
        context = AgentContext(
            run_id=run.run_id,
            agent_id="planner-agent",
            agent_context_id=context_id,
            latest_a2a_task_id=task_id,
        )
        event = TraceEvent(
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            a2a_task_id=task_id,
            agent_context_id=context_id,
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
            a2a_task_state=A2ATaskState.WORKING,
            workflow_state=run.status,
        )

        asyncio.run(
            self.repository.task_update_observer(run)(updated_step, context, event)
        )

        self.assertGreater(self.repository.get_run(run.run_id).updated_at, run.updated_at)
        self.assertEqual(self.repository.list_steps(run.run_id), [updated_step])
        self.assertEqual(self.repository.list_agent_contexts(run.run_id), [context])
        stored_events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual(stored_events[-1].event_id, event.event_id)

    def test_failed_trace_insert_rolls_back_step_and_agent_context_changes(self) -> None:
        run, step, events = self.create_bundle()
        context_id = "planner-context-opaque"
        task_id = "planner-task-opaque"
        updated_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": task_id,
                "a2a_task_state": A2ATaskState.WORKING,
                "agent_context_id": context_id,
            }
        )
        context = AgentContext(
            run_id=run.run_id,
            agent_id="planner-agent",
            agent_context_id=context_id,
            latest_a2a_task_id=task_id,
        )
        duplicate_event = TraceEvent(
            event_id=events[0].event_id,
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_task_update(run, updated_step, context, duplicate_event)

        self.assertEqual(self.repository.get_run(run.run_id), run)
        self.assertEqual(self.repository.list_steps(run.run_id), [step])
        self.assertEqual(self.repository.list_agent_contexts(run.run_id), [])
        stored_events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 2)
        self.assertEqual([event.event_id for event in stored_events], [e.event_id for e in events])

    def test_cancel_aborts_run_cancels_pending_step_and_records_trace(self) -> None:
        run, step, _ = self.create_bundle()

        canceled_run = self.repository.cancel_run(run.run_id, "USER_CANCELLED")

        self.assertEqual(canceled_run.status, WorkflowStatus.ABORTED)
        self.assertEqual(canceled_run.termination_reason, "USER_CANCELLED")
        self.assertIsNone(canceled_run.verdict)
        self.assertEqual(
            self.repository.list_steps(run.run_id)[0].status,
            WorkflowStepStatus.CANCELED,
        )
        events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 4)
        self.assertEqual(events[-2].event_type, "RUN_ABORTED")
        self.assertEqual(events[-1].event_type, "WORKFLOW_STEP_CANCELED")

    def test_active_a2a_task_is_not_falsely_marked_canceled(self) -> None:
        run, step, _ = self.create_bundle(status=WorkflowStatus.IMPLEMENTING)
        active_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": "developer-task",
                "a2a_task_state": A2ATaskState.WORKING,
            }
        )
        self.repository.save_run_update(
            run,
            (
                TraceEvent(
                    run_id=run.run_id,
                    workflow_step_id=step.workflow_step_id,
                    event_type="WORKFLOW_STEP_UPDATED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=run.status,
                ),
            ),
        )
        # Persist the active Step through the same validated Task-update path.
        active_context = AgentContext(
            run_id=run.run_id,
            agent_id="developer-agent",
            latest_a2a_task_id="developer-task",
        )
        task_event = TraceEvent(
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            a2a_task_id="developer-task",
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
            a2a_task_state=A2ATaskState.WORKING,
            workflow_state=run.status,
        )
        self.repository.save_task_update(run, active_step, active_context, task_event)

        with self.assertRaises(ActiveAgentTaskError):
            self.repository.cancel_run(run.run_id, "USER_CANCELLED")

        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.IMPLEMENTING)
        self.assertEqual(self.repository.list_steps(run.run_id)[0].status, WorkflowStepStatus.RUNNING)


if __name__ == "__main__":
    unittest.main()
