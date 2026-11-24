"""Issue resolution requires actual same-snapshot QA/Security execution proof."""

import tempfile
import unittest
from pathlib import Path

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict

from orchestrator.a2a import A2AAgentRegistry
from orchestrator.application import PlannerRunDispatcher
from orchestrator.domain import (
    AgentRole,
    FinalVerdict,
    QAReportArtifact,
    SCN_001_ID,
    SecurityReportArtifact,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)
from orchestrator.domain.tool_evidence import ToolExecutionOutcome
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _record_revalidation_results
from tests.test_dispatch import FakePlannerClient


class _RequirementRevalidationClient(FakePlannerClient):
    def __init__(self, role, *, missing_proof=False, after_fix="PASS"):
        super().__init__(
            "TASK_STATE_COMPLETED",
            qa_outcome="FAIL" if role == AgentRole.QA else "PASS",
            qa_outcome_after_fix=after_fix,
            security_outcome="FAIL" if role == AgentRole.SECURITY else "PASS",
        )
        self.role = role
        self.missing_proof = missing_proof
        self.after_fix = after_fix

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        is_fix = handoff.execution_manifest.code_version > 1
        if is_fix and self.role == AgentRole.SECURITY:
            self.security_outcome = self.after_fix
        task = await super().send_snapshot_handoff(handoff, recipient, request_text, **kwargs)
        if is_fix and recipient == self.role and self.missing_proof:
            payload = MessageToDict(task)
            for artifact in payload["artifacts"]:
                report = artifact["parts"][0]["data"]
                results = report.get("tests", report.get("requirementResults", []))
                for result in results:
                    result.pop("toolEvidence", None)
            task = ParseDict(payload, Task())
        return task


class _FindingRevalidationClient(FakePlannerClient):
    def __init__(self, after_fix, *, missing_proof=False):
        super().__init__("TASK_STATE_COMPLETED")
        self.after_fix = after_fix
        self.missing_proof = missing_proof

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        if recipient == AgentRole.SECURITY:
            version = handoff.execution_manifest.code_version
            self.security_findings = [{
                "findingId": f"scan-{version}-finding",
                "ruleId": "AUTH-BYPASS",
                "normalizedLocation": "src/signup.py:20",
                "requirementId": str(kwargs["requirement_ids"][0]),
                "severity": "HIGH",
                "disposition": "CONFIRMED" if version == 1 else self.after_fix,
                "title": "Access-control finding",
                "description": "Stable rule and location across scan reports.",
            }]
        task = await super().send_snapshot_handoff(handoff, recipient, request_text, **kwargs)
        if recipient == AgentRole.SECURITY and handoff.execution_manifest.code_version > 1 and self.missing_proof:
            payload = MessageToDict(task)
            for artifact in payload["artifacts"]:
                for result in artifact["parts"][0]["data"]["requirementResults"]:
                    result.pop("toolEvidence", None)
            task = ParseDict(payload, Task())
        return task


class IssueRevalidationEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(Path(self.temp_dir.name) / "issues.sqlite3")

    def tearDown(self):
        self.temp_dir.cleanup()

    async def run_pipeline(self, client):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 기능 구현")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(run, (step,), ())
        dispatcher = PlannerRunDispatcher(
            self.repository,
            A2AAgentRegistry({role: f"http://{role.value.lower()}.test" for role in AgentRole}),
            client_factory=lambda url: client,
        )
        await dispatcher.dispatch_planner(run.run_id)
        return self.repository.get_run(run.run_id)

    def original_issues(self, run, role):
        issues = [issue for issue in self.repository.list_issue_records(run.run_id)
                  if issue.code_version == 1 and issue.reporter == role]
        self.assertTrue(issues)
        return issues

    async def assert_missing_proof_is_unverified(self, role, after_fix):
        run = await self.run_pipeline(_RequirementRevalidationClient(
            role, missing_proof=True, after_fix=after_fix,
        ))
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.verdict, FinalVerdict.HUMAN_REVIEW)
        issues = self.original_issues(run, role)
        self.assertEqual({issue.revalidation_result for issue in issues}, {"UNVERIFIED"})

    async def test_qa_pass_without_execution_proof_does_not_resolve_issues(self):
        await self.assert_missing_proof_is_unverified(AgentRole.QA, "PASS")

    async def test_security_pass_without_execution_proof_does_not_resolve_issues(self):
        await self.assert_missing_proof_is_unverified(AgentRole.SECURITY, "PASS")

    async def test_qa_fail_without_execution_proof_is_not_a_verified_recurrence(self):
        await self.assert_missing_proof_is_unverified(AgentRole.QA, "FAIL")

    async def test_security_fail_without_execution_proof_is_not_a_verified_recurrence(self):
        await self.assert_missing_proof_is_unverified(AgentRole.SECURITY, "FAIL")

    async def test_actual_qa_and_security_fixes_remain_pass(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                run = await self.run_pipeline(_RequirementRevalidationClient(role))
                self.assertEqual(run.status, WorkflowStatus.FINISHED)
                self.assertEqual(run.verdict, FinalVerdict.SUCCESS)
                self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, role)}, {"PASS"})

    async def test_actual_qa_and_security_failures_remain_fail(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                run = await self.run_pipeline(_RequirementRevalidationClient(role, after_fix="FAIL"))
                self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
                self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, role)}, {"FAIL"})

    async def test_suspected_and_unverified_findings_are_not_resolved(self):
        for disposition in ("SUSPECTED", "UNVERIFIED"):
            with self.subTest(disposition=disposition):
                run = await self.run_pipeline(_FindingRevalidationClient(disposition))
                self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
                self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, AgentRole.SECURITY)}, {"UNVERIFIED"})

    async def test_explicit_false_positive_with_actual_scan_resolves_finding(self):
        run = await self.run_pipeline(_FindingRevalidationClient("FALSE_POSITIVE"))
        self.assertEqual(run.status, WorkflowStatus.FINISHED)
        self.assertEqual(run.verdict, FinalVerdict.SUCCESS)
        self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, AgentRole.SECURITY)}, {"PASS"})

    async def test_actual_confirmed_finding_recurrence_remains_fail(self):
        run = await self.run_pipeline(_FindingRevalidationClient("CONFIRMED"))
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, AgentRole.SECURITY)}, {"FAIL"})

    async def test_finding_without_scan_proof_is_not_resolved_or_confirmed(self):
        for disposition in ("CONFIRMED", "FALSE_POSITIVE"):
            with self.subTest(disposition=disposition):
                run = await self.run_pipeline(_FindingRevalidationClient(disposition, missing_proof=True))
                self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
                self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, AgentRole.SECURITY)}, {"UNVERIFIED"})

    async def test_storage_boundary_checks_tool_manifest_and_execution_outcome(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            for invalid_field in ("tool", "manifest", "outcome"):
                with self.subTest(role=role, invalid_field=invalid_field):
                    run = await self.run_pipeline(_RequirementRevalidationClient(role))
                    artifacts = self.repository.list_project_artifacts(run.run_id)
                    qa = next(artifact for artifact in artifacts
                              if isinstance(artifact, QAReportArtifact) and artifact.code_version == 2)
                    security = next(artifact for artifact in artifacts
                                    if isinstance(artifact, SecurityReportArtifact) and artifact.code_version == 2)
                    report = qa if role == AgentRole.QA else security
                    results = report.tests if role == AgentRole.QA else report.requirement_results
                    changed_results = []
                    for result in results:
                        evidence = result.tool_evidence
                        if invalid_field == "tool":
                            evidence = evidence.model_copy(update={"tool_name": "read_project_file"})
                        elif invalid_field == "manifest":
                            manifest = evidence.execution_manifest.model_copy(update={"snapshot_sha256": "f" * 64})
                            evidence = evidence.model_copy(update={"execution_manifest": manifest})
                        else:
                            attempt = evidence.attempts[-1].model_copy(update={"outcome": ToolExecutionOutcome.FAIL})
                            evidence = evidence.model_copy(update={"attempts": (attempt,)})
                        changed_results.append(result.model_copy(update={"tool_evidence": evidence}))
                    # model_copy deliberately bypasses report validators to exercise
                    # the persistence boundary's independent proof checks.
                    if role == AgentRole.QA:
                        qa = qa.model_copy(update={"tests": tuple(changed_results)})
                    else:
                        security = security.model_copy(update={"requirement_results": tuple(changed_results)})
                    with self.repository._transaction() as connection:
                        _record_revalidation_results(connection, run, qa, security)
                    self.assertEqual({issue.revalidation_result for issue in self.original_issues(run, role)}, {"UNVERIFIED"})


if __name__ == "__main__":
    unittest.main()
