"""Host-derived requirement authority; model prose never grants approval.

The frozen Run Configuration remains the durable source of truth. This view
does not introduce a second registry, approve user text, or change that source.
Its digest is an identity check, not an authentication or approval credential.
"""

from dataclasses import dataclass, field
import hashlib
import json
import re
from uuid import UUID

from orchestrator.core.security import redact_data
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import RequirementValidator


_MAX_BYTES = 1_048_576


class RequirementAuthorityError(ValueError):
    def __init__(self):
        super().__init__("REQUIREMENT_AUTHORITY_INVALID")


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _constant(_value):
    raise ValueError


def _text(value):
    if (type(value) is not str or not value or value != value.strip()
            or any(ord(char) < 32 and char not in "\n\t" or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError
    return value


def _identity(value):
    result = UUID(value)
    if result.version != 4 or str(result) != value:
        raise ValueError
    return result


@dataclass(frozen=True, kw_only=True)
class ApprovedRequirement:
    requirement_id: UUID
    key: str
    description: str = field(repr=False)
    acceptance_criteria: tuple[str, ...] = field(repr=False)
    validators: tuple[RequirementValidator, ...] = field(repr=False)


@dataclass(frozen=True, kw_only=True)
class RequirementAuthority:
    run_id: UUID
    scenario_id: UUID
    configuration_artifact_id: UUID
    scenario_contract_sha256: str
    requirements: tuple[ApprovedRequirement, ...] = field(repr=False)

    def to_prompt_json(self) -> dict[str, object]:
        """Reference the already supplied contract instead of duplicating prose."""
        return {
            "schemaVersion": 1,
            "runId": str(self.run_id),
            "scenarioId": str(self.scenario_id),
            "configurationArtifactId": str(self.configuration_artifact_id),
            "scenarioContractSha256": self.scenario_contract_sha256,
            "approvalSource": "HOST_FROZEN_SCENARIO_CONTRACT",
            "approvedRequirementIds": [str(item.requirement_id) for item in self.requirements],
            "requestCanChangeBaseline": False,
            "clarificationCanChangeBaseline": False,
            "modelCanApproveConditions": False,
            "unapprovedConditionsAction": "INPUT_REQUIRED",
        }


def freeze_requirement_authority(configuration: RunConfigurationArtifact) -> RequirementAuthority:
    """Read an existing, admitted Host snapshot; never consult today's registry.

    Only the trusted runtime calls this factory. A model-supplied authority
    dictionary or an APPROVED label is not accepted as a configuration.
    """
    invalid = False
    try:
        if type(configuration) is not RunConfigurationArtifact:
            raise ValueError
        if any(type(value) is not UUID or value.version != 4 for value in (
                configuration.run_id, configuration.scenario_id, configuration.artifact_id)):
            raise ValueError
        raw = configuration.frozen_scenario_contract_json
        if type(raw) is not str or not 0 < len(raw.encode("utf-8")) <= _MAX_BYTES:
            raise ValueError
        contract = json.loads(raw, object_pairs_hook=_object, parse_constant=_constant)
        if (type(contract) is not dict or contract.get("scenarioId") != str(configuration.scenario_id)
                or redact_data(contract) != contract):
            raise ValueError
        _identity(contract["scenarioId"])
        _text(contract["scenarioKey"])
        _text(contract["name"])
        source = contract["requirements"]
        if type(source) is not list or not 1 <= len(source) <= 128:
            raise ValueError
        requirements = []
        for item in source:
            if type(item) is not dict:
                raise ValueError
            identity, key = _identity(item["requirementId"]), _text(item["key"])
            if re.fullmatch(r"REQ-[0-9]{3,}", key) is None:
                raise ValueError
            _text(item["category"])
            description = _text(item["description"])
            criteria, validators = item["acceptanceCriteria"], item["validators"]
            if (type(criteria) is not list or not 1 <= len(criteria) <= 1000
                    or type(validators) is not list or not validators
                    or any(type(value) is not str for value in validators)):
                raise ValueError
            criteria = tuple(_text(value) for value in criteria)
            if len({value.casefold() for value in criteria}) != len(criteria) or len(set(validators)) != len(validators):
                raise ValueError
            requirements.append(ApprovedRequirement(requirement_id=identity, key=key,
                description=description, acceptance_criteria=criteria,
                validators=tuple(RequirementValidator(value) for value in validators)))
        if (len({item.requirement_id for item in requirements}) != len(requirements)
                or len({item.key for item in requirements}) != len(requirements)):
            raise ValueError
        excluded = contract["excludedFeatures"]
        if type(excluded) is not list or len(excluded) > 1000:
            raise ValueError
        for value in excluded:
            _text(value)
        if type(contract["securityPolicy"]) is not dict or type(contract["emailPolicy"]) is not dict:
            raise ValueError
        result = RequirementAuthority(run_id=configuration.run_id, scenario_id=configuration.scenario_id,
            configuration_artifact_id=configuration.artifact_id,
            scenario_contract_sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            requirements=tuple(requirements))
    except Exception:
        invalid = True
    if invalid:
        # No private baseline or nested JSON error retained in the exception.
        raise RequirementAuthorityError() from None
    return result
