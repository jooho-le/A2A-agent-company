from fastapi import FastAPI

from orchestrator.api.routes.health import router as health_router
from orchestrator.core.config import get_settings
from orchestrator.core.logging import configure_logging


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description="Coordinates A2A agents and their workflow state.",
    )
    app.include_router(health_router)
    return app


app = create_app()
