"""Host-bound Tool policy, independent of MCP wire transport and product verdicts.

The catalog declares future Tools; an absent implementation fails explicitly.
Injected handlers are trusted, cancellation-cooperative Host capabilities, not
model-supplied callbacks or a way to execute generated code on the Host.
"""

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import inspect
import math
from types import MappingProxyType
from urllib.parse import urlsplit
from uuid import UUID

import anyio

from agents.llm.content import sanitize_content
from agents.llm.contracts import json_text, parse_json
from mcp_tools.core.catalog import MAX_JSON_BYTES, ToolSchemaError, get_tool_contract
from mcp_tools.core.policy import ROLE_TOOL_NAMES
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.filesystem import BoundWorkspace
from orchestrator.workspaces.policy import (
    WorkspaceAccess, WorkspaceAccessError, WorkspaceErrorCode,
    authorize_path, relative_parts, workspace_uuid,
)


_MAX_JSON_BYTES = MAX_JSON_BYTES


class MCPConfigurationError(ValueError):
    """Invalid trusted configuration, without submitted values or Host paths."""

    def __init__(self):
        super().__init__("MCP_CONFIGURATION_INVALID")


class MCPProtocolError(ValueError):
    """Invalid request/arguments; the SDK adapter emits a JSON-RPC error."""

    code = -32602
    message = "Invalid parameters"

    def __init__(self):
        super().__init__(self.message)


class MCPExecutionError(str, Enum):
    PERMISSION_DENIED = "PERMISSION_DENIED"
    PATH_DENIED = "PATH_DENIED"
    SECRET_DENIED = "SECRET_DENIED"
    WORKSPACE_UNAVAILABLE = "WORKSPACE_UNAVAILABLE"
    TOOL_NOT_IMPLEMENTED = "TOOL_NOT_IMPLEMENTED"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_OUTPUT_INVALID = "TOOL_OUTPUT_INVALID"
    TIMEOUT = "TIMEOUT"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    FILE_ENCODING_ERROR = "FILE_ENCODING_ERROR"
    WRITE_CONFLICT = "WRITE_CONFLICT"
    WRITE_FAILED = "WRITE_FAILED"
    BASE_MISMATCH = "BASE_MISMATCH"
    PATCH_FAILED = "PATCH_FAILED"
    SNAPSHOT_REQUIRED = "SNAPSHOT_REQUIRED"
    SNAPSHOT_INTEGRITY_ERROR = "SNAPSHOT_INTEGRITY_ERROR"
    SANDBOX_ERROR = "SANDBOX_ERROR"
    BUILD_EXECUTION_ERROR = "BUILD_EXECUTION_ERROR"
    TEST_RUNNER_ERROR = "TEST_RUNNER_ERROR"
    BROWSER_START_FAILED = "BROWSER_START_FAILED"
    SCANNER_ERROR = "SCANNER_ERROR"
    PROFILE_NOT_FOUND = "PROFILE_NOT_FOUND"
    REPORT_NOT_FOUND = "REPORT_NOT_FOUND"


class MCPToolExecutionError(RuntimeError):
    """Handler failure containing only an approved, stable execution code."""

    def __init__(self, code):
        try:
            self.code = MCPExecutionError(code)
        except (ValueError, TypeError):
            self.code = MCPExecutionError.TOOL_EXECUTION_FAILED
        super().__init__(self.code.value)


@dataclass(frozen=True, kw_only=True)
class MCPBinding:
    """Selected by the Agent's trusted launcher, never by a Tool argument."""

    role: AgentRole
    agent_role: AgentRole
    run_id: UUID = field(repr=False)
    workspace_id: UUID = field(repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.role, AgentRole)
            or not isinstance(self.agent_role, AgentRole)
            or self.role is not self.agent_role
        ):
            raise MCPConfigurationError()
        try:
            object.__setattr__(self, "run_id", workspace_uuid(self.run_id))
            object.__setattr__(self, "workspace_id", workspace_uuid(self.workspace_id))
        except WorkspaceAccessError:
            raise MCPConfigurationError() from None


@dataclass(frozen=True, kw_only=True)
class MCPExecutionContext:
    binding: MCPBinding
    workspace: BoundWorkspace = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class ToolOutcome:
    """A successful Tool object OR a stable execution error, never a verdict."""

    data: dict | None = field(default=None, repr=False)
    error_code: str | None = None

    def __post_init__(self):
        if (self.data is None) == (self.error_code is None):
            raise MCPConfigurationError()
        if self.data is not None and type(self.data) is not dict:
            raise MCPConfigurationError()
        if self.error_code is not None:
            try:
                MCPExecutionError(self.error_code)
            except (ValueError, TypeError):
                raise MCPConfigurationError() from None


def _failure(code: MCPExecutionError) -> ToolOutcome:
    return ToolOutcome(error_code=code.value)


def _bounded_object(value):
    try:
        if type(value) is not dict:
            raise ValueError
        return parse_json(json_text(value, max_bytes=_MAX_JSON_BYTES), max_bytes=_MAX_JSON_BYTES)
    except Exception:
        raise MCPProtocolError() from None


def _artifact_reference(value):
    """Syntax only; actual report ownership/metadata is checked by future Tools."""
    if not isinstance(value, str) or not 1 <= len(value) <= 4096 or any(ord(c) < 32 for c in value):
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "artifact" or parsed.query or parsed.fragment or not parsed.path.startswith("/"):
            raise ValueError
        workspace_uuid(parsed.netloc)
        relative_parts(parsed.path[1:])
    except (ValueError, WorkspaceAccessError):
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED) from None


