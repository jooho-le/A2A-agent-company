"""Inert, bounded Host-selected unit-test scopes; never model arguments.

Only Python unittest discovery is supported here. The command is assembled by
the trusted Tool, not accepted as arbitrary argv, Shell, environment or mounts.
Protected test bytes are copied into immutable policy and never use Workspace
tests. The Run's protected-suite reference is checked by the execution Tool.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import re
from types import MappingProxyType
from urllib.parse import urlsplit

from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMRuntimeError
from mcp_tools.tools.build_config import BuildConfiguration, BuildConfigurationError
from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxLimits
from orchestrator.workspaces.policy import WorkspaceAccessError, relative_parts


MAX_UNIT_CONFIGURATION_BYTES = 64 * 1024
MAX_UNIT_FILES = 64
MAX_UNIT_FILE_BYTES = 1024 * 1024
MAX_UNIT_TOTAL_BYTES = 16 * 1024 * 1024
MAX_UNIT_DIRECTORY_DEPTH = 64
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_PATTERN = re.compile(r"[A-Za-z0-9_*?\[\].-]{1,128}\.py\Z")
_KINDS = frozenset({"SNAPSHOT", "QA_TESTS", "PROTECTED"})
_LIMIT_FIELDS = (
    "cpus", "memory_bytes", "pids", "timeout_seconds", "tmpfs_bytes",
    "max_stdout_bytes", "max_stderr_bytes", "control_timeout_seconds",
)
_SCOPE_FIELDS = frozenset({
    "name", "kind", "pattern", "source_directory", "protected_files", "protected_suite_ref",
})


class UnitTestConfigurationError(ValueError):
    """Stable code only: JSON, test bytes, argv and Host paths are excluded."""

    code = "UNIT_TEST_CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _string(value, max_bytes, *, redact=True):
    if not isinstance(value, str) or not value:
        raise UnitTestConfigurationError()
    try:
        if len(value.encode("utf-8")) > max_bytes:
            raise UnitTestConfigurationError()
    except UnicodeError:
        raise UnitTestConfigurationError() from None
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise UnitTestConfigurationError()
    if redact and redact_text(value) != value:
        raise UnitTestConfigurationError()
    return value


def _test_path(value, *, directory=False):
    _string(value, 4096)
    try:
        parts = relative_parts(value)
    except WorkspaceAccessError:
        raise UnitTestConfigurationError() from None
    if (
        parts[0] != "tests" or len(parts) > MAX_UNIT_DIRECTORY_DEPTH
        or (not directory and len(parts) < 2)
        or any(part.casefold().startswith(".mcp-write-") for part in parts)
    ):
        raise UnitTestConfigurationError()
    return value


def _suite_reference(value):
    value = _string(value, 4096)
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise UnitTestConfigurationError() from None
    # A logical Artifact or HTTPS reference, never a Host path/file/socket URL.
    # Fetching is deliberately absent: policy includes the approved bytes.
    if (
        parsed.scheme not in {"artifact", "https"} or not parsed.netloc
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment or not parsed.path.startswith("/")
        or "\\" in value or "%" in value
    ):
        raise UnitTestConfigurationError()
    try:
        relative_parts(parsed.path[1:])
    except WorkspaceAccessError:
        raise UnitTestConfigurationError() from None
    return value


def _protected_files(value):
    if not isinstance(value, Mapping) or not 1 <= len(value) <= MAX_UNIT_FILES:
        raise UnitTestConfigurationError()
    copied = {}
    total = 0
    for path, content in value.items():
        _test_path(path)
        if not isinstance(content, str):
            raise UnitTestConfigurationError()
        try:
            size = len(content.encode("utf-8"))
            sanitize_content({"content": content}, source_fields=("content",), reject_secrets=True)
        except (UnicodeError, LLMRuntimeError):
            raise UnitTestConfigurationError() from None
        if size > MAX_UNIT_FILE_BYTES or "\x00" in content:
            raise UnitTestConfigurationError()
        total += size
        if total > MAX_UNIT_TOTAL_BYTES:
            raise UnitTestConfigurationError()
        copied[path] = content
    # A file cannot also be a parent directory. Sorting is deterministic and
    # does not inspect or normalize a Host path.
    for path in copied:
        parts = path.split("/")
        if any("/".join(parts[:index]) in copied for index in range(1, len(parts))):
            raise UnitTestConfigurationError()
    return MappingProxyType(dict(sorted(copied.items())))


@dataclass(frozen=True, kw_only=True)
class UnitTestScope:
    name: str
    kind: str
    pattern: str = field(default="test_*.py", repr=False)
    source_directory: str = field(default="tests", repr=False)
    protected_files: Mapping[str, str] | None = field(default=None, repr=False)
    protected_suite_ref: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise UnitTestConfigurationError()
        if not isinstance(self.kind, str) or self.kind not in _KINDS:
            raise UnitTestConfigurationError()
        if (not isinstance(self.pattern, str) or _PATTERN.fullmatch(self.pattern) is None
                or self.pattern.startswith("-")):
            raise UnitTestConfigurationError()
        _test_path(self.source_directory, directory=True)
        if self.kind == "PROTECTED":
            object.__setattr__(self, "protected_files", _protected_files(self.protected_files))
            object.__setattr__(self, "protected_suite_ref", _suite_reference(self.protected_suite_ref))
        elif self.protected_files is not None or self.protected_suite_ref is not None:
            raise UnitTestConfigurationError()

    @property
    def roles(self):
        return (AgentRole.DEVELOPER,) if self.kind == "SNAPSHOT" else (AgentRole.QA,)


def _copy_scope(value):
    if type(value) is not UnitTestScope:
        raise UnitTestConfigurationError()
    return UnitTestScope(
        name=value.name, kind=value.kind, pattern=value.pattern,
        source_directory=value.source_directory, protected_files=value.protected_files,
        protected_suite_ref=value.protected_suite_ref,
    )


def _payload(configuration):
    return {
        "scopes": [
            {
                "name": scope.name, "kind": scope.kind, "pattern": scope.pattern,
                "source_directory": scope.source_directory,
                "protected_files": None if scope.protected_files is None else dict(scope.protected_files),
                "protected_suite_ref": scope.protected_suite_ref,
            }
            for scope in configuration.scopes
        ],
        "python_executable": configuration.python_executable,
        "limits": {name: getattr(configuration.limits, name) for name in _LIMIT_FIELDS},
        "image_reference": configuration.image_reference,
        "docker_endpoint": configuration.docker_endpoint,
    }


def _serialize(configuration):
    try:
        text = json.dumps(_payload(configuration), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_UNIT_CONFIGURATION_BYTES:
            raise UnitTestConfigurationError()
        return text
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise UnitTestConfigurationError() from None


@dataclass(frozen=True, kw_only=True)
class UnitTestConfiguration:
    scopes: tuple[UnitTestScope, ...] = field(repr=False)
    python_executable: str = field(default="/usr/local/bin/python", repr=False)
    limits: SandboxLimits = field(default_factory=SandboxLimits, repr=False)
    image_reference: str | None = field(default=None, repr=False)
    docker_endpoint: str = field(default="unix:///var/run/docker.sock", repr=False)

    def __post_init__(self):
        if not isinstance(self.scopes, tuple) or not 1 <= len(self.scopes) <= 32:
            raise UnitTestConfigurationError()
        scopes = tuple(_copy_scope(scope) for scope in self.scopes)
        if len({scope.name for scope in scopes}) != len(scopes):
            raise UnitTestConfigurationError()
        try:
            validated = BuildConfiguration(
                profile=ExecutionProfile(
                    name="unit-host-policy", tool_name="run_build", argv=(self.python_executable,),
                    limits=self.limits, image_reference=self.image_reference,
                ),
                docker_endpoint=self.docker_endpoint,
            )
        except (BuildConfigurationError, SandboxError, TypeError, ValueError):
            raise UnitTestConfigurationError() from None
        object.__setattr__(self, "scopes", scopes)
        object.__setattr__(self, "python_executable", validated.profile.argv[0])
        object.__setattr__(self, "limits", validated.profile.limits)
        object.__setattr__(self, "image_reference", validated.profile.image_reference)
        object.__setattr__(self, "docker_endpoint", validated.docker_endpoint)
        _serialize(self)


def encode_unit_configuration(configuration: UnitTestConfiguration) -> str:
    if type(configuration) is not UnitTestConfiguration:
        raise UnitTestConfigurationError()
    return _serialize(UnitTestConfiguration(
        scopes=configuration.scopes, python_executable=configuration.python_executable,
        limits=configuration.limits, image_reference=configuration.image_reference,
        docker_endpoint=configuration.docker_endpoint,
    ))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise UnitTestConfigurationError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise UnitTestConfigurationError()


def decode_unit_configuration(text: str) -> UnitTestConfiguration:
    if not isinstance(text, str):
        raise UnitTestConfigurationError()
    try:
        if len(text.encode("utf-8")) > MAX_UNIT_CONFIGURATION_BYTES:
            raise UnitTestConfigurationError()
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
        if (
            type(data) is not dict or "scopes" not in data
            or not set(data) <= {"scopes", "python_executable", "limits", "image_reference", "docker_endpoint"}
            or type(data["scopes"]) is not list
        ):
            raise UnitTestConfigurationError()
        scopes = []
        for scope in data["scopes"]:
            if type(scope) is not dict or not {"name", "kind"} <= set(scope) or not set(scope) <= _SCOPE_FIELDS:
                raise UnitTestConfigurationError()
            scopes.append(UnitTestScope(**scope))
        limits = data.get("limits", {})
        if type(limits) is not dict or not set(limits) <= set(_LIMIT_FIELDS):
            raise UnitTestConfigurationError()
        return UnitTestConfiguration(
            scopes=tuple(scopes), python_executable=data.get("python_executable", "/usr/local/bin/python"),
            limits=SandboxLimits(**limits), image_reference=data.get("image_reference"),
            docker_endpoint=data.get("docker_endpoint", "unix:///var/run/docker.sock"),
        )
    except (UnitTestConfigurationError, SandboxError, ValueError, TypeError, KeyError,
            AttributeError, UnicodeError, OverflowError, RecursionError):
        raise UnitTestConfigurationError() from None
