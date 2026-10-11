"""Frozen Planner Host context/SQLite reader; no provider, MCP or Source execution."""

from dataclasses import FrozenInstanceError
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
from uuid import uuid1, uuid4

from a2a.server.agent_execution import RequestContext
from a2a.server.context import ServerCallContext

from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.planner_context import (
    PlannerContextError, PlannerExecutionContext, SQLitePlannerContextLoader,
)
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.domain.models import WorkflowRun, WorkflowStep
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SCN_001_ID
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository


class _PlannerContextFixture:
    def setUp(self):
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 기능 구현",
                               status=WorkflowStatus.PLANNING)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER,
                                 status=WorkflowStepStatus.RUNNING)
        self.configuration = RunConfigurationArtifact(
            run_id=self.run.run_id, scenario_id=self.run.scenario_id, workspace_id=self.run.workspace_id,
            configuration={"model": {"provider": "fake", "modelId": "fake-model", "temperature": 0},
                           "limits": {"runtimeBudgetMs": 10000}},
        )
        self.metadata = A2AWorkflowMetadata(
            run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=0,
        )
        self.budget = ExecutionBudget(runtime_budget_ms=10000)

    def context(self, **changes):
        values = {"metadata": self.metadata, "configuration": self.configuration,
                  "budget": self.budget, "request_text": self.run.request_text}
        values.update(changes)
        return PlannerExecutionContext(**values)

    def altered_configuration(self, **changes):
        return RunConfigurationArtifact.model_validate({**self.configuration.model_dump(), **changes})

    def assert_invalid(self, operation, *arguments, **keywords):
        with self.assertRaises(PlannerContextError) as caught:
            operation(*arguments, **keywords)
        self.assertEqual(str(caught.exception), "PLANNER_CONTEXT_INVALID")
        return caught.exception


