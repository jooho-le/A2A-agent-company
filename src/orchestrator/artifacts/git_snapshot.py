"""Bounded, read-only Git object export; never executes or extracts Source.

The Host supplies a registry-bound Source root and an exact commit Object ID.
This is not a Sandbox: trusted Git metadata must not be concurrently modified
by another process with the same OS privileges (Sandbox work is step 22).
"""

from dataclasses import dataclass, field
import hashlib
import io
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import tarfile
import time
import unicodedata
from typing import Literal

from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.core.source_credentials import SourceCredentialError, validate_source_bytes
from orchestrator.workspaces.policy import WorkspaceAccessError, relative_parts


@dataclass(frozen=True)
class GitSnapshotLimits:
    max_files: int = 1000
    max_file_bytes: int = 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_archive_bytes: int = 20 * 1024 * 1024
    timeout_seconds: float = 30

    def __post_init__(self):
        for value in (self.max_files, self.max_file_bytes, self.max_total_bytes, self.max_archive_bytes):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)) or not 0 < self.timeout_seconds <= 300:
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)


@dataclass(frozen=True)
class GitSnapshot:
    commit_hash: str
    tree_hash: str
    git_object_format: str
    archive: bytes = field(repr=False)
    snapshot_sha256: str
    dependency_lock_hash: str


MAX_GIT_EXPORT_BYTES = 32 * 1024 * 1024
MAX_GIT_EXPORT_OBJECTS = 17_002


@dataclass(frozen=True)
class GitExportObject:
    """One exact immutable Git object, excluding its loose-object wrapper."""

    oid: str
    kind: Literal["commit", "tree", "blob"]
    content: bytes = field(repr=False)

    def __post_init__(self):
        _validate_export_object(self)


@dataclass(frozen=True)
class GitObjectExport:
    """Only the chosen commit/current tree graph, not history or Git metadata."""

    snapshot: GitSnapshot = field(repr=False)
    objects: tuple[GitExportObject, ...] = field(repr=False)

    def __post_init__(self):
        validate_git_export(self)


class _ObjectCollection:
    def __init__(self):
        self.objects = {}
        self.size_bytes = 0

    def add(self, oid, kind, value):
        previous = self.objects.get(oid)
        if previous is not None:
            if previous.kind != kind or previous.content != value:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
            return
        if (
            len(self.objects) >= MAX_GIT_EXPORT_OBJECTS
            or self.size_bytes + len(value) > MAX_GIT_EXPORT_BYTES
        ):
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        self.objects[oid] = GitExportObject(oid, kind, value)
        self.size_bytes += len(value)


class _BoundedBuffer(io.BytesIO):
    def __init__(self, maximum: int):
        super().__init__()
        self.maximum = maximum

    def write(self, value):
        if self.tell() + len(value) > self.maximum:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        return super().write(value)


