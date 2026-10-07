"""Trusted LLM configuration and bounded JSON, independent of any provider."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
import json
import math
import re
from typing import Literal, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from orchestrator.domain.run_configuration import ModelConfiguration
from orchestrator.domain.states import AgentRole


class LLMErrorCode(str, Enum):
    CONFIGURATION = "LLM_CONFIGURATION_ERROR"
    PROVIDER = "LLM_PROVIDER_ERROR"
    AUTH = "LLM_AUTH_REQUIRED"
    RESPONSE = "LLM_RESPONSE_INVALID"
    REFUSAL = "LLM_REFUSED"
    INCOMPLETE = "LLM_RESPONSE_INCOMPLETE"
    SCHEMA = "LLM_SCHEMA_INVALID"
    BUDGET = "LLM_BUDGET_EXHAUSTED"
    TIMEOUT = "LLM_EXECUTION_TIMEOUT"
    TOOL_POLICY = "LLM_TOOL_POLICY_VIOLATION"
    TOOL_FAILED = "LLM_TOOL_EXECUTION_FAILED"


class LLMRuntimeError(RuntimeError):
    """Stable reason only; never retain a submitted prompt or exception body."""

    def __init__(self, code: LLMErrorCode, *, records: tuple = (), usage=None):
        self.code = LLMErrorCode(code)
        self.records = tuple(records)
        self.usage = usage
        super().__init__(self.code.value)


def _no_constant(_value):
    raise ValueError("Nonfinite JSON")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nonfinite JSON number")
    return parsed


def _check_json_value(value):
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _check_json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _check_json_value(item)
        return
    raise ValueError("JSON types required")


def parse_json(value: str, *, max_bytes: int = 1_048_576) -> object:
    """No NaN, duplicate keys, Markdown repair, or values in errors."""
    try:
        if not isinstance(value, str) or len(value.encode("utf-8")) > max_bytes:
            raise ValueError("JSON size")
        return json.loads(value, parse_constant=_no_constant, parse_float=_finite_float, object_pairs_hook=_unique_object)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LLMRuntimeError(LLMErrorCode.RESPONSE) from None


def json_text(value: object, *, max_bytes: int = 1_048_576) -> str:
    try:
        _check_json_value(value)
        result = json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
        if len(result.encode("utf-8")) > max_bytes:
            raise ValueError("JSON size")
        return result
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise LLMRuntimeError(LLMErrorCode.RESPONSE) from None


@dataclass(frozen=True)
class JsonSchema:
    """Immutable, self-contained runtime model/tool schema, NOT an Artifact.

    Canonical project schemas still apply when executors assemble Artifacts.
    Remote refs are forbidden to keep validation offline and avoid schema SSRF.
    """
    schema_json: str = field(repr=False)

    def __post_init__(self):
        try:
            schema = parse_json(self.schema_json)
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Object schema required")
            self._check_refs(schema)
            Draft202012Validator.check_schema(schema)
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.SCHEMA) from None

    @classmethod
    def from_dict(cls, schema: Mapping[str, object]) -> "JsonSchema":
        return cls(json_text(dict(schema)))

    @staticmethod
    def _check_refs(value):
        if isinstance(value, dict):
            if "$id" in value or "$dynamicRef" in value or "$recursiveRef" in value:
                raise ValueError("Only local static schemas are supported")
            ref = value.get("$ref")
            if ref is not None and (not isinstance(ref, str) or not ref.startswith("#")):
                raise ValueError("External schema reference")
            for item in value.values():
                JsonSchema._check_refs(item)
        elif isinstance(value, list):
            for item in value:
                JsonSchema._check_refs(item)

    def to_dict(self) -> dict:
        return parse_json(self.schema_json)

    def validate(self, value: object) -> None:
        try:
            copied = parse_json(json_text(value))
            Draft202012Validator(self.to_dict(), format_checker=FormatChecker()).validate(copied)
        except Exception:
            raise LLMRuntimeError(LLMErrorCode.SCHEMA) from None

    def require_openai_strict(self) -> None:
        """Reject incompatible shape; never weaken/normalize project schemas."""
        schema = self.to_dict()
        try:
            if "anyOf" in schema:
                raise ValueError("Root union")

            def walk(node):
                if not isinstance(node, dict):
                    raise ValueError("Boolean subschema unsupported")
                supported = {
                    "$schema", "$defs", "$ref", "type", "title", "description",
                    "properties", "required", "additionalProperties", "anyOf", "items",
                    "enum", "const", "pattern", "format", "minLength", "maxLength",
                    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                    "multipleOf", "minItems", "maxItems",
                }
                if set(node) - supported:
                    raise ValueError("Unsupported strict constraint")
                kind = node.get("type")
                if "properties" in node and not (kind == "object" or isinstance(kind, list) and "object" in kind):
                    raise ValueError("Explicit object type required")
                if "items" in node and not (kind == "array" or isinstance(kind, list) and "array" in kind):
                    raise ValueError("Explicit array type required")
                if kind == "object" or isinstance(kind, list) and "object" in kind:
                    properties = node.get("properties", {})
                    required = node.get("required", [])
                    if node.get("additionalProperties") is not False or set(required) != set(properties):
                        raise ValueError("Object must be closed and all fields required")
                    for child in properties.values():
                        walk(child)
                for child in node.get("$defs", {}).values():
                    walk(child)
                for child in node.get("anyOf", []):
                    walk(child)
                if "items" in node:
                    walk(node["items"])

            walk(schema)
        except (ValueError, TypeError, AttributeError, RecursionError):
            raise LLMRuntimeError(LLMErrorCode.SCHEMA) from None


@dataclass(frozen=True)
class StructuredOutput:
    name: str
    schema: JsonSchema

    def __post_init__(self):
        if not isinstance(self.name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.name):
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str = field(repr=False)
    input_schema: JsonSchema
    output_schema: JsonSchema
    # Trusted host declaration only; model/discovery descriptions cannot grant it.
    source_argument_fields: tuple[str, ...] = ()
    source_output_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolCall:
    call_id: str = field(repr=False)
    name: str = field(repr=False)
    arguments_json: str = field(repr=False)


@dataclass(frozen=True)
class ToolContext:
    """Host binding only; not a filesystem grant or an MCP session yet."""
    role: AgentRole
    workspace_id: str | None = field(repr=False)
    deadline_monotonic: float


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cached_input_tokens: int | None = None
    reasoning_output_tokens: int | None = None

    def __post_init__(self):
        values = (self.input_tokens, self.output_tokens, self.total_tokens)
        optional = (self.cached_input_tokens, self.reasoning_output_tokens)
        if any(type(v) is not int or v < 0 for v in values):
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if any(v is not None and (type(v) is not int or v < 0) for v in optional):
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if self.cached_input_tokens is not None and self.cached_input_tokens > self.input_tokens:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)
        if self.reasoning_output_tokens is not None and self.reasoning_output_tokens > self.output_tokens:
            raise LLMRuntimeError(LLMErrorCode.RESPONSE)


@dataclass(frozen=True)
class LLMRequest:
    model: ModelConfiguration
    system_prompt: str = field(repr=False)
    input_items_json: str = field(repr=False)
    tools: tuple[ToolDefinition, ...]
    output: StructuredOutput
    max_output_tokens: int
    timeout_seconds: float


@dataclass(frozen=True)
class LLMResponse:
    status: Literal["completed", "incomplete", "failed"]
    model_id: str = field(repr=False)
    usage: TokenUsage | None
    output_text: str | None = field(default=None, repr=False)
    tool_calls: tuple[ToolCall, ...] = ()
    # Opaque reasoning/function-call continuation is wire-only, never Trace.
    output_items_json: str = field(default="[]", repr=False)
    refused: bool = False


class LLMProvider(Protocol):
    name: str

    def validate_configuration(self, model: ModelConfiguration, output: StructuredOutput) -> None: ...

    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class ToolExecutor(Protocol):
    async def execute(self, call: ToolCall, arguments: dict, context: ToolContext) -> object: ...


@dataclass(frozen=True)
class UsageRecord:
    """Accounting only: no prompt, Source, call arguments, output, or secret."""
    sequence: int
    role: AgentRole
    requested_model: ModelConfiguration
    reported_model_id: str | None = field(repr=False)
    outcome: str
    duration_ms: int
    usage: TokenUsage | None
    # Price not selected: unknown, never an invented zero-dollar estimate.
    cost_usd: None = None


@dataclass(frozen=True)
class LLMResult:
    data_json: str = field(repr=False)
    records: tuple[UsageRecord, ...]
    tool_calls: int

    @property
    def data(self) -> dict:
        return parse_json(self.data_json)
