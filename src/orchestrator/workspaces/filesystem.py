"""POSIX descriptor access; paths alone are not authorization capabilities.

    No Shell, content Tool, Snapshot freeze, or OS-user sandbox is implemented
    here. All writers must use the scoped handle and not reopen a checked Path.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
import errno
import os
from pathlib import Path
import stat

from orchestrator.domain.states import AgentRole
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, WorkspaceErrorCode, authorize_path,
)


def require_supported_platform() -> None:
    if (
        os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY")
        or not hasattr(os, "O_NONBLOCK") or os.open not in os.supports_dir_fd
        or os.mkdir not in os.supports_dir_fd
    ):
        raise WorkspaceAccessError(WorkspaceErrorCode.PLATFORM)


def _error(error: OSError) -> WorkspaceAccessError:
    if error.errno == errno.ENOENT:
        return WorkspaceAccessError(WorkspaceErrorCode.FILE_NOT_FOUND)
    if error.errno in (errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.EACCES, errno.EPERM, errno.ENXIO):
        return WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    return WorkspaceAccessError(WorkspaceErrorCode.IO)


def _components(parts: tuple[str, ...]) -> None:
    if not isinstance(parts, tuple) or any(
        not isinstance(part, str) or part in ("", ".", "..") or "/" in part or "\\" in part or "\x00" in part
        for part in parts
    ):
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)


@contextmanager
def walk_directory(root_fd: int, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
    """Open every component relative to a pinned parent, never through links."""
    require_supported_platform()
    _components(parts)
    current = None
    try:
        current = os.dup(root_fd)
        if not stat.S_ISDIR(os.fstat(current).st_mode):
            raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
        for name in parts:
            if create:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=current)
            os.close(current)
            current = child
        yield current
    except OSError as error:
        raise _error(error) from None
    except UnicodeError:
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED) from None
    finally:
        if current is not None:
            os.close(current)


@contextmanager
def open_absolute_directory(path: Path, *, create: bool = False) -> Iterator[int]:
    """Only for a trusted, server-selected canonical base; no recursive chmod."""
    require_supported_platform()
    if not isinstance(path, Path) or not path.is_absolute():
        raise WorkspaceAccessError(WorkspaceErrorCode.ROOT)
    root_fd = None
    try:
        root_fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with walk_directory(root_fd, tuple(path.parts[1:]), create=create) as directory:
            yield directory
    except OSError as error:
        raise _error(error) from None
    finally:
        if root_fd is not None:
            os.close(root_fd)


@contextmanager
def open_regular_file(
    root_fd: int, parts: tuple[str, ...], flags: int, *, create: bool = False,
) -> Iterator[int]:
    """Never truncate before inode validation; disallow links and special nodes."""
    require_supported_platform()
    _components(parts)
    if not parts or flags & os.O_TRUNC or flags & os.O_CREAT and not create:
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    file_fd = None
    try:
        with walk_directory(root_fd, parts[:-1]) as parent:
            effective = flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            if create:
                effective |= os.O_CREAT
            file_fd = os.open(parts[-1], effective, mode=0o600, dir_fd=parent)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
            yield file_fd
    except OSError as error:
        raise _error(error) from None
    except UnicodeError:
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED) from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


@dataclass(frozen=True, kw_only=True)
class BoundWorkspace:
    """A trusted role/Run binding, not a grant for the same OS user's Shell.

    Descriptor contexts remain owned here: callers must not close/reopen the fd.
    Writes do not truncate, apply a patch, check expectedSha256, or create parent
    dirs implicitly. The future file Tools implement those operations separately.
    """
    role: AgentRole
    record: WorkspaceRecord = field(repr=False)
    opener: Callable[[], AbstractContextManager[int]] = field(repr=False)

    @property
    def workspace_id(self):
        return self.record.workspace_id

    @property
    def run_id(self):
        return self.record.run_id

    @contextmanager
    def open_read(self, path: str) -> Iterator[int]:
        parts = authorize_path(self.role, path, WorkspaceAccess.READ)
        with self.opener() as root_fd:
            try:
                root = Path(self.record.root_path)
                # Resolve links only for reading, then reopen the canonical
                # relative target with O_NOFOLLOW at EVERY component.
                target = root.joinpath(*parts).resolve(strict=True)
                relative = target.relative_to(root).as_posix()
            except FileNotFoundError:
                raise WorkspaceAccessError(WorkspaceErrorCode.FILE_NOT_FOUND) from None
            except (OSError, ValueError, RuntimeError, UnicodeError):
                raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED) from None
            target_parts = authorize_path(self.role, relative, WorkspaceAccess.READ)
            with open_regular_file(root_fd, target_parts, os.O_RDONLY) as file_fd:
                yield file_fd

    @contextmanager
    def open_write(self, path: str, *, create: bool = False) -> Iterator[int]:
        parts = authorize_path(self.role, path, WorkspaceAccess.WRITE)
        with self.opener() as root_fd:
            with open_regular_file(root_fd, parts, os.O_WRONLY, create=create) as file_fd:
                yield file_fd

    def ensure_parent(self, path: str) -> None:
        """Only writable path parents; no arbitrary directory creation grant."""
        parts = authorize_path(self.role, path, WorkspaceAccess.WRITE)
        with self.opener() as root_fd:
            with walk_directory(root_fd, parts[:-1], create=True):
                pass
