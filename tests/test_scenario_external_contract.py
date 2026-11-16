"""SCN-001 connection contract (externalContract) reaches every role unchanged.

The hidden evaluation suite imports main.app, database.get_db and models.User and
posts to /api/auth/signup. These tests keep the frozen contract, its restore path,
the QA/Security request filters and the semantic-review basis consistent.
"""

import copy
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from agents.llm.content import sanitize_content
from agents.runtime import qa_context, security_context
from agents.semantic_review import approved_basis
from orchestrator.application.control_input import validate_control_input
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.application.validation_request import build_validation_request
from orchestrator.core.security import redact_data
from orchestrator.domain.requirement_authority import freeze_requirement_authority
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import (
    SCN_001_ID, RequirementValidator, ScenarioDefinition, get_scenario,
)
from orchestrator.domain.states import AgentRole


def _strings_only(value):
    if isinstance(value, str):
        return True
    if isinstance(value, list):
        return all(isinstance(item, str) for item in value)
    if isinstance(value, dict):
        return all(_strings_only(item) for item in value.values())
    return False


def _artifact():
    return RunConfigurationArtifact(runId=uuid4(), scenarioId=SCN_001_ID, workspaceId=uuid4())


def _plan(contract):
    return PlannerPlan.model_validate({
        "schemaVersion": 1,
        "requirements": [{key: item[key] for key in ("requirementId", "key", "description", "acceptanceCriteria")}
                         for item in contract["requirements"]],
        "implementationPlan": [{
            "taskId": "TASK-SIGNUP", "title": "회원가입 구현", "description": "보호된 요구사항에 따라 회원가입 기능 구현",
            "requirementIds": [item["requirementId"] for item in contract["requirements"]], "dependsOn": [],
        }],
    })


class ScenarioExternalContractTests(unittest.TestCase):
    def setUp(self):
        self.contract = get_scenario(SCN_001_ID).planner_contract()
        self.external = self.contract["externalContract"]

    def test_registry_contract_names_the_connection_points(self):
        self.assertEqual((self.external["app"]["module"], self.external["app"]["attribute"]), ("main", "app"))
        self.assertEqual(self.external["app"]["serve"], "uvicorn main:app")
        self.assertEqual((self.external["database"]["module"], self.external["database"]["dependency"]),
                         ("database", "get_db"))
        self.assertEqual(self.external["database"]["urlEnv"], "DATABASE_URL")
        self.assertEqual((self.external["userModel"]["module"], self.external["userModel"]["class"]), ("models", "User"))
        self.assertEqual(self.external["userModel"]["columns"], ["email", "password_hash"])
        self.assertEqual(self.external["userModel"]["uniqueInModel"], ["email"])
        self.assertEqual((self.external["signupApi"]["method"], self.external["signupApi"]["path"]),
                         ("POST", "/api/auth/signup"))
        self.assertEqual(self.external["signupApi"]["bodyFields"], ["email", "password"])
        # Semantic review can quote only string values as approved policy.
        self.assertTrue(_strings_only(self.external))

    def test_contract_survives_secret_redaction_checks(self):
        self.assertEqual(redact_data(self.contract), self.contract)
        sanitize_content(self.contract, reject_secrets=True)
        artifact = _artifact()
        self.assertEqual(artifact.scenario_contract["externalContract"], self.external)
        authority = freeze_requirement_authority(artifact)
        self.assertEqual(authority.scenario_contract_sha256, artifact.scenario_contract_sha256)

    def test_secret_looking_field_names_would_invalidate_the_contract(self):
        broken = copy.deepcopy(self.contract)
        broken["externalContract"]["signupApi"]["body"] = {"email": "string", "password": "string"}
        self.assertNotEqual(redact_data(broken), broken)

    def test_restore_keeps_the_field_and_old_contracts_stay_unchanged(self):
        frozen = _artifact().scenario_contract
        self.assertEqual(ScenarioDefinition.from_contract(frozen).planner_contract(), frozen)
        old = {key: value for key, value in frozen.items() if key != "externalContract"}
        restored = ScenarioDefinition.from_contract(old).planner_contract()
        self.assertEqual(restored, old)
        self.assertNotIn("externalContract", restored)

    def test_orchestrator_planner_payload_equals_planner_expectation(self):
        artifact = _artifact()
        sent = ScenarioDefinition.from_contract(artifact.scenario_contract).planner_contract()
        self.assertEqual(sent, redact_data(artifact.scenario_contract))

    def test_validation_requests_carry_contract_and_match_agent_reproduction(self):
        artifact = _artifact()
        scenario = ScenarioDefinition.from_contract(artifact.scenario_contract)
        plan = _plan(artifact.scenario_contract)
        # The frozen contract is stored with sorted keys, and the request reuses that order.
        encoded = json.dumps(self.external, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for role, validator, module in ((AgentRole.QA, RequirementValidator.QA, qa_context),
                                        (AgentRole.SECURITY, RequirementValidator.SECURITY, security_context)):
            with self.subTest(role=role.value):
                ids = scenario.requirement_ids_for(validator)
                text = build_validation_request(artifact, plan, scenario, ids, role)
                self.assertIn('"externalContract":' + encoded, text)
                self.assertEqual(text, module._dispatch_request(artifact, plan, scenario, ids))

    def test_semantic_review_basis_includes_contract(self):
        execution = SimpleNamespace(configuration=_artifact(), metadata=SimpleNamespace(requirement_ids=None))
        basis = approved_basis(execution, AgentRole.PLANNER)
        self.assertEqual(basis["policies"]["externalContract"], self.external)

    def test_clarification_cannot_replace_contract(self):
        with self.assertRaises(ValueError):
            validate_control_input({"externalContract": {"signupApi": {"path": "/signup"}}})


if __name__ == "__main__":
    unittest.main()
