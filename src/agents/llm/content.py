"""Wire-only Source preservation without treating code variables as secrets.

    This recognizes credential literals, not arbitrary secret prose or taint.
    Filesystem/secret access and real MCP enforcement remain sandbox tasks.
"""

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolDefinition
from orchestrator.core.security import redact_data
from orchestrator.core.source_credentials import SourceCredentialError, validate_source_text


_SOURCE_INPUTS = {"write_source_file": ("content",), "apply_patch": ("patch",), "write_test_file": ("content",)}
_SOURCE_OUTPUTS = {"read_project_file": ("content",)}


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
        try:
            validate_source_text(source)
        except SourceCredentialError:
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY) from None
        sanitized[name] = source
    return sanitized
