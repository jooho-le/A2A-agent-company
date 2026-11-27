"""Explicit OpenAI wire encoding; project/MCP schemas stay unchanged.

Only root optional fields that do not accept null use null as an omission
sentinel. No required field, non-null value, nested source or unknown property
is repaired. Unsupported strict schemas fail before the HTTP request.
"""
from jsonschema import Draft202012Validator, FormatChecker

from agents.llm.contracts import JsonSchema, LLMErrorCode, LLMRuntimeError, json_text, parse_json


STRICT_TOOL_ARGUMENTS = 'OPENAI_STRICT_OPTIONALS_V1'
PROJECT_TOOL_ARGUMENTS = 'PROJECT_JSON'
# Recorded in the Host policy (Codex review 2026-11-18): workspaceId offered as a one-value enum.
WORKSPACE_ARGUMENT_POLICY = 'HOST_PINNED_WORKSPACE_ARGUMENT_V1'


def pin_workspace_argument(tool, workspace_id):
    """The same tool with ``workspaceId`` fixed to the one Host-bound value (wire definition only).

    Live Run 480ce908 (#31, 2026-11-18) stopped after Security mistyped the workspace
    UUID in a batch of reads twice. The model never chooses this value, so the
    provider's constrained decoding now offers exactly one. Argument validation and
    the engine's workspace check stay on the original definition as before.
    """
    from dataclasses import replace
    if not isinstance(workspace_id, str) or not workspace_id:
        return tool
    schema = tool.input_schema.to_dict()
    field = schema.get('properties', {}).get('workspaceId')
    if not isinstance(field, dict) or field.get('type') != 'string':
        return tool
    schema['properties']['workspaceId'] = {
        'type': 'string', 'enum': [workspace_id],
        'description': 'The workspace of this step, fixed by the Host; the only accepted value.'}
    return replace(tool, input_schema=JsonSchema.from_dict(schema))


def pin_test_write_paths(tool, paths):
    """Narrow an existing scratch write capability; never add paths or tools."""
    from dataclasses import replace
    if tool.name != 'write_test_file' or not paths:
        return tool
    schema = tool.input_schema.to_dict()
    field = schema.get('properties', {}).get('path')
    if not isinstance(field, dict) or field.get('type') != 'string':
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
    # Keep existing prefix/pattern constraints as well as the narrower enum.
    field['enum'] = list(paths)
    return replace(tool, input_schema=JsonSchema.from_dict(schema))


def pin_source_repair(tool, path, expected_sha256=None, *, expected_absent=False):
    """Pin an already offered source operation to a settled-conflict recovery."""
    from dataclasses import replace
    if tool.name not in ('read_project_file', 'write_source_file', 'edit_source_file'):
        return tool
    schema = tool.input_schema.to_dict()
    properties = schema.get('properties', {})
    if 'path' not in properties:
        raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
    properties['path']['enum'] = [path]
    if expected_absent and tool.name == 'write_source_file':
        # Existing optional field omission now means a create-only CAS at the actual file Tool.
        # The narrowed wire must not let the model guess a hash or replace another file.
        properties.pop('expectedSha256', None)
        schema['required'] = [name for name in schema['required'] if name != 'expectedSha256']
        return replace(tool, input_schema=JsonSchema.from_dict(schema))
    if expected_sha256 is not None and tool.name != 'read_project_file':
        if 'expectedSha256' not in properties:
            raise LLMRuntimeError(LLMErrorCode.CONFIGURATION)
        properties['expectedSha256']['enum'] = [expected_sha256]
        if 'expectedSha256' not in schema['required']:
            schema['required'].append('expectedSha256')
    return replace(tool, input_schema=JsonSchema.from_dict(schema))


def strict_tool_schema(project: JsonSchema) -> JsonSchema:
    schema = project.to_dict()
    properties = schema.get('properties', {})
    required = schema.get('required', [])
    if not set(required).issubset(properties):
        raise LLMRuntimeError(LLMErrorCode.SCHEMA)
    for name, field in properties.items():
        if name not in required and not Draft202012Validator(field).is_valid(None):
            properties[name] = {'anyOf': [field, {'type': 'null'}],
                'description': 'Optional project argument. Use null to omit it; otherwise supply the declared value.'}
    schema['required'] = list(properties)
    result = JsonSchema.from_dict(schema)
    result.require_openai_strict()
    return result


def project_tool_arguments(project: JsonSchema, wire: dict) -> dict:
    """Decode only the declared sentinel after the wire schema was validated."""
    result = parse_json(json_text(wire))
    schema = project.to_dict()
    for name, field in schema.get('properties', {}).items():
        if (name not in schema.get('required', []) and name in result and result[name] is None
                and not Draft202012Validator(field, format_checker=FormatChecker()).is_valid(None)):
            del result[name]
    return result
