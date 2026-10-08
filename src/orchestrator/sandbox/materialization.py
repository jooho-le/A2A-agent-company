"""Private Snapshot materialization, never execution or a model-selected path.

The Artifact Store/Host grants access before calling this library. Descriptor
walks protect filesystem operations; they are not isolation from a malicious
process already running as the Host's operating-system user.
"""

from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import threading
import unicodedata
from uuid import UUID

from orchestrator.artifacts.contracts import StoredContent
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.workspaces.filesystem import open_regular_file, walk_directory
from orchestrator.workspaces.policy import WorkspaceAccessError, relative_parts, workspace_uuid


_BASE = ".sandbox"
_MARKER = ".execution.json"
_MAX_ARCHIVE_BYTES = 20 * 1024 * 1024
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_FILES = 1000
_MAX_DIRECTORIES = 4096
_LOCK = threading.RLock()


@dataclass(frozen=True, kw_only=True)
class _FileIdentity:
    path: str
    size_bytes: int
    sha256: str
    mode: int


@dataclass(frozen=True, kw_only=True)
class MaterializedExecution:
    """Host capability; do not expose these paths as Tool or Trace fields."""

    execution_id: UUID
    run_id: UUID
    workspace_id: UUID
    source_artifact_id: UUID
    snapshot_sha256: str
    source_root: Path = field(repr=False)
    inputs_root: Path | None = field(repr=False)
    execution_root: Path = field(repr=False)
    workspace_record: WorkspaceRecord = field(repr=False)
    source_files: tuple[_FileIdentity, ...] = field(repr=False)
    input_files: tuple[_FileIdentity, ...] = field(repr=False)


def _path(path):
    try:
        path.encode("utf-8", errors="strict")
        parts = relative_parts(path)
        # Recheck normalized text as well: compatibility forms must not hide
        # traversal, a separator, a Secret name, or an invalid platform path.
        normalized = unicodedata.normalize("NFKC", path)
        normalized_parts = relative_parts(normalized)
        if len(parts) != len(normalized_parts) or len(parts) > 128:
            raise ValueError("Ambiguous path")
        return parts, tuple(part.casefold() for part in normalized_parts)
    except (WorkspaceAccessError, AttributeError, UnicodeError, ValueError, TypeError):
        raise SandboxError(SandboxErrorCode.PATH) from None


def _check_names(names):
    files = set()
    directories = set()
    spellings = {}
    for name in names:
        parts, key = _path(name)
        if key in files or key in directories:
            raise SandboxError(SandboxErrorCode.PATH)
        for length in range(1, len(key) + 1):
            prefix = key[:length]
            spelling = parts[:length]
            if prefix in spellings and spellings[prefix] != spelling:
                raise SandboxError(SandboxErrorCode.PATH)
            spellings[prefix] = spelling
            if length < len(key):
                if prefix in files:
                    raise SandboxError(SandboxErrorCode.PATH)
                directories.add(prefix)
                if len(directories) > _MAX_DIRECTORIES:
                    raise SandboxError(SandboxErrorCode.PATH)
        files.add(key)


def _canonical_archive(entries):
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name, content, mode in entries:
            member = tarfile.TarInfo(name)
            member.size = len(content)
            member.mode = mode
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(content))
    return target.getvalue()


