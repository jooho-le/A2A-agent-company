"""Explicit Host preparation of a registered Run's approved starting Source.

Decoding/constructing is inert. Verification reads only committed Git objects;
preparation writes only a registered, empty Developer Source and private receipt.
Never clone, checkout, install, execute product code, reset an existing Source,
or manufacture the Developer's first candidate/Artifact/verdict here.
"""

from dataclasses import dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import time
import unicodedata
import zlib

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agents.llm.content import sanitize_content
from agents.llm.contracts import parse_json
from agents.platform.configuration import _comparison_path
from agents.platform.tool_configuration import _read_regular
from agents.runtime.developer_workspace import DeveloperCheckpointError, GitDeveloperCheckpoint, _MAX_DEPTH
from mcp_tools.tools.file_io import FileOperationError, _locked_root
from orchestrator.artifacts.contracts import ArtifactAccessError, ArtifactErrorCode
from orchestrator.artifacts.git_snapshot import (
    GitObjectExport, GitSnapshotBuilder, GitSnapshotLimits, validate_git_export,
)
from orchestrator.domain.run_configuration import ExecutionBaseline
from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole, WorkflowStatus, WorkflowStepStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.materialization import _archive_entries, _path
from orchestrator.workspaces.filesystem import open_absolute_directory, open_regular_file, walk_directory
from orchestrator.workspaces.policy import WorkspaceAccessError, WorkspaceErrorCode, workspace_uuid
from orchestrator.workspaces.registry import WorkspaceRegistry


MAX_CONFIGURATION_BYTES = 65_536
_MARKER = ".source-preparation.json"
_MARKER_NEW = ".source-preparation.done"
_CODES = frozenset({
    "SOURCE_CONFIGURATION_INVALID", "SOURCE_CONFIGURATION_FILE_INVALID", "SOURCE_BASELINE_MISMATCH",
    "SOURCE_PREPARATION_CONFLICT", "SOURCE_PREPARATION_FAILED", "SOURCE_PREPARATION_TIMEOUT",
})


class SourcePreparationError(ValueError):
    def __init__(self, code="SOURCE_CONFIGURATION_INVALID"):
        self.code = code if type(code) is str and code in _CODES else "SOURCE_CONFIGURATION_INVALID"
        super().__init__(self.code)


