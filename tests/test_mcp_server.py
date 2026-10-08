"""Official SDK adapter and bounded stdio framing; no product Tool execution."""

import asyncio
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import mcp_types as types
from mcp.shared.exceptions import MCPError

from mcp_tools.client import MCPChildConfiguration, child_parameters
from mcp_tools.core.catalog import get_tool_contract
from mcp_tools.core.policy import MCP_PROTOCOL_VERSION, ROLE_TOOL_NAMES
from mcp_tools.runtime import MCPBinding, MCPDispatcher
from mcp_tools.server import create_server
from mcp_tools.stdio import MAX_FRAME_BYTES, StdioTransportError, _Input, _Output
from orchestrator.domain import AgentRole, SCN_001_ID, WorkflowRun
from orchestrator.domain.workspaces import WorkspaceRecord
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.workspaces.registry import WorkspaceRegistry


def metadata(**extra):
    return {
        types.PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION,
        types.CLIENT_CAPABILITIES_META_KEY: {}, **extra,
    }


def message(request_id=1, method="tools/list", params=None):
    return {
        "jsonrpc": "2.0", "id": request_id, "method": method,
        "params": {"_meta": metadata()} if params is None else params,
    }


class MemoryInput:
    def __init__(self, data):
        self.data = data
        self.wrapped = self

    def readline(self, size):
        if not self.data:
            return b""
        end = self.data.find(b"\n", 0, size)
        end = end + 1 if end >= 0 else min(size, len(self.data))
        value, self.data = self.data[:end], self.data[end:]
        return value


class MemoryOutput:
    def __init__(self):
        self.errors = []

    async def error(self, code, *, request_id=None, data=None):
        self.errors.append({"code": code, "id": request_id, "data": data})


class MemoryWriter:
    def __init__(self):
        self.data = bytearray()
        self.flushes = 0

    async def write(self, value):
        await asyncio.sleep(0)
        self.data.extend(value)
        return len(value)

    async def flush(self):
        self.flushes += 1


class MCPServerConstructionTests(unittest.TestCase):
    def dispatcher(self, role=AgentRole.DEVELOPER):
        return MCPDispatcher(MCPBinding(role=role, agent_role=role, run_id=uuid4(), workspace_id=uuid4()), Mock())

    def test_server_construction_registers_only_discovery_tools_and_no_io(self):
        dispatcher = self.dispatcher()
        server = create_server(dispatcher)
        self.assertEqual(set(server._request_handlers), {"server/discover", "tools/list", "tools/call"})
        self.assertEqual(server._notification_handlers, {})
        self.assertEqual(dispatcher._registry.mock_calls, [])
        capabilities = server.get_capabilities(protocol_version=MCP_PROTOCOL_VERSION)
        self.assertIsNotNone(capabilities.tools)
        for absent in (capabilities.resources, capabilities.prompts, capabilities.logging, capabilities.completions):
            self.assertIsNone(absent)

    def test_server_does_not_install_raw_sdk_telemetry(self):
        # SDK default telemetry can observe an exception before the outer
        # sanitizer, or export untrusted names/IDs. Trace integration is later.
        server = create_server(self.dispatcher())
        self.assertEqual(len(server.middleware), 1)
        self.assertNotEqual(type(server.middleware[0]).__name__, "OpenTelemetryMiddleware")

    def test_server_requires_host_dispatcher(self):
        for invalid in (None, {}, "arbitrary", Mock()):
            with self.subTest(type=type(invalid)), self.assertRaisesRegex(ValueError, "^MCP_CONFIGURATION_REQUIRED$"):
                create_server(invalid)


