"""Factory for one role-specific local A2A server; no product execution by default."""

from contextlib import asynccontextmanager

from a2a.server.agent_execution import AgentExecutor
from fastapi import FastAPI, Request
from google.protobuf.json_format import MessageToDict
from starlette.responses import JSONResponse

from agents.api.card import build_agent_card
from agents.api.handler import ProjectRequestHandler
from agents.api.routes import authentication_response, create_protocol_router, is_authorized
from agents.api.task_store import RedactingInMemoryTaskStore
from agents.core.config import AgentSettings
from agents.core.logging import configure_agent_logging
from agents.runtime.bootstrap import BootstrapAgentExecutor


def create_app(
    settings: AgentSettings | None = None, *, executor: AgentExecutor | None = None
) -> FastAPI:
    settings = settings or AgentSettings()
    configure_agent_logging(settings.log_level)
    card = build_agent_card(settings)
    store = RedactingInMemoryTaskStore()
    handler = ProjectRequestHandler(
        agent_executor=executor or BootstrapAgentExecutor(),
        task_store=store, agent_card=card, validate_input_modes=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await handler.aclose()

    app = FastAPI(title=f"{settings.role.value.title()} A2A Agent", version="0.1.0", lifespan=lifespan)
    app.state.request_handler = handler
    app.state.task_store = store
    app.include_router(create_protocol_router(settings, handler))

    @app.get("/health", tags=["Health"])
    async def health() -> dict:
        return {"status": "ok", "role": settings.role.value, "executionReady": False}

    @app.get("/.well-known/agent-card.json", tags=["A2A Agent Card"])
    async def agent_card(request: Request) -> JSONResponse:
        if not is_authorized(request, settings):
            return authentication_response()
        return JSONResponse(MessageToDict(card), headers={"Cache-Control": "no-store"})

    return app
