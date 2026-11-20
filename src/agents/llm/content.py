"""Wire-only Source preservation without treating code variables as secrets.

    This recognizes credential literals, not arbitrary secret prose or taint.
    Filesystem/secret access and real MCP enforcement remain sandbox tasks.
"""

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolDefinition
from orchestrator.core.security import redact_data
from orchestrator.core.source_credentials import SourceCredentialError, validate_source_text


_SOURCE_INPUTS = {"write_source_file": ("content",), "edit_source_file": ("oldText", "newText"),
                  "apply_patch": ("patch",), "write_test_file": ("content",)}
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


def sanitize_content(data: dict, *, source_fields: tuple[str, ...] = (), reject_secrets: bool = False,
                     sequence=None, tool_name=None, boundary='TOOL_ARGUMENTS') -> dict:
    """Copy only; credential-bearing Source is rejected, never silently rewritten.

    Ordinary code assignments like password=request.password remain byte-exact.
    Annotations are restricted to known Source fields by validate_source_fields.
    """
    ordinary = {key: value for key, value in data.items() if key not in source_fields}
    sanitized = redact_data(ordinary)
    if reject_secrets and sanitized != ordinary:
        if sequence is not None:
            from agents.llm.validation import policy_error
            raise policy_error('ARGUMENT_CREDENTIAL_DENIED', sequence=sequence,
                               tool_name=tool_name, boundary=boundary)
        raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY)
    for name in source_fields:
        if name not in data:
            continue
        source = data[name]
        rejected = False
        source_rule = source_line = None
        try:
            validate_source_text(source)
        except SourceCredentialError as error:
            rejected = True
            source_rule, source_line = error.rule, error.line
        if rejected:
            if sequence is not None:
                from agents.llm.validation import policy_error
                raise policy_error('SOURCE_CREDENTIAL_DENIED', sequence=sequence,
                                   tool_name=tool_name, boundary=boundary, field_path='/' + name,
                                   source_rule=source_rule, source_line=source_line) from None
            raise LLMRuntimeError(LLMErrorCode.TOOL_POLICY) from None
        sanitized[name] = source
    return sanitized
