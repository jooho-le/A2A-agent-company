"""Bounded QA case bindings, not test execution or a product verdict.

The model may bind named cases to protected Requirement IDs and approved
Host scopes, or ask questions. The trusted Host must execute those scopes
and assemble the unchanged QA Artifact from independently stored receipts.
READY and expected-result prose prove neither coverage nor a passing test.
"""

from dataclasses import dataclass, field
import re
from uuid import UUID

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from orchestrator.core.security import redact_data


MAX_QA_JSON_BYTES = 1_048_576
MAX_QA_CASES = 256
MAX_QA_SELECTORS_PER_TOOL = 32
MAX_QA_TEST_ID_BYTES = 512
MAX_QA_TITLE_LENGTH = 1024
MAX_QA_TITLE_BYTES = 4096
MAX_QA_EXPECTED_RESULT_LENGTH = 4096
MAX_QA_EXPECTED_RESULT_BYTES = 16_384
MAX_QA_QUESTIONS = 8
MAX_QA_QUESTION_LENGTH = 1024
MAX_QA_QUESTION_BYTES = 4096
_KINDS = frozenset({"READY", "INPUT_REQUIRED", "REJECTED"})
_TOOLS = ("run_unit_tests", "run_browser_tests")
_FIELDS = frozenset({"kind", "cases", "questions"})
_CASE_FIELDS = frozenset({"toolName", "selector", "testId", "requirementId", "title", "expectedResult"})
_SELECTOR = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")


class QAContractError(ValueError):
    """A stable reason only; no submitted cases or underlying exception."""

    code = "QA_OUTPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _text(value, *, max_length, max_bytes):
    if (type(value) is not str or not value.strip() or len(value) > max_length
            or len(value.encode("utf-8")) > max_bytes
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)
            or redact_data(value) != value):
        raise QAContractError()


def _selector(value):
    if type(value) is not str or _SELECTOR.fullmatch(value) is None:
        raise QAContractError()


def _host_scope(requirement_ids, selectors):
    """Validate only inert Host declarations; do not infer a default scenario."""
    if (type(requirement_ids) is not tuple or not 1 <= len(requirement_ids) <= MAX_QA_CASES
            or any(type(value) is not UUID or value.version != 4 for value in requirement_ids)
            or len(set(requirement_ids)) != len(requirement_ids)
            or type(selectors) is not dict or not selectors
            or any(type(tool) is not str or tool not in _TOOLS for tool in selectors)):
        raise QAContractError()
    copied = {}
    for tool in _TOOLS:
        if tool not in selectors:
            continue
        names = selectors[tool]
        if (type(names) is not tuple or not 1 <= len(names) <= MAX_QA_SELECTORS_PER_TOOL
                or any(type(name) is not str for name in names)
                or len(set(names)) != len(names)):
            raise QAContractError()
        for name in names:
            _selector(name)
        copied[tool] = tuple(names)
    return tuple(str(value) for value in requirement_ids), copied


@dataclass(frozen=True, kw_only=True)
class QACaseBinding:
    tool_name: str = field(repr=False)
    selector: str = field(repr=False)
    test_id: str = field(repr=False)
    requirement_id: UUID = field(repr=False)
    title: str = field(repr=False)
    expected_result: str = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            if (type(self.tool_name) is not str or self.tool_name not in _TOOLS
                    or type(self.requirement_id) is not UUID or self.requirement_id.version != 4):
                raise QAContractError()
            _selector(self.selector)
            _text(self.test_id, max_length=MAX_QA_TEST_ID_BYTES, max_bytes=MAX_QA_TEST_ID_BYTES)
            _text(self.title, max_length=MAX_QA_TITLE_LENGTH, max_bytes=MAX_QA_TITLE_BYTES)
            _text(self.expected_result, max_length=MAX_QA_EXPECTED_RESULT_LENGTH,
                  max_bytes=MAX_QA_EXPECTED_RESULT_BYTES)
        except Exception:
            invalid = True
        if invalid:
            raise QAContractError() from None


def _validate_branch(kind, cases, questions, *, cases_type, questions_type):
    if (type(kind) is not str or kind not in _KINDS
            or type(cases) is not cases_type or len(cases) > MAX_QA_CASES
            or type(questions) is not questions_type or len(questions) > MAX_QA_QUESTIONS):
        raise QAContractError()
    for question in questions:
        _text(question, max_length=MAX_QA_QUESTION_LENGTH, max_bytes=MAX_QA_QUESTION_BYTES)
    if ((kind == "READY" and (not cases or questions))
            or (kind == "INPUT_REQUIRED" and (cases or not questions))
            or (kind == "REJECTED" and (cases or questions))):
        raise QAContractError()


