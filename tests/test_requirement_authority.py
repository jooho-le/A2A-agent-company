"""Frozen authority and hostile-baseline regressions; no model or tool calls."""

from dataclasses import FrozenInstanceError
import hashlib
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from orchestrator.domain.requirement_authority import RequirementAuthorityError, freeze_requirement_authority
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SCN_001_ID


class RequirementAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.configuration = RunConfigurationArtifact(run_id=uuid4(), scenario_id=SCN_001_ID, workspace_id=uuid4())

    def assert_invalid(self, value):
        with self.assertRaises(RequirementAuthorityError) as caught:
            freeze_requirement_authority(value)
        self.assertEqual(caught.exception.args, ("REQUIREMENT_AUTHORITY_INVALID",))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def with_contract(self, contract):
        raw = contract if type(contract) is str else json.dumps(contract, ensure_ascii=False)
        # Corruption is injected below the normal frozen-model admission to
        # exercise revalidation at the actual consumer boundary.
        return self.configuration.model_copy(update={"frozen_scenario_contract_json": raw})

    def test_ledger_preserves_exact_frozen_requirement_text_and_ids(self):
        authority = freeze_requirement_authority(self.configuration)
        canonical = self.configuration.scenario_contract["requirements"]
        self.assertEqual(len(authority.requirements), 8)
        for approved, frozen in zip(authority.requirements, canonical):
            self.assertEqual(str(approved.requirement_id), frozen["requirementId"])
            self.assertEqual(approved.description, frozen["description"])
            self.assertEqual(approved.acceptance_criteria, tuple(frozen["acceptanceCriteria"]))
        self.assertEqual(authority.scenario_contract_sha256,
            hashlib.sha256(self.configuration.frozen_scenario_contract_json.encode("utf-8")).hexdigest())

    def test_current_registry_is_never_used_to_replace_a_frozen_run(self):
        with patch("orchestrator.domain.scenario_registry.get_scenario", side_effect=AssertionError("registry changed")):
            authority = freeze_requirement_authority(self.configuration)
        self.assertEqual(authority.run_id, self.configuration.run_id)
        self.assertEqual(authority.configuration_artifact_id, self.configuration.artifact_id)

    def test_immutable_ledger_and_prompt_copy_do_not_alias_each_other(self):
        authority = freeze_requirement_authority(self.configuration)
        with self.assertRaises(FrozenInstanceError):
            authority.requirements[0].description = "new access restriction"
        presentation = authority.to_prompt_json()
        presentation["approvedRequirementIds"].clear()
        presentation["modelCanApproveConditions"] = True
        fresh = authority.to_prompt_json()
        self.assertEqual(len(fresh["approvedRequirementIds"]), 8)
        self.assertFalse(fresh["modelCanApproveConditions"])
        self.assertFalse(fresh["requestCanChangeBaseline"])
        self.assertFalse(fresh["clarificationCanChangeBaseline"])
        self.assertNotIn(authority.requirements[0].description, repr(authority))

    def test_presentation_is_a_compact_reference_not_duplicate_criteria(self):
        authority = freeze_requirement_authority(self.configuration)
        view = authority.to_prompt_json()
        self.assertEqual(view["scenarioContractSha256"], self.configuration.scenario_contract_sha256)
        self.assertEqual(view["approvalSource"], "HOST_FROZEN_SCENARIO_CONTRACT")
        self.assertEqual(view["unapprovedConditionsAction"], "INPUT_REQUIRED")
        self.assertNotIn("requirements", view)

    def test_model_approval_claim_is_not_an_admitted_configuration(self):
        for value in (None, {"approvalStatus": "APPROVED"}, {"requirementAuthority": "trusted"}):
            with self.subTest(type=type(value)):
                self.assert_invalid(value)

    def test_corrupt_host_metadata_is_not_published_as_an_authority_reference(self):
        for name in ("run_id", "scenario_id", "artifact_id"):
            with self.subTest(field=name):
                self.assert_invalid(self.configuration.model_copy(update={name: "api_key=private-reference-value"}))

    def test_duplicate_json_keys_and_nonfinite_values_are_rejected(self):
        raw = self.configuration.frozen_scenario_contract_json
        for value in (raw.replace('"scenarioId":', '"scenarioId":"forged","scenarioId":', 1),
                      raw[:-1] + ',"extra":NaN}', raw[:-1] + ',"extra":Infinity}'):
            self.assert_invalid(self.with_contract(value))

    def test_changed_identity_missing_criteria_and_duplicates_fail_closed(self):
        for mutation in ("scenario", "requirement", "duplicate", "missing", "blank", "trim", "validator"):
            data = self.configuration.scenario_contract
            if mutation == "scenario":
                data["scenarioId"] = str(uuid4())
            elif mutation == "requirement":
                data["requirements"][0]["requirementId"] = "not-a-uuid"
            elif mutation == "duplicate":
                data["requirements"].append(data["requirements"][0])
            elif mutation == "missing":
                del data["requirements"][0]["acceptanceCriteria"]
            elif mutation == "blank":
                data["requirements"][0]["acceptanceCriteria"] = [" "]
            elif mutation == "trim":
                data["requirements"][0]["description"] += " "
            else:
                data["requirements"][0]["validators"] = ["MODEL"]
            with self.subTest(mutation=mutation):
                self.assert_invalid(self.with_contract(data))

    def test_known_credential_is_neither_approved_nor_echoed_in_errors(self):
        data = self.configuration.scenario_contract
        data["requirements"][0]["description"] = "api_key=authority-private-fixture-value"
        self.assert_invalid(self.with_contract(data))


if __name__ == "__main__":
    unittest.main()
