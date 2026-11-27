"""Official MCP v2 adapter for fixed-role project contracts, not Tool bodies."""

import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from mcp_tools.core.policy import MCP_PROTOCOL_VERSION
from mcp_tools.runtime import MCPDispatcher, MCPProtocolError


_METHOD_PARAMS = {
    "server/discover": {"_meta"},
    "tools/list": {"_meta", "cursor"},
    "tools/call": {"_meta", "name", "arguments"},
}


def create_server(dispatcher: MCPDispatcher) -> Server:
    """Pure construction. Role authority is the Host binding, never metadata."""
    if not isinstance(dispatcher, MCPDispatcher):
        raise ValueError("MCP_CONFIGURATION_REQUIRED")

    async def list_tools(ctx, params):
        if params.cursor is not None:
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid parameters")
        return types.ListToolsResult(tools=[types.Tool(
            name=contract.name, description=contract.description,
            input_schema=contract.input_schema, output_schema=contract.output_schema,
            meta={"a2a-agent-company/implemented": dispatcher.is_implemented(contract.name)},
        ) for contract in dispatcher.list_tools()])

    async def call_tool(ctx, params):
        try:
            outcome = await dispatcher.call_tool(params.name, params.arguments)
        except MCPProtocolError:
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid parameters") from None
        if outcome.error_code is not None:
            return types.CallToolResult(
                content=[types.TextContent(type="text", text=outcome.error_code)],
                is_error=True,
            )
        # Do not duplicate a potentially large Source JSON into text as well.
        # The closed outputSchema applies to the structured_content value.
        return types.CallToolResult(
            content=[types.TextContent(type="text", text="TOOL_COMPLETED")],
            structured_content=outcome.data, is_error=False,
        )

    server = Server(
        "a2a-agent-company-tools", version="0.1.0",
        instructions=("Host-bound role and Workspace. Tool contracts without an "
                      "implementation return TOOL_NOT_IMPLEMENTED. No product verdict."),
        on_list_tools=list_tools, on_call_tool=call_tool, on_ping=None,
    )

    async def discover(ctx, params):
        return types.DiscoverResult(
            supported_versions=[MCP_PROTOCOL_VERSION],
            capabilities=server.get_capabilities(protocol_version=MCP_PROTOCOL_VERSION),
            instructions=server.instructions,
        )

    server.add_request_handler("server/discover", types.RequestParams, discover)

    async def boundary(ctx, call_next):
        if ctx.request_id is None:
            # Dispatcher handles cooperative stdio cancellation. There are no
            # project notification handlers, logging, roots, or sampling.
            return None
        params = ctx.params
        if ctx.method not in _METHOD_PARAMS:
            raise MCPError(code=types.METHOD_NOT_FOUND, message="Method not found")
        if not isinstance(params, dict) or set(params) - _METHOD_PARAMS[ctx.method]:
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid parameters")
        meta = params.get("_meta")
        if (
            ctx.protocol_version != MCP_PROTOCOL_VERSION or not isinstance(meta, dict)
            or meta.get(types.PROTOCOL_VERSION_META_KEY) != MCP_PROTOCOL_VERSION
            or not isinstance(meta.get(types.CLIENT_CAPABILITIES_META_KEY), dict)
        ):
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid parameters")
        try:
            return await call_next(ctx)
        except (ValidationError, MCPProtocolError):
            raise MCPError(code=types.INVALID_PARAMS, message="Invalid parameters") from None
        except MCPError as error:
            # Never forward a handler/SDK data field containing Source or paths.
            messages = {
                types.INVALID_PARAMS: "Invalid parameters",
                types.METHOD_NOT_FOUND: "Method not found",
                types.INVALID_REQUEST: "Invalid request",
            }
            raise MCPError(code=error.code if error.code in messages else types.INTERNAL_ERROR,
                           message=messages.get(error.code, "Internal error")) from None
        except Exception:
            raise MCPError(code=types.INTERNAL_ERROR, message="Internal error") from None

    # SDK's default OpenTelemetry middleware can emit submitted tool names,
    # request IDs and exception messages before sanitization. Explicit,
    # sanitized project Trace is a later step, not an implicit SDK export.
    server.middleware[:] = [boundary]
    return server
