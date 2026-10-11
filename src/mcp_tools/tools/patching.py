"""Pure, bounded unified-diff parsing and exact application to UTF-8 bytes.

There is no filesystem, Git, shell, fuzzy matching, or normalization here.
Only Developer ``source/`` paths are supported. The Host handler owns base
Snapshot identity, current-file comparison, secret inspection and atomic I/O.
"""

from dataclasses import dataclass, field
import re

from mcp_tools.core.catalog import MAX_FILE_BYTES
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, authorize_path,
)


MAX_PATCH_FILES = 64
MAX_PATCH_HUNKS = 4096
MAX_PATCH_LINES = 131_072


class PatchError(ValueError):
    """Stable code only; never retain a submitted diff or an inner error."""

    def __init__(self, code: str = "PATCH_FAILED"):
        if code not in ("PATCH_FAILED", "PATH_DENIED"):
            code = "PATCH_FAILED"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PatchLine:
    kind: str
    content: bytes = field(repr=False)


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[PatchLine, ...] = field(repr=False)


@dataclass(frozen=True)
class FilePatch:
    path: str
    old_path: str | None
    new_path: str | None
    hunks: tuple[PatchHunk, ...] = field(repr=False)


_HUNK = re.compile(rb"@@ -([0-9]{1,7})(?:,([0-9]{1,7}))? \+([0-9]{1,7})(?:,([0-9]{1,7}))? @@(?: [^\r\n]*)?\n")
_INDEX = re.compile(rb"index [0-9a-f]{7,64}\.\.[0-9a-f]{7,64}(?: 100644| 100755)?\n")
_NO_NEWLINE = b"\\ No newline at end of file"


def _fail(code: str = "PATCH_FAILED"):
    raise PatchError(code)


def _lf_lines(data: bytes) -> list[bytes]:
    # bytes.splitlines treats CR/VT/form-feed as separators. A diff must not.
    pieces = data.split(b"\n")
    result = [piece + b"\n" for piece in pieces[:-1]]
    if pieces[-1]:
        result.append(pieces[-1])
    if len(result) > MAX_PATCH_LINES:
        _fail()
    return result


def _path(value: str, prefix: str | None = None) -> str:
    if prefix is not None and value.startswith(prefix):
        value = value[len(prefix):]
    try:
        parts = authorize_path(AgentRole.DEVELOPER, value, WorkspaceAccess.WRITE)
    except WorkspaceAccessError:
        _fail("PATH_DENIED")
    if len(parts) < 2:
        _fail("PATH_DENIED")
    return value


def _header(line: bytes, label: bytes, prefix: str) -> str | None:
    if not line.startswith(label) or not line.endswith(b"\n"):
        _fail()
    raw = line[len(label):-1]
    # Quoted/escaped Git names and timestamp suffixes are not interpreted.
    if not raw or raw.startswith(b'"') or b"\t" in raw or b"\r" in raw:
        _fail()
    value = raw.decode("utf-8", errors="strict")
    if value == "/dev/null":
        return None
    return _path(value, prefix)


def _parse_hunk(lines: list[bytes], index: int) -> tuple[PatchHunk, int]:
    match = _HUNK.fullmatch(lines[index])
    if match is None:
        _fail()
    old_start, old_count, new_start, new_count = (
        int(match[1]), int(match[2]) if match[2] is not None else 1,
        int(match[3]), int(match[4]) if match[4] is not None else 1,
    )
    if (
        max(old_start, old_count, new_start, new_count) > MAX_FILE_BYTES + 1
        or old_count > 0 and old_start == 0
        or new_count > 0 and new_start == 0
        or old_count == new_count == 0
    ):
        _fail()
    index += 1
    body: list[PatchLine] = []
    old_seen = new_seen = 0
    while index < len(lines):
        line = lines[index]
        if line.rstrip(b"\n") == _NO_NEWLINE:
            if not body or not body[-1].content.endswith(b"\n"):
                _fail()
            previous = body[-1]
            content = previous.content[:-1]
            if not content:
                # An empty file has zero lines, not an unterminated empty line.
                _fail()
            body[-1] = PatchLine(previous.kind, content)
            index += 1
            continue
        if old_seen == old_count and new_seen == new_count:
            break
        if not line.endswith(b"\n") or line[:1] not in (b" ", b"-", b"+"):
            _fail()
        kind = line[:1].decode("ascii")
        content = line[1:]
        if b"\x00" in content:
            _fail()
        old_seen += kind in (" ", "-")
        new_seen += kind in (" ", "+")
        if old_seen > old_count or new_seen > new_count:
            _fail()
        body.append(PatchLine(kind, content))
        index += 1
    if old_seen != old_count or new_seen != new_count:
        _fail()
    return PatchHunk(old_start, old_count, new_start, new_count, tuple(body)), index


