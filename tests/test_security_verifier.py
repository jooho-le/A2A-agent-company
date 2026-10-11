"""Approved semantic-adapter plumbing, not a real scenario security assessor.

Fixtures use immutable real Source/SQLite and the existing inert FakeDocker
scanner. No generated project code, container, network or LLM is executed.
The explicitly approved PASS fixture tests trust-boundary wiring only; its
observations are intentionally marked INERT_TEST_FIXTURE, not product proof.
"""

import asyncio
from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
import json
from pathlib import Path
from types import MappingProxyType
import unittest
from unittest.mock import patch
from uuid import UUID, uuid1, uuid4

from agents.llm.budget import ExecutionBudget
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.security_approval import (
    calculate_implementation_sha256, load_approved_assessor,
)
from agents.platform.security_proof_store import SecurityProofReceiptStore
from agents.platform.security_verifier import (
    SecurityAssessment, SecurityAssessmentRequest, SecurityVerifierError,
    TrustedSecurityProofVerifier,
)
from agents.roles.security_contract import SecurityDecision, SecurityRequirementReview
from agents.runtime.security_services import SecuritySemanticProof
from orchestrator.domain.states import WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_artifacts import ValidationOutcome
import test_security_agent_services as services_fixture


_REQUESTS = []
_REPLAY_ASSESSMENT = None


