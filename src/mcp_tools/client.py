"""Trusted local stdio launcher and optional, explicitly attached LLM adapter.

Executable, entry module, environment, role, Run and Workspace are Host-owned.
The SDK's high-level auto-negotiation and tools/call retry helpers are bypassed:
each discovery and Tool invocation is a single public ClientSession operation.
This does not attach Tools to the default Agent or decide product verdicts.
"""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from hashlib import sha256
import math
import os
from pathlib import Path
import sys
import time

from mcp import Client
from mcp.client.stdio import DEFAULT_INHERITED_ENV_VARS, StdioServerParameters, stdio_client
from mcp_types import CallToolResult, DiscoverResult, TextContent

from agents.llm.content import sanitize_content
from agents.llm.contracts import JsonSchema, ToolCall, ToolContext, ToolDefinition, json_text, parse_json
from mcp_tools.core.catalog import MAX_JSON_BYTES, get_tool_contract
from mcp_tools.core.policy import MCP_PROTOCOL_VERSION, ROLE_TOOL_NAMES
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import BuildConfiguration, encode_build_configuration
from mcp_tools.tools.browser_config import BrowserTestConfiguration, encode_browser_configuration
from mcp_tools.tools.security_config import SecurityScanConfiguration, encode_security_configuration
from mcp_tools.tools.snapshots import FrozenSourceSelection
from mcp_tools.tools.unit_config import UnitTestConfiguration, encode_unit_configuration
from orchestrator.workspaces.policy import workspace_uuid


_MAX_BYTES = MAX_JSON_BYTES
_BOOTSTRAP = (
    "import runpy,sys;sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('mcp_tools',run_name='__main__')"
)


class MCPClientError(RuntimeError):
    """Stable reason only: no submitted Tool data, SDK text or Host paths."""

    def __init__(self, code="MCP_CLIENT_FAILED"):
        if code not in (
            "MCP_CLIENT_CONFIGURATION_INVALID", "MCP_CLIENT_PROTOCOL_INVALID",
            "MCP_CLIENT_PERMISSION_DENIED", "MCP_CLIENT_ARGUMENTS_INVALID",
            "MCP_CLIENT_OUTPUT_INVALID", "MCP_CLIENT_TOOL_FAILED",
            "MCP_CLIENT_TIMEOUT", "MCP_CLIENT_UNAVAILABLE", "MCP_CLIENT_FAILED",
        ):
            code = "MCP_CLIENT_FAILED"
        self.code = code
        super().__init__(code)


def _host_path(value):
    try:
        path = Path(value)
        text = str(path)
        if (
            not path.is_absolute() or ".." in path.parts
            or any(ord(char) < 32 for char in text) or len(text) > 4096
        ):
            raise ValueError
        return path
    except Exception:
        raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID") from None


@dataclass(frozen=True, kw_only=True)
class MCPChildConfiguration:
    binding: MCPBinding
    database_path: Path = field(repr=False)
    workspace_root: Path = field(repr=False)
    max_call_seconds: float = 60
    frozen_source: FrozenSourceSelection | None = field(default=None, repr=False)
    build_configuration: BuildConfiguration | None = field(default=None, repr=False)
    unit_test_configuration: UnitTestConfiguration | None = field(default=None, repr=False)
    browser_test_configuration: BrowserTestConfiguration | None = field(default=None, repr=False)
    security_scan_configuration: SecurityScanConfiguration | None = field(default=None, repr=False)

    def __post_init__(self):
        if (
            not isinstance(self.binding, MCPBinding)
            or isinstance(self.max_call_seconds, bool)
            or not isinstance(self.max_call_seconds, (float, int))
            or not math.isfinite(self.max_call_seconds)
            or not 0 < self.max_call_seconds <= 600
            or self.frozen_source is not None and not isinstance(self.frozen_source, FrozenSourceSelection)
            or self.build_configuration is not None and not isinstance(self.build_configuration, BuildConfiguration)
            or self.unit_test_configuration is not None and not isinstance(self.unit_test_configuration, UnitTestConfiguration)
            or self.browser_test_configuration is not None and not isinstance(self.browser_test_configuration, BrowserTestConfiguration)
            or self.security_scan_configuration is not None and not isinstance(self.security_scan_configuration, SecurityScanConfiguration)
        ):
            raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID")
        object.__setattr__(self, "database_path", _host_path(self.database_path))
        object.__setattr__(self, "workspace_root", _host_path(self.workspace_root))
        object.__setattr__(self, "max_call_seconds", float(self.max_call_seconds))


