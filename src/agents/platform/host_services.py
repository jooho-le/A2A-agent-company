"""Inert Host recipes for the existing role-bound runtime services.

Only a loaded Host execution context selects a Run, Workspace or frozen
Source. No constructor starts stdio, Docker, a provider, or project code.
The actual transport retains its approved policy while each lifetime shares
the already admitted Run deadline, including discovery and cleanup.
"""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from enum import Enum
import inspect
import json
from pathlib import Path
from uuid import UUID

from agents.core.deadlines import task_deadline
from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.composition import PlatformConfigurationError
from agents.platform.starting_source import SourceConfiguration, decode_source_configuration
from agents.platform.tool_configuration import OwnedToolConfiguration
from agents.roles.qa_contract import QACaseBinding
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.client import MCPChildConfiguration, open_mcp_client
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.snapshots import FrozenSourceSelection
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


def _model_json(model):
    # Pydantic's JSON serializer can coerce a privileged bool placed in an int
    # field to 1. Preserve Python primitive values until strict JSON decoding.
    def encode(value):
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, Enum):
            return value.value
        raise ValueError
    return json.dumps(model.model_dump(mode="python", by_alias=True, warnings=False),
                      default=encode, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


class HostRuntimeServices:
    """Host policy only; each factory issues one existing runtime service.

    validate_configuration is the caller's approved admission policy. The
    context loaders remain responsible for authenticated role-specific inputs;
    this layer independently verifies the persisted Run/Step/Workspace binding.
    Trusted callbacks are never model-selected and are not product evidence.
    """

    def __init__(self, repository, workspace_registry, artifact_store, *, tools: OwnedToolConfiguration,
                 source_configuration: SourceConfiguration, validate_configuration,
                 client_factory=open_mcp_client, protected_cases=(), proof_verifier=None):
        try:
            if (not isinstance(repository, SQLiteWorkflowRepository)
                    or not isinstance(workspace_registry, WorkspaceRegistry)
                    or not isinstance(artifact_store, ArtifactStore)
                    or workspace_registry._repository is not repository
                    or artifact_store._repository is not repository
                    or artifact_store._workspaces is not workspace_registry
                    or type(tools) is not OwnedToolConfiguration
                    or type(source_configuration) is not SourceConfiguration
                    or not callable(validate_configuration) or not callable(client_factory)
                    or proof_verifier is not None and not callable(proof_verifier)
                    or type(protected_cases) is not tuple or len(protected_cases) > 256
                    or any(type(case) is not QACaseBinding for case in protected_cases)):
                raise ValueError
            approved_tools = replace(tools)
            approved_source = decode_source_configuration(
                _model_json(source_configuration))
            if approved_source != source_configuration:
                raise ValueError
            approved_cases = tuple(replace(case) for case in protected_cases)
        except Exception:
            raise PlatformConfigurationError() from None
        self._repository, self._workspaces, self._artifacts = repository, workspace_registry, artifact_store
        self._tools, self._source = approved_tools, approved_source
        self._validate_configuration, self._client_factory = validate_configuration, client_factory
        self._protected_cases, self._proof_verifier = approved_cases, proof_verifier

    def __repr__(self):
        return "HostRuntimeServices()"

    def _binding(self, execution, role):
        try:
            metadata, configuration, budget = execution.metadata, execution.configuration, execution.budget
            if (type(metadata) is not A2AWorkflowMetadata
                    or type(configuration) is not RunConfigurationArtifact
                    or not isinstance(budget, ExecutionBudget)):
                raise ValueError
            checked_metadata = A2AWorkflowMetadata.model_validate_json(
                _model_json(metadata))
            checked_configuration = RunConfigurationArtifact.model_validate_json(
                _model_json(configuration))
            if (checked_metadata != metadata or checked_configuration != configuration
                    or metadata.run_id != configuration.run_id
                    or metadata.scenario_id != configuration.scenario_id):
                raise ValueError
            budget.check()
            result = self._validate_configuration(configuration.configuration, metadata.run_id)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ValueError
            run = self._repository.get_run(metadata.run_id)
            workspace = self._repository.get_workspace(configuration.workspace_id)
            # The official project metadata has no Agent role field. Bind the
            # fixed factory role to its actual persisted Workflow Step instead.
            steps = [step for step in self._repository.list_steps(metadata.run_id)
                     if step.workflow_step_id == metadata.workflow_step_id]
            if (run is None or workspace is None or len(steps) != 1
                    or run.run_id != configuration.run_id
                    or run.scenario_id != configuration.scenario_id
                    or run.workspace_id != configuration.workspace_id
                    or self._repository.get_run_configuration(run.run_id) != configuration
                    or workspace.run_id != run.run_id
                    or workspace.workspace_id != run.workspace_id
                    or Path(workspace.root_path) != self._workspaces.base_path / str(run.workspace_id)
                    or steps[0].run_id != run.run_id or steps[0].agent_role is not role
                    or steps[0].status is not WorkflowStepStatus.RUNNING
                    or steps[0].attempt != metadata.attempt
                    or steps[0].code_version != metadata.code_version
                    or tuple(steps[0].requirement_ids) != tuple(metadata.requirement_ids or ())
                    or tuple(steps[0].input_artifact_ids) != tuple(metadata.project_artifact_ids or ())):
                raise ValueError
            budget.check()
            return MCPBinding(role=role, agent_role=role, run_id=run.run_id,
                              workspace_id=run.workspace_id)
        except LLMRuntimeError:
            raise
        except Exception:
            raise PlatformConfigurationError() from None

    def _client(self, execution):
        budget, factory = execution.budget, self._client_factory

        @asynccontextmanager
        async def bounded(configuration):
            # Enter, yield and exit the AnyIO stdio manager in the same Task.
            # Do not clamp max_call_seconds: it is immutable evaluation policy,
            # not a fresh allowance on each continuation/revalidation attempt.
            budget.check()
            deadline = task_deadline(budget.remaining_seconds())
            try:
                async with deadline:
                    async with factory(configuration) as client:
                        budget.check()
                        yield client
                        budget.check()
                budget.check()
            except TimeoutError:
                # An unrelated transport/caller timeout is not proof that
                # the shared Run allowance was exhausted.
                if deadline.expired():
                    raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
                raise

        return bounded

    def _configuration(self, execution, role):
        binding = self._binding(execution, role)
        selection = None
        if role in (AgentRole.QA, AgentRole.SECURITY):
            try:
                source = execution.source
                if type(source) is not CodeSnapshotArtifact:
                    raise ValueError
                checked = CodeSnapshotArtifact.model_validate_json(_model_json(source))
                if (checked != source or source.run_id != binding.run_id
                        or source.code_version != execution.metadata.code_version
                        or execution.metadata.project_artifact_ids != (source.artifact_id,)):
                    raise ValueError
                selection = FrozenSourceSelection(project_artifact_id=source.artifact_id,
                                                   snapshot_sha256=source.snapshot_sha256)
            except Exception:
                raise PlatformConfigurationError() from None
        return MCPChildConfiguration(
            binding=binding, database_path=self._repository.database_path,
            workspace_root=self._workspaces.base_path, max_call_seconds=self._tools.max_call_seconds,
            frozen_source=selection,
            build_configuration=self._tools.build_configuration if role is AgentRole.DEVELOPER else None,
            unit_test_configuration=self._tools.unit_configuration_for(role)
                if role in (AgentRole.DEVELOPER, AgentRole.QA) else None,
            browser_test_configuration=self._tools.browser_configuration if role is AgentRole.QA else None,
            security_scan_configuration=self._tools.security_configuration if role is AgentRole.SECURITY else None,
        )

    def developer(self, execution) -> DeveloperRuntimeServices:
        try:
            return DeveloperRuntimeServices(self._repository, self._workspaces, self._artifacts,
                mcp_configuration=self._configuration(execution, AgentRole.DEVELOPER),
                baseline_commit_hash=self._source.commit_hash, repository_id=self._source.repository_id,
                lock_path=self._source.lock_path, client_factory=self._client(execution))
        except LLMRuntimeError:
            raise
        except Exception:
            raise PlatformConfigurationError() from None

    def qa(self, execution) -> QARuntimeServices:
        try:
            return QARuntimeServices(self._repository, self._workspaces, self._artifacts,
                mcp_configuration=self._configuration(execution, AgentRole.QA),
                protected_cases=self._protected_cases, client_factory=self._client(execution))
        except LLMRuntimeError:
            raise
        except Exception:
            raise PlatformConfigurationError() from None

    def security(self, execution) -> SecurityRuntimeServices:
        try:
            return SecurityRuntimeServices(self._repository, self._workspaces, self._artifacts,
                mcp_configuration=self._configuration(execution, AgentRole.SECURITY),
                proof_verifier=self._proof_verifier, client_factory=self._client(execution))
        except LLMRuntimeError:
            raise
        except Exception:
            raise PlatformConfigurationError() from None
