"""Long Trace histories and duplicate Findings preserve Workflow policy."""

import unittest
from unittest.mock import patch

from orchestrator.domain import FinalVerdict, SCENARIO_REGISTRY, TraceEvent, WorkflowStatus
from tests import test_dispatch as fixtures


class WorkflowContractRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = fixtures.PlannerDispatchTests(
            "test_valid_planner_plan_creates_and_dispatches_developer_step"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.repository, self.run = self.fixture.repository, self.fixture.run

    def add_events(self, count):
        self.repository.save_run_update(self.run, [
            TraceEvent(run_id=self.run.run_id, event_type="A2A_TASK_UPDATED",
                       actor="Orchestrator", attempt=0)
            for _ in range(count)
        ])

    async def pipeline(self, client=None):
        dispatcher = self.fixture.dispatcher_for(
            client or fixtures.FakePlannerClient("TASK_STATE_COMPLETED")
        )
        await dispatcher.dispatch_planner(self.run.run_id)
        return dispatcher, self.repository.get_run(self.run.run_id)

    async def test_trace_beyond_one_page_does_not_change_success(self):
        self.add_events(1000)
        dispatcher, run = await self.pipeline()
        self.assertEqual((run.status, run.verdict),
                         (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))
        self.assertTrue(dispatcher._trace_satisfies_success_contract(
            run, SCENARIO_REGISTRY[run.scenario_id]))

    async def test_trace_beyond_multiple_pages_does_not_change_success(self):
        self.add_events(2500)
        with patch.object(self.repository, "list_events", wraps=self.repository.list_events) as pages:
            _, run = await self.pipeline()
        self.assertEqual(run.verdict, FinalVerdict.SUCCESS)
        self.assertTrue({0, 1000, 2000}.issubset({
            call.kwargs["offset"] for call in pages.call_args_list
        }))

    async def test_missing_lineage_after_first_page_still_blocks_success(self):
        self.add_events(1000)
        read = self.repository.list_events

        def without_build(*args, **kwargs):
            events, total = read(*args, **kwargs)
            # Keep pagination positions intact but remove the required proof.
            return [event.model_copy(update={"event_type": "A2A_TASK_UPDATED"})
                    if event.event_type == "BUILD_PASSED" else event for event in events], total

        with patch.object(self.repository, "list_events", side_effect=without_build):
            _, run = await self.pipeline()
        self.assertEqual(run.verdict, FinalVerdict.HUMAN_REVIEW)

    async def test_truncated_trace_page_is_not_success(self):
        self.add_events(1000)
        read = self.repository.list_events

        def truncated(*args, **kwargs):
            events, total = read(*args, **kwargs)
            return ([], total) if kwargs["offset"] else (events, total)

        with patch.object(self.repository, "list_events", side_effect=truncated):
            _, run = await self.pipeline()
        self.assertEqual(run.verdict, FinalVerdict.HUMAN_REVIEW)

    async def test_fix_trace_beyond_multiple_pages_preserves_success(self):
        self.add_events(2000)
        _, run = await self.pipeline(fixtures.FakePlannerClient(
            "TASK_STATE_COMPLETED", qa_outcome="FAIL", qa_outcome_after_fix="PASS"))
        self.assertEqual((run.verdict, run.fix_attempt, run.code_version),
                         (FinalVerdict.SUCCESS, 1, 2))

    async def test_duplicate_findings_stop_after_two_failed_fix_cycles(self):
        findings = [self.finding(f"FIND-{index}") for index in range(2)]
        _, run = await self.pipeline(fixtures.FakePlannerClient(
            "TASK_STATE_COMPLETED", security_findings=findings))
        self.assertEqual((run.status, run.verdict, run.fix_attempt),
                         (WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW, 2))
        issues = self.repository.list_issue_records(run.run_id)
        self.assertEqual(len(issues), 6)  # Distinct Finding records are retained.
        for version, count in ((1, 0), (2, 1), (3, 2)):
            self.assertEqual([issue.consecutive_repeat_count for issue in issues
                              if issue.code_version == version], [count, count])

    async def test_many_duplicate_findings_do_not_inflate_repeat_count(self):
        findings = [self.finding(f"FIND-{index}") for index in range(5)]
        _, run = await self.pipeline(fixtures.FakePlannerClient(
            "TASK_STATE_COMPLETED", security_findings=findings))
        self.assertEqual(run.fix_attempt, 2)
        for version, count in ((1, 0), (2, 1), (3, 2)):
            issues = [issue for issue in self.repository.list_issue_records(run.run_id)
                      if issue.code_version == version]
            self.assertEqual(len(issues), 5)
            self.assertEqual({issue.consecutive_repeat_count for issue in issues}, {count})

    async def test_same_fingerprint_regression_after_pass_restarts_count(self):
        class RegressionClient(fixtures.FakePlannerClient):
            async def send_snapshot_handoff(client, handoff, recipient, request_text, **kwargs):
                if recipient == fixtures.AgentRole.SECURITY:
                    version = handoff.execution_manifest.code_version
                    client.security_findings = [] if version == 2 else [
                        WorkflowContractRegressionTests.finding("FIND-0")
                    ]
                return await super().send_snapshot_handoff(
                    handoff, recipient, request_text, **kwargs)

        # Independent QA defects keep the pipeline moving during the version
        # where this Security fingerprint is resolved, then it regresses.
        client = RegressionClient("TASK_STATE_COMPLETED", qa_outcome="FAIL",
                                  change_qa_issue_identity_by_version=True)
        _, run = await self.pipeline(client)
        issues = [issue for issue in self.repository.list_issue_records(run.run_id)
                  if issue.reporter == fixtures.AgentRole.SECURITY]
        self.assertEqual([(issue.code_version, issue.consecutive_repeat_count)
                          for issue in issues], [(1, 0), (3, 0), (4, 1)])
        self.assertEqual((run.verdict, run.fix_attempt), (FinalVerdict.FAIL, 3))

    @staticmethod
    def finding(identity):
        return {"findingId": identity, "severity": "HIGH", "disposition": "CONFIRMED",
                "title": "Repeated finding", "description": "Confirmed evidence",
                "ruleId": "shared-rule", "normalizedLocation": "source/auth.py:8"}


if __name__ == "__main__":
    unittest.main()
