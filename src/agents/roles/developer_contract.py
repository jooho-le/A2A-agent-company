"""A bounded Developer model decision, not Source or execution evidence.

The model may describe its work or ask for clarification. The trusted Host
must independently observe actual file changes, freeze a real candidate and
assemble the unchanged Source/Change/Build Artifact contracts from receipts.
READY alone proves neither a file mutation, a successful Build nor a verdict.
"""

from dataclasses import dataclass, field

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from orchestrator.core.security import redact_data
from orchestrator.domain.developer_artifacts import ReportedPathEdit
from agents.llm.development_rejection import RejectionReason, rejection_schema


MAX_DEVELOPER_JSON_BYTES = 1_048_576
MAX_DEVELOPER_SUMMARY_LENGTH = 4096
MAX_DEVELOPER_SUMMARY_BYTES = 16_384
MAX_DEVELOPER_QUESTIONS = 8
MAX_DEVELOPER_QUESTION_LENGTH = 1024
MAX_DEVELOPER_QUESTION_BYTES = 4096
_KINDS = frozenset({"READY", "INPUT_REQUIRED", "REJECTED"})
_FIELDS = frozenset({"kind", "summary", "questions", "rejection"})


class DeveloperContractError(ValueError):
    """A stable reason only; no submitted text or underlying exception."""

    code = "DEVELOPER_OUTPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _text(value, *, max_length, max_bytes, allow_empty=False):
    if (type(value) is not str or len(value) > max_length
            or len(value.encode("utf-8")) > max_bytes
            or not allow_empty and not value.strip()
            or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in value)
            or redact_data(value) != value):
        raise DeveloperContractError()


def _validate_fields(kind, summary, questions, *, questions_type):
    if (type(kind) is not str or kind not in _KINDS
            or type(questions) is not questions_type
            or len(questions) > MAX_DEVELOPER_QUESTIONS):
        raise DeveloperContractError()
    _text(summary, max_length=MAX_DEVELOPER_SUMMARY_LENGTH,
          max_bytes=MAX_DEVELOPER_SUMMARY_BYTES, allow_empty=True)
    for question in questions:
        _text(question, max_length=MAX_DEVELOPER_QUESTION_LENGTH,
              max_bytes=MAX_DEVELOPER_QUESTION_BYTES)
    if ((kind == "READY" and (not summary.strip() or questions))
            or (kind == "INPUT_REQUIRED" and (summary != "" or not questions))
            or (kind == "REJECTED" and (summary != "" or questions))):
        raise DeveloperContractError()


@dataclass(frozen=True, kw_only=True)
class DeveloperDecision:
    kind: str
    summary: str = field(repr=False)
    questions: tuple[str, ...] = field(repr=False)
    rejection: RejectionReason | None = field(default=None, repr=False)
    reported_path_edits: tuple[ReportedPathEdit, ...] = field(default=(), repr=False)

    def __post_init__(self):
        invalid = False
        try:
            _validate_fields(self.kind, self.summary, self.questions, questions_type=tuple)
            if (self.kind == "REJECTED") != (self.rejection is not None):
                raise DeveloperContractError()
            if type(self.reported_path_edits) is not tuple or (self.reported_path_edits and self.kind != "READY"):
                raise DeveloperContractError()
            for row in self.reported_path_edits:
                _text(row.recheck, max_length=400, max_bytes=1600)
                if row.host_diff_status is not None or (row.edit is None) != (row.source_ref is None):
                    raise DeveloperContractError()
                if row.edit is not None:
                    _text(row.edit, max_length=400, max_bytes=1600)
                    _text(row.source_ref, max_length=400, max_bytes=1600)
            if self.rejection is not None:
                RejectionReason.model_validate(self.rejection.public())
        except Exception:
            invalid = True
        if invalid:
            # Raised outside except so private validation data cannot remain
            # reachable through the error's __context__ or __cause__.
            raise DeveloperContractError() from None


def ready_report_paths(task_input):
    brief = task_input.get('activeRepairFocus', {}).get('brief', {})
    return tuple(brief.get('readyReportPaths', ()))


def corrective_ready_output(base: StructuredOutput, candidates) -> StructuredOutput:
    """Constrain only previously rejected claims, using the Host's current source diff."""
    schema = base.schema.to_dict()
    paths = schema['properties']['repairPaths']['properties']
    for path, refs in candidates.items():
        row = paths[path]
        fields = row['properties']
        # Distinct first keys keep the two nested object branches unambiguous on the provider wire.
        open_fields = {'edit': {'type': 'null'}, 'sourceRef': {'type': 'null'}, 'recheck': fields['recheck']}
        open_row = {**row, 'required': list(open_fields), 'properties': open_fields}
        if refs:
            linked_fields = {'sourceRef': {'type': 'string', 'enum': list(refs)},
                             'edit': {**fields['edit'], 'type': 'string'}, 'recheck': fields['recheck']}
            linked = {**row, 'required': list(linked_fields), 'properties': linked_fields}
            paths[path] = {'anyOf': [linked, open_row]}
        else:
            # Reads and edits remain available. A report on the current bytes can only leave this path open.
            paths[path] = open_row
    narrowed = JsonSchema.from_dict(schema)
    narrowed.require_openai_strict()
    return StructuredOutput(name=base.name, schema=narrowed)