class PlannerContextTests(_PlannerContextFixture, unittest.TestCase):
    def test_constructor_is_inert_frozen_and_request_repr_is_safe(self):
        with patch.object(SQLiteWorkflowRepository, "_connection", side_effect=AssertionError("no I/O")):
            context = self.context(request_text="password=host-request-secret")
        self.assertNotIn("host-request-secret", repr(context))
        self.assertNotIn("fake-model", repr(context))
        self.assertIs(context.budget, self.budget)
        self.assertIs(context.model, self.configuration.configuration.model)
        self.assertEqual(context.project_artifact_id.version, 4)
        with self.assertRaises(FrozenInstanceError):
            context.artifact_version = 2

    def test_scenario_is_detached_and_frozen_after_registry_changes(self):
        context = self.context()
        expected = self.configuration.scenario_contract
        with patch("orchestrator.domain.scenario_registry.get_scenario", side_effect=AssertionError("frozen only")):
            recreated = self.context()
            first, second = recreated.scenario, recreated.scenario
        self.assertIsNot(first, second)
        self.assertEqual(first.planner_contract(), expected)
        self.assertEqual(second.requirement_ids, context.scenario.requirement_ids)

    def test_identity_source_and_requirement_metadata_are_checked(self):
        for changes in ({"run_id": uuid4()}, {"scenario_id": uuid4()}, {"code_version": 1},
                        {"project_artifact_ids": (uuid4(),)}, {"requirement_ids": (uuid4(),)}):
            with self.subTest(changes=changes):
                metadata = A2AWorkflowMetadata.model_validate({**self.metadata.model_dump(), **changes})
                self.assert_invalid(self.context, metadata=metadata)
        for requirements in (None, (), self.context().scenario.requirement_ids):
            self.context(metadata=self.metadata.model_copy(update={"requirement_ids": requirements}))

    def test_missing_model_or_frozen_runtime_limit_is_not_invented(self):
        for changes in ({"model": None}, {"limits": {}}):
            config = {**self.configuration.configuration.model_dump(), **changes}
            self.assert_invalid(self.context, configuration=self.altered_configuration(configuration=config))
        for field in ("configuration", "metadata", "budget"):
            self.assert_invalid(self.context, **{field: None})

    def test_artifact_identity_version_and_blank_request_are_strict(self):
        for changes in ({"project_artifact_id": uuid1()}, {"project_artifact_id": str(uuid4())},
                        {"artifact_version": True}, {"artifact_version": 2},
                        {"artifact_version": 1.0}, {"request_text": "  "}):
            with self.subTest(changes=changes):
                self.assert_invalid(self.context, **changes)

    def test_existing_budget_usage_is_preserved_and_deadline_cannot_expand(self):
        self.budget.reserve_model_call()
        self.budget.reserve_tool_call()
        deadline = self.budget.deadline_monotonic
        self.context()
        self.assertEqual((self.budget.model_calls, self.budget.tool_calls), (1, 1))
        self.assertEqual(self.budget.deadline_monotonic, deadline)
        self.assert_invalid(self.context, budget=ExecutionBudget(runtime_budget_ms=20000))

    def test_exhausted_shared_budget_is_budget_failure_not_admission_error(self):
        self.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.context()
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    def test_known_credentials_in_model_or_frozen_scenario_are_rejected(self):
        model = {**self.configuration.configuration.model.model_dump(), "model_id": "password=protected-secret"}
        config = {**self.configuration.configuration.model_dump(), "model": model}
        error = self.assert_invalid(self.context, configuration=self.altered_configuration(configuration=config))
        self.assertNotIn("protected-secret", repr(error))
        scenario = self.configuration.scenario_contract
        scenario["emailPolicy"]["apiKey"] = "protected-secret"
        self.assert_invalid(self.context, configuration=self.altered_configuration(
            frozen_scenario_contract_json=json.dumps(scenario)))

    def test_malformed_or_noncanonical_frozen_baseline_fails_before_provider(self):
        alterations = (
            lambda data: data["requirements"][0].update(key="arbitrary"),
            lambda data: data["requirements"][0].update(description=" leading whitespace"),
            lambda data: data["requirements"][0].update(acceptanceCriteria=["Same", "same"]),
            lambda data: data["requirements"][0].update(acceptanceCriteria=[" padded "]),
            lambda data: data["requirements"].append(data["requirements"][0]),
            lambda data: data["requirements"][0].update(validators=["OTHER"]),
            lambda data: data.update(securityPolicy=[]),
            lambda data: data.update(excludedFeatures=[""]),
        )
        for index, alter in enumerate(alterations):
            data = self.configuration.scenario_contract
            alter(data)
            with self.subTest(index=index):
                self.assert_invalid(self.context, configuration=self.altered_configuration(
                    frozen_scenario_contract_json=json.dumps(data)))
        raw = self.configuration.frozen_scenario_contract_json
        duplicate = raw[:-1] + ',"name":"duplicate"}'
        self.assert_invalid(self.context, configuration=self.altered_configuration(
            frozen_scenario_contract_json=duplicate))

    def test_context_bypass_cannot_refreeze_missing_empty_or_nonstring_baseline(self):
        variants = [self.configuration.model_copy(update={"frozen_scenario_contract_json": value})
                    for value in ("", " \t\n", None, True)]
        missing = self.configuration.model_copy()
        missing.__dict__.pop("frozen_scenario_contract_json")
        variants.append(missing)
        with patch("orchestrator.domain.scenario_registry.get_scenario") as registry:
            for index, configuration in enumerate(variants):
                with self.subTest(index=index):
                    self.assert_invalid(self.context, configuration=configuration)
            registry.assert_not_called()


