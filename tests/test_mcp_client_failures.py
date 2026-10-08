"""Safe failure metadata without retries, raw peer prose, paths or secrets."""

import asyncio
import json
import time
import traceback
import unittest
from unittest.mock import patch

from mcp.shared.exceptions import MCPError
from mcp_types import CallToolResult, TextContent

import test_mcp_client as fixtures
from agents.llm.contracts import ToolCall, ToolContext
from mcp_tools.client import BoundMCPClient, MCPClientError, MCPDeliveryState, child_parameters, open_mcp_client
from mcp_tools.runtime import MCPExecutionError
from orchestrator.domain.states import AgentRole


class ClientFailureMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = fixtures.configuration()
        self.sdk = fixtures.FakeClient(self.config)
        self.client = BoundMCPClient(configuration=self.config, _client=self.sdk)
        self.arguments = fixtures.read_arguments(self.config)

    async def failed_call(self, name="read_project_file", arguments=None, **options):
        with self.assertRaises(MCPClientError) as raised:
            await self.client.call_tool(name, self.arguments if arguments is None else arguments, **options)
        error = raised.exception
        self.assertNotIn("private-secret", str(error))
        self.assertNotIn("private-secret", repr(error))
        self.assertNotIn("/private-path", "".join(traceback.format_exception(error)))
        return error

    def test_constructor_preserves_only_approved_codes_and_delivery_enum(self):
        for code in MCPExecutionError:
            error = MCPClientError("MCP_CLIENT_TOOL_FAILED", tool_error_code=code.value,
                                   delivery_state=MCPDeliveryState.REPLIED)
            self.assertEqual(error.tool_error_code, code.value)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)
            self.assertEqual(str(error), "MCP_CLIENT_TOOL_FAILED")
            self.assertEqual(error.args, ("MCP_CLIENT_TOOL_FAILED",))
        self.assertEqual(MCPExecutionError.PROCESS_STARTUP_FAILURE.value, "PROCESS_STARTUP_FAILURE")
        self.assertEqual(MCPExecutionError.RESOURCE_BUSY.value, "RESOURCE_BUSY")

    def test_constructor_unknown_metadata_never_echoed_or_retained(self):
        for code in ("private-secret /private-path", " TIMEOUT", "TIMEOUT\n", True, [], {}):
            error = MCPClientError("private-secret /private-path", tool_error_code=code,
                                   delivery_state=code, protocol_error_code=code)
            self.assertEqual(error.code, "MCP_CLIENT_FAILED")
            self.assertIsNone(error.tool_error_code)
            self.assertIsNone(error.protocol_error_code)
            self.assertIs(error.delivery_state, MCPDeliveryState.UNKNOWN)
            self.assertNotIn("private-secret", str(vars(error)))
            self.assertNotIn("private-secret", repr(error))

    def test_constructor_protocol_codes_are_strict_int_allowlist_only(self):
        for code in (-32700, -32600, -32601, -32602):
            error = MCPClientError(protocol_error_code=code, delivery_state=MCPDeliveryState.REPLIED)
            self.assertEqual(error.protocol_error_code, code)
        for code in (None, True, False, -32602.0, "-32602", -32000, -32001, -32020, -32603, [], {}):
            self.assertIsNone(MCPClientError(protocol_error_code=code).protocol_error_code)

    async def test_every_server_execution_code_preserved_exactly_without_retry(self):
        for code in MCPExecutionError:
            self.sdk.session.result = CallToolResult(content=[TextContent(type="text", text=code.value)], isError=True)
            error = await self.failed_call()
            self.assertEqual(error.code, "MCP_CLIENT_TOOL_FAILED")
            self.assertEqual(error.tool_error_code, code.value)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)
            self.assertIsNone(error.protocol_error_code)
        self.assertEqual(len(self.sdk.session.calls), len(MCPExecutionError))

    async def test_unknown_peer_prose_whitespace_json_and_large_text_not_code(self):
        for text in ("private-secret /private-path", "TIMEOUT\n", " TIMEOUT", "timeout", '{"code":"TIMEOUT"}',
                     "TIMEOUT /private-path", "x" * (1024 * 1024 + 1)):
            self.sdk.session.result = CallToolResult(content=[TextContent(type="text", text=text)], isError=True)
            error = await self.failed_call()
            self.assertEqual(error.code, "MCP_CLIENT_TOOL_FAILED")
            self.assertIsNone(error.tool_error_code)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)

    async def test_multiple_or_missing_error_content_never_extracts_code(self):
        for content in ([], [TextContent(type="text", text="TIMEOUT"), TextContent(type="text", text="private-secret /private-path")],
                        [TextContent(type="text", text="TIMEOUT"), TextContent(type="text", text="TIMEOUT")]):
            self.sdk.session.result = CallToolResult(content=content, isError=True)
            error = await self.failed_call()
            self.assertIsNone(error.tool_error_code)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)

    async def test_structured_payload_on_iserror_does_not_become_approved_code(self):
        for data in ({}, {"error": "TIMEOUT"}, {"secret": "private-secret /private-path"}):
            self.sdk.session.result = CallToolResult(content=[TextContent(type="text", text="TIMEOUT")],
                                                    structuredContent=data, isError=True)
            error = await self.failed_call()
            self.assertIsNone(error.tool_error_code)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)

    async def test_error_peer_metadata_ignored_not_grant_or_prose(self):
        self.sdk.session.result = CallToolResult(content=[TextContent(type="text", text="PATH_DENIED")],
                                                isError=True, _meta={"retrySafe": True, "source": "private-secret /private-path"})
        error = await self.failed_call()
        self.assertEqual(error.tool_error_code, "PATH_DENIED")
        self.assertNotIn("retrySafe", vars(error))
        self.assertNotIn("private-secret", str(vars(error)))

    async def test_local_unknown_tool_role_and_workspace_rejections_not_sent(self):
        error = await self.failed_call(name="run_security_scan")
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        error = await self.failed_call(arguments=self.arguments | {"workspaceId": str(fixtures.uuid4())})
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(self.sdk.session.calls, [])

    async def test_local_input_shape_and_credential_rejections_not_sent(self):
        error = await self.failed_call(arguments=self.arguments | {"command": "private-secret"})
        self.assertEqual(error.code, "MCP_CLIENT_ARGUMENTS_INVALID")
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        arguments = {"workspaceId": str(self.config.binding.workspace_id), "path": "source/app.py",
                     "content": "password='private-secret'\n"}
        error = await self.failed_call(name="write_source_file", arguments=arguments)
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(self.sdk.session.calls, [])

    async def test_local_closed_client_not_sent_even_after_prior_success(self):
        await self.client.call_tool("read_project_file", self.arguments)
        self.client._state.active = False
        error = await self.failed_call()
        self.assertEqual(error.code, "MCP_CLIENT_UNAVAILABLE")
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(len(self.sdk.session.calls), 1)

    async def test_expired_and_invalid_preflight_deadlines_not_sent(self):
        error = await self.failed_call(deadline_monotonic=time.monotonic() - 1)
        self.assertEqual(error.code, "MCP_CLIENT_TIMEOUT")
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        error = await self.failed_call(deadline_monotonic=float("nan"))
        self.assertIs(error.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(self.sdk.session.calls, [])

    async def test_session_timeout_is_unknown_delivery_and_no_retry(self):
        self.sdk.session.delay = 2
        error = await self.failed_call(deadline_monotonic=time.monotonic() + .005)
        self.assertEqual(error.code, "MCP_CLIENT_TIMEOUT")
        self.assertIs(error.delivery_state, MCPDeliveryState.UNKNOWN)
        self.assertIsNone(error.tool_error_code)
        self.assertEqual(len(self.sdk.session.calls), 1)

    async def test_generic_wire_exception_unknown_no_side_effect_safety_claim(self):
        self.sdk.session.error = RuntimeError("private-secret /private-path")
        error = await self.failed_call()
        self.assertEqual(error.code, "MCP_CLIENT_FAILED")
        self.assertIs(error.delivery_state, MCPDeliveryState.UNKNOWN)
        self.assertIsNone(error.tool_error_code)
        self.assertIsNone(error.protocol_error_code)
        self.assertEqual(len(self.sdk.session.calls), 1)

    async def test_jsonrpc_approved_error_codes_replied_without_raw_data(self):
        for code in (-32700, -32600, -32601, -32602):
            self.sdk.session.error = MCPError(code, "private-secret /private-path", data={"secret": "private-secret"})
            error = await self.failed_call()
            self.assertEqual(error.code, "MCP_CLIENT_FAILED")
            self.assertEqual(error.protocol_error_code, code)
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)
            self.assertIsNone(error.tool_error_code)
            self.assertNotIn("private-secret", str(vars(error)))
        self.assertEqual(len(self.sdk.session.calls), 4)

    async def test_sdk_timeout_connection_closed_and_unapproved_protocol_errors_unknown(self):
        for code in (-32000, -32001, -32020, -32603, -32099):
            self.sdk.session.error = MCPError(code, "private-secret /private-path", data={"secret": "private-secret"})
            error = await self.failed_call()
            self.assertEqual(error.code, "MCP_CLIENT_FAILED")
            self.assertIs(error.delivery_state, MCPDeliveryState.UNKNOWN)
            self.assertIsNone(error.protocol_error_code)
            self.assertIsNone(error.tool_error_code)
        self.assertEqual(len(self.sdk.session.calls), 5)

    async def test_success_invalid_output_or_content_is_replied_not_unsent(self):
        for result in (None, fixtures.successful_result({"private": "private-secret"}),
                       CallToolResult(content=[TextContent(type="text", text="private-secret /private-path")],
                                      structuredContent=fixtures.read_output())):
            self.sdk.session.result = result
            error = await self.failed_call()
            self.assertEqual(error.code, "MCP_CLIENT_OUTPUT_INVALID")
            self.assertIs(error.delivery_state, MCPDeliveryState.REPLIED)
            self.assertIsNone(error.tool_error_code)

    async def test_success_dict_api_and_single_session_call_unchanged(self):
        result = await self.client.call_tool("read_project_file", self.arguments)
        self.assertEqual(result, fixtures.read_output())
        self.assertIs(type(result), dict)
        self.assertEqual(len(self.sdk.session.calls), 1)
        self.assertTrue(self.client._state.tool_request_started)

    async def test_executor_context_rejection_not_sent_and_approved_error_survives_adapter(self):
        call = ToolCall(call_id="call-1", name="read_project_file", arguments_json=json.dumps(self.arguments))
        context = ToolContext(AgentRole.QA, str(self.config.binding.workspace_id), time.monotonic() + 1)
        with self.assertRaises(MCPClientError) as caught:
            await self.client.execute(call, self.arguments, context)
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(self.sdk.session.calls, [])
        context = ToolContext(AgentRole.DEVELOPER, str(self.config.binding.workspace_id), time.monotonic() + 1)
        self.sdk.session.result = CallToolResult(content=[TextContent(type="text", text="WRITE_CONFLICT")], isError=True)
        with self.assertRaises(MCPClientError) as caught:
            await self.client.execute(call, self.arguments, context)
        self.assertEqual(caught.exception.tool_error_code, "WRITE_CONFLICT")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.REPLIED)

    async def test_cancellation_propagates_without_error_retry_or_not_sent_claim(self):
        self.sdk.session.delay = 2
        task = asyncio.create_task(self.client.call_tool("read_project_file", self.arguments))
        await asyncio.sleep(.005)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.sdk.session.calls), 1)

    async def test_discovery_failure_or_timeout_is_not_sent_tool_request(self):
        self.sdk.session.discover_raw["supportedVersions"] = ["2025-11-25"]
        with self.assertRaises(MCPClientError) as caught:
            await self.client._verify_peer()
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)
        self.assertEqual(self.sdk.session.calls, [])
        self.sdk.session.delay = 2
        with self.assertRaises(MCPClientError) as caught:
            await self.client._verify_peer()
        self.assertEqual(caught.exception.code, "MCP_CLIENT_TIMEOUT")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)

    def test_invalid_child_parameters_local_not_sent(self):
        with self.assertRaises(MCPClientError) as caught:
            child_parameters({"command": "/private-path"})
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)

    async def test_child_open_failure_before_tool_request_not_sent(self):
        class Manager:
            async def __aenter__(self):
                raise OSError("private-secret /private-path")
            async def __aexit__(self, *_args):
                return None
        with patch("mcp_tools.client.Client", return_value=Manager()):
            with self.assertRaises(MCPClientError) as caught:
                async with open_mcp_client(self.config):
                    self.fail("must not yield")
        self.assertEqual(caught.exception.code, "MCP_CLIENT_UNAVAILABLE")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)

    async def test_child_shutdown_failure_without_tool_request_not_sent(self):
        sdk = self.sdk
        class Manager:
            async def __aenter__(self):
                return sdk
            async def __aexit__(self, *_args):
                raise RuntimeError("private-secret /private-path")
        with patch("mcp_tools.client.Client", return_value=Manager()):
            with self.assertRaises(MCPClientError) as caught:
                async with open_mcp_client(self.config):
                    pass
        self.assertEqual(caught.exception.code, "MCP_CLIENT_FAILED")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.NOT_SENT)

    async def test_child_shutdown_failure_after_tool_call_unknown_never_claims_not_sent(self):
        sdk = self.sdk
        class Manager:
            async def __aenter__(self):
                return sdk
            async def __aexit__(self, *_args):
                raise OSError("private-secret /private-path")
        with patch("mcp_tools.client.Client", return_value=Manager()):
            with self.assertRaises(MCPClientError) as caught:
                async with open_mcp_client(self.config) as client:
                    await client.call_tool("read_project_file", self.arguments)
        self.assertEqual(caught.exception.code, "MCP_CLIENT_UNAVAILABLE")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.UNKNOWN)
        self.assertEqual(len(sdk.session.calls), 1)

    async def test_open_context_preserves_exact_tool_failure_after_teardown(self):
        sdk = self.sdk
        sdk.session.result = CallToolResult(content=[TextContent(type="text", text="RESOURCE_BUSY")], isError=True)
        class Manager:
            async def __aenter__(self):
                return sdk
            async def __aexit__(self, *_args):
                return None
        with patch("mcp_tools.client.Client", return_value=Manager()):
            with self.assertRaises(MCPClientError) as caught:
                async with open_mcp_client(self.config) as client:
                    await client.call_tool("read_project_file", self.arguments)
        self.assertEqual(caught.exception.code, "MCP_CLIENT_TOOL_FAILED")
        self.assertEqual(caught.exception.tool_error_code, "RESOURCE_BUSY")
        self.assertIs(caught.exception.delivery_state, MCPDeliveryState.REPLIED)


if __name__ == "__main__":
    unittest.main()
