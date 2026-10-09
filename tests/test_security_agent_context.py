"""Frozen initial Security context and actual SQLite validation handoff."""

import asyncio
from dataclasses import FrozenInstanceError
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.security_context import (
    SecurityContextError, SecurityExecutionContext, SQLiteSecurityContextLoader,
)
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_snapshot_handoff_data
from orchestrator.application.a2a_tasks import TaskRunDisposition
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.domain.snapshot_handoff import SnapshotHandoff
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus

from test_qa_context import _QAContextFixture


class _SecurityContextFixture(_QAContextFixture):
    def setUp(self):
        super().setUp()
        # The same Developer candidate atomically creates both validation
        # Steps. Security selects its own Step and only its assigned subset.
        self.step = next(step for step in self.repository.list_steps(self.run.run_id)
                         if step.agent_role is AgentRole.SECURITY)
        self.metadata = A2AWorkflowMetadata(
            run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=self.step.attempt,
            requirement_ids=tuple(self.step.requirement_ids), code_version=1,
            project_artifact_ids=(self.source.artifact_id,),
        )
        self.resolved = []
        def resolve(configuration):
            self.resolved.append(configuration)
            return self.budget
        self.loader = SQLiteSecurityContextLoader(self.repository, resolve)

    def context(self, **changes):
        values = {"metadata": self.metadata, "configuration": self.configuration,
                  "budget": self.budget, "request_text": self.run.request_text,
                  "requirement_artifact": self.artifact, "source": self.source}
        values.update(changes)
        return SecurityExecutionContext(**values)

    def assert_invalid(self, operation, *arguments, **keywords):
        with self.assertRaises(SecurityContextError) as caught:
            operation(*arguments, **keywords)
        self.assertEqual(str(caught.exception), "SECURITY_CONTEXT_INVALID")
        self.assertNotIn("private-request", repr(caught.exception))
        return caught.exception


