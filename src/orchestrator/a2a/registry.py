"""Configured Agent endpoint registry for the Orchestrator."""

from orchestrator.core.config import Settings
from orchestrator.domain.states import AgentRole


class AgentNotConfiguredError(LookupError):
    """Raised when a requested Agent role has no configured A2A endpoint."""


class A2AAgentRegistry:
    """Map project Agent roles to their configured A2A base URLs."""

    def __init__(self, endpoints: dict[AgentRole, str | None]) -> None:
        self._endpoints = {
            role: url.strip() if url and url.strip() else None
            for role, url in endpoints.items()
        }

    @classmethod
    def from_settings(cls, settings: Settings) -> "A2AAgentRegistry":
        return cls(
            {
                AgentRole.PLANNER: settings.planner_agent_url,
                AgentRole.DEVELOPER: settings.developer_agent_url,
                AgentRole.QA: settings.qa_agent_url,
                AgentRole.SECURITY: settings.security_agent_url,
            }
        )

    def get_base_url(self, role: AgentRole) -> str | None:
        return self._endpoints.get(role)

    def require_base_url(self, role: AgentRole) -> str:
        base_url = self.get_base_url(role)
        if base_url is None:
            raise AgentNotConfiguredError(f"No A2A endpoint configured for {role.value}")
        return base_url
