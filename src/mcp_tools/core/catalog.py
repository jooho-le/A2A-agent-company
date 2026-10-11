"""Immutable, offline MCP contracts; this module grants no filesystem access.

Names and public fields follow definition sections 8-3 through 8-5. Report
payload internals and supported test/scanner profiles are not fixed there:
they remain bounded JSON rather than inventing a new product-report contract.
Filesystem, Snapshot, and Artifact identity checks belong to the handlers.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import math
import re
from types import MappingProxyType

from jsonschema import Draft202012Validator, FormatChecker


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
MAX_FILE_BYTES = 1_048_576
MAX_PATH_LENGTH = 4096
MAX_JSON_BYTES = 8 * MAX_FILE_BYTES
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 65_536


class ToolSchemaError(ValueError):
    """Stable reason only, with no submitted data or schema exception body."""

    def __init__(self):
        super().__init__("MCP_TOOL_SCHEMA_INVALID")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _no_constant(_value):
    raise ValueError("Nonfinite JSON number")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def _check_json(value):
    remaining = MAX_JSON_NODES

    def walk(node, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise ValueError("JSON bounds")
        if node is None or type(node) in (str, bool, int):
            return
        if type(node) is float and math.isfinite(node):
            return
        if type(node) is list:
            for child in node:
                walk(child, depth + 1)
            return
        if type(node) is dict and all(type(key) is str for key in node):
            for child in node.values():
                walk(child, depth + 1)
            return
        raise ValueError("JSON types required")

    walk(value, 0)


def _canonical_json(value):
    _check_json(value)
    result = json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    if len(result.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON size")
    return result


def _parse_json(value):
    if type(value) is not str or len(value.encode("utf-8")) > MAX_JSON_BYTES:
        raise ValueError("JSON size")
    result = json.loads(
        value, parse_constant=_no_constant, parse_float=_finite_float,
        object_pairs_hook=_unique_object,
    )
    _check_json(result)
    return result


def _check_schema_refs(value):
    """Local static refs only; validation must not fetch any external resource."""
    if isinstance(value, dict):
        if any(key in value for key in ("$id", "$dynamicRef", "$recursiveRef")):
            raise ValueError("Unsupported schema reference")
        ref = value.get("$ref")
        if ref is not None and (type(ref) is not str or not ref.startswith("#")):
            raise ValueError("External schema reference")
        for child in value.values():
            _check_schema_refs(child)
    elif isinstance(value, list):
        for child in value:
            _check_schema_refs(child)


def _schema(value):
    result = _parse_json(value)
    if (
        type(result) is not dict
        or result.get("$schema") != JSON_SCHEMA_DIALECT
        or result.get("type") != "object"
        or result.get("additionalProperties") is not False
    ):
        raise ValueError("Closed object schema required")
    _check_schema_refs(result)
    Draft202012Validator.check_schema(result)
    return result


@dataclass(frozen=True)
class ToolContract:
    name: str
    description: str
    input_schema_json: str = field(repr=False)
    output_schema_json: str = field(repr=False)
    source_argument_fields: tuple[str, ...] = ()
    source_output_fields: tuple[str, ...] = ()

    def __post_init__(self):
        invalid = False
        try:
            if (
                type(self.name) is not str
                or re.fullmatch(r"[a-z][a-z0-9_]{0,63}", self.name) is None
                or type(self.description) is not str
                or not self.description.strip()
                or len(self.description) > 4096
            ):
                raise ValueError("Contract identity")
            for schema_field, source_fields in (
                ("input_schema_json", self.source_argument_fields),
                ("output_schema_json", self.source_output_fields),
            ):
                parsed = _schema(getattr(self, schema_field))
                if (
                    type(source_fields) is not tuple
                    or any(type(name) is not str for name in source_fields)
                    or len(source_fields) != len(set(source_fields))
                    or any(
                        parsed.get("properties", {}).get(name, {}).get("type") != "string"
                        for name in source_fields
                    )
                ):
                    raise ValueError("Source fields")
                object.__setattr__(self, schema_field, _canonical_json(parsed))
        except Exception:
            invalid = True
        if invalid:
            # Outside the except block: do not retain a ValidationError with
            # submitted data in __context__, even when its traceback is hidden.
            raise ToolSchemaError() from None

    @property
    def input_schema(self) -> dict:
        return _parse_json(self.input_schema_json)

    @property
    def output_schema(self) -> dict:
        return _parse_json(self.output_schema_json)

    @staticmethod
    def _validate(value, schema, source_fields):
        invalid = False
        try:
            # Round-trip only JSON-native finite data; never coerce UUIDs,
            # tuples, custom mappings, objects, or non-string dictionary keys.
            copied = _parse_json(_canonical_json(value))
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(copied)
            for name in source_fields:
                if name in copied and len(copied[name].encode("utf-8")) > MAX_FILE_BYTES:
                    raise ValueError("Source byte limit")
        except Exception:
            invalid = True
        if invalid:
            raise ToolSchemaError() from None

    def validate_input(self, value) -> None:
        self._validate(value, self.input_schema, self.source_argument_fields)

    def validate_output(self, value) -> None:
        self._validate(value, self.output_schema, self.source_output_fields)


_UUID = {"type": "string", "format": "uuid"}
_PATH = {"type": "string", "minLength": 1, "maxLength": MAX_PATH_LENGTH}
_TEXT = {"type": "string", "maxLength": MAX_FILE_BYTES}
_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_REFERENCE = {"type": "string", "minLength": 1, "maxLength": MAX_PATH_LENGTH}
_COUNT = {"type": "integer", "minimum": 0}

# Deliberately open *bounded payload* objects, not a new QA/Security Artifact
# schema. Their parser/profile-specific semantics are decided in steps 26-28.
# Every specified Tool envelope is closed. newHashes is a bounded path->hash
# dictionary, so its keys are necessarily dynamic as well.
_JSON_DEFS = {
    "jsonValue": {
        "anyOf": [
            {"type": "null"}, {"type": "boolean"}, {"type": "number"},
            _TEXT,
            {"type": "array", "maxItems": 1000, "items": {"$ref": "#/$defs/jsonValue"}},
            {"$ref": "#/$defs/jsonObject"},
        ]
    },
    "jsonObject": {
        "type": "object", "maxProperties": 256,
        "propertyNames": {"type": "string", "minLength": 1, "maxLength": 256},
        "additionalProperties": {"$ref": "#/$defs/jsonValue"},
    },
}
_JSON_OBJECT = {"$ref": "#/$defs/jsonObject"}


def _object(properties, *, required=None, json_payload=False):
    value = {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object", "additionalProperties": False,
        "properties": properties,
        "required": list(properties) if required is None else list(required),
    }
    if json_payload:
        value["$defs"] = _JSON_DEFS
    return _canonical_json(value)


def _contract(name, description, input_fields, output_fields, **options):
    return ToolContract(
        name=name, description=description,
        input_schema_json=_object(
            {"workspaceId": _UUID, **input_fields}, required=options.get("input_required"),
        ),
        output_schema_json=_object(
            output_fields, required=options.get("output_required"),
            json_payload=options.get("json_payload", False),
        ),
        source_argument_fields=options.get("source_argument_fields", ()),
        source_output_fields=options.get("source_output_fields", ()),
    )


_CONTRACTS = (
    _contract(
        "read_project_file", "Read an allowed file in the assigned Workspace or frozen Snapshot.",
        {"path": _PATH},
        {"path": _PATH, "content": _TEXT, "sha256": _HASH, "sizeBytes": _COUNT},
        source_output_fields=("content",),
    ),
    _contract(
        "write_source_file", "Write Developer Source; optionally compare the existing SHA-256 first.",
        {"path": _PATH, "content": _TEXT, "expectedSha256": _HASH},
        {"path": _PATH, "sha256": _HASH, "sizeBytes": _COUNT, "changed": {"type": "boolean"}},
        input_required=("workspaceId", "path", "content"), source_argument_fields=("content",),
    ),
    _contract(
        "write_test_file", "Write only the assigned QA Test area, never Product Source.",
        {"path": _PATH, "content": _TEXT},
        {"path": _PATH, "sha256": _HASH, "changed": {"type": "boolean"}},
        source_argument_fields=("content",),
    ),
    _contract(
        "apply_patch", "Patch Developer Product Source against the declared base Snapshot hash.",
        {"patch": _TEXT, "baseSnapshotSha256": _HASH},
        {
            "changedFiles": {"type": "array", "maxItems": 1000, "uniqueItems": True, "items": _PATH},
            "newHashes": {
                "type": "object", "maxProperties": 1000,
                "propertyNames": _PATH, "additionalProperties": _HASH,
            },
        },
        source_argument_fields=("patch",),
    ),
    _contract(
        "run_build", "Build an immutable Snapshot in a Container Sandbox; no arbitrary command input.",
        {"snapshotId": _UUID},
        {
            "exitCode": {"type": "integer"}, "stdoutRef": _REFERENCE,
            "stderrRef": _REFERENCE, "durationMs": _COUNT, "executionManifestId": _UUID,
        },
        output_required=("exitCode", "durationMs", "executionManifestId"),
    ),
    _contract(
        "run_unit_tests", "Run a Host-approved test scope in a Snapshot Container Sandbox.",
        {"snapshotId": _UUID, "testScope": _REFERENCE},
        {
            "total": _COUNT, "passed": _COUNT, "failed": _COUNT, "skipped": _COUNT,
            "reportRef": _REFERENCE, "executionManifestId": _UUID,
        },
    ),
    _contract(
        "run_browser_tests", "Run a Host-approved browser test suite in a Snapshot Container Sandbox.",
        {"snapshotId": _UUID, "testSuite": _REFERENCE},
        {
            "total": _COUNT, "passed": _COUNT, "failed": _COUNT,
            "traceRefs": {"type": "array", "maxItems": 1000, "items": _REFERENCE},
            "executionManifestId": _UUID,
        },
    ),
    _contract(
        "run_security_scan", "Scan a read-only Snapshot with a Host-approved scanner profile.",
        {"snapshotId": _UUID, "scannerProfile": _REFERENCE},
        {
            "findings": {"type": "array", "maxItems": 1000, "items": _JSON_OBJECT},
            "reportRef": _REFERENCE, "executionManifestId": _UUID,
        },
        json_payload=True,
    ),
    _contract(
        "read_test_report", "Read the assigned test report; detailed payload follows the approved runner contract.",
        {"reportRef": _REFERENCE}, {"testResult": _JSON_OBJECT}, json_payload=True,
    ),
    _contract(
        "read_security_report", "Read the assigned security report; detailed payload follows the approved scanner contract.",
        {"reportRef": _REFERENCE}, {"securityResult": _JSON_OBJECT}, json_payload=True,
    ),
)

TOOL_CONTRACTS: Mapping[str, ToolContract] = MappingProxyType(
    {contract.name: contract for contract in _CONTRACTS}
)


def get_tool_contract(name: str) -> ToolContract | None:
    """Lookup only; discovery and call-time access control belong to the server."""
    return TOOL_CONTRACTS.get(name) if type(name) is str else None