class GitSnapshotBuilder:
    """Constructor is inert; build reads only committed objects, not HEAD/files."""

    def __init__(self, source_root: Path, limits: GitSnapshotLimits = GitSnapshotLimits()):
        if not isinstance(source_root, Path) or not isinstance(limits, GitSnapshotLimits):
            raise ArtifactAccessError(ArtifactErrorCode.INVALID)
        self._source_root = source_root
        self.limits = limits

    def __repr__(self):
        return "GitSnapshotBuilder()"

    def build(self, commit_hash: str, lock_path: str) -> GitSnapshot:
        return self._read(commit_hash, lock_path)

    def export(self, commit_hash: str, lock_path: str) -> GitObjectExport:
        """Read verified objects only; no checkout, refs, hooks or copy of .git.

        Original commit bytes retain parent references. The referenced parent
        objects are deliberately not exported; history traversal is unsupported
        in the minimal reconstructed repository. Export is bounded to 32 MiB.
        """
        collected = _ObjectCollection()
        snapshot = self._read(commit_hash, lock_path, collected=collected)
        return GitObjectExport(
            snapshot, tuple(collected.objects[oid] for oid in sorted(collected.objects)),
        )

    def _read(self, commit_hash, lock_path, *, collected=None):
        if not isinstance(commit_hash, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit_hash) is None:
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        _validate_source_path(lock_path)
        deadline = time.monotonic() + self.limits.timeout_seconds
        try:
            return self._build(commit_hash, lock_path, deadline, collected=collected)
        except ArtifactAccessError:
            raise
        except (OSError, ValueError, UnicodeError, tarfile.TarError):
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID) from None

    def _build(self, commit_hash, lock_path, deadline, *, collected=None):
        source = self._source_root
        if not source.is_absolute() or not stat.S_ISDIR(source.lstat().st_mode):
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        git_dir = source / ".git"
        self._check_metadata(git_dir, deadline)
        executable = shutil.which("git", path="/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin")
        if executable is None:
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        executable = str(Path(executable).resolve(strict=True))
        env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LANG": "C", "LC_ALL": "C",
            "HOME": "/dev/null", "XDG_CONFIG_HOME": "/dev/null",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_NO_REPLACE_OBJECTS": "1", "GIT_NO_LAZY_FETCH": "1",
            "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0",
        }
        # Parse just this file outside the repository, with includes disabled.
        config = self._run(
            [executable, "config", "--no-includes", "--null", "--file", str(git_dir / "config"), "--list"],
            env, deadline, 1024 * 1024, cwd=Path("/"),
        )
        for record in config.split(b"\0"):
            key = record.partition(b"\n")[0].lower()
            if (
                key == b"include.path" or key.startswith(b"includeif.")
                or key in (b"extensions.partialclone", b"extensions.worktreeconfig")
                or (key.startswith(b"remote.") and key.endswith(b".promisor"))
            ):
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        base = [
            executable, "--no-pager", "--no-optional-locks", "--git-dir", str(git_dir),
            "--work-tree", str(source), "-c", "protocol.allow=never",
            "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
            "-c", "core.attributesFile=/dev/null", "-c", "safe.directory=" + str(source),
        ]
        def git(arguments, cap):
            return self._run(base + arguments, env, deadline, cap, cwd=source)
        object_format = git(["rev-parse", "--show-object-format"], 32).strip().decode("ascii")
        expected_length = {"sha1": 40, "sha256": 64}.get(object_format)
        if expected_length is None or len(commit_hash) != expected_length:
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        def object_bytes(kind, oid, cap):
            if re.fullmatch("[0-9a-f]{" + str(expected_length) + "}", oid) is None:
                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
            size = git(["cat-file", "-s", oid], 32).strip()
            if re.fullmatch(rb"[0-9]+", size) is None:
                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
            if int(size) > cap:
                raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
            value = git(["cat-file", kind, oid], cap)
            if len(value) != int(size) or _object_hash(object_format, kind, value) != oid:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
            if collected is not None:
                collected.add(oid, kind, value)
            return value
        commit = object_bytes("commit", commit_hash, 1024 * 1024)
        first_line = commit.partition(b"\n")[0]
        if not first_line.startswith(b"tree "):
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        tree_hash = first_line[5:].decode("ascii")
        root_tree = object_bytes("tree", tree_hash, self.limits.max_file_bytes)
        _validate_tree(root_tree, object_format)
        # -z returns actual file bytes, not quoted names. -t includes nested trees.
        listing = git(["ls-tree", "-r", "-t", "-z", "--full-tree", commit_hash], min(32 * 1024 * 1024, self.limits.max_files * 8192))
        entries = []
        normalized = set()
        files = 0
        for line in listing.split(b"\0"):
            if not line:
                continue
            header, separator, raw_path = line.partition(b"\t")
            if not separator:
                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
            fields = header.split(b" ")
            if len(fields) != 3:
                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
            mode, kind, raw_oid = fields
            path = raw_path.decode("utf-8", errors="strict")
            _validate_source_path(path)
            canonical = unicodedata.normalize("NFKC", path).casefold()
            if canonical in normalized:
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
            normalized.add(canonical)
            if mode == b"040000" and kind == b"tree":
                raw = object_bytes("tree", raw_oid.decode("ascii"), self.limits.max_file_bytes)
                _validate_tree(raw, object_format)
                if len(normalized) > self.limits.max_files * 16 + 1:
                    raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
                continue
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
            files += 1
            if files > self.limits.max_files:
                raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
            entries.append((path, mode, raw_oid.decode("ascii")))
        if not any(path == lock_path for path, _, _ in entries):
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        output = _BoundedBuffer(self.limits.max_archive_bytes)
        total = 0
        lock_hash = None
        with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path, mode, oid in sorted(entries, key=lambda entry: entry[0].encode("utf-8")):
                value = object_bytes("blob", oid, self.limits.max_file_bytes)
                _validate_source_content(value)
                total += len(value)
                if total > self.limits.max_total_bytes:
                    raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
                if path == lock_path:
                    lock_hash = "sha256:" + hashlib.sha256(value).hexdigest()
                member = tarfile.TarInfo(path)
                member.size = len(value)
                member.mode = 0o755 if mode == b"100755" else 0o644
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = ""
                archive.addfile(member, io.BytesIO(value))
        value = output.getvalue()
        archive_hash = hashlib.sha256(value).hexdigest()
        if time.monotonic() >= deadline:
            raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
        return GitSnapshot(commit_hash, tree_hash, object_format, value, archive_hash, lock_hash)

    def _check_metadata(self, git_dir, deadline):
        if not stat.S_ISDIR(git_dir.lstat().st_mode):
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        denied = {"commondir", "gitdir", "worktrees", "shallow"}
        count = 0
        stack = [git_dir]
        while stack:
            directory = stack.pop()
            if time.monotonic() >= deadline:
                raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
                    count += 1
                    if count > 100_000:
                        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
                    relative = Path(entry.path).relative_to(git_dir)
                    metadata = entry.stat(follow_symlinks=False)
                    if relative.parts[0] in denied or relative.as_posix() in ("objects/info/alternates", "objects/info/http-alternates") or entry.name.endswith(".promisor"):
                        raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
                    if relative.parts[0] == "objects" and len(relative.parts) >= 2:
                        segment = relative.parts[1]
                        if segment not in ("info", "pack"):
                            if re.fullmatch(r"[0-9a-f]{2}", segment) is None:
                                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
                            if len(relative.parts) == 2 and not stat.S_ISDIR(metadata.st_mode):
                                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
                            if len(relative.parts) > 2 and (
                                len(relative.parts) != 3 or not stat.S_ISREG(metadata.st_mode)
                                or re.fullmatch(r"(?:[0-9a-f]{38}|[0-9a-f]{62})", relative.parts[2]) is None
                            ):
                                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
                    if relative.parts[0] == "refs" and not _valid_ref_path(relative.parts):
                        raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
                    if stat.S_ISDIR(metadata.st_mode):
                        stack.append(Path(entry.path))
                    elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                        raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        for required in ("HEAD", "config", "objects", "refs"):
            item = git_dir / required
            metadata = item.lstat()
            desired = stat.S_ISDIR if required in ("objects", "refs") else stat.S_ISREG
            if not desired(metadata.st_mode):
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)

    def _run(self, argv, env, deadline, stdout_cap, *, cwd):
        if time.monotonic() >= deadline:
            raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
        process = None
        try:
            process = subprocess.Popen(argv, cwd=cwd, env=env, shell=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            chunks = {"out": bytearray(), "err": bytearray()}
            with selectors.DefaultSelector() as selector:
                for stream, label in ((process.stdout, "out"), (process.stderr, "err")):
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, label)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
                    for key, _ in selector.select(min(remaining, 0.2)):
                        block = os.read(key.fileobj.fileno(), 64 * 1024)
                        if not block:
                            selector.unregister(key.fileobj)
                            continue
                        chunks[key.data].extend(block)
                        if len(chunks[key.data]) > (stdout_cap if key.data == "out" else 64 * 1024):
                            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT)
            try:
                result = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                raise ArtifactAccessError(ArtifactErrorCode.TIMEOUT) from None
            if result != 0:
                raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
            return bytes(chunks["out"])
        except OSError:
            raise ArtifactAccessError(ArtifactErrorCode.IO) from None
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
                process.stderr.close()