class SecurityAgentContextTests(_SecurityContextFixture, unittest.TestCase):
    def test_constructor_inert_frozen_repr_and_exact_snapshot_payload(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("no constructor I/O")):
            context = self.context(request_text="password=private-request")
        self.assertNotIn("private-request", repr(context))
        self.assertNotIn("fake-model", repr(context))
        self.assertNotIn(self.source.repository_id, repr(context))
        self.assertEqual(set(context.initial_payload), {"request", "snapshot"})
        self.assertEqual(context.initial_payload["snapshot"], {
            "projectArtifactId": str(self.source.artifact_id),
            "artifactUri": self.source.artifact_uri, "sourceAccess": "READ_ONLY",
            "executionManifest": self.source.execution_manifest().model_dump(mode="json", by_alias=True),
        })
        self.assertIs(context.model, self.configuration.configuration.model)
        self.assertIs(context.budget, self.budget)
        with self.assertRaises(FrozenInstanceError):
            context.request_text = "mutated"

    def test_exact_existing_orchestrator_dispatch_request_not_a_new_contract(self):
        client = SimpleNamespace(resolve_agent_card=AsyncMock())
        class Manager:
            async def __aenter__(self):
                return client
            async def __aexit__(self, *arguments):
                return None
        dispatcher = PlannerRunDispatcher(self.repository, None, client_factory=lambda _: Manager())
        captured = []
        async def submit(*arguments, **keywords):
            captured.append(keywords["request_text"])
            return SimpleNamespace(disposition=TaskRunDisposition.COMPLETED)
        with patch("orchestrator.application.dispatch.A2ATaskRunner.submit_snapshot_and_wait", side_effect=submit), \
             patch.object(dispatcher, "consume_validation_results", new=AsyncMock()):
            asyncio.run(dispatcher._dispatch_validation_agents(
                self.run, (self.step,), SnapshotHandoff.from_snapshot(self.source),
                agent_urls={AgentRole.SECURITY: "http://security.invalid"},
                plan=PlannerPlan.model_validate(self.plan_payload), scenario=self.context().scenario,
            ))
        self.assertEqual(self.context().initial_payload, build_snapshot_handoff_data(
            SnapshotHandoff.from_snapshot(self.source), AgentRole.SECURITY, captured[0]))

    def test_security_requirement_subset_and_frozen_policies_preserved(self):
        context = self.context()
        request = context.initial_payload["request"]
        self.assertIn("passwordHashPolicy", request)
        self.assertIn("canonicalEmailUniqueConstraint", request)
        self.assertIn("READ_ONLY", request)
        self.assertNotIn("frozenScenarioContractJson", request)
        self.assertIn("Task COMPLETED는 업무 완료일 뿐 결과 PASS를 뜻하지 않는다.", request)
        self.assertNotEqual(context.metadata.requirement_ids, self.artifact.requirement_ids)
        for requirement in context.scenario.requirements:
            if RequirementValidator.SECURITY in requirement.validators:
                self.assertIn(str(requirement.requirement_id), request)
            else:
                self.assertNotIn(str(requirement.requirement_id), request)

    def test_initial_payload_detached_from_caller_and_requirement_mutation(self):
        context = self.context()
        expected = context.initial_payload
        context.initial_payload["snapshot"]["sourceAccess"] = "WRITE"
        self.artifact.payload["requirements"][0]["description"] = "weakened original"
        self.assertEqual(context.initial_payload, expected)

    def test_metadata_exact_security_ids_initial_version_source_and_ownership(self):
        for changes in ({"run_id": uuid4()}, {"scenario_id": uuid4()}, {"code_version": 2},
                        {"code_version": None}, {"requirement_ids": None},
                        {"requirement_ids": self.artifact.requirement_ids},
                        {"requirement_ids": self.metadata.requirement_ids[:-1]},
                        {"project_artifact_ids": ()}, {"project_artifact_ids": (uuid4(),)}):
            with self.subTest(changes=changes):
                self.assert_invalid(self.context, metadata=self.metadata.model_copy(update=changes))

    def test_qa_requirement_assignment_cannot_become_security_scope(self):
        qa_ids = self.context().scenario.requirement_ids_for(RequirementValidator.QA)
        self.assertNotEqual(qa_ids, self.metadata.requirement_ids)
        self.assert_invalid(self.context, metadata=self.metadata.model_copy(update={"requirement_ids": qa_ids}))

    def test_source_references_environment_and_full_requirements_must_be_real(self):
        for changes in ({"run_id": uuid4()}, {"workflow_step_id": self.step.workflow_step_id},
                        {"requirement_ids": self.metadata.requirement_ids}, {"code_version": 2},
                        {"a2a_task_id": None, "a2a_artifact_id": None},
                        {"artifact_uri": "artifact://other/source.tar"},
                        {"container_image_digest": "sha256:" + "f" * 64},
                        {"dependency_lock_hash": "sha256:" + "f" * 64},
                        {"artifact_version": 2, "previous_artifact_id": uuid4()}):
            with self.subTest(changes=changes):
                self.assert_invalid(self.context, source=self.source.model_copy(update=changes))

    def test_canonical_planner_requirements_and_dag_cannot_be_weakened(self):
        for field, value in (("description", "weakened"), ("description", " padded "),
                             ("acceptanceCriteria", ["weakened"]), ("key", "unknown")):
            payload = json.loads(json.dumps(self.plan_payload))
            payload["requirements"][0][field] = value
            self.assert_invalid(self.context, requirement_artifact=self.artifact.model_copy(update={"payload": payload}))
        payload = json.loads(json.dumps(self.plan_payload))
        payload["implementationPlan"][0]["dependsOn"] = ["TASK-SIGNUP"]
        self.assert_invalid(self.context, requirement_artifact=self.artifact.model_copy(update={"payload": payload}))

    def test_missing_model_environment_budget_and_allowlist_never_defaulted(self):
        for changes in ({"model": None}, {"environment": None}, {"limits": {}},
                        {"environment": {**self.configuration.configuration.environment.model_dump(),
                                         "network_policy": "ALLOWLIST", "allowed_hosts": ["example.invalid"]}}):
            configuration = RunConfigurationArtifact.model_validate({
                **self.configuration.model_dump(),
                "configuration": {**self.configuration.configuration.model_dump(), **changes},
            })
            self.assert_invalid(self.context, configuration=configuration)

    def test_shared_usage_deadline_preserved_and_exhaustion_is_budget_failure(self):
        self.budget.reserve_model_call()
        self.budget.reserve_tool_call()
        deadline = self.budget.deadline_monotonic
        self.context()
        self.assertEqual((self.budget.model_calls, self.budget.tool_calls), (1, 1))
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assert_invalid(self.context, budget=ExecutionBudget(runtime_budget_ms=20000))
        self.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.context()
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    def test_missing_frozen_scenario_policy_never_uses_live_registry(self):
        with patch("orchestrator.domain.scenario_registry.get_scenario") as registry:
            for value in ("", None, True):
                self.assert_invalid(self.context, configuration=self.configuration.model_copy(
                    update={"frozen_scenario_contract_json": value}))
            for field in ("emailPolicy", "securityPolicy"):
                frozen = self.configuration.scenario_contract
                frozen.pop(field)
                self.assert_invalid(self.context, configuration=self.configuration.model_copy(
                    update={"frozen_scenario_contract_json": json.dumps(frozen)}))
            registry.assert_not_called()

    def test_known_secrets_and_invalid_objects_denied_without_echo(self):
        payload = json.loads(json.dumps(self.plan_payload))
        payload["implementationPlan"][0]["description"] = "password=protected-private-secret"
        error = self.assert_invalid(self.context, requirement_artifact=self.artifact.model_copy(update={"payload": payload}))
        self.assertNotIn("protected-private-secret", repr(error))
        self.assert_invalid(self.context, source=None)
        self.assert_invalid(self.context, requirement_artifact=None)
        self.assert_invalid(self.context, budget=None)