class _MCPServerFixture:
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix="a2a-mcp-server-", dir="/tmp")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.database = self.directory / "workflow.sqlite3"
        self.base = self.directory / "workspaces"
        self.repository = SQLiteWorkflowRepository(self.database)
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        record = WorkspaceRecord(
            workspace_id=self.run.workspace_id, run_id=self.run.run_id,
            root_path=str(self.base / str(self.run.workspace_id)),
        )
        self.repository.create_run(self.run, (), (), workspace=record)
        self.registry = WorkspaceRegistry(self.repository, self.base)
        self.registry.provision(self.run.workspace_id, run_id=self.run.run_id)

    def server(self, role=AgentRole.DEVELOPER, handlers=None):
        binding = MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id)
        return create_server(MCPDispatcher(binding, self.registry, handlers=handlers))

    def context(self, method="tools/list", params=None, **overrides):
        values = dict(method=method, params={"_meta": metadata()} if params is None else params,
                      protocol_version=MCP_PROTOCOL_VERSION, request_id=1)
        values.update(overrides)
        return SimpleNamespace(**values)

    async def dispatch(self, server, context):
        async def next_handler(ctx):
            entry = server.get_request_handler(ctx.method)
            if entry is None:
                raise MCPError(code=types.METHOD_NOT_FOUND, message="private handler missing")
            params = entry.params_type.model_validate(ctx.params, by_name=False)
            return await entry.handler(ctx, params)
        return await server.middleware[0](context, next_handler)

    def arguments(self, **extra):
        return {"workspaceId": str(self.run.workspace_id), "path": "source/signup.py", **extra}

    async def assert_rpc_error(self, invocation, code, text="Invalid parameters"):
        with self.assertRaises(MCPError) as raised:
            await invocation
        self.assertEqual(raised.exception.error.code, code)
        self.assertEqual(raised.exception.error.message, text)
        self.assertIsNone(raised.exception.error.data)
        self.assertNotIn("private-secret", repr(raised.exception))


