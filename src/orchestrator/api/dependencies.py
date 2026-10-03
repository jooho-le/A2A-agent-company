"""Request-scoped access to the lazily initialized persistence adapter."""

from fastapi import Request

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application import PlannerRunDispatcher
from orchestrator.application.workflow_controls import WorkflowControlService
from orchestrator.domain import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository


def get_workflow_repository(request: Request) -> SQLiteWorkflowRepository:
    repository = request.app.state.workflow_repository
    if repository is not None:
        return repository

    with request.app.state.repository_lock:
        repository = request.app.state.workflow_repository
        if repository is None:
            settings = request.app.state.settings
            repository = SQLiteWorkflowRepository(settings.database_path)
            request.app.state.workflow_repository = repository
    return repository


def get_run_dispatcher(request: Request) -> PlannerRunDispatcher | None:
    dispatcher = request.app.state.run_dispatcher
    if dispatcher is not None:
        return dispatcher

    settings = request.app.state.settings
    registry = A2AAgentRegistry.from_settings(settings)
    if registry.get_base_url(AgentRole.PLANNER) is None:
        return None

    repository = get_workflow_repository(request)
    with request.app.state.dispatcher_lock:
        dispatcher = request.app.state.run_dispatcher
        if dispatcher is None:
            dispatcher = PlannerRunDispatcher(
                repository, registry, client_factory=agent_client_factory(settings),
            )
            request.app.state.run_dispatcher = dispatcher
    return dispatcher


def agent_client_factory(settings):
    """Keep configured credentials in HTTP headers, never in Task payloads."""
    credentials = {}
    for role in AgentRole:
        prefix = role.value.lower()
        configured_url = getattr(settings, f"{prefix}_agent_url")
        url = configured_url.strip().rstrip("/") if configured_url else None
        secret = getattr(settings, f"{prefix}_bearer_token")
        if url and secret:
            if url in credentials and credentials[url] != secret.get_secret_value():
                raise ValueError("Roles sharing an endpoint must use the same HTTP credential")
            credentials[url] = secret.get_secret_value()

    def create_client(url):
        token = credentials.get(url.strip().rstrip("/"))
        return A2AAgentClient(
            url, headers={"Authorization": f"Bearer {token}"} if token else None,
        )

    return create_client


def get_workflow_controls(request: Request) -> WorkflowControlService:
    repository = get_workflow_repository(request)
    settings = request.app.state.settings
    registry = A2AAgentRegistry.from_settings(settings)
    factory = agent_client_factory(settings)
    dispatcher = get_run_dispatcher(request)
    if dispatcher is None:
        dispatcher = PlannerRunDispatcher(repository, registry, client_factory=factory)
    return WorkflowControlService(
        repository, registry, dispatcher, client_factory=factory,
        authenticated_roles=frozenset(
            role for role in AgentRole
            if getattr(settings, f"{role.value.lower()}_bearer_token")
        ),
    )
