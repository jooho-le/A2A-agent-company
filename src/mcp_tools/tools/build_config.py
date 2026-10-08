"""Inert, closed Host-only build policy; never a Model or MCP Tool argument.

JSON decoding and validation do not discover Docker, read environment/files,
or spawn processes. There is deliberately no default build command. The Host
must approve a container executable/argv and the existing bounded resources.
"""

from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit

from orchestrator.core.security import redact_text
from orchestrator.sandbox.contracts import ExecutionProfile, SandboxError, SandboxLimits


MAX_BUILD_CONFIGURATION_BYTES = 16 * 1024
_LIMIT_FIELDS = (
    "cpus", "memory_bytes", "pids", "timeout_seconds", "tmpfs_bytes",
    "max_stdout_bytes", "max_stderr_bytes", "control_timeout_seconds",
)
_PROFILE_FIELDS = frozenset({"name", "tool_name", "argv", "limits", "image_reference"})
_SHELL_EXECUTABLES = frozenset({
    "sh", "bash", "dash", "zsh", "ksh", "csh", "tcsh", "fish",
    "powershell", "pwsh", "cmd", "cmd.exe", "env", "busybox",
})
_CREDENTIAL_FLAG = re.compile(
    r"^--?(?:[a-z0-9]+[-_])*(?:password|passwd|pwd|token|(?:access|refresh|id|api)[-_]?token|"
    r"api[-_]?key|(?:api|client)[-_]?secret|secret(?:[-_]?key)?|authorization)"
    r"(?:$|[=:\s])", re.IGNORECASE,
)


class BuildConfigurationError(ValueError):
    """Only a stable code, never JSON, credential, argv, or Host paths."""

    code = "BUILD_CONFIGURATION_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _string(value: object, *, max_bytes: int, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise BuildConfigurationError()
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise BuildConfigurationError() from None
    if size > max_bytes or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value):
        raise BuildConfigurationError()
    return value


def _absolute_path(value: str) -> None:
    # POSIX container/socket paths are checked lexically, never resolved on the
    # Host. URL escaping, backslashes, empty/dot segments are not canonical.
    if (
        not value.startswith("/") or "\\" in value or "%" in value
        or any(part in {"", ".", ".."} for part in value.split("/")[1:])
    ):
        raise BuildConfigurationError()


def _endpoint(value: object) -> str:
    endpoint = _string(value, max_bytes=1024)
    if redact_text(endpoint) != endpoint:
        raise BuildConfigurationError()
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise BuildConfigurationError() from None
    if (
        parsed.scheme != "unix" or parsed.netloc or parsed.query or parsed.fragment
        or endpoint != "unix://" + parsed.path
    ):
        raise BuildConfigurationError()
    _absolute_path(parsed.path)
    return endpoint


def _copy_profile(value: object) -> ExecutionProfile:
    if type(value) is not ExecutionProfile or type(value.limits) is not SandboxLimits:
        raise BuildConfigurationError()
    if value.tool_name != "run_build":
        raise BuildConfigurationError()
    name = _string(value.name, max_bytes=64)
    if not isinstance(value.argv, tuple) or not 1 <= len(value.argv) <= 64:
        raise BuildConfigurationError()
    for arg in value.argv:
        _string(arg, max_bytes=8192, nonempty=False)
        if redact_text(arg) != arg or _CREDENTIAL_FLAG.search(arg):
            raise BuildConfigurationError()
    executable = value.argv[0]
    _string(executable, max_bytes=256)
    _absolute_path(executable)
    if executable.rsplit("/", 1)[-1].casefold() in _SHELL_EXECUTABLES:
        raise BuildConfigurationError()
    if value.image_reference is not None:
        _string(value.image_reference, max_bytes=512)
        if redact_text(value.image_reference) != value.image_reference:
            raise BuildConfigurationError()
    try:
        limits = SandboxLimits(**{name: getattr(value.limits, name) for name in _LIMIT_FIELDS})
        return ExecutionProfile(
            name=name, tool_name="run_build", argv=value.argv, limits=limits,
            image_reference=value.image_reference,
        )
    except (SandboxError, AttributeError, TypeError, ValueError, OverflowError):
        raise BuildConfigurationError() from None


def _payload(configuration: "BuildConfiguration") -> dict:
    profile = configuration.profile
    return {
        "profile": {
            "name": profile.name, "tool_name": "run_build", "argv": list(profile.argv),
            "limits": {name: getattr(profile.limits, name) for name in _LIMIT_FIELDS},
            "image_reference": profile.image_reference,
        },
        "docker_endpoint": configuration.docker_endpoint,
    }


def _serialize(configuration: "BuildConfiguration") -> str:
    try:
        text = json.dumps(_payload(configuration), ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > MAX_BUILD_CONFIGURATION_BYTES:
            raise BuildConfigurationError()
        return text
    except (TypeError, ValueError, UnicodeError, OverflowError):
        raise BuildConfigurationError() from None


@dataclass(frozen=True, kw_only=True)
class BuildConfiguration:
    """Fixed Host-selected argv, limits and local Docker capability only."""

    profile: ExecutionProfile = field(repr=False)
    docker_endpoint: str = field(default="unix:///var/run/docker.sock", repr=False)

    def __post_init__(self):
        object.__setattr__(self, "profile", _copy_profile(self.profile))
        object.__setattr__(self, "docker_endpoint", _endpoint(self.docker_endpoint))
        _serialize(self)


def encode_build_configuration(configuration: BuildConfiguration) -> str:
    """Serialize only explicitly selected validated Host policy, not defaults."""
    if type(configuration) is not BuildConfiguration:
        raise BuildConfigurationError()
    # Recheck forged or subsequently object.__setattr__-modified dataclasses.
    return _serialize(BuildConfiguration(
        profile=configuration.profile, docker_endpoint=configuration.docker_endpoint,
    ))


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BuildConfigurationError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise BuildConfigurationError()


def decode_build_configuration(text: str) -> BuildConfiguration:
    """Decode a bounded closed JSON object; unknown/duplicate fields fail closed.

    profile.name and profile.argv are required. Optional tool_name, when given,
    must be run_build; omitted limits use SandboxLimits, not a build command.
    """
    if not isinstance(text, str):
        raise BuildConfigurationError()
    try:
        if len(text.encode("utf-8")) > MAX_BUILD_CONFIGURATION_BYTES:
            raise BuildConfigurationError()
        data = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
        if type(data) is not dict or not set(data) <= {"profile", "docker_endpoint"} or "profile" not in data:
            raise BuildConfigurationError()
        profile = data["profile"]
        if (
            type(profile) is not dict or not {"name", "argv"} <= set(profile)
            or not set(profile) <= _PROFILE_FIELDS or type(profile["argv"]) is not list
        ):
            raise BuildConfigurationError()
        limits = profile.get("limits", {})
        if type(limits) is not dict or not set(limits) <= set(_LIMIT_FIELDS):
            raise BuildConfigurationError()
        return BuildConfiguration(
            profile=ExecutionProfile(
                name=profile["name"], tool_name=profile.get("tool_name", "run_build"),
                argv=tuple(profile["argv"]), limits=SandboxLimits(**limits),
                image_reference=profile.get("image_reference"),
            ),
            docker_endpoint=data.get("docker_endpoint", "unix:///var/run/docker.sock"),
        )
    except (SandboxError, BuildConfigurationError, ValueError, TypeError, KeyError,
            AttributeError, UnicodeError, OverflowError, RecursionError):
        raise BuildConfigurationError() from None