class MCPServerAdapterTests(_MCPServerFixture, unittest.IsolatedAsyncioTestCase):
    async def test_discovery_advertises_fixed_protocol_only(self):
        result = await self.dispatch(self.server(), self.context("server/discover"))
        self.assertEqual(result.supported_versions, [MCP_PROTOCOL_VERSION])
        self.assertIsNotNone(result.capabilities.tools)
        self.assertNotIn("private", result.instructions)

    async def test_role_list_schema_is_exact_and_future_implementations_marked_false(self):
        for role in AgentRole:
            with self.subTest(role=role):
                result = await self.dispatch(self.server(role), self.context())
                self.assertEqual(tuple(tool.name for tool in result.tools), ROLE_TOOL_NAMES[role])
                for tool in result.tools:
                    contract = get_tool_contract(tool.name)
                    self.assertEqual(tool.input_schema, contract.input_schema)
                    self.assertEqual(tool.output_schema, contract.output_schema)
                    self.assertIs(tool.meta["a2a-agent-company/implemented"], False)

    async def test_metadata_role_cannot_elevate_qa_discovery(self):
        result = await self.dispatch(self.server(AgentRole.QA), self.context(params={"_meta": metadata(role="DEVELOPER", hostPath="/private-secret")}))
        self.assertNotIn("write_source_file", [tool.name for tool in result.tools])
        self.assertNotIn("private-secret", result.model_dump_json(by_alias=True))

    async def test_authorized_stub_is_execution_error_not_rpc_error_or_success(self):
        params = {"_meta": metadata(), "name": "read_project_file", "arguments": self.arguments()}
        result = await self.dispatch(self.server(), self.context("tools/call", params))
        self.assertIs(result.is_error, True)
        self.assertEqual([item.text for item in result.content], ["TOOL_NOT_IMPLEMENTED"])
        self.assertIsNone(result.structured_content)
        self.assertFalse((self.base / str(self.run.workspace_id) / "source/signup.py").exists())

    async def test_forbidden_qa_direct_write_is_rpc_error(self):
        params = {"_meta": metadata(role="DEVELOPER"), "name": "write_source_file", "arguments": self.arguments(content="# source\n")}
        await self.assert_rpc_error(self.dispatch(self.server(AgentRole.QA), self.context("tools/call", params)), types.INVALID_PARAMS)

    async def test_unknown_tool_and_bad_schema_are_rpc_errors(self):
        for name, arguments in (("arbitrary_shell_private-secret", {}), ("read_project_file", self.arguments(command="private-secret")), ("read_project_file", {"path": "source/signup.py"})):
            with self.subTest(name=name):
                params = {"_meta": metadata(), "name": name, "arguments": arguments}
                await self.assert_rpc_error(self.dispatch(self.server(), self.context("tools/call", params)), types.INVALID_PARAMS)

    async def test_path_denied_and_foreign_workspace_are_execution_errors(self):
        for arguments, expected in ((self.arguments(path="../../private-secret"), "PATH_DENIED"), (self.arguments(workspaceId=str(uuid4())), "PERMISSION_DENIED")):
            with self.subTest(expected=expected):
                params = {"_meta": metadata(), "name": "read_project_file", "arguments": arguments}
                result = await self.dispatch(self.server(), self.context("tools/call", params))
                self.assertTrue(result.is_error)
                self.assertEqual([item.text for item in result.content], [expected])
                self.assertNotIn("private-secret", result.model_dump_json())

    async def test_success_has_structured_source_only_and_small_constant_text(self):
        source = "password = request.password\n# 한글\n"
        async def reader(context, arguments):
            return {"path": arguments["path"], "content": source, "sha256": hashlib.sha256(source.encode()).hexdigest(), "sizeBytes": len(source.encode())}
        server = self.server(handlers={"read_project_file": reader})
        params = {"_meta": metadata(), "name": "read_project_file", "arguments": self.arguments()}
        result = await self.dispatch(server, self.context("tools/call", params))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["content"], source)
        self.assertEqual([item.text for item in result.content], ["TOOL_COMPLETED"])
        listing = await self.dispatch(server, self.context())
        self.assertIs(listing.tools[0].meta["a2a-agent-company/implemented"], True)

    async def test_paginated_cursor_not_supported_and_closed_params(self):
        for params in ({"_meta": metadata(), "cursor": "private-secret"}, {"_meta": metadata(), "role": "QA"}, {"_meta": metadata(), "rootPath": "/private-secret"}):
            with self.subTest(params=params):
                await self.assert_rpc_error(self.dispatch(self.server(), self.context(params=params)), types.INVALID_PARAMS)

    async def test_wrong_version_missing_meta_or_nonobject_capabilities_rejected(self):
        for params in ({}, {"_meta": None}, {"_meta": {types.PROTOCOL_VERSION_META_KEY: "2025-11-25", types.CLIENT_CAPABILITIES_META_KEY: {}}}, {"_meta": {types.PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION}}, {"_meta": {types.PROTOCOL_VERSION_META_KEY: MCP_PROTOCOL_VERSION, types.CLIENT_CAPABILITIES_META_KEY: []}}):
            with self.subTest(params=params):
                await self.assert_rpc_error(self.dispatch(self.server(), self.context(params=params)), types.INVALID_PARAMS)
        await self.assert_rpc_error(self.dispatch(self.server(), self.context(protocol_version="2025-11-25")), types.INVALID_PARAMS)

    async def test_unknown_methods_and_old_initialize_are_not_supported(self):
        for method in ("initialize", "resources/read", "sampling/createMessage", "private-secret-method"):
            with self.subTest(method=method):
                await self.assert_rpc_error(self.dispatch(self.server(), self.context(method)), types.METHOD_NOT_FOUND, "Method not found")

    async def test_boundary_sanitizes_sdk_errors_data_and_unknown_codes(self):
        server = self.server()
        for code, expected, text in ((types.INVALID_PARAMS, types.INVALID_PARAMS, "Invalid parameters"), (types.METHOD_NOT_FOUND, types.METHOD_NOT_FOUND, "Method not found"), (-32055, types.INTERNAL_ERROR, "Internal error")):
            async def malicious_handler(ctx):
                raise MCPError(code=code, message="password=private-secret", data={"path": str(self.base)})
            with self.subTest(code=code):
                await self.assert_rpc_error(server.middleware[0](self.context(), malicious_handler), expected, text)

    async def test_arbitrary_handler_exception_is_generic_internal_error(self):
        async def handler(ctx):
            raise RuntimeError("/private-secret password=private-secret")
        await self.assert_rpc_error(self.server().middleware[0](self.context(), handler), types.INTERNAL_ERROR, "Internal error")

    async def test_notifications_do_not_reach_project_request_handlers(self):
        next_handler = AsyncMock()
        value = await self.server().middleware[0](self.context(request_id=None), next_handler)
        self.assertIsNone(value)
        next_handler.assert_not_called()


