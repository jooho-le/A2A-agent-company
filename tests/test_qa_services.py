"""QA services on real Git/SQLite/MCP boundaries with a fake Docker daemon.

No generated product or test code is executed on the Host. These tests do not
claim real browser/container coverage; they exercise private receipt trust.
"""

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from google.protobuf.json_format import MessageToDict

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMRuntimeError
from agents.roles.qa_contract import QACaseBinding, QADecision
from agents.runtime.qa_services import QARuntimeServices, QAServicesError
from mcp_tools.client import BoundMCPClient, MCPChildConfiguration
from mcp_tools.execution_runtime import TrackedMCPError
from mcp_tools.runtime import MCPDispatcher
from mcp_tools.tools.browser import BrowserTestTools
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.browser_store import BrowserTestOutputStore
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.unit import UnitTestTools
from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.scenario_registry import SCENARIO_REGISTRY, RequirementValidator
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import QAReportArtifact, ValidationOutcome
from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
from orchestrator.sandbox.runtime import SandboxRuntime
from test_mcp_execution_runtime import DispatcherSession
import test_mcp_unit_store as store_fixture
from test_sandbox_runtime import FakeDocker, IMAGE_ID


class QAServicesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = store_fixture.UnitTestOutputStoreTests("test_constructor_is_inert")
        original_configuration = store_fixture.RunConfiguration
        if "protected" in self._testMethodName:
            self.fixture.setUp()
        else:
            # Generated-only runs explicitly freeze no protected-suite
            # obligation at creation; never alter a frozen baseline later.
            def no_protected_configuration(**values):
                return original_configuration(**{**values, "protected_test_suite_ref": None})
            with patch.object(store_fixture, "RunConfiguration", side_effect=no_protected_configuration):
                self.fixture.setUp()
        for cleanup, args, kwargs in self.fixture._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.fixture._cleanups.clear()
        self.step = self.fixture.qa_context()
        self.repository, self.registry = self.fixture.repository, self.fixture.registry
        self.artifacts = ArtifactStore(self.repository, self.registry)
        scenario = SCENARIO_REGISTRY[self.fixture.run.scenario_id]
        self.ids = scenario.requirement_ids_for(RequirementValidator.QA)
        self.step = WorkflowStep.model_validate({**self.step.model_dump(), "requirement_ids": list(self.ids)})
        self.fixture.mutate_step(self.step)
        self.source = type(self.fixture.source).model_validate({**self.fixture.source.model_dump(),
            "a2a_task_id": "developer-task", "a2a_artifact_id": "developer-source-artifact"})
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)", (
                str(self.source.artifact_id), str(self.source.run_id), "SOURCE", 1, self.source.model_dump_json()))
        tests = self.fixture.root / "outputs/qa/tests"
        tests.mkdir(parents=True, exist_ok=True)
        self.test_file = tests / "test_signup.py"
        self.test_file.write_text("# inert generated QA fixture; never execute on Host\n", encoding="utf-8")
        self.docker = FakeDocker()
        self.sandbox = SandboxRuntime(self.repository, self.registry, self.artifacts, docker=self.docker)
        self.unit_configuration = UnitTestConfiguration(scopes=(self.fixture.scope,),
            limits=SandboxLimits(timeout_seconds=60, control_timeout_seconds=.5), image_reference=IMAGE_ID)
        self.configuration = MCPChildConfiguration(binding=self.fixture.binding,
            database_path=self.repository.database_path, workspace_root=self.registry.base_path,
            frozen_source=FrozenSourceSelection(project_artifact_id=self.source.artifact_id,
                snapshot_sha256=self.source.snapshot_sha256), unit_test_configuration=self.unit_configuration,
            max_call_seconds=5)
        self.metadata = A2AWorkflowMetadata(run_id=self.fixture.run.run_id,
            workflow_step_id=self.step.workflow_step_id, scenario_id=self.fixture.run.scenario_id,
            attempt=0, code_version=1, requirement_ids=self.ids, project_artifact_ids=(self.source.artifact_id,))
        self.execution = SimpleNamespace(metadata=self.metadata, configuration=self.fixture.configuration,
            source=self.source, request_text="회원가입 QA", budget=ExecutionBudget(runtime_budget_ms=15000,
                limits=LLMLimits(max_tool_calls=10, tool_timeout_seconds=5)))
        self.bindings = tuple(QACaseBinding(tool_name="run_unit_tests", selector=self.fixture.scope.name,
            test_id=f"tests.TestSignup.test_{index}", requirement_id=requirement_id,
            title=f"회원가입 기준 {index}", expected_result="보호된 요구사항을 만족한다")
            for index, requirement_id in enumerate(self.ids))
        self.decision = QADecision(kind="READY", cases=self.bindings, questions=())
        self.set_unit_report()

    def services(self, **extra):
        return QARuntimeServices(self.repository, self.registry, self.artifacts,
            mcp_configuration=self.configuration, **extra)

    def tracked(self, services):
        unit = UnitTestTools(self.artifacts, self.sandbox, self.fixture.store,
            configuration=self.configuration.unit_test_configuration, max_call_seconds=5)
        handlers = dict(unit.handlers(AgentRole.QA))
        if self.configuration.browser_test_configuration is not None:
            browser = BrowserTestTools(self.artifacts, self.sandbox, BrowserTestOutputStore(self.repository),
                configuration=self.configuration.browser_test_configuration, max_call_seconds=5)
            handlers.update(browser.handlers(AgentRole.QA))
        dispatcher = MCPDispatcher(self.fixture.binding, self.registry, handlers=handlers, max_call_seconds=5)
        session = DispatcherSession(dispatcher)
        self.session = session
        client = BoundMCPClient(configuration=self.configuration,
            _client=type("SDKPeerAdapter", (), {"session": session})())
        return services.tracked(client, self.execution)

    def set_unit_report(self, outcomes=None, *, extra=(), omit=()):
        outcomes = outcomes or {}
        cases = [{"testId": case.test_id, "outcome": outcomes.get(case.test_id, "PASS")}
                 for case in self.bindings if case.test_id not in omit]
        cases.extend(extra)
        failed = sum(case["outcome"] == "FAIL" for case in cases)
        self.docker.exit_code = int(bool(failed))
        self.docker.start_result = CLIResult(returncode=self.docker.exit_code, stderr=b"",
            stdout=json.dumps({"format": "unittest-v1", "total": len(cases),
                "passed": sum(case["outcome"] == "PASS" for case in cases), "failed": failed,
                "skipped": sum(case["outcome"] == "SKIP" for case in cases), "tests": cases}).encode())

    async def finish(self, services=None, decision=None):
        services = services or self.services()
        await services.prepare(self.execution)
        return await services.finalize(self.execution, decision or self.decision, self.tracked(services),
                                       task_id="qa-task", context_id="qa-context")

    def report(self, artifacts):
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].name, "qa-report.json")
        return QAReportArtifact.model_validate(MessageToDict(artifacts[0].parts[0].data))

    def input_counts(self):
        with self.repository._connection() as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                         for table in ("qa_test_input_captures", "qa_test_input_files", "qa_test_input_receipts"))

    async def test_constructor_inert_and_private_repr(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("inert")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("inert")):
            services = self.services()
        self.assertEqual(repr(services), "QARuntimeServices()")
        self.assertEqual(services.selectors, {"run_unit_tests": ("qa-unit",)})
        changed = services.selectors
        changed["run_unit_tests"] = ("other",)
        self.assertEqual(services.selectors, {"run_unit_tests": ("qa-unit",)})

    async def test_invalid_identity_and_role_rejected(self):
        for configuration in (replace(self.configuration, database_path=self.repository.database_path.parent / "other.sqlite3"),
                              replace(self.configuration, frozen_source=None),
                              replace(self.configuration, unit_test_configuration=None)):
            with self.subTest(configuration=configuration), self.assertRaises(QAServicesError):
                QARuntimeServices(self.repository, self.registry, self.artifacts, mcp_configuration=configuration)

    async def test_snapshot_scope_is_not_qa_configuration(self):
        configuration = replace(self.configuration, unit_test_configuration=UnitTestConfiguration(
            scopes=(UnitTestScope(name="developer", kind="SNAPSHOT"),)))
        with self.assertRaises(QAServicesError):
            QARuntimeServices(self.repository, self.registry, self.artifacts, mcp_configuration=configuration)

    async def test_success_has_one_measured_report_same_source_and_no_verdict_write(self):
        before = self.repository.get_run(self.metadata.run_id)
        report = self.report(await self.finish())
        self.assertTrue(report.passed)
        self.assertEqual(report.execution_manifest, self.source.execution_manifest())
        self.assertEqual(report.code_version, 1)
        self.assertEqual(report.artifact_version, 1)
        self.assertIsNone(report.previous_artifact_id)
        self.assertEqual({case.requirement_id for case in report.tests}, set(self.ids))
        self.assertEqual({case.tool_evidence.outcome.value for case in report.tests}, {"PASS"})
        self.assertEqual({case.actual_result for case in report.tests}, {"MEASURED_PASS"})
        self.assertEqual(self.repository.get_run(self.metadata.run_id), before)
        self.assertEqual(len(self.repository.list_project_artifacts(self.metadata.run_id)), 1)
        self.assertEqual(self.input_counts(), (1, 2, 1))
        self.assertEqual(self.execution.budget.tool_calls, 1)
        self.assertEqual(len(self.docker.commands("rm")), 1)

    async def test_product_failure_is_measured_fail_not_infra_error(self):
        self.set_unit_report({self.bindings[0].test_id: "FAIL"})
        report = self.report(await self.finish())
        self.assertEqual(report.tests[0].outcome, ValidationOutcome.FAIL)
        self.assertEqual(report.tests[0].tool_evidence.outcome.value, "PASS")
        self.assertFalse(report.passed)
        self.assertIsNone(self.repository.get_run(self.metadata.run_id).verdict)

    async def test_skip_is_unverified_without_fake_tool_evidence(self):
        self.set_unit_report({self.bindings[0].test_id: "SKIP"})
        report = self.report(await self.finish())
        self.assertEqual(report.tests[0].outcome, ValidationOutcome.UNVERIFIED)
        self.assertEqual(report.tests[0].actual_result, "RUNNER_REPORTED_SKIP")
        self.assertIsNone(report.tests[0].tool_evidence)

    async def test_missing_planned_case_is_unverified_without_pass_evidence(self):
        self.set_unit_report(omit=(self.bindings[0].test_id,))
        report = self.report(await self.finish())
        self.assertEqual(report.tests[0].outcome, ValidationOutcome.UNVERIFIED)
        self.assertEqual(report.tests[0].actual_result, "PLANNED_CASE_NOT_REPORTED")
        self.assertIsNone(report.tests[0].tool_evidence)

    async def test_unknown_passing_failing_skipped_cases_never_disappear(self):
        for outcome in ("PASS", "FAIL", "SKIP"):
            self.set_unit_report(extra=({"testId": "tests.Unbound.test_unknown", "outcome": outcome},))
            with self.subTest(outcome=outcome), self.assertRaises(QAServicesError) as raised:
                await self.finish()
            self.assertEqual(raised.exception.code, "QA_REPORT_BINDING_INVALID")

    async def test_missing_receipt_never_becomes_artifact(self):
        services = self.services()
        with patch.object(services._unit, "get", side_effect=ValueError("private path")):
            with self.assertRaises(QAServicesError) as raised:
                await self.finish(services)
        self.assertEqual(raised.exception.code, "QA_TEST_EVIDENCE_INVALID")
        self.assertNotIn("private path", str(raised.exception))
        self.assertEqual(self.input_counts(), (1, 2, 0))

    async def test_runner_infrastructure_failure_never_fabricates_test_result(self):
        self.docker.exit_code = 2
        self.docker.start_result = CLIResult(returncode=2, stdout=b'{"error":"TEST_RUNNER_ERROR"}', stderr=b"")
        services = self.services()
        tracked = self.tracked(services)
        with self.assertRaises(TrackedMCPError):
            await services.finalize(self.execution, self.decision, tracked, task_id="qa-task", context_id="qa-context")
        self.assertEqual(self.input_counts(), (1, 2, 0))
        self.assertEqual(len(self.docker.commands("start")), 1)
        record = services._tools.get(self.fixture.binding, tracked.logical_call_ids[-1])
        self.assertEqual(len(record.attempts), 1)
        self.assertIsNone(record.attempts[-1].execution_manifest_id)

    async def test_changed_inputs_before_tool_cannot_use_captured_test_bytes(self):
        services = self.services()
        tracked = self.tracked(services)
        original = self.session.call_tool
        async def mutate(name, arguments, **options):
            self.test_file.write_text("# changed after capture\n", encoding="utf-8")
            return await original(name, arguments, **options)
        self.session.call_tool = mutate
        with self.assertRaises(QAServicesError) as raised:
            await services.finalize(self.execution, self.decision, tracked, task_id="qa-task", context_id="qa-context")
        self.assertEqual(raised.exception.code, "QA_TEST_EVIDENCE_INVALID")
        self.assertEqual(self.input_counts(), (1, 2, 0))

    async def test_scratch_changed_after_capture_does_not_erase_persisted_test_bytes(self):
        original = self.test_file.read_bytes()
        self.docker.start_hook = lambda _container: self.test_file.write_text("# later scratch\n", encoding="utf-8")
        await self.finish()
        with self.repository._connection() as connection:
            row = connection.execute("SELECT content,content_sha256 FROM qa_test_input_files WHERE path='tests/test_signup.py'").fetchone()
        self.assertEqual(row[0], original)
        self.assertEqual(row[1], sha256(original).hexdigest())

    async def test_cancel_waits_cleanup_retains_input_without_completed_receipt(self):
        self.docker.block_start = True
        task = asyncio.create_task(self.finish())
        await asyncio.wait_for(self.docker.start_entered.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.docker.commands("rm")), 1)
        self.assertEqual(self.input_counts(), (1, 2, 0))

    async def test_budget_exhaustion_prevents_execution(self):
        self.execution.budget = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_tool_calls=0))
        with self.assertRaises(LLMRuntimeError):
            await self.finish()
        self.assertEqual(self.docker.calls, [])
        self.assertEqual(self.input_counts(), (1, 2, 0))

    async def test_aborted_run_cannot_start_qa(self):
        self.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        with self.assertRaises(QAServicesError):
            await self.services().prepare(self.execution)
        self.assertEqual(self.docker.calls, [])

    async def test_revalidating_is_not_implemented_in_stage32(self):
        self.fixture.mutate_run(status=WorkflowStatus.REVALIDATING)
        with self.assertRaises(QAServicesError):
            await self.services().prepare(self.execution)

    async def test_wrong_image_rejected_before_model_transport(self):
        self.configuration = replace(self.configuration, unit_test_configuration=replace(
            self.unit_configuration, image_reference="sha256:" + "e" * 64))
        with self.assertRaises(QAServicesError):
            await self.services().prepare(self.execution)
        self.assertEqual(self.docker.calls, [])

    async def test_source_grant_and_private_blob_are_required(self):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'", (str(self.source.artifact_id),))
        with self.assertRaises(QAServicesError):
            await self.services().prepare(self.execution)

    async def test_clean_continuation_attempt_is_independent_of_fix_cycle(self):
        self.step = WorkflowStep.model_validate({**self.step.model_dump(), "attempt": 1})
        self.fixture.mutate_step(self.step)
        self.metadata = type(self.metadata).model_validate({**self.metadata.model_dump(), "attempt": 1})
        self.execution.metadata = self.metadata
        report = self.report(await self.finish())
        self.assertTrue(report.passed)
        self.assertEqual(report.code_version, 1)

    async def test_protected_scope_hidden_and_always_executed_after_ready(self):
        protected = UnitTestScope(name="protected-unit", kind="PROTECTED",
            protected_files={"tests/test_signup.py": "# approved protected criteria\n"},
            protected_suite_ref=self.fixture.configuration.configuration.protected_test_suite_ref)
        self.configuration = replace(self.configuration, unit_test_configuration=replace(self.unit_configuration,
            scopes=(self.fixture.scope, protected)))
        protected_bindings = tuple(replace(case, selector="protected-unit") for case in self.bindings)
        services = self.services(protected_cases=protected_bindings)
        self.assertEqual(services.selectors, {"run_unit_tests": ("qa-unit",)})
        report = self.report(await self.finish(services))
        self.assertEqual(len(report.tests), 2 * len(self.bindings))
        self.assertEqual(self.input_counts(), (2, 4, 2))
        with self.repository._connection() as connection:
            contents = [row[0] for row in connection.execute("SELECT content FROM qa_test_input_files WHERE path='tests/test_signup.py'")]
        self.assertIn(b"# approved protected criteria\n", contents)

    async def test_protected_scope_without_host_bindings_is_invalid(self):
        protected = UnitTestScope(name="protected-unit", kind="PROTECTED",
            protected_files={"tests/test_signup.py": "# approved protected criteria\n"},
            protected_suite_ref=self.fixture.configuration.configuration.protected_test_suite_ref)
        self.configuration = replace(self.configuration, unit_test_configuration=replace(self.unit_configuration,
            scopes=(self.fixture.scope, protected)))
        with self.assertRaises(QAServicesError):
            self.services()

    async def test_frozen_protected_reference_cannot_run_generated_only(self):
        self.assertIsNotNone(self.fixture.configuration.configuration.protected_test_suite_ref)
        with self.assertRaises(QAServicesError):
            await self.services().prepare(self.execution)
        self.assertEqual(self.docker.calls, [])

    async def test_effective_profile_clamp_is_identical_to_actual_tool(self):
        await self.finish()
        with self.repository._connection() as connection:
            row = connection.execute("SELECT metadata_json FROM unit_test_execution_records").fetchone()
        self.assertEqual(json.loads(row[0])["executionProfile"]["limits"]["timeout_seconds"], 3)

    async def test_decision_with_partial_coverage_is_not_host_authority(self):
        decision = QADecision(kind="READY", cases=self.bindings[:1], questions=())
        with self.assertRaises(QAServicesError) as raised:
            await self.finish(decision=decision)
        self.assertEqual(raised.exception.code, "QA_REPORT_BINDING_INVALID")
        self.assertEqual(self.docker.calls, [])

    async def test_browser_only_pass_and_trace_refs_are_measured(self):
        suite = BrowserTestSuite(name="signup-browser", kind="QA_TESTS")
        browser = BrowserTestConfiguration(suites=(suite,), service_argv=("/usr/local/bin/python", "-B", "/snapshot/main.py"),
            playwright_version="1.60.0", limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5), image_reference=IMAGE_ID)
        self.configuration = replace(self.configuration, unit_test_configuration=None, browser_test_configuration=browser)
        data = {"format": "browser-suite-v1", "tests": [{"testId": f"signup.{index}", "steps": [
            {"action": "goto", "path": "/"}, {"action": "assert_visible", "selector": "#signup"}]}
            for index in range(len(self.ids))]}
        path = self.fixture.root / "outputs/qa/tests/browser/suite.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")
        self.decision = QADecision(kind="READY", questions=(), cases=tuple(QACaseBinding(
            tool_name="run_browser_tests", selector=suite.name, test_id=f"signup.{index}",
            requirement_id=requirement_id, title=f"브라우저 {index}", expected_result="화면 조건 충족")
            for index, requirement_id in enumerate(self.ids)))
        cases = [{"testId": case["testId"], "outcome": "PASS", "steps": [
            {"index": 1, "action": "goto", "outcome": "PASS", "durationMs": 2},
            {"index": 2, "action": "assert_visible", "outcome": "PASS", "durationMs": 2}]}
            for case in data["tests"]]
        self.docker.exit_code = 0
        self.docker.start_result = CLIResult(returncode=0, stderr=b"", stdout=json.dumps({
            "format": "browser-v1", "suiteName": suite.name, "playwrightVersion": "1.60.0", "browserVersion": "145.0.7632.6",
            "total": len(cases), "passed": len(cases), "failed": 0, "tests": cases}).encode())
        report = self.report(await self.finish())
        self.assertTrue(report.passed)
        self.assertEqual({case.tool_evidence.tool_name for case in report.tests}, {"run_browser_tests"})
        self.assertEqual(self.input_counts(), (1, 5, 1))


if __name__ == "__main__":
    unittest.main()
