"""OpenAI REST boundary tests using MockTransport only, never cloud calls."""

from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agents.llm.contracts import (
    JsonSchema, LLMErrorCode, LLMRequest, LLMResponse, LLMRuntimeError, StructuredOutput,
    OPENAI_RESPONSES_GLOBAL_TEXT, REPORTED_SERVICE_TIERS, TokenUsage,
    ToolDefinition, UsageRecord, parse_json,
)
from agents.llm.budget import ExecutionBudget
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.roles.prompts import prepare_role_prompt
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


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

    async def test_actual_cache_write_is_input_subset_without_double_counting(self):
        usage = response_payload()["usage"]
        usage["input_tokens_details"]["cache_write_tokens"] = 7
        result, _ = await self.call(response_payload(usage=usage))
        self.assertEqual(result.usage, TokenUsage(20, 4, 24, 3, 2, 7))
        self.assertEqual(result.usage.total_tokens, 24)

    async def test_missing_cache_write_stays_unknown_and_explicit_zero_is_known(self):
        for details, expected in ((None, None), ({}, None), ({"cached_tokens": 3}, None),
                                  ({"cache_write_tokens": 0}, 0), ({"cache_write_tokens": 20}, 20)):
            with self.subTest(details=details):
                usage = response_payload()["usage"]
                usage["input_tokens_details"] = details
                result, _ = await self.call(response_payload(usage=usage))
                self.assertIs(result.usage.cache_write_input_tokens, expected)

    async def test_invalid_cache_write_or_overlapping_input_subsets_fail_closed(self):
        for details in ({"cache_write_tokens": True}, {"cache_write_tokens": "2"},
                        {"cache_write_tokens": 2.0}, {"cache_write_tokens": -1},
                        {"cache_write_tokens": 21}, {"cached_tokens": 14, "cache_write_tokens": 7}):
            with self.subTest(details=details):
                usage = response_payload()["usage"]
                usage["input_tokens_details"] = details
                with self.assertRaises(LLMRuntimeError) as caught:
                    await self.call(response_payload(usage=usage))
                self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
                self.assertIsNone(caught.exception.usage)

    async def test_reported_service_tier_uses_actual_response_only(self):
        self.assertEqual(REPORTED_SERVICE_TIERS, {"default", "flex", "priority", "scale", "fast", "ultrafast"})
        for tier in sorted(REPORTED_SERVICE_TIERS):
            with self.subTest(tier=tier):
                result, sent = await self.call(response_payload(service_tier=tier))
                self.assertEqual(result.reported_service_tier, tier)
                self.assertNotIn("service_tier", json.loads(sent[0].content))

    async def test_unreported_auto_or_unknown_tier_is_not_inferred_or_logged(self):
        payloads = [response_payload()]
        payloads.extend(response_payload(service_tier=tier)
                        for tier in (None, "auto", "Priority", "future-tier", True, 1, {}, [], SECRET))
        for payload in payloads:
            with self.subTest(tier_type=type(payload.get("service_tier")).__name__):
                result, _ = await self.call(payload)
                self.assertIsNone(result.reported_service_tier)
                self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", repr(result))

    async def test_direct_global_initial_string_and_text_parts_have_text_context(self):
        inputs = (
            [{"role": "user", "content": "Task data"}],
            [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Task data"}]}],
            [{"role": "user", "content": "Task data"}, {"type": "message", "id": "msg_previous",
                "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                "text": "Previous answer", "annotations": [], "logprobs": []}]}],
        )
        for items in inputs:
            with self.subTest(items=items):
                result, sent = await self.call(request=make_request(input_items_json=json.dumps(items)))
                self.assertEqual(result.billing_context, OPENAI_RESPONSES_GLOBAL_TEXT)
                self.assertEqual(json.loads(sent[0].content)["input"], items)

    async def test_text_function_and_reasoning_continuations_have_text_context(self):
        items = [
            {"role": "user", "content": "Task data"},
            {"id": "rs_previous", "type": "reasoning", "summary": [{"type": "summary_text", "text": "Summary"}],
             "encrypted_content": "OPAQUE_SYNTHETIC", "status": "completed"},
            {"id": "fc_previous", "type": "function_call", "call_id": "call_previous",
             "name": "read_project_file", "arguments": "{}", "status": "completed"},
            {"type": "function_call_output", "call_id": "call_previous", "output": "{}"},
        ]
        result, sent = await self.call(request=make_request(input_items_json=json.dumps(items)))
        self.assertEqual(result.billing_context, OPENAI_RESPONSES_GLOBAL_TEXT)
        self.assertEqual(json.loads(sent[0].content)["input"], items)

    async def test_multimodal_or_unknown_input_has_unknown_billing_context_without_wire_rewrite(self):
        inputs = (
            [{"role": "user", "content": [{"type": "input_image", "image_url": "https://image.invalid/synthetic"}]}],
            [{"role": "user", "content": [{"type": "input_file", "file_id": "file_synthetic"}]}],
            [{"role": "user", "content": [{"type": "input_audio", "data": "synthetic"}]}],
            [{"role": "user", "content": [{"type": "input_text", "text": "Task", "unknown": "future"}]}],
            [{"role": "user", "content": "Task", "unknown": True}],
            [{"role": "future-role", "content": "Task"}],
            [{"role": {}, "content": "Task"}],
            [{"role": "user", "content": [{"type": [], "text": "Task"}]}],
            [{"type": {}, "content": "Task"}],
            [{"type": None, "role": "user", "content": "Task"}],
            [{"role": "user", "content": "Task", "status": "unknown"}],
            [{"role": "user", "content": "Task", "id": {}}],
            [{"role": "user", "content": [{"type": "input_text", "text": "Task", "annotations": []}]}],
            [{"role": "assistant", "content": [{"type": "output_text", "text": "Task", "annotations": {}}]}],
            [{"type": "reasoning", "summary": [{"type": "unknown", "text": "Task"}]}],
            [{"type": "function_call_output", "call_id": "call_previous", "output": {"future": "value"}}],
        )
        for items in inputs:
            with self.subTest(items=items):
                result, sent = await self.call(request=make_request(input_items_json=json.dumps(items)))
                self.assertIsNone(result.billing_context)
                self.assertEqual(json.loads(sent[0].content)["input"], items)

    async def test_alternate_endpoint_is_not_labelled_original_global_text(self):
        with patch("agents.llm.openai_responses._ENDPOINT", "https://alternate.invalid/v1/responses"):
            result, sent = await self.call()
        self.assertEqual(str(sent[0].url), "https://alternate.invalid/v1/responses")
        self.assertIsNone(result.billing_context)

    async def test_control_responses_retain_actual_billing_metadata_and_usage(self):
        usage = response_payload()["usage"]
        usage["input_tokens_details"]["cache_write_tokens"] = 5
        payloads = [response_payload(status=status, output=[], service_tier="flex", usage=usage)
                    for status in ("failed", "incomplete")]
        payloads.append(response_payload(service_tier="priority", usage=usage, output=[{
            "id": "msg_refused", "type": "message", "role": "assistant",
            "content": [{"type": "refusal", "refusal": SECRET}],
        }]))
        for payload in payloads:
            with self.subTest(status=payload["status"]):
                result, _ = await self.call(payload)
                self.assertEqual(result.reported_service_tier, payload["service_tier"])
                self.assertEqual(result.billing_context, OPENAI_RESPONSES_GLOBAL_TEXT)
                self.assertEqual(result.usage.cache_write_input_tokens, 5)
                self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", repr(result))

    async def test_parse_error_keeps_cache_write_usage_but_not_untrusted_provenance(self):
        usage = response_payload()["usage"]
        usage["input_tokens_details"]["cache_write_tokens"] = 5
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.call(response_payload(model="unrelated-model", service_tier="priority", usage=usage))
        self.assertEqual(caught.exception.usage.cache_write_input_tokens, 5)
        self.assertFalse(hasattr(caught.exception, "reported_service_tier"))
        self.assertFalse(hasattr(caught.exception, "billing_context"))

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


class OpenAIEngineUsageTests(unittest.IsolatedAsyncioTestCase):
    async def run_mock_engine(self, payload, records):
        provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=payload)))
        self.addAsyncCleanup(provider.aclose)
        metadata = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=uuid4(), attempt=0)
        prompt = prepare_role_prompt(AgentRole.PLANNER, task_input={"request": "계획 생성"}, metadata=metadata)
        budget = ExecutionBudget(runtime_budget_ms=1000)
        engine = LLMEngine(role=AgentRole.PLANNER, provider=provider)
        return await engine.run(prompt=prompt, model=make_request().model, output=make_request().output,
                                budget=budget, usage_sink=records.append)

    async def test_actual_adapter_metadata_reaches_engine_record_without_provider_cost(self):
        usage = response_payload()["usage"]
        usage["input_tokens_details"]["cache_write_tokens"] = 5
        records = []
        result = await self.run_mock_engine(response_payload(service_tier="priority", usage=usage), records)
        self.assertEqual(result.data, {"summary": "done"})
        self.assertEqual(tuple(records), result.records)
        self.assertEqual(records[0].reported_model_id, "gpt-4.1-2025-04-14")
        self.assertEqual(records[0].reported_service_tier, "priority")
        self.assertEqual(records[0].billing_context, OPENAI_RESPONSES_GLOBAL_TEXT)
        self.assertEqual(records[0].usage.cache_write_input_tokens, 5)
        self.assertIsNone(records[0].cost_usd)

    async def test_actual_adapter_identity_error_records_only_measured_usage(self):
        usage = response_payload()["usage"]
        usage["input_tokens_details"]["cache_write_tokens"] = 5
        records = []
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_mock_engine(response_payload(id=" ", service_tier="priority", usage=usage), records)
        self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
        self.assertEqual(tuple(records), caught.exception.records)
        self.assertEqual(records[0].usage.total_tokens, 24)
        self.assertEqual(records[0].usage.cache_write_input_tokens, 5)
        self.assertIsNone(records[0].reported_model_id)
        self.assertIsNone(records[0].reported_service_tier)
        self.assertIsNone(records[0].billing_context)
        self.assertIsNone(records[0].cost_usd)


