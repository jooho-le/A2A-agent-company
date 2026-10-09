"""Bounded Security analysis proposals, never independently trusted findings.

The model may propose a review of Host-issued Requirements/findings with
approved Source anchors, or request clarification. These anchors are only
claims until the Host verifies actual reads and independently approved proof.
Rationale, a valid range, READY, or a clean scanner do not establish PASS,
CONFIRMED, FALSE_POSITIVE, or a final product verdict.
"""

from dataclasses import dataclass, field
import unicodedata
from uuid import UUID

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from mcp_tools.tools.security_contract import _source_path
from orchestrator.core.security import redact_data


MAX_SECURITY_DECISION_JSON_BYTES = 1_048_576
MAX_SECURITY_REQUIREMENT_REVIEWS = 256
MAX_SECURITY_FINDING_REVIEWS = 1000
MAX_SECURITY_SOURCE_PATHS = 1000
MAX_SECURITY_FINDING_ID_BYTES = 512
MAX_SECURITY_RATIONALE_LENGTH = 4096
MAX_SECURITY_RATIONALE_BYTES = 16_384
MAX_SECURITY_REFERENCES = 32
MAX_SECURITY_REFERENCE_LINES = 200
MAX_SECURITY_LINE = 1_048_576
MAX_SECURITY_QUESTIONS = 8
MAX_SECURITY_QUESTION_LENGTH = 1024
MAX_SECURITY_QUESTION_BYTES = 4096
_KINDS = frozenset({"READY", "INPUT_REQUIRED", "REJECTED"})
_OUTCOMES = ("PASS", "FAIL", "UNVERIFIED")
_DISPOSITIONS = ("CONFIRMED", "SUSPECTED", "FALSE_POSITIVE", "UNVERIFIED")
_FIELDS = frozenset({"kind", "requirementReviews", "findingReviews", "questions"})
_REQUIREMENT_FIELDS = frozenset({"requirementId", "proposedOutcome", "rationale", "references"})
_FINDING_FIELDS = frozenset({"findingId", "proposedDisposition", "rationale", "references"})
_REFERENCE_FIELDS = frozenset({"path", "startLine", "endLine"})


class SecurityContractError(ValueError):
    """Stable reason only, without submitted analysis or original exceptions."""

    code = "SECURITY_OUTPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _text(value, *, max_length, max_bytes):
    if (type(value) is not str or not value.strip() or len(value) > max_length
            or len(value.encode("utf-8")) > max_bytes
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            or redact_data(value) != value):
        raise SecurityContractError()


def _path(value):
    # The existing standalone scanner contract applies lexical traversal,
    # Secret path, UTF-8 and compatibility-form checks without any I/O.
    _source_path(value)
    if redact_data(value) != value:
        raise SecurityContractError()
    return value


def _host_scope(requirement_ids, finding_ids, source_paths):
    if (type(requirement_ids) is not tuple
            or not 1 <= len(requirement_ids) <= MAX_SECURITY_REQUIREMENT_REVIEWS
            or any(type(value) is not UUID or value.version != 4 for value in requirement_ids)
            or len(set(requirement_ids)) != len(requirement_ids)
            or type(finding_ids) is not tuple or len(finding_ids) > MAX_SECURITY_FINDING_REVIEWS
            or type(source_paths) is not tuple or not 1 <= len(source_paths) <= MAX_SECURITY_SOURCE_PATHS):
        raise SecurityContractError()
    for identifier in finding_ids:
        _text(identifier, max_length=MAX_SECURITY_FINDING_ID_BYTES, max_bytes=MAX_SECURITY_FINDING_ID_BYTES)
    if len(set(finding_ids)) != len(finding_ids):
        raise SecurityContractError()
    for path in source_paths:
        _path(path)
    canonical = tuple(unicodedata.normalize("NFKC", path).casefold() for path in source_paths)
    if len(set(canonical)) != len(canonical):
        raise SecurityContractError()
    return tuple(str(value) for value in requirement_ids), tuple(finding_ids), tuple(source_paths)


@dataclass(frozen=True, kw_only=True)
class SecurityCodeReference:
    path: str = field(repr=False)
    start_line: int = field(repr=False)
    end_line: int = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            _path(self.path)
            if (type(self.start_line) is not int or type(self.end_line) is not int
                    or not 1 <= self.start_line <= self.end_line <= MAX_SECURITY_LINE
                    or self.end_line - self.start_line + 1 > MAX_SECURITY_REFERENCE_LINES):
                raise SecurityContractError()
        except Exception:
            invalid = True
        if invalid:
            raise SecurityContractError() from None


