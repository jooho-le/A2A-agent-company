"""Explicit, offline Host configuration for the owned four-Agent platform.

This is operator configuration, not an A2A message, Run Artifact, or MCP input.
Only ``load_runtime_configuration`` reads file contents. Inspection resolves storage
paths without creating them; secret resolution reads only the selected process
environment. No operation starts a server, provider, database, or subprocess.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType
from typing import Annotated, Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agents.core.config import AgentSettings
from agents.llm.budget import LLMLimits
from agents.llm.contracts import parse_json
from orchestrator.core.config import Settings
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


MAX_CONFIGURATION_BYTES = 65_536
_ERROR_CODES = frozenset({
    "RUNTIME_CONFIGURATION_INVALID", "RUNTIME_CONFIGURATION_FILE_INVALID",
    "RUNTIME_CONFIGURATION_STORAGE_CONFLICT", "RUNTIME_CONFIGURATION_SECRET_MISSING",
    "RUNTIME_CONFIGURATION_SECRET_INVALID",
})
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_INVALID = "RUNTIME_CONFIGURATION_INVALID"
_STORAGE = "RUNTIME_CONFIGURATION_STORAGE_CONFLICT"
_SQLITE_SUFFIXES = ("", "-wal", "-shm", "-journal")


class RuntimeConfigurationError(ValueError):
    """Stable reason only: never retain a path, JSON value, or credential."""

    def __init__(self, code=_INVALID):
        self.code = code if type(code) is str and code in _ERROR_CODES else _INVALID
        super().__init__(self.code)


class _ConfigurationModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True,
        validate_default=True,
    )


HostPath = Annotated[str, Field(min_length=1, max_length=4096)]
EnvName = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")]
Port = Annotated[int, Field(ge=1, le=65535)]


def _host_path(value):
    # Trusted Host paths, unlike MCP relative paths, may use ../.  Resolve
    # them against the configuration directory, never expand shell variables.
    if (not value.strip() or value in {".", ":memory:"}
            or value.startswith("~") or "$" in value or "://" in value
            or _has_controls(value)):
        raise ValueError("Persistent Host path required")
    return value


def _has_controls(value):
    return any(unicodedata.category(character) == "Cc" for character in value)


def _env_reference(value):
    if value is not None and not _ENV_NAME.fullmatch(value):
        raise ValueError("Environment variable name required")
    return value


def _comparison_path(path):
    # Conservatively refuse spelling aliases even before files exist. This
    # also rejects some distinct paths on case-sensitive filesystems; no
    # writable probe or claim of complete filesystem identity is needed.
    return Path(unicodedata.normalize("NFC", str(path)).casefold())


class RuntimeOrchestratorConfiguration(_ConfigurationModel):
    port: Port
    database_path: HostPath = Field(alias="databasePath")

    _check_path = field_validator("database_path")(_host_path)


class RuntimeAgentConfiguration(_ConfigurationModel):
    port: Port
    database_path: HostPath = Field(alias="databasePath")
    bearer_token_env: EnvName | None = Field(default=None, alias="bearerTokenEnv")

    _check_path = field_validator("database_path")(_host_path)
    _check_reference = field_validator("bearer_token_env")(_env_reference)


class RuntimeAgentsConfiguration(_ConfigurationModel):
    # A fixed frozen object avoids a mutable role dictionary inside a frozen
    # model. There is no fifth role or per-role model/budget override.
    planner: RuntimeAgentConfiguration = Field(alias="PLANNER")
    developer: RuntimeAgentConfiguration = Field(alias="DEVELOPER")
    qa: RuntimeAgentConfiguration = Field(alias="QA")
    security: RuntimeAgentConfiguration = Field(alias="SECURITY")

    @property
    def by_role(self):
        return MappingProxyType({role: getattr(self, role.value.lower()) for role in AgentRole})


class _RuntimeModel(ModelConfiguration):
    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True,
        populate_by_name=False, validate_by_name=False, validate_by_alias=True,
        validate_default=True,
    )
    # Admission matches the implemented provider factory, not an invented
    # adapter. It does not prove account access or model-specific API support.
    provider: Literal["openai"]
    temperature: float = Field(ge=0, le=2, allow_inf_nan=False)
    seed: None = None

    @field_validator("model_revision")
    @classmethod
    def nonblank_revision(cls, value):
        if value is not None and not value.strip():
            raise ValueError("Model revision must not be blank")
        return value.strip() if value is not None else None


class _RuntimeLimits(LLMLimits):
    # Require the operator to specify every cap instead of treating the
    # engine's development defaults as approved runtime policy.
    max_model_calls: int = Field(ge=1, strict=True)
    max_tool_calls: int = Field(ge=0, strict=True)
    max_output_tokens: int = Field(ge=16, strict=True)
    max_total_tokens: int | None = Field(ge=16, strict=True)
    model_timeout_seconds: float = Field(gt=0, strict=True, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(gt=0, strict=True, allow_inf_nan=False)
    max_json_bytes: int = Field(ge=128, strict=True)


class OwnedRuntimeConfiguration(_ConfigurationModel):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    host: Literal["127.0.0.1", "localhost", "::1"] = "127.0.0.1"
    environment: Literal["local", "development", "test", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO", alias="logLevel")
    orchestrator: RuntimeOrchestratorConfiguration
    workspace_root: HostPath = Field(alias="workspaceRoot")
    agents: RuntimeAgentsConfiguration
    model: _RuntimeModel
    api_key_env: EnvName = Field(alias="apiKeyEnv")
    llm_limits: _RuntimeLimits = Field(alias="llmLimits")
    runtime_budget_ms: int = Field(gt=0, alias="runtimeBudgetMs")

    _check_path = field_validator("workspace_root")(_host_path)
    _check_reference = field_validator("api_key_env")(_env_reference)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int or value != 1:
            raise ValueError("Unsupported configuration version")
        return value

    @model_validator(mode="after")
    def independent_roles(self):
        ports = (self.orchestrator.port, *(value.port for value in self.agents.by_role.values()))
        if len(set(ports)) != 5:
            raise ValueError("Platform listeners require distinct ports")
        if self.environment == "production" and any(
            value.bearer_token_env is None for value in self.agents.by_role.values()
        ):
            raise ValueError("Production roles require explicit authentication")
        return self


@dataclass(frozen=True, kw_only=True, repr=False)
class RuntimeLayout:
    orchestrator_database_path: Path
    workspace_root: Path
    agent_database_paths: Mapping
    agent_urls: Mapping

    def __repr__(self):
        return "RuntimeLayout()"


@dataclass(frozen=True, kw_only=True, repr=False)
class ResolvedRuntimeConfiguration:
    orchestrator_settings: Settings = field(repr=False)
    agent_settings: Mapping = field(repr=False)
    model: ModelConfiguration
    limits: LLMLimits
    runtime_budget_ms: int
    orchestrator_host: str
    orchestrator_port: int

    def __repr__(self):
        return "ResolvedRuntimeConfiguration()"


def _validated(configuration):
    try:
        if type(configuration) is not OwnedRuntimeConfiguration:
            raise ValueError
        # Frozen/model_copy/model_construct objects are not admission proofs.
        return OwnedRuntimeConfiguration.model_validate(
            configuration.model_dump(mode="json", by_alias=True, warnings=False)
        )
    except Exception:
        raise RuntimeConfigurationError() from None


def load_runtime_configuration(path) -> OwnedRuntimeConfiguration:
    """Read one bounded regular JSON file; reject symlinks/FIFOs and raw errors."""
    try:
        source = Path(path).absolute()
        descriptor = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIGURATION_BYTES:
                raise ValueError
            raw = stream.read(MAX_CONFIGURATION_BYTES + 1)
        if len(raw) > MAX_CONFIGURATION_BYTES:
            raise ValueError
        document = parse_json(raw.decode("utf-8"), max_bytes=MAX_CONFIGURATION_BYTES)
    except Exception:
        raise RuntimeConfigurationError("RUNTIME_CONFIGURATION_FILE_INVALID") from None
    try:
        configuration = OwnedRuntimeConfiguration.model_validate(document)
    except Exception:
        raise RuntimeConfigurationError() from None
    # Layout errors remain separately identifiable without disclosing paths.
    layout = inspect_runtime_configuration(configuration, base_directory=source.parent)
    try:
        if _comparison_path(source.resolve()).is_relative_to(_comparison_path(layout.workspace_root)):
            raise ValueError
    except Exception:
        raise RuntimeConfigurationError(_STORAGE) from None
    return configuration


def inspect_runtime_configuration(configuration, *, base_directory) -> RuntimeLayout:
    """Check canonical storage/layout only. No credentials or runtime effects."""
    selected = _validated(configuration)
    try:
        base = Path(base_directory).resolve()
        if not base.is_dir():
            raise ValueError

        def resolve(value):
            path = Path(value)
            return (path if path.is_absolute() else base / path).resolve()

        database = resolve(selected.orchestrator.database_path)
        workspace = resolve(selected.workspace_root)
        roles = {role: resolve(value.database_path) for role, value in selected.agents.by_role.items()}
        databases = (database, *roles.values())
        # Config/secrets and platform DBs must stay outside Agent-visible roots.
        workspace_key = _comparison_path(workspace)
        if _comparison_path(base).is_relative_to(workspace_key) or workspace == Path(workspace.anchor):
            raise ValueError
        if workspace.exists() and not workspace.is_dir():
            raise ValueError
        for parent in workspace.parents:
            if parent.exists() and not parent.is_dir():
                raise ValueError
        reserved_paths = tuple(Path(str(path) + suffix).resolve()
            for path in databases for suffix in _SQLITE_SUFFIXES)
        inodes = set()
        path_keys = []
        for path in reserved_paths:
            path_key = _comparison_path(path)
            if path_key.is_relative_to(workspace_key) or workspace_key.is_relative_to(path_key):
                raise ValueError
            for previous_key in path_keys:
                if path_key.is_relative_to(previous_key) or previous_key.is_relative_to(path_key):
                    raise ValueError
            path_keys.append(path_key)
            if path.exists():
                info = path.stat()
                identity = (info.st_dev, info.st_ino)
                if not stat.S_ISREG(info.st_mode) or identity in inodes:
                    raise ValueError
                inodes.add(identity)
            for parent in path.parents:
                if parent.exists() and not parent.is_dir():
                    raise ValueError
        url_host = f"[{selected.host}]" if selected.host == "::1" else selected.host
        urls = {role: f"http://{url_host}:{value.port}" for role, value in selected.agents.by_role.items()}
        return RuntimeLayout(orchestrator_database_path=database, workspace_root=workspace,
            agent_database_paths=MappingProxyType(roles), agent_urls=MappingProxyType(urls))
    except Exception:
        raise RuntimeConfigurationError(_STORAGE) from None


class _ExplicitAgentSettings(AgentSettings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings,)


class _ExplicitOrchestratorSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        return (init_settings,)


def _canonical_settings(settings_type, validated):
    # Validation occurs in the source-disabled subtype. Return the exact
    # existing canonical type required by create_platform, including secrets
    # that AgentSettings deliberately excludes from model_dump.
    return settings_type.model_construct(**{
        name: getattr(validated, name) for name in settings_type.model_fields
    })


def _credential(environ, name):
    # Never fall back to AGENT_*, .env, another key, or a dummy credential.
    if type(name) is not str or not _ENV_NAME.fullmatch(name):
        raise RuntimeConfigurationError()
    value = environ.get(name)
    if value is None or isinstance(value, str) and not value.strip():
        raise RuntimeConfigurationError("RUNTIME_CONFIGURATION_SECRET_MISSING")
    if (type(value) is not str or len(value) > 8192
            or value != value.strip() or _has_controls(value)):
        raise RuntimeConfigurationError("RUNTIME_CONFIGURATION_SECRET_INVALID")
    return SecretStr(value)


def resolve_runtime_configuration(configuration, *, base_directory, environ=None) -> ResolvedRuntimeConfiguration:
    """Explicitly fetch referenced secrets and convert to existing settings.

    Presence is not remote authentication, paid usage approval, or permission
    to start the platform. Run-specific baseline/profile configuration is still
    supplied separately and frozen by the existing Orchestrator.
    """
    selected = _validated(configuration)
    layout = inspect_runtime_configuration(selected, base_directory=base_directory)
    try:
        environment = os.environ if environ is None else environ
        if not isinstance(environment, Mapping):
            raise ValueError
        api_key = _credential(environment, selected.api_key_env)
        tokens = {role: None if value.bearer_token_env is None else
            _credential(environment, value.bearer_token_env)
            for role, value in selected.agents.by_role.items()}
        model = ModelConfiguration.model_validate(selected.model.model_dump(by_alias=True))
        limits = LLMLimits.model_validate(selected.llm_limits.model_dump())
        agents = {}
        for role, value in selected.agents.by_role.items():
            explicit = _ExplicitAgentSettings(_env_file=None,
                role=role, host=selected.host, port=value.port,
                environment=selected.environment, log_level=selected.log_level,
                database_path=layout.agent_database_paths[role], bearer_token=tokens[role],
                llm_provider=model.provider, llm_model_id=model.model_id,
                llm_model_revision=model.model_revision, llm_temperature=model.temperature,
                llm_seed=model.seed, llm_api_key=api_key, llm_limits=limits)
            agents[role] = _canonical_settings(AgentSettings, explicit)
        explicit = _ExplicitOrchestratorSettings(_env_file=None,
            app_name="A2A Orchestrator", environment=selected.environment,
            log_level=selected.log_level, api_prefix="/api/v1",
            database_path=str(layout.orchestrator_database_path), workspace_root=str(layout.workspace_root),
            **{f"{role.value.lower()}_agent_url": layout.agent_urls[role] for role in AgentRole},
            **{f"{role.value.lower()}_bearer_token": tokens[role] for role in AgentRole})
        return ResolvedRuntimeConfiguration(
            orchestrator_settings=_canonical_settings(Settings, explicit),
            agent_settings=MappingProxyType(agents), model=model, limits=limits,
            runtime_budget_ms=selected.runtime_budget_ms,
            orchestrator_host=selected.host, orchestrator_port=selected.orchestrator.port)
    except RuntimeConfigurationError:
        raise
    except Exception:
        raise RuntimeConfigurationError() from None
