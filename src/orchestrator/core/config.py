from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORCHESTRATOR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "A2A Orchestrator"
    environment: Literal["local", "development", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    api_prefix: str = "/api/v1"
    database_path: str = ".data/orchestrator.sqlite3"
    planner_agent_url: str | None = None
    developer_agent_url: str | None = None
    qa_agent_url: str | None = None
    security_agent_url: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()
