"""Shared Host admissions and atomic usage; no cloud or generated execution."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, TokenUsage
from agents.platform.budgets import RunBudgetError, RunBudgetRegistry
from orchestrator.domain import (
    AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import (
    ExecutionLimits, ModelConfiguration, RunConfiguration, RunConfigurationArtifact,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository


class RunBudgetRegistryTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-budget-registry-")
        self.addCleanup(temporary.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(temporary.name) / "workflow.sqlite3")
        self.limits = LLMLimits(max_model_calls=2, max_tool_calls=2, max_total_tokens=64)
        self.registry = RunBudgetRegistry(self.repository, limits=self.limits)

    def create(self, *, configuration=None, status=WorkflowStatus.PLANNING, **changes):
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="실행 예산 검사", status=status, **changes)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.RUNNING if status is WorkflowStatus.PLANNING else WorkflowStepStatus.PENDING)
        configuration = configuration or RunConfiguration(
            model=ModelConfiguration(provider="fake", model_id="fake-model", temperature=0),
            limits=ExecutionLimits(runtime_budget_ms=10000))
        artifact = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
            workspace_id=run.workspace_id, configuration=configuration)
        self.repository.create_run(run, (step,), (), run_configuration=artifact)
        return run, artifact

    def test_resolve_is_not_admission_and_does_not_create_budget(self):
        run, configuration = self.create()
        with self.assertRaises(RunBudgetError):
            self.registry.resolve(configuration)
        with self.assertRaises(RunBudgetError):
            self.registry.admit(uuid4())
        budget = self.registry.admit(run.run_id)
        self.assertIs(self.registry.resolve(configuration), budget)

    def test_roles_and_readmitted_run_keep_same_counters_and_deadline(self):
        run, configuration = self.create()
        budget = self.registry.admit(run.run_id)
        deadline = budget.deadline_monotonic
        budget.reserve_model_call()
        budget.reserve_tool_call()
        budget.account_usage(TokenUsage(input_tokens=10, output_tokens=10, total_tokens=20))
        for _role in AgentRole:
            resolved = self.registry.resolve(configuration)
            self.assertIs(resolved, budget)
            self.assertEqual((resolved.model_calls, resolved.tool_calls, resolved.total_tokens), (1, 1, 20))
        self.assertIs(self.registry.admit(run.run_id), budget)
        self.assertEqual(budget.deadline_monotonic, deadline)
        budget.reserve_model_call()
        with self.assertRaises(LLMRuntimeError) as raised:
            self.registry.resolve(configuration).reserve_model_call()
        self.assertEqual(raised.exception.code, LLMErrorCode.BUDGET)

    def test_restart_cannot_reconstruct_a_fresh_budget_for_existing_run(self):
        run, configuration = self.create()
        original = self.registry.admit(run.run_id)
        original.reserve_model_call()
        restarted = RunBudgetRegistry(self.repository, limits=self.limits)
        with self.assertRaises(RunBudgetError):
            restarted.resolve(configuration)
        with self.assertRaises(RunBudgetError):
            restarted.admit(run.run_id)
        self.assertEqual(original.model_calls, 1)

    def test_missing_model_or_runtime_limit_cannot_admit(self):
        for config in (
            RunConfiguration(),
            RunConfiguration(limits=ExecutionLimits(runtime_budget_ms=10000)),
            RunConfiguration(model=ModelConfiguration(provider="fake", model_id="fake-model", temperature=0)),
        ):
            with self.subTest(configuration=config):
                run, _ = self.create(configuration=config)
                with self.assertRaises(RunBudgetError):
                    self.registry.admit(run.run_id)

    def test_configuration_fingerprint_includes_ids_and_frozen_contents(self):
        run, configuration = self.create()
        self.registry.admit(run.run_id)
        variants = (
            configuration.model_copy(update={"artifact_id": uuid4()}),
            configuration.model_copy(update={"workspace_id": uuid4()}),
            configuration.model_copy(update={"frozen_scenario_contract_json": configuration.frozen_scenario_contract_json + " "}),
            configuration.model_copy(update={"configuration": RunConfiguration(
                model=configuration.configuration.model, limits=ExecutionLimits(runtime_budget_ms=20000))}),
        )
        for changed in variants:
            with self.subTest(field=changed.artifact_id), self.assertRaises(RunBudgetError):
                self.registry.resolve(changed)

    def test_configuration_changed_in_database_cannot_readmit_reset(self):
        run, configuration = self.create()
        budget = self.registry.admit(run.run_id)
        changed = configuration.model_copy(update={"artifact_id": uuid4()})
        # SQLite already forbids a normal rewrite. Simulate a corrupted reader
        # rather than weakening that trigger just to exercise Host admission.
        with patch.object(self.repository, "get_run_configuration", return_value=changed):
            with self.assertRaises(RunBudgetError):
                self.registry.admit(run.run_id)
        self.assertEqual(budget.model_calls, 0)

    def test_unclaimed_run_cannot_admit(self):
        run, _ = self.create(status=WorkflowStatus.RECEIVED)
        with self.assertRaises(RunBudgetError):
            self.registry.admit(run.run_id)
        self.repository.claim_planner_dispatch(run.run_id)
        self.assertEqual(self.registry.admit(run.run_id).model_calls, 0)

    def test_previous_a2a_identity_cannot_mint_new_budget(self):
        run, _ = self.create()
        step = self.repository.list_steps(run.run_id)[0]
        changed = step.model_copy(update={"a2a_task_id": "existing-opaque-task"})
        with self.repository._transaction() as connection:
            connection.execute("UPDATE workflow_steps SET payload_json=? WHERE workflow_step_id=?",
                (changed.model_dump_json(), str(step.workflow_step_id)))
        with self.assertRaises(RunBudgetError):
            self.registry.admit(run.run_id)

    def test_elapsed_queue_time_counts_toward_deadline(self):
        run, _ = self.create()
        with patch("agents.platform.budgets.datetime") as clock:
            clock.now.return_value = run.created_at + timedelta(seconds=2)
            budget = self.registry.admit(run.run_id)
        self.assertGreater(budget.remaining_seconds(), 7.8)
        self.assertLessEqual(budget.remaining_seconds(), 8)

    def test_expired_budget_and_unknown_tokens_never_reset_on_resolution(self):
        run, configuration = self.create()
        budget = self.registry.admit(run.run_id)
        with patch("agents.llm.budget.monotonic", return_value=budget.deadline_monotonic + 1):
            for action in (lambda: self.registry.resolve(configuration), lambda: self.registry.admit(run.run_id)):
                with self.assertRaises(LLMRuntimeError):
                    action()
        budget.account_usage(None)
        self.assertIsNone(budget.total_tokens)
        with self.assertRaises(LLMRuntimeError):
            self.registry.resolve(configuration)

    def test_future_or_expired_run_creation_fails_closed(self):
        run, _ = self.create()
        for offset in (-1, 11):
            with patch("agents.platform.budgets.datetime") as clock:
                clock.now.return_value = run.created_at + timedelta(seconds=offset)
                with self.assertRaises((RunBudgetError, LLMRuntimeError)):
                    self.registry.admit(run.run_id)

    def test_inert_configuration_and_safe_error_repr(self):
        with self.assertRaises(RunBudgetError):
            RunBudgetRegistry(object(), limits=self.limits)
        with self.assertRaises(RunBudgetError):
            RunBudgetRegistry(self.repository, limits=None)
        for run_id in (str(uuid4()), True, None):
            with self.assertRaises(RunBudgetError):
                self.registry.admit(run_id)
        self.assertEqual(repr(self.registry), "RunBudgetRegistry()")
        self.assertEqual(str(RunBudgetError()), "RUN_BUDGET_NOT_ADMITTED")


class AtomicBudgetTests(unittest.TestCase):
    def test_parallel_reservation_does_not_overrun_shared_caps(self):
        budget = ExecutionBudget(runtime_budget_ms=10000,
            limits=LLMLimits(max_model_calls=20, max_tool_calls=20))

        def reserve(_):
            try:
                budget.reserve_model_call()
            except LLMRuntimeError:
                return False
            budget.reserve_tool_call()
            budget.account_usage(TokenUsage(input_tokens=1, output_tokens=1, total_tokens=2))
            return True

        with ThreadPoolExecutor(max_workers=8) as workers:
            accepted = list(workers.map(reserve, range(100)))
        self.assertEqual(sum(accepted), 20)
        self.assertEqual((budget.model_calls, budget.tool_calls, budget.total_tokens), (20, 20, 40))
