from threading import Lock

from fastapi import FastAPI

from orchestrator.api.routes.health import router as health_router
from orchestrator.api.routes.runs import router as runs_router
from orchestrator.api.routes.scenarios import router as scenarios_router
from orchestrator.application import PlannerRunDispatcher
from orchestrator.core.config import Settings, get_settings
from orchestrator.core.logging import configure_logging
from orchestrator.infrastructure import SQLiteWorkflowRepository


def create_app(
    repository: SQLiteWorkflowRepository | None = None,
    dispatcher: PlannerRunDispatcher | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Coordinates A2A agents and their workflow state.",
    )
    app.state.settings = settings
    app.state.workflow_repository = repository
    app.state.repository_lock = Lock()
    app.state.run_dispatcher = dispatcher
    app.state.dispatcher_lock = Lock()
    app.include_router(health_router)
    app.include_router(runs_router, prefix=settings.api_prefix)
    app.include_router(scenarios_router, prefix=settings.api_prefix)
    return app


app = create_app()
