"""Build a stored Source in the Container Sandbox; never execute on the Host.

Only the trusted launcher supplies the fixed profile. Model arguments select
an already issued Snapshot UUID, not a command, image, endpoint or Host path.
Normal nonzero compilation exits are Tool results, not infrastructure errors.
"""

from dataclasses import replace
from functools import partial
import math
from types import MappingProxyType

from mcp_tools.runtime import MCPConfigurationError, MCPExecutionContext, MCPToolExecutionError
from mcp_tools.tools.build_config import BuildConfiguration
from mcp_tools.tools.build_store import BuildOutputStore, BuildStoreError
from mcp_tools.tools.files import _run_file_operation
from orchestrator.artifacts.contracts import ArtifactAccessError
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.states import AgentRole
from orchestrator.sandbox.contracts import SandboxError, SandboxErrorCode
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.policy import WorkspaceAccessError, workspace_uuid


class BuildTools:
    """Inert Host-owned Build capability, optionally awaiting configuration."""

    def __init__(self, artifact_store, sandbox, output_store, *, configuration=None, max_call_seconds=60):
        if (
            not isinstance(artifact_store, ArtifactStore)
            or not isinstance(sandbox, SandboxRuntime)
            or not isinstance(output_store, BuildOutputStore)
            or configuration is not None and not isinstance(configuration, BuildConfiguration)
            or isinstance(max_call_seconds, bool)
            or not isinstance(max_call_seconds, (int, float))
            or not math.isfinite(max_call_seconds)
            or not 0 < max_call_seconds <= 600
        ):
            raise MCPConfigurationError()
        self._artifacts = artifact_store
        self._sandbox = sandbox
        self._outputs = output_store
        self._configuration = configuration
        self._max_call_seconds = float(max_call_seconds)

    def __repr__(self):
        return "BuildTools()"

    def handlers(self, role):
        if not isinstance(role, AgentRole):
            raise MCPConfigurationError()
        return MappingProxyType({"run_build": self.run_build} if role is AgentRole.DEVELOPER else {})

    def _profile(self):
        if self._configuration is None:
            raise MCPToolExecutionError("SANDBOX_ERROR")
        profile = self._configuration.profile
        # Cleanup inspect + rm are outside SandboxRuntime's execution deadline.
        # Narrow, never expand, the Host limit to reserve their control budgets.
        # This is not a hard wall-clock guarantee for disk/DB/session teardown.
        timeout = min(profile.limits.timeout_seconds,
                      self._max_call_seconds - 2 * profile.limits.control_timeout_seconds - 1)
        if timeout < 0.01:
            raise MCPToolExecutionError("SANDBOX_ERROR")
        return replace(profile, limits=replace(profile.limits, timeout_seconds=timeout))

    @staticmethod
    def _context(context):
        if (
            not isinstance(context, MCPExecutionContext)
            or context.binding.role is not AgentRole.DEVELOPER
            or context.workspace.role is not context.binding.role
            or context.workspace.run_id != context.binding.run_id
            or context.workspace.workspace_id != context.binding.workspace_id
        ):
            raise MCPToolExecutionError("PERMISSION_DENIED")

    async def run_build(self, context, arguments):
        self._context(context)
        profile = self._profile()
        try:
            source_id = workspace_uuid(arguments["snapshotId"])
            source = self._artifacts.bind(context.binding.run_id, role=AgentRole.DEVELOPER).read(source_id).metadata
            if not isinstance(source, CodeSnapshotArtifact) or source.artifact_id != source_id:
                raise MCPToolExecutionError("SANDBOX_ERROR")
            result = await self._sandbox.bind(context.binding.run_id, role=AgentRole.DEVELOPER).run(source_id, profile)
            # Persist only after the Sandbox has completed its owned cleanup.
            # The Store rechecks the active Run/Step/Source inside publication.
            record = await _run_file_operation(partial(
                self._outputs.publish, context.binding, source, result, profile=profile,
            ))
            return record.tool_output()
        except SandboxError as error:
            code = {
                SandboxErrorCode.TIMEOUT: "TIMEOUT",
                SandboxErrorCode.DENIED: "PERMISSION_DENIED",
                SandboxErrorCode.PATH: "PATH_DENIED",
                SandboxErrorCode.EXECUTION: "BUILD_EXECUTION_ERROR",
            }.get(error.code, "SANDBOX_ERROR")
            raise MCPToolExecutionError(code) from None
        except (ArtifactAccessError, WorkspaceAccessError):
            raise MCPToolExecutionError("SANDBOX_ERROR") from None
        except BuildStoreError:
            # A completed command without durably verified receipt is not a
            # successful Tool response; do not retry blindly after storage fail.
            raise MCPToolExecutionError("BUILD_EXECUTION_ERROR") from None
