"""Host-selected immutable Source reads, with no Working Copy fallback.

The selection is a trusted launcher capability, not a Tool argument. The
Artifact Store rechecks Run/Workspace ownership and read grants on every call.
The existing pure canonical tar verifier checks the entire archive before any
file bytes are returned; this module never extracts or executes Source.
"""

from dataclasses import dataclass, field
import hashlib
import io
import re
import tarfile
from uuid import UUID

from mcp_tools.runtime import MCPBinding, MCPConfigurationError
from orchestrator.artifacts.contracts import (
    MAX_CONTENT_BYTES, ArtifactAccessError, ArtifactErrorCode, StoredContent,
)
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.materialization import _archive_entries, _path
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, authorize_path, workspace_uuid,
)


_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_FILES = 1000
_ERROR_CODES = frozenset({
    "SNAPSHOT_REQUIRED", "BASE_MISMATCH", "SNAPSHOT_INTEGRITY_ERROR",
    "FILE_NOT_FOUND", "FILE_TOO_LARGE", "PATH_DENIED",
})


class SnapshotReadError(RuntimeError):
    """Only a stable code, without submitted paths, Source, or Host failures."""

    def __init__(self, code: str):
        if code not in _ERROR_CODES:
            raise MCPConfigurationError()
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, kw_only=True)
class FrozenSourceSelection:
    """Exact Source identity supplied by the Host for one Agent invocation."""

    project_artifact_id: UUID = field(repr=False)
    snapshot_sha256: str = field(repr=False)

    def __post_init__(self):
        try:
            artifact_id = workspace_uuid(self.project_artifact_id)
        except WorkspaceAccessError:
            raise MCPConfigurationError() from None
        if (
            not isinstance(self.snapshot_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256) is None
        ):
            raise MCPConfigurationError()
        object.__setattr__(self, "project_artifact_id", artifact_id)


def _source_path(binding, path):
    try:
        if not isinstance(binding, MCPBinding):
            raise ValueError
        parts = authorize_path(binding.role, path, WorkspaceAccess.READ)
        if len(parts) < 2 or parts[0] != "source":
            raise ValueError
        relative = "/".join(parts[1:])
        # The canonical archive verifier rejects compatibility-form Secret
        # names and ambiguous paths; apply the identical rule to queries.
        _path(relative)
        return relative
    except (WorkspaceAccessError, SandboxError, ValueError, TypeError):
        raise SnapshotReadError("PATH_DENIED") from None


