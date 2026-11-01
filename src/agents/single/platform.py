"""Explicit Host composition of Orchestrator + one executable Single Agent.

No model, HTTP server, MCP child or Workspace starts during construction.
The existing Host service factories retain all frozen policies and receipts.
"""

import asyncio
import inspect
from pathlib import Path
from types import MappingProxyType

from agents.core.config import AgentSettings
from agents.core.identity import SingleAgentIdentity
from agents.llm.budget import LLMLimits
from agents.llm.configuration import model_from_settings
from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, parse_json
from agents.main import create_app as create_agent_app
from agents.platform.budgets import RunBudgetRegistry
from agents.platform.composition import (OwnedAgentPlatform, PlatformEndpoint, PlatformConfigurationError,
    PlatformPreparationError, _ADMISSION_OUTPUT, _Lifecycle, _bound_services, _host_call, _secret)
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security_services import SecurityRuntimeServices
from agents.single.runtime import SingleAgentExecutor
from orchestrator.a2a.registry import A2AAgentRegistry
from orchestrator.api.dependencies import agent_client_factory
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.core.config import Settings
from orchestrator.domain.run_configuration import RunConfiguration
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure.sqlite_workflows import SQLiteWorkflowRepository
from orchestrator.main import create_app as create_orchestrator_app
from orchestrator.workspaces.registry import WorkspaceRegistry


def create_single_platform(*, repository, workspace_registry, artifact_store, orchestrator_settings,
    agent_settings, provider, developer_services_factory, qa_services_factory, security_services_factory,
    prepare_workspace, limits, orchestrator_host="127.0.0.1", orchestrator_port=8000,
    a2a_client_factory=None, usage_sink=None) -> OwnedAgentPlatform:
    """Use one provider and server; role fields are only phase-routing aliases.

    All four legacy Orchestrator URL/token fields must explicitly target this
    same Agent. No existing credential or operator setting is overwritten.
    ``run_platform(platform)`` can serve the resulting two local endpoints.
    Provider close ownership transfers only on successful construction.

    This path does not enable restart/recovery or durable Single telemetry.
    Budget counters/deadline are shared across all phases and up to three fixes;
    a process restart fails closed instead of allocating another budget.
    """
    try:
        if (type(repository) is not SQLiteWorkflowRepository or type(workspace_registry) is not WorkspaceRegistry
                or type(artifact_store) is not ArtifactStore or type(orchestrator_settings) is not Settings
                or type(agent_settings) is not AgentSettings or agent_settings.role is not SingleAgentIdentity.SINGLE_AGENT
                or type(limits) is not LLMLimits or agent_settings.llm_limits != limits
                or workspace_registry._repository is not repository or artifact_store._repository is not repository
                or artifact_store._workspaces is not workspace_registry
                or any(not callable(value) for value in (prepare_workspace, developer_services_factory,
                    qa_services_factory, security_services_factory, provider.complete, provider.validate_configuration))
                or a2a_client_factory is not None and not callable(a2a_client_factory)
                or usage_sink is not None and not callable(usage_sink)):
            raise ValueError
        settings = orchestrator_settings.model_copy(deep=True)
        selected = agent_settings.model_copy(deep=True)
        if (Path(settings.database_path).resolve() != repository.database_path.resolve()
                or Path(settings.workspace_root).resolve() != workspace_registry.base_path
                or selected.task_database_path.resolve() == repository.database_path.resolve()
                or selected.listen_port == orchestrator_port):
            raise ValueError
        for role in AgentRole:
            if (getattr(settings, f"{role.value.lower()}_agent_url") != selected.agent_base_url
                    or _secret(getattr(settings, f"{role.value.lower()}_bearer_token")) != _secret(selected.bearer_token)):
                raise ValueError
        model = model_from_settings(selected)
        if provider.name != model.provider:
            raise ValueError
        limits = LLMLimits.model_validate(limits.model_dump())
        # Validate both endpoints before constructing apps or taking ownership.
        if (orchestrator_host not in {"127.0.0.1", "localhost", "::1"}
                or type(orchestrator_port) is not int or not 1 <= orchestrator_port <= 65535):
            raise ValueError
    except Exception:
        raise PlatformConfigurationError() from None

    lifecycle = _Lifecycle(providers=(provider,))
    budgets = RunBudgetRegistry(repository, limits=limits)

    def validate_submission(configuration):
        try:
            if lifecycle.closed or type(configuration) is not RunConfiguration:
                raise ValueError
            copied = RunConfiguration.model_validate(parse_json(configuration.model_dump_json(warnings=False)))
            if (copied != configuration or copied.architecture != "SINGLE_AGENT" or copied.model != model
                    or copied.environment is None or copied.environment.network_policy != "DENY"
                    or copied.limits.runtime_budget_ms is None or copied.starting_commit_hash is None
                    or copied.scanner_profile_ref is None):
                raise ValueError
            sanitize_content(copied.model_dump(mode="json"), reject_secrets=True)
            model_from_settings(selected, frozen_model=copied.model)
            result = provider.validate_configuration(copied.model, _ADMISSION_OUTPUT)
            if inspect.isawaitable(result):
                if inspect.iscoroutine(result):
                    result.close()
                raise ValueError
        except Exception:
            raise PlatformConfigurationError() from None

    async def before_dispatch(run_id):
        try:
            if lifecycle.closed:
                raise PlatformPreparationError()
            budget = await _host_call(budgets.admit, run_id)
            configuration = await _host_call(repository.get_run_configuration, run_id)
            validate_submission(configuration.configuration)
            budget.check()
            await asyncio.wait_for(_host_call(prepare_workspace, run_id), timeout=budget.remaining_seconds())
            budget.check()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
        except LLMRuntimeError:
            raise
        except Exception:
            raise PlatformPreparationError() from None

    factories = {role: _bound_services(factory, expected, repository, workspace_registry, artifact_store)
        for role, factory, expected in (
            (AgentRole.DEVELOPER, developer_services_factory, DeveloperRuntimeServices),
            (AgentRole.QA, qa_services_factory, QARuntimeServices),
            (AgentRole.SECURITY, security_services_factory, SecurityRuntimeServices))}
    executor = SingleAgentExecutor(repository=repository, budgets=budgets, provider=provider,
                                  services_factories=factories, usage_sink=usage_sink)
    client_factory = a2a_client_factory or agent_client_factory(settings, trust_env=False)
    dispatcher = PlannerRunDispatcher(repository, A2AAgentRegistry.from_settings(settings),
        client_factory=client_factory, before_dispatch=before_dispatch, allow_fix_dispatch=True, single_agent=True)
    app = create_orchestrator_app(repository=repository, settings=settings, dispatcher=dispatcher,
                                submission_validator=validate_submission)
    app.state.workspace_registry, app.state.agent_client_factory = workspace_registry, client_factory
    agent = create_agent_app(selected, executor=executor)
    endpoints = (PlatformEndpoint(name="orchestrator", host=orchestrator_host, port=orchestrator_port, app=app),
                 PlatformEndpoint(name="single_agent", host=selected.host, port=selected.listen_port, app=agent))
    return OwnedAgentPlatform(orchestrator_app=app, agent_apps=MappingProxyType({SingleAgentIdentity.SINGLE_AGENT: agent}),
        dispatcher=dispatcher, budgets=budgets, endpoints=endpoints, _lifecycle=lifecycle)
