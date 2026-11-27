"""Host-selected Bandit profiles only: frozen, bounded and side-effect free."""

from dataclasses import dataclass, field

from mcp_tools.tools.build_config import BuildConfiguration, BuildConfigurationError
from mcp_tools.tools.security_contract import (
    SecurityContractError, _name, _profile_reference, _rule, _version,
    canonical_json, parse_json, validate_security_host_payload,
)
from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxLimits


MAX_SECURITY_CONFIGURATION_BYTES = 64 * 1024
_LIMIT_FIELDS = (
    "cpus", "memory_bytes", "pids", "timeout_seconds", "tmpfs_bytes",
    "max_stdout_bytes", "max_stderr_bytes", "control_timeout_seconds",
)
_PROFILE_FIELDS = frozenset({"name", "scanner_version", "rule_ids", "profile_ref"})
_CONFIG_FIELDS = frozenset({"profiles", "python_executable", "limits", "image_reference", "docker_endpoint"})


class SecurityConfigurationError(ValueError):
    code = "SECURITY_CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class SecurityScannerProfile:
    name: str
    scanner_version: str = field(repr=False)
    rule_ids: tuple[str, ...] = field(repr=False)
    profile_ref: str = field(repr=False)

    def __post_init__(self):
        try:
            _name(self.name)
            _version(self.scanner_version)
            _profile_reference(self.profile_ref)
            if type(self.rule_ids) is not tuple or not 1 <= len(self.rule_ids) <= 128:
                raise SecurityConfigurationError()
            rules = tuple(sorted(_rule(value) for value in self.rule_ids))
            if len(set(rules)) != len(rules) or redact_text(self.profile_ref) != self.profile_ref:
                raise SecurityConfigurationError()
            object.__setattr__(self, "rule_ids", rules)
        except (SecurityConfigurationError, SecurityContractError, TypeError, ValueError, UnicodeError, OverflowError):
            raise SecurityConfigurationError() from None

    @property
    def roles(self):
        return (AgentRole.SECURITY,)


def _copy_profile(profile):
    if type(profile) is not SecurityScannerProfile:
        raise SecurityConfigurationError()
    try:
        return SecurityScannerProfile(name=profile.name, scanner_version=profile.scanner_version,
            rule_ids=profile.rule_ids, profile_ref=profile.profile_ref)
    except AttributeError:
        raise SecurityConfigurationError() from None


def _payload(configuration):
    return {
        "profiles": [{"name": profile.name, "scanner_version": profile.scanner_version,
            "rule_ids": list(profile.rule_ids), "profile_ref": profile.profile_ref} for profile in configuration.profiles],
        "python_executable": configuration.python_executable,
        "limits": {name: getattr(configuration.limits, name) for name in _LIMIT_FIELDS},
        "image_reference": configuration.image_reference, "docker_endpoint": configuration.docker_endpoint,
    }


def _serialize(configuration):
    try:
        return canonical_json(_payload(configuration), max_bytes=MAX_SECURITY_CONFIGURATION_BYTES)
    except (SecurityContractError, TypeError, AttributeError):
        raise SecurityConfigurationError() from None


@dataclass(frozen=True, kw_only=True)
class SecurityScanConfiguration:
    profiles: tuple[SecurityScannerProfile, ...] = field(repr=False)
    python_executable: str = field(default="/usr/local/bin/python", repr=False)
    limits: SandboxLimits = field(default_factory=SandboxLimits, repr=False)
    image_reference: str | None = field(default=None, repr=False)
    docker_endpoint: str = field(default="unix:///var/run/docker.sock", repr=False)

    def __post_init__(self):
        try:
            if type(self.profiles) is not tuple or not 1 <= len(self.profiles) <= 32:
                raise SecurityConfigurationError()
            profiles = tuple(_copy_profile(profile) for profile in self.profiles)
            if len({profile.name for profile in profiles}) != len(profiles):
                raise SecurityConfigurationError()
            validated = BuildConfiguration(profile=ExecutionProfile(name="security-python-policy", tool_name="run_build",
                argv=(self.python_executable,), limits=self.limits, image_reference=self.image_reference),
                docker_endpoint=self.docker_endpoint)
            object.__setattr__(self, "profiles", profiles)
            object.__setattr__(self, "python_executable", validated.profile.argv[0])
            object.__setattr__(self, "limits", validated.profile.limits)
            object.__setattr__(self, "image_reference", validated.profile.image_reference)
            object.__setattr__(self, "docker_endpoint", validated.docker_endpoint)
            _serialize(self)
        except (SecurityConfigurationError, SecurityContractError, BuildConfigurationError, SandboxError,
                AttributeError, TypeError, ValueError, UnicodeError, OverflowError):
            raise SecurityConfigurationError() from None


def _copy_configuration(configuration):
    if type(configuration) is not SecurityScanConfiguration:
        raise SecurityConfigurationError()
    try:
        return SecurityScanConfiguration(**{name: getattr(configuration, name) for name in _CONFIG_FIELDS})
    except AttributeError:
        raise SecurityConfigurationError() from None


def encode_security_configuration(configuration):
    return _serialize(_copy_configuration(configuration))


def decode_security_configuration(text):
    try:
        data = parse_json(text, max_bytes=MAX_SECURITY_CONFIGURATION_BYTES)
        if (type(data) is not dict or "profiles" not in data or not set(data) <= _CONFIG_FIELDS
                or type(data["profiles"]) is not list):
            raise SecurityConfigurationError()
        profiles = []
        for value in data["profiles"]:
            if type(value) is not dict or set(value) != _PROFILE_FIELDS or type(value["rule_ids"]) is not list:
                raise SecurityConfigurationError()
            profiles.append(SecurityScannerProfile(**{**value, "rule_ids": tuple(value["rule_ids"])}))
        limits = data.get("limits", {})
        if type(limits) is not dict or not set(limits) <= set(_LIMIT_FIELDS):
            raise SecurityConfigurationError()
        data["profiles"], data["limits"] = tuple(profiles), SandboxLimits(**limits)
        return SecurityScanConfiguration(**data)
    except (SecurityConfigurationError, SecurityContractError, SandboxError, TypeError, ValueError,
            KeyError, AttributeError, UnicodeError, OverflowError, RecursionError):
        raise SecurityConfigurationError() from None


def security_host_payload(configuration, profile):
    configuration, profile = _copy_configuration(configuration), _copy_profile(profile)
    if profile not in configuration.profiles:
        raise SecurityConfigurationError()
    try:
        return validate_security_host_payload({"scanner": "bandit", "profile_name": profile.name,
            "scanner_version": profile.scanner_version, "rule_ids": list(profile.rule_ids), "profile_ref": profile.profile_ref,
            "ignore_nosec": True, "scan_scope": "ALL_PYTHON"})
    except SecurityContractError:
        raise SecurityConfigurationError() from None