class UsageProvenanceContractTests(unittest.TestCase):
    def test_token_usage_last_field_preserves_existing_positional_order(self):
        existing = TokenUsage(20, 4, 24, 3, 2)
        self.assertIsNone(existing.cache_write_input_tokens)
        self.assertEqual(TokenUsage(20, 4, 24, 3, 2, 7).cache_write_input_tokens, 7)

    def test_token_usage_subset_validation_applies_to_direct_dataclasses(self):
        for write in (True, "1", 1.0, -1, 21):
            with self.subTest(write=write), self.assertRaises(LLMRuntimeError):
                TokenUsage(20, 4, 24, cache_write_input_tokens=write)
        with self.assertRaises(LLMRuntimeError):
            TokenUsage(20, 4, 24, cached_input_tokens=15, cache_write_input_tokens=6)
        self.assertEqual(TokenUsage(20, 4, 24, cached_input_tokens=None,
                                   cache_write_input_tokens=20).total_tokens, 24)

    def test_llm_response_metadata_is_optional_and_appended(self):
        legacy = LLMResponse("completed", "model", TokenUsage(1, 1, 2), "{}", (), "[]", False)
        self.assertIsNone(legacy.reported_service_tier)
        self.assertIsNone(legacy.billing_context)
        extended = replace(legacy, reported_service_tier="default", billing_context=OPENAI_RESPONSES_GLOBAL_TEXT)
        self.assertEqual(extended.reported_service_tier, "default")

    def test_usage_record_metadata_is_optional_and_cost_authority_is_unchanged(self):
        legacy = UsageRecord(1, AgentRole.PLANNER, make_request().model, "gpt-4.1-2025-04-14",
                             "completed", 0, TokenUsage(1, 1, 2))
        self.assertIsNone(legacy.cost_usd)
        self.assertIsNone(legacy.reported_service_tier)
        self.assertIsNone(legacy.billing_context)
        # Host Store/Pricing, not these optional metadata checks, owns cost admission.
        self.assertEqual(replace(legacy, cost_usd=1).cost_usd, 1)

    def test_dataclass_billing_metadata_rejects_coercion_and_unknown_values(self):
        response = LLMResponse("completed", "model", None)
        record = UsageRecord(1, AgentRole.PLANNER, make_request().model, "model", "completed", 0, None)
        for item in (response, record):
            for field, invalid in (("reported_service_tier", True), ("reported_service_tier", "auto"),
                                   ("reported_service_tier", SECRET), ("billing_context", True),
                                   ("billing_context", "openai"), ("billing_context", SECRET)):
                with self.subTest(type=type(item).__name__, field=field), self.assertRaises(LLMRuntimeError) as caught:
                    replace(item, **{field: invalid})
                self.assertEqual(caught.exception.code, LLMErrorCode.RESPONSE)
                self.assertNotIn("SYNTHETIC_ERROR_BODY_SECRET", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
