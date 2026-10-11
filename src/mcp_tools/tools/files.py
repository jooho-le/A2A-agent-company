"""Actual file Tool handlers; no source execution, commit, build or verdict.

Role/Run/Workspace are Host capabilities. A frozen Source selection is never
chosen from a Tool argument. QA/Security Source reads cannot fall back to the
Developer's working copy. Synchronous bounded transactions are drained on
request cancellation rather than left running after the request has ended.
"""

import asyncio
from hashlib import sha256
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

    def __repr__(self):
        return "FileTools()"

    def handlers(self, role: AgentRole):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        handlers = {}
        if role is not AgentRole.PLANNER:
            handlers["read_project_file"] = self.read_project_file
        if role is AgentRole.DEVELOPER:
            handlers.update(write_source_file=self.write_source_file, apply_patch=self.apply_patch)
        elif role is AgentRole.QA:
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
        return {"path": path, "content": content, "sha256": sha256(value).hexdigest(), "sizeBytes": len(value)}

    async def read_project_file(self, context, arguments):
        return await _run_file_operation(self._read, context, arguments["path"])

    def _write(self, context, path, content, expected_sha256, role):
        self._context(context)
        if context.binding.role is not role:
            raise MCPToolExecutionError("PERMISSION_DENIED")
        if role is AgentRole.QA:
            # Test scratch only. QA report bytes are published through the
            # Artifact contract, not overwritten by a test-file Tool.
            parts = relative_parts(path)
            if len(parts) < 4 or parts[:3] != ("outputs", "qa", "tests"):
                raise MCPToolExecutionError("PATH_DENIED")
        return write_working(context.workspace, path, content.encode("utf-8"), expected_sha256)

    async def write_source_file(self, context, arguments):
        return await _run_file_operation(
            self._write, context, arguments["path"], arguments["content"],
            arguments.get("expectedSha256"), AgentRole.DEVELOPER,
        )

    async def write_test_file(self, context, arguments):
        result = await _run_file_operation(
            self._write, context, arguments["path"], arguments["content"], None, AgentRole.QA,
        )
        # Exact existing write_test_file outputSchema excludes sizeBytes.
        return {key: result[key] for key in ("path", "sha256", "changed")}

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