class SQLitePlannerContextLoaderTests(_PlannerContextFixture, unittest.TestCase):
    # Reuse context setup without duplicating its test suite during discovery.
    def setUp(self):
        super().setUp()
        self.temporary = TemporaryDirectory(prefix="a2a-planner-context-")
        self.addCleanup(self.temporary.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(self.temporary.name) / "workflow.sqlite3")
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.configuration)
        self.resolved = []
        def resolve(configuration):
            self.resolved.append(configuration)
            return self.budget
        self.loader = SQLitePlannerContextLoader(self.repository, resolve)

    def request(self, *, metadata=None, task_id="planner/task?!%+", context_id="planner/context?!%+"):
        return RequestContext(
            call_context=ServerCallContext(state={}),
            request=build_send_message_request({"request": self.run.request_text}, metadata or self.metadata),
            task_id=task_id, context_id=context_id,
        )

    def update(self, *, run=None, step=None):
        with self.repository._transaction() as connection:
            if run is not None:
                connection.execute("UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?",
                    (run.status.value, run.model_dump_json(), str(run.run_id)))
            if step is not None:
                connection.execute("UPDATE workflow_steps SET status=?,payload_json=? WHERE workflow_step_id=?",
                    (step.status.value, step.model_dump_json(), str(step.workflow_step_id)))

    def test_loader_constructor_inert_and_actual_sqlite_read_is_nonmutating(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("constructor I/O")):
            loader = SQLitePlannerContextLoader(self.repository, lambda _: self.budget)
        self.assertEqual(repr(loader), "SQLitePlannerContextLoader()")
        before = self.repository.database_path.read_bytes()
        context = self.loader(self.request())
        self.assertEqual(self.repository.database_path.read_bytes(), before)
        self.assertEqual(context.configuration, self.configuration)
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.request_text, self.run.request_text)
        self.assertEqual(self.resolved, [self.configuration])

    def test_first_task_observer_race_and_exact_opaque_bound_ids(self):
        request = self.request()
        self.loader(request)  # No Task ID observed by Orchestrator yet.
        bound = self.step.model_copy(update={"a2a_task_id": request.task_id,
                                             "agent_context_id": request.context_id})
        self.update(step=bound)
        self.loader(request)
        self.assert_invalid(self.loader, self.request(task_id="different-task"))
        self.assert_invalid(self.loader, self.request(context_id="different-context"))

    def test_continuation_attempt_is_not_code_fix_attempt(self):
        self.budget.reserve_model_call()
        continued = self.step.model_copy(update={"attempt": 1})
        self.update(step=continued)
        metadata = self.metadata.model_copy(update={"attempt": 1})
        context = self.loader(self.request(metadata=metadata))
        self.assertIs(context.budget, self.budget)
        self.assertEqual(context.budget.model_calls, 1)
        self.assert_invalid(self.loader, self.request())

    def test_terminal_wrong_role_paused_and_source_contexts_are_denied(self):
        updates = ({"agent_role": AgentRole.DEVELOPER}, {"status": WorkflowStepStatus.WAITING_INPUT},
                   {"status": WorkflowStepStatus.SUCCEEDED}, {"code_version": 1},
                   {"input_artifact_ids": [uuid4()]})
        for changes in updates:
            self.update(step=self.step.model_copy(update=changes))
            with self.subTest(changes=changes):
                self.assert_invalid(self.loader, self.request())
        self.update(step=self.step)
        self.update(run=self.run.model_copy(update={"status": WorkflowStatus.ABORTED,
                                                   "termination_reason": "USER_CANCELLED"}))
        self.assert_invalid(self.loader, self.request())

    def test_missing_rows_wrong_identity_and_null_resolver_are_safe_errors(self):
        for changes in ({"run_id": uuid4()}, {"workflow_step_id": uuid4()}, {"scenario_id": uuid4()}):
            metadata = self.metadata.model_copy(update=changes)
            self.assert_invalid(self.loader, self.request(metadata=metadata))
        invalid_budget = SQLitePlannerContextLoader(self.repository, lambda _: None)
        self.assert_invalid(invalid_budget, self.request())
        self.assert_invalid(self.loader, None)

    def test_loader_does_not_recreate_exhausted_run_budget(self):
        self.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.loader(self.request())
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(self.resolved, [self.configuration])

    def replace_configuration_row(self, data):
        # Privileged fixture corruption only. Normal configuration rows are
        # immutable and the production Loader remains strictly read-only.
        with self.repository._transaction() as connection:
            connection.execute("DROP TRIGGER IF EXISTS run_configurations_no_update")
            connection.execute("UPDATE run_configurations SET payload_json=? WHERE run_id=?",
                (json.dumps(data), str(self.run.run_id)))

    def test_loader_requires_frozen_string_before_common_model_validation(self):
        for alias in (False, True):
            key = "frozenScenarioContractJson" if alias else "frozen_scenario_contract_json"
            for value in ("", " \t\n", None, True, "MISSING"):
                data = self.configuration.model_dump(mode="json", by_alias=alias)
                if value == "MISSING":
                    data.pop(key)
                else:
                    data[key] = value
                self.replace_configuration_row(data)
                with self.subTest(alias=alias, value=value), patch(
                    "orchestrator.domain.scenario_registry.get_scenario",
                ) as registry:
                    self.assert_invalid(self.loader, self.request())
                    registry.assert_not_called()

    def test_loader_accepts_each_frozen_alias_but_rejects_both_without_registry(self):
        with patch("orchestrator.domain.scenario_registry.get_scenario") as registry:
            for alias in (False, True):
                self.replace_configuration_row(self.configuration.model_dump(mode="json", by_alias=alias))
                self.assertEqual(self.loader(self.request()).scenario.planner_contract(),
                                 self.configuration.scenario_contract)
            data = self.configuration.model_dump(mode="json")
            data["frozenScenarioContractJson"] = self.configuration.frozen_scenario_contract_json
            self.replace_configuration_row(data)
            self.assert_invalid(self.loader, self.request())
            registry.assert_not_called()


if __name__ == "__main__":
    unittest.main()