class MCPStdioFramingTests(unittest.IsolatedAsyncioTestCase):
    async def frames(self, lines):
        output = MemoryOutput()
        values = [value async for value in _Input(MemoryInput(lines), output)]
        return values, output.errors

    def encoded(self, value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"

    async def test_valid_envelope_survives_utf8_exactly(self):
        value = message(params={"_meta": metadata(), "name": "read_project_file", "arguments": {"note": "한글"}})
        raw = self.encoded(value)
        frames, errors = await self.frames(raw)
        self.assertEqual(frames, [raw.decode()])
        self.assertEqual(errors, [])

    async def test_duplicate_keys_nonfinite_and_invalid_utf8_are_parse_errors(self):
        for raw in (b'{"jsonrpc":"2.0","jsonrpc":"2.0","method":"tools/list"}\n', b'{"jsonrpc":"2.0","number":NaN}\n', b'{"jsonrpc":"2.0","number":1e9999}\n', b'\xffprivate-secret\n', b'private-secret-not-json\n'):
            with self.subTest(raw=raw):
                frames, errors = await self.frames(raw)
                self.assertEqual(frames, [])
                self.assertEqual([item["code"] for item in errors], [types.PARSE_ERROR])
                self.assertNotIn("private-secret", repr(errors))

    async def test_batch_null_extra_envelope_fields_and_bad_ids_are_invalid_request(self):
        invalid = [[], None, [message()], {**message(), "outsidePath": "/private-secret"}, {**message(), "id": True}, {**message(), "id": None}, {**message(), "id": ""}, {**message(), "id": "x" * 257}, {**message(), "params": []}, {**message(), "method": None}]
        for value in invalid:
            with self.subTest(value=value):
                frames, errors = await self.frames(self.encoded(value))
                self.assertEqual(frames, [])
                self.assertEqual([item["code"] for item in errors], [types.INVALID_REQUEST])
                self.assertNotIn("private-secret", repr(errors))

    async def test_missing_meta_and_invalid_capabilities_are_invalid_params(self):
        for params in ({}, {"_meta": None}, {"_meta": metadata(**{types.CLIENT_CAPABILITIES_META_KEY: []})}):
            frames, errors = await self.frames(self.encoded(message(42, params=params)))
            self.assertEqual(frames, [])
            self.assertEqual(errors, [{"code": types.INVALID_PARAMS, "id": 42, "data": None}])

    async def test_version_mismatch_advertises_only_supported_safe_dates(self):
        for version, expected in (("2025-11-25", "2025-11-25"), ("password=private-secret", "invalid"), ({"token": "private-secret"}, "invalid")):
            with self.subTest(version=version):
                params = {"_meta": metadata(**{types.PROTOCOL_VERSION_META_KEY: version})}
                frames, errors = await self.frames(self.encoded(message(3, params=params)))
                self.assertEqual(frames, [])
                self.assertEqual(errors, [{"code": types.UNSUPPORTED_PROTOCOL_VERSION, "id": 3, "data": {"supported": [MCP_PROTOCOL_VERSION], "requested": expected}}])
                self.assertNotIn("private-secret", repr(errors))

    async def test_oversized_or_truncated_frame_drained_then_valid_request_recovers(self):
        valid = self.encoded(message(10))
        for invalid in (b"x" * (MAX_FRAME_BYTES + 1) + b"\n", b'{"private-secret":"unterminated"}'):
            with self.subTest(size=len(invalid)):
                tail = valid if invalid.endswith(b"\n") else b""
                frames, errors = await self.frames(invalid + tail)
                self.assertEqual(frames, [valid.decode()] if tail else [])
                self.assertEqual([item["code"] for item in errors], [types.INVALID_REQUEST])

    async def test_notifications_ignored_except_cooperative_cancellation(self):
        ignored = {"jsonrpc": "2.0", "method": "notifications/roots/list_changed", "params": {"secret": "private-secret"}}
        cancelled = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 5}}
        frames, errors = await self.frames(self.encoded(ignored) + self.encoded(cancelled))
        self.assertEqual(frames, [self.encoded(cancelled).decode()])
        self.assertEqual(errors, [])

    async def test_malformed_cancellation_is_ignored_before_sdk_logging(self):
        for params in ({}, {"requestId": None}, {"requestId": True}, {"requestId": ""}, {"requestId": "x" * 257}, {"requestId": []}, {"requestId": {"secret": "private-secret"}}):
            with self.subTest(params=params):
                cancelled = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params}
                frames, errors = await self.frames(self.encoded(cancelled))
                self.assertEqual(frames, [])
                self.assertEqual(errors, [])

    async def test_framing_error_has_only_generic_text_and_constant_version_data(self):
        writer = MemoryWriter()
        output = _Output(writer)
        await output.error(types.PARSE_ERROR)
        await output.error(types.INVALID_PARAMS, request_id=4)
        values = [json.loads(line) for line in writer.data.splitlines()]
        self.assertEqual(values[0], {"jsonrpc": "2.0", "id": None, "error": {"code": types.PARSE_ERROR, "message": "Parse error"}})
        self.assertEqual(values[1], {"jsonrpc": "2.0", "id": 4, "error": {"code": types.INVALID_PARAMS, "message": "Invalid parameters"}})
        self.assertEqual(writer.flushes, 2)

    async def test_output_serializes_concurrent_lines_atomically_and_limits_bytes(self):
        writer = MemoryWriter()
        output = _Output(writer)
        await asyncio.gather(*(output.write(json.dumps({"index": index}) + "\n") for index in range(20)))
        values = [json.loads(line) for line in writer.data.splitlines()]
        self.assertEqual({value["index"] for value in values}, set(range(20)))
        with self.assertRaisesRegex(StdioTransportError, "^MCP_TRANSPORT_ERROR$"):
            await output.write("x" * (MAX_FRAME_BYTES + 2))

    async def test_partial_writes_are_completed_before_flushing(self):
        class PartialWriter(MemoryWriter):
            async def write(self, value):
                return await super().write(value[:3])
        writer = PartialWriter()
        output = _Output(writer)
        expected = '{"message":"한글"}\n'
        await output.write(expected)
        self.assertEqual(writer.data, expected.encode())
        self.assertEqual(writer.flushes, 1)

    async def test_nonprogressing_output_write_fails_closed(self):
        for invalid in (0, None, -1):
            writer = MemoryWriter()
            writer.write = AsyncMock(return_value=invalid)
            with self.subTest(invalid=invalid), self.assertRaisesRegex(StdioTransportError, "^MCP_TRANSPORT_ERROR$"):
                await _Output(writer).write('{"jsonrpc":"2.0"}\n')
            self.assertEqual(writer.flushes, 0)


