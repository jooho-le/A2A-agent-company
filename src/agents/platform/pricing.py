"""Read-only, operator-approved text pricing; no live price or model selection.

A quote uses only observed usage and exact approved identities. Missing billing
details remain unknown, not a zero-dollar estimate. This is not an invoice or an
attestation that an operator's rates match any current external price schedule.
"""

from dataclasses import dataclass, field
from decimal import Decimal, Inexact, Rounded, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
from urllib.parse import urlsplit

from agents.llm.content import sanitize_content
from agents.llm.contracts import TokenUsage, UsageRecord, parse_json
from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


MAX_PRICE_BOOK_BYTES = 262_144
CALCULATION_VERSION = "USD_PER_MILLION_V1"
BILLING_SCOPE = "OPENAI_RESPONSES_GLOBAL_TEXT"
_ROOT_FIELDS = frozenset({"schemaVersion", "priceBookId", "priceBookVersion", "sourceRef",
    "currency", "billingScope", "entries"})
_ENTRY_FIELDS = frozenset({"entryId", "provider", "requestedModelId", "reportedModelId",
    "serviceTier", "minInputTokens", "maxInputTokens", "cacheWriteMode", "inputUsdPerMillion",
    "cachedInputUsdPerMillion", "cacheWriteUsdPerMillion", "outputUsdPerMillion"})
_TIERS = frozenset({"default", "flex", "priority", "scale", "fast", "ultrafast"})
_REASONS = frozenset({"KNOWN", "USAGE_UNKNOWN", "MODEL_UNMATCHED", "TIER_UNKNOWN",
    "CONTEXT_UNKNOWN", "RATE_UNMATCHED", "TOKEN_DETAILS_UNKNOWN"})
_RATE = re.compile(r"(?:0|[1-9][0-9]{0,5})(?:\.[0-9]{1,12})?\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,127}\Z", re.ASCII)
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}\Z", re.ASCII)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}\Z", re.ASCII)


class PricingError(ValueError):
    """Stable reason only; never retain file paths or submitted price data."""

    code = "LLM_PRICING_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _matches(pattern, value):
    if type(value) is not str or pattern.fullmatch(value) is None:
        raise ValueError


def _source(value):
    if (type(value) is not str or not 1 <= len(value) <= 1024 or not value.isascii()
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
            or any(char in value for char in ("\\", "?", "#"))):
        raise ValueError
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"developers.openai.com", "platform.openai.com"}
            or parsed.username is not None or parsed.password is not None or parsed.port is not None
            or parsed.netloc != parsed.hostname or parsed.query or parsed.fragment):
        raise ValueError


def _definition(value):
    if (type(value) is not dict or set(value) != _ROOT_FIELDS
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1):
        raise ValueError
    _matches(_IDENTIFIER, value["priceBookId"])
    _matches(_VERSION, value["priceBookVersion"])
    _source(value["sourceRef"])
    if (type(value["currency"]) is not str or value["currency"] != "USD"
            or type(value["billingScope"]) is not str or value["billingScope"] != BILLING_SCOPE):
        raise ValueError
    entries = value["entries"]
    if type(entries) is not list or not 1 <= len(entries) <= 128:
        raise ValueError
    identities, ranges = set(), {}
    for entry in entries:
        if type(entry) is not dict or set(entry) != _ENTRY_FIELDS:
            raise ValueError
        _matches(_IDENTIFIER, entry["entryId"])
        _matches(_MODEL, entry["requestedModelId"])
        _matches(_MODEL, entry["reportedModelId"])
        if (entry["entryId"] in identities or type(entry["provider"]) is not str or entry["provider"] != "openai"
                or type(entry["serviceTier"]) is not str or entry["serviceTier"] not in _TIERS):
            raise ValueError
        identities.add(entry["entryId"])
        minimum, maximum = entry["minInputTokens"], entry["maxInputTokens"]
        if (type(minimum) is not int or minimum < 0
                or maximum is not None and (type(maximum) is not int or maximum < minimum)):
            raise ValueError
        for key in ("inputUsdPerMillion", "cachedInputUsdPerMillion", "outputUsdPerMillion"):
            _matches(_RATE, entry[key])
        mode, write_rate = entry["cacheWriteMode"], entry["cacheWriteUsdPerMillion"]
        if type(mode) is not str:
            raise ValueError
        if mode == "NOT_APPLICABLE":
            if write_rate is not None:
                raise ValueError
        elif mode == "METERED":
            _matches(_RATE, write_rate)
        else:
            raise ValueError
        key = (entry["provider"], entry["requestedModelId"], entry["reportedModelId"], entry["serviceTier"])
        for old_minimum, old_maximum in ranges.setdefault(key, []):
            if ((maximum is None or old_minimum <= maximum)
                    and (old_maximum is None or minimum <= old_maximum)):
                raise ValueError
        ranges[key].append((minimum, maximum))
    if len(_json(value).encode("utf-8")) > MAX_PRICE_BOOK_BYTES:
        raise ValueError
    sanitize_content(value, reject_secrets=True)
    return value