def child_parameters(configuration):
    """Deterministic parameters only, with no filesystem/process side effects.

    The official SDK merges selected inherited variables; override every such
    name rather than inheriting HOME, user identity, shell or caller PATH. Its
    stderr is discarded by the transport so raw handler/library errors cannot
    become application logs. Python isolated mode ignores Python environment
    variables; only this installed module's trusted Source root is bootstrapped.
    """
    if not isinstance(configuration, MCPChildConfiguration):
        raise MCPClientError("MCP_CLIENT_CONFIGURATION_INVALID")
    binding = configuration.binding
    source_root = Path(__file__).absolute().parent.parent
    environment = {name: "" for name in DEFAULT_INHERITED_ENV_VARS}
    environment.update({
        "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "HOME": "/nonexistent", "SHELL": "/bin/false", "TERM": "dumb",
        "USER": "a2a-mcp", "LOGNAME": "a2a-mcp",
    })
    arguments = [
        "-I", "-c", _BOOTSTRAP, str(source_root),
        "--role", binding.role.value, "--agent-role", binding.agent_role.value,
        "--run-id", str(binding.run_id), "--workspace-id", str(binding.workspace_id),
        "--database-path", str(configuration.database_path),
        "--workspace-root", str(configuration.workspace_root),
        "--max-call-seconds", str(configuration.max_call_seconds),
    ]
    if configuration.frozen_source is not None:
        arguments.extend([
            "--source-artifact-id", str(configuration.frozen_source.project_artifact_id),
            "--source-snapshot-sha256", configuration.frozen_source.snapshot_sha256,
        ])
    if configuration.build_configuration is not None:
        arguments.extend([
            "--build-configuration-json", encode_build_configuration(configuration.build_configuration),
        ])
    if configuration.unit_test_configuration is not None:
        arguments.extend([
            "--unit-test-configuration-json", encode_unit_configuration(configuration.unit_test_configuration),
        ])
    if configuration.browser_test_configuration is not None:
        arguments.extend([
            "--browser-test-configuration-json", encode_browser_configuration(configuration.browser_test_configuration),
        ])
    if configuration.security_scan_configuration is not None:
        arguments.extend([
            "--security-scan-configuration-json", encode_security_configuration(configuration.security_scan_configuration),
        ])
    return StdioServerParameters(
        command=sys.executable,
        args=arguments,
        env=environment, cwd=str(source_root),
    )


@asynccontextmanager
async def _stdio_transport(parameters):
    with open(os.devnull, "w", encoding="utf-8") as error_sink:
        async with stdio_client(parameters, errlog=error_sink) as streams:
            yield streams


def _definition(contract):
    return ToolDefinition(
        name=contract.name, description=contract.description,
        input_schema=JsonSchema(contract.input_schema_json),
        output_schema=JsonSchema(contract.output_schema_json),
        source_argument_fields=contract.source_argument_fields,
        source_output_fields=contract.source_output_fields,
    )


@dataclass
class _SessionState:
    active: bool = True


