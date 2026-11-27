"""Ver1 rule (석민, 2026-11-17): passing protected tests settle generated-test results.

Live Runs 3754980f (#23) and 42f131fe (#24) showed generated QA/Security tests
failing correct code while the Host protected tests passed, so the bounded fix
loop chased test defects and ended FINISHED FAIL. These tests pin the rule in
``validation_output.protected_supersession`` and its dispatcher wiring:
reproduction (old behavior without selectors), the rule, and the boundaries
where the existing policy must stay.
"""
import json
import unittest
from types import SimpleNamespace
from uuid import uuid4

from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.validation_output import decide_verdict, protected_supersession
from orchestrator.domain import (
    ExecutionManifest, FinalVerdict, QAReportArtifact, QATestResult, SecurityReportArtifact,
    SecurityRequirementResult, ValidationOutcome, WorkflowStatus,
)
from test_dispatch import tool_evidence

PASS, FAIL, UNVERIFIED = ValidationOutcome.PASS, ValidationOutcome.FAIL, ValidationOutcome.UNVERIFIED
SELECTORS = frozenset({"run_unit_tests:protected-signup-qa", "run_unit_tests:protected-signup-security"})
GENERATED_QA, PROTECTED_QA = "run_unit_tests:qa-unit:", "run_unit_tests:protected-signup-qa:"
GENERATED_SEC, PROTECTED_SEC = "run_unit_tests:security-tests:", "run_unit_tests:protected-signup-security:"


class ProtectedSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.run_id = uuid4()
        self.r1, self.r2 = uuid4(), uuid4()
        self.manifest = ExecutionManifest(
            repository_id="scn-001-start", code_version=1, project_artifact_id=uuid4(), commit_hash="a" * 40,
            git_object_format="sha1", tree_hash="b" * 40, snapshot_sha256="c" * 64,
            container_image_digest="sha256:" + "d" * 64, dependency_lock_hash="sha256:" + "e" * 64)

    def evidence(self, tool="run_unit_tests"):
        return tool_evidence(tool, self.manifest.model_dump(mode="json", by_alias=True), outcome="PASS")

    def item(self, prefix, name, requirement, outcome):
        return QATestResult(test_id=prefix + name, requirement_id=requirement, outcome=outcome, title="t",
                            tool_evidence=None if outcome is UNVERIFIED else self.evidence())

    def qa(self, *tests):
        return QAReportArtifact(artifact_id=uuid4(), artifact_version=1, run_id=self.run_id,
            workflow_step_id=uuid4(), a2a_task_id="qa", a2a_artifact_id="qa-a",
            requirement_ids=tuple(dict.fromkeys(t.requirement_id for t in tests)), code_version=1,
            execution_manifest=self.manifest, tests=tests)

    def measured(self, requirement, rows, code="MEASURED_SECURITY_TESTS"):
        outcome = FAIL if any(o == "FAIL" for _t, o in rows) else PASS if all(o == "PASS" for _t, o in rows) else UNVERIFIED
        return SecurityRequirementResult(requirement_id=requirement, outcome=outcome,
            details=json.dumps({"code": code if outcome is not UNVERIFIED else "SECURITY_TESTS_UNVERIFIED",
                                "tests": [{"testId": t, "outcome": o, "evidenceRefs": []} for t, o in rows],
                                "codeReferences": []}),
            tool_evidence=None if outcome is UNVERIFIED else self.evidence())

    def semantic(self, requirement, outcome):
        return SecurityRequirementResult(requirement_id=requirement, outcome=outcome,
            details=json.dumps({"code": "HOST_SEMANTIC_PROOF", "codeReferences": []}),
            tool_evidence=self.evidence("run_security_scan"))

    def security(self, *results):
        return SecurityReportArtifact(artifact_id=uuid4(), artifact_version=1, run_id=self.run_id,
            workflow_step_id=uuid4(), a2a_task_id="sec", a2a_artifact_id="sec-a",
            requirement_ids=tuple(r.requirement_id for r in results), code_version=1,
            execution_manifest=self.manifest, requirement_results=results, findings=())

    def decide(self, qa, security, selectors=SELECTORS, fix_attempt=0):
        decision = decide_verdict(qa, security, fix_attempt=fix_attempt, requirements_authoritative=True,
                                  protected_selectors=selectors)
        return decision.target_status, decision.verdict

    # --- the live shape: generated tests fail correct code, protected tests pass

    def live_shape(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.test_signup", self.r1, FAIL),
                     self.item(PROTECTED_QA, "p.P.test_qa_signup_001", self.r1, PASS))
        security = self.security(self.measured(self.r2, [(GENERATED_SEC + "t.T.test_hash", "FAIL"),
                                                         (PROTECTED_SEC + "p.P.test_sec_002", "PASS")]))
        return qa, security

    def test_reproduction_without_selectors_sends_correct_code_to_fix(self):
        qa, security = self.live_shape()
        self.assertEqual(self.decide(qa, security, frozenset()), (WorkflowStatus.FIX_REQUIRED, None))
        self.assertEqual(self.decide(qa, security, frozenset(), 3), (WorkflowStatus.FINISHED, FinalVerdict.FAIL))

    def test_passing_protected_tests_settle_generated_failures(self):
        qa, security = self.live_shape()
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))
        self.assertEqual(self.decide(qa, security, fix_attempt=3), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))
        superseded = protected_supersession(qa, security, SELECTORS)
        self.assertEqual(superseded.qa_test_ids, {GENERATED_QA + "t.T.test_signup"})
        self.assertEqual(superseded.security_requirement_ids, {self.r2})
        decision = decide_verdict(qa, security, fix_attempt=0, requirements_authoritative=True,
                                  protected_selectors=SELECTORS)
        self.assertIn("unresolved conflicts", decision.reason)

    def test_generated_unverified_is_settled_too(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.test_a", self.r1, UNVERIFIED),
                     self.item(PROTECTED_QA, "p.P.test_a", self.r1, PASS))
        security = self.security(self.measured(self.r2, [(GENERATED_SEC + "t.T.b", "UNVERIFIED"),
                                                         (PROTECTED_SEC + "p.P.b", "PASS")]))
        self.assertEqual(self.decide(qa, security, frozenset())[0], WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS))

    def test_all_passing_reports_are_unchanged(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, PASS), self.item(PROTECTED_QA, "p.P.a", self.r1, PASS))
        security = self.security(self.measured(self.r2, [(GENERATED_SEC + "t.T.b", "PASS"), (PROTECTED_SEC + "p.P.b", "PASS")]))
        self.assertEqual(self.decide(qa, security), self.decide(qa, security, frozenset()))
        self.assertFalse(protected_supersession(qa, security, SELECTORS))

    # --- boundaries: the existing policy stays

    def test_protected_failure_still_requires_a_fix(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, FAIL), self.item(PROTECTED_QA, "p.P.a", self.r1, FAIL))
        security = self.security(self.measured(self.r2, [(GENERATED_SEC + "t.T.b", "FAIL"), (PROTECTED_SEC + "p.P.b", "FAIL")]))
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FIX_REQUIRED, None))
        self.assertFalse(protected_supersession(qa, security, SELECTORS))

    def test_one_protected_failure_on_the_requirement_blocks_settling(self):
        # REQ-003 shape: QA protected passes but the Security protected check fails.
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, FAIL), self.item(PROTECTED_QA, "p.P.a", self.r1, PASS))
        security = self.security(self.measured(self.r1, [(PROTECTED_SEC + "p.P.b", "FAIL")]))
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FIX_REQUIRED, None))
        self.assertFalse(protected_supersession(qa, security, SELECTORS))

    def test_protected_unverified_does_not_settle(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, FAIL), self.item(PROTECTED_QA, "p.P.a", self.r1, UNVERIFIED))
        security = self.security(self.measured(self.r2, [(PROTECTED_SEC + "p.P.b", "PASS")]))
        self.assertFalse(protected_supersession(qa, security, SELECTORS))
        self.assertEqual(self.decide(qa, security), self.decide(qa, security, frozenset()))

    def test_requirement_without_protected_tests_keeps_generated_failure(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, FAIL))
        security = self.security(self.measured(self.r2, [(PROTECTED_SEC + "p.P.b", "PASS")]))
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FIX_REQUIRED, None))

    def test_semantic_proof_failure_is_never_settled(self):
        qa = self.qa(self.item(PROTECTED_QA, "p.P.a", self.r1, PASS))
        security = self.security(self.semantic(self.r1, FAIL))
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FIX_REQUIRED, None))

    def test_qa_protected_pass_cannot_fill_missing_security_observations(self):
        qa = self.qa(self.item(PROTECTED_QA, "p.P.signup", self.r1, PASS))
        receipt = self.evidence()
        details = {"code": "SECURITY_TESTS_UNVERIFIED", "generatedEvidenceGaps": ["DB_CONSTRAINT_NOT_OBSERVED"],
                   "tests": [{"testId": GENERATED_SEC + "t.T.signup", "outcome": "PASS",
                              "evidenceRefs": [receipt["evidenceRef"]]}]}
        missing = SecurityRequirementResult(requirement_id=self.r1, outcome=UNVERIFIED,
            actual_result="OUTPUT_COVERAGE_INCOMPLETE", details=json.dumps(details), execution_evidence=(receipt,))
        self.assertEqual(self.decide(qa, self.security(missing)), (WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED))
        details["tests"][0]["evidenceRefs"] = []
        unlinked = missing.model_copy(update={"details": json.dumps(details)})
        self.assertEqual(self.decide(qa, self.security(unlinked)), (WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW))

    def test_unparsable_security_details_are_never_settled(self):
        qa = self.qa(self.item(PROTECTED_QA, "p.P.a", self.r1, PASS))
        broken = SecurityRequirementResult(requirement_id=self.r1, outcome=FAIL, details="not json",
                                           tool_evidence=self.evidence())
        self.assertEqual(self.decide(qa, self.security(broken)), (WorkflowStatus.FIX_REQUIRED, None))

    def test_security_receipt_cannot_replace_missing_qa_concurrency(self):
        receipt = self.evidence()
        details = {'code': 'CONCURRENT_SIGNUPS_NOT_OBSERVED', 'runnerOutcome': 'PASS',
                   'sourceAssertionScope': 'SEQUENTIAL_SIGNUPS_ONLY', 'evidenceRefs': [receipt['evidenceRef']]}
        missing = self.item(GENERATED_QA, 't.T.sequence', self.r1, UNVERIFIED)
        missing = QATestResult.model_validate({**missing.model_dump(), 'actual_result': details['code'],
                                               'details': json.dumps(details), 'execution_evidence': (receipt,)})
        security = self.security(self.measured(self.r1, [(PROTECTED_SEC + 'p.P.schema', 'PASS')]))
        self.assertEqual(self.decide(self.qa(missing), security), (WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED))
        details['evidenceRefs'] = []
        unlinked = missing.model_copy(update={'details': json.dumps(details)})
        self.assertEqual(self.decide(self.qa(unlinked), security), (WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW))

    def test_browser_case_without_concurrency_evidence_can_finish_unverified(self):
        from orchestrator.application.validation_output import generated_coverage_gap
        receipt = self.evidence('run_browser_tests')
        missing = self.item('run_browser_tests:qa-browser:', 'signup', self.r1, UNVERIFIED)
        details = {'code': 'CONCURRENT_SIGNUPS_NOT_OBSERVED', 'runnerOutcome': 'PASS',
                   'sourceAssertionScope': 'CONCURRENT_SIGNUPS_NOT_OBSERVED', 'evidenceRefs': [receipt['evidenceRef']]}
        missing = QATestResult.model_validate({**missing.model_dump(), 'actual_result': details['code'],
                                               'details': json.dumps(details), 'execution_evidence': (receipt,)})
        self.assertTrue(generated_coverage_gap(missing))
        self.assertFalse(generated_coverage_gap(missing.model_copy(update={'test_id': GENERATED_QA + 'signup'})))

    def test_qa_concurrency_gap_requires_all_assertions_from_one_linked_protected_case(self):
        receipt = self.evidence()
        gap = self.item(GENERATED_QA, 't.T.sequence', self.r1, UNVERIFIED)
        gap = gap.model_copy(update={'actual_result': 'CONCURRENT_SIGNUPS_NOT_OBSERVED'})
        proof = self.item(PROTECTED_QA, 'p.P.parallel', self.r1, PASS)
        rows = [{'caseId': 'attempt_1', 'itemId': name, 'kind': 'assertion', 'actual': value,
                 'status': 'PASS', 'reason': None} for name, value in
                [('account_count', 1), ('success_count', 1), ('response_statuses', [201, 409])]]
        details = {'observationStatus': 'COMPLETE', 'observations': rows,
                   'evidenceRefs': [proof.tool_evidence.evidence_ref]}
        def settled(data):
            protected = proof.model_copy(update={'details': json.dumps(data)})
            return protected_supersession(self.qa(gap, protected), None, SELECTORS).qa_test_ids
        self.assertEqual(settled(details), {gap.test_id})
        self.assertFalse(settled({**details, 'observations': rows[:-1]}))
        self.assertFalse(settled({**details, 'observations': [*rows[:-1], {**rows[-1], 'caseId': 'unrelated'}]}))
        self.assertFalse(settled({**details, 'observationStatus': 'UNVERIFIED'}))
        self.assertFalse(settled({**details, 'evidenceRefs': [receipt['evidenceRef'] + '/other']}))

    def test_fix_request_carries_only_unsettled_failures(self):
        qa = self.qa(self.item(GENERATED_QA, "t.T.a", self.r1, FAIL), self.item(PROTECTED_QA, "p.P.a", self.r1, PASS),
                     self.item(PROTECTED_QA, "p.P.b", self.r2, FAIL))
        security = self.security(self.measured(self.r2, [(GENERATED_SEC + "t.T.c", "FAIL"), (PROTECTED_SEC + "p.P.c", "FAIL")]))
        self.assertEqual(self.decide(qa, security), (WorkflowStatus.FIX_REQUIRED, None))
        superseded = protected_supersession(qa, security, SELECTORS)
        run = SimpleNamespace(run_id=self.run_id)
        source = SimpleNamespace(code_version=1, artifact_id=uuid4())
        issues = PlannerRunDispatcher._validation_issues(run, source, qa, security, superseded)
        self.assertEqual(sorted(issue.reference_id for issue in issues), sorted([PROTECTED_QA + "p.P.b", str(self.r2)]))
        everything = PlannerRunDispatcher._validation_issues(run, source, qa, security)
        self.assertIn(GENERATED_QA + "t.T.a", {issue.reference_id for issue in everything})

    def test_dispatcher_rejects_malformed_selectors(self):
        for bad in ({"run_unit_tests:x"}, frozenset({"no-colon"}), frozenset({"a:b:c"}), frozenset({":x"}), frozenset({1})):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                PlannerRunDispatcher(None, None, protected_selectors=bad)
        PlannerRunDispatcher(None, None, protected_selectors=SELECTORS)