def _validate_source_path(path):
    try:
        relative_parts("source/" + path if isinstance(path, str) else path)
    except WorkspaceAccessError:
        raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED) from None
    # Normalize for portable extraction safety, not for renaming Source files.
    normalized = unicodedata.normalize("NFKC", path)
    if normalized != path:
        try:
            relative_parts("source/" + normalized)
        except WorkspaceAccessError:
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED) from None


def _validate_source_content(content):
    try:
        validate_source_bytes(content)
    except SourceCredentialError:
        raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED) from None


def _object_hash(object_format, kind, value):
    digest = hashlib.new(object_format)
    digest.update(kind.encode("ascii") + b" " + str(len(value)).encode("ascii") + b"\0")
    digest.update(value)
    return digest.hexdigest()


def _validate_tree(value, object_format):
    oid_size = 20 if object_format == "sha1" else 32
    offset = 0
    names = set()
    while offset < len(value):
        end = value.find(b"\0", offset)
        if end < 0 or end + 1 + oid_size > len(value):
            raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
        header = value[offset:end]
        mode, separator, name = header.partition(b" ")
        if not separator or mode not in (b"40000", b"100644", b"100755"):
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        path = name.decode("utf-8", errors="strict")
        if "/" in path or name in names:
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        _validate_source_path(path)
        names.add(name)
        offset = end + 1 + oid_size