def _references(values):
    if (type(values) is not tuple or len(values) > MAX_SECURITY_REFERENCES
            or any(type(value) is not SecurityCodeReference for value in values)):
        raise SecurityContractError()
    keys = [(value.path, value.start_line, value.end_line) for value in values]
    if len(set(keys)) != len(keys):
        raise SecurityContractError()


@dataclass(frozen=True, kw_only=True)
class SecurityRequirementReview:
    requirement_id: UUID = field(repr=False)
    proposed_outcome: str
    rationale: str = field(repr=False)
    references: tuple[SecurityCodeReference, ...] = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            if (type(self.requirement_id) is not UUID or self.requirement_id.version != 4
                    or type(self.proposed_outcome) is not str or self.proposed_outcome not in _OUTCOMES):
                raise SecurityContractError()
            _text(self.rationale, max_length=MAX_SECURITY_RATIONALE_LENGTH, max_bytes=MAX_SECURITY_RATIONALE_BYTES)
            _references(self.references)
            if self.proposed_outcome in {"PASS", "FAIL"} and not self.references:
                raise SecurityContractError()
        except Exception:
            invalid = True
        if invalid:
            raise SecurityContractError() from None


@dataclass(frozen=True, kw_only=True)
class SecurityFindingReview:
    finding_id: str = field(repr=False)
    proposed_disposition: str
    rationale: str = field(repr=False)
    references: tuple[SecurityCodeReference, ...] = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            _text(self.finding_id, max_length=MAX_SECURITY_FINDING_ID_BYTES, max_bytes=MAX_SECURITY_FINDING_ID_BYTES)
            if type(self.proposed_disposition) is not str or self.proposed_disposition not in _DISPOSITIONS:
                raise SecurityContractError()
            _text(self.rationale, max_length=MAX_SECURITY_RATIONALE_LENGTH, max_bytes=MAX_SECURITY_RATIONALE_BYTES)
            _references(self.references)
            if self.proposed_disposition in {"CONFIRMED", "FALSE_POSITIVE"} and not self.references:
                raise SecurityContractError()
        except Exception:
            invalid = True
        if invalid:
            raise SecurityContractError() from None


def _validate_branch(kind, requirement_reviews, finding_reviews, questions, *, container_type):
    if (type(kind) is not str or kind not in _KINDS
            or type(requirement_reviews) is not container_type
            or len(requirement_reviews) > MAX_SECURITY_REQUIREMENT_REVIEWS
            or type(finding_reviews) is not container_type or len(finding_reviews) > MAX_SECURITY_FINDING_REVIEWS
            or type(questions) is not container_type or len(questions) > MAX_SECURITY_QUESTIONS):
        raise SecurityContractError()
    for question in questions:
        _text(question, max_length=MAX_SECURITY_QUESTION_LENGTH, max_bytes=MAX_SECURITY_QUESTION_BYTES)
    if ((kind == "READY" and (not requirement_reviews or questions))
            or (kind == "INPUT_REQUIRED" and (requirement_reviews or finding_reviews or not questions))
            or (kind == "REJECTED" and (requirement_reviews or finding_reviews or questions))):
        raise SecurityContractError()


@dataclass(frozen=True, kw_only=True)
class SecurityDecision:
    kind: str
    requirement_reviews: tuple[SecurityRequirementReview, ...] = field(repr=False)
    finding_reviews: tuple[SecurityFindingReview, ...] = field(repr=False)
    questions: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            _validate_branch(self.kind, self.requirement_reviews, self.finding_reviews, self.questions,
                             container_type=tuple)
            if (any(type(value) is not SecurityRequirementReview for value in self.requirement_reviews)
                    or any(type(value) is not SecurityFindingReview for value in self.finding_reviews)
                    or len({value.requirement_id for value in self.requirement_reviews}) != len(self.requirement_reviews)
                    or len({value.finding_id for value in self.finding_reviews}) != len(self.finding_reviews)):
                raise SecurityContractError()
        except Exception:
            invalid = True
        if invalid:
            raise SecurityContractError() from None


