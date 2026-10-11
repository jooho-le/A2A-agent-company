"""Bounded strict framing around the official SDK's local stdio transport.

No HTTP surface, session negotiation, arbitrary executable, or project code
execution is provided. Invalid frames are replied to without retaining their
raw bytes in an error/log. Host-selected callbacks remain trusted code.
"""

from contextlib import asynccontextmanager
import json
import os

import anyio
import mcp_types as types
from mcp.server.stdio import stdio_server

from mcp_tools.core.catalog import MAX_JSON_BYTES, _parse_json
from mcp_tools.core.policy import MCP_PROTOCOL_VERSION


MAX_FRAME_BYTES = MAX_JSON_BYTES + 65_536


class StdioTransportError(RuntimeError):
    def __init__(self):
        super().__init__("MCP_TRANSPORT_ERROR")


class _Output:
    def __init__(self, stream):
        self.stream = stream
        self.lock = anyio.Lock()

    async def write(self, text):
        encoded = text.encode("utf-8", errors="strict")
        if len(encoded) > MAX_FRAME_BYTES + 1:
            raise StdioTransportError()
        async with self.lock:
            remaining = memoryview(encoded)
            while remaining:
                written = await self.stream.write(remaining)
                if not isinstance(written, int) or written <= 0:
                    raise StdioTransportError()
                remaining = remaining[written:]
            await self.stream.flush()

    async def flush(self):
        # write is already atomic and flushed while holding the same lock.
        pass

    async def error(self, code, *, request_id=None, data=None):
        body = {"jsonrpc": "2.0", "id": request_id,
                "error": {"code": code, "message": {
                    types.PARSE_ERROR: "Parse error", types.INVALID_REQUEST: "Invalid request",
                    types.INVALID_PARAMS: "Invalid parameters", types.METHOD_NOT_FOUND: "Method not found",
                    types.UNSUPPORTED_PROTOCOL_VERSION: "Unsupported protocol version",
                }.get(code, "Invalid request")}}
        if data is not None:
            body["error"]["data"] = data
        await self.write(json.dumps(body, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


class _Input:
    def __init__(self, stream, output):
        self.stream, self.output = stream, output

    def __aiter__(self):
        return self

    async def _readline(self, size):
        # AsyncFile.readline has no size parameter. Bound the underlying
        # buffered file read in a worker rather than reading an unlimited line.
        return await anyio.to_thread.run_sync(self.stream.wrapped.readline, size)

    async def __anext__(self):
        while True:
            line = await self._readline(MAX_FRAME_BYTES + 1)
            if not line:
                raise StopAsyncIteration
            if len(line) > MAX_FRAME_BYTES or not line.endswith(b"\n"):
                if not line.endswith(b"\n"):
                    while line and not line.endswith(b"\n"):
                        line = await self._readline(65_536)
                await self.output.error(types.INVALID_REQUEST)
                continue
            try:
                # Parse duplicate/nonfinite keys before SDK parsing can erase
                # that ambiguity. The catalog's own argument limit is separate.
                text = line.decode("utf-8", errors="strict")
                if len(text.encode("utf-8")) > MAX_JSON_BYTES:
                    # An envelope may add up to64KiB around the payload.
                    from mcp_tools.core.catalog import _unique_object, _no_constant, _finite_float, _check_json
                    value = json.loads(text, object_pairs_hook=_unique_object,
                                       parse_constant=_no_constant, parse_float=_finite_float)
                    _check_json(value)
                else:
                    value = _parse_json(text)
            except Exception:
                await self.output.error(types.PARSE_ERROR)
                continue
            if not isinstance(value, dict) or value.get("jsonrpc") != "2.0":
                await self.output.error(types.INVALID_REQUEST)
                continue
            request_id = value.get("id")
            valid_id = (type(request_id) is int or
                        type(request_id) is str and 1 <= len(request_id) <= 256)
            notification = "id" not in value
            if (set(value) - {"jsonrpc", "id", "method", "params"}
                or not isinstance(value.get("method"), str)
                or not notification and not valid_id
                or not isinstance(value.get("params"), dict)):
                await self.output.error(types.INVALID_REQUEST, request_id=request_id if valid_id else None)
                continue
            if notification:
                # Cancellation has no per-request envelope requirement. Other
                # client notifications have no project handler and are ignored.
                if value["method"] != "notifications/cancelled":
                    continue
                cancelled_id = value["params"].get("requestId")
                if not (type(cancelled_id) is int or
                        type(cancelled_id) is str and 1 <= len(cancelled_id) <= 256):
                    # SDK's dual-era types also accept legacy absent/null IDs;
                    # our2026 binding permits cancellation of a named request.
                    continue
                try:
                    types.jsonrpc_message_adapter.validate_json(text, by_name=False)
                    types.CancelledNotificationParams.model_validate(value["params"], by_name=False)
                except Exception:
                    continue
                return text
            params = value["params"]
            meta = params.get("_meta")
            if not isinstance(meta, dict):
                await self.output.error(types.INVALID_PARAMS, request_id=request_id)
                continue
            requested = meta.get(types.PROTOCOL_VERSION_META_KEY)
            if requested != MCP_PROTOCOL_VERSION:
                # Only real date versions are safe to echo as version metadata.
                import re
                safe_requested = requested if isinstance(requested, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", requested) else "invalid"
                await self.output.error(types.UNSUPPORTED_PROTOCOL_VERSION, request_id=request_id,
                                        data={"supported": [MCP_PROTOCOL_VERSION], "requested": safe_requested})
                continue
            if not isinstance(meta.get(types.CLIENT_CAPABILITIES_META_KEY), dict):
                await self.output.error(types.INVALID_PARAMS, request_id=request_id)
                continue
            if value["method"] not in {"server/discover", "tools/list", "tools/call"}:
                # Reject legacy initialize before the dual-era SDK router.
                await self.output.error(types.METHOD_NOT_FOUND, request_id=request_id)
                continue
            # Validate the base message now, rather than exposing a raw SDK
            # ValidationError through its default transport exception logging.
            try:
                types.jsonrpc_message_adapter.validate_json(text, by_name=False)
            except Exception:
                await self.output.error(types.INVALID_REQUEST, request_id=request_id)
                continue
            return text


@asynccontextmanager
async def _streams():
    if os.name != "posix":
        raise StdioTransportError()
    saved = []
    files = []
    try:
        # Isolate fd0/1 from trusted handler prints/child stdin. The official
        # SDK accepts these explicit streams and owns MCP framing/dispatch.
        saved.append(os.dup(0))
        saved.append(os.dup(1))
        input_file = os.fdopen(os.dup(saved[0]), "rb", buffering=65_536)
        output_file = os.fdopen(os.dup(saved[1]), "wb", buffering=0)
        files = [input_file, output_file]
        null_fd = os.open(os.devnull, os.O_RDONLY)
        try:
            os.dup2(null_fd, 0)
        finally:
            os.close(null_fd)
        os.dup2(2, 1)
        output = _Output(anyio.wrap_file(output_file))
        async with stdio_server(stdin=_Input(anyio.wrap_file(input_file), output), stdout=output) as streams:
            yield streams
    finally:
        # The CLI process owns this single stdio lifetime. Do not reuse a
        # process with concurrent stdio servers; callbacks must not close fds.
        for index, descriptor in enumerate(saved):
            os.dup2(descriptor, index)
            os.close(descriptor)
        for stream in files:
            stream.close()


async def run_stdio(server):
    async with _streams() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())
