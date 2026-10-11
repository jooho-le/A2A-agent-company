"""Explicit OpenAI Responses REST adapter; no SDK retry or remote state.

The adapter does not execute Tools, declare product success, or persist Trace.
Raw continuation items, including signed encrypted reasoning, stay wire-only.
"""

import math
import re

import httpx
from pydantic import SecretStr

from agents.llm.contracts import (
    LLMErrorCode, LLMRequest, LLMResponse, LLMRuntimeError, StructuredOutput,
    OPENAI_RESPONSES_GLOBAL_TEXT, REPORTED_SERVICE_TIERS,
    TokenUsage, ToolCall, json_text, parse_json,
)
from orchestrator.domain.run_configuration import ModelConfiguration


_GLOBAL_ENDPOINT = "https://api.openai.com/v1/responses"
_ENDPOINT = _GLOBAL_ENDPOINT
_FUNCTION_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _nonblank(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _text_only_items(items) -> bool:
    """Conservative billing classification; does not change wire admission."""
    if type(items) is not list or not items:
        return False
    for item in items:
        if type(item) is not dict:
            return False
        kind = item.get("type")
        if ("type" in item and type(kind) is not str
                or "id" in item and not _nonblank(item["id"])
                or "status" in item and (type(item["status"]) is not str
                    or item["status"] not in {"completed", "incomplete", "in_progress"})):
            return False
        if kind in (None, "message"):
            if (set(item) - {"type", "id", "status", "role", "content"}
                    or type(item.get("role")) is not str
                    or item["role"] not in {"user", "assistant", "system", "developer"}):
                return False
            content = item.get("content")
            if type(content) is str:
                continue
            if type(content) is not list or not content:
                return False
            for part in content:
                if (type(part) is not dict or type(part.get("type")) is not str
                        or part["type"] not in {"input_text", "output_text"}
                        or type(part.get("text")) is not str
                        or set(part) - ({"type", "text"} if part["type"] == "input_text"
                            else {"type", "text", "annotations", "logprobs"})
                        or any(name in part and type(part[name]) is not list
                               for name in ("annotations", "logprobs"))):
                    return False
        elif kind == "function_call":
            if (set(item) - {"type", "id", "status", "call_id", "name", "arguments"}
                    or not _nonblank(item.get("call_id")) or not _nonblank(item.get("name"))
                    or type(item.get("arguments")) is not str):
                return False
        elif kind == "function_call_output":
            if (set(item) - {"type", "id", "status", "call_id", "output"}
                    or not _nonblank(item.get("call_id")) or type(item.get("output")) is not str):
                return False
        elif kind == "reasoning":
            if (set(item) - {"type", "id", "status", "summary", "encrypted_content"}
                    or type(item.get("summary")) is not list
                    or item.get("encrypted_content") is not None and type(item["encrypted_content"]) is not str):
                return False
            for part in item["summary"]:
                if (type(part) is not dict or set(part) != {"type", "text"}
                        or part["type"] != "summary_text" or type(part["text"]) is not str):
                    return False
        else:
            return False
    return True


class OpenAIResponsesProvider:
    """One explicitly selected provider; construction performs no HTTP I/O."""

    name = "openai"

    def __init__(
        self, api_key: SecretStr | None, *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            transport=transport, trust_env=False, follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def validate_configuration(
        self, model: ModelConfiguration, output: StructuredOutput,
    ) -> None:
        if (
            not isinstance(model, ModelConfiguration)
            or model.provider != self.name
            or not _nonblank(model.model_id)
            or not 0 <= model.temperature <= 2
            or model.seed is not None
            or model.model_revision is not None and not _nonblank(model.model_revision)
            or not isinstance(self._api_key, SecretStr)
            or not self._api_key.get_secret_value().strip()
            or not isinstance(output, StructuredOutput)
        ):
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
        # Reject incompatible schemas instead of silently weakening contracts.
        output.schema.require_openai_strict()

    def _request_body(self, request: LLMRequest) -> dict:
        self.validate_configuration(request.model, request.output)
        try:
            if (
                type(request.max_output_tokens) is not int
                or request.max_output_tokens < 16
                or isinstance(request.timeout_seconds, bool)
                or not math.isfinite(request.timeout_seconds)
                or request.timeout_seconds <= 0
                or not _nonblank(request.system_prompt)
            ):
                raise ValueError("Invalid request settings")
            items = parse_json(request.input_items_json)
            if not isinstance(items, list) or not items or any(not isinstance(item, dict) for item in items):
                raise ValueError("Invalid input items")
            tools = []
            names = set()
            for tool in request.tools:
                if (
                    not isinstance(tool.name, str)
                    or not _FUNCTION_NAME.fullmatch(tool.name)
                    or tool.name in names
                    or not isinstance(tool.description, str)
                ):
                    raise ValueError("Invalid function definition")
                names.add(tool.name)
                tools.append({
                    "type": "function", "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema.to_dict(),
                    # Optional project Tool arguments must stay optional.
                    # The engine enforces their unchanged schemas locally.
                    "strict": False,
                })
            return {
                "model": request.model.model_revision or request.model.model_id,
                "instructions": request.system_prompt,
                "input": items,
                "temperature": request.model.temperature,
                "max_output_tokens": request.max_output_tokens,
                "tools": tools,
                "parallel_tool_calls": False,
                "text": {"format": {
                    "type": "json_schema", "name": request.output.name,
                    "strict": True, "schema": request.output.schema.to_dict(),
                }},
                "store": False, "background": False, "stream": False,
                "include": ["reasoning.encrypted_content"],
            }
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION) from None

    async def complete(self, request: LLMRequest) -> LLMResponse:
        body = self._request_body(request)
        try:
            response = await self._client.post(
                _ENDPOINT, json=body,
                headers={"Authorization": "Bearer " + self._api_key.get_secret_value()},
                timeout=request.timeout_seconds,
            )
        except httpx.TimeoutException:
            raise LLMRuntimeError(LLMErrorCode.TIMEOUT) from None
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.PROVIDER) from None
        if response.status_code in (401, 403):
            raise LLMRuntimeError(LLMErrorCode.AUTH)
        if not 200 <= response.status_code < 300:
            # Includes redirects, rate limits and unsupported model options.
            # No automatic retry and no response-body exception messages.
            raise LLMRuntimeError(LLMErrorCode.PROVIDER)
        usage = None
        try:
            payload = parse_json(response.text)
            if isinstance(payload, dict):
                # Accounting remains factual even if subsequent response
                # identity or Tool/output validation fails.
                usage = self._parse_usage(payload.get("usage"))
            context = OPENAI_RESPONSES_GLOBAL_TEXT if (
                _ENDPOINT == _GLOBAL_ENDPOINT and str(response.request.url) == _GLOBAL_ENDPOINT
                and _text_only_items(body["input"])) else None
            return self._parse_response(payload, request.model, usage=usage, billing_context=context)
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE, usage=usage) from None

    @staticmethod
    def _parse_usage(payload: object) -> TokenUsage | None:
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise ValueError("Invalid usage")
        details = []
        for key, token_key in (
            ("input_tokens_details", "cached_tokens"),
            ("output_tokens_details", "reasoning_tokens"),
        ):
            detail = payload.get(key)
            if detail is not None and not isinstance(detail, dict):
                raise ValueError("Invalid usage details")
            details.append(detail.get(token_key) if detail is not None else None)
        return TokenUsage(
            input_tokens=payload["input_tokens"],
            output_tokens=payload["output_tokens"],
            total_tokens=payload["total_tokens"],
            cached_input_tokens=details[0], reasoning_output_tokens=details[1],
            cache_write_input_tokens=payload.get("input_tokens_details", {}).get("cache_write_tokens")
                if payload.get("input_tokens_details") is not None else None,
        )

    @classmethod
    def _parse_response(
        cls, payload: object, model: ModelConfiguration, *, usage: TokenUsage | None,
        billing_context: str | None = None,
    ) -> LLMResponse:
        if not isinstance(payload, dict) or not _nonblank(payload.get("id")):
            raise ValueError("Invalid response")
        status = payload.get("status")
        if status not in ("completed", "incomplete", "failed"):
            raise ValueError("Invalid response status")
        reported_model = payload.get("model")
        wire_model = model.model_revision or model.model_id
        if not _nonblank(reported_model):
            raise ValueError("Missing reported model")
        if reported_model != wire_model and (
            model.model_revision is not None
            or re.fullmatch(re.escape(model.model_id) + r"-\d{4}-\d{2}-\d{2}", reported_model) is None
        ):
            raise ValueError("Reported model mismatch")
        # With no pinned revision, an API alias may resolve to its dated
        # snapshot. Record that actual identity; do not silently swap models.
        items = payload.get("output")
        if not isinstance(items, list):
            raise ValueError("Invalid output items")
        texts = []
        calls = []
        item_ids = set()
        call_ids = set()
        refused = False
        for item in items:
            if not isinstance(item, dict) or not _nonblank(item.get("id")):
                raise ValueError("Invalid output ID")
            if item["id"] in item_ids:
                raise ValueError("Duplicate output ID")
            item_ids.add(item["id"])
            kind = item.get("type")
            if kind == "message":
                content = item.get("content")
                if item.get("role") != "assistant" or not isinstance(content, list):
                    raise ValueError("Invalid assistant message")
                for part in content:
                    if not isinstance(part, dict):
                        raise ValueError("Invalid message part")
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        texts.append(part["text"])
                    elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                        refused = True
                    else:
                        raise ValueError("Unsupported message part")
            elif kind == "function_call":
                call_id, name, arguments = item.get("call_id"), item.get("name"), item.get("arguments")
                if (
                    not _nonblank(call_id) or call_id in call_ids
                    or not isinstance(name, str) or not _FUNCTION_NAME.fullmatch(name)
                    or not isinstance(arguments, str)
                    # Token exhaustion can interrupt an argument JSON value.
                    # Preserve that response's usage/status; the engine must
                    # reject incomplete/failed responses before Tool execution.
                    or status == "completed" and not isinstance(parse_json(arguments), dict)
                ):
                    raise ValueError("Invalid function call")
                call_ids.add(call_id)
                calls.append(ToolCall(call_id=call_id, name=name, arguments_json=arguments))
            elif kind == "reasoning":
                if not isinstance(item.get("summary"), list):
                    raise ValueError("Invalid reasoning item")
                if "encrypted_content" in item and item["encrypted_content"] is not None and not isinstance(item["encrypted_content"], str):
                    raise ValueError("Invalid encrypted continuation")
            else:
                # Built-in tools are not permissioned project MCP tools.
                raise ValueError("Unsupported output item")
        return LLMResponse(
            status=status, model_id=reported_model,
            usage=usage,
            output_text="".join(texts) if texts else None,
            tool_calls=tuple(calls), output_items_json=json_text(items),
            refused=refused,
            reported_service_tier=payload.get("service_tier") if (
                type(payload.get("service_tier")) is str and payload["service_tier"] in REPORTED_SERVICE_TIERS) else None,
            billing_context=billing_context,
        )
