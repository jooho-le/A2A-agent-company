"""Factory for one role-specific local A2A server; no product execution by default."""

from contextlib import asynccontextmanager

from a2a.server.agent_execution import AgentExecutor
from fastapi import FastAPI, Request
from google.protobuf.json_format import MessageToDict
from starlette.responses import JSONResponse

from agents.api.card import build_agent_card
from agents.api.handler import ProjectRequestHandler
from agents.api.routes import authentication_response, create_protocol_router, is_authorized
from agents.api.sqlite_task_store import SQLiteAgentTaskStore
from agents.core.config import AgentSettings
from agents.core.logging import configure_agent_logging
from agents.runtime.bootstrap import BootstrapAgentExecutor
from agents.runtime.lifecycle import SafeAgentExecutor


def create_app(
    settings: AgentSettings | None = None, *, executor: AgentExecutor | None = None
) -> FastAPI:
    settings = settings or AgentSettings()
    execution_ready = False
    if executor is not None:
        # Only the explicitly implemented role advertises execution. Arbitrary
        # test/injected executors and provider settings are not readiness proof.
        from agents.runtime.planner import PlannerAgentExecutor
        from agents.runtime.developer import DeveloperAgentExecutor
        if isinstance(executor, (PlannerAgentExecutor, DeveloperAgentExecutor)):
            from orchestrator.domain.states import AgentRole
            expected_role = AgentRole.PLANNER if isinstance(executor, PlannerAgentExecutor) else AgentRole.DEVELOPER
            if settings.role is not expected_role:
                raise ValueError("AGENT_EXECUTOR_ROLE_MISMATCH")
            execution_ready = True
    configure_agent_logging(settings.log_level)
    card = build_agent_card(settings, execution_ready=execution_ready)
    # Construction has no DB IO. Startup acquires exclusive process ownership
    # before the SDK may execute a Task; each role has its own durable store.
    store = SQLiteAgentTaskStore(settings.task_database_path, settings.role)
    handler = ProjectRequestHandler(
        agent_executor=SafeAgentExecutor(executor or BootstrapAgentExecutor()),
        task_store=store, agent_card=card, validate_input_modes=True,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.start()
        try:
            yield
        finally:
            # If SDK shutdown fails, keep the DB lease rather than let another
            # process execute while a worker's termination is unconfirmed.
            await handler.aclose()
            await store.aclose()

    app = FastAPI(title=f"{settings.role.value.title()} A2A Agent", version="0.1.0", lifespan=lifespan)
    app.state.request_handler = handler
    app.state.task_store = store
    app.include_router(create_protocol_router(settings, handler))

    @app.get("/health", tags=["Health"])
    async def health() -> dict:
        return {"status": "ok", "role": settings.role.value, "executionReady": execution_ready}

    @app.get("/.well-known/agent-card.json", tags=["A2A Agent Card"])
    async def agent_card(request: Request) -> JSONResponse:
        if not is_authorized(request, settings):
            return authentication_response()
        return JSONResponse(MessageToDict(card), headers={"Cache-Control": "no-store"})

    return app
