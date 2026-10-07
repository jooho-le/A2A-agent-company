"""Explicit DB-bound Workspace provisioning and revalidated directory handles.

Run creation owns the UUIDs. This registry never adopts a nonempty unmarked
directory, rewrites a DB root, or treats a model-provided Host path as a grant.
No filesystem changes occur during construction, record lookup, or binding.
"""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import threading
from uuid import UUID

try:
    import fcntl
except ImportError:  # pragma: no cover - current runtime is POSIX.
    fcntl = None

from orchestrator.domain.states import AgentRole
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.workspaces.filesystem import (
    BoundWorkspace, open_absolute_directory, open_regular_file, walk_directory,
)
from orchestrator.workspaces.policy import (
    LAYOUT_VERSION, OWNER_MARKER, WORKSPACE_LAYOUT,
    WorkspaceAccessError, WorkspaceErrorCode, workspace_uuid,
)


_MARKER_MAX_BYTES = 4096
_REGISTRY_LOCK = ".registry.lock"
# flock serializes already-open lock files across processes. This guard also
# serializes initial base/lock creation and separate Registry instances in the
# same process, before the file lock can exist.
_PROVISION_THREAD_LOCK = threading.RLock()


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate marker field")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("Nonfinite marker value")


class WorkspaceRegistry:
    """Trusted repository and configuration; public input contains only IDs."""

    def __init__(self, repository, base_root: Path | str) -> None:
        self._repository = repository
        try:
            if isinstance(base_root, str) and not base_root.strip():
                raise ValueError("Scoped base required")
            self._base_path = Path(base_root).resolve()
            if self._base_path in (
                Path(self._base_path.anchor), Path.home().resolve(), Path.cwd().resolve(),
            ):
                raise ValueError("Scoped base required")
        except Exception:
            raise WorkspaceAccessError(WorkspaceErrorCode.ROOT) from None

    @property
    def base_path(self) -> Path:
        return self._base_path

    def get_record(self, workspace_id: UUID | str, *, run_id: UUID | str) -> WorkspaceRecord:
        """Read existing immutable identity; never generate IDs or repair roots."""
        workspace_id = workspace_uuid(workspace_id)
        run_id = workspace_uuid(run_id)
        try:
            run = self._repository.get_run(run_id)
            record = self._repository.get_workspace(workspace_id)
        except Exception:
            raise WorkspaceAccessError(WorkspaceErrorCode.IO) from None
        if run is None or record is None:
            raise WorkspaceAccessError(WorkspaceErrorCode.NOT_FOUND)
        if (
            not isinstance(record, WorkspaceRecord)
            or run.run_id != run_id
            or run.workspace_id != workspace_id
            or record.workspace_id != workspace_id
            or record.run_id != run_id
        ):
            raise WorkspaceAccessError(WorkspaceErrorCode.IDENTITY)
        if Path(record.root_path) != self._base_path / str(workspace_id):
            raise WorkspaceAccessError(WorkspaceErrorCode.ROOT)
        return record

    def provision(self, workspace_id: UUID | str, *, run_id: UUID | str) -> WorkspaceRecord:
        """Create only a registered Workspace, under a process/thread lock.

        The DB identity already exists before provisioning. A valid private
        ownership marker permits retrying partial layout creation. Conflicting
        files, invalid markers and unrelated user directories are left intact.
        """
        record = self.get_record(workspace_id, run_id=run_id)
        with _PROVISION_THREAD_LOCK:
            return self._provision_record(record)

    def _provision_record(self, record: WorkspaceRecord) -> WorkspaceRecord:
        if fcntl is None:
            raise WorkspaceAccessError(WorkspaceErrorCode.PLATFORM)
        try:
            with open_absolute_directory(self._base_path, create=True) as base_fd:
                with open_regular_file(
                    base_fd, (_REGISTRY_LOCK,), os.O_RDWR | os.O_CREAT, create=True,
                ) as lock_fd:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                    try:
                        # Identity must still be current after waiting on a lock.
                        current = self.get_record(record.workspace_id, run_id=record.run_id)
                        if current != record:
                            raise WorkspaceAccessError(WorkspaceErrorCode.IDENTITY)
                        with walk_directory(base_fd, (str(record.workspace_id),), create=True) as root_fd:
                            marker = self._read_marker(root_fd)
                            if marker is None:
                                with os.scandir(root_fd) as entries:
                                    if next(entries, None) is not None:
                                        raise WorkspaceAccessError(WorkspaceErrorCode.CONFLICT)
                                self._write_marker(root_fd, record)
                            else:
                                self._validate_marker(marker, record)
                            for path in WORKSPACE_LAYOUT:
                                with walk_directory(root_fd, tuple(path.split("/")), create=True):
                                    pass
                            self._check_layout(root_fd)
                    finally:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except WorkspaceAccessError:
            raise
        except Exception:
            raise WorkspaceAccessError(WorkspaceErrorCode.IO) from None
        return record

    def bind(
        self, workspace_id: UUID | str, *, run_id: UUID | str, role: AgentRole,
    ) -> BoundWorkspace:
        """Select a trusted service role, validate now, then revalidate per use."""
        if not isinstance(role, AgentRole):
            raise WorkspaceAccessError(WorkspaceErrorCode.PERMISSION_DENIED)
        record = self.get_record(workspace_id, run_id=run_id)
        with self._open_workspace(record):
            pass
        return BoundWorkspace(
            role=role, record=record, opener=lambda: self._open_workspace(record),
        )

    @contextmanager
    def _open_workspace(self, record: WorkspaceRecord):
        current = self.get_record(record.workspace_id, run_id=record.run_id)
        if current != record:
            raise WorkspaceAccessError(WorkspaceErrorCode.IDENTITY)
        with ExitStack() as stack:
            try:
                base_fd = stack.enter_context(open_absolute_directory(self._base_path, create=False))
                root_fd = stack.enter_context(walk_directory(base_fd, (str(record.workspace_id),), create=False))
                marker = self._read_marker(root_fd)
                if marker is None:
                    raise WorkspaceAccessError(WorkspaceErrorCode.NOT_PROVISIONED)
                self._validate_marker(marker, record)
                self._check_layout(root_fd)
            except WorkspaceAccessError as error:
                if error.code is WorkspaceErrorCode.FILE_NOT_FOUND:
                    raise WorkspaceAccessError(WorkspaceErrorCode.NOT_PROVISIONED) from None
                raise
            except Exception:
                raise WorkspaceAccessError(WorkspaceErrorCode.IO) from None
            # Caller file access has its own errors. A missing product file
            # must not be relabeled as an unprovisioned Workspace by this CM.
            yield root_fd

    @staticmethod
    def _expected_marker(record: WorkspaceRecord) -> dict[str, object]:
        return {
            "workspaceId": str(record.workspace_id), "runId": str(record.run_id),
            "layoutVersion": LAYOUT_VERSION,
        }

    @staticmethod
    def _validate_marker(marker: object, record: WorkspaceRecord) -> None:
        if (
            not isinstance(marker, dict)
            or type(marker.get("layoutVersion")) is not int
            or marker != WorkspaceRegistry._expected_marker(record)
        ):
            raise WorkspaceAccessError(WorkspaceErrorCode.CONFLICT)

    @staticmethod
    def _read_marker(root_fd: int) -> object | None:
        try:
            with open_regular_file(root_fd, (OWNER_MARKER,), os.O_RDONLY) as marker_fd:
                stat = os.fstat(marker_fd)
                if stat.st_size <= 0 or stat.st_size > _MARKER_MAX_BYTES or stat.st_mode & 0o077:
                    raise WorkspaceAccessError(WorkspaceErrorCode.CONFLICT)
                chunks = []
                remaining = _MARKER_MAX_BYTES + 1
                while remaining:
                    chunk = os.read(marker_fd, remaining)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                raw = b"".join(chunks)
                after = os.fstat(marker_fd)
                if (
                    len(raw) != stat.st_size or len(raw) > _MARKER_MAX_BYTES
                    or stat.st_mtime_ns != after.st_mtime_ns
                    or stat.st_ctime_ns != after.st_ctime_ns
                ):
                    raise WorkspaceAccessError(WorkspaceErrorCode.CONFLICT)
                try:
                    marker = json.loads(
                        raw.decode("utf-8"), object_pairs_hook=_unique_keys,
                        parse_constant=_reject_constant,
                    )
                    if not isinstance(marker, dict):
                        raise ValueError("Marker object required")
                    return marker
                except (ValueError, TypeError, UnicodeError, RecursionError):
                    raise WorkspaceAccessError(WorkspaceErrorCode.CONFLICT) from None
        except WorkspaceAccessError as error:
            if error.code is WorkspaceErrorCode.FILE_NOT_FOUND:
                return None
            raise

    @staticmethod
    def _write_marker(root_fd: int, record: WorkspaceRecord) -> None:
        raw = json.dumps(
            WorkspaceRegistry._expected_marker(record), sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        with open_regular_file(
            root_fd, (OWNER_MARKER,), os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True,
        ) as marker_fd:
            pending = memoryview(raw)
            while pending:
                written = os.write(marker_fd, pending)
                if written <= 0:
                    raise WorkspaceAccessError(WorkspaceErrorCode.IO)
                pending = pending[written:]
            os.fsync(marker_fd)
        os.fsync(root_fd)

    @staticmethod
    def _check_layout(root_fd: int) -> None:
        try:
            for path in WORKSPACE_LAYOUT:
                with walk_directory(root_fd, tuple(path.split("/")), create=False):
                    pass
        except WorkspaceAccessError as error:
            if error.code is WorkspaceErrorCode.FILE_NOT_FOUND:
                raise WorkspaceAccessError(WorkspaceErrorCode.NOT_PROVISIONED) from None
            raise
