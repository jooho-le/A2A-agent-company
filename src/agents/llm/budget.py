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
    # Pre-side-effect format mistakes (tool arguments, output schema, role
    # contract, semantic-review format) may be regenerated with fixed feedback
    # at most this many times per engine run, never twice for the same mistake.
    # 0 keeps the original fail-closed behavior. See agents.llm.corrections.
    max_output_corrections: int = Field(default=0, ge=0, le=3, strict=True)
    # Autonomous measurement Runs: when a role/phase returns INPUT_REQUIRED, send
    # one fixed reminder that nobody will answer and the scenario contract holds
    # the fixed answers, then let it continue. Asking again stops as before.
    # 0 keeps questions as an immediate pause (product default).
    max_question_reminders: int = Field(default=0, ge=0, le=1, strict=True)
    # Same call sent again after a provider failure with no model response
    # (connection never made, HTTP 429/5xx). 0 = off; measurement configs only.
    max_provider_retries: int = Field(default=0, ge=0, le=1, strict=True)


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
    pending_token_reservations: dict[int, int] = Field(default_factory=dict)
    accounted_usage: dict[int, TokenUsage | None] = Field(default_factory=dict)
    # 2026-11-18 (Codex review): a provider failure without usage stays unknown in accounted_usage;
    # its conservative upper bound lives here and only feeds the token-cap check.
    unknown_usage_bounds: dict[int, int] = Field(default_factory=dict)
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
        if (not set(self.pending_token_reservations).issubset(pending)
                or any(type(v) is not int or v <= 0 for v in self.pending_token_reservations.values())):
            raise ValueError('invalid pending token reservation')
        if any(type(value) is not int or value < 1 for value in keys) or keys.intersection(pending):
            raise ValueError("budget accounted sequences are invalid")
        if keys.union(pending) != set(range(1, self.model_calls + 1)):
            raise ValueError("budget sequence accounting is incomplete")
        known = sum(value.total_tokens for value in self.accounted_usage.values() if value is not None)
        complete = all(value is not None for value in self.accounted_usage.values())
        if known != self.known_total_tokens or complete is not self.usage_complete:
            raise ValueError("budget token accounting is inconsistent")
        if any(type(key) is not int or self.accounted_usage.get(key, 0) is not None
               or type(value) is not int or value <= 0 for key, value in self.unknown_usage_bounds.items()):
            raise ValueError("budget unknown-usage bounds are inconsistent")
        tool_sequences = self.pending_tool_sequences + self.accounted_tool_sequences
        if (any(type(value) is not int or value < 1 for value in tool_sequences)
                or len(set(tool_sequences)) != len(tool_sequences)
                or set(tool_sequences) != set(range(1, self.tool_calls + 1))):
            raise ValueError("budget Tool sequences are inconsistent")
        return self


