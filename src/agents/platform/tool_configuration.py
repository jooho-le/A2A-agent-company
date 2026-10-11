"""Offline, operator-only bundle for the existing four sandboxed tools.

No import, decode or property access starts Docker, installs dependencies,
loads environment defaults, or creates storage. Only the explicit loader reads
the configuration and dependency lock. This is not a new MCP input contract.
"""

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import unicodedata

from agents.llm.content import sanitize_content
from agents.llm.contracts import parse_json
from mcp_tools.tools.build_config import decode_build_configuration, encode_build_configuration
from mcp_tools.tools.unit_config import decode_unit_configuration, encode_unit_configuration
from mcp_tools.tools.browser_config import decode_browser_configuration, encode_browser_configuration
from mcp_tools.tools.security_config import decode_security_configuration, encode_security_configuration
from orchestrator.domain.run_configuration import ExecutionBaseline
from orchestrator.domain.states import AgentRole


MAX_CONFIGURATION_BYTES = 262_144
MAX_LOCK_BYTES = 1_048_576
_LIMITS = frozenset({
    "cpus", "memory_bytes", "pids", "timeout_seconds", "tmpfs_bytes",
    "max_stdout_bytes", "max_stderr_bytes", "control_timeout_seconds",
})
_FIELDS = frozenset({
    "schemaVersion", "imageReference", "dependencyLockFile", "dependencyLockHash",
    "hardwareProfile", "dockerEndpoint", "maxCallSeconds", "build", "unit", "browser", "security",
})
_CODES = frozenset({
    "TOOL_CONFIGURATION_INVALID", "TOOL_CONFIGURATION_FILE_INVALID", "TOOL_CONFIGURATION_LOCK_MISMATCH",
})


class ToolConfigurationError(ValueError):
    """Stable, credential/path-safe reason only."""

    def __init__(self, code="TOOL_CONFIGURATION_INVALID"):
        self.code = code if type(code) is str and code in _CODES else "TOOL_CONFIGURATION_INVALID"
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > maximum
            or any(unicodedata.category(char) == "Cc" for char in value)):
        raise ToolConfigurationError()
    return value


def _path(value):
    _text(value, 4096)
    if value in {".", ":memory:"} or value.startswith("~") or "$" in value or "://" in value:
        raise ToolConfigurationError()
    return value


@dataclass(frozen=True, kw_only=True, repr=False)
class OwnedToolConfiguration:
    """Canonical JSON strings keep nested operator input deeply immutable.

    Properties return fresh existing configuration types, never mutable shared
    dictionaries. This object holds policy, not proof of image readiness.
    """

    image_reference: str
    dependency_lock_file: str
    dependency_lock_hash: str
    hardware_profile: str
    docker_endpoint: str
    max_call_seconds: float
    _build_json: str = field(repr=False)
    _unit_json: str = field(repr=False)
    _browser_json: str = field(repr=False)
    _security_json: str = field(repr=False)

    def __repr__(self):
        return "OwnedToolConfiguration()"

    def __post_init__(self):
        try:
            _path(self.dependency_lock_file)
            _text(self.hardware_profile, 256)
            if (type(self.dependency_lock_hash) is not str
                    or re.fullmatch(r"sha256:[0-9a-f]{64}", self.dependency_lock_hash) is None
                    or type(self.max_call_seconds) not in (int, float)
                    or not math.isfinite(self.max_call_seconds) or not 0 < self.max_call_seconds <= 600):
                raise ToolConfigurationError()
            # A trusted Host can also construct/replace this dataclass directly.
            # That path must not restore the old codecs' provisional defaults.
            _explicit_fields(*(parse_json(value, max_bytes=MAX_CONFIGURATION_BYTES) for value in (
                self._build_json, self._unit_json, self._browser_json, self._security_json)), canonical=True)
            build, unit, browser, security = (
                decode_build_configuration(self._build_json), decode_unit_configuration(self._unit_json),
                decode_browser_configuration(self._browser_json), decode_security_configuration(self._security_json),
            )
            for image, endpoint, limits in (
                (build.profile.image_reference, build.docker_endpoint, build.profile.limits),
                *((item.image_reference, item.docker_endpoint, item.limits) for item in (unit, browser, security)),
            ):
                if (image is None or image != self.image_reference or endpoint != self.docker_endpoint
                        or self.max_call_seconds < limits.timeout_seconds + 2 * limits.control_timeout_seconds + 1):
                    raise ToolConfigurationError()
            if (unit.python_executable != browser.python_executable
                    or unit.python_executable != security.python_executable
                    or not any(item.kind == "SNAPSHOT" for item in unit.scopes)
                    or not any(item.kind == "QA_TESTS" for item in unit.scopes)
                    or not any(item.kind == "QA_TESTS" for item in browser.suites)
                    or len({item.scanner_version for item in security.profiles}) != 1):
                raise ToolConfigurationError()
            sanitize_content({"hardwareProfile": self.hardware_profile}, reject_secrets=True)
            # Reuse the original execution baseline; network never configurable.
            self.baseline
            for name, encoder, configuration in (
                ("_build_json", encode_build_configuration, build), ("_unit_json", encode_unit_configuration, unit),
                ("_browser_json", encode_browser_configuration, browser),
                ("_security_json", encode_security_configuration, security),
            ):
                object.__setattr__(self, name, encoder(configuration))
        except Exception:
            raise ToolConfigurationError() from None

    @property
    def baseline(self):
        return ExecutionBaseline(
            containerImageDigest=self.image_reference.rsplit("@", 1)[-1],
            dependencyLockHash=self.dependency_lock_hash, hardwareProfile=self.hardware_profile,
            networkPolicy="DENY", allowedHosts=(),
        )

    @property
    def build_configuration(self):
        return decode_build_configuration(self._build_json)

    def unit_configuration_for(self, role):
        if role not in (AgentRole.DEVELOPER, AgentRole.QA) or type(role) is not AgentRole:
            raise ToolConfigurationError()
        configuration = decode_unit_configuration(self._unit_json)
        # Developer gets Source tests only; QA gets generated and protected tests.
        return replace(configuration, scopes=tuple(item for item in configuration.scopes if role in item.roles))

    @property
    def browser_configuration(self):
        return decode_browser_configuration(self._browser_json)

    @property
    def security_configuration(self):
        return decode_security_configuration(self._security_json)


