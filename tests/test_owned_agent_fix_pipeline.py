"""Actual owned executors and receipts across bounded code-fix cycles.

Providers, Docker, and the explicitly supplied semantic verifier are synthetic.
The verifier tests Source/receipt/anchor provenance, NOT signup correctness or
real semantic security proof. No product code or tests execute on the Host.
Official A2A HTTP uses ASGI here; MCP uses the actual Dispatcher adapter.
"""

import asyncio
from contextlib import asynccontextmanager
import json
import unittest
from uuid import UUID

import test_owned_agent_pipeline as pipeline_fixture
from agents.llm.contracts import ToolCall
from agents.runtime.security_services import SecurityRuntimeServices, SecuritySemanticProof
from orchestrator.domain import AgentRole, FinalVerdict, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.run_configuration import ExecutionLimits
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.validation_artifacts import ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult
from test_llm_runtime import FakeProvider, text_response, tool_response


class _FixHarness:
    """Reuse inert Stage 34 storage/transport fixtures, not its test methods."""

    def __init__(self, owner, *, qa_mode="initial-failure", build_failure=False,
                 build_failure_versions=(), proof=True, model_cap=40):
        self.base = pipeline_fixture.OwnedAgentPipelineTests()
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            owner.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()
        self.base.configuration = self.base.configuration.model_copy(update={
            "limits": ExecutionLimits(runtime_budget_ms=60000)})
        self.qa_mode, self.build_failure, self.proof = qa_mode, build_failure, proof
        self.build_failure_versions = frozenset(build_failure_versions) | ({1} if build_failure else set())
        self.model_cap = model_cap
        self.developer_version, self.qa_version = None, None
        self.fix_inputs, self.configurations, self.budgets, self.budget_samples = [], [], [], []
        self.versions = []
        original_services, original_peer = self.base.services, self.base.peer_client

        def services(role, execution):
            self.configurations.append(execution.configuration)
            self.budgets.append(execution.budget)
            self.budget_samples.append((execution.budget.deadline_monotonic,
                execution.budget.model_calls, execution.budget.tool_calls))
            self.versions.append((role, execution.metadata.code_version))
            if role is AgentRole.DEVELOPER:
                self.developer_version = execution.metadata.code_version
            elif role is AgentRole.QA:
                self.qa_version = execution.source.code_version
            service = original_services(role, execution)
            if role is not AgentRole.SECURITY or not self.proof:
                return service

            def synthetic_verifier(context, decision, measured):
                # This intentionally synthetic Host result tests the trusted
                # callback interface, not this fixture's product semantics.
                if (measured.source_artifact_id != context.source.artifact_id
                        or measured.snapshot_sha256 != context.source.snapshot_sha256
                        or b"inert-candidate-" not in measured.source_files["signup.py"]
                        or not all(review.references for review in decision.requirement_reviews)):
                    raise ValueError("synthetic fixture provenance mismatch")
                return SecuritySemanticProof(run_id=context.metadata.run_id,
                    workflow_step_id=context.metadata.workflow_step_id,
                    source_artifact_id=measured.source_artifact_id, snapshot_sha256=measured.snapshot_sha256,
                    requirement_outcomes=tuple((value, "PASS") for value in context.metadata.requirement_ids))

            return SecurityRuntimeServices(self.base.repository, self.base.registry, self.base.artifacts,
                mcp_configuration=service.configuration, client_factory=self.base.peer_client,
                proof_verifier=synthetic_verifier)

        @asynccontextmanager
        async def peer(configuration):
            async with original_peer(configuration) as client:
                role = configuration.binding.role
                docker = self.base.dockers[role][-1]
                if role is AgentRole.DEVELOPER and self.developer_version in self.build_failure_versions:
                    docker.exit_code = 1
                    docker.start_result = CLIResult(returncode=1, stdout=b"synthetic compilation failure\n", stderr=b"")
                elif role is AgentRole.QA:
                    version = self.base.repository.get_run(configuration.binding.run_id).code_version
                    cases = self.cases(version)
                    failure_index = self.failure_index(version)
                    failed = int(failure_index is not None)
                    docker.exit_code = failed
                    docker.start_result = CLIResult(returncode=failed, stdout=json.dumps({
                        "format": "unittest-v1", "total": len(cases), "passed": len(cases) - failed,
                        "failed": failed, "skipped": 0, "tests": [{"testId": case["testId"],
                            "outcome": "FAIL" if index == failure_index else "PASS"}
                            for index, case in enumerate(cases)]}).encode(), stderr=b"")
                yield client

        self.base.services, self.base.peer_client = services, peer

    def failure_index(self, version):
        if self.qa_mode == "pass" or self.qa_mode == "initial-failure" and version > 1:
            return None
        return version - 1 if self.qa_mode == "distinct-failures" else 0

    def cases(self, version):
        requirements = self.base.scenario.requirement_ids_for(RequirementValidator.QA)
        return [{"toolName": "run_unit_tests", "selector": "qa-unit",
            "testId": f"test_signup.Signup.test_req_{index}", "requirementId": str(requirement),
            "title": f"독립 QA 기준 {index}", "expectedResult": "동결 기준을 충족한다."}
            for index, requirement in enumerate(requirements, 1)]

    def providers(self):
        providers = self.base.providers()

        def developer_write(request):
            data = pipeline_fixture._task_input(request)
            if "fixRequest" in data:
                self.fix_inputs.append(data)
            version = self.developer_version
            return tool_response(ToolCall(call_id=f"developer-write-candidate-{version}", name="write_source_file",
                arguments_json=json.dumps({"workspaceId": data["workspaceId"], "path": "source/signup.py",
                    "content": f"def signup():\n    return 'inert-candidate-{version}'\n"})))

        developer_ready = lambda _request: text_response(json.dumps({
            "kind": "READY", "summary": "동결 기준에 대한 실제 변경 후보입니다.", "questions": []}, ensure_ascii=False))

        def qa_write(request):
            data = pipeline_fixture._task_input(request)
            return tool_response(ToolCall(call_id=f"qa-write-candidate-{self.qa_version}", name="write_test_file",
                arguments_json=json.dumps({"workspaceId": data["workspaceId"],
                    "path": "outputs/qa/tests/test_signup.py",
                    "content": f"# inert QA fixture for candidate {self.qa_version}; never executed on Host\n"})))

        qa_ready = lambda _request: text_response(json.dumps({"kind": "READY", "questions": [],
            "cases": self.cases(self.qa_version)}, ensure_ascii=False))

        def security_read(request):
            data = pipeline_fixture._task_input(request)
            return tool_response(ToolCall(call_id="security-source-read", name="read_project_file",
                arguments_json=json.dumps({"workspaceId": data["workspaceId"], "path": "source/signup.py"})))

        def security_ready(request):
            measured = pipeline_fixture._task_input(request)["measuredSecurity"]
            return text_response(json.dumps({"kind": "READY", "questions": [], "requirementReviews": [{
                "requirementId": str(value), "proposedOutcome": "PASS",
                "rationale": "실제 읽은 Source anchor를 명시하며 합성 Host 검증 결과를 별도 요구합니다.",
                "references": [{"path": "signup.py", "startLine": 1, "endLine": 1}]}
                for value in self.base.scenario.requirement_ids_for(RequirementValidator.SECURITY)],
                "findingReviews": [{"findingId": finding["findingId"], "proposedDisposition": "SUSPECTED",
                    "rationale": "독립 검증이 필요합니다.", "references": []}
                    for profile in measured["profiles"] for finding in profile["findings"]]}, ensure_ascii=False))

        providers[AgentRole.DEVELOPER] = FakeProvider(*[item for _cycle in range(4)
            for item in (developer_write, developer_ready)])
        providers[AgentRole.QA] = FakeProvider(*[item for _cycle in range(4) for item in (qa_write, qa_ready)])
        if self.proof:
            providers[AgentRole.SECURITY] = FakeProvider(*[item for _cycle in range(4)
                for item in (security_read, security_ready)])
        return providers

    @asynccontextmanager
    async def running(self):
        # These explicit Host limits are installed once, before Run admission.
        # The existing helper accepts a model cap; override the resulting
        # shared declaration only through the trusted composition call.
        original_create = pipeline_fixture.create_platform

        def create_with_fix_limits(**values):
            from agents.llm.budget import LLMLimits
            limits = LLMLimits(max_model_calls=self.model_cap, max_tool_calls=40,
                model_timeout_seconds=2, max_output_tokens=4096)
            values["limits"] = limits
            values["agent_settings"] = {role: settings.model_copy(update={"llm_limits": limits})
                for role, settings in values["agent_settings"].items()}
            return original_create(**values)

        from unittest.mock import patch
        with patch.object(pipeline_fixture, "create_platform", side_effect=create_with_fix_limits):
            async with self.base.running_platform(providers=self.providers(), model_cap=self.model_cap) as values:
                yield values


class OwnedAgentFixPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def submit(self, harness):
        async with harness.running() as (client, providers):
            response = await asyncio.wait_for(client.post("/api/v1/runs", json=harness.base.submission()), timeout=40)
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()["run"]["runId"])
            run = harness.base.repository.get_run(run_id)
            artifacts = harness.base.repository.list_project_artifacts(run_id)
            steps = harness.base.repository.list_steps(run_id)
            configuration = harness.base.repository.get_run_configuration(run_id)
            budget = harness.base.platform.budgets.resolve(configuration)
            self.assertTrue(harness.budgets)
            self.assertTrue(all(value is budget for value in harness.budgets))
            self.assertTrue(all(value == configuration for value in harness.configurations))
            self.assertEqual(len({sample[0] for sample in harness.budget_samples}), 1)
            self.assertEqual(budget.model_calls, sum(len(provider.requests) for provider in providers.values()))
            return run, artifacts, steps, budget.model_calls

    def assert_lineage(self, artifacts, kind, versions):
        lineage = sorted([item for item in artifacts if item.artifact_type == kind], key=lambda item: item.artifact_version)
        self.assertEqual([item.artifact_version for item in lineage], list(range(1, versions + 1)))
        self.assertEqual([item.code_version for item in lineage], list(range(1, versions + 1)))
        self.assertIsNone(lineage[0].previous_artifact_id)
        for previous, current in zip(lineage, lineage[1:]):
            self.assertEqual(current.previous_artifact_id, previous.artifact_id)
            self.assertNotEqual(current.artifact_id, previous.artifact_id)
        return lineage

    async def test_qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget(self):
        harness = _FixHarness(self)
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
            (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 1, 2))
        self.assertEqual((len(steps), calls), (7, 13))
        self.assertTrue(all(step.status is WorkflowStepStatus.SUCCEEDED for step in steps))
        sources = self.assert_lineage(artifacts, "SOURCE", 2)
        for kind in ("BUILD_REPORT", "CHANGE_REPORT", "QA_REPORT", "SECURITY_REPORT"):
            reports = self.assert_lineage(artifacts, kind, 2)
            if kind != "CHANGE_REPORT":
                for report, source in zip(reports, sources):
                    self.assertEqual(report.execution_manifest, source.execution_manifest())
        qa = sorted([item for item in artifacts if item.artifact_type == "QA_REPORT"], key=lambda item: item.code_version)
        self.assertTrue(any(test.outcome is ValidationOutcome.FAIL for test in qa[0].tests))
        self.assertTrue(qa[1].passed)
        self.assertEqual(len(harness.fix_inputs), 1)
        fix = harness.fix_inputs[0]["fixRequest"]
        issue = harness.base.repository.list_issue_records(run.run_id)[0]
        self.assertEqual(fix["attempt"], 1)
        self.assertEqual(len(fix["issues"]), 1)
        self.assertEqual(fix["issues"][0]["issueId"], str(issue.issue_id))
        self.assertEqual(fix["issues"][0]["sourceArtifactId"], str(sources[0].artifact_id))
        self.assertEqual(fix["issues"][0]["reportArtifactId"], str(qa[0].artifact_id))
        self.assertEqual(fix["issues"][0]["requirementIds"], [str(value) for value in issue.requirement_ids])
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            role_steps = [step for step in steps if step.agent_role is role]
            self.assertEqual(len({step.agent_context_id for step in role_steps}), 1)
            self.assertEqual(len({step.a2a_task_id for step in role_steps}), 2)
            self.assertTrue(all(step.agent_context_id and step.a2a_task_id for step in role_steps))

    async def test_three_distinct_failed_fix_cycles_stop_at_four_candidates(self):
        harness = _FixHarness(self, qa_mode="distinct-failures")
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
            (WorkflowStatus.FINISHED, FinalVerdict.FAIL, 3, 4))
        self.assertEqual((len(steps), calls, len(harness.fix_inputs)), (13, 25, 3))
        self.assert_lineage(artifacts, "SOURCE", 4)
        issues = harness.base.repository.list_issue_records(run.run_id)
        self.assertEqual(len(issues), 4)
        self.assertEqual(len({issue.fingerprint for issue in issues}), 4)
        self.assertTrue(all(issue.consecutive_repeat_count == 0 for issue in issues))

    async def test_repeated_issue_stops_after_two_unsolved_fix_cycles(self):
        harness = _FixHarness(self, qa_mode="repeated-failure")
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
            (WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW, 2, 3))
        self.assertEqual((len(steps), calls, len(harness.fix_inputs)), (10, 19, 2))
        self.assert_lineage(artifacts, "SOURCE", 3)
        issues = sorted(harness.base.repository.list_issue_records(run.run_id), key=lambda issue: issue.code_version)
        self.assertEqual([issue.consecutive_repeat_count for issue in issues], [0, 1, 2])
        self.assertEqual(len({issue.fingerprint for issue in issues}), 1)
        events, _total = harness.base.repository.list_events(run.run_id, limit=500, offset=0)
        self.assertTrue(any(event.event_type == "SAME_ISSUE_REQUIRES_REVIEW" for event in events))

    async def test_initial_build_failure_is_fixed_then_missing_security_proof_stays_unverified(self):
        harness = _FixHarness(self, qa_mode="pass", build_failure=True, proof=False)
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.fix_attempt, run.code_version), (WorkflowStatus.HUMAN_REVIEW, 1, 2))
        self.assertNotEqual(run.verdict, FinalVerdict.SUCCESS)
        self.assertEqual((len(steps), calls, len(harness.fix_inputs)), (5, 8, 1))
        sources = self.assert_lineage(artifacts, "SOURCE", 2)
        build = self.assert_lineage(artifacts, "BUILD_REPORT", 2)
        self.assertFalse(build[0].passed)
        self.assertTrue(build[1].passed)
        for report in (item for item in artifacts if item.artifact_type in {"QA_REPORT", "SECURITY_REPORT"}):
            self.assertEqual(report.code_version, 2)
            self.assertEqual(report.execution_manifest, sources[1].execution_manifest())
        security = next(item for item in artifacts if item.artifact_type == "SECURITY_REPORT")
        self.assertTrue(security.has_unverified)

    async def test_fix_cannot_reset_budget_after_initial_agents_spend_call_cap(self):
        # Initial Planner(1), Developer(2), QA(2), Security(2) consume all seven
        # calls. The admitted fix must resolve that exact already-spent budget.
        harness = _FixHarness(self, model_cap=7)
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.fix_attempt, run.code_version),
            (WorkflowStatus.HUMAN_REVIEW, 1, 1))
        self.assertNotEqual(run.verdict, FinalVerdict.SUCCESS)
        self.assertEqual((len(steps), calls), (5, 7))
        self.assertIn((AgentRole.DEVELOPER, 2), harness.versions)
        self.assertFalse(harness.fix_inputs)
        self.assertEqual(len([item for item in artifacts if item.artifact_type == "SOURCE"]), 1)
        developer_steps = [step for step in steps if step.agent_role is AgentRole.DEVELOPER]
        self.assertIs(developer_steps[0].status, WorkflowStepStatus.SUCCEEDED)
        self.assertIs(developer_steps[1].status, WorkflowStepStatus.FAILED)
        self.assertFalse(developer_steps[1].output_artifact_ids)
        self.assertEqual(len(harness.base.repository.list_issue_records(run.run_id)), 1)

    async def test_build_failure_gap_keeps_report_lineage_distinct_from_code_version(self):
        # QA/Security inspect code1 and code3 only: code2 has a measured Build
        # failure. Their second reports are version2, not codeVersion3.
        harness = _FixHarness(self, build_failure_versions=(2,))
        run, artifacts, steps, calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
            (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 2, 3))
        self.assertEqual((len(steps), calls, len(harness.fix_inputs)), (8, 15, 2))
        sources = self.assert_lineage(artifacts, "SOURCE", 3)
        for kind in ("BUILD_REPORT", "CHANGE_REPORT"):
            self.assert_lineage(artifacts, kind, 3)
        for kind in ("QA_REPORT", "SECURITY_REPORT"):
            reports = sorted([item for item in artifacts if item.artifact_type == kind], key=lambda item: item.artifact_version)
            self.assertEqual([item.artifact_version for item in reports], [1, 2])
            self.assertEqual([item.code_version for item in reports], [1, 3])
            self.assertEqual(reports[1].previous_artifact_id, reports[0].artifact_id)
            self.assertEqual(reports[0].execution_manifest, sources[0].execution_manifest())
            self.assertEqual(reports[1].execution_manifest, sources[2].execution_manifest())
        builds = sorted([item for item in artifacts if item.artifact_type == "BUILD_REPORT"], key=lambda item: item.code_version)
        self.assertEqual([item.passed for item in builds], [True, False, True])
        self.assertFalse([step for step in steps if step.agent_role in {AgentRole.QA, AgentRole.SECURITY}
                          and step.code_version == 2])