def _archive_entries(content):
    entries = []
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:") as archive:
            for member in archive:
                if (
                    member.type != tarfile.REGTYPE or member.sparse is not None
                    or member.uid != 0 or member.gid != 0 or member.mtime != 0
                    or member.uname or member.gname or member.linkname
                    or member.mode not in (0o644, 0o755)
                    or set(member.pax_headers) - {"path"}
                    or member.size < 0 or member.size > _MAX_FILE_BYTES
                    or len(entries) >= _MAX_FILES
                ):
                    raise SandboxError(SandboxErrorCode.INTEGRITY)
                _path(member.name)
                stream = archive.extractfile(member)
                if stream is None:
                    raise SandboxError(SandboxErrorCode.INTEGRITY)
                value = stream.read(_MAX_FILE_BYTES + 1)
                if len(value) != member.size:
                    raise SandboxError(SandboxErrorCode.INTEGRITY)
                total += len(value)
                if total > _MAX_TOTAL_BYTES:
                    raise SandboxError(SandboxErrorCode.INTEGRITY)
                entries.append((member.name, value, member.mode))
    except (tarfile.TarError, OSError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise SandboxError(SandboxErrorCode.INTEGRITY) from None
    if not entries:
        raise SandboxError(SandboxErrorCode.INTEGRITY)
    _check_names(name for name, _, _ in entries)
    if (
        entries != sorted(entries, key=lambda item: item[0].encode("utf-8"))
        or _canonical_archive(entries) != content
    ):
        # Only the exact normalized archive generated by Snapshot step 21 is
        # accepted; unexpected PAX/header fields and hidden trailing archives
        # cannot become a second interpretation of the verified bytes.
        raise SandboxError(SandboxErrorCode.INTEGRITY)
    return tuple(entries)


def _inputs_entries(inputs):
    if inputs is None:
        return ()
    if not isinstance(inputs, Mapping) or len(inputs) > _MAX_FILES:
        raise SandboxError(SandboxErrorCode.INVALID)
    entries = []
    total = 0
    for name, value in inputs.items():
        _path(name)
        if not isinstance(value, bytes) or len(value) > _MAX_FILE_BYTES:
            raise SandboxError(SandboxErrorCode.INVALID)
        total += len(value)
        if total > _MAX_TOTAL_BYTES:
            raise SandboxError(SandboxErrorCode.INVALID)
        entries.append((name, value, 0o644))
    _check_names(name for name, _, _ in entries)
    return tuple(sorted(entries, key=lambda item: item[0].encode("utf-8")))


def _identities(entries):
    return tuple(_FileIdentity(path=name, size_bytes=len(value), sha256=hashlib.sha256(value).hexdigest(),
                               mode=0o555 if mode == 0o755 else 0o444)
                 for name, value, mode in entries)


def _input_digest(files):
    encoded = json.dumps(
        [{"path": item.path, "size": item.size_bytes, "sha256": item.sha256, "mode": item.mode}
         for item in files], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _marker(execution):
    return json.dumps({
        "layoutVersion": 1, "runId": str(execution.run_id),
        "workspaceId": str(execution.workspace_id), "executionId": str(execution.execution_id),
        "sourceArtifactId": str(execution.source_artifact_id), "snapshotSha256": execution.snapshot_sha256,
        "inputsSha256": _input_digest(execution.input_files),
    }, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write(fd, value):
    pending = memoryview(value)
    while pending:
        amount = os.write(fd, pending)
        if amount <= 0:
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        pending = pending[amount:]


def _create_tree(execution_fd, name, entries):
    os.mkdir(name, mode=0o755, dir_fd=execution_fd)
    with walk_directory(execution_fd, (name,)) as tree_fd:
        os.fchmod(tree_fd, 0o755)
        for path, value, mode in entries:
            parts, _ = _path(path)
            with walk_directory(tree_fd, parts[:-1], create=True) as parent_fd:
                # The private enclosing execution directory is not mounted;
                # Source directories must be traversable by the Container UID.
                os.fchmod(parent_fd, 0o755)
            # Every intermediate directory also needs normalized read modes.
            for length in range(1, len(parts)):
                with walk_directory(tree_fd, parts[:length]) as directory_fd:
                    os.fchmod(directory_fd, 0o755)
            with open_regular_file(tree_fd, parts, os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True) as file_fd:
                _write(file_fd, value)
                os.fchmod(file_fd, 0o555 if mode == 0o755 else 0o444)
                os.fsync(file_fd)
        os.fsync(tree_fd)


def _tree_identities(root_fd, prefix=(), *, allowed_directories):
    result = []
    for name in sorted(os.listdir(root_fd)):
        parts = prefix + (name,)
        _path("/".join(parts))
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            if stat.S_IMODE(info.st_mode) != 0o755 or parts not in allowed_directories:
                raise SandboxError(SandboxErrorCode.INTEGRITY)
            with walk_directory(root_fd, (name,)) as child_fd:
                result.extend(_tree_identities(child_fd, parts, allowed_directories=allowed_directories))
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            if info.st_size > _MAX_FILE_BYTES:
                raise SandboxError(SandboxErrorCode.INTEGRITY)
            with open_regular_file(root_fd, (name,), os.O_RDONLY) as file_fd:
                before = os.fstat(file_fd)
                digest = hashlib.sha256()
                size = 0
                while value := os.read(file_fd, min(65536, _MAX_FILE_BYTES + 1 - size)):
                    size += len(value)
                    if size > _MAX_FILE_BYTES:
                        raise SandboxError(SandboxErrorCode.INTEGRITY)
                    digest.update(value)
                after = os.fstat(file_fd)
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise SandboxError(SandboxErrorCode.INTEGRITY)
                result.append(_FileIdentity(path="/".join(parts), size_bytes=size, sha256=digest.hexdigest(),
                                            mode=stat.S_IMODE(after.st_mode)))
        else:
            raise SandboxError(SandboxErrorCode.PATH)
        if len(result) > _MAX_FILES:
            raise SandboxError(SandboxErrorCode.INTEGRITY)
    return tuple(sorted(result, key=lambda item: item.path.encode("utf-8")))


def _no_links(root_fd, *, depth=0, remaining=None):
    """Validate even a partial owned tree before descriptor-relative deletion."""
    if depth > 129:
        raise SandboxError(SandboxErrorCode.CLEANUP)
    if remaining is None:
        remaining = [_MAX_DIRECTORIES * 2 + _MAX_FILES * 2 + 3]
    for name in os.listdir(root_fd):
        remaining[0] -= 1
        if remaining[0] < 0:
            raise SandboxError(SandboxErrorCode.CLEANUP)
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            with walk_directory(root_fd, (name,)) as child_fd:
                _no_links(child_fd, depth=depth + 1, remaining=remaining)
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SandboxError(SandboxErrorCode.CLEANUP)


def _require_removal_platform():
    # These descriptor-relative POSIX APIs are available in Python 3.10;
    # shutil.rmtree(dir_fd=...) is not. Never fall back to path-based deletion.
    if (
        os.unlink not in os.supports_dir_fd or os.rmdir not in os.supports_dir_fd
        or os.stat not in os.supports_dir_fd or os.stat not in os.supports_follow_symlinks
        or os.listdir not in os.supports_fd
    ):
        raise SandboxError(SandboxErrorCode.UNAVAILABLE)


def _same_inode(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _remove_tree(root_fd, *, depth=0, remaining=None):
    """Delete relative to held parent descriptors, without following links."""
    if depth > 129:
        raise SandboxError(SandboxErrorCode.CLEANUP)
    if remaining is None:
        remaining = [_MAX_DIRECTORIES * 2 + _MAX_FILES * 2 + 3]
    # Keep the private owner marker until all other members are gone, so a
    # partial cleanup error retains the identity needed for trusted recovery.
    for name in sorted(os.listdir(root_fd), key=lambda value: (value == _MARKER, value)):
        remaining[0] -= 1
        if remaining[0] < 0:
            raise SandboxError(SandboxErrorCode.CLEANUP)
        before = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            with walk_directory(root_fd, (name,)) as child_fd:
                opened = os.fstat(child_fd)
                if not _same_inode(before, opened):
                    raise SandboxError(SandboxErrorCode.CLEANUP)
                _remove_tree(child_fd, depth=depth + 1, remaining=remaining)
            current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_inode(opened, current):
                raise SandboxError(SandboxErrorCode.CLEANUP)
            os.rmdir(name, dir_fd=root_fd)
        elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
            with open_regular_file(root_fd, (name,), os.O_RDONLY) as file_fd:
                opened = os.fstat(file_fd)
                if not _same_inode(before, opened):
                    raise SandboxError(SandboxErrorCode.CLEANUP)
                current = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode) or current.st_nlink != 1
                    or not _same_inode(opened, current)
                ):
                    raise SandboxError(SandboxErrorCode.CLEANUP)
                # unlink never follows a symlink, even if a hostile Host user
                # races the final name check. No target path is reopened.
                os.unlink(name, dir_fd=root_fd)
        else:
            raise SandboxError(SandboxErrorCode.CLEANUP)


class SnapshotMaterializer:
    def __init__(self, workspace_registry):
        self._registry = workspace_registry

    @contextmanager
    def _workspace(self, record):
        if not isinstance(record, WorkspaceRecord):
            raise SandboxError(SandboxErrorCode.INVALID)
        try:
            registered = self._registry.get_record(record.workspace_id, run_id=record.run_id)
            if registered != record:
                raise SandboxError(SandboxErrorCode.INTEGRITY)
            with self._registry._open_workspace(registered) as root_fd:
                yield root_fd
        except WorkspaceAccessError:
            raise SandboxError(SandboxErrorCode.PATH) from None

    @staticmethod
    def _content(record, stored):
        if (
            not isinstance(record, WorkspaceRecord) or not isinstance(stored, StoredContent)
            or not isinstance(stored.metadata, CodeSnapshotArtifact)
            or not isinstance(stored.content, bytes) or not stored.content
            or len(stored.content) > _MAX_ARCHIVE_BYTES
            or isinstance(stored.size_bytes, bool) or not isinstance(stored.size_bytes, int)
            or stored.size_bytes != len(stored.content)
            or stored.media_type != "application/x-tar" or stored.run_id != record.run_id
            or stored.metadata.artifact_uri != f"artifact://{stored.artifact_id}/source.tar"
            or hashlib.sha256(stored.content).hexdigest() != stored.content_sha256
            or stored.metadata.snapshot_sha256 != stored.content_sha256
        ):
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        return _archive_entries(stored.content)

    def prepare(self, workspace_record, stored_content, execution_id, *, inputs=None):
        _require_removal_platform()
        try:
            execution_id = workspace_uuid(execution_id)
        except WorkspaceAccessError:
            raise SandboxError(SandboxErrorCode.INVALID) from None
        entries = self._content(workspace_record, stored_content)
        input_entries = _inputs_entries(inputs)
        root = Path(workspace_record.root_path) / _BASE / str(execution_id)
        execution = MaterializedExecution(
            execution_id=execution_id, run_id=workspace_record.run_id,
            workspace_id=workspace_record.workspace_id, source_artifact_id=stored_content.artifact_id,
            snapshot_sha256=stored_content.content_sha256, execution_root=root,
            source_root=root / "source", inputs_root=root / "inputs" if input_entries else None,
            workspace_record=workspace_record, source_files=_identities(entries), input_files=_identities(input_entries),
        )
        with _LOCK, self._workspace(workspace_record) as root_fd:
            try:
                with walk_directory(root_fd, (_BASE,), create=True) as base_fd:
                    if stat.S_IMODE(os.fstat(base_fd).st_mode) != 0o700:
                        raise SandboxError(SandboxErrorCode.PATH)
                    try:
                        os.mkdir(str(execution_id), mode=0o700, dir_fd=base_fd)
                    except FileExistsError:
                        # Never adopt, overwrite, or remove an existing UUID.
                        raise SandboxError(SandboxErrorCode.INVALID) from None
                    with walk_directory(base_fd, (str(execution_id),)) as execution_fd:
                        owned = os.fstat(execution_fd)
                        try:
                            with open_regular_file(execution_fd, (_MARKER,), os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True) as marker_fd:
                                _write(marker_fd, _marker(execution))
                                os.fchmod(marker_fd, 0o600)
                                os.fsync(marker_fd)
                            _create_tree(execution_fd, "source", entries)
                            if input_entries:
                                _create_tree(execution_fd, "inputs", input_entries)
                            os.fsync(execution_fd)
                            os.fsync(base_fd)
                        except BaseException:
                            self._remove_owned(base_fd, str(execution_id), owned)
                            raise
            except (OSError, UnicodeError, WorkspaceAccessError):
                raise SandboxError(SandboxErrorCode.PATH, execution_id=execution_id) from None
        return execution

    @staticmethod
    def _remove_owned(base_fd, name, owned):
        _require_removal_platform()
        try:
            current = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_inode(current, owned):
                raise SandboxError(SandboxErrorCode.CLEANUP)
            with walk_directory(base_fd, (name,)) as execution_fd:
                if not _same_inode(os.fstat(execution_fd), owned):
                    raise SandboxError(SandboxErrorCode.CLEANUP)
                _no_links(execution_fd)
                _remove_tree(execution_fd)
            current = os.stat(name, dir_fd=base_fd, follow_symlinks=False)
            if not stat.S_ISDIR(current.st_mode) or not _same_inode(current, owned):
                raise SandboxError(SandboxErrorCode.CLEANUP)
            os.rmdir(name, dir_fd=base_fd)
        except (OSError, WorkspaceAccessError, RecursionError):
            raise SandboxError(SandboxErrorCode.CLEANUP) from None

    @staticmethod
    def _identity(execution):
        if not isinstance(execution, MaterializedExecution):
            raise SandboxError(SandboxErrorCode.INVALID)
        record = execution.workspace_record
        try:
            workspace_uuid(execution.execution_id)
        except WorkspaceAccessError:
            raise SandboxError(SandboxErrorCode.INVALID) from None
        expected = Path(record.root_path) / _BASE / str(execution.execution_id)
        if (
            execution.run_id != record.run_id or execution.workspace_id != record.workspace_id
            or execution.execution_root != expected or execution.source_root != expected / "source"
            or execution.inputs_root != (expected / "inputs" if execution.input_files else None)
        ):
            raise SandboxError(SandboxErrorCode.INTEGRITY)

    @staticmethod
    def _validate_tree(execution_fd, execution):
        expected_names = {_MARKER, "source"} | ({"inputs"} if execution.inputs_root is not None else set())
        if set(os.listdir(execution_fd)) != expected_names or stat.S_IMODE(os.fstat(execution_fd).st_mode) != 0o700:
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        with open_regular_file(execution_fd, (_MARKER,), os.O_RDONLY) as marker_fd:
            info = os.fstat(marker_fd)
            if stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 4096 or os.read(marker_fd, 4097) != _marker(execution):
                raise SandboxError(SandboxErrorCode.INTEGRITY)
        for name, expected in (("source", execution.source_files), ("inputs", execution.input_files)):
            if name == "inputs" and execution.inputs_root is None:
                continue
            directories = {
                parts[:length]
                for item in expected
                for parts in (tuple(item.path.split("/")),)
                for length in range(1, len(parts))
            }
            with walk_directory(execution_fd, (name,)) as tree_fd:
                if stat.S_IMODE(os.fstat(tree_fd).st_mode) != 0o755 or _tree_identities(tree_fd, allowed_directories=directories) != expected:
                    raise SandboxError(SandboxErrorCode.INTEGRITY)

    def validate(self, execution, stored_content):
        self._identity(execution)
        entries = self._content(execution.workspace_record, stored_content)
        if (
            execution.source_artifact_id != stored_content.artifact_id
            or execution.snapshot_sha256 != stored_content.content_sha256
            or execution.source_files != _identities(entries)
        ):
            raise SandboxError(SandboxErrorCode.INTEGRITY)
        try:
            with _LOCK, self._workspace(execution.workspace_record) as root_fd:
                with walk_directory(root_fd, (_BASE, str(execution.execution_id))) as execution_fd:
                    self._validate_tree(execution_fd, execution)
        except (OSError, WorkspaceAccessError):
            raise SandboxError(SandboxErrorCode.PATH) from None

    def cleanup(self, execution):
        self._identity(execution)
        try:
            with _LOCK, self._workspace(execution.workspace_record) as root_fd:
                with walk_directory(root_fd, (_BASE,)) as base_fd:
                    with walk_directory(base_fd, (str(execution.execution_id),)) as execution_fd:
                        self._validate_tree(execution_fd, execution)
                        owned = os.fstat(execution_fd)
                    self._remove_owned(base_fd, str(execution.execution_id), owned)
        except (SandboxError, OSError, WorkspaceAccessError):
            raise SandboxError(SandboxErrorCode.CLEANUP, execution_id=execution.execution_id) from None
