"""Inert, bounded Host-approved browser runner and local service policy.

The Model chooses a suite name, never service argv, origin, browser version,
Shell, network, mounts or environment. Configuration does not run anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re

from mcp_tools.tools.browser_contract import BrowserContractError, parse_browser_suite, validate_local_path
from mcp_tools.tools.build_config import BuildConfiguration, BuildConfigurationError
from mcp_tools.tools.unit_config import UnitTestConfigurationError, UnitTestScope, _test_path
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxLimits


MAX_BROWSER_CONFIGURATION_BYTES = 64 * 1024
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_ORIGIN = re.compile(r"http://127\.0\.0\.1:([1-9][0-9]{3,4})\Z")
_LIMIT_FIELDS = (
    "cpus", "memory_bytes", "pids", "timeout_seconds", "tmpfs_bytes",
    "max_stdout_bytes", "max_stderr_bytes", "control_timeout_seconds",
)
_SUITE_FIELDS = frozenset({"name", "kind", "suite_path", "protected_files", "protected_suite_ref"})
_CONFIG_FIELDS = frozenset({
    "suites", "service_argv", "python_executable", "base_url", "ready_path",
    "startup_timeout_seconds", "action_timeout_ms", "playwright_version", "limits",
    "image_reference", "docker_endpoint",
})
HOST_FIELDS = frozenset({
    "suite_name", "suite_path", "service_argv", "base_url", "ready_path",
    "startup_timeout_seconds", "action_timeout_ms", "playwright_version",
})


class BrowserConfigurationError(ValueError):
    code = "BROWSER_CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class BrowserTestSuite:
    name: str
    kind: str
    suite_path: str = field(default="tests/browser/suite.json", repr=False)
    protected_files: Mapping[str, str] | None = field(default=None, repr=False)
    protected_suite_ref: str | None = field(default=None, repr=False)

    def __post_init__(self):
        try:
            if type(self.name) is not str or _NAME.fullmatch(self.name) is None or self.kind not in {"QA_TESTS", "PROTECTED"}:
                raise BrowserConfigurationError()
            _test_path(self.suite_path)
            if not self.suite_path.endswith(".json"):
                raise BrowserConfigurationError()
            copied = UnitTestScope(name=self.name, kind=self.kind, protected_files=self.protected_files,
                                   protected_suite_ref=self.protected_suite_ref)
            object.__setattr__(self, "protected_files", copied.protected_files)
            object.__setattr__(self, "protected_suite_ref", copied.protected_suite_ref)
            if self.kind == "PROTECTED":
                if self.suite_path not in copied.protected_files:
                    raise BrowserConfigurationError()
                parse_browser_suite(copied.protected_files[self.suite_path], self.name)
        except (BrowserConfigurationError, UnitTestConfigurationError, BrowserContractError, TypeError, AttributeError):
            raise BrowserConfigurationError() from None

    @property
    def roles(self):
        return (AgentRole.QA,)


def _copy_suite(value):
    if type(value) is not BrowserTestSuite:
        raise BrowserConfigurationError()
    return BrowserTestSuite(name=value.name, kind=value.kind, suite_path=value.suite_path,
                            protected_files=value.protected_files, protected_suite_ref=value.protected_suite_ref)


def _payload(configuration):
    return {
        "suites": [{"name": suite.name, "kind": suite.kind, "suite_path": suite.suite_path,
                    "protected_files": None if suite.protected_files is None else dict(suite.protected_files),
                    "protected_suite_ref": suite.protected_suite_ref} for suite in configuration.suites],
        "service_argv": list(configuration.service_argv), "python_executable": configuration.python_executable,
        "base_url": configuration.base_url, "ready_path": configuration.ready_path,
        "startup_timeout_seconds": configuration.startup_timeout_seconds,
        "action_timeout_ms": configuration.action_timeout_ms, "playwright_version": configuration.playwright_version,
        "limits": {name: getattr(configuration.limits, name) for name in _LIMIT_FIELDS},
        "image_reference": configuration.image_reference, "docker_endpoint": configuration.docker_endpoint,
    }


def _serialize(configuration):
    try:
        text = json.dumps(_payload(configuration), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_BROWSER_CONFIGURATION_BYTES:
            raise BrowserConfigurationError()
        return text
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise BrowserConfigurationError() from None


@dataclass(frozen=True, kw_only=True)
class BrowserTestConfiguration:
    suites: tuple[BrowserTestSuite, ...] = field(repr=False)
    service_argv: tuple[str, ...] = field(repr=False)
    playwright_version: str = field(repr=False)
    python_executable: str = field(default="/usr/local/bin/python", repr=False)
    base_url: str = field(default="http://127.0.0.1:8765", repr=False)
    ready_path: str = field(default="/", repr=False)
    startup_timeout_seconds: float = field(default=10, repr=False)
    action_timeout_ms: int = field(default=5000, repr=False)
    limits: SandboxLimits = field(default_factory=SandboxLimits, repr=False)
    image_reference: str | None = field(default=None, repr=False)
    docker_endpoint: str = field(default="unix:///var/run/docker.sock", repr=False)

    def __post_init__(self):
        try:
            if type(self.suites) is not tuple or not 1 <= len(self.suites) <= 32:
                raise BrowserConfigurationError()
            suites = tuple(_copy_suite(suite) for suite in self.suites)
            if len({suite.name for suite in suites}) != len(suites):
                raise BrowserConfigurationError()
            if type(self.service_argv) is not tuple:
                raise BrowserConfigurationError()
            service = BuildConfiguration(profile=ExecutionProfile(name="browser-service-policy", tool_name="run_build",
                argv=self.service_argv, limits=self.limits, image_reference=self.image_reference), docker_endpoint=self.docker_endpoint)
            python = BuildConfiguration(profile=ExecutionProfile(name="browser-python-policy", tool_name="run_build",
                argv=(self.python_executable,), limits=self.limits, image_reference=self.image_reference), docker_endpoint=self.docker_endpoint)
            origin = _ORIGIN.fullmatch(self.base_url) if type(self.base_url) is str else None
            if origin is None or not 1024 <= int(origin.group(1)) <= 65535:
                raise BrowserConfigurationError()
            validate_local_path(self.ready_path)
            if (type(self.playwright_version) is not str or len(self.playwright_version) > 32
                    or _VERSION.fullmatch(self.playwright_version) is None):
                raise BrowserConfigurationError()
            if (type(self.startup_timeout_seconds) not in (int, float)
                    or not math.isfinite(self.startup_timeout_seconds)
                    or not 0 < self.startup_timeout_seconds <= 60):
                raise BrowserConfigurationError()
            if type(self.action_timeout_ms) is not int or not 1 <= self.action_timeout_ms <= 30_000:
                raise BrowserConfigurationError()
            object.__setattr__(self, "suites", suites)
            object.__setattr__(self, "service_argv", service.profile.argv)
            object.__setattr__(self, "python_executable", python.profile.argv[0])
            object.__setattr__(self, "limits", service.profile.limits)
            object.__setattr__(self, "image_reference", service.profile.image_reference)
            object.__setattr__(self, "docker_endpoint", service.docker_endpoint)
            _serialize(self)
        except (BrowserConfigurationError, BuildConfigurationError, BrowserContractError, SandboxError,
                AttributeError, TypeError, ValueError, OverflowError):
            raise BrowserConfigurationError() from None


def _copy_configuration(configuration):
    if type(configuration) is not BrowserTestConfiguration:
        raise BrowserConfigurationError()
    return BrowserTestConfiguration(**{name: getattr(configuration, name) for name in _CONFIG_FIELDS})


def encode_browser_configuration(configuration):
    return _serialize(_copy_configuration(configuration))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrowserConfigurationError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise BrowserConfigurationError()


def decode_browser_configuration(text):
    try:
        if type(text) is not str or len(text.encode("utf-8")) > MAX_BROWSER_CONFIGURATION_BYTES:
            raise BrowserConfigurationError()
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
        if (type(data) is not dict or not {"suites", "service_argv", "playwright_version"} <= set(data)
                or not set(data) <= _CONFIG_FIELDS or type(data["suites"]) is not list
                or type(data["service_argv"]) is not list):
            raise BrowserConfigurationError()
        suites = []
        for value in data["suites"]:
            if type(value) is not dict or not {"name", "kind"} <= set(value) or not set(value) <= _SUITE_FIELDS:
                raise BrowserConfigurationError()
            suites.append(BrowserTestSuite(**value))
        limits = data.get("limits", {})
        if type(limits) is not dict or not set(limits) <= set(_LIMIT_FIELDS):
            raise BrowserConfigurationError()
        data["suites"], data["service_argv"], data["limits"] = tuple(suites), tuple(data["service_argv"]), SandboxLimits(**limits)
        return BrowserTestConfiguration(**data)
    except (BrowserConfigurationError, BrowserContractError, SandboxError, ValueError, TypeError,
            KeyError, AttributeError, UnicodeError, OverflowError, RecursionError):
        raise BrowserConfigurationError() from None


def browser_host_payload(configuration, suite):
    """Copy the exact closed /inputs/_browser_host.json runner payload."""
    configuration, suite = _copy_configuration(configuration), _copy_suite(suite)
    if suite not in configuration.suites:
        raise BrowserConfigurationError()
    return {
        "suite_name": suite.name, "suite_path": suite.suite_path, "service_argv": list(configuration.service_argv),
        "base_url": configuration.base_url, "ready_path": configuration.ready_path,
        "startup_timeout_seconds": configuration.startup_timeout_seconds,
        "action_timeout_ms": configuration.action_timeout_ms, "playwright_version": configuration.playwright_version,
    }


def validate_browser_host_payload(data):
    """Validate runner policy bytes without executing or inspecting Host paths."""
    if type(data) is not dict or set(data) != HOST_FIELDS or type(data["service_argv"]) is not list:
        raise BrowserConfigurationError()
    configuration = BrowserTestConfiguration(suites=(BrowserTestSuite(name=data["suite_name"], kind="QA_TESTS",
        suite_path=data["suite_path"]),), service_argv=tuple(data["service_argv"]), playwright_version=data["playwright_version"],
        base_url=data["base_url"], ready_path=data["ready_path"], startup_timeout_seconds=data["startup_timeout_seconds"],
        action_timeout_ms=data["action_timeout_ms"])
    return browser_host_payload(configuration, configuration.suites[0])
