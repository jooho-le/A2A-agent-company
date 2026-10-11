"""Initial Developer context/real SQLite handoff; no LLM, MCP or Source execution."""

from dataclasses import FrozenInstanceError
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext

from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.developer_context import (
    DeveloperContextError, DeveloperExecutionContext, SQLiteDeveloperContextLoader,
)
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.application.dispatch import _DEVELOPER_OUTPUT_CONTRACT
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SCN_001_ID
from orchestrator.domain.states import A2ATaskState, AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class _DeveloperContextFixture:
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-developer-context-")
        self.addCleanup(temporary.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(temporary.name) / "workflow.sqlite3")
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 기능 구현",
                          status=WorkflowStatus.PLANNING)
        self.configuration = RunConfigurationArtifact(
            run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
            configuration={"model": {"provider": "fake", "modelId": "fake-model", "temperature": 0},
                           "limits": {"runtimeBudgetMs": 10000},
                           "environment": {"containerImageDigest": "sha256:" + "a" * 64,
                                           "dependencyLockHash": "sha256:" + "b" * 64,
                                           "hardwareProfile": "fixture", "networkPolicy": "DENY"}},
        )
        self.planner = WorkflowStep(
            run_id=run.run_id, agent_role=AgentRole.PLANNER, status=WorkflowStepStatus.SUCCEEDED,
            a2a_task_state=A2ATaskState.COMPLETED, a2a_task_id="planner/task?!%+ password=opaque",
            a2a_artifact_ids=["planner/artifact?!%+"],
        )
        baseline = self.configuration.scenario_contract
        self.plan_payload = {
            "schemaVersion": 1,
            "requirements": [{key: requirement[key] for key in (
                "requirementId", "key", "description", "acceptanceCriteria",
            )} for requirement in baseline["requirements"]],
            "implementationPlan": [{"taskId": "TASK-SIGNUP", "title": "회원가입 구현",
                "description": "보호된 요구사항에 따라 회원가입 기능 구현",
                "requirementIds": [item["requirementId"] for item in baseline["requirements"]],
                "dependsOn": []}],
        }
        ids = [UUID(item["requirementId"]) for item in baseline["requirements"]]
        self.repository.create_run(run, (self.planner,), (), run_configuration=self.configuration)
        self.run, self.step = self.repository.create_developer_step_from_plan(
            run.run_id, self.planner.workflow_step_id,
            a2a_artifact_id=self.planner.a2a_artifact_ids[0], requirement_ids=ids,
            project_artifact_id=uuid4(), developer_configured=True, requirement_payload=self.plan_payload,
        )
        self.planner = self.repository.list_steps(run.run_id)[0]
        self.artifact = self.repository.get_planning_artifact(run.run_id)
        self.metadata = A2AWorkflowMetadata(
            run_id=run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=run.scenario_id, attempt=self.step.attempt, requirement_ids=tuple(ids),
            code_version=1, project_artifact_ids=tuple(self.step.input_artifact_ids),
        )
        self.budget = ExecutionBudget(runtime_budget_ms=10000)
        self.resolved = []
        def resolve(configuration):
            self.resolved.append(configuration)
            return self.budget
        self.loader = SQLiteDeveloperContextLoader(self.repository, resolve)

    def context(self, **changes):
        values = {"metadata": self.metadata, "configuration": self.configuration,
                  "budget": self.budget, "request_text": self.run.request_text,
                  "requirement_artifact": self.artifact}
        values.update(changes)
        return DeveloperExecutionContext(**values)

    def request(self, *, metadata=None, task_id="developer/task?!%+", context_id="developer/context?!%+"):
        return RequestContext(
            call_context=ServerCallContext(state={}),
            request=build_send_message_request(self.context().initial_payload, metadata or self.metadata),
            task_id=task_id, context_id=context_id,
        )

    def assert_invalid(self, operation, *arguments, **keywords):
        with self.assertRaises(DeveloperContextError) as caught:
            operation(*arguments, **keywords)
        self.assertEqual(str(caught.exception), "DEVELOPER_CONTEXT_INVALID")
        return caught.exception

    def update(self, *, run=None, step=None):
        with self.repository._transaction() as connection:
            if run is not None:
                connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                    (run.status.value, run.model_dump_json(), str(run.run_id)))
            if step is not None:
                connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                    (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))


