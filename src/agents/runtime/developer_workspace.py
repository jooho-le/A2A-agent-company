"""One Host-only Working Copy checkpoint, never a product-code executor.

The caller owns a registered Developer Workspace and an exact starting commit.
Only immutable Git objects are written: HEAD, the real index and Working Copy
are untouched. Source capture cooperates with the File Tools' root inode lock.
This is not protection against another process with the same OS privileges
replacing Git metadata/pathnames. Callers running this synchronous capability
in a worker must drain that worker on cancellation; cancelling to_thread alone
does not stop Git writes. A failed/uncertain checkpoint cannot be replayed.
"""

from dataclasses import asdict, dataclass, field, replace
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata

from mcp_tools.tools.file_io import _locked_root
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits, _object_hash, _validate_source_path
from orchestrator.core.security import redact_text
from orchestrator.domain.developer_artifacts import ChangeReportFile
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.materialization import _archive_entries, _check_names, _path as _snapshot_path
from orchestrator.workspaces.filesystem import BoundWorkspace, open_regular_file, walk_directory


_CODES = frozenset({
    "DEVELOPER_CHECKPOINT_INVALID", "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH",
    "DEVELOPER_CHECKPOINT_NO_CHANGES", "DEVELOPER_CHECKPOINT_LOCK_MISMATCH",
    "DEVELOPER_CHECKPOINT_STATE_INVALID", "DEVELOPER_CHECKPOINT_TIMEOUT",
    "DEVELOPER_CHECKPOINT_FAILED",
})
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_MAX_DEPTH = 64
_MAX_STDERR = 65536


class DeveloperCheckpointError(RuntimeError):
    def __init__(self, code="DEVELOPER_CHECKPOINT_FAILED"):
        self.code = code if type(code) is str and code in _CODES else "DEVELOPER_CHECKPOINT_FAILED"
        super().__init__(self.code)


@dataclass(frozen=True, kw_only=True)
class DeveloperCheckpointResult:
    commit_hash: str = field(repr=False)
    changes: tuple[ChangeReportFile, ...] = field(repr=False)
    repository_id: str = field(repr=False)
    lock_path: str = field(repr=False)
    baseline_commit_hash: str = field(repr=False)
    parent_commit_hash: str | None = field(default=None, repr=False)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_TIMEOUT")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)


def _failure(error):
    if isinstance(error, DeveloperCheckpointError):
        return error.code
    if isinstance(error, ArtifactAccessError) and error.code is ArtifactErrorCode.TIMEOUT:
        return "DEVELOPER_CHECKPOINT_TIMEOUT"
    return "DEVELOPER_CHECKPOINT_FAILED"


