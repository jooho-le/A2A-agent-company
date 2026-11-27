"""Actual file Tool handlers; no source execution, commit, build or verdict.

Role/Run/Workspace are Host capabilities. A frozen Source selection is never
chosen from a Tool argument. QA/Security Source reads cannot fall back to the
Developer's working copy. Synchronous bounded transactions are drained on
request cancellation rather than left running after the request has ended.
"""

import asyncio
from collections import OrderedDict
from hashlib import sha256
from threading import Lock
from types import MappingProxyType

from agents.llm.content import sanitize_content
from mcp_tools.runtime import (
    MCPConfigurationError, MCPExecutionContext, MCPToolExecutionError,
    _finish_handler,
)
from mcp_tools.tools.file_io import (
    FileOperationError, apply_changes, read_working, write_working,
)
from mcp_tools.tools.patching import PatchError, apply_file_patch, parse_patch
from mcp_tools.tools.snapshots import FrozenSourceSelection, SnapshotReadError, SnapshotReader
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import WorkspaceAccessError, relative_parts, workspace_uuid


async def _run_file_operation(operation, *arguments):
    # asyncio.to_thread alone loses its awaitable on cancellation while its
    # worker still mutates files. Shield that worker and wait for its bounded
    # transaction/cleanup before propagating cancellation to the MCP request.
    task = asyncio.create_task(asyncio.to_thread(operation, *arguments))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await _finish_handler(task)
        raise
    except (FileOperationError, SnapshotReadError, PatchError) as error:
        raise MCPToolExecutionError(error.code) from None
    except WorkspaceAccessError:
        raise MCPToolExecutionError("WORKSPACE_UNAVAILABLE") from None


