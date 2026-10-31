"""One actor, several draft shapes; drafts are never execution evidence."""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Callable

from agents.core.identity import SinglePhase
from agents.llm.contracts import StructuredOutput
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from agents.roles.planner_contract import build_planner_output_contract, validate_planner_decision
from agents.roles.qa_contract import build_qa_output_contract, validate_qa_decision
from agents.roles.security_contract import build_security_output_contract, validate_security_decision
from orchestrator.domain.scenario_registry import RequirementValidator, ScenarioDefinition


class SingleContractError(ValueError):
    def __init__(self):
        super().__init__("SINGLE_AGENT_DRAFT_INVALID")


@dataclass(frozen=True)
class SingleDraftContract:
    phase: SinglePhase
    scenario_contract_sha256: str = field(repr=False)
    output: StructuredOutput = field(repr=False)
    _validate: Callable = field(repr=False, compare=False)

    def validate(self, data: dict):
        try:
            self.output.schema.validate(data)
            return self._validate(data)
        except Exception:
            raise SingleContractError() from None


def scenario_contract_sha256(scenario: ScenarioDefinition) -> str:
    return sha256(json.dumps(scenario.planner_contract(), ensure_ascii=False,
                             allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_single_draft_contract(
    phase: SinglePhase, *, scenario: ScenarioDefinition,
    selectors: dict[str, tuple[str, ...]] | None = None,
    finding_ids: tuple[str, ...] = (), source_paths: tuple[str, ...] = (),
    security_test_selectors: dict[str, tuple[str, ...]] | None = None,
) -> SingleDraftContract:
    """Use the existing bounded decision validators, without running Agents.

    All scope arguments are trusted Host inputs, not fields from an A2A body.
    The optional measured Security contract follows dev's test_selectors API;
    a checkout without that API refuses it instead of falling back to static
    proof. Test execution/receipt assembly belongs to later Host integration.
    """
    try:
        phase = SinglePhase(phase)
        if not isinstance(scenario, ScenarioDefinition):
            raise SingleContractError()
        if phase is SinglePhase.PLANNING:
            output = build_planner_output_contract(scenario)
            validate = lambda data: validate_planner_decision(data, scenario)
        elif phase in (SinglePhase.IMPLEMENTING, SinglePhase.FIXING):
            output = build_developer_output_contract()
            validate = validate_developer_decision
        elif phase is SinglePhase.QA:
            ids = scenario.requirement_ids_for(RequirementValidator.QA)
            scopes = {tool: tuple(names) for tool, names in (selectors or {}).items()}
            output = build_qa_output_contract(ids, scopes)
            validate = lambda data: validate_qa_decision(data, ids, scopes)
        else:
            ids = scenario.requirement_ids_for(RequirementValidator.SECURITY)
            findings, paths = tuple(finding_ids), tuple(source_paths)
            options = {} if security_test_selectors is None else {
                "test_selectors": {tool: tuple(names) for tool, names in security_test_selectors.items()},
            }
            output = build_security_output_contract(ids, findings, paths, **options)
            validate = lambda data: validate_security_decision(data, ids, findings, paths, **options)
        output.schema.require_openai_strict()
        return SingleDraftContract(phase, scenario_contract_sha256(scenario), output, validate)
    except Exception:
        raise SingleContractError() from None