@dataclass(frozen=True, kw_only=True)
class BoundMCPClient:
    configuration: MCPChildConfiguration
    _client: object = field(repr=False)
    _state: _SessionState = field(default_factory=_SessionState, repr=False)

    def _check_open(self):
        if not self._state.active:
            raise MCPClientError("MCP_CLIENT_UNAVAILABLE")

    def list_tools(self) -> tuple[ToolDefinition, ...]:
        """Local trusted contracts, never remote instructions/role annotations."""
        self._check_open()
        return tuple(
            _definition(get_tool_contract(name))
            for name in ROLE_TOOL_NAMES[self.configuration.binding.role]
        )

    async def _verify_peer(self):
        timeout = self.configuration.max_call_seconds
        deadline = time.monotonic() + timeout
        try:
            # discover() caches the Client's synthetic adopt result, and may
            # downgrade/retry. send_discover() performs exactly one probe.
            raw = await asyncio.wait_for(
                self._client.session.send_discover(MCP_PROTOCOL_VERSION), timeout,
            )
            raw = parse_json(json_text(raw, max_bytes=_MAX_BYTES), max_bytes=_MAX_BYTES)
            discovered = DiscoverResult.model_validate(raw)
            if (
                discovered.result_type != "complete"
                or MCP_PROTOCOL_VERSION not in discovered.supported_versions
            ):
                raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID")
            self._client.session.adopt(discovered)
            if self._client.protocol_version != MCP_PROTOCOL_VERSION:
                raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID")
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                raise MCPClientError("MCP_CLIENT_TIMEOUT")
            listing = await asyncio.wait_for(self._client.session.list_tools(), timeout)
            encoded = listing.model_dump(mode="json", by_alias=True, exclude_none=True)
            json_text(encoded, max_bytes=_MAX_BYTES)
            expected = ROLE_TOOL_NAMES[self.configuration.binding.role]
            if listing.next_cursor is not None or len(listing.tools) != len(expected):
                raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID")
            if set(tool.name for tool in listing.tools) != set(expected):
                raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID")
            for tool in listing.tools:
                contract = get_tool_contract(tool.name)
                if (
                    tool.input_schema != contract.input_schema
                    or tool.output_schema != contract.output_schema
                ):
                    raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID")
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise MCPClientError("MCP_CLIENT_TIMEOUT") from None
        except MCPClientError:
            raise
        except Exception:
            raise MCPClientError("MCP_CLIENT_PROTOCOL_INVALID") from None

    async def call_tool(self, name, arguments, *, deadline_monotonic=None) -> dict:
        self._check_open()
        binding = self.configuration.binding
        contract = get_tool_contract(name)
        if contract is None or name not in ROLE_TOOL_NAMES[binding.role]:
            raise MCPClientError("MCP_CLIENT_PERMISSION_DENIED")
        timeout = self.configuration.max_call_seconds
        deadline = time.monotonic() + timeout
        if deadline_monotonic is not None:
            if (
                isinstance(deadline_monotonic, bool)
                or not isinstance(deadline_monotonic, (float, int))
                or not math.isfinite(deadline_monotonic)
            ):
                raise MCPClientError("MCP_CLIENT_ARGUMENTS_INVALID")
            timeout = min(timeout, deadline_monotonic - time.monotonic())
            deadline = min(deadline, deadline_monotonic)
            if timeout <= 0:
                raise MCPClientError("MCP_CLIENT_TIMEOUT")
        try:
            contract.validate_input(arguments)
            arguments = parse_json(json_text(arguments, max_bytes=_MAX_BYTES), max_bytes=_MAX_BYTES)
            if workspace_uuid(arguments["workspaceId"]) != binding.workspace_id:
                raise MCPClientError("MCP_CLIENT_PERMISSION_DENIED")
            arguments["workspaceId"] = str(binding.workspace_id)
            arguments = sanitize_content(
                arguments, source_fields=contract.source_argument_fields, reject_secrets=True,
            )
        except MCPClientError:
            raise
        except Exception:
            raise MCPClientError("MCP_CLIENT_ARGUMENTS_INVALID") from None
        timeout = deadline - time.monotonic()
        if timeout <= 0:
            raise MCPClientError("MCP_CLIENT_TIMEOUT")
        try:
            # Session API deliberately avoids Client.call_tool's automatic
            # HEADER_MISMATCH re-list/resend and input-required loop.
            result = await asyncio.wait_for(
                self._client.session.call_tool(
                    name, arguments, read_timeout_seconds=timeout,
                    allow_input_required=False, allow_claimed=False,
                ), timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise MCPClientError("MCP_CLIENT_TIMEOUT") from None
        except Exception:
            raise MCPClientError("MCP_CLIENT_FAILED") from None
        if not isinstance(result, CallToolResult) or result.result_type != "complete":
            raise MCPClientError("MCP_CLIENT_OUTPUT_INVALID")
        if result.is_error:
            # Never echo the peer's error TextContent, stderr or metadata.
            raise MCPClientError("MCP_CLIENT_TOOL_FAILED")
        try:
            data = result.structured_content
            contract.validate_output(data)
            data = parse_json(json_text(data, max_bytes=_MAX_BYTES), max_bytes=_MAX_BYTES)
            if len(result.content) > 1:
                raise ValueError
            if result.content:
                content = result.content[0]
                if (
                    not isinstance(content, TextContent)
                    or (
                        content.text != "TOOL_COMPLETED"
                        and parse_json(content.text, max_bytes=_MAX_BYTES) != data
                    )
                ):
                    raise ValueError
            data = sanitize_content(
                data, source_fields=contract.source_output_fields, reject_secrets=True,
            )
            if name == "read_project_file":
                content = data["content"].encode("utf-8", errors="strict")
                if (
                    data["path"] != arguments["path"]
                    or data["sha256"] != sha256(content).hexdigest()
                    or data["sizeBytes"] != len(content)
                ):
                    raise ValueError
            return data
        except Exception:
            raise MCPClientError("MCP_CLIENT_OUTPUT_INVALID") from None

    async def execute(self, call: ToolCall, arguments: dict, context: ToolContext) -> object:
        """Explicit LLM ToolExecutor adapter; never enables Agent Tools itself."""
        binding = self.configuration.binding
        try:
            if (
                not isinstance(call, ToolCall) or not isinstance(context, ToolContext)
                or context.role is not binding.agent_role
                or workspace_uuid(context.workspace_id) != binding.workspace_id
                or parse_json(call.arguments_json) != arguments
            ):
                raise ValueError
        except Exception:
            raise MCPClientError("MCP_CLIENT_PERMISSION_DENIED") from None
        return await self.call_tool(
            call.name, arguments, deadline_monotonic=context.deadline_monotonic,
        )


@asynccontextmanager
async def open_mcp_client(configuration: MCPChildConfiguration):
    """Open/close one local child in the same task as the caller's context.

    SDK AnyIO transport shutdown is bounded and shielded. Like the upstream
    SDK, repeated native asyncio Task.cancel() during teardown is not claimed
    to be fully shielded; do not move __aexit__ to another task, since AnyIO
    cancel scopes must exit in the task that entered them.
    """
    parameters = child_parameters(configuration)
    failure = None
    try:
        async with Client(
            _stdio_transport(parameters), mode=MCP_PROTOCOL_VERSION,
            read_timeout_seconds=configuration.max_call_seconds, cache=None,
        ) as sdk_client:
            client = BoundMCPClient(configuration=configuration, _client=sdk_client)
            try:
                await client._verify_peer()
                yield client
            except BaseException as error:
                # Do not throw the caller's exception through SDK TaskGroups:
                # AnyIO would wrap it in a raw ExceptionGroup. Exit the official
                # contexts normally in this same task, then propagate it intact.
                failure = error
            finally:
                client._state.active = False
    except asyncio.CancelledError:
        raise
    except MCPClientError:
        raise
    except (OSError, ImportError):
        raise MCPClientError("MCP_CLIENT_UNAVAILABLE") from None
    except Exception:
        raise MCPClientError("MCP_CLIENT_FAILED") from None
    if failure is not None:
        raise failure