class GitDeveloperCheckpoint:
    """Inert Host capability: prepare once, then commit one actual nonempty diff."""

    def __init__(self, workspace: BoundWorkspace, *, baseline_commit_hash,
                 lock_path, repository_id, limits=GitSnapshotLimits(), parent_commit_hash=None):
        invalid = False
        try:
            if (not isinstance(workspace, BoundWorkspace) or workspace.role is not AgentRole.DEVELOPER
                    or not callable(workspace.opener)
                    or type(baseline_commit_hash) is not str or _OID.fullmatch(baseline_commit_hash) is None
                    or parent_commit_hash is not None and (type(parent_commit_hash) is not str
                        or _OID.fullmatch(parent_commit_hash) is None)
                    or type(lock_path) is not str or type(repository_id) is not str
                    or not repository_id.strip() or repository_id != repository_id.strip()
                    or len(repository_id.encode("utf-8")) > 256
                    or any(ord(char) < 32 or ord(char) == 127 for char in repository_id)
                    or redact_text(repository_id) != repository_id
                    or not isinstance(limits, GitSnapshotLimits)):
                raise ValueError
            _validate_source_path(lock_path)
            copied_limits = GitSnapshotLimits(**asdict(limits))
            if (not math.isfinite(copied_limits.timeout_seconds)
                    or copied_limits.max_files > 1000 or copied_limits.max_file_bytes > 1048576
                    or copied_limits.max_total_bytes > 16 * 1048576 or copied_limits.max_archive_bytes > 20 * 1048576):
                raise ValueError
            source = Path(workspace.record.root_path) / "source"
            if not source.is_absolute():
                raise ValueError
        except Exception:
            invalid = True
        if invalid:
            raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_INVALID")
        self._workspace, self._source = workspace, source
        self._baseline_commit, self._lock_path, self._repository_id = baseline_commit_hash, lock_path, repository_id
        self._parent_commit = baseline_commit_hash if parent_commit_hash is None else parent_commit_hash
        self._limits = copied_limits
        self._state, self._guard = "NEW", threading.Lock()
        self._baseline = None
        self._parent = None

    def __repr__(self):
        return "GitDeveloperCheckpoint()"

    def _enter(self, expected, next_state):
        with self._guard:
            if self._state != expected:
                raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_STATE_INVALID")
            self._state = next_state

    def _deadline(self, supplied):
        now = time.monotonic()
        if supplied is not None and (type(supplied) not in (int, float) or not math.isfinite(supplied)):
            raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_INVALID")
        deadline = now + self._limits.timeout_seconds
        if supplied is not None:
            deadline = min(deadline, supplied)
        _check_deadline(deadline)
        return deadline

    def _builder(self, deadline):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_TIMEOUT")
        limits = replace(self._limits, timeout_seconds=min(self._limits.timeout_seconds, remaining))
        return GitSnapshotBuilder(self._source, limits)

    def _source_identity(self, descriptor):
        current = self._source.lstat()
        opened = os.fstat(descriptor)
        if (not stat.S_ISDIR(current.st_mode) or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            raise DeveloperCheckpointError()

    def _inventory(self, source_fd, deadline):
        files, normalized = {}, set()
        total = 0

        def walk(directory_fd, prefix):
            nonlocal total
            _check_deadline(deadline)
            if len(prefix) > _MAX_DEPTH:
                raise DeveloperCheckpointError()
            before_directory = os.fstat(directory_fd)
            with os.scandir(directory_fd) as entries:
                names = []
                for entry in entries:
                    _check_deadline(deadline)
                    if len(names) >= self._limits.max_files * 16 + 2:
                        raise DeveloperCheckpointError()
                    names.append(entry.name)
                names.sort()
            for name in names:
                _check_deadline(deadline)
                if not prefix and name == ".git":
                    continue
                parts = (*prefix, name)
                path = "/".join(parts)
                _validate_source_path(path)
                _raw_parts, normalized_parts = _snapshot_path(path)
                if (redact_text(path) != path
                        or any(part.startswith(".mcp-write-") for part in normalized_parts)):
                    raise DeveloperCheckpointError()
                canonical = unicodedata.normalize("NFKC", path).casefold()
                if canonical in normalized or len(normalized) >= self._limits.max_files * 16 + 1:
                    raise DeveloperCheckpointError()
                normalized.add(canonical)
                info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    with walk_directory(directory_fd, (name,)) as child:
                        if (info.st_dev, info.st_ino) != (os.fstat(child).st_dev, os.fstat(child).st_ino):
                            raise DeveloperCheckpointError()
                        walk(child, parts)
                elif stat.S_ISREG(info.st_mode):
                    if len(files) >= self._limits.max_files or info.st_size > self._limits.max_file_bytes:
                        raise DeveloperCheckpointError()
                    with open_regular_file(directory_fd, (name,), os.O_RDONLY) as file_fd:
                        before = os.fstat(file_fd)
                        if _signature(info) != _signature(before):
                            raise DeveloperCheckpointError()
                        chunks, size = [], 0
                        while True:
                            _check_deadline(deadline)
                            chunk = os.read(file_fd, min(65536, self._limits.max_file_bytes + 1 - size))
                            if not chunk:
                                break
                            chunks.append(chunk)
                            size += len(chunk)
                            if size > self._limits.max_file_bytes:
                                raise DeveloperCheckpointError()
                        after = os.fstat(file_fd)
                        if (_signature(before) != _signature(after) or size != before.st_size
                                or _signature(after) != _signature(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))):
                            raise DeveloperCheckpointError()
                    total += size
                    if total > self._limits.max_total_bytes:
                        raise DeveloperCheckpointError()
                    files[path] = (b"".join(chunks), 0o755 if info.st_mode & 0o111 else 0o644)
                else:
                    raise DeveloperCheckpointError()
            if _signature(before_directory) != _signature(os.fstat(directory_fd)):
                raise DeveloperCheckpointError()

        walk(source_fd, ())
        _check_names(files)
        self._source_identity(source_fd)
        return files

    @staticmethod
    def _snapshot_files(snapshot):
        return {path: (content, mode) for path, content, mode in _archive_entries(snapshot.archive)}

    def prepare(self, *, deadline_monotonic=None):
        self._enter("NEW", "PREPARING")
        failure = None
        try:
            deadline = self._deadline(deadline_monotonic)
            with _locked_root(self._workspace, write=True) as root_fd:
                with walk_directory(root_fd, ("source",)) as source_fd:
                    self._source_identity(source_fd)
                    baseline = self._builder(deadline).build(self._baseline_commit, self._lock_path)
                    parent = baseline if self._parent_commit == self._baseline_commit else self._builder(deadline).build(
                        self._parent_commit, self._lock_path)
                    if (parent.git_object_format != baseline.git_object_format
                            or parent.dependency_lock_hash != baseline.dependency_lock_hash
                            or self._inventory(source_fd, deadline) != self._snapshot_files(parent)):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
            self._baseline = baseline
            self._parent = parent
            self._state = "PREPARED"
        except Exception as error:
            failure = _failure(error)
            self._state = "FAILED"
        if failure is not None:
            raise DeveloperCheckpointError(failure)

    def _git(self, index_path):
        executable = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
        if executable is None:
            raise DeveloperCheckpointError()
        executable = str(Path(executable).resolve(strict=True))
        environment = {
            "PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
            "HOME": "/dev/null", "XDG_CONFIG_HOME": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": str(index_path),
            "GIT_AUTHOR_NAME": "A2A Developer Runtime", "GIT_COMMITTER_NAME": "A2A Developer Runtime",
            "GIT_AUTHOR_EMAIL": "runtime@example.invalid", "GIT_COMMITTER_EMAIL": "runtime@example.invalid",
        }
        base = [executable, "--no-pager", "--no-optional-locks", "--git-dir", str(self._source / ".git"),
            "--work-tree", str(self._source), "-c", "protocol.allow=never", "-c", "core.hooksPath=/dev/null",
            "-c", "core.fsmonitor=false", "-c", "core.attributesFile=/dev/null", "-c", "commit.gpgSign=false",
            "-c", "core.splitIndex=false", "-c", "core.sparseCheckout=false", "-c", "index.sparse=false",
            "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "safe.directory=" + str(self._source)]
        return base, environment

    def _run(self, argv, environment, deadline, *, content=b"", output_limit=256):
        _check_deadline(deadline)
        process, failure, output = None, None, None
        try:
            process = subprocess.Popen(argv, cwd=self._source, env=environment, shell=False,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            buffers = {"stdout": bytearray(), "stderr": bytearray()}
            pending = memoryview(content)
            with selectors.DefaultSelector() as selector:
                for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, label)
                if pending:
                    os.set_blocking(process.stdin.fileno(), False)
                    selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
                else:
                    process.stdin.close()
                while selector.get_map():
                    _check_deadline(deadline)
                    for key, _ in selector.select(min(.1, max(0, deadline - time.monotonic()))):
                        if key.data == "stdin":
                            size = os.write(key.fileobj.fileno(), pending[:65536])
                            pending = pending[size:]
                            if not pending:
                                selector.unregister(key.fileobj)
                                key.fileobj.close()
                        else:
                            value = os.read(key.fileobj.fileno(), 65536)
                            if not value:
                                selector.unregister(key.fileobj)
                            else:
                                buffers[key.data].extend(value)
                                if len(buffers[key.data]) > (output_limit if key.data == "stdout" else _MAX_STDERR):
                                    raise DeveloperCheckpointError()
            _check_deadline(deadline)
            if process.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
                raise DeveloperCheckpointError()
            output = bytes(buffers["stdout"])
        except subprocess.TimeoutExpired:
            failure = "DEVELOPER_CHECKPOINT_TIMEOUT"
        except Exception as error:
            failure = _failure(error)
        finally:
            if process is not None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except (ProcessLookupError, PermissionError):
                        process.kill()
                process.wait()
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
        if failure is not None:
            raise DeveloperCheckpointError(failure)
        return output

    def _commit(self, files, deadline):
        # The index starts outside the Workspace and does not import the real
        # index or apply .gitignore, clean filters, checkout filters or hooks.
        with tempfile.TemporaryDirectory(prefix="a2a-developer-index-") as temporary:
            base, environment = self._git(Path(temporary) / "index")
            self._run(base + ["read-tree", "--empty"], environment, deadline)
            lines = []
            object_format = self._baseline.git_object_format
            expected_size = 40 if object_format == "sha1" else 64
            for path in sorted(files, key=lambda value: value.encode("utf-8")):
                content, mode = files[path]
                raw = self._run(base + ["hash-object", "--no-filters", "-w", "--stdin"],
                    environment, deadline, content=content).strip()
                if (re.fullmatch(b"[0-9a-f]{" + str(expected_size).encode() + b"}", raw) is None
                        or raw.decode("ascii") != _object_hash(object_format, "blob", content)):
                    raise DeveloperCheckpointError()
                lines.append(str(100755 if mode == 0o755 else 100644).encode() + b" " + raw + b"\t" + path.encode("utf-8") + b"\0")
            self._run(base + ["update-index", "-z", "--index-info"], environment, deadline, content=b"".join(lines))
            tree = self._run(base + ["write-tree"], environment, deadline).strip().decode("ascii")
            if re.fullmatch(r"[0-9a-f]{" + str(expected_size) + r"}", tree) is None:
                raise DeveloperCheckpointError()
            commit = self._run(base + ["commit-tree", tree, "-p", self._parent_commit, "--no-gpg-sign",
                "-m", "A2A Developer checkpoint"], environment, deadline).strip().decode("ascii")
            if re.fullmatch(r"[0-9a-f]{" + str(expected_size) + r"}", commit) is None:
                raise DeveloperCheckpointError()
            return commit

    def checkpoint(self, *, deadline_monotonic=None):
        self._enter("PREPARED", "CHECKPOINTING")
        failure, result = None, None
        try:
            deadline = self._deadline(deadline_monotonic)
            with _locked_root(self._workspace, write=True) as root_fd:
                with walk_directory(root_fd, ("source",)) as source_fd:
                    self._source_identity(source_fd)
                    # Revalidate all Git metadata before any object write.
                    baseline = self._builder(deadline).build(self._baseline_commit, self._lock_path)
                    if baseline != self._baseline:
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
                    parent = baseline if self._parent_commit == self._baseline_commit else self._builder(deadline).build(
                        self._parent_commit, self._lock_path)
                    if parent != self._parent:
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
                    files = self._inventory(source_fd, deadline)
                    before = self._snapshot_files(parent)
                    if files.get(self._lock_path) != before.get(self._lock_path):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_LOCK_MISMATCH")
                    changes = tuple(ChangeReportFile(path="source/" + path,
                        action="ADDED" if path not in before else "DELETED" if path not in files else "MODIFIED")
                        for path in sorted(set(before) | set(files), key=lambda value: value.encode("utf-8"))
                        if before.get(path) != files.get(path))
                    if not changes:
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_NO_CHANGES")
                    commit = self._commit(files, deadline)
                    candidate = self._builder(deadline).build(commit, self._lock_path)
                    if (self._snapshot_files(candidate) != files or self._inventory(source_fd, deadline) != files
                            or candidate.dependency_lock_hash != baseline.dependency_lock_hash):
                        raise DeveloperCheckpointError()
                    result = DeveloperCheckpointResult(commit_hash=commit, changes=changes,
                        repository_id=self._repository_id, lock_path=self._lock_path,
                        baseline_commit_hash=self._baseline_commit, parent_commit_hash=self._parent_commit)
            self._state = "DONE"
        except Exception as error:
            failure = _failure(error)
            self._state = "FAILED"
        if failure is not None:
            raise DeveloperCheckpointError(failure)
        return result
