"""Immutable Host evaluation criteria; real SQLite/Git, no live execution.

The protected source below is inert provenance data, not a product test suite.
Existing Fake Docker fixtures validate boundaries, not actual signup quality.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.runtime.evaluation_policy_store import (
    EvaluationPolicyError, EvaluationPolicyStore, qa_policy_sha256, security_policy_sha256,
)
from agents.runtime.qa_services import QARuntimeServices, QAServicesError
from agents.runtime.security_services import SecurityRuntimeServices, SecurityServicesError
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.browser_config import BrowserTestConfiguration, BrowserTestSuite
from mcp_tools.tools.unit_config import UnitTestScope
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry
import test_qa_services as qa_fixture
import test_security_agent_services as security_fixture
import test_validation_revalidation_services as revalidation_fixture


class EvaluationPolicyPinningTests(unittest.IsolatedAsyncioTestCase):
    def borrow(self, role, *, protected=False):
        case = qa_fixture.QAServicesTests("test_protected_scope_hidden_and_always_executed_after_ready"
            if protected else "test_constructor_inert_and_private_repr") if role is AgentRole.QA \
            else security_fixture.SecurityAgentServicesTests("test_constructor_is_inert_and_private_repr")
        case.setUp()
        for cleanup, args, kwargs in case._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        case._cleanups.clear()
        return case

    def protected_unit(self, case, text="# immutable approved fixture A\n"):
        protected = UnitTestScope(name="protected-unit", kind="PROTECTED",
            protected_files={"tests/test_signup.py": text},
            protected_suite_ref=case.execution.configuration.configuration.protected_test_suite_ref)
        case.configuration = replace(case.configuration, unit_test_configuration=replace(case.unit_configuration,
            scopes=(case.fixture.scope, protected)))
        return tuple(replace(binding, selector=protected.name) for binding in case.bindings)

    @staticmethod
    def pin(store, case, digest):
        store.pin(case.configuration.binding, workflow_step_id=case.metadata.workflow_step_id,
            source_artifact_id=case.source.artifact_id, policy_sha256=digest)

    @staticmethod
    def rows(case):
        with case.repository._connection() as connection:
            return tuple(tuple(row) for row in connection.execute(
                "SELECT run_id,workspace_id,role,policy_sha256 FROM run_evaluation_policies ORDER BY role"))

    async def test_construction_stays_inert(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            with patch.object(case.repository, "_connection", side_effect=AssertionError("inert")), \
                    patch.object(case.repository, "_transaction", side_effect=AssertionError("inert")), \
                    patch.object(Path, "read_bytes", side_effect=AssertionError("inert")):
                self.assertEqual(repr(EvaluationPolicyStore(case.repository)), "EvaluationPolicyStore()")
                case.services()

    async def test_protected_bytes_same_reference_cannot_drift(self):
        case = self.borrow(AgentRole.QA, protected=True)
        bindings = self.protected_unit(case)
        await case.services(protected_cases=bindings).prepare(case.execution)
        before = self.rows(case)
        self.protected_unit(case, "# immutable approved fixture B\n")
        with self.assertRaises(QAServicesError):
            await case.services(protected_cases=bindings).prepare(case.execution)
        self.assertEqual(self.rows(case), before)
        self.assertEqual(case.docker.calls, [])

    async def test_protected_requirement_binding_cannot_drift(self):
        case = self.borrow(AgentRole.QA, protected=True)
        bindings = self.protected_unit(case)
        await case.services(protected_cases=bindings).prepare(case.execution)
        changed = (replace(bindings[0], requirement_id=bindings[1].requirement_id), *bindings[1:])
        with self.assertRaises(QAServicesError):
            await case.services(protected_cases=changed).prepare(case.execution)
        self.assertEqual(case.docker.calls, [])

    async def test_protected_test_id_and_expected_result_cannot_drift(self):
        case = self.borrow(AgentRole.QA, protected=True)
        bindings = self.protected_unit(case)
        await case.services(protected_cases=bindings).prepare(case.execution)
        for changes in ({"test_id": "tests.Different.test_case"}, {"expected_result": "other criteria"}):
            changed = (replace(bindings[0], **changes), *bindings[1:])
            with self.subTest(changes=changes), self.assertRaises(QAServicesError):
                await case.services(protected_cases=changed).prepare(case.execution)

    async def test_selector_discovery_pattern_cannot_drift(self):
        case = self.borrow(AgentRole.QA)
        await case.services().prepare(case.execution)
        case.configuration = replace(case.configuration, unit_test_configuration=replace(case.unit_configuration,
            scopes=(replace(case.fixture.scope, pattern="test_other.py"),)))
        with self.assertRaises(QAServicesError):
            await case.services().prepare(case.execution)

    async def test_browser_protected_bytes_and_version_cannot_drift(self):
        case = self.borrow(AgentRole.QA, protected=True)
        data = {"format": "browser-suite-v1", "tests": [{"testId": "signup.first", "steps": [
            {"action": "goto", "path": "/"}, {"action": "assert_visible", "selector": "#signup"}]}]}
        suite = BrowserTestSuite(name="protected-browser", kind="PROTECTED",
            protected_suite_ref=case.execution.configuration.configuration.protected_test_suite_ref,
            protected_files={"tests/browser/suite.json": json.dumps(data)})
        browser = BrowserTestConfiguration(suites=(suite,), service_argv=("/usr/local/bin/python", "/snapshot/main.py"),
            playwright_version="1.60.0", limits=case.unit_configuration.limits,
            image_reference=case.unit_configuration.image_reference)
        case.configuration = replace(case.configuration, browser_test_configuration=browser)
        bindings = (replace(case.bindings[0], tool_name="run_browser_tests", selector=suite.name, test_id="signup.first"),)
        await case.services(protected_cases=bindings).prepare(case.execution)
        data["tests"][0]["steps"][1]["selector"] = "#other"
        changed = replace(suite, protected_files={"tests/browser/suite.json": json.dumps(data)})
        for policy in (replace(browser, suites=(changed,)), replace(browser, playwright_version="1.61.0")):
            case.configuration = replace(case.configuration, browser_test_configuration=policy)
            with self.assertRaises(QAServicesError):
                await case.services(protected_cases=bindings).prepare(case.execution)
        self.assertEqual(case.docker.calls, [])

    async def test_generated_qa_test_bytes_may_change(self):
        case = self.borrow(AgentRole.QA)
        await case.services().prepare(case.execution)
        before = self.rows(case)
        case.test_file.write_text("# changed inert generated QA fixture\n", encoding="utf-8")
        await case.services().prepare(case.execution)
        self.assertEqual(self.rows(case), before)

    async def test_binding_order_has_no_semantic_drift(self):
        case = self.borrow(AgentRole.QA, protected=True)
        bindings = self.protected_unit(case)
        await case.services(protected_cases=bindings).prepare(case.execution)
        policy = case.configuration.unit_test_configuration
        case.configuration = replace(case.configuration, unit_test_configuration=replace(policy, scopes=policy.scopes[::-1]))
        await case.services(protected_cases=bindings[::-1]).prepare(case.execution)
        self.assertEqual(len(self.rows(case)), 1)

    async def test_scanner_version_rules_name_and_executable_cannot_drift(self):
        case = self.borrow(AgentRole.SECURITY)
        await case.services().prepare(case.execution)
        initial = case.configuration
        policy = initial.security_scan_configuration
        profile = policy.profiles[0]
        variants = (replace(policy, profiles=(replace(profile, rule_ids=("B101",)),)),
            replace(policy, profiles=(replace(profile, scanner_version="1.8.7"),)),
            replace(policy, profiles=(replace(profile, name="changed-security"),)),
            replace(policy, python_executable="/usr/bin/python3"))
        for changed in variants:
            case.configuration = replace(initial, security_scan_configuration=changed)
            with self.subTest(policy=changed), self.assertRaises(SecurityServicesError):
                await case.services().prepare(case.execution)
        self.assertEqual(case.docker.calls, [])

    async def test_execution_limits_cannot_drift(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            await case.services().prepare(case.execution)
            case.configuration = replace(case.configuration, max_call_seconds=4)
            with self.assertRaises(QAServicesError if role is AgentRole.QA else SecurityServicesError):
                await case.services().prepare(case.execution)

    async def test_new_service_and_repository_keep_same_baseline(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            await case.services().prepare(case.execution)
            repository = SQLiteWorkflowRepository(case.repository.database_path)
            registry = WorkspaceRegistry(repository, case.registry.base_path)
            artifacts = ArtifactStore(repository, registry)
            cls = QARuntimeServices if role is AgentRole.QA else SecurityRuntimeServices
            restored = cls(repository, registry, artifacts, mcp_configuration=case.configuration)
            await restored.prepare(case.execution)
            changed = replace(case.configuration, max_call_seconds=4)
            with self.assertRaises(QAServicesError if role is AgentRole.QA else SecurityServicesError):
                await cls(repository, registry, artifacts, mcp_configuration=changed).prepare(case.execution)

    async def test_same_policy_is_valid_for_new_snapshot_step_and_code_version(self):
        helper = revalidation_fixture.ValidationRevalidationServicesTests("test_first_unit_report_for_second_candidate_keeps_report_version_one")
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            await case.services().prepare(case.execution)
            before = self.rows(case)
            helper.advance(case, role)
            await case.services().prepare(case.execution)
            self.assertEqual(case.source.code_version, 2)
            self.assertEqual(self.rows(case), before)

    async def test_protected_policy_drift_after_source_fix_is_rejected(self):
        case = self.borrow(AgentRole.QA, protected=True)
        bindings = self.protected_unit(case)
        await case.services(protected_cases=bindings).prepare(case.execution)
        helper = revalidation_fixture.ValidationRevalidationServicesTests("test_first_unit_report_for_second_candidate_keeps_report_version_one")
        helper.advance(case, AgentRole.QA)
        self.protected_unit(case, "# altered inert fixed candidate policy\n")
        with self.assertRaises(QAServicesError):
            await case.services(protected_cases=bindings).prepare(case.execution)
        self.assertEqual(case.docker.calls, [])

    async def test_scanner_rule_drift_after_source_fix_is_rejected(self):
        case = self.borrow(AgentRole.SECURITY)
        await case.services().prepare(case.execution)
        helper = revalidation_fixture.ValidationRevalidationServicesTests("test_first_unit_report_for_second_candidate_keeps_report_version_one")
        helper.advance(case, AgentRole.SECURITY)
        policy = case.configuration.security_scan_configuration
        case.configuration = replace(case.configuration, security_scan_configuration=replace(policy,
            profiles=(replace(policy.profiles[0], rule_ids=("B101",)),)))
        with self.assertRaises(SecurityServicesError):
            await case.services().prepare(case.execution)

    async def test_a2a_resume_attempt_does_not_change_policy(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            await case.services().prepare(case.execution)
            before = self.rows(case)
            step = case.step.model_copy(update={"attempt": 6})
            revalidation_fixture.ValidationRevalidationServicesTests.update_step(case.repository, step)
            case.execution.metadata = case.metadata.model_copy(update={"attempt": 6})
            await case.services().prepare(case.execution)
            self.assertEqual(self.rows(case), before)

    async def test_platform_runner_change_is_not_hidden_by_same_reference(self):
        case = self.borrow(AgentRole.QA)
        await case.services().prepare(case.execution)
        original = Path.read_bytes
        def changed(path):
            content = original(path)
            return content + b"\n# inert runner revision\n" if path.name == "unit_runner.py" else content
        with patch.object(Path, "read_bytes", changed), self.assertRaises(QAServicesError):
            await case.services().prepare(case.execution)

    async def test_ledger_stores_only_fingerprints_not_protected_source(self):
        case = self.borrow(AgentRole.QA, protected=True)
        marker = "EVALUATION_PROTECTED_SOURCE_MARKER"
        bindings = self.protected_unit(case, f"# {marker}\n")
        await case.services(protected_cases=bindings).prepare(case.execution)
        rows = self.rows(case)
        self.assertNotIn(marker, repr(rows))
        self.assertRegex(rows[0][3], "^[0-9a-f]{64}$")

    async def test_ledger_is_immutable_for_update_delete_and_replace(self):
        case = self.borrow(AgentRole.SECURITY)
        await case.services().prepare(case.execution)
        before = self.rows(case)
        for query in ("UPDATE run_evaluation_policies SET policy_sha256='" + "a" * 64 + "'",
                      "DELETE FROM run_evaluation_policies",
                      "INSERT OR REPLACE INTO run_evaluation_policies SELECT * FROM run_evaluation_policies"):
            with self.subTest(query=query), self.assertRaises(sqlite3.IntegrityError):
                with case.repository._transaction() as connection:
                    connection.execute(query)
        self.assertEqual(self.rows(case), before)

    async def test_first_admission_race_pins_only_one_policy(self):
        case = self.borrow(AgentRole.SECURITY)
        def attempt(digest):
            try:
                self.pin(EvaluationPolicyStore(case.repository), case, digest)
                return True
            except EvaluationPolicyError:
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(attempt, ("a" * 64, "b" * 64)))
        self.assertEqual(sorted(outcomes), [False, True])
        self.assertEqual(len(self.rows(case)), 1)

    async def test_invalid_identity_role_hash_and_ended_run_cannot_pin(self):
        case = self.borrow(AgentRole.SECURITY)
        store = EvaluationPolicyStore(case.repository)
        for changes in ({"workflow_step_id": uuid4()}, {"source_artifact_id": uuid4()},
                        {"policy_sha256": "bad"}, {"policy_sha256": "A" * 64},
                        {"binding": MCPBinding(role=AgentRole.DEVELOPER, agent_role=AgentRole.DEVELOPER,
                            run_id=case.metadata.run_id, workspace_id=case.execution.configuration.workspace_id)}):
            values = dict(binding=case.configuration.binding, workflow_step_id=case.metadata.workflow_step_id,
                source_artifact_id=case.source.artifact_id, policy_sha256="a" * 64)
            with self.subTest(changes=changes), self.assertRaises(EvaluationPolicyError):
                store.pin(**{**values, **changes})
        case.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        with self.assertRaises(EvaluationPolicyError):
            self.pin(store, case, "a" * 64)

    async def test_qa_and_security_pin_independent_role_policies(self):
        case = self.borrow(AgentRole.SECURITY)
        self.pin(EvaluationPolicyStore(case.repository), case, "a" * 64)
        qa = WorkflowStep(run_id=case.metadata.run_id, agent_role=AgentRole.QA, status=WorkflowStepStatus.RUNNING,
            code_version=1, requirement_ids=list(case.ids), input_artifact_ids=[case.source.artifact_id])
        revalidation_fixture.ValidationRevalidationServicesTests.insert_step(case.repository, qa)
        EvaluationPolicyStore(case.repository).pin(MCPBinding(role=AgentRole.QA, agent_role=AgentRole.QA,
            run_id=case.metadata.run_id, workspace_id=case.execution.configuration.workspace_id),
            workflow_step_id=qa.workflow_step_id, source_artifact_id=case.source.artifact_id, policy_sha256="b" * 64)
        self.assertEqual({row[2] for row in self.rows(case)}, {"QA", "SECURITY"})

    async def test_legacy_qa_evidence_without_baseline_cannot_be_reapproved(self):
        case = self.borrow(AgentRole.QA)
        with patch.object(EvaluationPolicyStore, "pin"):
            await case.finish()
        with self.assertRaises(QAServicesError):
            await case.services().prepare(case.execution)

    async def test_legacy_security_evidence_without_baseline_cannot_be_reapproved(self):
        case = self.borrow(AgentRole.SECURITY)
        with patch.object(EvaluationPolicyStore, "pin"):
            await case.scan()
        with self.assertRaises(SecurityServicesError):
            await case.services().prepare(case.execution)

    async def test_developer_unit_receipt_is_not_qa_policy(self):
        case = self.borrow(AgentRole.QA)
        with case.repository._transaction() as connection:
            connection.execute("CREATE TABLE unit_test_execution_records(run_id TEXT, metadata_json TEXT)")
            connection.execute("INSERT INTO unit_test_execution_records VALUES(?,?)",
                (str(case.metadata.run_id), '{"role":"DEVELOPER"}'))
        await case.services().prepare(case.execution)

    async def test_policy_fingerprints_ignore_candidate_identity_and_daemon_transport(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            case = self.borrow(role)
            calculate = (lambda child: qa_policy_sha256(child, ())) if role is AgentRole.QA else security_policy_sha256
            original = calculate(case.configuration)
            field = "unit_test_configuration" if role is AgentRole.QA else "security_scan_configuration"
            policy = getattr(case.configuration, field)
            changed = replace(case.configuration, **{field: replace(policy, docker_endpoint="unix:///tmp/approved.sock")})
            self.assertEqual(calculate(changed), original)

    async def test_read_only_query_before_evaluation_does_not_create_ledger(self):
        case = self.borrow(AgentRole.SECURITY)
        store = EvaluationPolicyStore(case.repository)
        self.assertEqual(store.list_for_run(case.metadata.run_id), ())
        with case.repository._connection() as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM sqlite_master WHERE name='run_evaluation_policies'").fetchone())

    async def test_read_only_query_returns_typed_immutable_hash_after_termination(self):
        case = self.borrow(AgentRole.SECURITY)
        await case.services().prepare(case.execution)
        store = EvaluationPolicyStore(case.repository)
        records = store.list_for_run(case.metadata.run_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].role, AgentRole.SECURITY)
        self.assertEqual(records[0].policy_sha256, security_policy_sha256(case.configuration))
        with self.assertRaises(AttributeError):
            records[0].policy_sha256 = "changed"
        case.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        self.assertEqual(store.list_for_run(case.metadata.run_id), records)

    async def test_read_only_query_rejects_unknown_and_non_uuid_run(self):
        case = self.borrow(AgentRole.SECURITY)
        for identity in (uuid4(), "REQ-001", "secret private path"):
            with self.assertRaises(EvaluationPolicyError) as error:
                EvaluationPolicyStore(case.repository).list_for_run(identity)
            self.assertEqual(str(error.exception), "EVALUATION_POLICY_INVALID")


if __name__ == "__main__":
    unittest.main()
