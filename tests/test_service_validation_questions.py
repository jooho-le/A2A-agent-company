"""Interactive validation pauses, independent of unattended experiment stops.

Real dispatcher decisions and SQLite state; stub A2A results, no model or Docker.
"""
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.application.a2a_tasks import A2ATaskRunResult, A2ATaskRunner, TaskRunDisposition
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.domain import (A2ATaskState, AgentContext, AgentRole, SCN_001_ID,
                                 WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus)
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID
from orchestrator.infrastructure import SQLiteWorkflowRepository


class ServiceValidationQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def dispatch_question(self, role, stage, *, scenario_id=SERVICE_WEB_ID, single=False):
        with TemporaryDirectory() as directory:
            repository = SQLiteWorkflowRepository(Path(directory) / "host.db")
            run = WorkflowRun(scenario_id=scenario_id, request_text="회원가입 기능을 만들어줘.",
                              status=stage, code_version=1)
            steps, outcomes = [], {}
            for agent in (AgentRole.QA, AgentRole.SECURITY):
                waiting = agent is role
                step = WorkflowStep(run_id=run.run_id, agent_role=agent, code_version=1,
                    status=WorkflowStepStatus.WAITING_INPUT if waiting else WorkflowStepStatus.SUCCEEDED,
                    a2a_task_id=f"task-{agent.value}", agent_context_id=f"context-{agent.value}",
                    a2a_task_state=A2ATaskState.INPUT_REQUIRED if waiting else A2ATaskState.COMPLETED)
                task = ParseDict({"id": step.a2a_task_id, "contextId": step.agent_context_id,
                    "status": {"state": step.a2a_task_state.value, "message": {"parts": [{"data": {
                        "code": f"{agent.value}_INPUT_REQUIRED", "questions": ["확정된 범위를 확인해주세요."]}}]}}}, Task())
                context = AgentContext(run_id=run.run_id, agent_id=agent.value,
                    agent_context_id=step.agent_context_id, latest_a2a_task_id=step.a2a_task_id)
                outcomes[agent] = A2ATaskRunResult(step, context, task,
                    TaskRunDisposition.WAITING_INPUT if waiting else TaskRunDisposition.COMPLETED, (), 0, 0)
                steps.append(step)
            repository.create_run(run, tuple(steps), ())

            @asynccontextmanager
            async def client(_url):
                yield SimpleNamespace(resolve_agent_card=AsyncMock())

            async def submit(_run, step, _handoff, **_kwargs):
                return outcomes[step.agent_role]

            dispatcher = PlannerRunDispatcher(repository, SimpleNamespace(), client_factory=client,
                                              single_agent=single, automatic_stop=True)
            with patch("orchestrator.application.dispatch.build_validation_request", return_value="check"), \
                 patch.object(A2ATaskRunner, "submit_snapshot_and_wait", side_effect=submit) as send, \
                 patch.object(dispatcher, "_finish_validation_stops", return_value=True) as finish, \
                 patch.object(dispatcher, "consume_validation_results", new_callable=AsyncMock) as consume:
                await dispatcher._dispatch_validation_agents(run, tuple(steps), None,
                    agent_urls={agent: "http://isolated-agent" for agent in outcomes}, plan=None, scenario=None)
                current = repository.get_run(run.run_id)
                if scenario_id == SERVICE_WEB_ID:
                    self.assertEqual(current.status, WorkflowStatus.HUMAN_REVIEW)
                    self.assertEqual(current.resume_state, stage)
                    self.assertIsNone(current.verdict)
                    finish.assert_not_called()
                    consume.assert_not_awaited()
                    stored = repository.list_steps(run.run_id)
                    self.assertEqual({s.a2a_task_id for s in stored}, {s.a2a_task_id for s in steps})
                    self.assertEqual(next(s for s in stored if s.agent_role is role).status,
                                     WorkflowStepStatus.WAITING_INPUT)
                    if not single:
                        self.assertEqual(next(s for s in stored if s.agent_role is not role).status,
                                         WorkflowStepStatus.SUCCEEDED)
                        self.assertEqual(send.await_count, 2)
                else:
                    finish.assert_called_once()

    async def test_service_question_keeps_validation_and_completed_peer_resumable(self):
        for stage in (WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING):
            for role in (AgentRole.QA, AgentRole.SECURITY):
                with self.subTest(stage=stage, role=role):
                    await self.dispatch_question(role, stage)

    async def test_single_service_question_also_pauses(self):
        await self.dispatch_question(AgentRole.QA, WorkflowStatus.VALIDATING, single=True)

    async def test_experiment_question_retains_its_automatic_stop_policy(self):
        await self.dispatch_question(AgentRole.QA, WorkflowStatus.VALIDATING, scenario_id=SCN_001_ID)
