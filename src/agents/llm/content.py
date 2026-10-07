"""Wire-only Source preservation without treating code variables as secrets.

    This recognizes credential literals, not arbitrary secret prose or taint.
    Filesystem/secret access and real MCP enforcement remain sandbox tasks.
"""

import re

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolDefinition
from orchestrator.core.security import redact_data


_SOURCE_INPUTS = {"write_source_file": ("content",), "apply_patch": ("patch",), "write_test_file": ("content",)}
_SOURCE_OUTPUTS = {"read_project_file": ("content",)}
_CREDENTIAL_NAME = (
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*(?:password(?:[_-]?hash)?|hashed[_-]?password|passwd|pwd|"
    r"(?:access|refresh|id|api)[_-]?token|token|api[_-]?key|(?:api|client)[_-]?secret|"
    r"secret(?:[_-]?key)?|authorization|비밀번호|암호|토큰)"
)
_LITERAL = re.compile(
    rf"(?<![\w])[\"']?{_CREDENTIAL_NAME}[\"']?\s*(?::|=)\s*"
    r"(?:[rubfRUBF]{0,2})?(?:\"(?:\\.|[^\"\\])+\"|'(?:\\.|[^'\\])+'|`[^`]+`)",
    re.IGNORECASE,
)
_BARE_CREDENTIAL = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"\$argon2(?:id|i|d)\$|\$2[aby]\$\d{2}\$|"
    r"(?<![\w.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\w.-])",
    re.IGNORECASE,
)


def validate_source_fields(definition: ToolDefinition) -> None:
    for fields, allowed, schema in (
        (definition.source_argument_fields, _SOURCE_INPUTS, definition.input_schema),
        (definition.source_output_fields, _SOURCE_OUTPUTS, definition.output_schema),
    ):
        if not isinstance(fields, tuple) or len(set(fields)) != len(fields):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        if any(name not in allowed.get(definition.name, ()) for name in fields):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        properties = schema.to_dict().get("properties", {})
        if any(properties.get(name, {}).get("type") != "string" for name in fields):
            raise LLMRuntimeError(LLMErrorCode.SCHEMA)


def sanitize_content(data: dict, *, source_fields: tuple[str, ...] = (), reject_secrets: bool = False) -> dict:
    """Copy only; credential-bearing Source is rejected, never silently rewritten.

    Ordinary code assignments like password=request.password remain byte-exact.
    Annotations are restricted to known Source fields by validate_source_fields.
    """
    ordinary = {key: value for key, value in data.items() if key not in source_fields}
    sanitized = redact_data(ordinary)
    if reject_secrets and sanitized != ordinary:
        raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
    for name in source_fields:
        if name not in data:
            continue
        source = data[name]
        if not isinstance(source, str) or _LITERAL.search(source) or _BARE_CREDENTIAL.search(source):
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
        sanitized[name] = source
    return sanitized
