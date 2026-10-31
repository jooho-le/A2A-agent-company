"""Service identities are separate from Multi's four workflow roles."""

from enum import Enum

from orchestrator.domain.states import AgentRole


class SingleAgentIdentity(str, Enum):
    SINGLE_AGENT = "SINGLE_AGENT"


AgentServiceIdentity = AgentRole | SingleAgentIdentity


def service_identity(value: AgentServiceIdentity | str) -> AgentServiceIdentity:
    """Only trusted service configuration may select a persistent identity."""
    try:
        return AgentRole(value)
    except (ValueError, TypeError):
        try:
            return SingleAgentIdentity(value)
        except (ValueError, TypeError):
            raise ValueError("AGENT_SERVICE_IDENTITY_INVALID") from None