class DeveloperContextTests(_DeveloperContextFixture, unittest.TestCase):
    def test_constructor_inert_safe_repr_and_existing_six_field_contract(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("no I/O")):
            context = self.context(request_text="password=private-request")
        self.assertNotIn("private-request", repr(context))
        self.assertNotIn("fake-model", repr(context))
        self.assertEqual(set(context.initial_payload), {
            "workspaceId", "scenario", "runConfiguration", "plan", "sourceArtifact", "outputContract",
        })
        self.assertEqual(context.initial_payload["plan"], self.plan_payload)
        self.assertEqual(context.initial_payload["outputContract"], _DEVELOPER_OUTPUT_CONTRACT)
        self.assertEqual(context.initial_payload["sourceArtifact"], {
            "a2aArtifactId": self.artifact.a2a_artifact_id,
            "projectArtifactId": str(self.artifact.artifact_id), "artifactVersion": 1,
        })
        self.assertIs(context.model, self.configuration.configuration.model)
        self.assertIs(context.budget, self.budget)
        with self.assertRaises(FrozenInstanceError):
            context.request_text = "changed"

    def test_plan_and_initial_payload_are_detached_from_caller_mutation(self):
        context = self.context()
        context.initial_payload["outputContract"]["schemaVersion"] = 999
        context.plan.requirements[0].description = "changed copy"
        self.artifact.payload["requirements"][0]["description"] = "changed original"
        self.assertEqual(context.plan.requirements[0].description,
                         context.scenario.requirements[0].description)
        self.assertEqual(context.initial_payload["outputContract"]["schemaVersion"], 1)

    def test_metadata_requires_exact_initial_code_requirements_and_artifact_reference(self):
        for changes in ({"run_id": uuid4()}, {"scenario_id": uuid4()}, {"code_version": 2},
                        {"code_version": None}, {"requirement_ids": None},
                        {"requirement_ids": self.metadata.requirement_ids[:-1]},
                        {"project_artifact_ids": None}, {"project_artifact_ids": (uuid4(),)}):
            with self.subTest(changes=changes):
                self.assert_invalid(self.context, metadata=self.metadata.model_copy(update=changes))

    def test_missing_model_environment_limit_and_allowlist_are_not_invented(self):
        for changes in ({"model": None}, {"environment": None}, {"limits": {}},
                        {"environment": {**self.configuration.configuration.environment.model_dump(),
                                         "network_policy": "ALLOWLIST", "allowed_hosts": ["example.invalid"]}}):
            data = {**self.configuration.configuration.model_dump(), **changes}
            configuration = RunConfigurationArtifact.model_validate({
                **self.configuration.model_dump(), "configuration": data,
            })
            self.assert_invalid(self.context, configuration=configuration)

    def test_requirement_payload_must_preserve_canonical_requirements_and_valid_dag(self):
        for field, value in (("description", "weakened requirement"), ("description", " padded "),
                             ("acceptanceCriteria", ["weakened"]), ("key", "arbitrary")):
            payload = self.context().initial_payload["plan"]
            payload["requirements"][0][field] = value
            artifact = self.artifact.model_copy(update={"payload": payload})
            self.assert_invalid(self.context, requirement_artifact=artifact)
        payload = self.context().initial_payload["plan"]
        payload["implementationPlan"][0]["dependsOn"] = ["TASK-SIGNUP"]
        self.assert_invalid(self.context, requirement_artifact=self.artifact.model_copy(update={"payload": payload}))

    def test_shared_budget_deadline_usage_and_exhaustion_keep_their_meaning(self):
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

    def test_missing_frozen_baseline_never_falls_back_to_registry(self):
        with patch("orchestrator.domain.scenario_registry.get_scenario") as registry:
            for value in ("", None, True):
                self.assert_invalid(self.context, configuration=self.configuration.model_copy(
                    update={"frozen_scenario_contract_json": value}))
            registry.assert_not_called()

    def test_credentials_in_protected_plan_are_denied_without_echoing_literals(self):
        payload = self.context().initial_payload["plan"]
        payload["implementationPlan"][0]["description"] = "password=protected-fixture-secret"
        error = self.assert_invalid(self.context, requirement_artifact=self.artifact.model_copy(update={"payload": payload}))
        self.assertNotIn("protected-fixture-secret", repr(error))
        self.assert_invalid(self.context, requirement_artifact=None)


