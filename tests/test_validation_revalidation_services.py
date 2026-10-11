"""Actual revalidation Tool/receipt assembly with Git and fake Docker only.

The approved inert Git fixtures are committed but never executed on the Host.
Source revisions here are provenance fixtures, not a claimed product repair.
"""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from agents.runtime.qa_services import QAServicesError
from agents.runtime.security_services import SecurityServicesError
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.snapshots import FrozenSourceSelection
from agents.roles.qa_contract import QACaseBinding, QADecision
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
import test_qa_services as qa_fixture
import test_security_agent_services as security_fixture


class ValidationRevalidationServicesTests(unittest.IsolatedAsyncioTestCase):
    def borrow(self, kind):
        case = (qa_fixture.QAServicesTests("test_constructor_inert_and_private_repr") if kind is AgentRole.QA
                else security_fixture.SecurityAgentServicesTests("test_constructor_is_inert_and_private_repr"))
        case.setUp()
        for cleanup, args, kwargs in case._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        case._cleanups.clear()
        return case

    @staticmethod
    def insert_step(repository, step):
        with repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (str(step.workflow_step_id), str(step.run_id), step.status.value,
                 step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))

    @staticmethod
    def update_step(repository, step):
        with repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    @staticmethod
    def insert_artifact(repository, artifact):
        with repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)", (str(artifact.artifact_id),
                str(artifact.run_id), artifact.artifact_type, artifact.artifact_version, artifact.model_dump_json()))

    def advance(self, case, role, previous_report=None):
        previous_source = case.source
        old = case.step
        if previous_report is None:
            self.update_step(case.repository, old.model_copy(update={"status": WorkflowStepStatus.CANCELED}))
        else:
            self.insert_artifact(case.repository, previous_report)
            self.update_step(case.repository, old.model_copy(update={"status": WorkflowStepStatus.SUCCEEDED,
                "a2a_task_state": A2ATaskState.COMPLETED, "a2a_task_id": previous_report.a2a_task_id,
                "agent_context_id": f"{role.value}-context", "a2a_artifact_ids": [previous_report.a2a_artifact_id],
                "output_artifact_ids": [previous_report.artifact_id]}))
        run = case.repository.get_run(case.metadata.run_id).model_copy(update={"status": WorkflowStatus.FIXING,
            "fix_attempt": previous_source.code_version})
        with case.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (run.status.value, run.model_dump_json(), str(run.run_id)))
        developer = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.RUNNING, code_version=previous_source.code_version + 1,
            requirement_ids=list(previous_source.requirement_ids), input_artifact_ids=[previous_source.artifact_id])
        self.insert_step(case.repository, developer)
        git_fixture = getattr(case.fixture, "fixture", case.fixture)
        directory = case.fixture.root / "source"
        git_fixture.git(directory, "commit", "--allow-empty", "-m", "approved inert revalidation fixture")
        commit = git_fixture.git(directory, "rev-parse", "HEAD").strip()
        frozen = case.artifacts.bind(run.run_id, role=AgentRole.DEVELOPER).freeze_source(
            workflow_step_id=developer.workflow_step_id, commit_hash=commit,
            repository_id=previous_source.repository_id, lock_path="requirements.lock")
        case.source = frozen.model_copy(update={"a2a_task_id": f"developer-task-{frozen.code_version}",
            "a2a_artifact_id": f"developer-source-{frozen.code_version}"})
        self.insert_artifact(case.repository, case.source)
        self.update_step(case.repository, developer.model_copy(update={"status": WorkflowStepStatus.SUCCEEDED,
            "a2a_task_state": A2ATaskState.COMPLETED, "a2a_task_id": case.source.a2a_task_id,
            "a2a_artifact_ids": [case.source.a2a_artifact_id], "output_artifact_ids": [case.source.artifact_id]}))
        run = run.model_copy(update={"status": WorkflowStatus.REVALIDATING, "code_version": case.source.code_version})
        with case.repository._transaction() as connection:
            connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                (run.status.value, run.model_dump_json(), str(run.run_id)))
        case.step = WorkflowStep(run_id=run.run_id, agent_role=role, status=WorkflowStepStatus.RUNNING,
            attempt=6, code_version=case.source.code_version, requirement_ids=list(case.ids),
            input_artifact_ids=[case.source.artifact_id])
        self.insert_step(case.repository, case.step)
        case.metadata = A2AWorkflowMetadata(run_id=run.run_id, scenario_id=run.scenario_id,
            workflow_step_id=case.step.workflow_step_id, attempt=case.step.attempt,
            code_version=case.source.code_version, requirement_ids=case.ids,
            project_artifact_ids=(case.source.artifact_id,))
        case.execution.metadata, case.execution.source = case.metadata, case.source
        case.execution.fix_attempt, case.execution.previous_source = run.fix_attempt, previous_source
        case.execution.previous_report = previous_report
        case.configuration = replace(case.configuration, frozen_source=FrozenSourceSelection(
            project_artifact_id=case.source.artifact_id, snapshot_sha256=case.source.snapshot_sha256))
        return run

    async def qa_report(self, case, services=None, tracked=None):
        services = services or case.services()
        await services.prepare(case.execution)
        tracked = tracked or case.tracked(services)
        artifacts = await services.finalize(case.execution, case.decision, tracked,
            task_id=f"qa-task-{case.source.code_version}", context_id="QA-context")
        return case.report(artifacts)

    async def security_report(self, case, services=None, tracked=None, measured=None):
        services = services or case.services()
        await services.prepare(case.execution)
        tracked = tracked or case.tracked(services)
        measured = measured or await services.scan(case.execution, tracked)
        from google.protobuf.json_format import MessageToDict
        from orchestrator.domain.validation_artifacts import SecurityReportArtifact
        artifacts = await services.finalize(case.execution, case.decision(measured), tracked, measured,
            task_id=f"security-task-{case.source.code_version}", context_id="SECURITY-context")
        return SecurityReportArtifact.model_validate(MessageToDict(artifacts[0].parts[0].data))

    async def test_actual_unit_revalidation_preserves_report_lineage_and_budget(self):
        case = self.borrow(AgentRole.QA)
        initial = await self.qa_report(case)
        deadline = case.execution.budget.deadline_monotonic
        self.advance(case, AgentRole.QA, initial)
        report = await self.qa_report(case)
        self.assertEqual((report.code_version, report.artifact_version), (2, 2))
        self.assertEqual(report.previous_artifact_id, initial.artifact_id)
        self.assertEqual(report.execution_manifest, case.source.execution_manifest())
        self.assertTrue(report.passed)
        self.assertTrue(all(item.tool_evidence.execution_manifest.project_artifact_id == case.source.artifact_id
            for item in report.tests))
        self.assertEqual(case.execution.budget.tool_calls, 2)
        self.assertEqual(case.execution.budget.deadline_monotonic, deadline)
        self.assertIsNone(case.repository.get_run(case.metadata.run_id).verdict)

    async def test_first_unit_report_for_second_candidate_keeps_report_version_one(self):
        case = self.borrow(AgentRole.QA)
        self.advance(case, AgentRole.QA)
        report = await self.qa_report(case)
        self.assertEqual((report.code_version, report.artifact_version), (2, 1))
        self.assertIsNone(report.previous_artifact_id)

    async def test_all_three_actual_unit_revalidations_keep_one_budget_and_receipt_chain(self):
        case = self.borrow(AgentRole.QA)
        deadline = case.execution.budget.deadline_monotonic
        report = await self.qa_report(case)
        executions = {item.tool_evidence.execution_id for item in report.tests}
        for code_version in range(2, 5):
            previous = report
            self.advance(case, AgentRole.QA, previous)
            report = await self.qa_report(case)
            self.assertEqual((report.code_version, report.artifact_version), (code_version, code_version))
            self.assertEqual(report.previous_artifact_id, previous.artifact_id)
            self.assertEqual(report.execution_manifest, case.source.execution_manifest())
            current = {item.tool_evidence.execution_id for item in report.tests}
            self.assertTrue(current.isdisjoint(executions))
            executions.update(current)
        self.assertEqual(case.execution.budget.tool_calls, 4)
        self.assertEqual(case.execution.budget.deadline_monotonic, deadline)
        self.assertIsNone(case.repository.get_run(case.metadata.run_id).verdict)

    def configure_browser(self, case):
        suite = BrowserTestSuite(name="signup-browser", kind="QA_TESTS")
        configuration = BrowserTestConfiguration(suites=(suite,),
            service_argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"), playwright_version="1.60.0",
            limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5),
            image_reference=case.configuration.unit_test_configuration.image_reference)
        case.configuration = replace(case.configuration, unit_test_configuration=None,
                                     browser_test_configuration=configuration)
        cases = [{"testId": f"signup.{index}", "steps": [
            {"action": "goto", "path": "/"}, {"action": "assert_visible", "selector": "#signup"}]}
            for index in range(len(case.ids))]
        path = case.fixture.root / "outputs/qa/tests/browser/suite.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"format": "browser-suite-v1", "tests": cases}), encoding="utf-8")
        case.decision = QADecision(kind="READY", questions=(), cases=tuple(QACaseBinding(
            tool_name="run_browser_tests", selector=suite.name, test_id=actual["testId"],
            requirement_id=identity, title="브라우저 기준", expected_result="화면 기준 충족")
            for identity, actual in zip(case.ids, cases)))
        case.docker.exit_code = 0
        case.docker.start_result = CLIResult(returncode=0, stderr=b"", stdout=json.dumps({
            "format": "browser-v1", "suiteName": suite.name, "playwrightVersion": "1.60.0",
            "browserVersion": "145.0.7632.6", "total": len(cases), "passed": len(cases), "failed": 0,
            "tests": [{"testId": actual["testId"], "outcome": "PASS", "steps": [
                {"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 2},
                {"index": 2, "action": "assert_visible", "outcome": "PASS", "durationMs": 2}]}
                for actual in cases]}).encode())

    async def test_actual_browser_revalidation_binds_new_source_and_report_lineage(self):
        case = self.borrow(AgentRole.QA)
        self.configure_browser(case)
        initial = await self.qa_report(case)
        self.advance(case, AgentRole.QA, initial)
        report = await self.qa_report(case)
        self.assertEqual((report.code_version, report.artifact_version), (2, 2))
        self.assertEqual(report.previous_artifact_id, initial.artifact_id)
        self.assertEqual({item.tool_evidence.tool_name for item in report.tests}, {"run_browser_tests"})
        self.assertEqual({item.tool_evidence.execution_manifest.project_artifact_id for item in report.tests},
                         {case.source.artifact_id})

    async def test_stale_browser_receipt_from_prior_source_is_rejected(self):
        case = self.borrow(AgentRole.QA)
        self.configure_browser(case)
        services, observed = case.services(), []
        tracked = case.tracked(services)
        original = tracked.invoke
        async def capture(*arguments, **options):
            value = await original(*arguments, **options)
            observed.append(value)
            return value
        with patch.object(tracked, "invoke", side_effect=capture):
            initial = await self.qa_report(case, services, tracked)
        self.advance(case, AgentRole.QA, initial)
        current_services = case.services()
        current_tracked = case.tracked(current_services)
        with patch.object(current_tracked, "invoke", return_value=observed[0]):
            with self.assertRaises(QAServicesError) as caught:
                await self.qa_report(case, current_services, current_tracked)
        self.assertEqual(caught.exception.code, "QA_TEST_EVIDENCE_INVALID")

    async def test_stale_unit_receipt_from_previous_step_cannot_be_reused(self):
        case = self.borrow(AgentRole.QA)
        old_services = case.services()
        old_tracked = case.tracked(old_services)
        observed = []
        original = old_tracked.invoke
        async def capture(*arguments, **options):
            value = await original(*arguments, **options)
            observed.append(value)
            return value
        with patch.object(old_tracked, "invoke", side_effect=capture):
            initial = await self.qa_report(case, old_services, old_tracked)
        self.advance(case, AgentRole.QA, initial)
        current_services = case.services()
        current_tracked = case.tracked(current_services)
        with patch.object(current_tracked, "invoke", return_value=observed[0]):
            with self.assertRaises(QAServicesError) as caught:
                await self.qa_report(case, current_services, current_tracked)
        self.assertEqual(caught.exception.code, "QA_TEST_EVIDENCE_INVALID")

    async def test_old_unmapped_case_does_not_disappear_in_revalidation(self):
        case = self.borrow(AgentRole.QA)
        initial = await self.qa_report(case)
        self.advance(case, AgentRole.QA, initial)
        case.set_unit_report(extra=({"testId": "retained.old_case", "outcome": "FAIL"},))
        with self.assertRaises(QAServicesError) as caught:
            await self.qa_report(case)
        self.assertEqual(caught.exception.code, "QA_REPORT_BINDING_INVALID")

    async def test_default_security_revalidation_still_requires_semantic_proof(self):
        case = self.borrow(AgentRole.SECURITY)
        initial = await self.security_report(case)
        self.advance(case, AgentRole.SECURITY, initial)
        report = await self.security_report(case)
        self.assertEqual((report.code_version, report.artifact_version), (2, 2))
        self.assertEqual(report.previous_artifact_id, initial.artifact_id)
        self.assertEqual(report.execution_manifest, case.source.execution_manifest())
        self.assertTrue(all(item.outcome is ValidationOutcome.UNVERIFIED and item.tool_evidence is None
            for item in report.requirement_results))
        self.assertEqual(case.execution.budget.tool_calls, 2)

    async def test_first_security_report_for_second_candidate_keeps_report_version_one(self):
        case = self.borrow(AgentRole.SECURITY)
        self.advance(case, AgentRole.SECURITY)
        report = await self.security_report(case)
        self.assertEqual((report.code_version, report.artifact_version), (2, 1))
        self.assertIsNone(report.previous_artifact_id)
        self.assertTrue(report.has_unverified)

    async def test_old_scan_bundle_and_read_evidence_are_rejected_for_new_source(self):
        case = self.borrow(AgentRole.SECURITY)
        services = case.services()
        tracked = case.tracked(services)
        measured = await services.scan(case.execution, tracked)
        await case.read(services, tracked, measured)
        initial = await self.security_report(case, services, tracked, measured)
        self.advance(case, AgentRole.SECURITY, initial)
        current_services = case.services()
        current_tracked = case.tracked(current_services)
        with self.assertRaises(SecurityServicesError) as caught:
            await current_services.finalize(case.execution, case.decision(measured), current_tracked, measured,
                task_id="security-task-2", context_id="SECURITY-context")
        self.assertEqual(caught.exception.code, "SECURITY_SCAN_EVIDENCE_INVALID")

    async def test_stale_security_scan_receipt_is_rejected_for_new_step(self):
        case = self.borrow(AgentRole.SECURITY)
        services, observed = case.services(), []
        tracked = case.tracked(services)
        original = tracked.invoke
        async def capture(*arguments, **options):
            value = await original(*arguments, **options)
            observed.append(value)
            return value
        with patch.object(tracked, "invoke", side_effect=capture):
            initial = await self.security_report(case, services, tracked)
        self.advance(case, AgentRole.SECURITY, initial)
        current_services = case.services()
        current_tracked = case.tracked(current_services)
        with patch.object(current_tracked, "invoke", return_value=observed[0]):
            with self.assertRaises(SecurityServicesError) as caught:
                await current_services.scan(case.execution, current_tracked)
        self.assertEqual(caught.exception.code, "SECURITY_SCAN_EVIDENCE_INVALID")


if __name__ == "__main__":
    unittest.main()
