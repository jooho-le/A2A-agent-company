"""Recovery regressions using complete Planner/Build/QA/Security provenance."""

import tempfile
import unittest
from pathlib import Path

from a2a.types import Task

from orchestrator.a2a import A2AAgentRegistry
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlService
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
from orchestrator.infrastructure import SQLiteWorkflowRepository

from test_dispatch import FakePlannerClient


class RecoverableClient(FakePlannerClient):
    """Return the original completed reports when recovery fetches known Tasks."""

    def __init__(self, **kwargs):
        super().__init__("TASK_STATE_COMPLETED", **kwargs)
        self.saved_tasks = {}
        self.polled = []

    async def send_snapshot_handoff(self, *args, **kwargs):
        task = await super().send_snapshot_handoff(*args, **kwargs)
        stored = Task()
        stored.CopyFrom(task)
        self.saved_tasks[task.id] = stored
        return task

    async def get_task(self, task_id):
        self.polled.append(task_id)
        task = Task()
        task.CopyFrom(self.saved_tasks[task_id])
        return task


class StopBeforeReportConsumption(PlannerRunDispatcher):
    async def consume_validation_results(self, *args, **kwargs):
        # Simulate restart after Task observers committed but before Report ingest.
        return None


class StopBeforeThirdFixSend(PlannerRunDispatcher):
    async def _dispatch_fix(self, run, plan, scenario, issues):
        if run.fix_attempt == 2:
            # Atomically create attempt 3, then stop before A2A_MESSAGE_SENT.
            self._repository.start_fix_cycle(run.run_id, issues, developer_configured=True)
            return
        await super()._dispatch_fix(run, plan, scenario, issues)


class WorkflowRecoveryIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.directory.name) / "recovery.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)
        self.registry = A2AAgentRegistry({role: f"http://{role.value.lower()}.test" for role in AgentRole})
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 기능을 구현해줘.")
        self.planner_step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(self.run, (self.planner_step,), (
            TraceEvent(run_id=self.run.run_id, event_type="RUN_STARTED", actor="Orchestrator", attempt=0, workflow_state=self.run.status),
            TraceEvent(run_id=self.run.run_id, workflow_step_id=self.planner_step.workflow_step_id, event_type="WORKFLOW_STEP_CREATED", actor="Orchestrator", attempt=0, workflow_state=self.run.status),
        ))

    def tearDown(self):
        self.directory.cleanup()

    def controller_after_restart(self, client):
        # A fresh repository/dispatcher must use durable Plan, Step and Issue data.
        self.repository = SQLiteWorkflowRepository(self.database_path)
        dispatcher = PlannerRunDispatcher(self.repository, self.registry, client_factory=lambda _: client)
        return WorkflowControlService(self.repository, self.registry, dispatcher, client_factory=lambda _: client)

    async def test_missing_developer_endpoint_resumes_durable_plan_to_success(self):
        client = RecoverableClient()
        initial_registry = A2AAgentRegistry({
            AgentRole.PLANNER: "http://planner.test", AgentRole.DEVELOPER: None,
            AgentRole.QA: "http://qa.test", AgentRole.SECURITY: "http://security.test",
        })
        dispatcher = PlannerRunDispatcher(self.repository, initial_registry, client_factory=lambda _: client)
        await dispatcher.dispatch_planner(self.run.run_id)
        paused = self.repository.get_run(self.run.run_id)
        self.assertEqual(paused.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(paused.resume_state, WorkflowStatus.IMPLEMENTING)
        pending = next(step for step in self.repository.list_steps(self.run.run_id) if step.agent_role == AgentRole.DEVELOPER)
        self.assertEqual(pending.status, WorkflowStepStatus.PENDING)
        self.assertIsNone(pending.a2a_task_id)
        original_plan_artifact = self.repository.get_planning_artifact(self.run.run_id)
        self.assertIsNotNone(original_plan_artifact)
        self.assertEqual(len(client.sent), 1)

        controller = self.controller_after_restart(client)
        completed = await controller.resume(self.run.run_id)

        self.assertEqual(completed.status, WorkflowStatus.FINISHED)
        self.assertEqual(completed.verdict, FinalVerdict.SUCCESS)
        self.assertEqual(completed.fix_attempt, 0)
        self.assertEqual(completed.code_version, 1)
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(len(client.handoffs), 2)
        self.assertEqual(client.polled, [])
        payload, metadata, context_id = client.sent[-1]
        self.assertEqual(payload["sourceArtifact"]["projectArtifactId"], str(original_plan_artifact.artifact_id))
        self.assertEqual(metadata.workflow_step_id, pending.workflow_step_id)
        self.assertIsNone(context_id)
        self.assertEqual(len([step for step in self.repository.list_steps(self.run.run_id) if step.agent_role == AgentRole.DEVELOPER]), 1)

    async def test_completed_validation_tasks_are_refetched_without_agent_resend(self):
        client = RecoverableClient()
        dispatcher = StopBeforeReportConsumption(self.repository, self.registry, client_factory=lambda _: client)
        await dispatcher.dispatch_planner(self.run.run_id)
        interrupted = self.repository.get_run(self.run.run_id)
        self.assertEqual(interrupted.status, WorkflowStatus.VALIDATING)
        validators = [step for step in self.repository.list_steps(self.run.run_id) if step.agent_role in (AgentRole.QA, AgentRole.SECURITY)]
        self.assertEqual(len(validators), 2)
        self.assertTrue(all(step.status == WorkflowStepStatus.SUCCEEDED and step.a2a_task_state == A2ATaskState.COMPLETED for step in validators))
        artifact_types = {artifact.artifact_type for artifact in self.repository.list_project_artifacts(self.run.run_id)}
        self.assertNotIn("QA_REPORT", artifact_types)
        self.assertNotIn("SECURITY_REPORT", artifact_types)

        controller = self.controller_after_restart(client)
        completed = await controller.resume(self.run.run_id, recover=True)

        self.assertEqual(completed.status, WorkflowStatus.FINISHED)
        self.assertEqual(completed.verdict, FinalVerdict.SUCCESS)
        self.assertEqual(set(client.polled), {step.a2a_task_id for step in validators})
        self.assertEqual(len(client.polled), 2)
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(len(client.handoffs), 2)
        reports = [artifact for artifact in self.repository.list_project_artifacts(self.run.run_id) if artifact.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}]
        self.assertEqual(len(reports), 2)
        self.assertEqual({report.a2a_task_id for report in reports}, set(client.polled))
        self.assertTrue(all(report.code_version == completed.code_version for report in reports))

    async def test_unsent_third_fix_resumes_once_with_original_issues_and_attempt(self):
        client = RecoverableClient(
            qa_outcome="FAIL", qa_version_outcomes={4: "PASS"},
            change_qa_issue_identity_by_version=True,
        )
        dispatcher = StopBeforeThirdFixSend(self.repository, self.registry, client_factory=lambda _: client)
        await dispatcher.dispatch_planner(self.run.run_id)
        interrupted = self.repository.get_run(self.run.run_id)
        self.assertEqual(interrupted.status, WorkflowStatus.FIXING)
        self.assertEqual(interrupted.fix_attempt, 3)
        self.assertEqual(interrupted.code_version, 3)
        pending = next(step for step in self.repository.list_steps(self.run.run_id) if step.agent_role == AgentRole.DEVELOPER and step.code_version == 4)
        self.assertEqual(pending.status, WorkflowStepStatus.RUNNING)
        self.assertIsNone(pending.a2a_task_id)
        original_issues = [issue for issue in self.repository.list_issue_records(self.run.run_id) if issue.code_version == 3 and issue.revalidation_result is None]
        self.assertTrue(original_issues)
        self.assertTrue(all(issue.fix_workflow_step_id == pending.workflow_step_id for issue in original_issues))
        events, _ = self.repository.list_events(self.run.run_id, limit=500, offset=0)
        self.assertFalse(any(event.event_type == "A2A_MESSAGE_SENT" and event.workflow_step_id == pending.workflow_step_id for event in events))

        controller = self.controller_after_restart(client)
        completed = await controller.resume(self.run.run_id, recover=True)

        self.assertEqual(completed.status, WorkflowStatus.FINISHED)
        self.assertEqual(completed.verdict, FinalVerdict.SUCCESS)
        self.assertEqual(completed.fix_attempt, 3)
        self.assertEqual(completed.code_version, 4)
        fixes = [entry for entry in client.sent if "fixRequest" in entry[0]]
        self.assertEqual([entry[0]["fixRequest"]["attempt"] for entry in fixes], [1, 2, 3])
        payload, metadata, context_id = fixes[-1]
        self.assertEqual(metadata.workflow_step_id, pending.workflow_step_id)
        self.assertEqual(metadata.code_version, 4)
        self.assertEqual(context_id, "developer-context-opaque")
        self.assertEqual({item["issueId"] for item in payload["fixRequest"]["issues"]}, {str(issue.issue_id) for issue in original_issues})
        self.assertTrue(all(item["codeVersion"] == 3 for item in payload["fixRequest"]["issues"]))
        self.assertEqual({item["artifactType"] for item in payload["fixRequest"]["inputArtifacts"]}, {"SOURCE", "CHANGE_REPORT", "BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"})
        developers = [step for step in self.repository.list_steps(self.run.run_id) if step.agent_role == AgentRole.DEVELOPER]
        self.assertEqual(len(developers), 4)
        events, _ = self.repository.list_events(self.run.run_id, limit=500, offset=0)
        self.assertEqual(len([event for event in events if event.event_type == "FIX_ATTEMPT_STARTED"]), 3)


if __name__ == "__main__":
    unittest.main()
