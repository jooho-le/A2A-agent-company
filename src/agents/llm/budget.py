"""Shared monotonic invocation budget; no timer restart inside a Tool loop."""

from collections.abc import Callable
from datetime import datetime, timezone
from time import monotonic
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, UUID4, model_validator

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


class BudgetSnapshot(BaseModel):
    """Typed accounting state only; prompts, Tool data and secrets cannot enter."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    run_id: UUID4
    configuration_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    limits: LLMLimits
    created_at_utc: datetime
    deadline_utc: datetime
    last_observed_utc: datetime
    revision: int = Field(default=0, ge=0, strict=True)
    model_calls: int = Field(default=0, ge=0, strict=True)
    tool_calls: int = Field(default=0, ge=0, strict=True)
    known_total_tokens: int = Field(default=0, ge=0, strict=True)
    usage_complete: bool = Field(default=True, strict=True)
    pending_model_sequences: tuple[int, ...] = ()
    accounted_usage: dict[int, TokenUsage | None] = Field(default_factory=dict)
    pending_tool_sequences: tuple[int, ...] = ()
    accounted_tool_sequences: tuple[int, ...] = ()
    invalidated: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def coherent_accounting(self):
        clocks = (self.created_at_utc, self.deadline_utc, self.last_observed_utc)
        if any(value.tzinfo is None or value.utcoffset() is None for value in clocks):
            raise ValueError("budget timestamps require timezone")
        if self.deadline_utc <= self.created_at_utc or self.last_observed_utc < self.created_at_utc:
            raise ValueError("budget timestamps are inconsistent")
        if self.model_calls > self.limits.max_model_calls or self.tool_calls > self.limits.max_tool_calls:
            raise ValueError("budget reservation counters exceed limits")
        pending = self.pending_model_sequences
        if any(type(value) is not int or value < 1 for value in pending) or len(set(pending)) != len(pending):
            raise ValueError("budget pending sequences are invalid")
        keys = set(self.accounted_usage)
        if any(type(value) is not int or value < 1 for value in keys) or keys.intersection(pending):
            raise ValueError("budget accounted sequences are invalid")
        if keys.union(pending) != set(range(1, self.model_calls + 1)):
            raise ValueError("budget sequence accounting is incomplete")
        known = sum(value.total_tokens for value in self.accounted_usage.values() if value is not None)
        complete = all(value is not None for value in self.accounted_usage.values())
        if known != self.known_total_tokens or complete is not self.usage_complete:
            raise ValueError("budget token accounting is inconsistent")
        tool_sequences = self.pending_tool_sequences + self.accounted_tool_sequences
        if (any(type(value) is not int or value < 1 for value in tool_sequences)
                or len(set(tool_sequences)) != len(tool_sequences)
                or set(tool_sequences) != set(range(1, self.tool_calls + 1))):
            raise ValueError("budget Tool sequences are inconsistent")
        return self


class ExecutionBudget:
    """Trusted host can share this object across roles in a single process.

    Token usage is checked AFTER each response; input billing cannot be reserved
    exactly without provider tokenization. Unknown usage blocks subsequent model
    calls when a token cap is configured. Optional owned-Host persistence records
    reservations before effects; it is not distributed Run coordination.
    """

    def __init__(self, *, runtime_budget_ms: int, limits: LLMLimits | None = None,
                 snapshot: BudgetSnapshot | None = None,
                 persistence: Callable[[BudgetSnapshot, int], BudgetSnapshot] | None = None,
                 utc_clock: Callable[[], datetime] | None = None):
        if type(runtime_budget_ms) is not int or runtime_budget_ms <= 0:
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
        self._limits = limits or LLMLimits()
        self._deadline = monotonic() + runtime_budget_ms / 1000
        self._model_calls = 0
        self._tool_calls = 0
        self._known_tokens = 0
        self._usage_complete = True
        self._guard = RLock()
        self._snapshot = None
        self._persistence = persistence
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._failed = False
        self._pending = set()
        self._accounted = {}
        self._recovered_pending = False
        self._pending_tools = set()
        self._accounted_tools = set()
        self._last_utc_observed = None
        if snapshot is not None:
            if type(snapshot) is not BudgetSnapshot or persistence is None or snapshot.limits != self._limits:
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            now = self._utc_clock()
            if now < snapshot.last_observed_utc:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            remaining = (snapshot.deadline_utc - now).total_seconds()
            if remaining <= 0:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            self._deadline = monotonic() + remaining
            self._snapshot = snapshot
            self._last_utc_observed = now
            self._model_calls = snapshot.model_calls
            self._tool_calls = snapshot.tool_calls
            self._known_tokens = snapshot.known_total_tokens
            self._usage_complete = snapshot.usage_complete
            self._pending = set(snapshot.pending_model_sequences)
            self._accounted = dict(snapshot.accounted_usage)
            self._pending_tools = set(snapshot.pending_tool_sequences)
            self._accounted_tools = set(snapshot.accounted_tool_sequences)
            self._recovered_pending = (bool(self._pending or self._pending_tools)
                or not self._usage_complete or snapshot.invalidated)
        elif persistence is not None:
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)

    @property
    def snapshot(self) -> BudgetSnapshot | None:
        with self._guard:
            return self._snapshot.model_copy(deep=True) if self._snapshot is not None else None

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
        with self._guard:
            return self._remaining_seconds()

    def _remaining_seconds(self) -> float:
        if self._failed:
            raise LLMRuntimeError(LLMErrorCode.BUDGET)
        if self._snapshot is not None:
            now = self._utc_clock()
            if now < self._snapshot.last_observed_utc or now < self._last_utc_observed:
                self.invalidate()
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            self._last_utc_observed = now
            if now >= self._snapshot.deadline_utc:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            raise LLMRuntimeError(LLMErrorCode.BUDGET)
        return remaining

    def check(self) -> None:
        with self._guard:
            self.remaining_seconds()
            if self._recovered_pending:
                # A process restart cannot establish the external model result.
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
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
            sequence = self._model_calls + 1
            self._persist(model_calls=sequence, pending_model_sequences=tuple(sorted(self._pending | {sequence})))
            self._model_calls = sequence
            self._pending.add(sequence)
            return sequence, output_cap, min(self.remaining_seconds(), self.limits.model_timeout_seconds)

    def check_model_call(self) -> None:
        """Non-consuming admission check; never reset a continuation budget."""
        with self._guard:
            self.check()
            if self._model_calls >= self.limits.max_model_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            cap = self.limits.max_total_tokens
            if cap is not None and cap - self._known_tokens < 16:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def account_usage(self, usage: TokenUsage | None, *, sequence: int | None = None) -> None:
        with self._guard:
            if usage is not None and type(usage) is not TokenUsage:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            if sequence is not None:
                if type(sequence) is not int or sequence < 1 or sequence > self._model_calls:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
                if sequence in self._accounted:
                    if self._accounted[sequence] != usage:
                        raise LLMRuntimeError(LLMErrorCode.BUDGET)
                    return
            elif self._snapshot is not None:
                # Durable engine calls must bind their exact reservation.
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            if self._snapshot is not None:
                receipts = {**self._accounted, sequence: usage}
                self._persist(
                    known_total_tokens=self._known_tokens + (usage.total_tokens if usage is not None else 0),
                    usage_complete=self._usage_complete and usage is not None,
                    pending_model_sequences=tuple(sorted(self._pending - {sequence})),
                    accounted_usage=receipts,
                )
            if usage is None:
                self._usage_complete = False
            else:
                self._known_tokens += usage.total_tokens
            if sequence is not None:
                self._accounted[sequence] = usage
                self._pending.discard(sequence)
                self._recovered_pending = self._recovered_pending and (
                    bool(self._pending or self._pending_tools) or not self._usage_complete)

    def check_tool_batch(self, count: int) -> None:
        with self._guard:
            self.check()
            if self._tool_calls + count > self.limits.max_tool_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def reserve_tool_call(self) -> float:
        """Legacy float API; durable callers should acknowledge tracked calls."""
        _, timeout = self.reserve_tracked_tool_call()
        return timeout

    def reserve_tracked_tool_call(self) -> tuple[int, float]:
        with self._guard:
            self.check_tool_batch(1)
            sequence = self._tool_calls + 1
            self._persist(tool_calls=sequence,
                          pending_tool_sequences=tuple(sorted(self._pending_tools | {sequence})))
            self._tool_calls = sequence
            self._pending_tools.add(sequence)
            return sequence, min(self.remaining_seconds(), self.limits.tool_timeout_seconds)

    def account_tool_call(self, sequence: int) -> None:
        with self._guard:
            if type(sequence) is not int or not 1 <= sequence <= self._tool_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            if sequence in self._accounted_tools:
                return
            self._persist(pending_tool_sequences=tuple(sorted(self._pending_tools - {sequence})),
                          accounted_tool_sequences=tuple(sorted(self._accounted_tools | {sequence})))
            self._pending_tools.discard(sequence)
            self._accounted_tools.add(sequence)
            self._recovered_pending = self._recovered_pending and (
                bool(self._pending or self._pending_tools) or not self._usage_complete)

    def invalidate(self) -> None:
        """Stop live side effects when a mandatory accounting sink fails."""
        with self._guard:
            try:
                self._persist(invalidated=True)
            except LLMRuntimeError:
                # The broken durable writer must never permit another effect.
                pass
            self._failed = True

    def _persist(self, **changes) -> None:
        if self._snapshot is None:
            return
        try:
            values = {**self._snapshot.model_dump(), **changes}
            candidate = BudgetSnapshot.model_validate(values)
            saved = self._persistence(candidate, self._snapshot.revision)
            if type(saved) is not BudgetSnapshot or saved.revision != self._snapshot.revision + 1:
                raise ValueError("invalid budget persistence receipt")
            self._snapshot = saved
            if self._last_utc_observed is not None:
                self._last_utc_observed = max(self._last_utc_observed, saved.last_observed_utc)
        except Exception:
            self._failed = True
            raise LLMRuntimeError(LLMErrorCode.BUDGET) from None
