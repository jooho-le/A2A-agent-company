"""Bounded file I/O through pinned Workspace descriptors, never a Shell.

All cooperating file Tools lock the Workspace root inode. An individual file
replacement is atomic. Multi-file edits preflight every target, stage both new
bytes and recoverable originals, and roll back ordinary commit failures. They
are *not* a crash-atomic filesystem transaction or an OS-user sandbox. Snapshot
capture must not overlap a writer unless it participates in the same lock.
"""

from collections.abc import Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path
import re
import secrets
import stat

try:
    import fcntl
except ImportError:  # pragma: no cover - the Workspace runtime is POSIX.
    fcntl = None

from mcp_tools.core.catalog import MAX_FILE_BYTES
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.filesystem import BoundWorkspace, open_regular_file
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, WorkspaceErrorCode, authorize_path,
)


MAX_BATCH_FILES = 64
MAX_TOTAL_CHANGE_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_DEPTH = 64
MAX_CREATED_DIRECTORIES = 64
_CHUNK_BYTES = 64 * 1024
_TEMP_PREFIX = ".mcp-write-"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_CODES = frozenset({
    "FILE_NOT_FOUND", "PATH_DENIED", "FILE_TOO_LARGE", "WRITE_CONFLICT",
    "WRITE_FAILED", "PATCH_FAILED",
})
_DENIED_ERRNOS = frozenset({
    errno.ELOOP, errno.ENOTDIR, errno.EISDIR, errno.EACCES, errno.EPERM,
    errno.ENXIO, errno.ENAMETOOLONG,
})


class FileOperationError(RuntimeError):
    """Stable code only; no path, file content, Host root, or nested exception."""

    def __init__(self, code: str):
        self.code = code if isinstance(code, str) and code in _CODES else "WRITE_FAILED"
        super().__init__(self.code)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _signature(info):
    return (
        info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
        info.st_ctime_ns, info.st_mode, info.st_nlink,
    )


def _same_inode(left, right) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _io_code(error: OSError, fallback: str) -> str:
    if error.errno == errno.ENOENT:
        return "FILE_NOT_FOUND"
    if error.errno in _DENIED_ERRNOS:
        return "PATH_DENIED"
    if error.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
        return "WRITE_CONFLICT"
    return fallback


def _parts(workspace: BoundWorkspace, path: str, access: WorkspaceAccess):
    failure = None
    try:
        if not isinstance(workspace, BoundWorkspace):
            raise ValueError
        if access is WorkspaceAccess.WRITE and workspace.role not in (
            AgentRole.DEVELOPER, AgentRole.QA,
        ):
            raise ValueError
        parts = authorize_path(workspace.role, path, access)
        path.encode("utf-8", errors="strict")
        if len(parts) > MAX_DIRECTORY_DEPTH + 1 or any(
            part.casefold().startswith(_TEMP_PREFIX) for part in parts
        ):
            raise ValueError
        return parts
    except (WorkspaceAccessError, ValueError, UnicodeError):
        failure = "PATH_DENIED"
    raise FileOperationError(failure)


@contextmanager
def _locked_root(workspace: BoundWorkspace, *, write: bool):
    if fcntl is None:
        raise FileOperationError("PATH_DENIED")
    # Keep identity/provision errors typed. The Agent adapter can distinguish
    # an unavailable Workspace from a missing product file.
    with workspace.opener() as descriptor:
        acquired = False
        failure = None
        try:
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise FileOperationError("PATH_DENIED")
            try:
                operation = fcntl.LOCK_EX if write else fcntl.LOCK_SH
                fcntl.flock(descriptor, operation | fcntl.LOCK_NB)
                acquired = True
            except OSError as error:
                failure = _io_code(error, "WRITE_FAILED")
            if failure is not None:
                raise FileOperationError(failure)
            yield descriptor
        finally:
            if acquired:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    # Closing the descriptor at the surrounding context exit
                    # releases the lock even if explicit unlock fails.
                    pass


def _regular_info(descriptor):
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FileOperationError("PATH_DENIED")
    if info.st_size > MAX_FILE_BYTES:
        raise FileOperationError("FILE_TOO_LARGE")
    return info


def _read_descriptor(descriptor):
    before = _regular_info(descriptor)
    chunks = []
    size = 0
    while True:
        chunk = os.read(descriptor, min(_CHUNK_BYTES, MAX_FILE_BYTES + 1 - size))
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise FileOperationError("FILE_TOO_LARGE")
    after = _regular_info(descriptor)
    if _signature(before) != _signature(after) or size != before.st_size:
        raise FileOperationError("WRITE_CONFLICT")
    return b"".join(chunks), before


