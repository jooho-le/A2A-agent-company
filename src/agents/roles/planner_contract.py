"""Bounded model decisions assembled into the existing protected Plan contract.

The model can divide work or ask questions, but cannot supply Requirements,
project IDs, Artifact metadata, model settings, Tool results or a product
verdict. Canonical Requirements come only from the trusted Host scenario.
"""

from dataclasses import dataclass, field
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from agents.llm.contracts import JsonSchema, StructuredOutput, json_text, parse_json
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.core.security import redact_data
from orchestrator.domain.scenario_registry import ScenarioDefinition


MAX_PLANNER_TASKS = 128
MAX_PLANNER_QUESTIONS = 8
MAX_PLANNER_JSON_BYTES = 1_048_576
_KINDS = frozenset({"PLAN", "INPUT_REQUIRED", "REJECTED"})
_SCHEMA_PATH = Path(__file__).absolute().parents[3] / "schemas" / "project" / "planner_output.schema.json"


class PlannerContractError(ValueError):
    """A safe reason only; no model decision, question or validation exception."""

    code = "PLANNER_OUTPUT_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _text(value, maximum):
    if (type(value) is not str or not value.strip() or len(value) > maximum
            or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in value)
            or redact_data(value) != value):
        raise PlannerContractError()
    return value


@dataclass(frozen=True, kw_only=True)
class PlannerDecision:
    kind: str
    plan: PlannerPlan | None = field(repr=False)
    questions: tuple[str, ...] = field(repr=False)

    def __post_init__(self):
        if (type(self.kind) is not str or self.kind not in _KINDS or type(self.questions) is not tuple
                or len(self.questions) > MAX_PLANNER_QUESTIONS):
            raise PlannerContractError()
        for question in self.questions:
            _text(question, 1024)
        if ((self.kind == "PLAN" and (not isinstance(self.plan, PlannerPlan) or self.questions))
                or (self.kind == "INPUT_REQUIRED" and (self.plan is not None or not self.questions))
                or (self.kind == "REJECTED" and (self.plan is not None or self.questions))):
            raise PlannerContractError()


def _scenario_ids(scenario):
    if not isinstance(scenario, ScenarioDefinition):
        raise PlannerContractError()
    values = [str(value) for value in scenario.requirement_ids]
    if not values or len(values) > MAX_PLANNER_TASKS or len(values) != len(set(values)):
        raise PlannerContractError()
    return values


def build_planner_output_contract(scenario: ScenarioDefinition) -> StructuredOutput:
    """All-required closed decision schema compatible with provider strict mode.

    This deliberately is not a weakened copy of the Artifact Schema: the
    smaller model decision is validated and Host-assembled into that contract.
    Host scenario admission/authenticity belongs to the context capability.
    """
    invalid = False
    try:
        requirement_ids = _scenario_ids(scenario)
        task_key = {"type": "string", "pattern": r"^TASK-[A-Z0-9_-]{1,64}$", "minLength": 6, "maxLength": 69}
        schema = JsonSchema.from_dict({
            "type": "object", "additionalProperties": False,
            "required": ["kind", "implementationPlan", "questions"],
            "properties": {
                "kind": {"type": "string", "enum": ["PLAN", "INPUT_REQUIRED", "REJECTED"]},
                "implementationPlan": {"type": "array", "maxItems": MAX_PLANNER_TASKS, "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["taskId", "title", "description", "requirementIds", "dependsOn"],
                    "properties": {
                        "taskId": task_key,
                        "title": {"type": "string", "minLength": 1, "maxLength": 256},
                        "description": {"type": "string", "minLength": 1, "maxLength": 4096},
                        "requirementIds": {"type": "array", "minItems": 1, "maxItems": len(requirement_ids),
                            "items": {"type": "string", "enum": requirement_ids}},
                        "dependsOn": {"type": "array", "maxItems": MAX_PLANNER_TASKS, "items": task_key},
                    },
                }},
                "questions": {"type": "array", "maxItems": MAX_PLANNER_QUESTIONS,
                    "items": {"type": "string", "minLength": 1, "maxLength": 1024}},
            },
        })
        schema.require_openai_strict()
        output = StructuredOutput(name="planner_decision", schema=schema)
    except Exception:
        invalid = True
    if invalid:
        raise PlannerContractError() from None
    return output


def _canonical_schema():
    """Read only the shipped project schema; never resolve model-supplied URLs.

    The repository's existing schema is the Artifact contract, including its
    local $defs/refs. A missing/corrupt shipped asset fails closed. Its $id is
    an identifier, not a location fetched by this validator.
    """
    schema = parse_json(_SCHEMA_PATH.read_text(encoding="utf-8"), max_bytes=MAX_PLANNER_JSON_BYTES)

    def local_only(node):
        if type(node) is dict:
            if "$dynamicRef" in node or "$recursiveRef" in node:
                raise PlannerContractError()
            if "$ref" in node and (type(node["$ref"]) is not str or not node["$ref"].startswith("#")):
                raise PlannerContractError()
            for value in node.values():
                local_only(value)
        elif type(node) is list:
            for value in node:
                local_only(value)

    local_only(schema)
    Draft202012Validator.check_schema(schema)
    return schema


def validate_planner_decision(data: dict, scenario: ScenarioDefinition) -> PlannerDecision:
    """Validate a model decision without persisting it or completing a Task.

    Recognizable credential assignments/hashes/tokens are rejected rather than
    silently rewritten into a different planning decision. This recognition is
    not a guarantee that arbitrary prose contains no secret. LLM wire output
    must already cross its existing redaction boundary as well.
    """
    invalid = False
    try:
        contract = build_planner_output_contract(scenario)
        # Round-trip bounded native JSON so returned Plan/list objects do not
        # alias a mutable submitted decision. Duplicate JSON wire keys and
        # nonfinite numbers are rejected by the earlier LLM decoder too.
        copied = parse_json(json_text(data, max_bytes=MAX_PLANNER_JSON_BYTES), max_bytes=MAX_PLANNER_JSON_BYTES)
        contract.schema.validate(copied)
        if redact_data(copied) != copied:
            raise PlannerContractError()
        kind = copied["kind"]
        tasks, questions = copied["implementationPlan"], copied["questions"]
        for task in tasks:
            _text(task["title"], 256)
            _text(task["description"], 4096)
        for question in questions:
            _text(question, 1024)
        if kind == "PLAN":
            if not tasks or questions:
                raise PlannerContractError()
            plan = PlannerPlan.model_validate({
                "schemaVersion": 1,
                "requirements": [{
                    "requirementId": str(requirement.requirement_id),
                    "key": requirement.key, "description": requirement.description,
                    "acceptanceCriteria": list(requirement.acceptance_criteria),
                } for requirement in scenario.requirements],
                "implementationPlan": tasks,
            })
            scenario.validate_planner_requirements(plan.requirements)
            payload = plan.model_dump(mode="json", by_alias=True)
            if redact_data(payload) != payload:
                raise PlannerContractError()
            Draft202012Validator(_canonical_schema(), format_checker=FormatChecker()).validate(payload)
            result = PlannerDecision(kind=kind, plan=plan, questions=())
        elif kind == "INPUT_REQUIRED":
            if tasks or not questions:
                raise PlannerContractError()
            result = PlannerDecision(kind=kind, plan=None, questions=tuple(questions))
        else:
            if tasks or questions:
                raise PlannerContractError()
            result = PlannerDecision(kind="REJECTED", plan=None, questions=())
    except Exception:
        invalid = True
    if invalid:
        # Outside except: no raw Pydantic/JSONSchema content in __context__.
        raise PlannerContractError() from None
    return result
