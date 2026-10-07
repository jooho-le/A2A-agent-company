"""OpenAI REST boundary tests using MockTransport only, never cloud calls."""

from dataclasses import replace
import json
import unittest

import httpx
from pydantic import SecretStr

from agents.llm.contracts import (
    JsonSchema, LLMErrorCode, LLMRequest, LLMRuntimeError, StructuredOutput,
    ToolDefinition, parse_json,
)
from agents.llm.openai_responses import OpenAIResponsesProvider
from orchestrator.domain.run_configuration import ModelConfiguration


KEY = "SYNTHETIC_API_KEY_NOT_REAL"
SECRET = "password=SYNTHETIC_ERROR_BODY_SECRET"
STRICT_SCHEMA = {
    "type": "object", "properties": {"summary": {"type": "string"}},
    "required": ["summary"], "additionalProperties": False,
}


def make_request(**changes):
    baseline = LLMRequest(
        model=ModelConfiguration(provider="openai", modelId="gpt-4.1", temperature=0),
        system_prompt="Trusted static instructions",
        input_items_json='[{"role":"user","content":"Task data"}]',
        tools=(), output=StructuredOutput("role_result", JsonSchema.from_dict(STRICT_SCHEMA)),
        max_output_tokens=64, timeout_seconds=5,
    )
    return replace(baseline, **changes)


def response_payload(**changes):
    payload = {
        "id": "resp_synthetic", "status": "completed", "model": "gpt-4.1-2025-04-14",
        "output": [{
            "id": "msg_synthetic", "type": "message", "role": "assistant",
            "status": "completed", "content": [{
                "type": "output_text", "text": '{"summary":"done"}', "annotations": [],
            }],
        }],
        "usage": {
            "input_tokens": 20, "output_tokens": 4, "total_tokens": 24,
            "input_tokens_details": {"cached_tokens": 3},
            "output_tokens_details": {"reasoning_tokens": 2},
        },
    }
    payload.update(changes)
    return payload


