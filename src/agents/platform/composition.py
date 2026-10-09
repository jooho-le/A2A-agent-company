"""Explicit Host composition of the initial, owned four-Agent pipeline.

Construction does not start HTTP servers, initialize Agent databases, provision
Workspaces, seed Git, invoke models, or launch MCP/container processes.  The
Host supplies already prepared storage capabilities and a trusted preparation
callback; only a durably claimed new Run can invoke that callback.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from types import MappingProxyType

from fastapi import FastAPI

from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from agents.llm.configuration import model_from_settings
from agents.llm.content import sanitize_content
from agents.llm.contracts import (
    JsonSchema, LLMErrorCode, LLMRuntimeError, StructuredOutput, parse_json,
)
from agents.main import create_app as create_agent_app
from agents.platform.budgets import RunBudgetRegistry
from agents.runtime.developer import DeveloperAgentExecutor
from agents.runtime.developer_context import SQLiteDeveloperContextLoader
from agents.runtime.developer_services import DeveloperRuntimeServices
from agents.runtime.planner import PlannerAgentExecutor
from agents.runtime.planner_context import SQLitePlannerContextLoader
from agents.runtime.qa import QAAgentExecutor
from agents.runtime.qa_context import SQLiteQAContextLoader
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security import SecurityAgentExecutor
from agents.runtime.security_context import SQLiteSecurityContextLoader
from agents.runtime.security_services import SecurityRuntimeServices
from orchestrator.a2a.registry import A2AAgentRegistry
from orchestrator.api.dependencies import agent_client_factory
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.core.config import Settings
from orchestrator.domain.run_configuration import RunConfiguration
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app as create_orchestrator_app
from orchestrator.workspaces.registry import WorkspaceRegistry


class PlatformConfigurationError(ValueError):
    """No submitted value, credential, filesystem path or provider prose."""

    code = "OWNED_AGENT_CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


class PlatformPreparationError(ValueError):
    code = "OWNED_AGENT_PREPARATION_FAILED"

    def __init__(self):
        super().__init__(self.code)


# Provider admission checks model selection/authentication without pretending
# to know runtime QA selectors or scanner findings before a Source exists.
# Each executor still validates its actual role-specific structured output.
_ADMISSION_OUTPUT = StructuredOutput(
    name="owned_platform_admission",
    schema=JsonSchema.from_dict({
        "type": "object", "properties": {}, "required": [],
        "additionalProperties": False,
    }),
)


async def _host_call(factory, argument):
    """Drain a synchronous Host worker before propagating cancellation.

    A timeout may stop admission, but must not leave an orphaned preparation
    thread modifying a Workspace after the dispatcher has released its claim.
    Async Host capabilities remain responsible for their own child cleanup.
    """
    if inspect.iscoroutinefunction(factory):
        result = factory(argument)
    else:
        worker = asyncio.create_task(asyncio.to_thread(factory, argument))
        try:
            result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled():
                try:
                    pending = worker.result()
                    if inspect.iscoroutine(pending):
                        pending.close()
                except Exception:
                    pass
            raise
    return await result if inspect.isawaitable(result) else result


@dataclass(frozen=True, kw_only=True)
class PlatformEndpoint:
    name: str
    host: str
    port: int
    app: FastAPI = field(repr=False)

    def __post_init__(self):
        if (type(self.name) is not str or type(self.host) is not str
                or self.name not in {"orchestrator", *(role.value.lower() for role in AgentRole)}
                or self.host not in {"127.0.0.1", "localhost", "::1"}
                or type(self.port) is not int or not 1 <= self.port <= 65535
                or not isinstance(self.app, FastAPI)):
            raise PlatformConfigurationError()


@dataclass(repr=False)
class _Lifecycle:
    providers: tuple
    closed: bool = False
    guard: asyncio.Lock = field(default_factory=asyncio.Lock)
    close_task: asyncio.Task | None = None


@dataclass(frozen=True, kw_only=True, repr=False)
class OwnedAgentPlatform:
    orchestrator_app: FastAPI
    agent_apps: Mapping
    dispatcher: PlannerRunDispatcher
    budgets: RunBudgetRegistry
    endpoints: tuple[PlatformEndpoint, ...]
    _lifecycle: _Lifecycle = field(repr=False)

    def __repr__(self):
        return "OwnedAgentPlatform()"

    async def aclose(self):
        """Close transferred providers once; Host storage remains caller-owned.

        Call after Agent lifespans have drained their workers.  Every provider
        is attempted even if another close fails, with credential-safe errors.
        """
        async with self._lifecycle.guard:
            if self._lifecycle.close_task is None:
                self._lifecycle.closed = True
                self._lifecycle.close_task = asyncio.create_task(_close_providers(self._lifecycle.providers))
            closing = self._lifecycle.close_task
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            while not closing.done():
                try:
                    await asyncio.shield(closing)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if closing.done() and not closing.cancelled():
                try:
                    closing.result()
                except Exception:
                    pass
            raise


async def _close_providers(providers):
    failed = False
    for provider in providers:
        try:
            close = getattr(provider, "aclose", None) or getattr(provider, "close", None)
            if not callable(close):
                continue
            if inspect.iscoroutinefunction(close):
                await close()
            else:
                await _host_call(lambda _argument: close(), None)
        except Exception:
            failed = True
    if failed:
        raise PlatformConfigurationError() from None


def _secret(value):
    return None if value is None else value.get_secret_value()


def _role_mapping(value):
    if (not isinstance(value, Mapping)
            or any(type(role) is not AgentRole for role in value)
            or set(value) != set(AgentRole)):
        raise PlatformConfigurationError()
    return dict(value)


def _bound_services(factory, expected, repository, workspace_registry, artifact_store):
    async def create(execution):
        value = await _host_call(factory, execution)
        if (type(value) is not expected or value._repository is not repository
                or value._workspaces is not workspace_registry
                or value._artifacts is not artifact_store):
            raise PlatformConfigurationError()
        return value
    return create


def create_platform(
    *, repository, workspace_registry, artifact_store, orchestrator_settings,
    agent_settings, providers, developer_services_factory, qa_services_factory,
    security_services_factory, prepare_workspace, limits,
    orchestrator_host="127.0.0.1", orchestrator_port=8000,
    a2a_client_factory=None,
) -> OwnedAgentPlatform:
    """Bind actual executors and official HTTP apps to one shared Run budget.

    ``prepare_workspace(run_id)`` is mandatory and trusted.  It may explicitly
    provision/seed an approved baseline; this module supplies no such default.
    Host service factories receive the already loaded execution context and
    must return the corresponding existing runtime-service type.

    Every AgentSettings.llm_limits must equal the authoritative Host limits.
    Successful construction transfers provider close ownership to this object;
    construction failures do not close caller-owned providers or Host stores.

    Verified product defects can enter the existing bounded fix/revalidation
    loop. Every cycle retains the same frozen configuration and Run budget;
    unverified evidence does not authorize a fabricated PASS or a new budget.
    """
    try:
        if (not isinstance(repository, SQLiteWorkflowRepository)
                or not isinstance(workspace_registry, WorkspaceRegistry)
                or not isinstance(artifact_store, ArtifactStore)
                or type(orchestrator_settings) is not Settings
                or type(limits) is not LLMLimits
                or workspace_registry._repository is not repository
                or artifact_store._repository is not repository
                or artifact_store._workspaces is not workspace_registry
                or any(not callable(factory) for factory in (
                    developer_services_factory, qa_services_factory,
                    security_services_factory, prepare_workspace,
                ))
                or a2a_client_factory is not None and not callable(a2a_client_factory)
                or orchestrator_host not in {"127.0.0.1", "localhost", "::1"}
                or type(orchestrator_port) is not int or not 1 <= orchestrator_port <= 65535):
            raise ValueError
        settings = orchestrator_settings.model_copy(deep=True)
        if (Path(settings.database_path).resolve() != repository.database_path.resolve()
                or Path(settings.workspace_root).resolve() != workspace_registry.base_path):
            raise ValueError
        roles = _role_mapping(agent_settings)
        provider_map = _role_mapping(providers)
        models, databases, ports = {}, {repository.database_path.resolve()}, {orchestrator_port}
        for role in AgentRole:
            selected = roles[role]
            provider = provider_map[role]
            if (type(selected) is not AgentSettings or selected.role is not role
                    or selected.llm_limits != limits
                    or selected.listen_port in ports
                    or selected.task_database_path.resolve() in databases
                    or getattr(settings, f"{role.value.lower()}_agent_url") != selected.agent_base_url
                    or _secret(getattr(settings, f"{role.value.lower()}_bearer_token")) != _secret(selected.bearer_token)
                    or not callable(getattr(provider, "complete", None))
                    or not callable(getattr(provider, "validate_configuration", None))):
                raise ValueError
            model = model_from_settings(selected)
            if getattr(provider, "name", None) != model.provider:
                raise ValueError
            models[role] = model
            databases.add(selected.task_database_path.resolve())
            ports.add(selected.listen_port)
        if any(model != models[AgentRole.PLANNER] for model in models.values()):
            raise ValueError
        limits = LLMLimits.model_validate(limits.model_dump())
    except Exception:
        raise PlatformConfigurationError() from None

    lifecycle = _Lifecycle(providers=tuple({id(value): value for value in provider_map.values()}.values()))
    budgets = RunBudgetRegistry(repository, limits=limits)

    def validate_submission(configuration):
        try:
            if lifecycle.closed or type(configuration) is not RunConfiguration:
                raise ValueError
            copied = RunConfiguration.model_validate(parse_json(configuration.model_dump_json(warnings=False)))
            if (configuration != copied or copied.model != models[AgentRole.PLANNER]
                    or copied.environment is None or copied.environment.network_policy != "DENY"
                    or copied.limits.runtime_budget_ms is None
                    or copied.starting_commit_hash is None
                    or copied.scanner_profile_ref is None):
                raise ValueError
            sanitize_content(copied.model_dump(mode="json"), reject_secrets=True)
            for role in AgentRole:
                model_from_settings(roles[role], frozen_model=copied.model)
                result = provider_map[role].validate_configuration(copied.model, _ADMISSION_OUTPUT)
                if inspect.isawaitable(result):
                    if inspect.iscoroutine(result):
                        result.close()
                    raise ValueError
        except Exception:
            raise PlatformConfigurationError() from None

    async def before_dispatch(run_id):
        if lifecycle.closed:
            raise PlatformPreparationError()
        try:
            budget = await _host_call(budgets.admit, run_id)
            configuration = await _host_call(repository.get_run_configuration, run_id)
            validate_submission(configuration.configuration)
            budget.check()
            try:
                await asyncio.wait_for(_host_call(prepare_workspace, run_id), timeout=budget.remaining_seconds())
            except asyncio.TimeoutError:
                raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
            budget.check()
        except asyncio.CancelledError:
            raise
        except LLMRuntimeError as error:
            if error.code in {LLMErrorCode.BUDGET, LLMErrorCode.TIMEOUT}:
                raise
            raise PlatformPreparationError() from None
        except Exception:
            raise PlatformPreparationError() from None

    contexts = {
        AgentRole.PLANNER: SQLitePlannerContextLoader(repository, budgets.resolve),
        AgentRole.DEVELOPER: SQLiteDeveloperContextLoader(repository, budgets.resolve),
        AgentRole.QA: SQLiteQAContextLoader(repository, budgets.resolve),
        AgentRole.SECURITY: SQLiteSecurityContextLoader(repository, budgets.resolve),
    }
    executors = {
        AgentRole.PLANNER: PlannerAgentExecutor(provider=provider_map[AgentRole.PLANNER],
            context_factory=contexts[AgentRole.PLANNER]),
        AgentRole.DEVELOPER: DeveloperAgentExecutor(provider=provider_map[AgentRole.DEVELOPER],
            context_factory=contexts[AgentRole.DEVELOPER], services_factory=_bound_services(
                developer_services_factory, DeveloperRuntimeServices, repository, workspace_registry, artifact_store)),
        AgentRole.QA: QAAgentExecutor(provider=provider_map[AgentRole.QA],
            context_factory=contexts[AgentRole.QA], services_factory=_bound_services(
                qa_services_factory, QARuntimeServices, repository, workspace_registry, artifact_store)),
        AgentRole.SECURITY: SecurityAgentExecutor(provider=provider_map[AgentRole.SECURITY],
            context_factory=contexts[AgentRole.SECURITY], services_factory=_bound_services(
                security_services_factory, SecurityRuntimeServices, repository, workspace_registry, artifact_store)),
    }
    # Local credentials/Task payloads must not follow HTTP_PROXY/ALL_PROXY.
    # The explicit test seam also stays shared with existing control endpoints.
    client_factory = a2a_client_factory if a2a_client_factory is not None else agent_client_factory(settings, trust_env=False)
    dispatcher = PlannerRunDispatcher(repository, A2AAgentRegistry.from_settings(settings),
        client_factory=client_factory,
        before_dispatch=before_dispatch, allow_fix_dispatch=True)
    app = create_orchestrator_app(repository=repository, dispatcher=dispatcher,
        settings=settings, submission_validator=validate_submission)
    app.state.workspace_registry = workspace_registry
    app.state.agent_client_factory = client_factory

    def control_preflight(run_id):
        if lifecycle.closed:
            raise PlatformPreparationError()
        configuration = repository.get_run_configuration(run_id)
        budgets.resolve(configuration).check_model_call()

    app.state.control_preflight = control_preflight
    apps = {role: create_agent_app(roles[role], executor=executors[role]) for role in AgentRole}
    endpoints = (PlatformEndpoint(name="orchestrator", host=orchestrator_host, port=orchestrator_port, app=app),
        *(PlatformEndpoint(name=role.value.lower(), host=roles[role].host,
            port=roles[role].listen_port, app=apps[role]) for role in AgentRole))
    return OwnedAgentPlatform(orchestrator_app=app, agent_apps=MappingProxyType(apps),
        dispatcher=dispatcher, budgets=budgets, endpoints=endpoints, _lifecycle=lifecycle)