class FileTools:
    """Inert Host-owned file handlers for one optional frozen Source identity."""

    def __init__(self, artifact_store: ArtifactStore, *, frozen_source=None):
        if frozen_source is not None and not isinstance(frozen_source, FrozenSourceSelection):
            raise MCPConfigurationError()
        self._snapshots = SnapshotReader(artifact_store)
        self._selection = frozen_source
        # Host-observed reads for this client; never retain source text.
        self._read_versions = OrderedDict()
        self._read_lock = Lock()

    @staticmethod
    def _read_key(context, path):
        return (context.binding.run_id, context.binding.workspace_id, tuple(relative_parts(path)))

    def __repr__(self):
        return "FileTools()"

    def handlers(self, role: AgentRole):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        handlers = {}
        if role is not AgentRole.PLANNER:
            handlers["read_project_file"] = self.read_project_file
        if role is AgentRole.DEVELOPER:
            handlers.update(write_source_file=self.write_source_file, apply_patch=self.apply_patch,
                            edit_source_file=self.edit_source_file)
        elif role in (AgentRole.QA, AgentRole.SECURITY):
            handlers["write_test_file"] = self.write_test_file
        return MappingProxyType(handlers)

    @staticmethod
    def _context(context):
        if (
            not isinstance(context, MCPExecutionContext)
            or context.binding.role is not context.workspace.role
            or context.binding.workspace_id != context.workspace.workspace_id
            or context.binding.run_id != context.workspace.run_id
        ):
            raise MCPToolExecutionError("PERMISSION_DENIED")

    def _read(self, context, path):
        self._context(context)
        try:
            parts = relative_parts(path)
            if parts[0] == "snapshots":
                # Explicit frozen alias; never read a mutable physical
                # snapshots directory or a model-selected arbitrary Artifact.
                if (
                    len(parts) < 4 or parts[2] != "source"
                    or self._selection is None
                    or workspace_uuid(parts[1]) != self._selection.project_artifact_id
                ):
                    raise MCPToolExecutionError("PATH_DENIED")
                value = self._snapshots.read(context.binding, self._selection, "/".join(parts[2:]))
            elif parts[0] == "source" and context.binding.role in (AgentRole.QA, AgentRole.SECURITY):
                value = self._snapshots.read(context.binding, self._selection, path)
            else:
                # A planning/output symlink into source/ would bypass the
                # frozen branch above. Validator scratch reads therefore
                # reject symlink components; Developer keeps normal policy.
                value = read_working(
                    context.workspace, path,
                    allow_symlinks=context.binding.role is AgentRole.DEVELOPER,
                )
            content = value.decode("utf-8", errors="strict")
        except UnicodeError:
            raise MCPToolExecutionError("FILE_ENCODING_ERROR") from None
        except WorkspaceAccessError:
            raise MCPToolExecutionError("PATH_DENIED") from None
        try:
            sanitize_content({"content": content}, source_fields=("content",), reject_secrets=True)
        except Exception:
            raise MCPToolExecutionError("SECRET_DENIED") from None
        digest = sha256(value).hexdigest()
        if context.binding.role is AgentRole.DEVELOPER and parts[0] == 'source':
            self._remember(context, path, digest)
        return {"path": path, "content": content, "sha256": digest, "sizeBytes": len(value)}

    def _remember(self, context, path, digest):
        # The Developer knows these exact bytes: it read them, or the Host applied its own write/edit.
        with self._read_lock:
            key = self._read_key(context, path)
            self._read_versions[key] = digest
            self._read_versions.move_to_end(key)
            while len(self._read_versions) > 1024:
                self._read_versions.popitem(last=False)

    async def read_project_file(self, context, arguments):
        return await _run_file_operation(self._read, context, arguments["path"])

    def _write(self, context, path, content, expected_sha256, role, *, require_absent=False):
        self._context(context)
        if context.binding.role is not role:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        if role in (AgentRole.QA, AgentRole.SECURITY):
            # Test scratch only. Validator report bytes are published through
            # the Artifact contract, not overwritten by a test-file Tool.
            parts = relative_parts(path)
            if len(parts) < 4 or parts[:3] != ("outputs", "qa" if role is AgentRole.QA else "security", "tests"):
                raise MCPToolExecutionError("PATH_DENIED")
        return write_working(context.workspace, path, content.encode("utf-8"), expected_sha256,
                             require_absent=require_absent)

    def _write_source(self, context, path, content, expected_sha256):
        # No hash authorizes creation only. Replacing an existing file always needs its current hash.
        result = self._write(context, path, content, expected_sha256, AgentRole.DEVELOPER,
                             require_absent=expected_sha256 is None)
        # Codex review 2026-11-18: a follow-up edit of the file just written needs no extra read.
        if relative_parts(path)[0] == 'source':
            self._remember(context, path, result['sha256'])
        return result

    async def write_source_file(self, context, arguments):
        return await _run_file_operation(
            self._write_source, context, arguments["path"], arguments["content"],
            arguments.get("expectedSha256"),
        )

    async def write_test_file(self, context, arguments):
        result = await _run_file_operation(
            self._write, context, arguments["path"], arguments["content"], None,
            AgentRole.SECURITY if context.binding.role is AgentRole.SECURITY else AgentRole.QA,
        )
        # Exact existing write_test_file outputSchema excludes sizeBytes.
        return {key: result[key] for key in ("path", "sha256", "changed")}

    def _edit(self, context, arguments):
        self._context(context)
        if context.binding.role is not AgentRole.DEVELOPER:
            raise MCPToolExecutionError('PERMISSION_DENIED')
        if relative_parts(arguments['path'])[0] != 'source':
            raise MCPToolExecutionError('PATH_DENIED')
        current = read_working(context.workspace, arguments['path'])
        old = arguments['oldText'].encode('utf-8')
        with self._read_lock:
            observed = self._read_versions.get(self._read_key(context, arguments['path']))
        if observed is None:
            raise MCPToolExecutionError('EDIT_READ_REQUIRED')
        if observed != arguments['expectedSha256'] or sha256(current).hexdigest() != observed:
            raise MCPToolExecutionError('EDIT_STALE_READ')
        if not old or current.count(old) == 0:
            raise MCPToolExecutionError('EDIT_TARGET_MISSING')
        if current.count(old) != 1:
            raise MCPToolExecutionError('EDIT_TARGET_AMBIGUOUS')
        updated = current.replace(old, arguments['newText'].encode('utf-8'), 1)
        result = self._write(context, arguments['path'], updated.decode('utf-8'),
                             arguments['expectedSha256'], AgentRole.DEVELOPER)
        # Codex review 2026-11-18 #3: after a Host-applied edit the new version is known, so the next
        # edit of the same file with this sha256 is accepted; any other change in between still fails.
        self._remember(context, arguments['path'], result['sha256'])
        return result

    async def edit_source_file(self, context, arguments):
        return await _run_file_operation(self._edit, context, arguments)

    def _patch(self, context, patch_text, base_sha256):
        self._context(context)
        if context.binding.role is not AgentRole.DEVELOPER:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        patches = parse_patch(patch_text)
        base = self._snapshots.read_base(
            context.binding, self._selection, base_sha256, tuple(item.path for item in patches),
        )
        changes, expected = {}, {}
        for item in patches:
            try:
                current = read_working(context.workspace, item.path)
            except FileOperationError as error:
                if error.code != "FILE_NOT_FOUND":
                    raise
                current = None
            # Exact affected-file comparison, not only hunk context matching.
            # Unrelated working-file edits are preserved, not claimed to be
            # equal to the complete frozen Snapshot.
            if current != base[item.path]:
                raise MCPToolExecutionError("BASE_MISMATCH")
            changed = apply_file_patch(item, current)
            if changed is not None:
                try:
                    text = changed.decode("utf-8", errors="strict")
                    sanitize_content({"content": text}, source_fields=("content",), reject_secrets=True)
                except Exception:
                    raise MCPToolExecutionError("SECRET_DENIED") from None
            changes[item.path] = changed
            expected[item.path] = None if current is None else sha256(current).hexdigest()
        # Preflight/compare-and-swap all paths under one cooperative Workspace
        # lock. This is rollback for ordinary errors, not power-loss atomicity.
        return apply_changes(context.workspace, changes, expected)

    async def apply_patch(self, context, arguments):
        return await _run_file_operation(
            self._patch, context, arguments["patch"], arguments["baseSnapshotSha256"],
        )