def _descriptor(value):
    if (type(value) is not dict or set(value) != {"calculationVersion", "priceBook"}
            or type(value["calculationVersion"]) is not str
            or value["calculationVersion"] != CALCULATION_VERSION):
        raise ValueError
    _definition(value["priceBook"])
    # Reparse to detach all nested containers and reject non-JSON/subclass data.
    return {"calculationVersion": CALCULATION_VERSION,
            "priceBook": _definition(parse_json(_json(value["priceBook"]), max_bytes=MAX_PRICE_BOOK_BYTES))}


def _parents(path):
    if type(path) not in (Path, type(Path())) or not path.is_absolute():
        raise ValueError
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError


def _read_regular(path):
    descriptor = None
    try:
        _parents(path)
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or not 1 <= before.st_size <= MAX_PRICE_BOOK_BYTES):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(opened) != signature(before) or not stat.S_ISREG(opened.st_mode):
            raise ValueError
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65_536, MAX_PRICE_BOOK_BYTES + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PRICE_BOOK_BYTES:
                raise ValueError
            chunks.append(chunk)
        _parents(path)
        if (size != opened.st_size or signature(os.fstat(descriptor)) != signature(opened)
                or signature(path.lstat()) != signature(opened)):
            raise ValueError
        return path.resolve(strict=True), b"".join(chunks)
    finally:
        if descriptor is not None:
            os.close(descriptor)


@dataclass(frozen=True)
class CostQuote:
    cost_usd: str | None
    entry_id: str | None
    reason: str

    def __post_init__(self):
        try:
            if type(self.reason) is not str or self.reason not in _REASONS:
                raise ValueError
            if self.entry_id is not None:
                _matches(_IDENTIFIER, self.entry_id)
            if self.reason == "KNOWN":
                if (type(self.cost_usd) is not str or re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", self.cost_usd) is None
                        or self.entry_id is None):
                    raise ValueError
            elif self.cost_usd is not None:
                raise ValueError
        except Exception:
            raise PricingError() from None


def _record(value):
    if (type(value) is not UsageRecord or value.cost_usd is not None
            or type(value.sequence) is not int or value.sequence < 1
            or type(value.role) is not AgentRole or type(value.duration_ms) is not int or value.duration_ms < 0
            or type(value.outcome) is not str or not value.outcome
            or type(value.requested_model) is not ModelConfiguration):
        raise ValueError
    model = value.requested_model
    checked = ModelConfiguration.model_validate(model.model_dump(by_alias=True), strict=True)
    if checked != model:
        raise ValueError
    for item in (value.reported_model_id, value.reported_service_tier, value.billing_context):
        if item is not None and type(item) is not str:
            raise ValueError
    if value.usage is not None:
        if type(value.usage) is not TokenUsage:
            raise ValueError
        TokenUsage.__post_init__(value.usage)
    return value


