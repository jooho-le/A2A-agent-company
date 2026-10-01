"""Request-scoped access to the lazily initialized persistence adapter."""

from fastapi import Request

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