def _read_parts(workspace, parts, *, allow_symlinks):
    if not allow_symlinks:
        return parts
    failure = None
    try:
        root = Path(workspace.record.root_path)
        target = root.joinpath(*parts).resolve(strict=True)
        relative = target.relative_to(root).as_posix()
        # Reauthorization also denies aliases pointing at reserved staging
        # files. A second open_read/resolve would create another target race.
        return _parts(workspace, relative, WorkspaceAccess.READ)
    except FileNotFoundError:
        failure = "FILE_NOT_FOUND"
    except (ValueError, RuntimeError, OSError, UnicodeError):
        failure = "PATH_DENIED"
    raise FileOperationError(failure)


def read_working(
    workspace: BoundWorkspace, path: str, *, allow_symlinks: bool = True,
) -> bytes:
    """Read exact bytes; authorized internal read symlinks retain policy behavior.

    Like BoundWorkspace.open_read, resolve and reauthorize a symlink target,
    then open every canonical component with O_NOFOLLOW. Additional reserved
    staging names cannot be reached through aliases. Setting allow_symlinks
    False rejects every link, including internal links; this prevents a QA/
    Security scratch path from aliasing the unfrozen working Source. A shared
    root lock coordinates with these Tools' writers. Binary-to-text policy is
    the higher-level Tool's job.
    """
    if type(allow_symlinks) is not bool:
        raise FileOperationError("PATH_DENIED")
    parts = _parts(workspace, path, WorkspaceAccess.READ)
    failure = None
    try:
        with _locked_root(workspace, write=False) as root:
            actual_parts = _read_parts(workspace, parts, allow_symlinks=allow_symlinks)
            with open_regular_file(root, actual_parts, os.O_RDONLY) as descriptor:
                content, _ = _read_descriptor(descriptor)
                return content
    except WorkspaceAccessError as error:
        if error.code is WorkspaceErrorCode.FILE_NOT_FOUND:
            failure = "FILE_NOT_FOUND"
        elif error.code in (WorkspaceErrorCode.PATH_DENIED, WorkspaceErrorCode.PERMISSION_DENIED):
            failure = "PATH_DENIED"
        else:
            raise
    except OSError as error:
        failure = _io_code(error, "WRITE_FAILED")
    raise FileOperationError(failure)


@dataclass
class _CreatedDirectory:
    parent: int
    name: str
    info: object


@dataclass
class _Stage:
    parent: int
    name: str
    info: object
    backup: bool = False
    preserve: bool = False


@dataclass
class _Change:
    path: str
    parts: tuple[str, ...]
    content: bytes | None = field(repr=False)
    parent: int | None = None
    previous: bytes | None = field(default=None, repr=False)
    previous_info: object | None = None
    changed: bool = False
    replacement: _Stage | None = None
    original: _Stage | None = None
    committed: bool = False


def _open_parent(root, parts, *, create, stack, created):
    current = os.dup(root)
    try:
        for name in parts:
            made = False
            creation_parent = None
            if create:
                # Pin the recovery handle *before* the directory mutation.
                creation_parent = os.dup(current)
                try:
                    if len(created) >= MAX_CREATED_DIRECTORIES:
                        # Existing directories are still allowed at the cap.
                        try:
                            existing = os.stat(name, dir_fd=current, follow_symlinks=False)
                        except FileNotFoundError:
                            raise FileOperationError("PATH_DENIED") from None
                        if not stat.S_ISDIR(existing.st_mode):
                            raise FileOperationError("PATH_DENIED")
                    else:
                        try:
                            os.mkdir(name, mode=0o700, dir_fd=current)
                            made = True
                        except FileExistsError:
                            pass
                except BaseException:
                    os.close(creation_parent)
                    raise
            try:
                child = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                    dir_fd=current,
                )
            except BaseException:
                if creation_parent is not None:
                    if made:
                        try:
                            os.rmdir(name, dir_fd=creation_parent)
                        except OSError:
                            pass
                    os.close(creation_parent)
                raise
            if creation_parent is not None:
                if made:
                    created.append(_CreatedDirectory(creation_parent, name, os.fstat(child)))
                else:
                    os.close(creation_parent)
            os.close(current)
            current = child
        stack.callback(os.close, current)
        result = current
        current = None
        return result
    finally:
        if current is not None:
            os.close(current)