def _valid_ref_path(parts):
    # Names are never passed as revisions. Reject malformed administrative
    # ref names nevertheless, without opening their contents or invoking hooks.
    for part in parts:
        if (
            not part or part.startswith(".") or part.endswith((".", ".lock"))
            or ".." in part or "@{" in part
            or any(ord(char) < 33 or ord(char) == 127 or char in "~^:?*[\\" for char in part)
        ):
            return False
    return True


def _validate_export_object(value):
    if (
        type(value) is not GitExportObject
        or type(value.oid) is not str
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value.oid) is None
        or type(value.kind) is not str or value.kind not in ("commit", "tree", "blob")
        or type(value.content) is not bytes
    ):
        raise ArtifactAccessError(ArtifactErrorCode.INVALID)
    if len(value.content) > MAX_GIT_EXPORT_BYTES:
        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    object_format = "sha1" if len(value.oid) == 40 else "sha256"
    if _object_hash(object_format, value.kind, value.content) != value.oid:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
    try:
        if value.kind == "commit":
            _commit_tree(value.content, object_format)
        elif value.kind == "tree":
            _tree_members(value.content, object_format)
    except ArtifactAccessError:
        raise
    except (ValueError, UnicodeError):
        raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID) from None


def _commit_tree(commit, object_format):
    first = commit.partition(b"\n")[0]
    width = 40 if object_format == "sha1" else 64
    if re.fullmatch(rb"tree [0-9a-f]{" + str(width).encode("ascii") + rb"}", first) is None:
        raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
    return first[5:].decode("ascii")


def _tree_members(content, object_format):
    _validate_tree(content, object_format)
    width = 20 if object_format == "sha1" else 32
    offset = 0
    entries = []
    while offset < len(content):
        end = content.index(b"\0", offset)
        mode, name = content[offset:end].split(b" ", 1)
        oid = content[end + 1:end + 1 + width].hex()
        entries.append((mode, name, oid))
        offset = end + 1 + width
    # Native trees sort a directory name as if its trailing character were '/'
    # and an ordinary filename as if it were NUL (Git's base_name_compare).
    sort_key = lambda item: item[1] + (b"/" if item[0] == b"40000" else b"\0")
    if entries != sorted(entries, key=sort_key):
        raise ArtifactAccessError(ArtifactErrorCode.GIT_INVALID)
    return entries