class SQLiteSecurityContextLoaderTests(_SecurityContextFixture, unittest.TestCase):
    def test_constructor_inert_real_handoff_readonly_shared_existing_budget(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("constructor I/O")):
            loader = SQLiteSecurityContextLoader(self.repository, lambda _: self.budget)
        self.assertEqual(repr(loader), "SQLiteSecurityContextLoader()")
        before = self.repository.database_path.read_bytes()
        context = self.loader(self.request())
        self.assertEqual(self.repository.database_path.read_bytes(), before)
        self.assertEqual(context.source, self.source)
        self.assertEqual(context.requirement_artifact, self.artifact)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.initial_payload, self.context().initial_payload)
        self.assertEqual(self.resolved, [self.configuration])

    def test_task_observer_race_and_opaque_ids_preserved(self):
        request = self.request(task_id="security/task?!%+", context_id="security/context?!%+")
        self.loader(request)
        self.update(step=self.step.model_copy(update={"a2a_task_id": request.task_id,
                                                      "agent_context_id": request.context_id}))
        self.loader(request)
        self.assert_invalid(self.loader, self.request(task_id="different-task", context_id=request.context_id))
        self.assert_invalid(self.loader, self.request(task_id=request.task_id, context_id="different-context"))

    def test_continuation_attempt_increment_does_not_change_code_or_reset_budget(self):
        self.budget.reserve_model_call()
        self.update(step=self.step.model_copy(update={"attempt": 1}))
        context = self.loader(self.request(metadata=self.metadata.model_copy(update={"attempt": 1})))
        self.assertEqual(context.metadata.code_version, 1)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.budget.model_calls, 1)
        self.assert_invalid(self.loader, self.request())

    def test_noninitial_terminal_and_wrong_active_step_denied(self):
        for changes in ({"status": WorkflowStepStatus.SUCCEEDED}, {"status": WorkflowStepStatus.WAITING_INPUT},
                        {"agent_role": AgentRole.QA}, {"code_version": 2},
                        {"input_artifact_ids": [uuid4()]}, {"output_artifact_ids": [uuid4()]},
                        {"a2a_artifact_ids": ["unexpected-result"]}):
            self.update(step=self.step.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())
        self.update(step=self.step)
        for changes in ({"status": WorkflowStatus.REVALIDATING, "fix_attempt": 1, "code_version": 2},
                        {"status": WorkflowStatus.FIXING, "fix_attempt": 1},
                        {"status": WorkflowStatus.ABORTED, "termination_reason": "USER_CANCELLED"}):
            self.update(run=self.run.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())

    def test_duplicate_running_security_step_denied(self):
        from orchestrator.domain.models import WorkflowStep
        from orchestrator.infrastructure.sqlite_workflows import _insert_step
        duplicate = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.SECURITY,
                                 status=WorkflowStepStatus.RUNNING,
                                 requirement_ids=self.step.requirement_ids, code_version=1,
                                 input_artifact_ids=[self.source.artifact_id])
        with self.repository._transaction() as connection:
            _insert_step(connection, duplicate)
        self.assert_invalid(self.loader, self.request())

    def test_developer_producer_completed_owns_source_and_preserves_requirements(self):
        for changes in ({"status": WorkflowStepStatus.RUNNING}, {"a2a_task_state": A2ATaskState.FAILED},
                        {"a2a_task_id": "wrong-developer-task"}, {"a2a_artifact_ids": []},
                        {"output_artifact_ids": []}, {"code_version": 2},
                        {"requirement_ids": list(self.metadata.requirement_ids)}, {"input_artifact_ids": []}):
            self.update(step=self.developer.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())

    def test_planner_producer_must_own_completed_canonical_requirement(self):
        for changes in ({"status": WorkflowStepStatus.RUNNING}, {"a2a_task_state": A2ATaskState.FAILED},
                        {"a2a_task_id": "wrong-planner-task"}, {"a2a_artifact_ids": []},
                        {"output_artifact_ids": []}):
            self.update(step=self.planner.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())

    def test_registry_source_metadata_must_match_private_measured_source(self):
        source = self.source.model_copy(update={"commit_hash": "c" * 40})
        self.rewrite_artifact(source)
        self.assert_invalid(self.loader, self.request())

    def test_private_source_bytes_hash_and_security_read_grant_are_checked(self):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER artifact_contents_no_update")
            connection.execute("UPDATE artifact_contents SET content=?,size_bytes=? WHERE artifact_id=?",
                (b"tampered private bytes", len(b"tampered private bytes"), str(self.source.artifact_id)))
        self.assert_invalid(self.loader, self.request())

    def test_missing_security_grant_does_not_infer_access_from_handoff(self):
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=? AND role='SECURITY'",
                (str(self.source.artifact_id),))
        self.assert_invalid(self.loader, self.request())

    def test_missing_private_store_denied_without_installing_or_writing_schema(self):
        # A registry reference alone is not executable/measured Source content.
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER snapshot_read_grants_no_delete")
            connection.execute("DROP TRIGGER artifact_contents_no_delete")
            connection.execute("DELETE FROM snapshot_read_grants WHERE artifact_id=?", (str(self.source.artifact_id),))
            connection.execute("DELETE FROM artifact_contents WHERE artifact_id=?", (str(self.source.artifact_id),))
        before = self.repository.database_path.read_bytes()
        self.assert_invalid(self.loader, self.request())
        self.assertEqual(self.repository.database_path.read_bytes(), before)

    def test_loader_missing_frozen_baseline_never_consults_registry(self):
        payload = self.configuration.model_dump()
        payload["frozen_scenario_contract_json"] = ""
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER run_configurations_no_update")
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?",
                (json.dumps(payload, default=str), str(self.run.run_id)))
        with patch("orchestrator.domain.scenario_registry.get_scenario") as registry:
            self.assert_invalid(self.loader, self.request())
            registry.assert_not_called()

    def test_loader_changed_canonical_requirement_is_denied(self):
        payload = json.loads(json.dumps(self.plan_payload))
        payload["requirements"][0]["acceptanceCriteria"] = ["weakened private baseline"]
        self.rewrite_artifact(self.artifact.model_copy(update={"payload": payload}))
        self.assert_invalid(self.loader, self.request())

    def test_column_status_and_payload_status_mismatch_denied(self):
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET status='SUCCEEDED' WHERE workflow_step_id=?",
                (str(self.step.workflow_step_id),))
        self.assert_invalid(self.loader, self.request())

    def test_metadata_mismatch_null_budget_and_exhaustion_never_start_new_budget(self):
        for changes in ({"run_id": uuid4()}, {"workflow_step_id": uuid4()}, {"scenario_id": uuid4()},
                        {"requirement_ids": ()}, {"project_artifact_ids": ()}):
            self.assert_invalid(self.loader, self.request(metadata=self.metadata.model_copy(update=changes)))
        self.assert_invalid(SQLiteSecurityContextLoader(self.repository, lambda _: None), self.request())
        request = self.request()
        self.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.loader(request)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    def test_qa_step_or_requirement_subset_cannot_be_used_for_security(self):
        qa = next(step for step in self.repository.list_steps(self.run.run_id)
                  if step.agent_role is AgentRole.QA)
        qa_ids = tuple(qa.requirement_ids)
        for changes in ({"workflow_step_id": qa.workflow_step_id}, {"requirement_ids": qa_ids}):
            self.assert_invalid(self.loader, self.request(metadata=self.metadata.model_copy(update=changes)))


if __name__ == "__main__":
    unittest.main()