def build_developer_output_contract(repair_paths=()) -> StructuredOutput:
    """A closed, all-required provider-strict Draft, without Artifact fields.

    Branch consistency is checked locally after structural model validation.
    The canonical Artifact schemas are not loaded, copied or weakened here.
    No filesystem, provider, MCP, repository or process operation is performed.
    """
    invalid = False
    try:
        definition = {
            "type": "object", "additionalProperties": False,
            "required": ["kind", "summary", "questions", "rejection"],
            "properties": {
                "rejection": {**rejection_schema(), "description": "Required explanation object for REJECTED; null for READY and INPUT_REQUIRED. BUILD_TOOL_UNAVAILABLE is a claim about build execution, which the Host owns. IMPLEMENTATION_BLOCKED means inability to implement assigned requirements, never a policy refusal."},
                "kind": {"type": "string", "enum": ["READY", "INPUT_REQUIRED", "REJECTED"],
                    "description": "READY submits work for Host verification; INPUT_REQUIRED asks for clarification; REJECTED declines the task. Follow the branch rules on summary and questions."},
                "summary": {"type": "string", "maxLength": MAX_DEVELOPER_SUMMARY_LENGTH,
                    "description": "For READY, describe the path actually changed, how the edit reaches observable behavior, and untouched or pending work. The Host records changed files and Build separately. For INPUT_REQUIRED or REJECTED, this must be exactly the empty string."},
                "questions": {"type": "array", "maxItems": MAX_DEVELOPER_QUESTIONS,
                    "description": "For INPUT_REQUIRED, provide 1 to 8 nonblank questions. For READY or REJECTED, this must be an empty array.",
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_DEVELOPER_QUESTION_LENGTH}},
            },
        }
        if repair_paths:
            row = {"type": "object", "additionalProperties": False, "required": ["edit", "recheck", "sourceRef"],
                   "properties": {
                       "sourceRef": {"type": ["string", "null"], "minLength": 1, "maxLength": 400,
                                     "description": "Changed source/file.py::function (or ::@module for module code) on this observable path. Null with edit when unresolved."},
                       "edit": {"type": ["string", "null"], "minLength": 1, "maxLength": 400,
                                "description": "What changed and how it reaches the observed output. Null for an untouched path."},
                       "recheck": {"type": "string", "minLength": 1, "maxLength": 400,
                                   "description": "Observable behavior QA or Security must still verify."}}}
            definition["properties"]["repairPaths"] = {
                "type": ["object", "null"], "description": "Required path account for READY. Null for all other decisions.",
                "additionalProperties": False, "required": list(repair_paths),
                "properties": {name: row for name in repair_paths}}
            definition["required"].append("repairPaths")
        schema = JsonSchema.from_dict(definition)
        schema.require_openai_strict()
        result = StructuredOutput(name="developer_decision", schema=schema)
    except Exception:
        invalid = True
    if invalid:
        raise DeveloperContractError() from None
    return result


def validate_developer_decision(data: dict, repair_paths=()) -> DeveloperDecision:
    """Validate only a model decision; do not declare any measured success.

    Recognizable credential assignments/hashes/tokens are rejected rather than
    silently rewritten. This is not arbitrary-secret prose detection. The LLM
    wire response must also cross its existing redaction/admission boundary.
    """
    invalid = False
    try:
        if type(data) is not dict or set(data) != (_FIELDS | {"repairPaths"} if repair_paths else _FIELDS):
            raise DeveloperContractError()
        # Check the fixed shallow shape and text bounds before serialization;
        # arbitrary nested payloads cannot enter through unknown Draft fields.
        _validate_fields(data["kind"], data["summary"], data["questions"], questions_type=list)
        copied = parse_json(json_text(data, max_bytes=MAX_DEVELOPER_JSON_BYTES),
                            max_bytes=MAX_DEVELOPER_JSON_BYTES)
        build_developer_output_contract(repair_paths).schema.validate(copied)
        rows = copied.get("repairPaths")
        if repair_paths and (rows is not None) != (copied["kind"] == "READY"):
            raise DeveloperContractError()
        if redact_data(copied) != copied:
            raise DeveloperContractError()
        result = DeveloperDecision(kind=copied["kind"], summary=copied["summary"],
                                   questions=tuple(copied["questions"]),
                                   reported_path_edits=tuple(ReportedPathEdit(path=path, **rows[path])
                                                            for path in repair_paths if rows is not None),
                                   rejection=None if copied["rejection"] is None else RejectionReason.model_validate(copied["rejection"]))
    except Exception:
        invalid = True
    if invalid:
        raise DeveloperContractError() from None
    return result
