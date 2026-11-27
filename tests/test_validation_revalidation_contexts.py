"""Readonly validation lineage fixtures; no provider or executable Source.

Synthetic immutable Source bytes and reports exercise loader admission only.
They do not claim Build or Security PASS, Docker execution or a product fix.
"""

from hashlib import sha256
import unittest
from uuid import uuid4

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext

from agents.runtime.qa_context import (
    QAContextError, QAExecutionContext, SQLiteQAContextLoader, verify_report_predecessor,
)
from agents.runtime.security_context import SecurityContextError, SecurityExecutionContext, SQLiteSecurityContextLoader
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.artifacts.sqlite_store import SQLiteArtifactContentStore
from orchestrator.domain.models import AgentContext, WorkflowStep
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, A2ATaskState, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import (
    QAReportArtifact, QATestResult, SecurityReportArtifact, SecurityRequirementResult,
)
from test_qa_context import _QAContextFixture


class ValidationRevalidationContextTests(_QAContextFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.current_sources = [self.source]
        self.last_reports = {AgentRole.QA: None, AgentRole.SECURITY: None}

    def insert_step(self, step):
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO workflow_steps(workflow_step_id,run_id,status,created_at,updated_at,payload_json) VALUES(?,?,?,?,?,?)",
                (str(step.workflow_step_id), str(step.run_id), step.status.value,
                 step.created_at.isoformat(), step.updated_at.isoformat(), step.model_dump_json()))

    def insert_artifact(self, artifact):
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO project_artifacts VALUES(?,?,?,?,?)", (str(artifact.artifact_id),
                str(artifact.run_id), artifact.artifact_type, artifact.artifact_version, artifact.model_dump_json()))

    def finish_current_validation(self, *, reports=True):
        for step in self.repository.list_steps(self.run.run_id):
            if step.agent_role not in self.last_reports or step.status is not WorkflowStepStatus.RUNNING:
                continue
            if not reports:
                self.update(step=step.model_copy(update={"status": WorkflowStepStatus.CANCELED}))
                continue
            previous = self.last_reports[step.agent_role]
            values = {"artifact_id": uuid4(), "artifact_version": 1 if previous is None else previous.artifact_version + 1,
                "previous_artifact_id": None if previous is None else previous.artifact_id,
                "run_id": self.run.run_id, "workflow_step_id": step.workflow_step_id,
                "a2a_task_id": f"{step.agent_role.value}-task-{self.source.code_version}",
                "a2a_artifact_id": f"{step.agent_role.value}-report-{self.source.code_version}",
                "code_version": self.source.code_version, "execution_manifest": self.source.execution_manifest(),
                "requirement_ids": tuple(step.requirement_ids)}
            if step.agent_role is AgentRole.QA:
                report = QAReportArtifact(**values, tests=tuple(QATestResult(test_id=f"case-{index}",
                    requirement_id=identity, outcome="UNVERIFIED", title="Unverified fixture")
                    for index, identity in enumerate(step.requirement_ids)))
            else:
                report = SecurityReportArtifact(**values, requirement_results=tuple(SecurityRequirementResult(
                    requirement_id=identity, outcome="UNVERIFIED") for identity in step.requirement_ids))
            self.insert_artifact(report)
            self.update(step=step.model_copy(update={"status": WorkflowStepStatus.SUCCEEDED,
                "a2a_task_state": A2ATaskState.COMPLETED, "a2a_task_id": report.a2a_task_id,
                "agent_context_id": f"{step.agent_role.value}-shared-context",
                "a2a_artifact_ids": [report.a2a_artifact_id], "output_artifact_ids": [report.artifact_id]}))
            self.set_mapping(step.agent_role, task_id=report.a2a_task_id)
            self.last_reports[step.agent_role] = report

    def set_mapping(self, role, *, task_id, context_id=None):
        mapping = AgentContext(run_id=self.run.run_id, agent_id=role.value.lower(),
            agent_context_id=context_id or f"{role.value}-shared-context", latest_a2a_task_id=task_id)
        with self.repository._transaction() as connection:
            connection.execute("INSERT INTO agent_contexts VALUES(?,?,?) ON CONFLICT(run_id,agent_id) DO UPDATE SET payload_json=excluded.payload_json",
                (str(mapping.run_id), mapping.agent_id, mapping.model_dump_json()))

    def advance(self, *, reports=True, attempt=0):
        self.finish_current_validation(reports=reports)
        previous = self.source
        code = previous.code_version + 1
        self.run = self.run.model_copy(update={"status": WorkflowStatus.FIXING, "fix_attempt": code - 1})
        self.update(run=self.run)
        identity, task = uuid4(), f"developer-task-{code}"
        developer = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.SUCCEEDED, code_version=code, requirement_ids=list(previous.requirement_ids),
            input_artifact_ids=[previous.artifact_id], output_artifact_ids=[identity],
            a2a_task_state=A2ATaskState.COMPLETED, a2a_task_id=task,
            agent_context_id="developer-shared-context", a2a_artifact_ids=[f"developer-source-{code}"])
        self.insert_step(developer)
        archive = f"nonexecutable immutable source fixture candidate {code}".encode()
        self.source = CodeSnapshotArtifact(**{**previous.model_dump(), "artifact_id": identity,
            "artifact_version": code, "previous_artifact_id": previous.artifact_id,
            "workflow_step_id": developer.workflow_step_id, "code_version": code,
            "commit_hash": str(code) * 40, "tree_hash": str(code + 1) * 40,
            "snapshot_sha256": sha256(archive).hexdigest(), "artifact_uri": f"artifact://{identity}/source.tar",
            "a2a_task_id": task, "a2a_artifact_id": f"developer-source-{code}"})
        SQLiteArtifactContentStore(self.repository).put(self.source, archive, "application/x-tar",
            grants=(AgentRole.QA, AgentRole.SECURITY))
        self.insert_artifact(self.source)
        self.current_sources.append(self.source)
        self.run = self.run.model_copy(update={"status": WorkflowStatus.REVALIDATING, "code_version": code})
        self.update(run=self.run)
        for role, validator in ((AgentRole.QA, RequirementValidator.QA), (AgentRole.SECURITY, RequirementValidator.SECURITY)):
            # The Registry remains frozen; use its immutable contract rather
            # than consulting any mutable global scenario definition.
            from orchestrator.domain.scenario_registry import ScenarioDefinition
            required = ScenarioDefinition.from_contract(self.configuration.scenario_contract).requirement_ids_for(validator)
            self.insert_step(WorkflowStep(run_id=self.run.run_id, agent_role=role,
                status=WorkflowStepStatus.RUNNING, code_version=code, attempt=attempt,
                requirement_ids=list(required), input_artifact_ids=[identity],
                agent_context_id=f"{role.value}-shared-context" if self.last_reports[role] else None))

    def context_for(self, role, **changes):
        step = next(step for step in self.repository.list_steps(self.run.run_id)
            if step.agent_role is role and step.status is WorkflowStepStatus.RUNNING)
        metadata = A2AWorkflowMetadata(run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
            scenario_id=self.run.scenario_id, code_version=self.source.code_version, attempt=step.attempt,
            requirement_ids=tuple(step.requirement_ids), project_artifact_ids=(self.source.artifact_id,))
        report_type = QAExecutionContext if role is AgentRole.QA else SecurityExecutionContext
        report = changes.get("previous_report", self.last_reports[role])
        report_source = None if report is None else next(
            source for source in self.current_sources if source.code_version == report.code_version)
        return report_type(**({"metadata": metadata, "configuration": self.configuration,
            "budget": self.budget, "request_text": self.run.request_text,
            "requirement_artifact": self.artifact, "source": self.source,
            "previous_source": None if len(self.current_sources) == 1 else self.current_sources[-2],
            "previous_report": self.last_reports[role], "previous_report_source": report_source,
            "fix_attempt": self.run.fix_attempt} | changes))

    def request_for(self, role, *, metadata=None, task_id=None, context_id=None):
        execution = self.context_for(role)
        return RequestContext(call_context=ServerCallContext(state={}), request=build_send_message_request(
            execution.initial_payload, metadata or execution.metadata),
            task_id=task_id or f"{role.value}-new-task-{self.source.code_version}",
            context_id=context_id or f"{role.value}-shared-context")

    def load(self, role, **changes):
        loader_type = SQLiteQAContextLoader if role is AgentRole.QA else SQLiteSecurityContextLoader
        return loader_type(self.repository, lambda _configuration: self.budget)(self.request_for(role, **changes))

    def assert_denied(self, role, operation):
        with self.assertRaises(QAContextError if role is AgentRole.QA else SecurityContextError):
            operation()

    def test_all_three_fix_cycles_load_actual_report_and_source_predecessors(self):
        self.budget.reserve_model_call()
        self.budget.reserve_tool_call()
        for code in (2, 3, 4):
            self.advance(attempt=7)
            for role in self.last_reports:
                with self.subTest(code=code, role=role):
                    execution = self.load(role)
                    self.assertIs(execution.budget, self.budget)
                    self.assertEqual((execution.budget.model_calls, execution.budget.tool_calls), (1, 1))
                    self.assertEqual(execution.fix_attempt, code - 1)
                    self.assertEqual(execution.metadata.attempt, 7)
                    self.assertEqual(execution.source, self.source)
                    self.assertEqual(execution.previous_source, self.current_sources[-2])
                    self.assertEqual(execution.previous_report, self.last_reports[role])
                    self.assertEqual(execution.previous_report_source, self.current_sources[-2])
                    self.assertEqual(execution.initial_payload["snapshot"]["executionManifest"]["codeVersion"], code)

    def test_first_report_after_build_failure_can_be_version_one_for_code_two(self):
        self.advance(reports=False)
        for role in self.last_reports:
            execution = self.load(role)
            self.assertIsNone(execution.previous_report)
            model = QAReportArtifact if role is AgentRole.QA else SecurityReportArtifact
            self.assertEqual(verify_report_predecessor(self.repository, execution, role, model), 1)

    def test_report_lineage_uses_actual_prior_report_not_prior_code_number(self):
        self.advance(reports=True)
        self.advance(reports=False)
        for role in self.last_reports:
            execution = self.load(role)
            self.assertEqual(execution.source.code_version, 3)
            self.assertEqual(execution.previous_source.code_version, 2)
            self.assertEqual(execution.previous_report.code_version, 1)
            self.assertEqual(execution.previous_report_source, self.current_sources[0])
            model = QAReportArtifact if role is AgentRole.QA else SecurityReportArtifact
            self.assertEqual(verify_report_predecessor(self.repository, execution, role, model), 2)

    def test_previous_candidate_metadata_cannot_load_new_active_step(self):
        self.advance()
        for role in self.last_reports:
            metadata = self.context_for(role).metadata.model_copy(update={"code_version": 1,
                "project_artifact_ids": (self.current_sources[0].artifact_id,)})
            self.assert_denied(role, lambda: self.load(role, metadata=metadata))

    def test_revalidation_cannot_claim_initial_validating_state(self):
        self.advance()
        self.update(run=self.run.model_copy(update={"status": WorkflowStatus.VALIDATING}))
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role))

    def test_fix_cycle_is_not_inferred_from_a2a_continuation_attempt(self):
        self.advance(attempt=9)
        self.update(run=self.run.model_copy(update={"fix_attempt": 0}))
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role))

    def test_foreign_related_context_and_previous_terminal_task_are_denied(self):
        self.advance()
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role, context_id="different-context"))
            self.assert_denied(role, lambda: self.load(role, task_id=self.last_reports[role].a2a_task_id))

    def test_missing_source_predecessor_and_wrong_fix_scope_are_denied(self):
        self.advance()
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.context_for(role, previous_source=None))
            self.assert_denied(role, lambda: self.context_for(role, fix_attempt=2))
            self.assert_denied(role, lambda: self.context_for(role, fix_attempt=True))

    def test_report_predecessor_for_wrong_source_or_role_is_denied(self):
        self.advance()
        for role in self.last_reports:
            other = AgentRole.SECURITY if role is AgentRole.QA else AgentRole.QA
            self.assert_denied(role, lambda: self.context_for(role, previous_report=self.last_reports[other]))
            forged = self.last_reports[role].model_copy(update={"code_version": 2,
                "execution_manifest": self.source.execution_manifest()})
            self.assert_denied(role, lambda: self.context_for(role, previous_report=forged))

    def test_immediate_previous_report_requires_exact_source_manifest_even_with_fallback(self):
        self.advance()
        for role in self.last_reports:
            report = self.last_reports[role]
            for change in ({"project_artifact_id": uuid4()}, {"tree_hash": "a" * 40},
                           {"commit_hash": "b" * 40}, {"snapshot_sha256": "c" * 64}):
                with self.subTest(role=role, change=change):
                    forged = report.model_copy(update={"execution_manifest":
                        report.execution_manifest.model_copy(update=change)})
                    self.assert_denied(role, lambda: self.context_for(role,
                        previous_report=forged, previous_report_source=None))
            self.assertEqual(self.context_for(role, previous_report_source=None).previous_report, report)

    def test_report_gap_requires_explicit_exact_older_source(self):
        self.advance()
        self.advance(reports=False)
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.context_for(role, previous_report_source=None))
            self.assert_denied(role, lambda: self.context_for(role,
                previous_report_source=self.current_sources[-2]))
            forged = self.current_sources[0].model_copy(update={"artifact_id": uuid4()})
            self.assert_denied(role, lambda: self.context_for(role, previous_report_source=forged))
            self.assertEqual(self.load(role).previous_report_source, self.current_sources[0])

    def test_no_previous_report_cannot_carry_an_unrelated_report_source(self):
        self.advance(reports=False)
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.context_for(role,
                previous_report_source=self.current_sources[0]))

    def test_unobserved_active_context_cannot_change_historical_role_context(self):
        self.advance()
        for role in self.last_reports:
            step = next(step for step in self.repository.list_steps(self.run.run_id)
                if step.agent_role is role and step.status is WorkflowStepStatus.RUNNING)
            self.update(step=step.model_copy(update={"agent_context_id": None}))
            self.assert_denied(role, lambda: self.load(role, context_id="foreign-context"))
            self.assertEqual(self.load(role).previous_report, self.last_reports[role])

    def test_every_historical_role_task_is_distinct_from_fourth_candidate_task(self):
        self.advance()
        earliest = {role: self.last_reports[role].a2a_task_id for role in self.last_reports}
        self.advance()
        self.advance()
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role, task_id=earliest[role]))

    def test_historical_role_contexts_cannot_split(self):
        self.advance()
        earliest = {role: self.last_reports[role].workflow_step_id for role in self.last_reports}
        self.advance()
        for role in self.last_reports:
            step = next(step for step in self.repository.list_steps(self.run.run_id)
                if step.workflow_step_id == earliest[role])
            self.update(step=step.model_copy(update={"agent_context_id": "foreign-historical-context"}))
            self.assert_denied(role, lambda: self.load(role))

    def test_historical_role_context_requires_matching_mapping(self):
        self.advance()
        for role in self.last_reports:
            self.set_mapping(role, task_id="unrelated-task")
            self.assert_denied(role, lambda: self.load(role))
            self.set_mapping(role, task_id=self.last_reports[role].a2a_task_id, context_id="foreign-context")
            self.assert_denied(role, lambda: self.load(role))
            with self.repository._transaction() as connection:
                connection.execute("DELETE FROM agent_contexts WHERE run_id=? AND agent_id=?",
                    (str(self.run.run_id), role.value.lower()))
            self.assert_denied(role, lambda: self.load(role))

    def test_mapping_task_none_is_only_an_unobserved_new_task_gap(self):
        self.advance()
        for role in self.last_reports:
            self.set_mapping(role, task_id=None)
            self.load(role)
            step = next(step for step in self.repository.list_steps(self.run.run_id)
                if step.agent_role is role and step.status is WorkflowStepStatus.RUNNING)
            task_id = f"{role.value}-new-task-{self.source.code_version}"
            self.update(step=step.model_copy(update={"a2a_task_id": task_id}))
            self.assert_denied(role, lambda: self.load(role))
            self.set_mapping(role, task_id=task_id)
            self.load(role)

    def test_current_step_requires_exact_source_only_inputs(self):
        self.advance()
        for role in self.last_reports:
            step = next(step for step in self.repository.list_steps(self.run.run_id)
                if step.agent_role is role and step.status is WorkflowStepStatus.RUNNING)
            self.update(step=step.model_copy(update={"input_artifact_ids": [self.source.artifact_id, self.artifact.artifact_id]}))
            self.assert_denied(role, lambda: self.load(role))

    def test_previous_report_chain_cannot_skip_version_or_predecessor(self):
        self.advance()
        report = self.last_reports[AgentRole.QA]
        self.rewrite_artifact(report.model_copy(update={"artifact_version": 2, "previous_artifact_id": uuid4()}))
        self.assert_denied(AgentRole.QA, lambda: self.load(AgentRole.QA))

    def test_previous_source_blob_grant_is_required(self):
        self.advance()
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='QA'",
                (str(self.current_sources[0].artifact_id),))
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role))

    def test_previous_source_producer_task_identity_is_required(self):
        self.advance()
        previous = self.current_sources[0]
        step = next(step for step in self.repository.list_steps(self.run.run_id)
            if step.workflow_step_id == previous.workflow_step_id)
        self.update(step=step.model_copy(update={"a2a_task_id": "different-task"}))
        for role in self.last_reports:
            self.assert_denied(role, lambda: self.load(role))

    def test_services_cannot_omit_actual_existing_report_predecessor(self):
        self.advance()
        for role in self.last_reports:
            model = QAReportArtifact if role is AgentRole.QA else SecurityReportArtifact
            execution = self.context_for(role, previous_report=None)
            with self.assertRaises(ValueError):
                verify_report_predecessor(self.repository, execution, role, model)


if __name__ == "__main__":
    unittest.main()