class HostProtectedSelectorsTests(unittest.TestCase):
    def test_host_names_every_protected_scope_and_nothing_for_ver2(self):
        from agents.platform.host import _protected_selectors
        from orchestrator.domain.states import AgentRole

        def tools(qa, security, browser=None):
            units = {AgentRole.QA: SimpleNamespace(scopes=qa), AgentRole.SECURITY: security}
            return SimpleNamespace(unit_configuration_for=lambda role: units[role], browser_configuration=browser)
        scope = lambda name, kind: SimpleNamespace(name=name, kind=kind)
        ver1 = tools((scope("qa-unit", "QA_TESTS"), scope("protected-signup-qa", "PROTECTED")),
                     SimpleNamespace(scopes=(scope("security-tests", "QA_TESTS"), scope("protected-signup-security", "PROTECTED"))),
                     SimpleNamespace(suites=(scope("protected-browser", "PROTECTED"),)))
        self.assertEqual(_protected_selectors(ver1), frozenset({"run_unit_tests:protected-signup-qa",
            "run_unit_tests:protected-signup-security", "run_browser_tests:protected-browser"}))
        ver2 = tools((scope("qa-unit", "QA_TESTS"),), None)
        self.assertEqual(_protected_selectors(ver2), frozenset())


if __name__ == "__main__":
    unittest.main()
