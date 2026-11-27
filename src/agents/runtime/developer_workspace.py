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
from hashlib import sha256
from io import StringIO
from itertools import zip_longest
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
import tokenize
import unicodedata

from mcp_tools.tools.file_io import _locked_root
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import GitSnapshotBuilder, GitSnapshotLimits, _object_hash, _validate_source_path
from orchestrator.core.security import redact_text
from orchestrator.core.source_credentials import SourceCredentialError, validate_source_bytes
from orchestrator.domain.developer_artifacts import ChangeReportFile, ReadyEdit, ReportedPathEdit
from agents.runtime.repair_progress import changed_functions
from agents.runtime.normal_request_repair import NORMAL_REQUEST, changed_frame_refs
from agents.runtime.repair_dependencies import indirect_frame_refs
from agents.runtime.ready_path_diff import INVALID_INPUT, ReadySourceComparison, changed_path_refs, claim_issues
from agents.runtime.ready_library_types import ready_library_type_mismatches
from agents.runtime.unused_rewrite import without_discarded_operands
from agents.llm.contracts import SourceEditStatus
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.materialization import _archive_entries, _check_names, _path as _snapshot_path
from orchestrator.workspaces.filesystem import BoundWorkspace, open_regular_file, walk_directory


_CODES = frozenset({
    "DEVELOPER_CHECKPOINT_INVALID", "DEVELOPER_CHECKPOINT_BASELINE_MISMATCH",
    "DEVELOPER_CHECKPOINT_NO_CHANGES", "DEVELOPER_CHECKPOINT_LOCK_MISMATCH",
    "DEVELOPER_CHECKPOINT_STATE_INVALID", "DEVELOPER_CHECKPOINT_TIMEOUT",
    "DEVELOPER_CHECKPOINT_FAILED", "DEVELOPER_CHECKPOINT_PATH_EVIDENCE_MISMATCH",
    "DEVELOPER_CHECKPOINT_LIBRARY_TYPE_MISMATCH",
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
    edits: tuple[ReadyEdit, ...] = field(repr=False)
    working_base_commit: str = field(repr=False)
    repository_id: str = field(repr=False)
    lock_path: str = field(repr=False)
    baseline_commit_hash: str = field(repr=False)
    parent_commit_hash: str | None = field(default=None, repr=False)
    reported_path_edits: tuple[ReportedPathEdit, ...] = field(default=(), repr=False)


def _check_deadline(deadline):
    if time.monotonic() >= deadline:
        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_TIMEOUT")


def _signature(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_mode, info.st_nlink)


def _python_tokens(content):
    # Read source as data. Preserve string literals (including '#' in multiline
    # strings), logical newlines and indentation. Defer unfamiliar encodings.
    lines = StringIO(content.decode('utf-8')).readline
    for token in tokenize.generate_tokens(lines):
        if token.type == tokenize.COMMENT:
            if ((token.start[0] == 1 and token.string.startswith('#!'))
                    or (token.start[0] <= 2 and re.search(r'coding[:=]', token.string))
                    or re.match(r'#\s*(?:type:|noqa\b|pragma:|fmt:|pyright:|mypy:|nosec\b)', token.string)):
                yield token.type, token.string, token.start
        elif token.type != tokenize.NL:
            yield token.type, token.string


def _repair_edit_status(before, after, *, check_unused_rewrite=True):
    if before.keys() != after.keys():
        return SourceEditStatus.CHANGED
    ignored = SourceEditStatus.UNCHANGED
    imports = {'re', 'fastapi', 'starlette'}
    shadowed = check_unused_rewrite and any(
        part.removesuffix('.py') in imports for path in before for part in path.split('/'))
    for path, (content, mode) in after.items():
        original, old_mode = before[path]
        if mode != old_mode:
            return SourceEditStatus.CHANGED
        if content == original:
            continue
        if not path.endswith('.py'):
            return SourceEditStatus.CHANGED
        try:
            if all(a == b for a, b in zip_longest(_python_tokens(original), _python_tokens(content))):
                continue
            if check_unused_rewrite and not shadowed:
                left, right = without_discarded_operands(original), without_discarded_operands(content)
                if left != original and right != content and all(
                        a == b for a, b in zip_longest(_python_tokens(left), _python_tokens(right))):
                    ignored = SourceEditStatus.UNUSED_TEXT_REWRITE
                    continue
        except (UnicodeError, SyntaxError, tokenize.TokenError):
            pass  # Leave unfamiliar source to the ordinary build.
        return SourceEditStatus.CHANGED
    return ignored


def _repair_changes_code(before, after, *, check_unused_rewrite=True):
    return _repair_edit_status(before, after, check_unused_rewrite=check_unused_rewrite) is SourceEditStatus.CHANGED


def _failure(error):
    if isinstance(error, DeveloperCheckpointError):
        return error.code
    if isinstance(error, ArtifactAccessError) and error.code is ArtifactErrorCode.TIMEOUT:
        return "DEVELOPER_CHECKPOINT_TIMEOUT"
    return "DEVELOPER_CHECKPOINT_FAILED"


class GitDeveloperCheckpoint:
    """Freeze real working bytes; a service can resubmit an unchanged candidate."""

    def __init__(self, workspace: BoundWorkspace, *, baseline_commit_hash,
                 lock_path, repository_id, limits=GitSnapshotLimits(), parent_commit_hash=None,
                 runtime_repair=False, library_environment=None, initial_source_files=None,
                 allow_unchanged_source=False):
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
                    or not isinstance(limits, GitSnapshotLimits)
                    or type(allow_unchanged_source) is not bool):
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
        self._working_base_commit = self._parent_commit
        self._limits = copied_limits
        self._state, self._guard = "NEW", threading.Lock()
        self._baseline = None
        self._parent = None
        self.repair_baseline = None
        self.repair_history = None
        self.repair_issues = ()
        self._progress_base = None
        # Host-only immutable continuation seed. Never a Tool/model argument.
        # Git approval stays on the original baseline; this only authenticates
        # the selected parent's working bytes before tools are exposed.
        self._initial_files = None
        if initial_source_files is not None:
            if self._parent_commit != self._baseline_commit:
                raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_BASELINE_MISMATCH')
            from orchestrator.infrastructure.service_continuations import _validate_inventory
            _validate_inventory(initial_source_files)
            self._initial_files = dict(initial_source_files)
        self._runtime_repair = runtime_repair or self._initial_files is not None
        self._allow_unchanged_source = allow_unchanged_source
        self._library_environment = dict(library_environment or {})
        self._library_type_issues = ()
        self.observed_output_repair = False
        self._ready_paths = ()
        self._priority_paths = ()
        self._normal_request_sites = ()
        self._invalid_input_sites = ()
        self._changed_refs = set()
        self._source_comparison = ReadySourceComparison()
        self._app_entry = (None, None)
        self._signup_route = (None, None)
        self._path_links = {}

    def __repr__(self):
        return "GitDeveloperCheckpoint()"

    @property
    def source_root(self) -> Path:
        """Absolute workspace source directory, for read-only Host prechecks."""
        return self._source

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
                    content = b"".join(chunks)
                    try:
                        validate_source_bytes(content)
                    except SourceCredentialError:
                        raise DeveloperCheckpointError() from None
                    files[path] = (content, 0o755 if info.st_mode & 0o111 else 0o644)
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

    def repair_source_files(self):
        """Use the restored working baseline when the latest candidate failed its build."""
        files = self._progress_base if self._progress_base is not None else self._snapshot_files(self._parent)
        return {path: content for path, (content, _mode) in files.items() if path.endswith('.py')}

    def starting_source_files(self):
        """Immutable approved starting code, never the latest generated candidate."""
        with self._guard:
            if self._state != "PREPARED":
                raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_STATE_INVALID")
            return {path: content for path, (content, _mode) in self._snapshot_files(self._baseline).items()
                    if path.endswith('.py')}

    def review_source_files(self, *, deadline_monotonic=None):
        """Read-only bounded working inventory under the existing workspace lock."""
        with self._guard:
            if self._state != "PREPARED":
                raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_STATE_INVALID")
            deadline = self._deadline(deadline_monotonic)
            with _locked_root(self._workspace, write=False) as root_fd:
                with walk_directory(root_fd, ("source",)) as source_fd:
                    files = self._inventory(source_fd, deadline)
            return {path: content for path, (content, _mode) in files.items() if path.endswith('.py')}

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
                    expected = self._snapshot_files(parent) if self._initial_files is None else self._initial_files
                    if (parent.git_object_format != baseline.git_object_format
                            or parent.dependency_lock_hash != baseline.dependency_lock_hash
                            or expected.get(self._lock_path) != self._snapshot_files(parent).get(self._lock_path)
                            or self._inventory(source_fd, deadline) != expected):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
                    self._baseline, self._parent = baseline, parent
                    if self._initial_files is not None:
                        # A private object gives ReadyReview an exact diff base.
                        # HEAD, the real index and approved starting identity stay unchanged.
                        seed = self._builder(deadline).build(self._commit(expected, deadline), self._lock_path)
                        if self._snapshot_files(seed) != expected or self._inventory(source_fd, deadline) != expected:
                            raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_BASELINE_MISMATCH')
                        self._progress_base = dict(expected)
                        self._working_base_commit = seed.commit_hash
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

    def restore_repair_baseline(self, snapshot, *, deadline_monotonic=None):
        """Restore authenticated earlier bytes; keep the rejected candidate as Git parent.

        Called only before exposing tools. A restoration by itself is not model
        progress and cannot satisfy READY. CAS never overwrites intervening edits.
        """
        from mcp_tools.tools.file_io import apply_changes
        with self._guard:
            if self._state != 'PREPARED' or self._progress_base is not None:
                raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_STATE_INVALID')
            deadline = self._deadline(deadline_monotonic)
            parent = self._snapshot_files(self._parent)
            target = self._snapshot_files(snapshot)
            if (snapshot.dependency_lock_hash != self._parent.dependency_lock_hash
                    or target.get(self._lock_path) != parent.get(self._lock_path)
                    or any(mode != parent.get(path, (None, 0o644))[1]
                           for path, (_content, mode) in target.items())):
                raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_LOCK_MISMATCH')
            with _locked_root(self._workspace, write=False) as root_fd:
                with walk_directory(root_fd, ('source',)) as source_fd:
                    if self._inventory(source_fd, deadline) != parent:
                        raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_BASELINE_MISMATCH')
            changed = {path for path in set(target) | set(parent) if target.get(path) != parent.get(path)}
            if changed:
                apply_changes(self._workspace,
                    {'source/' + p: target[p][0] if p in target else None for p in changed},
                    {'source/' + p: sha256(parent[p][0]).hexdigest() if p in parent else None for p in changed})
            with _locked_root(self._workspace, write=False) as root_fd:
                with walk_directory(root_fd, ('source',)) as source_fd:
                    if self._inventory(source_fd, deadline) != target:
                        raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_BASELINE_MISMATCH')
            self._progress_base = target
            self._working_base_commit = snapshot.commit_hash

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

    def has_source_changes(self, *, deadline_monotonic=None):
        return self.source_edit_status(deadline_monotonic=deadline_monotonic) is SourceEditStatus.CHANGED

    def configure_path_review(self, paths, scenario, priority_paths=(), normal_request_sites=(), invalid_input_sites=()):
        external = scenario.get('externalContract', {})
        app = external.get('app', {})
        self._app_entry = (app.get('module'), app.get('attribute'))
        api = external.get('signupApi', {})
        self._signup_route = (api.get('method'), api.get('path')) if isinstance(api, dict) else (None, None)
        self._normal_request_sites = tuple(normal_request_sites)
        self._invalid_input_sites = tuple(invalid_input_sites)
        self._ready_paths = tuple(paths)
        self._priority_paths = tuple(p for p in priority_paths if p in self._ready_paths)

    def _review_links(self, base, files):
        before = {p: data for p, (data, _) in base.items()}
        after = {p: data for p, (data, _) in files.items()}
        self._changed_refs = set()
        self._source_comparison = ReadySourceComparison()
        links = changed_path_refs(before, after, self._ready_paths, self._signup_route, self._app_entry,
                                  invalid_input_sites=self._invalid_input_sites, changed_refs=self._changed_refs,
                                  comparison=self._source_comparison)
        indirect = {}
        if NORMAL_REQUEST in self._ready_paths:
            direct = changed_frame_refs(before, after, self._normal_request_sites)
            indirect[NORMAL_REQUEST] = indirect_frame_refs(before, after, self._normal_request_sites) - direct
            links[NORMAL_REQUEST] = direct | indirect[NORMAL_REQUEST]
        if INVALID_INPUT in self._ready_paths:
            # A trace from a related request cannot prove the same cause for all failed cases.
            indirect[INVALID_INPUT] = links[INVALID_INPUT]
        return links, indirect

    def ready_path_issues(self, draft):
        return claim_issues(draft.get('repairPaths') or {}, self._path_links, self._changed_refs, self._source_comparison)

    def pending_priority_paths(self):
        if NORMAL_REQUEST in self._priority_paths and not self._path_links.get(NORMAL_REQUEST):
            return (NORMAL_REQUEST,)
        return () if any(self._path_links.get(p) for p in self._priority_paths) else self._priority_paths

    def correction_candidates(self, paths):
        # source_edit_status() observes the working tree before these links are used.
        from agents.llm.recovery import safe_source_ref
        return {path: [ref for ref in sorted(self._path_links[path]) if safe_source_ref(ref)][:4]
                for path in paths}

    def _check_library_types(self, files):
        if self._library_environment.get('dependencyLockHash') != self._parent.dependency_lock_hash:
            return ()
        return ready_library_type_mismatches(
            {path: content for path, (content, _mode) in files.items()}, self._library_environment)

    def library_type_issues(self):
        """Findings from the same Host inventory used to validate this READY."""
        return self._library_type_issues

    def source_edit_status(self, *, deadline_monotonic=None):
        """Host-only net diff against the prepared immutable parent; no commit or execution.

        Shares inventory, lock and Git identity checks with checkpoint. A write
        followed by a revert is not progress. A resumed step keeps its parent.
        """
        with self._guard:
            if self._state != "PREPARED":
                raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_STATE_INVALID")
            deadline = self._deadline(deadline_monotonic)
            with _locked_root(self._workspace, write=False) as root_fd:
                with walk_directory(root_fd, ("source",)) as source_fd:
                    self._source_identity(source_fd)
                    parent = self._builder(deadline).build(self._parent_commit, self._lock_path)
                    if parent != self._parent:
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_BASELINE_MISMATCH")
                    files = self._inventory(source_fd, deadline)
                    before = self._snapshot_files(parent)
                    if files.get(self._lock_path) != before.get(self._lock_path):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_LOCK_MISMATCH")
                    base = self._progress_base if self._progress_base is not None else before
                    self._library_type_issues = self._check_library_types(files)
                    if self._ready_paths:
                        self._path_links, _ = self._review_links(base, files)
                    if self._runtime_repair:
                        return _repair_edit_status(base, files, check_unused_rewrite=self.observed_output_repair)
                    return SourceEditStatus.CHANGED if files != base else SourceEditStatus.UNCHANGED

    def checkpoint(self, *, deadline_monotonic=None, reported_path_edits=()):
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
                    base = self._progress_base if self._progress_base is not None else before
                    if not self._allow_unchanged_source and (not changes or files == base or (
                            self._runtime_repair and not _repair_changes_code(
                                base, files, check_unused_rewrite=self.observed_output_repair))):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_NO_CHANGES")
                    if self._check_library_types(files):
                        raise DeveloperCheckpointError("DEVELOPER_CHECKPOINT_LIBRARY_TYPE_MISMATCH")
                    verified = ()
                    if self._ready_paths or reported_path_edits:
                        links, indirect = self._review_links(base, files)
                        rows = {row.path: {'sourceRef': row.source_ref, 'edit': row.edit} for row in reported_path_edits}
                        if set(rows) != set(links) or claim_issues(rows, links):
                            raise DeveloperCheckpointError('DEVELOPER_CHECKPOINT_PATH_EVIDENCE_MISMATCH')
                        verified = tuple(row.with_host_comparison(
                            indirect=row.source_ref in indirect.get(row.path, ())
                        ) for row in reported_path_edits)
                    # Reuse the authenticated parent/continuation seed when its bytes match.
                    # Do not invent a change or create an empty checkpoint commit.
                    if self._allow_unchanged_source and files == base:
                        commit = self._working_base_commit
                    elif self._allow_unchanged_source and files == before:
                        commit = parent.commit_hash
                    else:
                        commit = self._commit(files, deadline)
                    candidate = self._builder(deadline).build(commit, self._lock_path)
                    if (self._snapshot_files(candidate) != files or self._inventory(source_fd, deadline) != files
                            or candidate.dependency_lock_hash != baseline.dependency_lock_hash):
                        raise DeveloperCheckpointError()
                    edited = sorted(p for p in base.keys() | files.keys() if base.get(p) != files.get(p))
                    symbols = changed_functions({p: base[p][0] for p in edited if p in base},
                                                {p: files[p][0] for p in edited if p in files}, edited)
                    edits = tuple(ReadyEdit(path="source/" + path, changed_functions=symbols.get(path, ()),
                                  action="ADDED" if path not in base else "DELETED" if path not in files else "MODIFIED")
                                  for path in edited)
                    result = DeveloperCheckpointResult(commit_hash=commit, changes=changes,
                        edits=edits, working_base_commit=self._working_base_commit, reported_path_edits=verified,
                        repository_id=self._repository_id, lock_path=self._lock_path,
                        baseline_commit_hash=self._baseline_commit, parent_commit_hash=self._parent_commit)
            self._state = "DONE"
        except Exception as error:
            failure = _failure(error)
            self._state = "FAILED"
        if failure is not None:
            raise DeveloperCheckpointError(failure)
        return result