class OpenAIResponsesTests(unittest.IsolatedAsyncioTestCase):
    async def call(self, payload=None, *, request=None, status=200, handler=None, key=SecretStr(KEY)):
        sent = []

        def respond(wire_request):
            sent.append(wire_request)
            if handler is not None:
                return handler(wire_request)
            return httpx.Response(status, json=response_payload() if payload is None else payload)

        provider = OpenAIResponsesProvider(key, transport=httpx.MockTransport(respond))
        try:
            result = await provider.complete(request or make_request())
            return result, sent
        finally:
            await provider.aclose()

    async def expect_error(self, expected, **kwargs):
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.call(**kwargs)
        self.assertEqual(caught.exception.code, expected)
        self.assertEqual(str(caught.exception), expected.value)
        self.assertNotIn(KEY, repr(caught.exception))
        self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    async def test_stateless_fixed_endpoint_and_explicit_schema_request(self):
        result, sent = await self.call()
        self.assertEqual(len(sent), 1)
        self.assertEqual(str(sent[0].url), "https://api.openai.com/v1/responses")
        self.assertEqual(sent[0].headers["Authorization"], "Bearer " + KEY)
        wire = json.loads(sent[0].content)
        self.assertEqual(wire["model"], "gpt-4.1")
        self.assertEqual(wire["temperature"], 0)
        self.assertEqual(wire["max_output_tokens"], 64)
        self.assertEqual(wire["instructions"], "Trusted static instructions")
        self.assertEqual(wire["input"], [{"role": "user", "content": "Task data"}])
        self.assertEqual(wire["text"]["format"], {
            "type": "json_schema", "name": "role_result", "strict": True,
            "schema": STRICT_SCHEMA,
        })
        self.assertEqual(wire["include"], ["reasoning.encrypted_content"])
        for setting in ("store", "background", "stream", "parallel_tool_calls"):
            self.assertIs(wire[setting], False)
        self.assertNotIn("seed", wire)
        self.assertNotIn("previous_response_id", wire)
        self.assertEqual(result.model_id, "gpt-4.1-2025-04-14")
        self.assertEqual(result.output_text, '{"summary":"done"}')

    async def test_actual_token_usage_and_details(self):
        result, _ = await self.call()
        self.assertEqual(result.usage.input_tokens, 20)
        self.assertEqual(result.usage.output_tokens, 4)
        self.assertEqual(result.usage.total_tokens, 24)
        self.assertEqual(result.usage.cached_input_tokens, 3)
        self.assertEqual(result.usage.reasoning_output_tokens, 2)

    async def test_missing_usage_stays_unknown(self):
        for usage in (None, "missing"):
            with self.subTest(usage=usage):
                payload = response_payload()
                if usage == "missing":
                    payload.pop("usage")
                else:
                    payload["usage"] = usage
                result, _ = await self.call(payload)
                self.assertIsNone(result.usage)

    async def test_function_schema_stays_optional_and_is_not_made_strict(self):
        schema = {
            "type": "object", "properties": {
                "workspaceId": {"type": "string"},
                "expectedSha256": {"type": "string"},
            }, "required": ["workspaceId"], "additionalProperties": False,
        }
        tool = ToolDefinition("write_source_file", "Write allowed source", JsonSchema.from_dict(schema), JsonSchema.from_dict(STRICT_SCHEMA))
        _, sent = await self.call(request=make_request(tools=(tool,)))
        function = json.loads(sent[0].content)["tools"][0]
        self.assertIs(function["strict"], False)
        self.assertEqual(function["parameters"], schema)
        self.assertEqual(tool.input_schema.to_dict(), schema)

    async def test_function_call_and_encrypted_reasoning_are_preserved(self):
        items = [{
            "id": " rs_synthetic ", "type": "reasoning", "summary": [],
            "encrypted_content": "OPAQUE_SIGNED-password=not-a-log",
        }, {
            "id": " fc_synthetic ", "type": "function_call",
            "call_id": " call_opaque ", "name": "read_project_file",
            "arguments": '{ "workspaceId" : "synthetic" }', "status": "completed",
        }]
        result, _ = await self.call(response_payload(output=items))
        self.assertEqual(parse_json(result.output_items_json), items)
        self.assertEqual(result.tool_calls[0].call_id, " call_opaque ")
        self.assertEqual(result.tool_calls[0].arguments_json, '{ "workspaceId" : "synthetic" }')
        self.assertIsNone(result.output_text)
        self.assertNotIn("OPAQUE_SIGNED", repr(result))
        self.assertNotIn("call_opaque", repr(result.tool_calls[0]))
        self.assertNotIn("synthetic", repr(result.tool_calls[0]))

    async def test_refusal_is_control_response_with_usage(self):
        payload = response_payload(output=[{
            "id": "msg_refusal", "type": "message", "role": "assistant",
            "content": [{"type": "refusal", "refusal": SECRET}],
        }])
        result, _ = await self.call(payload)
        self.assertTrue(result.refused)
        self.assertIsNotNone(result.usage)
        self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", repr(result))

    async def test_incomplete_and_failed_preserve_real_usage(self):
        for status in ("incomplete", "failed"):
            with self.subTest(status=status):
                result, _ = await self.call(response_payload(status=status, output=[], error={"message": SECRET}))
                self.assertEqual(result.status, status)
                self.assertEqual(result.usage.total_tokens, 24)
                self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", repr(result))

    async def test_interrupted_function_arguments_preserve_status_and_usage(self):
        item = {
            "id": "fc_interrupted", "type": "function_call", "call_id": "call_interrupted",
            "name": "write_source_file", "arguments": '{"content":"unfinished',
        }
        for status in ("incomplete", "failed"):
            with self.subTest(status=status):
                result, _ = await self.call(response_payload(status=status, output=[item]))
                self.assertEqual(result.status, status)
                self.assertEqual(result.usage.total_tokens, 24)
                self.assertEqual(result.tool_calls[0].arguments_json, item["arguments"])

    async def test_wire_continuation_is_sent_without_signature_redaction(self):
        items = [
            {"role": "user", "content": "Task data"},
            {"id": "reasoning", "type": "reasoning", "summary": [], "encrypted_content": "OPAQUE_password=signed-by-provider"},
            {"id": "function", "type": "function_call", "call_id": " call ", "name": "read_project_file", "arguments": "{}"},
            {"type": "function_call_output", "call_id": " call ", "output": '{"path":"file.py"}'},
        ]
        _, sent = await self.call(request=make_request(input_items_json=json.dumps(items)))
        self.assertEqual(json.loads(sent[0].content)["input"], items)

    async def test_pinned_revision_is_sent_and_must_match(self):
        model = ModelConfiguration(provider="openai", modelId="gpt-4.1", modelRevision="gpt-4.1-2025-04-14", temperature=1)
        _, sent = await self.call(request=make_request(model=model))
        self.assertEqual(json.loads(sent[0].content)["model"], model.model_revision)
        await self.expect_error(LLMErrorCode.RESPONSE, request=make_request(model=model), payload=response_payload(model="gpt-4.1"))

    async def test_unrelated_reported_model_cannot_be_fallback(self):
        for model in (None, " ", "gpt-4.1-mini", "gpt-4.1-mini-2025-04-14", "another-provider-model"):
            with self.subTest(model=model):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(model=model))

    async def test_no_default_provider_model_or_key(self):
        for key in (None, SecretStr(" "), KEY):
            with self.subTest(key_type=type(key).__name__):
                await self.expect_error(LLMErrorCode.CONFIGURATION, key=key)
        wrong = ModelConfiguration(provider="another", modelId="model", temperature=0)
        await self.expect_error(LLMErrorCode.CONFIGURATION, request=make_request(model=wrong))

    async def test_seed_and_invalid_temperature_are_not_silently_ignored(self):
        for changes in ({"seed": 1}, {"temperature": 3}, {"model_revision": " "}):
            with self.subTest(changes=changes):
                model = make_request().model.model_copy(update=changes)
                await self.expect_error(LLMErrorCode.CONFIGURATION, request=make_request(model=model))

    async def test_output_schema_must_be_openai_compatible_without_rewrite(self):
        schema = JsonSchema.from_dict({"type": "object", "properties": {"optional": {"type": "string"}}, "additionalProperties": False})
        await self.expect_error(LLMErrorCode.SCHEMA, request=make_request(output=StructuredOutput("bad_schema", schema)))
        self.assertNotIn("required", schema.to_dict())

    async def test_invalid_request_limits_are_rejected_before_http(self):
        for changes in (
            {"max_output_tokens": 15}, {"max_output_tokens": True},
            {"timeout_seconds": 0}, {"timeout_seconds": float("nan")},
            {"timeout_seconds": True}, {"system_prompt": " "},
            {"input_items_json": "{}"}, {"input_items_json": "[]"},
            {"input_items_json": "[1]"},
        ):
            with self.subTest(changes=changes):
                def no_http(_request):
                    self.fail("Invalid configuration must not issue HTTP")
                await self.expect_error(LLMErrorCode.CONFIGURATION, request=make_request(**changes), handler=no_http)

    async def test_duplicate_or_invalid_function_definitions_are_not_sent(self):
        definition = ToolDefinition("read_project_file", "read", JsonSchema.from_dict(STRICT_SCHEMA), JsonSchema.from_dict(STRICT_SCHEMA))
        for definitions in ((definition, definition), (replace(definition, name="arbitrary shell"),)):
            with self.subTest(names=tuple(item.name for item in definitions)):
                await self.expect_error(LLMErrorCode.CONFIGURATION, request=make_request(tools=definitions))

    async def test_auth_failure_does_not_echo_body(self):
        for status in (401, 403):
            with self.subTest(status=status):
                await self.expect_error(LLMErrorCode.AUTH, status=status, payload={"error": {"message": SECRET}})

    async def test_other_http_errors_and_redirects_do_not_retry(self):
        for status in (302, 400, 429, 500, 503):
            with self.subTest(status=status):
                calls = []

                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, headers={"Location": "https://elsewhere.invalid"}, text=SECRET)

                await self.expect_error(LLMErrorCode.PROVIDER, handler=handler)
                self.assertEqual(len(calls), 1)

    async def test_timeout_and_transport_errors_are_safe(self):
        for error_type, expected in ((httpx.ReadTimeout, LLMErrorCode.TIMEOUT), (httpx.ConnectError, LLMErrorCode.PROVIDER)):
            with self.subTest(error=error_type.__name__):
                def handler(request):
                    raise error_type(SECRET, request=request)
                await self.expect_error(expected, handler=handler)

    async def test_malformed_status_and_response_ids_are_rejected(self):
        for changes in ({"status": "queued"}, {"status": None}, {"id": " "}, {"id": 1}, {"output": {}}, {"output": ["wrong"]}):
            with self.subTest(changes=changes):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(**changes))

    async def test_unsupported_built_in_tools_are_not_project_tool_calls(self):
        for kind in ("web_search_call", "computer_call", "code_interpreter_call", "file_search_call"):
            with self.subTest(kind=kind):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(output=[{"id": "item", "type": kind}]))

    async def test_malformed_function_arguments_and_ids_are_rejected(self):
        base = {"id": "function", "type": "function_call", "call_id": "call", "name": "read_project_file", "arguments": "{}"}
        for changes in ({"id": " "}, {"call_id": " "}, {"name": "shell command"}, {"arguments": "[]"}, {"arguments": '{"x":1,"x":2}'}, {"arguments": '{"x":NaN}'}, {"arguments": "```json\n{}\n```"}):
            with self.subTest(changes=changes):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(output=[{**base, **changes}]))
        await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(output=[base, {**base, "id": "second"}]))

    async def test_duplicate_output_ids_and_malformed_message_parts_are_rejected(self):
        message = response_payload()["output"][0]
        for items in (
            [message, message], [{**message, "role": "user"}],
            [{**message, "content": {}}], [{**message, "content": ["wrong"]}],
            [{**message, "content": [{"type": "output_text", "text": 1}]}],
            [{**message, "content": [{"type": "input_text", "text": "wrong"}]}],
        ):
            with self.subTest(items=items):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(output=items))

    async def test_usage_must_be_factual_nonnegative_integral_and_consistent(self):
        for usage in (
            "unknown", {}, {"input_tokens": True, "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": "1", "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": 1.5, "output_tokens": 1, "total_tokens": 2},
            {"input_tokens": -1, "output_tokens": 1, "total_tokens": 0},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 3},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "input_tokens_details": []},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "input_tokens_details": {"cached_tokens": 2}},
            {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2, "output_tokens_details": {"reasoning_tokens": 2}},
        ):
            with self.subTest(usage=usage):
                await self.expect_error(LLMErrorCode.RESPONSE, payload=response_payload(usage=usage))

    async def test_response_contract_failure_retains_prevalidated_actual_usage(self):
        cases = (
            response_payload(model="unrelated-model"),
            response_payload(id=" "),
            response_payload(status="unknown-status"),
            response_payload(output=[{"id": "unsupported", "type": "web_search_call"}]),
            response_payload(output=[{
                "id": "bad_function", "type": "function_call", "call_id": "call",
                "name": "read_project_file", "arguments": "not valid JSON",
            }]),
        )
        for payload in cases:
            with self.subTest(status=payload["status"]):
                with self.assertRaises(LLMRuntimeError) as caught:
                    await self.call(payload)
                self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
                self.assertEqual(caught.exception.usage.total_tokens, 24)
                self.assertEqual(caught.exception.usage.cached_input_tokens, 3)

    async def test_malformed_usage_is_unknown_even_with_other_response_errors(self):
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.call(response_payload(model="wrong", usage={"input_tokens": SECRET}))
        self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
        self.assertIsNone(caught.exception.usage)
        self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", str(caught.exception))

    async def test_invalid_response_json_is_not_repaired(self):
        for raw in (SECRET, '{"id":"one","id":"two"}', '{"value":NaN}'):
            with self.subTest(raw=raw):
                await self.expect_error(LLMErrorCode.RESPONSE, handler=lambda _request: httpx.Response(200, text=raw))

    async def test_environment_proxy_and_redirect_behavior_are_disabled(self):
        provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response_payload())))
        self.assertFalse(provider._client.trust_env)
        self.assertFalse(provider._client.follow_redirects)
        self.assertNotIn(KEY, repr(provider))
        await provider.aclose()
        self.assertTrue(provider._client.is_closed)


if __name__ == "__main__":
    unittest.main()