def _limits(value):
    if type(value) is not dict or set(value) != _LIMITS:
        raise ToolConfigurationError()


def _explicit_fields(build, unit, browser, security, *, canonical=False):
    common = {"image_reference", "docker_endpoint"} if canonical else set()
    for child in (build, unit, browser, security):
        if type(child) is not dict or not canonical and {"image_reference", "docker_endpoint"} & set(child):
            raise ToolConfigurationError()
    build_fields = {"profile", "docker_endpoint"} if canonical else {"profile"}
    if set(build) != build_fields or type(build["profile"]) is not dict:
        raise ToolConfigurationError()
    profile = build["profile"]
    if (not {"name", "argv", "limits"} <= set(profile)
            or not canonical and "image_reference" in profile
            or canonical and not {"image_reference", "tool_name"} <= set(profile)):
        raise ToolConfigurationError()
    _limits(profile["limits"])
    for child, required in (
        (unit, {"scopes", "python_executable", "limits"}),
        (browser, {"suites", "service_argv", "playwright_version", "python_executable", "base_url",
                   "ready_path", "startup_timeout_seconds", "action_timeout_ms", "limits"}),
        (security, {"profiles", "python_executable", "limits"}),
    ):
        if set(child) != required | common:
            raise ToolConfigurationError()
        _limits(child["limits"])


def decode_tool_configuration(text):
    """Pure closed JSON decode: no files, credentials, Docker or callbacks."""
    try:
        data = parse_json(text, max_bytes=MAX_CONFIGURATION_BYTES)
        if type(data) is not dict or set(data) != _FIELDS or type(data["schemaVersion"]) is not int or data["schemaVersion"] != 1:
            raise ToolConfigurationError()
        image, endpoint = data["imageReference"], data["dockerEndpoint"]
        build, unit, browser, security = (data[key] for key in ("build", "unit", "browser", "security"))
        _explicit_fields(build, unit, browser, security)
        profile = build["profile"]
        build = {"profile": {**profile, "image_reference": image}, "docker_endpoint": endpoint}
        unit, browser, security = ({**child, "image_reference": image, "docker_endpoint": endpoint}
                                   for child in (unit, browser, security))
        return OwnedToolConfiguration(
            image_reference=image, dependency_lock_file=data["dependencyLockFile"],
            dependency_lock_hash=data["dependencyLockHash"], hardware_profile=data["hardwareProfile"],
            docker_endpoint=endpoint, max_call_seconds=data["maxCallSeconds"],
            _build_json=encode_build_configuration(decode_build_configuration(_json(build))),
            _unit_json=encode_unit_configuration(decode_unit_configuration(_json(unit))),
            _browser_json=encode_browser_configuration(decode_browser_configuration(_json(browser))),
            _security_json=encode_security_configuration(decode_security_configuration(_json(security))),
        )
    except Exception:
        raise ToolConfigurationError() from None


def _read_regular(path, maximum):
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= maximum:
            raise ToolConfigurationError()
        raw = bytearray()
        while len(raw) <= maximum:
            chunk = os.read(descriptor, min(65536, maximum + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        if (len(raw) != before.st_size or len(raw) > maximum
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise ToolConfigurationError()
        return bytes(raw)
    except Exception:
        raise ToolConfigurationError("TOOL_CONFIGURATION_FILE_INVALID") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def load_tool_configuration(path):
    """Explicit regular-file reads; lock identity is not installed-image proof."""
    try:
        path = Path(path).absolute()
        raw = _read_regular(path, MAX_CONFIGURATION_BYTES)
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise ToolConfigurationError("TOOL_CONFIGURATION_FILE_INVALID") from None
        configuration = decode_tool_configuration(text)
        lock_path = Path(configuration.dependency_lock_file)
        if not lock_path.is_absolute():
            lock_path = path.parent / lock_path
        lock = _read_regular(lock_path, MAX_LOCK_BYTES)
        if "sha256:" + hashlib.sha256(lock).hexdigest() != configuration.dependency_lock_hash:
            raise ToolConfigurationError("TOOL_CONFIGURATION_LOCK_MISMATCH")
        return configuration
    except ToolConfigurationError:
        raise
    except Exception:
        raise ToolConfigurationError("TOOL_CONFIGURATION_FILE_INVALID") from None
