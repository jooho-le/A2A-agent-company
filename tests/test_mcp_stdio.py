"""Strict local framing tests; no product source execution or external MCP."""

import io
import json
import unittest
from unittest.mock import patch

import anyio
import mcp_types as types

from mcp_tools.core.policy import MCP_PROTOCOL_VERSION
from mcp_tools.stdio import _Input, _Output, StdioTransportError


def request(method="tools/list", params=None, **extra):
    selected_params = {"_meta": {
        types.PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION,
        types.CLIENT_CAPABILITIES_META_KEY: {},
    }, **params} if isinstance(params, dict) else params
    return {"jsonrpc": "2.0", "id": 1, "method": method,
            "params": selected_params if params is not None else {"_meta": {
                types.PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION,
                types.CLIENT_CAPABILITIES_META_KEY: {},
            }}, **extra}


def wire(value):
    return (json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n").encode()


class BoundedStdioTests(unittest.IsolatedAsyncioTestCase):
    async def read(self, raw, *, limit=None):
        sink = io.BytesIO()
        output = _Output(anyio.wrap_file(sink))
        source = _Input(anyio.wrap_file(io.BytesIO(raw)), output)
        accepted = []
        with patch("mcp_tools.stdio.MAX_FRAME_BYTES", limit or 8_454_144):
            async for frame in source:
                accepted.append(json.loads(frame))
        replies = [json.loads(line) for line in sink.getvalue().splitlines()]
        return accepted, replies

    async def test_valid_modern_request_is_forwarded_unchanged(self):
        value = request("tools/call", {"name": "read_project_file", "arguments": {
            "workspaceId": "483bc29d-d832-4939-9a8e-7d0e4e31ef3c", "path": "source/한글.py"}})
        accepted, errors = await self.read(wire(value))
        self.assertEqual(accepted, [value])
        self.assertEqual(errors, [])

    async def test_malformed_json_utf8_duplicates_and_nonfinite_are_safe(self):
        for raw in (b'{"secret":"private-value"\n', b'\xff\n',
                    b'{"jsonrpc":"2.0","jsonrpc":"secret-value"}\n',
                    b'{"number":NaN}\n', b'{"number":1e9999}\n'):
            with self.subTest(raw=raw[:20]):
                accepted, errors = await self.read(raw + wire(request()))
                self.assertEqual(accepted, [request()])
                self.assertEqual(errors[0]["error"], {"code": types.PARSE_ERROR, "message": "Parse error"})
                self.assertNotIn("private-value", str(errors))
                self.assertNotIn("secret-value", str(errors))

    async def test_oversized_frame_is_drained_and_next_frame_survives(self):
        accepted, errors = await self.read(b"x" * 3000 + b"\n" + wire(request()), limit=1024)
        self.assertEqual(accepted, [request()])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["error"]["code"], types.INVALID_REQUEST)

    async def test_unterminated_final_frame_is_not_dispatched(self):
        accepted, errors = await self.read(wire(request()).rstrip(b"\n"))
        self.assertEqual(accepted, [])
        self.assertEqual(errors[0]["error"]["code"], types.INVALID_REQUEST)

    async def test_invalid_envelopes_do_not_retain_submitted_values(self):
        values = ([request()], None, request(jsonrpc="1.0"), request(extra="private-value"),
                  request(id=True), request(id=None), request(id="x" * 257), request(params=[]))
        for value in values:
            with self.subTest(value=type(value).__name__):
                accepted, errors = await self.read(wire(value))
                self.assertEqual(accepted, [])
                self.assertEqual(errors[0]["error"]["code"], types.INVALID_REQUEST)
                self.assertNotIn("private-value", str(errors))

    async def test_metadata_is_required_for_every_request(self):
        value = request()
        value["params"] = {}
        accepted, errors = await self.read(wire(value))
        self.assertEqual(accepted, [])
        self.assertEqual(errors[0]["error"]["code"], types.INVALID_PARAMS)

    async def test_client_capabilities_must_be_object(self):
        value = request()
        value["params"]["_meta"][types.CLIENT_CAPABILITIES_META_KEY] = []
        accepted, errors = await self.read(wire(value))
        self.assertEqual(accepted, [])
        self.assertEqual(errors[0]["error"]["code"], types.INVALID_PARAMS)

    async def test_unsupported_version_does_not_echo_secret(self):
        for version, reflected in (("2025-11-25", "2025-11-25"), ("private-value", "invalid"), (None, "invalid")):
            value = request()
            value["params"]["_meta"][types.PROTOCOL_VERSION_META_KEY] = version
            accepted, errors = await self.read(wire(value))
            self.assertEqual(accepted, [])
            self.assertEqual(errors[0]["error"]["code"], types.UNSUPPORTED_PROTOCOL_VERSION)
            self.assertEqual(errors[0]["error"]["data"], {
                "supported": [MCP_PROTOCOL_VERSION], "requested": reflected})

    async def test_legacy_initialize_and_arbitrary_methods_never_reach_sdk(self):
        for method in ("initialize", "resources/read", "run_shell", "private-value"):
            accepted, errors = await self.read(wire(request(method)))
            self.assertEqual(accepted, [])
            self.assertEqual(errors[0]["error"]["code"], types.METHOD_NOT_FOUND)
            self.assertNotIn("private-value", str(errors))

    async def test_notification_only_valid_cancellation_is_forwarded(self):
        valid = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}}
        ignored = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
        malformed = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": []}}
        accepted, errors = await self.read(wire(ignored) + wire(malformed) + wire(valid))
        self.assertEqual(accepted, [valid])
        self.assertEqual(errors, [])

    async def test_output_handles_short_writes_and_flushes_once(self):
        class ShortWriter:
            def __init__(self):
                self.body, self.flushes = bytearray(), 0

            async def write(self, value):
                chunk = value[:3]
                self.body.extend(chunk)
                return len(chunk)

            async def flush(self):
                self.flushes += 1

        writer = ShortWriter()
        await _Output(writer).write("한글과 JSON\n")
        self.assertEqual(bytes(writer.body), "한글과 JSON\n".encode())
        self.assertEqual(writer.flushes, 1)

    async def test_zero_write_is_transport_error(self):
        class ClosedWriter:
            async def write(self, value):
                return 0

        with self.assertRaisesRegex(StdioTransportError, "^MCP_TRANSPORT_ERROR$"):
            await _Output(ClosedWriter()).write("{}\n")

    async def test_output_size_limit_is_fail_closed(self):
        with patch("mcp_tools.stdio.MAX_FRAME_BYTES", 10):
            with self.assertRaises(StdioTransportError):
                await _Output(anyio.wrap_file(io.BytesIO())).write("x" * 12)


if __name__ == "__main__":
    unittest.main()