class SourceConfiguration(BaseModel):
    """Operator-only configuration, not an A2A/MCP/Run wire schema."""

    model_config = ConfigDict(
        extra="forbid", frozen=True, strict=True, hide_input_in_errors=True,
        validate_default=True, validate_by_alias=True, validate_by_name=False,
    )
    schema_version: int = Field(alias="schemaVersion")
    repository_path: str = Field(alias="repositoryPath", min_length=1, max_length=4096)
    repository_id: str = Field(alias="repositoryId", min_length=1, max_length=256)
    commit_hash: str = Field(alias="commitHash", pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    snapshot_sha256: str = Field(alias="snapshotSha256", pattern=r"^[0-9a-f]{64}$")
    lock_path: str = Field(alias="lockPath", min_length=1, max_length=4096)
    git_timeout_seconds: float = Field(alias="gitTimeoutSeconds", gt=0, le=300, allow_inf_nan=False)

    @field_validator("schema_version")
    @classmethod
    def exact_version(cls, value):
        if value != 1:
            raise ValueError("Unsupported source configuration")
        return value

    @field_validator("repository_path")
    @classmethod
    def trusted_host_path(cls, value):
        if (not value.strip() or value in {".", ":memory:"} or value.startswith("~")
                or "$" in value or "://" in value
                or any(unicodedata.category(char) == "Cc" for char in value)):
            raise ValueError("Explicit Host repository required")
        return value

    @field_validator("repository_id")
    @classmethod
    def safe_identifier(cls, value):
        if (value != value.strip() or len(value.encode("utf-8")) > 256
                or redact_text(value) != value
                or any(unicodedata.category(char) == "Cc" for char in value)):
            raise ValueError("Stable repository identity required")
        sanitize_content({"repositoryId": value}, reject_secrets=True)
        return value

    @field_validator("lock_path")
    @classmethod
    def source_relative_path(cls, value):
        _path(value)
        return value


def decode_source_configuration(text):
    try:
        return SourceConfiguration.model_validate(parse_json(text, max_bytes=MAX_CONFIGURATION_BYTES))
    except Exception:
        raise SourcePreparationError() from None


def load_source_configuration(path):
    try:
        raw = _read_regular(Path(path).absolute(), MAX_CONFIGURATION_BYTES)
        text = raw.decode("utf-8")
    except Exception:
        raise SourcePreparationError("SOURCE_CONFIGURATION_FILE_INVALID") from None
    return decode_source_configuration(text)


def _copy_configuration(configuration):
    if type(configuration) is not SourceConfiguration:
        raise SourcePreparationError()
    return decode_source_configuration(configuration.model_dump_json(by_alias=True, warnings=False))


def _copy_environment(environment):
    if type(environment) is not ExecutionBaseline:
        raise SourcePreparationError()
    result = ExecutionBaseline.model_validate(parse_json(environment.model_dump_json(by_alias=True, warnings=False)))
    if result != environment or result.network_policy != "DENY":
        raise SourcePreparationError()
    return result


@dataclass(frozen=True, kw_only=True, repr=False)
class VerifiedStartingSource:
    configuration: SourceConfiguration = field(repr=False)
    environment: ExecutionBaseline = field(repr=False)
    export: GitObjectExport = field(repr=False)
    reference_repository_path: Path = field(repr=False)

    def __repr__(self):
        return "VerifiedStartingSource()"

    def __post_init__(self):
        try:
            configuration = _copy_configuration(self.configuration)
            environment = _copy_environment(self.environment)
            validate_git_export(self.export)
            snapshot = self.export.snapshot
            files = {path: value for path, value, _mode in _archive_entries(snapshot.archive)}
            # Match the existing Developer checkpoint before any Workspace
            # writes, including portable names and its directory depth limit.
            for path in files:
                parts, normalized = _path(path)
                if (len(parts) - 1 > _MAX_DEPTH or redact_text(path) != path
                        or any(part.startswith(".mcp-write-") for part in normalized)):
                    raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
            if (not isinstance(self.reference_repository_path, Path)
                    or not self.reference_repository_path.is_absolute()
                    or ".." in self.reference_repository_path.parts
                    or snapshot.commit_hash != configuration.commit_hash
                    or snapshot.snapshot_sha256 != configuration.snapshot_sha256
                    or snapshot.dependency_lock_hash != environment.dependency_lock_hash
                    or configuration.lock_path not in files
                    or "sha256:" + hashlib.sha256(files[configuration.lock_path]).hexdigest() != environment.dependency_lock_hash):
                raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
            object.__setattr__(self, "configuration", configuration)
            object.__setattr__(self, "environment", environment)
        except Exception:
            raise SourcePreparationError("SOURCE_BASELINE_MISMATCH") from None


def verify_starting_source(configuration, *, base_directory, environment):
    """Read the exact approved commit, never Working Tree/HEAD or remote refs."""
    try:
        configuration = _copy_configuration(configuration)
        environment = _copy_environment(environment)
        base = Path(base_directory).resolve(strict=True)
        if not base.is_dir():
            raise SourcePreparationError()
        reference = Path(configuration.repository_path)
        if not reference.is_absolute():
            reference = base / reference
        reference = reference.resolve(strict=True)
        with open_absolute_directory(reference):
            pass
        exported = GitSnapshotBuilder(reference, GitSnapshotLimits(
            timeout_seconds=configuration.git_timeout_seconds)).export(configuration.commit_hash, configuration.lock_path)
        return VerifiedStartingSource(configuration=configuration, environment=environment,
            export=exported, reference_repository_path=reference)
    except SourcePreparationError:
        raise
    except ArtifactAccessError as error:
        code = "SOURCE_PREPARATION_TIMEOUT" if error.code is ArtifactErrorCode.TIMEOUT else "SOURCE_BASELINE_MISMATCH"
        raise SourcePreparationError(code) from None
    except Exception:
        raise SourcePreparationError("SOURCE_BASELINE_MISMATCH") from None


def _deadline(value):
    if value is not None and (type(value) not in (int, float) or not math.isfinite(value)):
        raise SourcePreparationError()
    if value is not None and time.monotonic() >= value:
        raise SourcePreparationError("SOURCE_PREPARATION_TIMEOUT")


def _write(root_fd, parts, content, *, mode=0o600, deadline):
    _deadline(deadline)
    with walk_directory(root_fd, parts[:-1], create=True) as parent_fd:
        with open_regular_file(parent_fd, (parts[-1],), os.O_WRONLY | os.O_CREAT | os.O_EXCL, create=True) as descriptor:
            pending = memoryview(content)
            while pending:
                _deadline(deadline)
                count = os.write(descriptor, pending[:65536])
                if count <= 0:
                    raise SourcePreparationError("SOURCE_PREPARATION_FAILED")
                pending = pending[count:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        os.fsync(parent_fd)


def _marker(root_fd):
    try:
        with open_regular_file(root_fd, (_MARKER,), os.O_RDONLY) as descriptor:
            before = os.fstat(descriptor)
            if not 0 < before.st_size <= 4096 or before.st_mode & 0o077:
                raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
            raw = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            if (len(raw) != before.st_size or before.st_size != after.st_size
                    or before.st_mtime_ns != after.st_mtime_ns or before.st_ctime_ns != after.st_ctime_ns):
                raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
            result = parse_json(raw.decode("utf-8"), max_bytes=4096)
            if type(result) is not dict or type(result.get("schemaVersion")) is not int:
                raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
            return result
    except WorkspaceAccessError as error:
        if error.code is WorkspaceErrorCode.FILE_NOT_FOUND:
            return None
        raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT") from None
    except Exception:
        raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT") from None


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class RegisteredSourcePreparer:
    """Host callback for the existing composition seam; construction is inert.

    The existing dispatcher/Host owns cancellation and drains sync workers.
    A STARTED receipt means uncertain partial writes: never replay/adopt it.
    DONE is verified read-only on repeat, not permission to reset later Source.
    """

    def __init__(self, repository, registry, starting_source):
        try:
            if (not isinstance(repository, SQLiteWorkflowRepository) or not isinstance(registry, WorkspaceRegistry)
                    or registry._repository is not repository or type(starting_source) is not VerifiedStartingSource):
                raise SourcePreparationError()
            # Revalidate even an object modified via object.__setattr__ by Host code.
            checked = VerifiedStartingSource(configuration=starting_source.configuration, environment=starting_source.environment,
                export=starting_source.export, reference_repository_path=starting_source.reference_repository_path)
            reference_key = _comparison_path(checked.reference_repository_path)
            workspace_key = _comparison_path(registry.base_path)
            if (reference_key.is_relative_to(workspace_key)
                    or workspace_key.is_relative_to(reference_key)):
                raise SourcePreparationError()
        except Exception:
            raise SourcePreparationError() from None
        self._repository, self._registry, self._starting_source = repository, registry, checked

    def __repr__(self):
        return "RegisteredSourcePreparer()"

    def __call__(self, run_id):
        return self.prepare(run_id)

    def _context(self, run_id, deadline):
        _deadline(deadline)
        run = self._repository.get_run(run_id)
        frozen = self._repository.get_run_configuration(run_id)
        steps = self._repository.list_steps(run_id)
        source = self._starting_source
        if (run is None or frozen is None or run.run_id != run_id or run.status is not WorkflowStatus.PLANNING
                or run.fix_attempt != 0 or run.code_version is not None
                or frozen.run_id != run.run_id or frozen.workspace_id != run.workspace_id or frozen.scenario_id != run.scenario_id
                or frozen.configuration.starting_commit_hash != source.configuration.commit_hash
                or frozen.configuration.starting_snapshot_sha256 != source.configuration.snapshot_sha256
                or frozen.configuration.environment != source.environment
                or len(steps) != 1 or steps[0].agent_role is not AgentRole.PLANNER
                or steps[0].run_id != run_id or steps[0].requirement_ids
                or steps[0].status is not WorkflowStepStatus.RUNNING
                or steps[0].attempt != 0 or steps[0].code_version is not None or steps[0].a2a_task_state is not None
                or steps[0].a2a_task_id is not None or steps[0].agent_context_id is not None
                or steps[0].input_artifact_ids or steps[0].output_artifact_ids or steps[0].a2a_artifact_ids):
            raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
        if (self._repository.list_project_artifacts(run_id)
                or self._repository.list_agent_contexts(run_id)):
            raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
        # An uncertain Send can be recorded before a Task ID arrives. Absence
        # of a remote ID alone must never permit initial Source re-seeding.
        offset = 0
        while True:
            _deadline(deadline)
            events, total = self._repository.list_events(run_id, limit=1000, offset=offset)
            if any(event.event_type == "A2A_MESSAGE_SENT" for event in events):
                raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
            offset += len(events)
            if offset >= total:
                break
            if not events:
                raise SourcePreparationError("SOURCE_BASELINE_MISMATCH")
        self._registry.get_record(run.workspace_id, run_id=run.run_id)
        return run

    def _receipt(self, run, status):
        source = self._starting_source
        return {
            "schemaVersion": 1, "runId": str(run.run_id), "workspaceId": str(run.workspace_id),
            "repositoryId": source.configuration.repository_id, "commitHash": source.configuration.commit_hash,
            "treeHash": source.export.snapshot.tree_hash, "gitObjectFormat": source.export.snapshot.git_object_format,
            "snapshotSha256": source.configuration.snapshot_sha256,
            "dependencyLockHash": source.environment.dependency_lock_hash,
            "environmentSha256": hashlib.sha256(_encode(source.environment.model_dump(mode="json", by_alias=True))).hexdigest(),
            "status": status,
        }

    def _verify_destination(self, workspace, source_fd, deadline):
        source = self._starting_source
        remaining = deadline - time.monotonic()
        _deadline(deadline)
        limits = GitSnapshotLimits(timeout_seconds=min(source.configuration.git_timeout_seconds, remaining))
        destination = Path(workspace.record.root_path) / "source"
        snapshot = GitSnapshotBuilder(destination, limits).build(source.configuration.commit_hash, source.configuration.lock_path)
        if snapshot != source.export.snapshot:
            raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
        checkpoint = GitDeveloperCheckpoint(workspace, baseline_commit_hash=source.configuration.commit_hash,
            lock_path=source.configuration.lock_path, repository_id=source.configuration.repository_id, limits=limits)
        # Already under the same Developer inode lock: do not reacquire flock.
        if checkpoint._inventory(source_fd, deadline) != checkpoint._snapshot_files(snapshot):
            raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")

    def prepare(self, run_id, *, deadline_monotonic=None):
        try:
            run_id = workspace_uuid(run_id)
            source = self._starting_source
            VerifiedStartingSource(configuration=source.configuration, environment=source.environment,
                export=source.export, reference_repository_path=source.reference_repository_path)
            _deadline(deadline_monotonic)
            deadline = time.monotonic() + source.configuration.git_timeout_seconds
            if deadline_monotonic is not None:
                deadline = min(deadline, deadline_monotonic)
            run = self._context(run_id, deadline)
            self._registry.provision(run.workspace_id, run_id=run_id)
            workspace = self._registry.bind(run.workspace_id, run_id=run_id, role=AgentRole.DEVELOPER)
            with _locked_root(workspace, write=True) as root_fd:
                self._context(run_id, deadline)
                with walk_directory(root_fd, ("source",)) as source_fd:
                    receipt = _marker(root_fd)
                    if receipt is not None:
                        if receipt != self._receipt(run, "DONE"):
                            raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
                        self._verify_destination(workspace, source_fd, deadline)
                        return source.configuration
                    with os.scandir(source_fd) as entries:
                        if next(entries, None) is not None:
                            raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
                    try:
                        os.stat(_MARKER_NEW, dir_fd=root_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
                    _write(root_fd, (_MARKER,), _encode(self._receipt(run, "STARTED")), deadline=deadline)
                    for name, content, mode in _archive_entries(source.export.snapshot.archive):
                        _write(source_fd, tuple(name.split("/")), content, mode=mode, deadline=deadline)
                    with walk_directory(source_fd, (".git", "objects"), create=True):
                        pass
                    with walk_directory(source_fd, (".git", "refs"), create=True):
                        pass
                    object_format = source.export.snapshot.git_object_format
                    config = "[core]\n\trepositoryformatversion = " + ("1" if object_format == "sha256" else "0") + "\n\tbare = false\n\tfilemode = true\n"
                    if object_format == "sha256":
                        config += "[extensions]\n\tobjectformat = sha256\n"
                    _write(source_fd, (".git", "config"), config.encode("ascii"), deadline=deadline)
                    _write(source_fd, (".git", "HEAD"), (source.configuration.commit_hash + "\n").encode("ascii"), deadline=deadline)
                    for item in source.export.objects:
                        raw = item.kind.encode("ascii") + b" " + str(len(item.content)).encode("ascii") + b"\0" + item.content
                        _write(source_fd, (".git", "objects", item.oid[:2], item.oid[2:]), zlib.compress(raw), deadline=deadline)
                    self._verify_destination(workspace, source_fd, deadline)
                    self._context(run_id, deadline)
                    if _marker(root_fd) != self._receipt(run, "STARTED"):
                        raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT")
                    _write(root_fd, (_MARKER_NEW,), _encode(self._receipt(run, "DONE")), deadline=deadline)
                    os.replace(_MARKER_NEW, _MARKER, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    os.fsync(root_fd)
                    return source.configuration
        except SourcePreparationError:
            raise
        except ArtifactAccessError as error:
            code = "SOURCE_PREPARATION_TIMEOUT" if error.code is ArtifactErrorCode.TIMEOUT else "SOURCE_PREPARATION_FAILED"
            raise SourcePreparationError(code) from None
        except DeveloperCheckpointError as error:
            code = "SOURCE_PREPARATION_TIMEOUT" if error.code == "DEVELOPER_CHECKPOINT_TIMEOUT" else "SOURCE_PREPARATION_CONFLICT"
            raise SourcePreparationError(code) from None
        except (WorkspaceAccessError, FileOperationError):
            raise SourcePreparationError("SOURCE_PREPARATION_CONFLICT") from None
        except Exception:
            raise SourcePreparationError("SOURCE_PREPARATION_FAILED") from None
