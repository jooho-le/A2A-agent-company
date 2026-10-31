"""Single service identity using the existing guarded A2A server and store."""

from a2a.server.agent_execution import AgentExecutor
from fastapi import FastAPI

from agents.core.config import AgentSettings
from agents.core.identity import SingleAgentIdentity
from agents.main import create_app as create_agent_app


def create_app(settings: AgentSettings | None = None, *, executor: AgentExecutor | None = None) -> FastAPI:
    settings = settings or AgentSettings(role=SingleAgentIdentity.SINGLE_AGENT)
    if settings.role is not SingleAgentIdentity.SINGLE_AGENT:
        raise ValueError("SINGLE_AGENT_IDENTITY_REQUIRED")
    # Bootstrap by default. Provider settings or an arbitrary injected executor
    # do not advertise measured product execution before 51–52 are implemented.
    return create_agent_app(settings, executor=executor)