def _bounded_archive(content):
    """Detect resource excess without reading entries before full validation."""
    if len(content) > MAX_CONTENT_BYTES:
        raise SnapshotReadError("FILE_TOO_LARGE")
    total = 0
    count = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                count += 1
                total += max(member.size, 0)
                if (
                    count > _MAX_FILES or member.size > _MAX_FILE_BYTES
                    or total > _MAX_TOTAL_BYTES
                ):
                    raise SnapshotReadError("FILE_TOO_LARGE")
    except SnapshotReadError:
        raise
    except (tarfile.TarError, OSError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise SnapshotReadError("SNAPSHOT_INTEGRITY_ERROR") from None


class SnapshotReader:
    """Read-only, inert capability; no automatic latest-snapshot selection."""

    def __init__(self, artifact_store: ArtifactStore):
        if not isinstance(artifact_store, ArtifactStore):
            raise MCPConfigurationError()
        self._store = artifact_store

    def __repr__(self):
        return "SnapshotReader()"

    def _load(self, binding, selection):
        if not isinstance(selection, FrozenSourceSelection):
            raise SnapshotReadError("SNAPSHOT_REQUIRED")
        if not isinstance(binding, MCPBinding) or binding.role is AgentRole.PLANNER:
            raise SnapshotReadError("PATH_DENIED")
        try:
            # Source metadata has no workspaceId; explicitly compare the
            # Store's authoritative Run/Registry binding as well as the public
            # role-bound read. Do not rely solely on caller-supplied metadata.
            run, workspace = self._store._binding(binding.run_id, binding.role)
            if (
                run.run_id != binding.run_id or run.workspace_id != binding.workspace_id
                or workspace.run_id != binding.run_id
                or workspace.workspace_id != binding.workspace_id
                or workspace.role is not binding.role
            ):
                raise SnapshotReadError("PATH_DENIED")
            record = self._store.bind(binding.run_id, role=binding.role).read(
                selection.project_artifact_id,
            )
        except ArtifactAccessError as error:
            code = "PATH_DENIED" if error.code in {
                ArtifactErrorCode.DENIED, ArtifactErrorCode.PATH_DENIED,
            } else "SNAPSHOT_INTEGRITY_ERROR"
            raise SnapshotReadError(code) from None
        except SnapshotReadError:
            raise
        except Exception:
            raise SnapshotReadError("SNAPSHOT_INTEGRITY_ERROR") from None

        if not isinstance(record, StoredContent) or not isinstance(record.metadata, CodeSnapshotArtifact):
            raise SnapshotReadError("SNAPSHOT_INTEGRITY_ERROR")
        source = record.metadata
        if (
            source.artifact_type != "SOURCE" or source.created_by is not AgentRole.DEVELOPER
            or source.run_id != binding.run_id
            or source.artifact_id != selection.project_artifact_id
            or source.snapshot_sha256 != selection.snapshot_sha256
            or source.artifact_uri != f"artifact://{selection.project_artifact_id}/source.tar"
            or record.media_type != "application/x-tar"
            or not isinstance(record.content, bytes)
            or type(record.size_bytes) is not int or record.size_bytes != len(record.content)
            or record.content_sha256 != selection.snapshot_sha256
            or hashlib.sha256(record.content).hexdigest() != selection.snapshot_sha256
        ):
            raise SnapshotReadError("SNAPSHOT_INTEGRITY_ERROR")
        _bounded_archive(record.content)
        try:
            # Shared step-22 pure parser verifies normalized headers, allowed
            # regular-file types, bounds, collisions, sort order and exact tar
            # reserialization, including hidden trailing data. No extraction.
            entries = _archive_entries(record.content)
        except SandboxError as error:
            code = "PATH_DENIED" if error.code is SandboxErrorCode.PATH else "SNAPSHOT_INTEGRITY_ERROR"
            raise SnapshotReadError(code) from None
        except Exception:
            raise SnapshotReadError("SNAPSHOT_INTEGRITY_ERROR") from None
        return source, {name: content for name, content, _mode in entries}

    def read(self, binding: MCPBinding, selection: FrozenSourceSelection | None, path: str) -> bytes:
        relative = _source_path(binding, path)
        _source, files = self._load(binding, selection)
        if relative not in files:
            raise SnapshotReadError("FILE_NOT_FOUND")
        # Encoding belongs to the text File Tool, not archive integrity.
        # Binary assets are valid immutable Source and must not be rewritten.
        return files[relative]

    def verify_base(
        self, binding: MCPBinding, selection: FrozenSourceSelection | None, base_sha256: str,
    ) -> CodeSnapshotArtifact:
        source, _files = self._load(binding, selection)
        if not isinstance(base_sha256, str) or base_sha256 != source.snapshot_sha256:
            raise SnapshotReadError("BASE_MISMATCH")
        return source

    def read_base(
        self, binding: MCPBinding, selection: FrozenSourceSelection | None,
        base_sha256: str, paths: tuple[str, ...],
    ) -> dict[str, bytes | None]:
        """Read all affected base files in one verification; None means absent."""
        if (
            not isinstance(paths, tuple) or not 1 <= len(paths) <= _MAX_FILES
            or any(not isinstance(path, str) for path in paths)
            or len(set(paths)) != len(paths)
        ):
            raise SnapshotReadError("PATH_DENIED")
        relative = tuple(_source_path(binding, path) for path in paths)
        source, files = self._load(binding, selection)
        if not isinstance(base_sha256, str) or base_sha256 != source.snapshot_sha256:
            raise SnapshotReadError("BASE_MISMATCH")
        return {
            path: files[name] if name in files else None
            for path, name in zip(paths, relative)
        }
