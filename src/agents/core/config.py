"""Agent process settings, without starting a server, model, or MCP subprocess."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agents.core.contracts import AGENT_PORTS
from agents.llm.budget import LLMLimits
from orchestrator.domain.states import AgentRole


class AgentConfigurationError(ValueError):
    """Required model selection has not been supplied by the operator."""


class AgentSettings(BaseSettings):
    """One role per process; all optional model settings start unconfigured."""

    model_config = SettingsConfigDict(
        env_prefix="AGENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    role: AgentRole
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    port: int | None = Field(default=None, ge=1, le=65535)
    environment: Literal["local", "development", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_path: Path | None = None
    bearer_token: SecretStr | None = Field(default=None, exclude=True, repr=False)
    llm_provider: str | None = None
    llm_model_id: str | None = None
    llm_model_revision: str | None = None
    llm_temperature: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    llm_seed: int | None = None
    llm_api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    llm_limits: LLMLimits = Field(default_factory=LLMLimits)

    @field_validator("database_path", mode="before")
    @classmethod
    def validate_database_path(cls, value):
        if value is not None and (
            not str(value).strip() or str(value) in (".", ":memory:")
        ):
            raise ValueError("Agent database must be a persistent file path")
        return value

    @field_validator("llm_provider", "llm_model_id", "llm_model_revision")
    @classmethod
    def validate_model_selection(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("LLM provider/model ID must not be blank")
        return value

    @field_validator("bearer_token", "llm_api_key")
    @classmethod
    def validate_credentials(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("Credential must not be blank; omit it when unconfigured")
        return value

    @property
    def listen_port(self) -> int:
        return self.port if self.port is not None else AGENT_PORTS[self.role]

    @property
    def agent_base_url(self) -> str:
        host = f"[{self.host}]" if self.host == "::1" else self.host
        return f"http://{host}:{self.listen_port}"

    @property
    def task_database_path(self) -> Path:
        """Trusted operator path; accessing this property does not create a DB."""
        if self.database_path is not None:
            return self.database_path
        return Path(".data") / "agents" / f"{self.role.value.lower()}.sqlite3"

    def require_llm_configuration(self) -> None:
        """Check selection only; provider support/authentication belong to step 19."""
        missing = []
        if self.llm_provider is None:
            missing.append("AGENT_LLM_PROVIDER")
        if self.llm_model_id is None:
            missing.append("AGENT_LLM_MODEL_ID")
        if missing:
            raise AgentConfigurationError(
                "LLM selection is not configured: " + ", ".join(missing)
            )