class SQLiteDeveloperContextLoaderTests(_DeveloperContextFixture, unittest.TestCase):
    def test_real_handoff_loader_is_inert_readonly_and_preserves_shared_budget(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("constructor I/O")):
            loader = SQLiteDeveloperContextLoader(self.repository, lambda _: self.budget)
        self.assertEqual(repr(loader), "SQLiteDeveloperContextLoader()")
        before = self.repository.database_path.read_bytes()
        context = self.loader(self.request())
        self.assertEqual(self.repository.database_path.read_bytes(), before)
        self.assertEqual(context.requirement_artifact, self.artifact)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.initial_payload, self.context().initial_payload)
        self.assertEqual(self.resolved, [self.configuration])

    def test_task_observer_race_and_existing_opaque_ids_are_not_rewritten(self):
        request = self.request()
        self.loader(request)
        self.update(step=self.step.model_copy(update={"a2a_task_id": request.task_id,
                                                      "agent_context_id": request.context_id}))
        self.loader(request)
        self.assert_invalid(self.loader, self.request(task_id="different-task"))
        self.assert_invalid(self.loader, self.request(context_id="different-context"))

    def test_continuation_attempt_can_increment_without_new_code_version(self):
        self.budget.reserve_model_call()
        self.update(step=self.step.model_copy(update={"attempt": 1}))
        metadata = self.metadata.model_copy(update={"attempt": 1})
        context = self.loader(self.request(metadata=metadata))
        self.assertEqual(context.metadata.code_version, 1)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.budget.model_calls, 1)
        self.assert_invalid(self.loader, self.request())

    def test_fixing_terminal_wrong_role_and_wrong_input_refs_are_denied(self):
        for changes in ({"status": WorkflowStepStatus.SUCCEEDED}, {"status": WorkflowStepStatus.WAITING_INPUT},
                        {"agent_role": AgentRole.QA}, {"code_version": 2},
                        {"input_artifact_ids": [uuid4()]}, {"output_artifact_ids": [uuid4()]}):
            self.update(step=self.step.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())
        self.update(step=self.step)
        self.update(run=self.run.model_copy(update={"status": WorkflowStatus.FIXING,
                                                   "fix_attempt": 1, "code_version": 1}))
        self.assert_invalid(self.loader, self.request())
        self.update(run=self.run.model_copy(update={"status": WorkflowStatus.ABORTED,
                                                   "termination_reason": "USER_CANCELLED"}))
        self.assert_invalid(self.loader, self.request())

    def test_planner_producer_must_be_completed_and_own_exact_task_and_artifact(self):
        for changes in ({"status": WorkflowStepStatus.RUNNING}, {"a2a_task_state": A2ATaskState.FAILED},
                        {"a2a_task_id": "different-planner-task"}, {"a2a_artifact_ids": []},
                        {"output_artifact_ids": []}):
            self.update(step=self.planner.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())

    def test_wrong_metadata_null_budget_and_exhaustion_do_not_start_new_execution(self):
        for changes in ({"run_id": uuid4()}, {"workflow_step_id": uuid4()}, {"scenario_id": uuid4()},
                        {"requirement_ids": ()}, {"project_artifact_ids": ()}):
            self.assert_invalid(self.loader, self.request(metadata=self.metadata.model_copy(update=changes)))
        self.assert_invalid(SQLiteDeveloperContextLoader(self.repository, lambda _: None), self.request())
        request = self.request()
        self.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.loader(request)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)


if __name__ == "__main__":
    unittest.main()