def canonical(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


def assessment_for(request, **changes):
    values = dict(request_id=request.request_id, request_sha256=request.request_sha256,
                  approval_sha256=request.approval_sha256)
    return SecurityAssessment(**{**values, **changes})


def assess_unverified(request):
    _REQUESTS.append(request)
    return assessment_for(request, requirement_outcomes=tuple(
        (identifier, "UNVERIFIED") for identifier in request.requirement_ids))


def assess_none(request):
    return None


def assess_inert_pass(request):
    """Inert, explicitly approved fixture: never SCN-001 product evidence."""
    _REQUESTS.append(request)
    return assessment_for(request, requirement_outcomes=tuple(
        (identifier, "PASS") for identifier in request.requirement_ids),
        finding_dispositions=tuple((identifier, "CONFIRMED") for identifier in request.finding_ids),
        evidence_refs=request.permitted_evidence_refs[:1], evidence_json=canonical({
            "fixtureKind": "INERT_TEST_FIXTURE",
            "independentObservation": "Adapter binding only; not scenario security evidence."}))


def assess_inert_fail(request):
    return assessment_for(request, requirement_outcomes=tuple(
        (identifier, "FAIL") for identifier in request.requirement_ids),
        evidence_refs=request.permitted_evidence_refs[:1],
        evidence_json=canonical({"fixtureKind": "INERT_TEST_FIXTURE", "observation": "Fixture failure."}))


def assess_inert_false_positive(request):
    return assessment_for(request, finding_dispositions=tuple(
        (identifier, "FALSE_POSITIVE") for identifier in request.finding_ids),
        evidence_refs=request.permitted_evidence_refs[:1],
        evidence_json=canonical({"fixtureKind": "INERT_TEST_FIXTURE", "observation": "Fixture disposition only."}))


def assess_replay_previous(request):
    global _REPLAY_ASSESSMENT
    if _REPLAY_ASSESSMENT is None:
        _REPLAY_ASSESSMENT = assessment_for(request)
    return _REPLAY_ASSESSMENT


def assess_without_refs(request):
    return assessment_for(request, requirement_outcomes=((request.requirement_ids[0], "PASS"),),
        evidence_json=canonical({"fixtureKind": "INERT_TEST_FIXTURE"}))


def assess_without_observations(request):
    return assessment_for(request, requirement_outcomes=((request.requirement_ids[0], "PASS"),),
                          evidence_refs=request.permitted_evidence_refs[:1])


def assess_foreign_requirement(request):
    return assessment_for(request, requirement_outcomes=((uuid4(), "PASS"),),
        evidence_refs=request.permitted_evidence_refs[:1], evidence_json=canonical({"fixtureKind": "INERT_TEST_FIXTURE"}))


def assess_foreign_finding(request):
    return assessment_for(request, finding_dispositions=(("foreign-finding", "CONFIRMED"),),
        evidence_refs=request.permitted_evidence_refs[:1], evidence_json=canonical({"fixtureKind": "INERT_TEST_FIXTURE"}))


def assess_foreign_evidence(request):
    return assessment_for(request, evidence_refs=(f"artifact://{uuid4()}/security-scan-report.json",))


def assess_wrong_request(request):
    return assessment_for(request, request_id=uuid4())


def assess_wrong_request_hash(request):
    return assessment_for(request, request_sha256="0" * 64)


def assess_wrong_approval(request):
    return assessment_for(request, approval_sha256="0" * 64)


def assess_legacy_proof(request):
    return SecuritySemanticProof(run_id=request.run_id, workflow_step_id=request.workflow_step_id,
        source_artifact_id=request.source_artifact_id, snapshot_sha256=request.snapshot_sha256)


def assess_untyped_mapping(request):
    return {"requestId": str(request.request_id), "requirementOutcomes": []}


def assess_privileged_mutation(request):
    result = assessment_for(request)
    object.__setattr__(result, "requirement_outcomes", [(request.requirement_ids[0], "PASS")])
    return result


async def assess_until_cancel(request):
    await asyncio.Event().wait()


def assess_budget_error(request):
    raise LLMRuntimeError(LLMErrorCode.BUDGET)


def assess_native_timeout(request):
    raise TimeoutError("inert-private-timeout")


class SecurityAssessmentContractTests(unittest.TestCase):
    def values(self):
        return dict(request_id=uuid4(), request_sha256="a" * 64, approval_sha256="b" * 64)

    def test_defaults_are_unverified_not_pass(self):
        value = SecurityAssessment(**self.values())
        self.assertEqual(value.requirement_outcomes, ())
        self.assertEqual(value.finding_dispositions, ())
        self.assertEqual(value.evidence_refs, ())
        self.assertEqual(json.loads(value.evidence_json), {})
        with self.assertRaises(FrozenInstanceError):
            value.request_sha256 = "c" * 64

    def test_closed_constructor_and_exact_identity_types(self):
        for changes in ({"request_id": uuid1()}, {"request_id": str(uuid4())},
                        {"request_sha256": True}, {"request_sha256": "A" * 64},
                        {"approval_sha256": "sha256:" + "b" * 64}):
            with self.subTest(changes=changes), self.assertRaises(SecurityVerifierError):
                SecurityAssessment(**{**self.values(), **changes})
        with self.assertRaises(TypeError):
            SecurityAssessment(**self.values(), model_says_pass=True)

    def test_requirement_pairs_exact_bounded_and_unique(self):
        identifier = uuid4()
        for outcomes in ([(identifier, "PASS")], ((identifier, True),),
                         ((str(identifier), "PASS"),), ((uuid1(), "PASS"),),
                         ((identifier, "SAFE"),), ((identifier, "PASS"), (identifier, "FAIL")),
                         tuple((uuid4(), "UNVERIFIED") for _ in range(257))):
            with self.subTest(kind=type(outcomes).__name__), self.assertRaises(SecurityVerifierError):
                SecurityAssessment(**self.values(), requirement_outcomes=outcomes)

    def test_finding_pairs_exact_bounded_and_unique(self):
        for dispositions in ([('finding', 'SUSPECTED')], ((True, 'CONFIRMED'),),
                             (("finding", True),), (("", "UNVERIFIED"),),
                             (("finding", "PASS"),), (("finding", "CONFIRMED"), ("finding", "SUSPECTED")),
                             tuple((str(index), "UNVERIFIED") for index in range(1001))):
            with self.subTest(kind=type(dispositions).__name__), self.assertRaises(SecurityVerifierError):
                SecurityAssessment(**self.values(), finding_dispositions=dispositions)

    def test_evidence_json_must_be_bounded_closed_finite_object(self):
        for content in (True, "[]", "null", '{"x": NaN}', '{"x": Infinity}',
                        '{"x": 1, "x": 2}', '{"x": "' + 'a' * (2 * 1024 * 1024) + '"}'):
            with self.subTest(kind=type(content).__name__), self.assertRaises(SecurityVerifierError):
                SecurityAssessment(**self.values(), evidence_json=content)

    def test_evidence_refs_are_exact_unique_artifact_references(self):
        reference = f"artifact://{uuid4()}/security-scan-report.json"
        for references in ([reference], (True,), ("https://example.invalid/proof",),
                           (reference, reference), ("",)):
            with self.subTest(references=references), self.assertRaises(SecurityVerifierError):
                SecurityAssessment(**self.values(), evidence_refs=references)

    def test_representation_never_contains_protected_facts(self):
        values = self.values()
        result = SecurityAssessment(**values, evidence_json=canonical({"privateObservation": "inert-private-fact"}))
        self.assertNotIn(str(values["request_id"]), repr(result))
        self.assertNotIn("inert-private-fact", repr(result))


class TrustedSecurityProofVerifierTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        global _REPLAY_ASSESSMENT
        _REPLAY_ASSESSMENT = None
        self.case = services_fixture.SecurityAgentServicesTests("test_constructor_is_inert_and_private_repr")
        self.case.setUp()
        # The helper IsolatedAsyncioTestCase has no own Runner. Transfer its
        # cleanups into this running case instead of calling its doCleanups.
        for cleanup, args, kwargs in self.case._cleanups:
            self.addCleanup(cleanup, *args, **kwargs)
        self.case._cleanups.clear()
        self.repository, self.artifacts = self.case.repository, self.case.artifacts
        self.execution = self.case.execution
        self.approval_file = self.case.fixture.directory / "semantic-approval.json"
        _REQUESTS.clear()

    def approve(self, assessor=assess_unverified, **changes):
        source_file = Path(__file__).resolve()
        files = (("test_security_verifier.py", source_file, sha256(source_file.read_bytes()).hexdigest()),)
        data = dict(schemaVersion=1, verifierId="inert-adapter-test", verifierVersion="1.0.0",
            criteriaRef="criteria://inert-adapter/v1",
            scenarioContractSha256=self.execution.configuration.scenario_contract_sha256,
            criteria={"fixtureKind": "INERT_TEST_FIXTURE", "purpose": "Adapter contracts, not scenario tests."},
            implementationSha256=calculate_implementation_sha256(assessor, files),
            implementationFiles=[{"name": name, "path": str(path), "sha256": digest} for name, path, digest in files])
        data.update(changes)
        self.approval_file.write_text(canonical(data), encoding="utf-8")
        return load_approved_assessor(self.approval_file, assessor)

    def verifier(self, assessor=assess_unverified, **changes):
        return TrustedSecurityProofVerifier(self.repository, self.approve(assessor, **changes), artifact_store=self.artifacts)

    async def measured(self, verifier=None, *, findings=False):
        if findings:
            self.case.set_scan_report((self.case.fixture.finding(),))
        services = self.case.services(proof_verifier=verifier)
        return await self.case.scan(services)

    def decision(self, measured, **changes):
        return self.case.decision(measured, **changes)

    def receipt(self, proof):
        self.assertIsInstance(proof.verification_receipt_id, UUID)
        return SecurityProofReceiptStore(self.repository).get(self.execution.metadata.run_id, proof.verification_receipt_id)

    async def proof(self, assessor=assess_unverified):
        verifier = self.verifier(assessor)
        services, tracked, measured = await self.measured(verifier)
        decision = self.decision(measured)
        return verifier, services, tracked, measured, decision, await verifier(self.execution, decision, measured)

    def test_constructor_is_database_inert_and_private(self):
        approved = self.approve()
        with patch.object(self.repository, "_transaction", side_effect=AssertionError("no writes")), \
                patch.object(self.repository, "_connection", side_effect=AssertionError("no reads")):
            verifier = TrustedSecurityProofVerifier(self.repository, approved, artifact_store=self.artifacts)
        self.assertNotIn(str(self.repository.database_path), repr(verifier))
        self.assertNotIn(str(self.approval_file), repr(verifier))

    def test_raw_callbacks_and_unbound_storage_are_rejected(self):
        for approval in (assess_unverified, lambda request: None, object()):
            with self.subTest(kind=type(approval).__name__), self.assertRaises(SecurityVerifierError):
                TrustedSecurityProofVerifier(self.repository, approval, artifact_store=self.artifacts)
        with self.assertRaises(SecurityVerifierError):
            TrustedSecurityProofVerifier(object(), self.approve(), artifact_store=self.artifacts)
        with self.assertRaises(SecurityVerifierError):
            TrustedSecurityProofVerifier(self.repository, self.approve(), artifact_store=object())

    async def test_none_assessment_cannot_upgrade_any_requirement(self):
        verifier, services, tracked, measured, decision, proof = await self.proof(assess_none)
        self.assertIsNone(proof)
        report = await self.case.finish(services, tracked, measured, decision)
        self.assertEqual({value.outcome for value in report.requirement_results}, {ValidationOutcome.UNVERIFIED})

    async def test_real_scan_request_and_receipt_match_current_source_and_environment(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        request = _REQUESTS[-1]
        self.assertIs(type(request), SecurityAssessmentRequest)
        self.assertEqual(request.run_id, self.execution.metadata.run_id)
        self.assertEqual(request.workflow_step_id, self.execution.metadata.workflow_step_id)
        self.assertEqual(request.workspace_id, self.execution.configuration.workspace_id)
        self.assertEqual(request.source_artifact_id, self.execution.source.artifact_id)
        self.assertEqual(request.snapshot_sha256, self.execution.source.snapshot_sha256)
        self.assertEqual(request.execution_manifest, self.execution.source.execution_manifest().model_dump(mode="json", by_alias=True))
        self.assertEqual(request.requirement_ids, self.execution.metadata.requirement_ids)
        self.assertEqual(request.finding_ids, measured.finding_ids)
        self.assertEqual(dict(request.source_files), dict(measured.source_files))
        self.assertIn(measured.scans[0][1].report_ref, request.permitted_evidence_refs)
        receipt = self.receipt(proof)
        self.assertEqual(receipt.request_id, request.request_id)
        self.assertEqual(receipt.request_sha256, request.request_sha256)
        self.assertEqual(receipt.approval_sha256, sha256(canonical(verifier.descriptor).encode()).hexdigest())
        self.assertEqual(receipt.requirement_outcomes, proof.requirement_outcomes)
        verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_request_is_deeply_detached_and_does_not_disclose_mutable_capabilities(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        request = _REQUESTS[-1]
        with self.assertRaises(FrozenInstanceError):
            request.request_sha256 = "f" * 64
        with self.assertRaises(TypeError):
            request.source_files["main.py"] = b"changed"
        for property_name in ("criteria", "scenario_contract", "decision", "scans", "execution_manifest"):
            first, second = getattr(request, property_name), getattr(request, property_name)
            self.assertEqual(first, second)
            self.assertIsNot(first, second)
            if isinstance(first, dict):
                first.clear()
            elif isinstance(first, list):
                first.clear()
            self.assertNotEqual(first, getattr(request, property_name))
        for name in ("repository", "artifact_store", "budget", "workspace_root"):
            self.assertFalse(hasattr(request, name))
        self.assertNotIn("Do not execute", repr(request))
        self.assertNotIn(str(request.run_id), repr(request))

    async def test_working_copy_changes_do_not_change_frozen_request(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        (self.case.fixture.root / "source/main.py").write_bytes(b"# mutable candidate ignored\n")
        verifier.verify_receipt(proof, self.execution, decision, measured)
        self.assertIn(b"Do not execute", _REQUESTS[-1].source_files["main.py"])

    async def test_model_pass_and_zero_warnings_are_not_independent_assessment_pass(self):
        verifier = self.verifier(assess_unverified)
        services, tracked, measured = await self.measured(verifier)
        await self.case.read(services, tracked, measured)
        decision = self.decision(measured, outcome="PASS", references=(self.case.reference,))
        report = await self.case.finish(services, tracked, measured, decision)
        self.assertEqual({value.outcome for value in report.requirement_results}, {ValidationOutcome.UNVERIFIED})
        self.assertTrue(all(value.tool_evidence is None for value in report.requirement_results))

    async def test_approved_inert_pass_has_persisted_proof_ref_but_does_not_write_verdict(self):
        verifier = self.verifier(assess_inert_pass)
        services, tracked, measured = await self.measured(verifier)
        await self.case.read(services, tracked, measured)
        decision = self.decision(measured, outcome="PASS", references=(self.case.reference,))
        report = await self.case.finish(services, tracked, measured, decision)
        self.assertEqual({value.outcome for value in report.requirement_results}, {ValidationOutcome.PASS})
        for value in report.requirement_results:
            details = json.loads(value.details)
            self.assertIn("/security-semantic-proof.json", details["semanticProofRef"])
            self.assertEqual(details["semanticProofApprovalSha256"], sha256(canonical(verifier.descriptor).encode()).hexdigest())
        self.assertIsNone(self.repository.get_run(self.execution.metadata.run_id).verdict)

    async def test_approved_failure_is_semantic_failure_not_scanner_execution_failure(self):
        verifier = self.verifier(assess_inert_fail)
        services, tracked, measured = await self.measured(verifier)
        await self.case.read(services, tracked, measured)
        report = await self.case.finish(services, tracked, measured,
            self.decision(measured, outcome="FAIL", references=(self.case.reference,)))
        self.assertEqual({value.outcome for value in report.requirement_results}, {ValidationOutcome.FAIL})
        self.assertTrue(all(value.tool_evidence.outcome.value == "PASS" for value in report.requirement_results))


    async def test_confirmed_finding_keeps_actual_scanner_identity_and_semantic_receipt(self):
        verifier = self.verifier(assess_inert_pass)
        services, tracked, measured = await self.measured(verifier, findings=True)
        await self.case.read(services, tracked, measured)
        report = await self.case.finish(services, tracked, measured, self.decision(
            measured, outcome="PASS", disposition="CONFIRMED", references=(self.case.reference,)))
        self.assertEqual(len(report.findings), 1)
        finding = report.findings[0]
        self.assertEqual(finding.finding_id, measured.finding_ids[0])
        self.assertEqual(finding.disposition.value, "CONFIRMED")
        self.assertEqual(finding.evidence_ref, measured.scans[0][1].report_ref)
        self.assertIn("/security-semantic-proof.json", json.loads(finding.description)["semanticProofRef"])

    async def test_false_positive_is_approved_independent_disposition_not_requirement_pass(self):
        verifier = self.verifier(assess_inert_false_positive)
        services, tracked, measured = await self.measured(verifier, findings=True)
        await self.case.read(services, tracked, measured)
        report = await self.case.finish(services, tracked, measured, self.decision(
            measured, disposition="FALSE_POSITIVE", references=(self.case.reference,)))
        self.assertEqual(report.findings[0].disposition.value, "FALSE_POSITIVE")
        self.assertEqual({value.outcome for value in report.requirement_results}, {ValidationOutcome.UNVERIFIED})

    async def test_unverified_assessor_cannot_confirm_model_scanner_proposal(self):
        verifier = self.verifier(assess_unverified)
        services, tracked, measured = await self.measured(verifier, findings=True)
        await self.case.read(services, tracked, measured)
        report = await self.case.finish(services, tracked, measured, self.decision(
            measured, disposition="CONFIRMED", references=(self.case.reference,)))
        self.assertEqual(report.findings[0].disposition.value, "SUSPECTED")
    async def test_verified_claim_requires_refs_and_independent_observations(self):
        for assessor in (assess_without_refs, assess_without_observations):
            verifier = self.verifier(assessor)
            services, tracked, measured = await self.measured(verifier)
            with self.subTest(assessor=assessor.__name__), self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, self.decision(measured), measured)

    async def test_foreign_scope_and_untyped_or_mutated_assessments_fail_closed(self):
        for assessor in (assess_foreign_requirement, assess_foreign_finding, assess_foreign_evidence,
                         assess_legacy_proof, assess_untyped_mapping, assess_privileged_mutation):
            verifier = self.verifier(assessor)
            services, tracked, measured = await self.measured(verifier)
            with self.subTest(assessor=assessor.__name__), self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, self.decision(measured), measured)

    async def test_response_request_and_approval_identity_must_match(self):
        for assessor in (assess_wrong_request, assess_wrong_request_hash, assess_wrong_approval):
            verifier = self.verifier(assessor)
            services, tracked, measured = await self.measured(verifier)
            with self.subTest(assessor=assessor.__name__), self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, self.decision(measured), measured)

    async def test_distinct_requests_have_distinct_nonce_and_receipt(self):
        verifier, services, tracked, measured, decision, first = await self.proof()
        second = await verifier(self.execution, decision, measured)
        self.assertNotEqual(first.verification_receipt_id, second.verification_receipt_id)
        self.assertNotEqual(first.request_sha256, second.request_sha256)
        self.assertNotEqual(self.receipt(first).request_id, self.receipt(second).request_id)
        verifier.verify_receipt(first, self.execution, decision, measured)
        verifier.verify_receipt(second, self.execution, decision, measured)


    async def test_previous_assessment_cannot_answer_a_fresh_request_challenge(self):
        verifier, services, tracked, measured, decision, proof = await self.proof(assess_replay_previous)
        self.receipt(proof)
        with self.assertRaises(SecurityVerifierError):
            await verifier(self.execution, decision, measured)
    async def test_receipt_replay_with_other_decision_is_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        changed = self.decision(measured, outcome="PASS", references=(self.case.reference,))
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, changed, measured)

    async def test_receipt_replay_with_another_scan_of_same_step_is_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        other_services, other_tracked, other = await self.measured(verifier)
        self.assertEqual(other.snapshot_sha256, measured.snapshot_sha256)
        self.assertNotEqual(other.scans[0][0].logical_call_id, measured.scans[0][0].logical_call_id)
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, self.decision(other), other)

    async def test_forged_source_bytes_and_inventory_are_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        forged = replace(measured, source_files=MappingProxyType({**dict(measured.source_files), "main.py": b"# forged\n"}))
        with self.assertRaises(SecurityVerifierError):
            await verifier(self.execution, decision, forged)
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, forged)

    async def test_forged_scan_receipt_and_journal_are_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        journal, receipt, evidence = measured.scans[0]
        for scans in (((replace(journal, logical_call_id=uuid4()), receipt, evidence),),
                      ((journal, replace(receipt, metadata_sha256="0" * 64), evidence),)):
            forged = replace(measured, scans=scans)
            with self.subTest(kind=type(scans[0][0]).__name__), self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, decision, forged)

    async def test_foreign_measured_run_step_or_source_are_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        for changes in ({"run_id": uuid4()}, {"workflow_step_id": uuid4()},
                        {"source_artifact_id": uuid4()}, {"snapshot_sha256": "0" * 64}):
            with self.subTest(changes=changes), self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, decision, replace(measured, **changes))

    async def test_current_step_ownership_or_attempt_drift_rejects_receipt(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        for changes in ({"status": WorkflowStepStatus.SUCCEEDED}, {"attempt": 1},
                        {"input_artifact_ids": []}, {"requirement_ids": [uuid4()]}):
            self.case.fixture.mutate_step(self.case.step, **changes)
            with self.subTest(changes=changes), self.assertRaises(SecurityVerifierError):
                verifier.verify_receipt(proof, self.execution, decision, measured)
            self.case.fixture.mutate_step(self.case.step)

    async def test_aborted_run_rejects_assessment_and_historical_proof_publication(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        self.case.fixture.mutate_run(status=WorkflowStatus.ABORTED, termination_reason="USER_CANCELLED")
        with self.assertRaises(SecurityVerifierError):
            await verifier(self.execution, decision, measured)
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_frozen_configuration_drift_rejects_receipt(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        self.case.fixture.replace_frozen_configuration(scanner_profile_ref="https://scanner.example.invalid/changed/v2")
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_scenario_contract_approval_mismatch_is_rejected(self):
        verifier = self.verifier(scenarioContractSha256="0" * 64)
        services, tracked, measured = await self.measured(verifier)
        with self.assertRaises(SecurityVerifierError):
            await verifier(self.execution, self.decision(measured), measured)

    async def test_proof_cannot_be_replayed_under_different_criteria_version(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        changed = self.verifier(verifierVersion="2.0.0")
        with self.assertRaises(SecurityVerifierError):
            changed.verify_receipt(proof, self.execution, decision, measured)

    async def test_foreign_requirement_decision_cannot_change_frozen_security_scope(self):
        verifier = self.verifier()
        services, tracked, measured = await self.measured(verifier)
        decision = SecurityDecision(kind="READY", questions=(), finding_reviews=(), requirement_reviews=(
            SecurityRequirementReview(requirement_id=uuid4(), proposed_outcome="UNVERIFIED",
                rationale="Foreign fixture requirement.", references=()),))
        with self.assertRaises(SecurityVerifierError):
            await verifier(self.execution, decision, measured)

    async def test_failed_persistence_never_returns_unpersisted_proof(self):
        verifier = self.verifier()
        services, tracked, measured = await self.measured(verifier)
        with patch.object(SecurityProofReceiptStore, "publish", side_effect=ValueError("private SQL error")):
            with self.assertRaises(SecurityVerifierError) as caught:
                await verifier(self.execution, self.decision(measured), measured)
        self.assertNotIn("private SQL", str(caught.exception))

    async def test_missing_receipt_and_privileged_proof_mutation_are_rejected(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        for changes in ({"verification_receipt_id": uuid4()}, {"request_sha256": "0" * 64},
                        {"approval_sha256": "0" * 64}, {"requirement_outcomes": ((self.case.ids[0], "PASS"),)}):
            with self.subTest(changes=changes), self.assertRaises(SecurityVerifierError):
                verifier.verify_receipt(replace(proof, **changes), self.execution, decision, measured)

    async def test_receipt_assessment_hash_must_match_saved_outcomes_and_evidence_hash(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        saved = self.receipt(proof)
        verifier.verify_receipt(proof, self.execution, decision, measured)
        for changes in ({"assessment_sha256": "0" * 64}, {"evidence_sha256": "f" * 64}):
            changed = replace(saved, **changes)
            with self.subTest(changes=changes), \
                    patch.object(SecurityProofReceiptStore, "get", return_value=changed), \
                    self.assertRaises(SecurityVerifierError):
                verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_self_consistent_receipt_hash_cannot_admit_foreign_evidence_reference(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        saved = self.receipt(proof)
        references = (f"artifact://{uuid4()}/security-scan-report.json",)
        # Recalculate the response digest independently to isolate membership
        # rejection: the altered record remains syntactically valid and its
        # assessment hash is internally consistent with the foreign pointer.
        altered_hash = sha256(canonical({
            "requestId": str(saved.request_id), "requestSha256": saved.request_sha256,
            "approvalSha256": saved.approval_sha256,
            "requirements": [[str(key), value] for key, value in saved.requirement_outcomes],
            "findings": [list(pair) for pair in saved.finding_dispositions],
            "evidenceRefs": list(references), "evidenceSha256": saved.evidence_sha256,
        }).encode("utf-8")).hexdigest()
        changed = replace(saved, evidence_refs=references, assessment_sha256=altered_hash)
        with patch.object(SecurityProofReceiptStore, "get", return_value=changed), \
                self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_corrupted_persisted_scan_receipt_invalidates_semantic_receipt(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        self.case.fixture.corrupt(measured.scans[0][1], execution_id=str(uuid4()))
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)


    async def test_corrupted_persisted_semantic_receipt_is_not_publishable_proof(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        with self.repository._transaction() as connection:
            triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='security_semantic_proof_receipts'").fetchall()
            for trigger in triggers:
                connection.execute('DROP TRIGGER "' + trigger[0].replace('"', '""') + '"')
            connection.execute("UPDATE security_semantic_proof_receipts SET metadata_sha256=? WHERE receipt_id=?",
                               ("0" * 64, str(proof.verification_receipt_id)))
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)

    async def test_changed_approval_file_invalidates_loaded_adapter_and_old_receipt(self):
        verifier, services, tracked, measured, decision, proof = await self.proof()
        definition = json.loads(self.approval_file.read_text(encoding="utf-8"))
        definition["criteria"]["purpose"] = "Different approval, never silently reused."
        self.approval_file.write_text(canonical(definition), encoding="utf-8")
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_integrity()
        with self.assertRaises(SecurityVerifierError):
            verifier.verify_receipt(proof, self.execution, decision, measured)
    async def test_expired_shared_budget_prevents_assessment_publication(self):
        verifier = self.verifier()
        services, tracked, measured = await self.measured(verifier)
        self.execution.budget = ExecutionBudget(runtime_budget_ms=1)
        await asyncio.sleep(.005)
        with self.assertRaises(LLMRuntimeError) as caught:
            await verifier(self.execution, self.decision(measured), measured)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    async def test_cancel_is_preserved_and_no_assessment_receipt_is_published(self):
        verifier = self.verifier(assess_until_cancel)
        services, tracked, measured = await self.measured(verifier)
        with patch.object(SecurityProofReceiptStore, "publish", wraps=SecurityProofReceiptStore.publish) as publish:
            task = asyncio.create_task(verifier(self.execution, self.decision(measured), measured))
            await asyncio.sleep(.02)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        publish.assert_not_called()

    async def test_assessor_uses_remaining_shared_deadline_not_fresh_timeout(self):
        verifier = self.verifier(assess_until_cancel)
        services, tracked, measured = await self.measured(verifier)
        self.execution.budget = ExecutionBudget(runtime_budget_ms=20)
        with self.assertRaises(LLMRuntimeError) as caught:
            await verifier(self.execution, self.decision(measured), measured)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    async def test_assessor_budget_error_has_stable_budget_classification(self):
        verifier = self.verifier(assess_budget_error)
        services, tracked, measured = await self.measured(verifier)
        with self.assertRaises(LLMRuntimeError) as caught:
            await verifier(self.execution, self.decision(measured), measured)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    async def test_assessor_native_timeout_is_not_misreported_as_run_budget_exhaustion(self):
        verifier = self.verifier(assess_native_timeout)
        services, tracked, measured = await self.measured(verifier)
        with self.assertRaises(SecurityVerifierError) as caught:
            await verifier(self.execution, self.decision(measured), measured)
        self.assertNotIn("inert-private-timeout", str(caught.exception))

    async def test_assessor_without_python_311_timeout_api_retains_budget_classification(self):
        verifier = self.verifier(assess_until_cancel)
        services, tracked, measured = await self.measured(verifier)
        self.execution.budget = ExecutionBudget(runtime_budget_ms=20)
        with patch.dict(asyncio.__dict__):
            asyncio.__dict__.pop("timeout", None)
            with self.assertRaises(LLMRuntimeError) as caught:
                await verifier(self.execution, self.decision(measured), measured)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)

    async def test_assessor_without_timeout_api_keeps_native_timeout_a_verifier_error(self):
        verifier = self.verifier(assess_native_timeout)
        services, tracked, measured = await self.measured(verifier)
        with patch.dict(asyncio.__dict__):
            asyncio.__dict__.pop("timeout", None)
            with self.assertRaises(SecurityVerifierError):
                await verifier(self.execution, self.decision(measured), measured)


if __name__ == "__main__":
    unittest.main()
