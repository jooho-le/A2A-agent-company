"""Bounded Host-only execution policy and sanitized, non-verdict results."""

from dataclasses import dataclass, field
from enum import Enum
import math
import re
from uuid import UUID

from orchestrator.domain.snapshot_handoff import ExecutionManifest


class SandboxErrorCode(str, Enum):
    UNAVAILABLE = "SANDBOX_UNAVAILABLE"
    INVALID = "SANDBOX_INPUT_INVALID"
    DENIED = "SANDBOX_PERMISSION_DENIED"
    CONFIGURATION = "SANDBOX_CONFIGURATION_REQUIRED"
    IMAGE = "SANDBOX_IMAGE_MISMATCH"
    INTEGRITY = "SANDBOX_INTEGRITY_ERROR"
    PATH = "PATH_DENIED"
    OUTPUT_LIMIT = "SANDBOX_OUTPUT_LIMIT"
    TIMEOUT = "SANDBOX_TIMEOUT"
    EXECUTION = "SANDBOX_EXECUTION_ERROR"
    CLEANUP = "SANDBOX_CLEANUP_FAILED"


class SandboxError(RuntimeError):
    def __init__(self, code: SandboxErrorCode, *, execution_id: UUID | None = None):
        self.code = SandboxErrorCode(code)
        self.execution_id = execution_id
        super().__init__(self.code.value)


@dataclass(frozen=True)
class SandboxLimits:
    """Provisional bounded Host defaults, not team-approved experiment values."""
    cpus: float = 1.0
    memory_bytes: int = 512 * 1024 * 1024
    pids: int = 128
    timeout_seconds: float = 60.0
    tmpfs_bytes: int = 64 * 1024 * 1024
    max_stdout_bytes: int = 1024 * 1024
    max_stderr_bytes: int = 1024 * 1024
    control_timeout_seconds: float = 10.0

    def __post_init__(self):
        for value, low, high in (
            (self.cpus, 0.1, 4.0), (self.timeout_seconds, 0.01, 600.0),
            (self.control_timeout_seconds, 0.1, 30.0),
        ):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise SandboxError(SandboxErrorCode.INVALID)
        for value, low, high in (
            (self.memory_bytes, 32 * 1024 * 1024, 4 * 1024 * 1024 * 1024),
            (self.pids, 16, 1024), (self.tmpfs_bytes, 1024 * 1024, 1024 * 1024 * 1024),
            (self.max_stdout_bytes, 1, 4 * 1024 * 1024),
            (self.max_stderr_bytes, 1, 4 * 1024 * 1024),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise SandboxError(SandboxErrorCode.INVALID)


@dataclass(frozen=True, kw_only=True)
class ExecutionProfile:
    """Injected by the Host, never accepted directly from Model/Tool arguments."""
    name: str
    tool_name: str
    argv: tuple[str, ...] = field(repr=False)
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    image_reference: str | None = None

    def __post_init__(self):
        if (
            not isinstance(self.name, str) or re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", self.name) is None
            or self.tool_name not in {"run_build", "run_unit_tests", "run_browser_tests", "run_security_scan"}
            or not isinstance(self.limits, SandboxLimits)
            or not isinstance(self.argv, tuple) or not 1 <= len(self.argv) <= 64
            or any(not isinstance(arg, str) or len(arg.encode("utf-8", errors="surrogatepass")) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in arg) for arg in self.argv)
            or not self.argv[0].startswith("/") or ".." in self.argv[0].split("/")
            or len(self.argv[0]) > 256
        ):
            raise SandboxError(SandboxErrorCode.INVALID)
        if self.image_reference is not None and (
            not isinstance(self.image_reference, str) or len(self.image_reference) > 512
            or re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[a-z0-9][a-z0-9._:/-]*@sha256:[0-9a-f]{64})", self.image_reference) is None
        ):
            raise SandboxError(SandboxErrorCode.INVALID)


@dataclass(frozen=True, kw_only=True)
class CLIResult:
    returncode: int
    stdout: bytes = field(repr=False)
    stderr: bytes = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class SandboxResult:
    execution_id: UUID
    run_id: UUID
    source_artifact_id: UUID
    profile_name: str
    tool_name: str
    execution_manifest: ExecutionManifest
    image_id: str
    container_id: str
    exit_code: int
    duration_ms: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)
    # Output files live only in container tmpfs and are not silently exported.
    # Product PASS/FAIL, report parsing and ToolEvidence are future Tool work.