class ExecutionBudget:
    """Trusted host can share this object across roles in a single process.

    Actual token usage is checked AFTER each response. Callers may reserve a
    conservative input allowance before sending; concurrent calls share these
    reservations. This is not provider-exact tokenization. Unknown usage blocks subsequent model
    calls when a token cap is configured, unless the engine recorded a conservative upper bound for it. Optional owned-Host persistence records
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
        # Unknown usage with a recorded upper bound does not block the token cap (see BudgetSnapshot).
        self._unknown_bounds = {}
        self._usage_bounded = True
        self._guard = RLock()
        self._snapshot = None
        self._persistence = persistence
        self._utc_clock = utc_clock or (lambda: datetime.now(timezone.utc))
        self._failed = False
        self._pending = set()
        self._pending_tokens = {}
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
            self._pending_tokens = dict(snapshot.pending_token_reservations)
            self._accounted = dict(snapshot.accounted_usage)
            self._unknown_bounds = dict(snapshot.unknown_usage_bounds)
            self._usage_bounded = all(sequence in self._unknown_bounds
                                      for sequence, usage in self._accounted.items() if usage is None)
            self._pending_tools = set(snapshot.pending_tool_sequences)
            self._accounted_tools = set(snapshot.accounted_tool_sequences)
            self._recovered_pending = (bool(self._pending or self._pending_tools)
                or not self._usage_bounded or snapshot.invalidated)
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
                from agents.llm.failure_diagnostics import timeout_diagnostic
                raise LLMRuntimeError(LLMErrorCode.BUDGET, failure_diagnostic=timeout_diagnostic('RUN_BUDGET'))
        remaining = self._deadline - monotonic()
        if remaining <= 0:
            from agents.llm.failure_diagnostics import timeout_diagnostic
            raise LLMRuntimeError(LLMErrorCode.BUDGET, failure_diagnostic=timeout_diagnostic('RUN_BUDGET'))
        return remaining

    def check(self) -> None:
        with self._guard:
            self.remaining_seconds()
            if self._recovered_pending:
                # A process restart cannot establish the external model result.
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            cap = self.limits.max_total_tokens
            if cap is not None and (not self._usage_bounded or self._capped_tokens() > cap):
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def _capped_tokens(self) -> int:
        """Known usage plus the upper bounds of unknown usage (the token-cap view, not billing)."""
        return self._known_tokens + sum(self._unknown_bounds.values())

    def reserve_model_call(self, *, input_tokens: int = 0) -> tuple[int, int, float]:
        with self._guard:
            self.check_model_call(input_tokens=input_tokens)
            output_cap = self.limits.max_output_tokens
            reserved = 0
            if self.limits.max_total_tokens is not None:
                available = self.limits.max_total_tokens - self._capped_tokens() - sum(self._pending_tokens.values())
                output_cap = min(output_cap, available - input_tokens)
                if output_cap < 16:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
                if input_tokens:
                    reserved = input_tokens + output_cap
            sequence = self._model_calls + 1
            reservations = {**self._pending_tokens, **({sequence: reserved} if reserved else {})}
            self._persist(model_calls=sequence, pending_model_sequences=tuple(sorted(self._pending | {sequence})),
                          pending_token_reservations=reservations)
            self._model_calls = sequence
            self._pending.add(sequence)
            self._pending_tokens = reservations
            return sequence, output_cap, min(self.remaining_seconds(), self.limits.model_timeout_seconds)

    def waits_for_pending_reservations(self, *, input_tokens: int = 0) -> bool:
        """True when only other in-flight token reservations block this call (Codex review 2026-11-18).

        Parallel QA and Security share one budget. A reservation still held by the other role
        settles to its real usage, so this call may fit afterwards: the caller should wait within
        the Run deadline instead of reporting exhaustion. Real exhaustion (no pending reservation,
        no room even without them, call/time limits, unbounded unknown usage, restart) is False.
        """
        with self._guard:
            cap = self.limits.max_total_tokens
            if (cap is None or not self._pending_tokens or self._failed or self._recovered_pending
                    or not self._usage_bounded or self._model_calls >= self.limits.max_model_calls
                    or type(input_tokens) is not int or input_tokens < 0):
                return False
            try:
                self._remaining_seconds()
            except LLMRuntimeError:
                return False
            free_without_pending = cap - self._capped_tokens() - input_tokens
            return free_without_pending - sum(self._pending_tokens.values()) < 16 <= free_without_pending

    def check_model_call(self, *, input_tokens: int = 0) -> None:
        """Check that the estimated input and minimum output fit without reserving a call."""
        with self._guard:
            if type(input_tokens) is not int or input_tokens < 0:
                raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
            self.check()
            if self._model_calls >= self.limits.max_model_calls:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            cap = self.limits.max_total_tokens
            if cap is not None and cap - self._capped_tokens() - sum(self._pending_tokens.values()) - input_tokens < 16:
                raise LLMRuntimeError(LLMErrorCode.BUDGET)

    def account_usage(self, usage: TokenUsage | None, *, sequence: int | None = None,
                      bound: int | None = None) -> None:
        """Record a call's measured usage. ``bound`` is only for unknown usage (``usage is None``): a
        conservative upper bound that keeps the token cap enforceable without pretending to be usage."""
        with self._guard:
            if usage is not None and type(usage) is not TokenUsage:
                raise LLMRuntimeError(LLMErrorCode.RESPONSE)
            if bound is not None and (type(bound) is not int or bound <= 0 or usage is not None or sequence is None):
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            if sequence is not None:
                if type(sequence) is not int or sequence < 1 or sequence > self._model_calls:
                    raise LLMRuntimeError(LLMErrorCode.BUDGET)
                if sequence in self._accounted:
                    if self._accounted[sequence] != usage or self._unknown_bounds.get(sequence) != bound:
                        raise LLMRuntimeError(LLMErrorCode.BUDGET)
                    return
            elif self._snapshot is not None or self._pending_tokens:
                # Durable or input-reserving calls must bind their exact reservation.
                raise LLMRuntimeError(LLMErrorCode.BUDGET)
            bounds = {**self._unknown_bounds, **({sequence: bound} if bound is not None else {})}
            if self._snapshot is not None:
                receipts = {**self._accounted, sequence: usage}
                self._persist(
                    known_total_tokens=self._known_tokens + (usage.total_tokens if usage is not None else 0),
                    usage_complete=self._usage_complete and usage is not None,
                    pending_model_sequences=tuple(sorted(self._pending - {sequence})),
                    pending_token_reservations={k:v for k,v in self._pending_tokens.items() if k != sequence},
                    accounted_usage=receipts,
                    unknown_usage_bounds=bounds,
                )
            if usage is None:
                self._usage_complete = False
                self._usage_bounded = self._usage_bounded and bound is not None
            else:
                self._known_tokens += usage.total_tokens
            self._unknown_bounds = bounds
            if sequence is not None:
                self._accounted[sequence] = usage
                self._pending.discard(sequence)
                self._pending_tokens.pop(sequence, None)
                self._recovered_pending = self._recovered_pending and (
                    bool(self._pending or self._pending_tools) or not self._usage_bounded)

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
                bool(self._pending or self._pending_tools) or not self._usage_bounded)

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