@dataclass(frozen=True, kw_only=True)
class QADecision:
    kind: str
    cases: tuple[QACaseBinding, ...] = field(repr=False)
    questions: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        invalid = False
        try:
            _validate_branch(self.kind, self.cases, self.questions,
                             cases_type=tuple, questions_type=tuple)
            if any(type(case) is not QACaseBinding for case in self.cases):
                raise QAContractError()
            keys = [(case.tool_name, case.selector, case.test_id) for case in self.cases]
            if len(set(keys)) != len(keys):
                raise QAContractError()
        except Exception:
            invalid = True
        if invalid:
            raise QAContractError() from None


def build_qa_output_contract(requirement_ids: tuple[UUID, ...],
                             selectors: dict[str, tuple[str, ...]]) -> StructuredOutput:
    """A closed, all-required strict Draft with no model-supplied results.

    Tool/selector pair consistency and complete Requirement coverage are
    checked locally. This schema does not load or weaken canonical Artifacts.
    Constructor calls perform no filesystem, process, network or MCP I/O.
    """
    invalid = False
    try:
        ids, scopes = _host_scope(requirement_ids, selectors)
        names = list(dict.fromkeys(name for values in scopes.values() for name in values))
        schema = JsonSchema.from_dict({
            "type": "object", "additionalProperties": False,
            "required": ["kind", "cases", "questions"],
            "properties": {
                "kind": {"type": "string", "enum": ["READY", "INPUT_REQUIRED", "REJECTED"]},
                "cases": {"type": "array", "maxItems": MAX_QA_CASES, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["toolName", "selector", "testId", "requirementId", "title", "expectedResult"],
                    "properties": {
                        "toolName": {"type": "string", "enum": list(scopes)},
                        "selector": {"type": "string", "enum": names},
                        "testId": {"type": "string", "minLength": 1, "maxLength": MAX_QA_TEST_ID_BYTES},
                        "requirementId": {"type": "string", "enum": list(ids)},
                        "title": {"type": "string", "minLength": 1, "maxLength": MAX_QA_TITLE_LENGTH},
                        "expectedResult": {"type": "string", "minLength": 1, "maxLength": MAX_QA_EXPECTED_RESULT_LENGTH},
                    },
                }},
                "questions": {"type": "array", "maxItems": MAX_QA_QUESTIONS,
                    "items": {"type": "string", "minLength": 1, "maxLength": MAX_QA_QUESTION_LENGTH}},
            },
        })
        schema.require_openai_strict()
        result = StructuredOutput(name="qa_decision", schema=schema)
    except Exception:
        invalid = True
    if invalid:
        raise QAContractError() from None
    return result


def validate_qa_decision(data: dict, requirement_ids: tuple[UUID, ...],
                         selectors: dict[str, tuple[str, ...]]) -> QADecision:
    """Admit case bindings, never execute cases or invent a test outcome.

    Recognizable credentials are rejected, not silently rewritten. This does
    not detect arbitrary secrets in prose or attest that hostile tests are
    honest. Actual receipt IDs, outcomes and final report IDs belong to Host.
    """
    invalid = False
    try:
        ids, scopes = _host_scope(requirement_ids, selectors)
        if type(data) is not dict or set(data) != _FIELDS:
            raise QAContractError()
        _validate_branch(data["kind"], data["cases"], data["questions"],
                         cases_type=list, questions_type=list)
        bindings = []
        for row in data["cases"]:
            if type(row) is not dict or set(row) != _CASE_FIELDS:
                raise QAContractError()
            if (type(row["toolName"]) is not str or row["toolName"] not in scopes
                    or type(row["selector"]) is not str or row["selector"] not in scopes[row["toolName"]]
                    or type(row["requirementId"]) is not str or row["requirementId"] not in ids):
                raise QAContractError()
            bindings.append(QACaseBinding(
                tool_name=row["toolName"], selector=row["selector"], test_id=row["testId"],
                requirement_id=UUID(row["requirementId"]), title=row["title"], expected_result=row["expectedResult"],
            ))
        copied = parse_json(json_text(data, max_bytes=MAX_QA_JSON_BYTES), max_bytes=MAX_QA_JSON_BYTES)
        build_qa_output_contract(requirement_ids, selectors).schema.validate(copied)
        if redact_data(copied) != copied:
            raise QAContractError()
        result = QADecision(kind=copied["kind"], cases=tuple(bindings), questions=tuple(copied["questions"]))
        if result.kind == "READY" and {str(case.requirement_id) for case in result.cases} != set(ids):
            raise QAContractError()
    except Exception:
        invalid = True
    if invalid:
        raise QAContractError() from None
    return result
