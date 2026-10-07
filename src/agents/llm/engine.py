"""Bounded model → approved Tool → structured JSON loop; no product verdict."""

import asyncio
from collections.abc import Callable
from time import monotonic

from agents.llm.budget import ExecutionBudget
from agents.llm.content import sanitize_content, validate_source_fields
from agents.llm.contracts import (
    LLMErrorCode, LLMProvider, LLMRequest, LLMResult, LLMRuntimeError,
    StructuredOutput, ToolContext, ToolDefinition, ToolExecutor, UsageRecord,
    json_text, parse_json,
)
from agents.roles.contracts import get_role_contract
from agents.roles.prompts import PreparedRolePrompt, build_system_prompt
from orchestrator.core.security import redact_data, redact_text
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


class LLMEngine:
    """Tool executors are trusted injection points; MCP/ACL arrive in 20–29.

    No automatic model or Tool retry: a failed/uncertain write is never repeated.
    Accounting is in-memory only; the optional sink must be a trusted host writer.
    The default A2A server stays Bootstrap/REJECTED until role executors exist.
    """

    def __init__(
        self, *, role: AgentRole, provider: LLMProvider,
        tools: tuple[ToolDefinition, ...] = (), tool_executor: ToolExecutor | None = None,
    ):
        self._role = get_role_contract(role).role
        self._provider = provider
        allowed = get_role_contract(role).allowed_tool_names
        if not isinstance(tools, tuple) or any(tool.name not in allowed for tool in tools):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        if len({tool.name for tool in tools}) != len(tools) or tools and tool_executor is None:
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        for tool in tools:
            validate_source_fields(tool)
        self._tools = tools
        self._definitions = {tool.name: tool for tool in tools}
        self._executor = tool_executor

    async def run(
        self, *, prompt: PreparedRolePrompt, model: ModelConfiguration,
        output: StructuredOutput, budget: ExecutionBudget,
        workspace_id: str | None = None,
        usage_sink: Callable[[UsageRecord], None] | None = None,
    ) -> LLMResult:
        records: list[UsageRecord] = []
        seen_calls: set[str] = set()
        executed = 0
        try:
            if (
                prompt.role != self._role
                or prompt.version != get_role_contract(self._role).version
                or prompt.system_prompt != build_system_prompt(self._role)
                or model.provider != self._provider.name
            ):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            if workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id.strip()):
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            self._provider.validate_configuration(model, output)
            limit = budget.limits.max_json_bytes
            task_input = parse_json(prompt.input_json, max_bytes=limit)
            if not isinstance(task_input, dict):
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            items = [{"role": "user", "content": json_text(redact_data(task_input), max_bytes=limit)}]
            while True:
                # Includes wire-only reasoning continuation; never send it to a sink.
                history = json_text(items, max_bytes=limit)
                if len(history.encode("utf-8")) + len(prompt.system_prompt.encode("utf-8")) > limit:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
                sequence, output_cap, timeout = budget.reserve_model_call()
                request = LLMRequest(
                    model=model, system_prompt=prompt.system_prompt,
                    input_items_json=history, tools=self._tools, output=output,
                    max_output_tokens=output_cap, timeout_seconds=timeout,
                )
                started = monotonic()
                try:
                    response = await asyncio.wait_for(self._provider.complete(request), timeout=timeout)
                except asyncio.CancelledError as canceled:
                    budget.account_usage(None)
                    record = UsageRecord(sequence, self._role, model, None, "canceled", self._duration(started), None)
                    records.append(record)
                    try:
                        if usage_sink is not None:
                            usage_sink(record)
                    finally:
                        raise canceled from None
                except Exception as error:
                    code = error.code if isinstance(error, LLMRuntimeError) else (
                        LLMErrorCode.TIMEOUT if isinstance(error, asyncio.TimeoutError) else LLMErrorCode.PROVIDER
                    )
                    known_usage = error.usage if isinstance(error, LLMRuntimeError) else None
                    budget.account_usage(known_usage)
                    record = UsageRecord(sequence, self._role, model, None, code.value, self._duration(started), known_usage)
                    records.append(record)
                    if usage_sink is not None:
                        usage_sink(record)
                    raise LLMRuntimeError(code) from None
                budget.account_usage(response.usage)
                outcome = "refused" if response.refused else (
                    response.status if response.status in ("completed", "incomplete", "failed") else LLMErrorCode.RESPONSE.value
                )
                reported_model = redact_text(response.model_id)[:256] if isinstance(response.model_id, str) else None
                record = UsageRecord(
                    sequence, self._role, model, reported_model, outcome,
                    self._duration(started), response.usage,
                )
                records.append(record)
                if usage_sink is not None:
                    usage_sink(record)
                budget.check()
                if response.refused:
                    raise LLMRuntimeError(LLMErrorCode.REFUSAL)
                if response.status == "incomplete":
                    raise LLMRuntimeError(LLMErrorCode.INCOMPLETE)
                if response.status != "completed":
                    raise LLMRuntimeError(LLMErrorCode.PROVIDER)
                if response.tool_calls:
                    budget.check_tool_batch(len(response.tool_calls))
                    prepared = []
                    batch_ids = set()
                    # Validate the ENTIRE batch before the first side effect.
                    for call in response.tool_calls:
                        definition = self._definitions.get(call.name)
                        if (
                            definition is None or not isinstance(call.call_id, str) or not call.call_id.strip()
                            or call.call_id in seen_calls or call.call_id in batch_ids
                        ):
                            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
                        arguments = parse_json(call.arguments_json, max_bytes=limit)
                        definition.input_schema.validate(arguments)
                        if not isinstance(arguments, dict):
                            raise LLMRuntimeError(LLMErrorCode.SCHEMA)
                        if "workspaceId" in arguments and arguments["workspaceId"] != workspace_id:
                            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
                        # Never pass recognized secrets from model output into Tools.
                        sanitize_content(arguments, source_fields=definition.source_argument_fields, reject_secrets=True)
                        prepared.append((call, definition, arguments))
                        batch_ids.add(call.call_id)
                    continuation = parse_json(response.output_items_json, max_bytes=limit)
                    self._validate_continuation(continuation, response.tool_calls)
                    items.extend(continuation)
                    for call, definition, arguments in prepared:
                        timeout = budget.reserve_tool_call()
                        seen_calls.add(call.call_id)
                        context = ToolContext(self._role, workspace_id, budget.deadline_monotonic)
                        try:
                            tool_result = await asyncio.wait_for(
                                self._executor.execute(call, arguments, context), timeout=timeout,
                            )
                        except asyncio.TimeoutError:
                            raise LLMRuntimeError(LLMErrorCode.TIMEOUT) from None
                        except Exception:
                            raise LLMRuntimeError(LLMErrorCode.TOOL_FAILED) from None
                        executed += 1
                        budget.check()
                        definition.output_schema.validate(tool_result)
                        # Transcript copy is redacted, evidence assembly uses host data later.
                        tool_output = json_text(sanitize_content(
                            tool_result, source_fields=definition.source_output_fields,
                        ), max_bytes=limit)
                        definition.output_schema.validate(parse_json(tool_output, max_bytes=limit))
                        items.append({"type": "function_call_output", "call_id": call.call_id, "output": tool_output})
                    continue
                if response.output_text is None:
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                data = parse_json(response.output_text, max_bytes=limit)
                output.schema.validate(data)
                sanitized = redact_data(data)
                output.schema.validate(sanitized)
                budget.check()
                return LLMResult(json_text(sanitized, max_bytes=limit), tuple(records), executed)
        except LLMRuntimeError as error:
            raise LLMRuntimeError(error.code, records=tuple(records)) from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE, records=tuple(records)) from None

    @staticmethod
    def _duration(started: float) -> int:
        return max(0, int((monotonic() - started) * 1000))

    @staticmethod
    def _validate_continuation(items, calls) -> None:
        """No System/User injection or hidden calls in a provider continuation."""
        if not isinstance(items, list) or not items:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        expected = {call.call_id: (call.name, call.arguments_json) for call in calls}
        found = set()
        item_ids = set()
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in (None, "completed"):
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            identity = item.get("id")
            if not isinstance(identity, str) or not identity.strip() or identity in item_ids:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            item_ids.add(identity)
            kind = item.get("type")
            if kind == "function_call":
                call_id = item.get("call_id")
                if (
                    set(item) - {"id", "type", "status", "call_id", "name", "arguments"}
                    or not isinstance(call_id, str) or call_id in found
                    or call_id not in expected
                    or (item.get("name"), item.get("arguments")) != expected[call_id]
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                found.add(call_id)
            elif kind == "message":
                if (
                    set(item) - {"id", "type", "status", "role", "content"}
                    or item.get("role") != "assistant" or not isinstance(item.get("content"), list)
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
                for part in item["content"]:
                    if (
                        not isinstance(part, dict) or part.get("type") != "output_text"
                        or not isinstance(part.get("text"), str)
                        or set(part) - {"type", "text", "annotations", "logprobs"}
                    ):
                        raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            elif kind == "reasoning":
                if (
                    set(item) - {"id", "type", "status", "summary", "encrypted_content"}
                    or not isinstance(item.get("summary"), list)
                    or item.get("encrypted_content") is not None and not isinstance(item["encrypted_content"], str)
                ):
                    raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            else:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if found != set(expected):
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