def _validate_git_export(export):
    if (
        type(export) is not GitObjectExport or type(export.snapshot) is not GitSnapshot
        or type(export.objects) is not tuple or not export.objects
    ):
        raise ArtifactAccessError(ArtifactErrorCode.INVALID)
    if len(export.objects) > MAX_GIT_EXPORT_OBJECTS:
        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    snapshot = export.snapshot
    limits = GitSnapshotLimits()
    width = {"sha1": 40, "sha256": 64}.get(snapshot.git_object_format)
    if (
        width is None or type(snapshot.git_object_format) is not str
        or type(snapshot.commit_hash) is not str or type(snapshot.tree_hash) is not str
        or re.fullmatch("[0-9a-f]{" + str(width) + "}", snapshot.commit_hash) is None
        or re.fullmatch("[0-9a-f]{" + str(width) + "}", snapshot.tree_hash) is None
        or type(snapshot.archive) is not bytes
        or type(snapshot.snapshot_sha256) is not str
        or re.fullmatch(r"[0-9a-f]{64}", snapshot.snapshot_sha256) is None
        or type(snapshot.dependency_lock_hash) is not str
        or re.fullmatch(r"sha256:[0-9a-f]{64}", snapshot.dependency_lock_hash) is None
    ):
        raise ArtifactAccessError(ArtifactErrorCode.INVALID)
    if len(snapshot.archive) > limits.max_archive_bytes:
        raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    if hashlib.sha256(snapshot.archive).hexdigest() != snapshot.snapshot_sha256:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
    objects = {}
    total = 0
    for item in export.objects:
        _validate_export_object(item)
        if len(item.oid) != width or item.oid in objects:
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        objects[item.oid] = item
        total += len(item.content)
        if total > MAX_GIT_EXPORT_BYTES:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
    commit = objects.get(snapshot.commit_hash)
    if commit is None or commit.kind != "commit" or len(commit.content) > 1024 * 1024:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
    if _commit_tree(commit.content, snapshot.git_object_format) != snapshot.tree_hash:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
    reachable = {commit.oid}
    normalized = set()
    files = []
    file_bytes = 0
    # Walking paths (not only OIDs) also validates duplicated subtrees mounted
    # at different prefixes and their total Snapshot size/count.
    stack = [("", snapshot.tree_hash, ())]
    while stack:
        prefix, oid, ancestors = stack.pop()
        if oid in ancestors or len(ancestors) >= 128:
            raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
        tree = objects.get(oid)
        if tree is None or tree.kind != "tree":
            raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
        if len(tree.content) > limits.max_file_bytes:
            raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
        reachable.add(oid)
        for mode, name, child_oid in _tree_members(tree.content, snapshot.git_object_format):
            filename = name.decode("utf-8", errors="strict")
            path = prefix + filename
            _validate_source_path(path)
            parts = relative_parts(path)
            portable = unicodedata.normalize("NFKC", path)
            if len(parts) > 128 or len(relative_parts(portable)) != len(parts):
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
            canonical = portable.casefold()
            if canonical in normalized:
                raise ArtifactAccessError(ArtifactErrorCode.PATH_DENIED)
            normalized.add(canonical)
            if len(normalized) > limits.max_files * 16 + 1:
                raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
            child = objects.get(child_oid)
            if child is None:
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
            reachable.add(child_oid)
            if mode == b"40000":
                if child.kind != "tree":
                    raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
                stack.append((path + "/", child_oid, ancestors + (oid,)))
                continue
            if child.kind != "blob":
                raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
            file_bytes += len(child.content)
            if (
                len(files) >= limits.max_files or len(child.content) > limits.max_file_bytes
                or file_bytes > limits.max_total_bytes
            ):
                raise ArtifactAccessError(ArtifactErrorCode.TOO_LARGE)
            _validate_source_content(child.content)
            files.append((path, child.content, 0o755 if mode == b"100755" else 0o644))
    # Parents, history, unrelated objects and tag objects cannot piggyback on a
    # trusted starting Source. Only this commit's exact current graph is kept.
    if reachable != set(objects) or not files:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)
    output = _BoundedBuffer(limits.max_archive_bytes)
    lock_found = False
    with tarfile.open(fileobj=output, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for path, content, mode in sorted(files, key=lambda item: item[0].encode("utf-8")):
            member = tarfile.TarInfo(path)
            member.size, member.mode = len(content), mode
            member.uid = member.gid = member.mtime = 0
            member.uname = member.gname = ""
            archive.addfile(member, io.BytesIO(content))
            if "sha256:" + hashlib.sha256(content).hexdigest() == snapshot.dependency_lock_hash:
                lock_found = True
    if output.getvalue() != snapshot.archive or not lock_found:
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY)


def validate_git_export(export: GitObjectExport) -> None:
    """Pure, bounded validation without Git, filesystem, URL or extraction.

    Rechecks immutable object hashes, exact current graph and canonical Source
    Archive. The caller knows the configured lock path: validate its exact file
    hash there and rebuild with that path after importing. Here the Lock hash
    must match a real blob in the verified current Snapshot, not guessed data.
    """
    try:
        _validate_git_export(export)
    except ArtifactAccessError:
        raise
    except (AttributeError, OSError, ValueError, TypeError, UnicodeError, tarfile.TarError, WorkspaceAccessError):
        raise ArtifactAccessError(ArtifactErrorCode.INTEGRITY) from None
