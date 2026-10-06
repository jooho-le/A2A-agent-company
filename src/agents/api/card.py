"""Honest A2A 1.0 Agent Cards for the transport-only bootstrap server."""

from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
    SecurityScheme,
    StringList,
)

from agents.core.config import AgentSettings
from agents.core.contracts import A2A_PROTOCOL_BINDING, A2A_PROTOCOL_VERSION
from orchestrator.domain.states import AgentRole


_ROLE_PURPOSES = {
    AgentRole.PLANNER: "requirement analysis and task planning",
    AgentRole.DEVELOPER: "product implementation, fixes, and build requests",
    AgentRole.QA: "independent functional testing and QA reporting",
    AgentRole.SECURITY: "independent security inspection and reporting",
}


def build_agent_card(settings: AgentSettings) -> AgentCard:
    """Use official SDK models without advertising unimplemented skills."""
    card = AgentCard(
        name=f"{settings.role.value} Agent",
        description=(
            f"Intended role: {_ROLE_PURPOSES[settings.role]}. "
            "Transport-only bootstrap: the Agent runtime is not configured. "
            "Tasks are explicitly rejected; no LLM or MCP execution occurs."
        ),
        version="0.1.0",
        supported_interfaces=[
            AgentInterface(
                url=settings.agent_base_url,
                protocol_binding=A2A_PROTOCOL_BINDING,
                protocol_version=A2A_PROTOCOL_VERSION,
            )
        ],
        capabilities=AgentCapabilities(streaming=False, push_notifications=False),
        default_input_modes=["application/json"],
        default_output_modes=["application/json"],
        skills=[],
    )
    if settings.bearer_token is not None:
        card.security_schemes["bearerAuth"].CopyFrom(
            SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        )
        card.security_requirements.append(
            SecurityRequirement(schemes={"bearerAuth": StringList(list=[])})
        )
    return card