class MCPServerWireTests(_MCPServerFixture, unittest.IsolatedAsyncioTestCase):
    async def launch(self, role=AgentRole.QA):
        config = MCPChildConfiguration(
            binding=MCPBinding(role=role, agent_role=role, run_id=self.run.run_id, workspace_id=self.run.workspace_id),
            database_path=self.database, workspace_root=self.base,
        )
        params = child_parameters(config)
        process = await asyncio.create_subprocess_exec(
            params.command, *params.args, env=params.env,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        async def cleanup():
            if process.returncode is None:
                process.kill()
            await process.communicate()
        self.addAsyncCleanup(cleanup)
        return process

    async def exchange(self, process, raw_frames, count):
        process.stdin.write(raw_frames)
        await process.stdin.drain()
        replies = []
        for _ in range(count):
            line = await asyncio.wait_for(process.stdout.readline(), timeout=10)
            self.assertTrue(line, "Child closed before sending expected protocol replies")
            replies.append(json.loads(line))
        process.stdin.close()
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        self.assertEqual(stdout, b"")
        self.assertEqual(stderr, b"")
        self.assertEqual(process.returncode, 0)
        return replies

    async def test_real_child_protocol_and_tool_errors_stay_distinct_on_wire(self):
        process = await self.launch()
        frames = [
            message(1, "server/discover"), message(2, "tools/list"),
            message(3, "tools/call", {"_meta": metadata(), "name": "read_project_file", "arguments": self.arguments()}),
            message(4, "tools/call", {"_meta": metadata(role="DEVELOPER"), "name": "write_source_file", "arguments": self.arguments(content="# code\n")}),
            message(5, "tools/call", {"_meta": metadata(), "name": "read_project_file", "arguments": self.arguments(path="../../private-secret")}),
            message(6, "tools/call", {"_meta": metadata(), "name": "read_project_file", "arguments": self.arguments(hostPath="/private-secret")}),
        ]
        replies = await self.exchange(process, b"".join(json.dumps(value).encode() + b"\n" for value in frames), len(frames))
        by_id = {reply["id"]: reply for reply in replies}
        self.assertEqual(by_id[1]["result"]["supportedVersions"], [MCP_PROTOCOL_VERSION])
        self.assertEqual([tool["name"] for tool in by_id[2]["result"]["tools"]], list(ROLE_TOOL_NAMES[AgentRole.QA]))
        self.assertTrue(by_id[3]["result"]["isError"])
        self.assertEqual(by_id[3]["result"]["content"][0]["text"], "TOOL_NOT_IMPLEMENTED")
        self.assertEqual(by_id[4]["error"], {"code": types.INVALID_PARAMS, "message": "Invalid parameters"})
        self.assertEqual(by_id[5]["result"]["content"][0]["text"], "PATH_DENIED")
        self.assertEqual(by_id[6]["error"], {"code": types.INVALID_PARAMS, "message": "Invalid parameters"})
        self.assertNotIn("private-secret", json.dumps(replies))

    async def test_real_child_recovers_after_malformed_frame_without_logging_source(self):
        process = await self.launch()
        frames = (
            b'{"jsonrpc":"2.0","secret":"private-secret","secret":"duplicate"}\n'
            + json.dumps(message(9, params={"_meta": metadata(**{types.PROTOCOL_VERSION_META_KEY: "password=private-secret"})})).encode() + b"\n"
            + json.dumps(message(10, "server/discover")).encode() + b"\n"
        )
        replies = await self.exchange(process, frames, 3)
        null = next(reply for reply in replies if reply["id"] is None)
        self.assertEqual(null["error"], {"code": types.PARSE_ERROR, "message": "Parse error"})
        wrong = next(reply for reply in replies if reply["id"] == 9)
        self.assertEqual(wrong["error"]["code"], types.UNSUPPORTED_PROTOCOL_VERSION)
        self.assertEqual(wrong["error"]["data"], {"supported": [MCP_PROTOCOL_VERSION], "requested": "invalid"})
        self.assertEqual(next(reply for reply in replies if reply["id"] == 10)["result"]["supportedVersions"], [MCP_PROTOCOL_VERSION])
        self.assertNotIn("private-secret", json.dumps(replies))


if __name__ == "__main__":
    unittest.main()
