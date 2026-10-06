"""Validate project extensions without duplicating the official A2A models."""

import math
from uuid import UUID

from a2a.types import SendMessageRequest
from a2a.utils.errors import InvalidParamsError, PushNotificationNotSupportedError
from google.protobuf.descriptor import Descriptor
from google.protobuf.json_format import MessageToDict, ParseDict, ParseError
from pydantic import ValidationError

from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.core.security import redact_data


def parse_project_request(body: object) -> SendMessageRequest:
    """Reject invalid requests before the SDK starts any Agent execution."""
    if not isinstance(body, dict):
        raise InvalidParamsError(message="Request body must be an A2A JSON object")
    reject_proto_field_aliases(body, SendMessageRequest.DESCRIPTOR)
    message = body.get("message")
    configuration = body.get("configuration")
    if not isinstance(message, dict) or not isinstance(configuration, dict):
        raise InvalidParamsError(message="Message and configuration are required")
    if message.get("role") != "ROLE_USER":
        raise InvalidParamsError(message="Project requests require ROLE_USER")
    try:
        message_id = UUID(message.get("messageId", ""))
    except (ValueError, TypeError, AttributeError):
        raise InvalidParamsError(message="messageId must be UUIDv4") from None
    if message_id.version != 4:
        raise InvalidParamsError(message="messageId must be UUIDv4")
    if configuration.get("returnImmediately") is not True:
        raise InvalidParamsError(message="Project requests require returnImmediately=true")
    modes = configuration.get("acceptedOutputModes")
    if not isinstance(modes, list) or "application/json" not in modes:
        raise InvalidParamsError(message="Project requests must accept application/json")
    if "taskPushNotificationConfig" in configuration:
        raise PushNotificationNotSupportedError()
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise InvalidParamsError(message="Message must contain JSON data parts")
    for part in parts:
        if (
            not isinstance(part, dict)
            or part.get("mediaType") != "application/json"
            or not isinstance(part.get("data"), dict)
            or not part["data"]
            or "text" in part
            or "file" in part
        ):
            raise InvalidParamsError(message="Project input parts must contain JSON data")
    for field in ("taskId", "contextId"):
        if field in message and (
            not isinstance(message[field], str) or not message[field].strip()
        ):
            raise InvalidParamsError(message="Task/Context references must not be blank")

    metadata = parse_workflow_metadata(body.get("metadata"))
    # Parse the OFFICIAL request with the SDK. Never echo parsing errors, which
    # can contain raw input. Only validated metadata crosses into the executor.
    sanitized = redact_data(body)
    sanitized["metadata"] = metadata.to_a2a_json()
    try:
        params = ParseDict(sanitized, SendMessageRequest())
    except (ParseError, ValueError, TypeError):
        raise InvalidParamsError(message="Request does not match the A2A model") from None
    if params.tenant:
        raise InvalidParamsError(message="Tenant routing is not enabled for this local server")
    return params


def reject_proto_field_aliases(value: object, descriptor: Descriptor) -> None:
    """Require canonical ProtoJSON field names, except inside arbitrary JSON data.

    ParseDict accepts both json_name and snake_case name. Allowing both would
    let a later alias overwrite fields that our project checks already accepted.
    This is descriptor inspection, NOT a second handwritten A2A schema.
    """
    if not isinstance(value, dict) or descriptor.full_name in {
        "google.protobuf.Struct", "google.protobuf.Value", "google.protobuf.ListValue",
    }:
        return
    fields = {field.json_name: field for field in descriptor.fields}
    for key, item in value.items():
        if key in descriptor.fields_by_name:
            if descriptor.fields_by_name[key].json_name != key:
                raise InvalidParamsError(message="Use canonical A2A ProtoJSON field names")
        field = fields.get(key)
        if field is not None and field.message_type is not None:
            if field.is_repeated and isinstance(item, list):
                for entry in item:
                    reject_proto_field_aliases(entry, field.message_type)
            else:
                reject_proto_field_aliases(item, field.message_type)


def parse_workflow_metadata(value: object) -> A2AWorkflowMetadata:
    if not isinstance(value, dict):
        raise InvalidParamsError(message="Project workflow metadata is required")
    normalized = dict(value)
    # Protobuf Struct renders all numbers as float. Accept exactly integer
    # numeric values, but not booleans, fractions, nonfinite values, or strings.
    for field in ("attempt", "codeVersion"):
        number = normalized.get(field)
        if isinstance(number, float) and math.isfinite(number) and number.is_integer():
            normalized[field] = int(number)
    try:
        return A2AWorkflowMetadata.model_validate(normalized)
    except ValidationError:
        raise InvalidParamsError(message="Invalid project workflow metadata") from None


def request_metadata(params: SendMessageRequest) -> A2AWorkflowMetadata:
    return parse_workflow_metadata(MessageToDict(params.metadata))
