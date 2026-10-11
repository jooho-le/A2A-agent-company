"""42 Host approval integration, with temporary Git and inert providers only."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.platform.composition import PlatformPreparationError
from agents.platform.host import HostFactoryError
from agents.platform.security_approval import calculate_implementation_sha256, load_approved_assessor
from orchestrator.domain.scenario_registry import SCENARIO_REGISTRY, SCN_001_ID
from test_owned_host import _HostFixture
import test_owned_host as host_tests


def inert_assessment(request):
    # Absence of independent evidence must not generate PASS.
    return None


class _ApprovalFixture(_HostFixture):
    def setUp(self):
        super().setUp()
        self.approval_file = self.base / "security-approval.json"
        self.dependency = self.base / "trusted_dependency.py"
        self.dependency.write_text("# inert declared trusted dependency\n", encoding="utf-8")
        module = Path(__file__).resolve()
        self.files = (("assessor", module, sha256(module.read_bytes()).hexdigest()),
                      ("dependency", self.dependency, sha256(self.dependency.read_bytes()).hexdigest()))
        contract = SCENARIO_REGISTRY[SCN_001_ID].planner_contract()
        self.definition = {
            "schemaVersion": 1, "verifierId": "inert-test-assessor", "verifierVersion": "1.0.0",
            "criteriaRef": "criteria://inert-host-test/version-1",
            "scenarioContractSha256": sha256(json.dumps(contract, ensure_ascii=False, sort_keys=True,
                separators=(",", ":")).encode("utf-8")).hexdigest(),
            "criteria": {"purpose": "inert integration fixture, never product security evidence"},
            "implementationSha256": calculate_implementation_sha256(inert_assessment, self.files),
            "implementationFiles": [{"name": name, "path": str(path), "sha256": digest}
                                    for name, path, digest in self.files],
        }
        self.approval_file.write_text(json.dumps(self.definition), encoding="utf-8")
        self.approved = load_approved_assessor(self.approval_file, inert_assessment)

    def changed_approval(self, **changes):
        definition = {**deepcopy(self.definition), **changes}
        self.approval_file.write_text(json.dumps(definition), encoding="utf-8")
        return load_approved_assessor(self.approval_file, inert_assessment)


class SecurityHostInspectionTests(_ApprovalFixture, unittest.TestCase):
    def test_approved_assessor_is_hash_bound_without_running_assessor_or_creating_storage(self):
        baseline = self.inspect().policy_sha256
        with patch("agents.platform.host.SQLiteWorkflowRepository", side_effect=AssertionError("inert")):
            approved = self.inspect(proof_verifier=self.approved)
        self.assertNotEqual(approved.policy_sha256, baseline)
        self.assertEqual(self.inspect(proof_verifier=self.approved).policy_sha256, approved.policy_sha256)
        self.assertFalse((self.directory / ".data").exists())

    def test_declared_criteria_version_content_and_scenario_change_the_host_fingerprint(self):
        before = self.inspect(proof_verifier=self.approved).policy_sha256
        for changes in ({"verifierVersion": "2.0.0"}, {"criteriaRef": "criteria://inert-host-test/version-2"},
                        {"criteria": {"purpose": "different approved inert criteria"}},
                        {"scenarioContractSha256": "1" * 64}):
            with self.subTest(changes=changes):
                approval = self.changed_approval(**changes)
                self.assertNotEqual(self.inspect(proof_verifier=approval).policy_sha256, before)

    def test_dependency_drift_fails_closed_before_storage_creation(self):
        self.dependency.write_text("# changed trusted dependency\n", encoding="utf-8")
        with self.assertRaises(HostFactoryError):
            self.inspect(proof_verifier=self.approved)
        self.assertFalse((self.directory / ".data").exists())

    def test_approval_files_cannot_be_in_agent_workspace(self):
        self.runtime["workspaceRoot"] = str(self.base)
        self.write()
        with self.assertRaises(HostFactoryError):
            self.inspect(proof_verifier=self.approved)
        self.assertFalse((self.directory / ".data").exists())

    def test_reference_repository_cannot_supply_the_trusted_assessor_asset(self):
        asset = self.seed / "trusted_dependency.py"
        asset.write_text("# not an approved Host boundary\n", encoding="utf-8")
        files = (self.files[0], ("dependency", asset, sha256(asset.read_bytes()).hexdigest()))
        approved = self.changed_approval(
            implementationFiles=[{"name": name, "path": str(path), "sha256": digest} for name, path, digest in files],
            implementationSha256=calculate_implementation_sha256(inert_assessment, files))
        with self.assertRaises(HostFactoryError):
            self.inspect(proof_verifier=approved)


class SecurityHostCreationTests(_ApprovalFixture, unittest.IsolatedAsyncioTestCase):
    claimed_run = host_tests.OwnedHostCreationTests.claimed_run

    async def test_approved_assessor_connects_existing_services_without_executing_it(self):
        platform = await self.host(proof_verifier=self.approved)
        self.addAsyncCleanup(platform.aclose)
        self.assertEqual(len(platform.endpoints), 5)
        platform.orchestrator_app.state.submission_validator(self.inspect(proof_verifier=self.approved).approved_configuration)
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
        self.assertFalse(platform.orchestrator_app.state.workspace_registry.base_path.exists())

    async def test_restarted_run_cannot_switch_approved_verifier_version(self):
        first = await self.host(proof_verifier=self.approved)
        self.addAsyncCleanup(first.aclose)
        run, _frozen, root = self.claimed_run(first)
        await first.dispatcher._before_dispatch(run.run_id)
        original = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
        await first.aclose()
        replacement = self.changed_approval(verifierVersion="2.0.0")
        second = await self.host(proof_verifier=replacement)
        self.addAsyncCleanup(second.aclose)
        with self.assertRaises(PlatformPreparationError):
            await second.dispatcher._before_dispatch(run.run_id)
        self.assertEqual(original, {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()})
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))

    async def test_running_host_checks_declared_dependency_before_new_submission(self):
        platform = await self.host(proof_verifier=self.approved)
        self.addAsyncCleanup(platform.aclose)
        configuration = self.inspect(proof_verifier=self.approved).approved_configuration
        self.dependency.write_text("# late changed implementation\n", encoding="utf-8")
        with self.assertRaises(Exception):
            platform.orchestrator_app.state.submission_validator(configuration)
        self.assertTrue(all(provider.completed == 0 for provider in self.providers))