def _read_target(parent, name):
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=parent,
        )
    except FileNotFoundError:
        return None, None
    try:
        return _read_descriptor(descriptor)
    finally:
        os.close(descriptor)


def _stat_target(parent, name):
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise FileOperationError("PATH_DENIED")
    return info


def _verify_target(change):
    current = _stat_target(change.parent, change.parts[-1])
    if change.previous_info is None:
        if current is not None:
            raise FileOperationError("WRITE_CONFLICT")
    elif current is None or _signature(current) != _signature(change.previous_info):
        raise FileOperationError("WRITE_CONFLICT")


def _stage(parent, content, mode, stages, *, backup=False):
    name = _TEMP_PREFIX + secrets.token_hex(16) + ".tmp"
    descriptor = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode=0o600, dir_fd=parent,
    )
    staged = None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise FileOperationError("PATH_DENIED")
        staged = _Stage(parent, name, info, backup=backup)
        stages.append(staged)
        pending = memoryview(content)
        while pending:
            count = os.write(descriptor, pending)
            if count <= 0:
                raise OSError(errno.EIO, "File write failed")
            pending = pending[count:]
        os.fchmod(descriptor, mode & 0o777)
        os.fsync(descriptor)
        staged.info = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent)
    return staged


def _verify_stage(stage):
    info = _stat_target(stage.parent, stage.name)
    if info is None or not _same_inode(info, stage.info):
        raise FileOperationError("WRITE_CONFLICT")


def _cleanup_stages(stages):
    for staged in reversed(stages):
        if staged.preserve:
            continue
        try:
            current = _stat_target(staged.parent, staged.name)
            if current is not None and _same_inode(current, staged.info):
                os.unlink(staged.name, dir_fd=staged.parent)
        except (OSError, FileOperationError):
            # Never remove a replacement belonging to an uncooperative actor.
            # A crash/unrecoverable storage failure may need operator recovery.
            pass


def _cleanup_directories(created, *, remove):
    for directory in reversed(created):
        try:
            if remove:
                current = os.stat(directory.name, dir_fd=directory.parent, follow_symlinks=False)
                if stat.S_ISDIR(current.st_mode) and _same_inode(current, directory.info):
                    os.rmdir(directory.name, dir_fd=directory.parent)
        except OSError:
            # Leave nonempty or externally replaced directories untouched.
            pass
        finally:
            os.close(directory.parent)


def _rollback(changes, stages):
    complete = True
    for change in reversed(changes):
        if not change.committed:
            continue
        try:
            current = _stat_target(change.parent, change.parts[-1])
            if change.content is None:
                if current is not None:
                    raise FileOperationError("WRITE_CONFLICT")
            elif (
                current is None or change.replacement is None
                or not _same_inode(current, change.replacement.info)
            ):
                raise FileOperationError("WRITE_CONFLICT")
            if change.previous is None:
                os.unlink(change.parts[-1], dir_fd=change.parent)
            else:
                _verify_stage(change.original)
                os.replace(
                    change.original.name, change.parts[-1],
                    src_dir_fd=change.parent, dst_dir_fd=change.parent,
                )
            os.fsync(change.parent)
        except (OSError, FileOperationError):
            complete = False
    if not complete:
        for staged in stages:
            if staged.backup:
                staged.preserve = True
    return complete


def _validated_changes(workspace, changes, expected_hashes):
    failure = None
    try:
        if not isinstance(changes, Mapping) or not isinstance(expected_hashes, Mapping):
            raise ValueError
        changes = dict(changes)
        expected_hashes = dict(expected_hashes)
        if not 1 <= len(changes) <= MAX_BATCH_FILES or set(expected_hashes) - set(changes):
            raise ValueError
        for digest in expected_hashes.values():
            if digest is not None and (not isinstance(digest, str) or _HASH.fullmatch(digest) is None):
                raise ValueError
        result = []
        size = 0
        for path, content in changes.items():
            parts = _parts(workspace, path, WorkspaceAccess.WRITE)
            if content is not None:
                if type(content) is not bytes:
                    raise ValueError
                if len(content) > MAX_FILE_BYTES:
                    raise FileOperationError("FILE_TOO_LARGE")
                size += len(content)
                if size > MAX_TOTAL_CHANGE_BYTES:
                    raise FileOperationError("FILE_TOO_LARGE")
            result.append(_Change(path=path, parts=parts, content=content))
        all_parts = {change.parts for change in result}
        if any(parts[:length] in all_parts for parts in all_parts for length in range(1, len(parts))):
            raise ValueError
        return result, expected_hashes
    except (ValueError, TypeError, UnicodeError):
        failure = "PATH_DENIED"
    raise FileOperationError(failure)


