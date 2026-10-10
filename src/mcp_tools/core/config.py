"""MCP configuration only; no Tool execution, filesystem mounts, or child process."""

from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

from mcp_tools.core.policy import (
    MCP_PROTOCOL_VERSION,
    MCP_TRANSPORT,
    MCP_TOOL_NAMES,
    MCPHostPrincipal,
)
from orchestrator.domain.constants import MAX_MCP_TOOL_RETRIES
from orchestrator.domain.states import AgentRole


class MCPSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    role: AgentRole | MCPHostPrincipal
    environment: Literal["local", "development", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    transport: Literal["stdio"] = MCP_TRANSPORT
    protocol_version: Literal["2026-07-28"] = MCP_PROTOCOL_VERSION

    @property
    def allowed_tool_names(self) -> tuple[str, ...]:
        """Declared role policy, not yet a tools/list or tools/call implementation."""
        return MCP_TOOL_NAMES[self.role]

    @property
    def max_tool_retries(self) -> int:
        return MAX_MCP_TOOL_RETRIES