async def _finish_handler(task):
    """Reap a cancelled cooperative handler despite repeated caller cancels."""
    # SDK request cancellation uses AnyIO level cancellation. Shield that
    # scope as well as direct asyncio Task cancellation while finally runs.
    with anyio.CancelScope(shield=True):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                # Do not cancel the handler again while its own finally runs.
                continue
            except Exception:
                break
    try:
        task.result()
    except (asyncio.CancelledError, Exception):
        pass


async def _await_handler(invocation, timeout):
    task = asyncio.ensure_future(invocation)
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        task.cancel()
        await _finish_handler(task)
        raise


class MCPDispatcher:
    """Role-filtered discovery plus separately enforced, revalidated dispatch.

    Construction/discovery perform no DB, filesystem, subprocess, or HTTP I/O.
    Registry provisioning is not implicit. Each call obtains a fresh capability.
    Files/Snapshots/Build/Test/Scan operations arrive in steps 24–28.
    """

    def __init__(
        self, binding: MCPBinding, workspace_registry, *,
        handlers: Mapping | None = None, max_call_seconds: float = 60,
    ):
        if not isinstance(binding, MCPBinding) or (
            isinstance(max_call_seconds, bool)
            or not isinstance(max_call_seconds, (float, int))
            or not math.isfinite(max_call_seconds)
            or not 0 < max_call_seconds <= 600
        ):
            raise MCPConfigurationError()
        try:
            if handlers is not None and not isinstance(handlers, Mapping):
                raise ValueError
            copied = dict(handlers or {})
            allowed = ROLE_TOOL_NAMES[binding.role]
            if any(name not in allowed or not callable(handler) for name, handler in copied.items()):
                raise ValueError
        except Exception:
            raise MCPConfigurationError() from None
        self.binding = binding
        self._registry = workspace_registry
        self._handlers = MappingProxyType(copied)
        self._max_call_seconds = float(max_call_seconds)

    def list_tools(self):
        return tuple(get_tool_contract(name) for name in ROLE_TOOL_NAMES[self.binding.role])

    def is_implemented(self, name) -> bool:
        return isinstance(name, str) and name in self._handlers

    async def call_tool(self, name, arguments) -> ToolOutcome:
        contract = get_tool_contract(name)
        if contract is None or name not in ROLE_TOOL_NAMES[self.binding.role]:
            raise MCPProtocolError()
        arguments = _bounded_object(arguments)
        try:
            contract.validate_input(arguments)
            workspace_id = workspace_uuid(arguments["workspaceId"])
        except (ToolSchemaError, WorkspaceAccessError, KeyError):
            raise MCPProtocolError() from None
        if workspace_id != self.binding.workspace_id:
            return _failure(MCPExecutionError.PERMISSION_DENIED)
        # Normalize the known UUID, not arbitrary paths or Source bytes.
        arguments["workspaceId"] = str(workspace_id)
        try:
            if name == "read_project_file":
                authorize_path(self.binding.role, arguments["path"], WorkspaceAccess.READ)
            elif name in ("write_source_file", "write_test_file"):
                authorize_path(self.binding.role, arguments["path"], WorkspaceAccess.WRITE)
            if "reportRef" in arguments:
                _artifact_reference(arguments["reportRef"])
        except WorkspaceAccessError:
            return _failure(MCPExecutionError.PATH_DENIED)
        try:
            # Preserve normal Source code; reject credential literals instead
            # of altering bytes and making the subsequent Hash inaccurate.
            arguments = sanitize_content(
                arguments, source_fields=contract.source_argument_fields,
                reject_secrets=True,
            )
        except Exception:
            return _failure(MCPExecutionError.SECRET_DENIED)
        try:
            record = self._registry.get_record(workspace_id, run_id=self.binding.run_id)
            workspace = self._registry.bind(
                workspace_id, run_id=self.binding.run_id, role=self.binding.role,
            )
            if (
                record.workspace_id != workspace_id or record.run_id != self.binding.run_id
                or not isinstance(workspace, BoundWorkspace)
                or workspace.workspace_id != workspace_id or workspace.run_id != self.binding.run_id
                or workspace.role is not self.binding.role or workspace.record != record
            ):
                return _failure(MCPExecutionError.PERMISSION_DENIED)
        except WorkspaceAccessError as error:
            denied = error.code in (
                WorkspaceErrorCode.IDENTITY, WorkspaceErrorCode.PERMISSION_DENIED,
                WorkspaceErrorCode.ROOT, WorkspaceErrorCode.CONFLICT,
            )
            return _failure(MCPExecutionError.PERMISSION_DENIED if denied else MCPExecutionError.WORKSPACE_UNAVAILABLE)
        except Exception:
            return _failure(MCPExecutionError.WORKSPACE_UNAVAILABLE)
        handler = self._handlers.get(name)
        if handler is None:
            return _failure(MCPExecutionError.TOOL_NOT_IMPLEMENTED)
        context = MCPExecutionContext(binding=self.binding, workspace=workspace)
        try:
            invocation = handler(context, arguments)
            if not inspect.isawaitable(invocation):
                return _failure(MCPExecutionError.TOOL_EXECUTION_FAILED)
            result = await _await_handler(invocation, self._max_call_seconds)
        except asyncio.TimeoutError:
            return _failure(MCPExecutionError.TIMEOUT)
        except asyncio.CancelledError:
            raise
        except MCPToolExecutionError as error:
            return _failure(error.code)
        except Exception:
            return _failure(MCPExecutionError.TOOL_EXECUTION_FAILED)
        try:
            result = _bounded_object(result)
            contract.validate_output(result)
            result = sanitize_content(result, source_fields=contract.source_output_fields)
            contract.validate_output(result)
        except Exception:
            return _failure(MCPExecutionError.TOOL_OUTPUT_INVALID)
        return ToolOutcome(data=result)
