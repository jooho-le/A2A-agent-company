"""Step 41 Host recipes on temporary SQLite, without launching transports.

Configuration/source digests are inert placeholders, never image readiness
or completed product evidence. Runtime service prepare/finalize are not run.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.composition import PlatformConfigurationError
from agents.platform.host_services import HostRuntimeServices
from agents.platform.starting_source import decode_source_configuration
from agents.platform.tool_configuration import decode_tool_configuration
from agents.roles.qa_contract import QACaseBinding
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.client import BoundMCPClient
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain import (
    AgentRole, SCN_001_ID, WorkflowRun, WorkflowStatus, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.run_configuration import RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry
from test_tool_configuration import configuration_data, protected_unit


class _HostFixture:
    def setUp(self):
        temporary = TemporaryDirectory(prefix="a2a-host-services-", dir="/tmp")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.repository = SQLiteWorkflowRepository(self.directory / "workflow.sqlite3")
        self.registry = WorkspaceRegistry(self.repository, self.directory / "workspaces")
        self.artifacts = ArtifactStore(self.repository, self.registry)
        self.tools = decode_tool_configuration(json.dumps(configuration_data()))
        self.source_configuration = decode_source_configuration(json.dumps({
            "schemaVersion": 1, "repositoryPath": "../inert-source",
            "repositoryId": "host-service-fixture", "commitHash": "a" * 40,
            "snapshotSha256": "b" * 64, "lockPath": "requirements.lock",
            "gitTimeoutSeconds": 30,
        }))
        self.validated, self.transport_events = [], []

        @asynccontextmanager
        async def inert_client(configuration):
            self.transport_events.append(("enter", asyncio.current_task(), configuration))
            try:
                yield BoundMCPClient(configuration=configuration, _client=object())
            finally:
                self.transport_events.append(("exit", asyncio.current_task(), configuration))

        self.client_factory = inert_client

    def validate(self, configuration, run_id):
        self.validated.append((configuration, run_id))

    def host(self, **changes):
        values = dict(tools=self.tools, source_configuration=self.source_configuration,
            validate_configuration=self.validate, client_factory=self.client_factory)
        values.update(changes)
        return HostRuntimeServices(self.repository, self.registry, self.artifacts, **values)

    def execution(self, role, *, budget=None, protected=False):
        validating = role in (AgentRole.QA, AgentRole.SECURITY)
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="inert Host binding fixture",
            status=WorkflowStatus.VALIDATING if validating else WorkflowStatus.IMPLEMENTING,
            code_version=1 if validating else None)
        input_id, requirement_id = uuid4(), uuid4()
        step = WorkflowStep(run_id=run.run_id, agent_role=role, status=WorkflowStepStatus.RUNNING,
            requirement_ids=[requirement_id], code_version=1, input_artifact_ids=[input_id])
        configuration = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
            workspace_id=run.workspace_id, configuration=RunConfiguration(
                starting_commit_hash=self.source_configuration.commit_hash,
                starting_snapshot_sha256=self.source_configuration.snapshot_sha256,
                model={"provider": "fake", "modelId": "fake-model", "temperature": 0},
                limits={"runtimeBudgetMs": 30000}, environment=self.tools.baseline,
                scanner_profile_ref=self.tools.security_configuration.profiles[0].profile_ref,
                protected_test_suite_ref=protected_unit()["protected_suite_ref"] if protected else None))
        workspace = WorkspaceRecord(workspace_id=run.workspace_id, run_id=run.run_id,
            root_path=str(self.registry.base_path / str(run.workspace_id)))
        self.repository.create_run(run, (step,), (), run_configuration=configuration, workspace=workspace)
        metadata = A2AWorkflowMetadata(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
            scenario_id=run.scenario_id, attempt=0, code_version=1,
            requirement_ids=(requirement_id,), project_artifact_ids=(input_id,))
        source = None if not validating else CodeSnapshotArtifact(
            artifact_id=input_id, artifact_version=1, run_id=run.run_id,
            workflow_step_id=uuid4(), a2a_task_id="inert-developer-task",
            a2a_artifact_id="inert-developer-source", requirement_ids=(requirement_id,),
            code_version=1, repository_id=self.source_configuration.repository_id,
            commit_hash="c" * 40, git_object_format="sha1", tree_hash="d" * 40,
            snapshot_sha256="e" * 64, artifact_uri=f"artifact://{input_id}/source.tar",
            container_image_digest=self.tools.baseline.container_image_digest,
            dependency_lock_hash=self.tools.baseline.dependency_lock_hash)
        return SimpleNamespace(metadata=metadata, configuration=configuration,
            budget=ExecutionBudget(runtime_budget_ms=30000) if budget is None else budget,
            source=source, step=step, run=run)

    def assert_invalid(self, operation, *args, **kwargs):
        with self.assertRaises(PlatformConfigurationError) as caught:
            operation(*args, **kwargs)
        self.assertEqual(str(caught.exception), "OWNED_AGENT_CONFIGURATION_INVALID")
        self.assertNotIn(str(self.directory), str(caught.exception))


class HostRuntimeServicesTests(_HostFixture, unittest.TestCase):
    def test_constructor_is_inert_and_private(self):
        with patch.object(self.repository, "_connection", side_effect=AssertionError("no constructor reads")), \
                patch.object(self.repository, "_transaction", side_effect=AssertionError("no constructor writes")), \
                patch("agents.platform.host_services.open_mcp_client", side_effect=AssertionError("no transport")):
            host = self.host()
        self.assertEqual(repr(host), "HostRuntimeServices()")
        self.assertFalse(self.registry.base_path.exists())
        self.assertEqual(self.validated, [])
        self.assertEqual(self.transport_events, [])

    def test_foreign_storage_capabilities_are_rejected(self):
        other = SQLiteWorkflowRepository(self.directory / "other.sqlite3")
        foreign_registry = WorkspaceRegistry(other, self.directory / "other-workspaces")
        foreign_artifacts = ArtifactStore(other, foreign_registry)
        for registry, artifacts in ((foreign_registry, self.artifacts),
                                    (self.registry, foreign_artifacts),
                                    (self.registry, ArtifactStore(self.repository,
                                        WorkspaceRegistry(self.repository, self.directory / "other-root")))):
            with self.subTest(registry=registry, artifacts=artifacts):
                self.assert_invalid(HostRuntimeServices, self.repository, registry, artifacts,
                    tools=self.tools, source_configuration=self.source_configuration,
                    validate_configuration=self.validate, client_factory=self.client_factory)

    def test_constructor_revalidates_typed_policies_and_callbacks(self):
        malformed_source = self.source_configuration.model_copy(update={"schema_version": True})
        for changes in (
            {"tools": object()}, {"source_configuration": object()},
            {"source_configuration": malformed_source}, {"validate_configuration": None},
            {"client_factory": None}, {"proof_verifier": object()},
            {"protected_cases": []}, {"protected_cases": (object(),)},
        ):
            with self.subTest(changes=changes):
                self.assert_invalid(self.host, **changes)
        malformed_tools = replace(self.tools)
        object.__setattr__(malformed_tools, "max_call_seconds", True)
        self.assert_invalid(self.host, tools=malformed_tools)

    def test_approved_configuration_is_copied_against_host_object_mutation(self):
        host = self.host()
        object.__setattr__(self.tools, "max_call_seconds", 1)
        object.__setattr__(self.source_configuration, "repository_id", "changed-after-admission")
        execution = self.execution(AgentRole.DEVELOPER)
        services = host.developer(execution)
        self.assertEqual(services.configuration.max_call_seconds, 60)
        self.assertEqual(services._repository_id, "host-service-fixture")

    def test_factories_issue_existing_role_services_with_least_role_configuration(self):
        host = self.host()
        for role, expected in ((AgentRole.DEVELOPER, DeveloperRuntimeServices),
                               (AgentRole.QA, QARuntimeServices),
                               (AgentRole.SECURITY, SecurityRuntimeServices)):
            with self.subTest(role=role):
                execution = self.execution(role)
                services = getattr(host, role.value.lower())(execution)
                self.assertIs(type(services), expected)
                configuration = services.configuration
                self.assertIs(configuration.binding.role, role)
                self.assertIs(configuration.binding.agent_role, role)
                self.assertEqual(configuration.binding.run_id, execution.metadata.run_id)
                self.assertEqual(configuration.binding.workspace_id, execution.configuration.workspace_id)
                self.assertEqual(configuration.database_path, self.repository.database_path)
                self.assertEqual(configuration.workspace_root, self.registry.base_path)
                self.assertEqual(configuration.max_call_seconds, self.tools.max_call_seconds)
                self.assertEqual(configuration.build_configuration is not None, role is AgentRole.DEVELOPER)
                self.assertEqual(configuration.unit_test_configuration is not None,
                    role in (AgentRole.DEVELOPER, AgentRole.QA))
                self.assertEqual(configuration.browser_test_configuration is not None, role is AgentRole.QA)
                self.assertEqual(configuration.security_scan_configuration is not None, role is AgentRole.SECURITY)
                if role is AgentRole.DEVELOPER:
                    self.assertIsNone(configuration.frozen_source)
                    self.assertEqual(tuple(item.kind for item in configuration.unit_test_configuration.scopes),
                        ("SNAPSHOT",))
                    self.assertEqual(services._baseline, self.source_configuration.commit_hash)
                    self.assertEqual(services._lock_path, self.source_configuration.lock_path)
                else:
                    self.assertEqual(configuration.frozen_source.project_artifact_id, execution.source.artifact_id)
                    self.assertEqual(configuration.frozen_source.snapshot_sha256, execution.source.snapshot_sha256)
                self.assertEqual(self.validated[-1],
                    (execution.configuration.configuration, execution.metadata.run_id))
        self.assertFalse(self.registry.base_path.exists())
        self.assertEqual(self.transport_events, [])

    def test_each_execution_gets_fresh_services_without_a_fresh_budget_or_policy(self):
        host = self.host()
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            execution = self.execution(role)
            budget = execution.budget
            factory = getattr(host, role.value.lower())
            one, two = factory(execution), factory(execution)
            self.assertIsNot(one, two)
            self.assertEqual(one.configuration, two.configuration)
            self.assertIs(execution.budget, budget)

    def test_wrong_run_scenario_workspace_or_frozen_configuration_is_rejected(self):
        execution = self.execution(AgentRole.DEVELOPER)
        host = self.host()
        originals = execution.metadata, execution.configuration
        for field, changed in (
            ("metadata", execution.metadata.model_copy(update={"run_id": uuid4()})),
            ("metadata", execution.metadata.model_copy(update={"scenario_id": uuid4()})),
            ("configuration", execution.configuration.model_copy(update={"workspace_id": uuid4()})),
            ("configuration", execution.configuration.model_copy(update={"artifact_id": uuid4()})),
        ):
            with self.subTest(field=field, changed=changed):
                execution.metadata, execution.configuration = originals
                setattr(execution, field, changed)
                self.assert_invalid(host.developer, execution)

    def test_actual_persisted_step_provides_role_attempt_and_input_binding(self):
        execution = self.execution(AgentRole.QA)
        host = self.host()
        self.assert_invalid(host.developer, execution)
        for changes in ({"agent_role": AgentRole.DEVELOPER}, {"attempt": 1},
                        {"status": WorkflowStepStatus.PENDING}, {"code_version": 2},
                        {"requirement_ids": [uuid4()]}, {"input_artifact_ids": [uuid4()]}):
            with self.subTest(changes=changes), patch.object(self.repository, "list_steps",
                    return_value=[execution.step.model_copy(update=changes)]):
                self.assert_invalid(host.qa, execution)

    def test_metadata_strict_integer_is_not_coerced_from_privileged_boolean(self):
        execution = self.execution(AgentRole.DEVELOPER)
        execution.metadata = execution.metadata.model_copy(update={"attempt": False})
        self.assert_invalid(self.host().developer, execution)

    def test_wrong_stored_workspace_owner_and_root_are_rejected(self):
        execution = self.execution(AgentRole.DEVELOPER)
        workspace = self.repository.get_workspace(execution.configuration.workspace_id)
        host = self.host()
        for changes in ({"run_id": uuid4()}, {"workspace_id": uuid4()},
                        {"root_path": str(self.directory / "foreign-root")}):
            with self.subTest(changes=changes), patch.object(self.repository, "get_workspace",
                    return_value=workspace.model_copy(update=changes)):
                self.assert_invalid(host.developer, execution)

    def test_service_construction_reads_identity_without_initializing_receipt_tables(self):
        execution = self.execution(AgentRole.QA)
        host = self.host()
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("no factory writes")):
            services = host.qa(execution)
        self.assertIs(type(services), QARuntimeServices)
        with self.repository._connection() as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(tables & {"run_evaluation_policies", "tool_executions", "qa_test_input_captures"})
        self.assertFalse(self.registry.base_path.exists())

    def test_qa_and_security_source_selection_cannot_cross_run_version_or_artifact(self):
        host = self.host()
        for role in (AgentRole.QA, AgentRole.SECURITY):
            execution = self.execution(role)
            source = execution.source
            for changes in ({"run_id": uuid4()}, {"code_version": 2}, {"artifact_id": uuid4()}):
                with self.subTest(role=role, changes=changes):
                    execution.source = source.model_copy(update=changes)
                    self.assert_invalid(getattr(host, role.value.lower()), execution)

    def test_callback_errors_and_async_callback_are_safe_closed_errors(self):
        execution = self.execution(AgentRole.DEVELOPER)
        def rejected(_configuration, _run_id):
            raise ValueError("private-host-value")
        async def asynchronous(_configuration, _run_id):
            raise AssertionError("not executed")
        for validator in (rejected, asynchronous):
            self.assert_invalid(self.host(validate_configuration=validator).developer, execution)

    def test_expired_or_untyped_budget_cannot_issue_services(self):
        execution = self.execution(AgentRole.DEVELOPER)
        execution.budget = object()
        self.assert_invalid(self.host().developer, execution)
        execution.budget = ExecutionBudget(runtime_budget_ms=1)
        execution.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            self.host().developer(execution)
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(self.transport_events, [])

    def test_approved_protected_case_and_security_verifier_remain_host_only(self):
        data = configuration_data()
        data["unit"]["scopes"].append(protected_unit())
        tools = decode_tool_configuration(json.dumps(data))
        execution = self.execution(AgentRole.QA, protected=True)
        case = QACaseBinding(tool_name="run_unit_tests", selector="protected-unit",
            test_id="tests.Signup.test_valid", requirement_id=execution.metadata.requirement_ids[0],
            title="inert protected fixture", expected_result="approved criterion")
        host = self.host(tools=tools, protected_cases=(case,))
        services = host.qa(execution)
        self.assertEqual(services._protected_cases, (case,))
        self.assertNotIn("protected-unit", services.selectors["run_unit_tests"])
        security_execution = self.execution(AgentRole.SECURITY)
        verifier = lambda *_args: None
        self.assertIs(self.host(proof_verifier=verifier).security(security_execution)._verifier, verifier)
        self.assertIsNone(self.host().security(security_execution)._verifier)


class HostRuntimeClientDeadlineTests(_HostFixture, unittest.IsolatedAsyncioTestCase):
    async def test_transport_lifetime_uses_same_task_without_clamping_approved_policy(self):
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host().developer(execution)
        task = asyncio.current_task()
        async with services.client_factory(services.configuration) as client:
            self.assertIs(type(client), BoundMCPClient)
            self.assertEqual(client.configuration.max_call_seconds, 60)
            self.assertEqual(self.transport_events[0][1], task)
        self.assertEqual([item[0] for item in self.transport_events], ["enter", "exit"])
        self.assertIs(self.transport_events[1][1], task)
        self.assertEqual(services.configuration.max_call_seconds, 60)

    async def test_expired_budget_never_enters_transport(self):
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host().developer(execution)
        execution.budget._deadline = time.monotonic() - 1
        with self.assertRaises(LLMRuntimeError) as caught:
            async with services.client_factory(services.configuration):
                self.fail("expired budget cannot yield")
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(self.transport_events, [])

    async def test_slow_discovery_is_cancelled_with_budget_and_transport_cleanup(self):
        events = []
        @asynccontextmanager
        async def slow(_configuration):
            events.append(("enter", asyncio.current_task()))
            try:
                await asyncio.sleep(1)
                yield object()
            finally:
                events.append(("exit", asyncio.current_task()))
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host(client_factory=slow).developer(execution)
        execution.budget._deadline = time.monotonic() + .02
        with self.assertRaises(LLMRuntimeError) as caught:
            async with services.client_factory(services.configuration):
                self.fail("slow discovery cannot yield")
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual([item[0] for item in events], ["enter", "exit"])
        self.assertIs(events[0][1], events[1][1])

    async def test_deadline_covers_yield_body_without_new_budget(self):
        execution = self.execution(AgentRole.QA)
        services = self.host().qa(execution)
        execution.budget._deadline = time.monotonic() + .02
        with self.assertRaises(LLMRuntimeError) as caught:
            async with services.client_factory(services.configuration):
                await asyncio.sleep(1)
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual([item[0] for item in self.transport_events], ["enter", "exit"])

    async def test_cancellation_is_preserved_and_cleanup_stays_in_entering_task(self):
        execution = self.execution(AgentRole.SECURITY)
        services = self.host().security(execution)
        entered = asyncio.Event()
        async def operation():
            async with services.client_factory(services.configuration):
                entered.set()
                await asyncio.sleep(1)
        operation_task = asyncio.create_task(operation())
        await entered.wait()
        operation_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation_task
        self.assertEqual([item[0] for item in self.transport_events], ["enter", "exit"])
        self.assertTrue(all(item[1] is operation_task for item in self.transport_events))

    async def test_native_handshake_timeout_is_preserved_before_run_deadline(self):
        native = TimeoutError("intentional fixture transport timeout")
        @asynccontextmanager
        async def unavailable(_configuration):
            raise native
            yield
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host(client_factory=unavailable).developer(execution)
        with self.assertRaises(TimeoutError) as caught:
            async with services.client_factory(services.configuration):
                self.fail("unavailable transport cannot yield")
        self.assertIs(caught.exception, native)
        self.assertGreater(execution.budget.remaining_seconds(), 1)

    async def test_native_body_timeout_and_unrelated_body_error_are_not_hidden(self):
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host().developer(execution)
        native = TimeoutError("intentional fixture body timeout")
        with self.assertRaises(TimeoutError) as caught:
            async with services.client_factory(services.configuration):
                raise native
        self.assertIs(caught.exception, native)
        self.assertGreater(execution.budget.remaining_seconds(), 1)
        self.assertEqual([item[0] for item in self.transport_events], ["enter", "exit"])
        with self.assertRaisesRegex(ValueError, "intentional caller exception"):
            async with services.client_factory(services.configuration):
                raise ValueError("intentional caller exception")

    async def test_cleanup_that_drains_after_cancellation_does_not_publish_late_result(self):
        events = []
        @asynccontextmanager
        async def draining(_configuration):
            events.append(("enter", asyncio.current_task()))
            try:
                yield object()
            finally:
                await asyncio.sleep(.03)
                events.append(("drained", asyncio.current_task()))
        execution = self.execution(AgentRole.DEVELOPER)
        services = self.host(client_factory=draining).developer(execution)
        execution.budget._deadline = time.monotonic() + .01
        with self.assertRaises(LLMRuntimeError) as caught:
            async with services.client_factory(services.configuration):
                await asyncio.sleep(1)
        self.assertIs(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual([item[0] for item in events], ["enter", "drained"])
        self.assertIs(events[0][1], events[1][1])


if __name__ == "__main__":
    unittest.main()