def build_security_output_contract(requirement_ids, finding_ids, source_paths) -> StructuredOutput:
    """Build an inert closed provider-strict analysis schema, not an Artifact.

    Empty scanner findings deliberately produce maxItems=0, never enum=[].
    Exact coverage, range relationships and semantic branch checks are local.
    No canonical product schema is loaded, weakened or fetched remotely.
    """
    invalid = False
    try:
        ids, findings, paths = _host_scope(requirement_ids, finding_ids, source_paths)
        rationale = {"type": "string", "minLength": 1, "maxLength": MAX_SECURITY_RATIONALE_LENGTH}
        references = {"type": "array", "maxItems": MAX_SECURITY_REFERENCES, "items": {
            "type": "object", "additionalProperties": False, "required": ["path", "startLine", "endLine"],
            "properties": {
                "path": {"type": "string", "enum": list(paths)},
                "startLine": {"type": "integer", "minimum": 1, "maximum": MAX_SECURITY_LINE},
                "endLine": {"type": "integer", "minimum": 1, "maximum": MAX_SECURITY_LINE},
            },
        }}
        finding_id = {"type": "string", "minLength": 1, "maxLength": MAX_SECURITY_FINDING_ID_BYTES}
        if findings:
            finding_id["enum"] = list(findings)
        schema = JsonSchema.from_dict({
            "type": "object", "additionalProperties": False,
            "required": ["kind", "requirementReviews", "findingReviews", "questions"],
            "properties": {
                "kind": {"type": "string", "enum": ["READY", "INPUT_REQUIRED", "REJECTED"]},
                "requirementReviews": {"type": "array", "maxItems": len(ids), "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["requirementId", "proposedOutcome", "rationale", "references"],
                    "properties": {"requirementId": {"type": "string", "enum": list(ids)},
                        "proposedOutcome": {"type": "string", "enum": list(_OUTCOMES)},
                        "rationale": rationale, "references": references},
                }},
                "findingReviews": {"type": "array", "maxItems": len(findings), "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["findingId", "proposedDisposition", "rationale", "references"],
                    "properties": {"findingId": finding_id,
                        "proposedDisposition": {"type": "string", "enum": list(_DISPOSITIONS)},
                        "rationale": rationale, "references": references},
                }},
                "questions": {"type": "array", "maxItems": MAX_SECURITY_QUESTIONS,
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_SECURITY_QUESTION_LENGTH}},
            },
        })
        schema.require_openai_strict()
        result = StructuredOutput(name="security_decision", schema=schema)
    except Exception:
        invalid = True
    if invalid:
        raise SecurityContractError() from None
    return result


def _read_references(values, paths):
    if type(values) is not list or len(values) > MAX_SECURITY_REFERENCES:
        raise SecurityContractError()
    result = []
    for row in values:
        if (type(row) is not dict or set(row) != _REFERENCE_FIELDS
                or type(row["path"]) is not str or row["path"] not in paths):
            raise SecurityContractError()
        result.append(SecurityCodeReference(path=row["path"], start_line=row["startLine"], end_line=row["endLine"]))
    values = tuple(result)
    _references(values)
    return values


def validate_security_decision(data, requirement_ids, finding_ids, source_paths) -> SecurityDecision:
    """Admit proposals only; independent Host proof is still mandatory.

    A listed path/range does not prove the model actually read that Source,
    that the lines exist, or that the rationale is correct. No Tool evidence,
    outcome promotion, test, scanner, repository or file I/O occurs here.
    """
    invalid = False
    try:
        ids, findings, paths = _host_scope(requirement_ids, finding_ids, source_paths)
        if type(data) is not dict or set(data) != _FIELDS:
            raise SecurityContractError()
        _validate_branch(data["kind"], data["requirementReviews"], data["findingReviews"], data["questions"],
                         container_type=list)
        requirements, reviews = [], []
        for row in data["requirementReviews"]:
            if (type(row) is not dict or set(row) != _REQUIREMENT_FIELDS
                    or type(row["requirementId"]) is not str or row["requirementId"] not in ids):
                raise SecurityContractError()
            requirements.append(SecurityRequirementReview(
                requirement_id=UUID(row["requirementId"]), proposed_outcome=row["proposedOutcome"],
                rationale=row["rationale"], references=_read_references(row["references"], paths)))
        for row in data["findingReviews"]:
            if (type(row) is not dict or set(row) != _FINDING_FIELDS
                    or type(row["findingId"]) is not str or row["findingId"] not in findings):
                raise SecurityContractError()
            reviews.append(SecurityFindingReview(finding_id=row["findingId"],
                proposed_disposition=row["proposedDisposition"], rationale=row["rationale"],
                references=_read_references(row["references"], paths)))
        copied = parse_json(json_text(data, max_bytes=MAX_SECURITY_DECISION_JSON_BYTES),
                            max_bytes=MAX_SECURITY_DECISION_JSON_BYTES)
        build_security_output_contract(requirement_ids, finding_ids, source_paths).schema.validate(copied)
        if redact_data(copied) != copied:
            raise SecurityContractError()
        result = SecurityDecision(kind=copied["kind"], requirement_reviews=tuple(requirements),
                                  finding_reviews=tuple(reviews), questions=tuple(copied["questions"]))
        if (result.kind == "READY" and (
                {str(value.requirement_id) for value in result.requirement_reviews} != set(ids)
                or {value.finding_id for value in result.finding_reviews} != set(findings))):
            raise SecurityContractError()
    except Exception:
        invalid = True
    if invalid:
        raise SecurityContractError() from None
    return result