def _apply(workspace, changes, expected_hashes, *, failure_code):
    entries, expected_hashes = _validated_changes(workspace, changes, expected_hashes)
    failure = None
    with _locked_root(workspace, write=True) as root:
        with ExitStack() as stack:
            stages = []
            created = []
            success = False
            try:
                previous_size = 0
                # Phase 1: inspect/CAS every target, without creating any dirs.
                for change in entries:
                    try:
                        change.parent = _open_parent(
                            root, change.parts[:-1], create=False, stack=stack, created=created,
                        )
                    except FileNotFoundError:
                        change.parent = None
                    if change.parent is not None:
                        change.previous, change.previous_info = _read_target(change.parent, change.parts[-1])
                    if change.previous is not None:
                        previous_size += len(change.previous)
                        if previous_size > MAX_TOTAL_CHANGE_BYTES:
                            raise FileOperationError("FILE_TOO_LARGE")
                    if change.path in expected_hashes:
                        expected = expected_hashes[change.path]
                        actual = None if change.previous is None else _digest(change.previous)
                        if actual != expected:
                            raise FileOperationError("WRITE_CONFLICT")
                    if change.content is None and change.previous is None:
                        raise FileOperationError("FILE_NOT_FOUND")
                    change.changed = change.content != change.previous
                # Phase 2: prepare parents/new bytes/original backups only after
                # all paths, all contents and all expected hashes were checked.
                for change in entries:
                    if not change.changed:
                        continue
                    if change.parent is None:
                        change.parent = _open_parent(
                            root, change.parts[:-1], create=True, stack=stack, created=created,
                        )
                    mode = 0o600 if change.previous_info is None else stat.S_IMODE(change.previous_info.st_mode)
                    if change.content is not None:
                        change.replacement = _stage(change.parent, change.content, mode, stages)
                    if change.previous is not None:
                        change.original = _stage(change.parent, change.previous, mode, stages, backup=True)
                for change in entries:
                    if change.changed:
                        _verify_target(change)
                # Phase 3: per-file atomic commit; every held descriptor stays
                # live until commit/rollback/temporary-file cleanup completes.
                for change in entries:
                    if not change.changed:
                        continue
                    _verify_target(change)
                    if change.content is None:
                        os.unlink(change.parts[-1], dir_fd=change.parent)
                    else:
                        _verify_stage(change.replacement)
                        os.replace(
                            change.replacement.name, change.parts[-1],
                            src_dir_fd=change.parent, dst_dir_fd=change.parent,
                        )
                    change.committed = True
                    os.fsync(change.parent)
                success = True
                return {
                    "changedFiles": [change.path for change in entries if change.changed],
                    "newHashes": {
                        change.path: _digest(change.content)
                        for change in entries if change.content is not None
                    },
                }
            except FileOperationError as error:
                failure = error.code
            except OSError as error:
                failure = _io_code(error, failure_code)
            finally:
                if not success:
                    if not _rollback(entries, stages):
                        failure = "PATCH_FAILED"
                _cleanup_stages(stages)
                _cleanup_directories(created, remove=not success)
    raise FileOperationError(failure or failure_code)


def write_working(
    workspace: BoundWorkspace, path: str, content: bytes, expected_sha256: str | None = None,
) -> dict:
    """Atomically replace/create a product file, optionally comparing its hash.

    expected_sha256=None means the optional CAS was omitted. To explicitly
    require a missing file, use apply_changes with expected_hashes[path]=None.
    """
    if type(content) is not bytes:
        raise FileOperationError("PATH_DENIED")
    expected = {} if expected_sha256 is None else {path: expected_sha256}
    result = _apply(workspace, {path: content}, expected, failure_code="WRITE_FAILED")
    return {
        "path": path, "sha256": result["newHashes"][path],
        "sizeBytes": len(content), "changed": bool(result["changedFiles"]),
    }


def apply_changes(
    workspace: BoundWorkspace, changes: Mapping[str, bytes | None],
    expected_hashes: Mapping[str, str | None],
) -> dict:
    """Apply a bounded already-parsed patch; None deletes an existing file.

    A supplied None expected hash requires absence; omitted keys disable CAS
    for those paths. Callers applying a patch should supply every target hash.
    No patch parsing, Snapshot validation, generated-code execution or implicit
    retry occurs here. A failure code never promises crash-atomic recovery.
    """
    return _apply(workspace, changes, expected_hashes, failure_code="PATCH_FAILED")
