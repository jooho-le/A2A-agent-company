"""A bounded Developer model decision, not Source or execution evidence.

The model may describe its work or ask for clarification. The trusted Host
must independently observe actual file changes, freeze a real candidate and
assemble the unchanged Source/Change/Build Artifact contracts from receipts.
READY alone proves neither a file mutation, a successful Build nor a verdict.
"""

from dataclasses import dataclass, field

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from orchestrator.core.security import redact_data


MAX_DEVELOPER_JSON_BYTES = 1_048_576
MAX_DEVELOPER_SUMMARY_LENGTH = 4096
MAX_DEVELOPER_SUMMARY_BYTES = 16_384
MAX_DEVELOPER_QUESTIONS = 8
MAX_DEVELOPER_QUESTION_LENGTH = 1024
MAX_DEVELOPER_QUESTION_BYTES = 4096
_KINDS = frozenset({"READY", "INPUT_REQUIRED", "REJECTED"})
_FIELDS = frozenset({"kind", "summary", "questions"})


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

    def __post_init__(self):
        invalid = False
        try:
            _validate_fields(self.kind, self.summary, self.questions, questions_type=tuple)
        except Exception:
            invalid = True
        if invalid:
            # Raised outside except so private validation data cannot remain
            # reachable through the error's __context__ or __cause__.
            raise DeveloperContractError() from None


def build_developer_output_contract() -> StructuredOutput:
    """A closed, all-required provider-strict Draft, without Artifact fields.

    Branch consistency is checked locally after structural model validation.
    The canonical Artifact schemas are not loaded, copied or weakened here.
    No filesystem, provider, MCP, repository or process operation is performed.
    """
    invalid = False
    try:
        schema = JsonSchema.from_dict({
            "type": "object", "additionalProperties": False,
            "required": ["kind", "summary", "questions"],
            "properties": {
                "kind": {"type": "string", "enum": ["READY", "INPUT_REQUIRED", "REJECTED"]},
                "summary": {"type": "string", "maxLength": MAX_DEVELOPER_SUMMARY_LENGTH},
                "questions": {"type": "array", "maxItems": MAX_DEVELOPER_QUESTIONS,
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_DEVELOPER_QUESTION_LENGTH}},
            },
        })
        schema.require_openai_strict()
        result = StructuredOutput(name="developer_decision", schema=schema)
    except Exception:
        invalid = True
    if invalid:
        raise DeveloperContractError() from None
    return result


def validate_developer_decision(data: dict) -> DeveloperDecision:
    """Validate only a model decision; do not declare any measured success.

    Recognizable credential assignments/hashes/tokens are rejected rather than
    silently rewritten. This is not arbitrary-secret prose detection. The LLM
    wire response must also cross its existing redaction/admission boundary.
    """
    invalid = False
    try:
        if type(data) is not dict or set(data) != _FIELDS:
            raise DeveloperContractError()
        # Check the fixed shallow shape and text bounds before serialization;
        # arbitrary nested payloads cannot enter through unknown Draft fields.
        _validate_fields(data["kind"], data["summary"], data["questions"], questions_type=list)
        copied = parse_json(json_text(data, max_bytes=MAX_DEVELOPER_JSON_BYTES),
                            max_bytes=MAX_DEVELOPER_JSON_BYTES)
        build_developer_output_contract().schema.validate(copied)
        if redact_data(copied) != copied:
            raise DeveloperContractError()
        result = DeveloperDecision(kind=copied["kind"], summary=copied["summary"],
                                   questions=tuple(copied["questions"]))
    except Exception:
        invalid = True
    if invalid:
        raise DeveloperContractError() from None
    return result
