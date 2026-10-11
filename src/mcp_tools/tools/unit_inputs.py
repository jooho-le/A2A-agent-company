"""Freeze bounded unit-test input bytes without executing product/test code.

QA scratch capture coordinates with cooperating MCP writers using the pinned
Workspace root flock. Every directory and file opens with O_NOFOLLOW, including
parents, and capture rechecks inode metadata and the complete directory list.
This is not an OS-user sandbox or a filesystem transaction against malicious
same-user processes; executable input goes only to the later container Tool.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import errno
import hashlib
import json
import os
import stat
from types import MappingProxyType

from agents.llm.content import sanitize_content
from agents.llm.contracts import LLMRuntimeError
from mcp_tools.runtime import MCPExecutionContext
from mcp_tools.tools.file_io import FileOperationError, _locked_root, _signature
from mcp_tools.tools.unit_config import (
    MAX_UNIT_DIRECTORY_DEPTH, MAX_UNIT_FILE_BYTES, MAX_UNIT_FILES, MAX_UNIT_TOTAL_BYTES,
    UnitTestConfigurationError, UnitTestScope, _copy_scope, _test_path,
)
from orchestrator.workspaces.filesystem import BoundWorkspace, open_regular_file, walk_directory
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, authorize_path,
)


_RUNNER_PATH = "_unit_runner.py"
_READ_CHUNK = 64 * 1024
_CODES = frozenset({
    "PERMISSION_DENIED", "PATH_DENIED", "SECRET_DENIED", "FILE_TOO_LARGE",
    "FILE_ENCODING_ERROR", "WRITE_CONFLICT", "FILE_NOT_FOUND", "TOOL_EXECUTION_FAILED",
})


class UnitTestInputsError(RuntimeError):
    def __init__(self, code):
        self.code = code if isinstance(code, str) and code in _CODES else "TOOL_EXECUTION_FAILED"
        super().__init__(self.code)


def _hash(content):
    return hashlib.sha256(content).hexdigest()


def _files_hash(files):
    entries = [
        {"path": path, "sha256": _hash(content), "sizeBytes": len(content)}
        for path, content in sorted(files.items())
    ]
    return _hash(json.dumps(entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


@dataclass(frozen=True, kw_only=True)
class UnitTestInputs:
    files: Mapping[str, bytes] = field(repr=False)
    inputs_sha256: str
    runner_sha256: str
    test_files_sha256: str

    def __post_init__(self):
        if not isinstance(self.files, Mapping):
            raise UnitTestInputsError("TOOL_EXECUTION_FAILED")
        copied = dict(self.files)
        if _RUNNER_PATH not in copied or not 1 <= len(copied) <= MAX_UNIT_FILES + 1:
            raise UnitTestInputsError("TOOL_EXECUTION_FAILED")
        total = 0
        for path, content in copied.items():
            if path != _RUNNER_PATH:
                try:
                    _test_path(path)
                except UnitTestConfigurationError:
                    raise UnitTestInputsError("PATH_DENIED") from None
            if type(content) is not bytes or len(content) > MAX_UNIT_FILE_BYTES:
                raise UnitTestInputsError("FILE_TOO_LARGE")
            _validate_content(content)
            total += len(content)
        # Match SnapshotMaterializer's complete /inputs tree cap. The trusted
        # runner is an input file too; it does not get a second size budget.
        if total > MAX_UNIT_TOTAL_BYTES:
            raise UnitTestInputsError("FILE_TOO_LARGE")
        tests = {path: value for path, value in copied.items() if path != _RUNNER_PATH}
        if sum(map(len, tests.values())) > MAX_UNIT_TOTAL_BYTES:
            raise UnitTestInputsError("FILE_TOO_LARGE")
        for path in tests:
            parts = path.split("/")
            if any("/".join(parts[:index]) in tests for index in range(1, len(parts))):
                raise UnitTestInputsError("PATH_DENIED")
        expected = (_files_hash(copied), _hash(copied[_RUNNER_PATH]), _files_hash(tests))
        if (self.inputs_sha256, self.runner_sha256, self.test_files_sha256) != expected:
            raise UnitTestInputsError("TOOL_EXECUTION_FAILED")
        object.__setattr__(self, "files", MappingProxyType(dict(sorted(copied.items()))))


def _validate_content(content):
    try:
        text = content.decode("utf-8")
    except UnicodeError:
        raise UnitTestInputsError("FILE_ENCODING_ERROR") from None
    if "\x00" in text:
        raise UnitTestInputsError("FILE_ENCODING_ERROR")
    try:
        sanitize_content({"content": text}, source_fields=("content",), reject_secrets=True)
    except LLMRuntimeError:
        raise UnitTestInputsError("SECRET_DENIED") from None


def _read_file(directory_fd, name):
    with open_regular_file(directory_fd, (name,), os.O_RDONLY) as descriptor:
        before = os.fstat(descriptor)
        if before.st_size > MAX_UNIT_FILE_BYTES:
            raise UnitTestInputsError("FILE_TOO_LARGE")
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(_READ_CHUNK, MAX_UNIT_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_UNIT_FILE_BYTES:
                raise UnitTestInputsError("FILE_TOO_LARGE")
        if _signature(before) != _signature(os.fstat(descriptor)) or size != before.st_size:
            raise UnitTestInputsError("WRITE_CONFLICT")
        # Verify the pinned file is still the leaf of the captured tree. An
        # uncooperating rename cannot silently leave bytes from an old inode.
        if _signature(before) != _signature(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)):
            raise UnitTestInputsError("WRITE_CONFLICT")
        content = b"".join(chunks)
        _validate_content(content)
        return content, _signature(before)


def _capture_qa(workspace):
    captured = {}
    total = 0
    visited = 0
    observations = []
    parts = authorize_path(workspace.role, "outputs/qa/tests", WorkspaceAccess.READ)

    def visit(directory_fd, components):
        nonlocal total, visited
        visited += 1
        if len(components) > MAX_UNIT_DIRECTORY_DEPTH or visited > MAX_UNIT_FILES + 1:
            raise UnitTestInputsError("FILE_TOO_LARGE")
        before = os.fstat(directory_fd)
        names = tuple(sorted(os.listdir(directory_fd)))
        # Empty-directory forests, not only file count, are bounded as well.
        if len(names) > MAX_UNIT_FILES + 1:
            raise UnitTestInputsError("FILE_TOO_LARGE")
        entries = []
        for name in names:
            logical = "/".join(("tests", *components, name))
            try:
                _test_path(logical, directory=True)
                authorize_path(workspace.role, "/".join((*parts, *components, name)), WorkspaceAccess.READ)
            except (WorkspaceAccessError, UnitTestConfigurationError):
                raise UnitTestInputsError("PATH_DENIED") from None
            info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                with walk_directory(directory_fd, (name,)) as child:
                    if _signature(info) != _signature(os.fstat(child)):
                        raise UnitTestInputsError("WRITE_CONFLICT")
                    visit(child, (*components, name))
                entries.append((name, _signature(info)))
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                if len(captured) >= MAX_UNIT_FILES:
                    raise UnitTestInputsError("FILE_TOO_LARGE")
                content, signature = _read_file(directory_fd, name)
                if signature != _signature(info):
                    raise UnitTestInputsError("WRITE_CONFLICT")
                captured[logical] = content
                total += len(content)
                if total > MAX_UNIT_TOTAL_BYTES:
                    raise UnitTestInputsError("FILE_TOO_LARGE")
                entries.append((name, signature))
            else:
                raise UnitTestInputsError("PATH_DENIED")
        if _signature(before) != _signature(os.fstat(directory_fd)) or names != tuple(sorted(os.listdir(directory_fd))):
            raise UnitTestInputsError("WRITE_CONFLICT")
        observations.append((components, _signature(before), names, tuple(entries)))

    with _locked_root(workspace, write=False) as root_fd:
        with walk_directory(root_fd, parts) as tests_fd:
            visit(tests_fd, ())
            for components, directory_signature, names, entries in observations:
                with walk_directory(tests_fd, components) as directory:
                    if (
                        directory_signature != _signature(os.fstat(directory))
                        or names != tuple(sorted(os.listdir(directory)))
                        or any(signature != _signature(os.stat(name, dir_fd=directory, follow_symlinks=False)) for name, signature in entries)
                    ):
                        raise UnitTestInputsError("WRITE_CONFLICT")
        with walk_directory(root_fd, parts) as reopened:
            # observations always contains the root (even an empty tree).
            root_signature = next(signature for components, signature, _, _ in observations if not components)
            if root_signature != _signature(os.fstat(reopened)):
                raise UnitTestInputsError("WRITE_CONFLICT")
    return captured


def prepare_unit_inputs(context: MCPExecutionContext, scope: UnitTestScope, runner_source: bytes) -> UnitTestInputs:
    """Capture only approved inputs; construction/discovery do no I/O."""
    if (
        type(context) is not MCPExecutionContext or not isinstance(context.workspace, BoundWorkspace)
        or context.binding.role is not context.workspace.role
        or context.binding.run_id != context.workspace.run_id
        or context.binding.workspace_id != context.workspace.workspace_id
    ):
        raise UnitTestInputsError("PERMISSION_DENIED")
    try:
        scope = _copy_scope(scope)
    except UnitTestConfigurationError:
        raise UnitTestInputsError("TOOL_EXECUTION_FAILED") from None
    if context.binding.role not in scope.roles:
        raise UnitTestInputsError("PERMISSION_DENIED")
    if type(runner_source) is not bytes or not 1 <= len(runner_source) <= MAX_UNIT_FILE_BYTES:
        raise UnitTestInputsError("TOOL_EXECUTION_FAILED")
    _validate_content(runner_source)
    try:
        if scope.kind == "QA_TESTS":
            tests = _capture_qa(context.workspace)
        elif scope.kind == "PROTECTED":
            tests = {path: content.encode("utf-8") for path, content in scope.protected_files.items()}
        else:
            tests = {}
    except FileOperationError as error:
        raise UnitTestInputsError(error.code) from None
    except WorkspaceAccessError as error:
        code = "FILE_NOT_FOUND" if error.code.value == "FILE_NOT_FOUND" else "PATH_DENIED"
        raise UnitTestInputsError(code) from None
    except OSError as error:
        code = "FILE_NOT_FOUND" if error.errno == errno.ENOENT else "PATH_DENIED"
        raise UnitTestInputsError(code) from None
    files = {_RUNNER_PATH: runner_source, **tests}
    return UnitTestInputs(
        files=files, inputs_sha256=_files_hash(files), runner_sha256=_hash(runner_source),
        test_files_sha256=_files_hash(tests),
    )
