"""Request-scoped access to the lazily initialized persistence adapter."""

from fastapi import Request

from orchestrator.a2a import A2AAgentRegistry
from orchestrator.application import PlannerRunDispatcher
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
            dispatcher = PlannerRunDispatcher(repository, registry)
            request.app.state.run_dispatcher = dispatcher
    return dispatcher