def _quote(descriptor, record):
    book = descriptor["priceBook"]
    record = _record(record)
    usage = record.usage
    if usage is None:
        return CostQuote(None, None, "USAGE_UNKNOWN")
    model = record.requested_model
    requested = model.model_revision or model.model_id
    model_entries = [entry for entry in book["entries"] if (entry["provider"], entry["requestedModelId"],
        entry["reportedModelId"]) == (model.provider, requested, record.reported_model_id)]
    if not model_entries:
        return CostQuote(None, None, "MODEL_UNMATCHED")
    if record.reported_service_tier not in _TIERS:
        return CostQuote(None, None, "TIER_UNKNOWN")
    if record.billing_context != BILLING_SCOPE:
        return CostQuote(None, None, "CONTEXT_UNKNOWN")
    entry = next((entry for entry in model_entries if entry["serviceTier"] == record.reported_service_tier
        and entry["minInputTokens"] <= usage.input_tokens
        and (entry["maxInputTokens"] is None or usage.input_tokens <= entry["maxInputTokens"])), None)
    if entry is None:
        return CostQuote(None, None, "RATE_UNMATCHED")
    cached, write = usage.cached_input_tokens, usage.cache_write_input_tokens
    if (cached is None or entry["cacheWriteMode"] == "METERED" and write is None
            or entry["cacheWriteMode"] == "NOT_APPLICABLE" and write not in (None, 0)):
        return CostQuote(None, entry["entryId"], "TOKEN_DETAILS_UNKNOWN")
    write = 0 if write is None else write
    # Input-cache-write partitions are validated independently of output; the
    # reasoning count is already part of output and must never be added again.
    with localcontext() as context:
        # Rates have at most 6 whole and 12 fractional digits. Include all
        # actual counter digits plus ample rate/sum margin rather than silently
        # rounding, or rejecting a valid large observed integer solely at 80.
        context.prec = max(80, len(str(max(usage.input_tokens, usage.output_tokens, 1))) + 32)
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        total = (Decimal(usage.input_tokens - cached - write) * Decimal(entry["inputUsdPerMillion"])
                 + Decimal(cached) * Decimal(entry["cachedInputUsdPerMillion"])
                 + Decimal(write) * Decimal(entry["cacheWriteUsdPerMillion"] or "0")
                 + Decimal(usage.output_tokens) * Decimal(entry["outputUsdPerMillion"])) / Decimal(1_000_000)
    text = format(total, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return CostQuote(text, entry["entryId"], "KNOWN")


def quote_frozen_usage(descriptor: dict, record: UsageRecord) -> CostQuote:
    """Pure historical calculation from validated, frozen policy and usage."""
    try:
        return _quote(_descriptor(descriptor), record)
    except Exception:
        raise PricingError() from None


def validate_frozen_price_book(descriptor: dict) -> None:
    """Pure closed policy validation, including a zero-call historical Run."""
    try:
        _descriptor(descriptor)
    except Exception:
        raise PricingError() from None


@dataclass(frozen=True, init=False, repr=False)
class ApprovedPriceBook:
    """Only load_price_book seals a read-only approval; construction is inert."""

    _definition_json: str = field(repr=False)
    _approval_path: Path = field(repr=False)

    def __init__(self):
        raise PricingError()

    def __repr__(self):
        return "ApprovedPriceBook()"

    def _definition(self):
        try:
            if type(self) is not ApprovedPriceBook or type(self._definition_json) is not str:
                raise ValueError
            return _definition(parse_json(self._definition_json, max_bytes=MAX_PRICE_BOOK_BYTES))
        except Exception:
            raise PricingError() from None

    @property
    def descriptor(self):
        return {"calculationVersion": CALCULATION_VERSION, "priceBook": self._definition()}

    @property
    def sha256(self):
        return _digest(self.descriptor)

    @property
    def approval_path(self):
        return self._approval_path

    def verify_integrity(self):
        """Explicit Host re-read; quote itself does not access any file."""
        try:
            data = self._definition()
            path, content = _read_regular(self._approval_path)
            current = _definition(parse_json(content.decode("utf-8"), max_bytes=MAX_PRICE_BOOK_BYTES))
            if path != self._approval_path or _json(current) != _json(data):
                raise ValueError
        except Exception:
            raise PricingError() from None

    def quote(self, record: UsageRecord) -> CostQuote:
        return quote_frozen_usage(self.descriptor, record)


def load_price_book(path) -> ApprovedPriceBook:
    """Read only approved JSON; never fetch rates, select a model or execute."""
    try:
        canonical, content = _read_regular(Path(path).absolute())
        data = _definition(parse_json(content.decode("utf-8"), max_bytes=MAX_PRICE_BOOK_BYTES))
        approved = object.__new__(ApprovedPriceBook)
        object.__setattr__(approved, "_definition_json", _json(data))
        object.__setattr__(approved, "_approval_path", canonical)
        return approved
    except Exception:
        raise PricingError() from None