def _parse(text: str) -> tuple[FilePatch, ...]:
    if type(text) is not str:
        _fail()
    encoded = text.encode("utf-8", errors="strict")
    if not encoded or len(encoded) > MAX_FILE_BYTES:
        _fail()
    lines = _lf_lines(encoded)
    index = 0
    total_hunks = 0
    patches: list[FilePatch] = []
    seen_paths: set[str] = set()
    while index < len(lines):
        if len(patches) >= MAX_PATCH_FILES:
            _fail()
        git_path = None
        mode = None
        if lines[index].startswith(b"diff --git "):
            match = re.fullmatch(rb"diff --git (a/.+) (b/.+)\n", lines[index])
            if match is None or b'"' in lines[index]:
                _fail()
            old_git = _path(match[1].decode("utf-8"), "a/")
            new_git = _path(match[2].decode("utf-8"), "b/")
            if old_git != new_git:
                _fail()  # Renames, including rename-like diff headers.
            git_path = old_git
            index += 1
            if index < len(lines) and lines[index] in (
                b"new file mode 100644\n", b"deleted file mode 100644\n",
            ):
                mode = "add" if lines[index].startswith(b"new") else "delete"
                index += 1
            if index < len(lines) and lines[index].startswith(b"index "):
                if (
                    _INDEX.fullmatch(lines[index]) is None
                    or mode is not None and lines[index].endswith(b" 100755\n")
                ):
                    _fail()
                index += 1
        if index + 1 >= len(lines):
            _fail()
        old_path = _header(lines[index], b"--- ", "a/")
        new_path = _header(lines[index + 1], b"+++ ", "b/")
        index += 2
        if old_path is None and new_path is None:
            _fail()
        if old_path is not None and new_path is not None and old_path != new_path:
            _fail()
        path = new_path if new_path is not None else old_path
        if (
            path in seen_paths or git_path is not None and git_path != path
            or mode == "add" and old_path is not None
            or mode == "delete" and new_path is not None
        ):
            _fail()
        hunks: list[PatchHunk] = []
        while index < len(lines) and lines[index].startswith(b"@@ "):
            hunk, index = _parse_hunk(lines, index)
            total_hunks += 1
            if total_hunks > MAX_PATCH_HUNKS:
                _fail()
            hunks.append(hunk)
        if not hunks or not any(line.kind != " " for hunk in hunks for line in hunk.lines):
            _fail()
        seen_paths.add(path)
        patches.append(FilePatch(path, old_path, new_path, tuple(hunks)))
    return tuple(patches)


def parse_patch(text: str) -> tuple[FilePatch, ...]:
    """Parse one or more source-file diffs without retaining failed input."""
    reason = None
    try:
        return _parse(text)
    except PatchError as error:
        reason = error.code
    except Exception:
        reason = "PATCH_FAILED"
    raise PatchError(reason) from None


def _validate_patch(patch: FilePatch) -> None:
    if type(patch) is not FilePatch or type(patch.path) is not str:
        _fail()
    _path(patch.path)
    if (
        patch.old_path is None and patch.new_path is None
        or patch.old_path not in (None, patch.path)
        or patch.new_path not in (None, patch.path)
        or type(patch.hunks) is not tuple or not patch.hunks
        or len(patch.hunks) > MAX_PATCH_HUNKS
    ):
        _fail()
    total_lines = total_bytes = changes = 0
    for hunk in patch.hunks:
        if type(hunk) is not PatchHunk or type(hunk.lines) is not tuple:
            _fail()
        counts = (hunk.old_start, hunk.old_count, hunk.new_start, hunk.new_count)
        if (
            any(type(value) is not int or not 0 <= value <= MAX_FILE_BYTES + 1 for value in counts)
            or hunk.old_count > 0 and hunk.old_start == 0
            or hunk.new_count > 0 and hunk.new_start == 0
            or hunk.old_count == hunk.new_count == 0
        ):
            _fail()
        old_count = new_count = 0
        for line in hunk.lines:
            if (
                type(line) is not PatchLine or type(line.kind) is not str
                or line.kind not in (" ", "+", "-")
                or type(line.content) is not bytes or not line.content
                or b"\x00" in line.content or b"\n" in line.content[:-1]
            ):
                _fail()
            line.content.decode("utf-8", errors="strict")
            old_count += line.kind in (" ", "-")
            new_count += line.kind in (" ", "+")
            total_lines += 1
            total_bytes += len(line.content) + 1
            changes += line.kind != " "
        if (old_count, new_count) != (hunk.old_count, hunk.new_count):
            _fail()
    if total_lines > MAX_PATCH_LINES or total_bytes > MAX_FILE_BYTES or changes == 0:
        _fail()


def _apply(patch: FilePatch, original: bytes | None) -> bytes | None:
    _validate_patch(patch)
    if patch.old_path is None:
        if original is not None:
            _fail()
        original = b""
    elif type(original) is not bytes:
        _fail()
    if len(original) > MAX_FILE_BYTES or b"\x00" in original:
        _fail()
    original.decode("utf-8", errors="strict")
    old_lines = _lf_lines(original)
    output: list[bytes] = []
    cursor = 0
    output_bytes = 0

    def append(line: bytes):
        nonlocal output_bytes
        if output and not output[-1].endswith(b"\n"):
            _fail()  # A no-newline marker may only describe actual EOF.
        output_bytes += len(line)
        if output_bytes > MAX_FILE_BYTES or len(output) >= MAX_PATCH_LINES:
            _fail()
        output.append(line)

    for hunk in patch.hunks:
        old_index = hunk.old_start - 1 if hunk.old_count else hunk.old_start
        if old_index < cursor or old_index > len(old_lines):
            _fail()
        for line in old_lines[cursor:old_index]:
            append(line)
        cursor = old_index
        new_index = hunk.new_start - 1 if hunk.new_count else hunk.new_start
        if new_index != len(output):
            _fail()
        for line in hunk.lines:
            if line.kind in (" ", "-"):
                if cursor >= len(old_lines) or old_lines[cursor] != line.content:
                    _fail()
                cursor += 1
            if line.kind in (" ", "+"):
                append(line.content)
    for line in old_lines[cursor:]:
        append(line)
    result = b"".join(output)
    if patch.new_path is None:
        if result:
            _fail()
        return None
    return result


def apply_file_patch(patch: FilePatch, original: bytes | None) -> bytes | None:
    """Return exact changed bytes or None for deletion; no matching fallback."""
    reason = None
    try:
        return _apply(patch, original)
    except PatchError as error:
        reason = error.code
    except Exception:
        reason = "PATCH_FAILED"
    raise PatchError(reason) from None
