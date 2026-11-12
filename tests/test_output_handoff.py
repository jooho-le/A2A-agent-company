"""Host admission before registration/dispatch; real SQLite, synthetic Tasks."""

import unittest
from uuid import uuid4

from orchestrator.domain.states import AgentRole, WorkflowStatus
import test_dispatch as fixtures


class _ContaminatedClient(fixtures.FakePlannerClient):
    def __init__(self, target):
        super().__init__("TASK_STATE_COMPLETED")
        self.target = target

    def contaminate(self, task, role):
        if role == self.target:
            extra = task.artifacts.add()
            extra.CopyFrom(task.artifacts[0])
            extra.name = "unapproved-other-role.json"
            extra.artifact_id = "extra/opaque?!%"
            extra.metadata.update({"projectArtifactId": str(uuid4())})
        return task

    async def send_task(self, payload, metadata, *, context_id=None):
        task = await super().send_task(payload, metadata, context_id=context_id)
        role = AgentRole.PLANNER if "request" in payload else AgentRole.DEVELOPER
        return self.contaminate(task, role)

    async def send_snapshot_handoff(self, handoff, role, request_text, **kwargs):
        task = await super().send_snapshot_handoff(handoff, role, request_text, **kwargs)
        return self.contaminate(task, role)


class OutputHandoffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.PlannerDispatchTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repository, self.run = self.fixture.repository, self.fixture.run

    async def dispatch(self, role):
        client = _ContaminatedClient(role)
        await self.fixture.dispatcher_for(client).dispatch_planner(self.run.run_id)
        run = self.repository.get_run(self.run.run_id)
        self.assertIs(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertNotEqual(getattr(run.verdict, "value", None), "SUCCESS")
        events, _ = self.repository.list_events(run.run_id, limit=500, offset=0)
        return client, [item.artifact_type for item in self.repository.list_project_artifacts(run.run_id)], {
            event.event_type for event in events}

    async def test_planner_extra_output_stops_before_developer_or_requirement_registration(self):
        client, artifacts, events = await self.dispatch(AgentRole.PLANNER)
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(client.handoffs, [])
        self.assertEqual(artifacts, [])
        self.assertIn("PLANNER_OUTPUT_REJECTED", events)

    async def test_developer_extra_output_stops_before_candidate_registration_or_validation(self):
        client, artifacts, events = await self.dispatch(AgentRole.DEVELOPER)
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(client.handoffs, [])
        self.assertEqual(artifacts, ["REQUIREMENT"])
        self.assertIn("DEVELOPER_OUTPUT_REJECTED", events)

    async def test_qa_extra_output_cannot_register_a_report_or_reach_success(self):
        _client, artifacts, events = await self.dispatch(AgentRole.QA)
        self.assertEqual(set(artifacts), {"REQUIREMENT", "SOURCE", "CHANGE_REPORT", "BUILD_REPORT"})
        self.assertIn("QA_SECURITY_OUTPUT_REJECTED", events)

    async def test_security_extra_output_cannot_register_a_report_or_reach_success(self):
        _client, artifacts, events = await self.dispatch(AgentRole.SECURITY)
        self.assertEqual(set(artifacts), {"REQUIREMENT", "SOURCE", "CHANGE_REPORT", "BUILD_REPORT"})
        self.assertIn("QA_SECURITY_OUTPUT_REJECTED", events)
