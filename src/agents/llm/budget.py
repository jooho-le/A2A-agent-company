"""Shared monotonic invocation budget; no timer restart inside a Tool loop."""

from time import monotonic
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, TokenUsage


class LLMLimits(BaseModel):
    """Operator limits, not new Code Fix or MCP Retry policies."""
    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    max_model_calls: int = Field(default=8, ge=1, strict=True)
    max_tool_calls: int = Field(default=20, ge=0, strict=True)
    max_output_tokens: int = Field(default=2048, ge=16, strict=True)
    max_total_tokens: int | None = Field(default=None, ge=16, strict=True)
    model_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    tool_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_json_bytes: int = Field(default=1_048_576, ge=128, strict=True)


class ExecutionBudget:
    """Trusted host can share this object across roles in a single process.

    Token usage is checked AFTER each response; input billing cannot be reserved
    exactly without provider tokenization. Unknown usage blocks subsequent model
    calls when a token cap is configured. This is not distributed Run persistence.
    """

    def __init__(self, *, runtime_budget_ms: int, limits: LLMLimits | None = None):
        if type(runtime_budget_ms) is not int or runtime_budget_ms <= 0:
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
        self._limits = limits or LLMLimits()
        self._deadline = monotonic() + runtime_budget_ms / 1000
        self._model_calls = 0
        self._tool_calls = 0
        self._known_tokens = 0
        self._usage_complete = True
        self._guard = RLock()

    @property
    def limits(self) -> LLMLimits:
        return self._limits

    @property
    def deadline_monotonic(self) -> float:
        return self._deadline

    @property
    def model_calls(self) -> int:
        with self._guard:
            return self._model_calls

    @property
    def tool_calls(self) -> int:
        with self._guard:
            return self._tool_calls

    @property
    def known_total_tokens(self) -> int:
        with self._guard:
            return self._known_tokens

    @property
    def total_tokens(self) -> int | None:
        with self._guard:
            return self._known_tokens if self._usage_complete else None

    def remaining_seconds(self) -> float:
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise LLMRuntimeError(LLMErrorCode.BUDGET)
        return remaining

    def check(self) -> None:
        with self._guard:
            self.remaining_seconds()
            cap = self.limits.max_total_tokens
            if cap is not None and (not self._usage_complete or self._known_tokens > cap):
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def reserve_model_call(self) -> tuple[int, int, float]:
        with self._guard:
            self.check_model_call()
            output_cap = self.limits.max_output_tokens
            if self.limits.max_total_tokens is not None:
                output_cap = min(output_cap, self.limits.max_total_tokens - self._known_tokens)
                if output_cap < 16:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
            self._model_calls += 1
            return self._model_calls, output_cap, min(self.remaining_seconds(), self.limits.model_timeout_seconds)

    def check_model_call(self) -> None:
        """Non-consuming admission check; never reset a continuation budget."""
        with self._guard:
            self.check()
            if self._model_calls >= self.limits.max_model_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            cap = self.limits.max_total_tokens
            if cap is not None and cap - self._known_tokens < 16:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def account_usage(self, usage: TokenUsage | None) -> None:
        with self._guard:
            if usage is None:
                self._usage_complete = False
            else:
                self._known_tokens += usage.total_tokens

    def check_tool_batch(self, count: int) -> None:
        with self._guard:
            self.check()
            if self._tool_calls + count > self.limits.max_tool_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def reserve_tool_call(self) -> float:
        with self._guard:
            self.check_tool_batch(1)
            self._tool_calls += 1
            return min(self.remaining_seconds(), self.limits.tool_timeout_seconds)
