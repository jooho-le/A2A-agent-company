"""Independent fixture references and adversarial checks beyond value/count."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from diagnostics.communication.contracts import CheckOptions, ProbeRecord, compare, fingerprint, grounding
from diagnostics.communication.faults import alter_agent_claims
from diagnostics.communication.lab import running_lab
from diagnostics.communication.strict_suite import Case, judge, plan, protocol_checks
from diagnostics.communication.workloads import load_request, expected_specification, mock_specification


def fixture_payload(profile="documents"):
    doc = load_request(profile)
    return {"profile": profile, "workRequest": doc,
            "sample": {"value": 100, "label": "사용자 요구 전달", "requirementIds": [r["id"] for r in doc["requirements"]]},
            "upstream": None}


def fixture_response(payload, role="DEVELOPER"):
    return {"probeId": "fixture-test", "claims": {"value": 100, "label": payload["sample"]["label"],
            "requirementCount": len(payload["sample"]["requirementIds"]), "upstreamHash": None,
            "specification": mock_specification(payload["workRequest"], role)}}


class FrozenFixtureTests(unittest.TestCase):
    def test_fixture_structure_and_case_requirement_links_are_consistent(self):
        for profile in ("signup", "documents", "mystery"):
            doc = load_request(profile)
            self.assertGreaterEqual(len(doc["brief"]), 500)
            self.assertGreaterEqual(len(doc["requirements"]), 12)
            self.assertGreaterEqual(len(doc["acceptanceCases"]), 12)
            ids = {r["id"] for r in doc["requirements"]}
            self.assertEqual(len(ids), len(doc["requirements"]))
            for case in doc["acceptanceCases"]:
                self.assertIn(case["requirementId"], ids)
                self.assertEqual(case["execution"], "NOT_EXECUTED")
            for requirement in doc["requirements"]:
                self.assertTrue(set(requirement["dependsOn"]) <= ids)

    def test_mock_cannot_load_reference_answer(self):
        doc = load_request("documents")
        with patch("diagnostics.communication.workloads._asset", side_effect=AssertionError("gold access forbidden")):
            result = mock_specification(doc, "SINGLE_AGENT")
        self.assertEqual(result["constraints"]["deployAllowed"], False)
        self.assertEqual(result["executionEvidence"]["build"], "NOT_EXECUTED")

    def test_unicode_opaque_ids_and_false_zero_null_remain_distinct(self):
        doc = load_request("documents")
        self.assertEqual(doc["project"]["organizationId"], "9007199254740993")
        self.assertIn("가", doc["project"]["customerNote"])
        data = doc["boundaryData"]
        self.assertEqual(data["leadingZeros"], "000007")
        self.assertEqual(compare(data["falseBoolean"], data["falseString"])["status"], "FAIL")
        self.assertEqual(compare(data["zeroNumber"], data["zeroString"])["status"], "FAIL")
        self.assertIsNone(data["nullValue"])
        self.assertIsNone(load_request("mystery")["domainPolicy"]["timeLimitSeconds"])

    def test_five_semantic_faults_preserve_old_basic_fields_but_fail_frozen_contract(self):
        payload = fixture_payload()
        for scenario in ("response_requirement_drop", "response_limit_change", "permission_swap", "invented_evidence", "invented_decision"):
            response = fixture_response(payload)
            basic = {k: deepcopy(v) for k, v in response["claims"].items() if k != "specification"}
            row = ProbeRecord("DEVELOPER", 1, 0, scenario)
            allowed_before = sum(r["allowed"] for r in response["claims"]["specification"]["accessMatrix"])
            alter_agent_claims(response, row)
            self.assertEqual({k: v for k, v in response["claims"].items() if k != "specification"}, basic)
            if scenario == "permission_swap":
                self.assertEqual(sum(r["allowed"] for r in response["claims"]["specification"]["accessMatrix"]), allowed_before)
            self.assertEqual(grounding(response, payload, "DEVELOPER")["status"], "FAIL", scenario)

    def test_modified_request_is_not_allowed_to_redefine_the_reference(self):
        payload = fixture_payload()
        payload["workRequest"]["constraints"]["retentionDays"] = 90
        response = fixture_response(payload)
        verdict = grounding(response, payload, "DEVELOPER")
        self.assertEqual(verdict["status"], "FAIL")
        self.assertTrue(any("retentionDays" in d["path"] for d in verdict["differences"]))

    def test_mutating_returned_fixture_does_not_poison_cache_or_next_run(self):
        baseline = fingerprint(load_request("signup"))
        mutated = load_request("signup")
        mutated["requirements"].clear()
        self.assertEqual(fingerprint(load_request("signup")), baseline)
        expected = expected_specification("signup", "QA")
        expected["coverageIds"].clear()
        self.assertGreater(len(expected_specification("signup", "QA")["coverageIds"]), 0)

    def test_basic_profile_cannot_silently_skip_a_complex_fault(self):
        with self.assertRaises(ValidationError):
            CheckOptions(profile="basic", scenario="permission_swap")
        with self.assertRaises(ValidationError):
            CheckOptions(padding_kib=129)

    def test_full_plan_has_repetitions_all_roles_and_separate_expected_rejections(self):
        cases = plan()
        self.assertEqual(len({c.name for c in cases}), len(cases))
        for profile in ("signup", "documents", "mystery"):
            for architecture in ("MULTI_AGENT", "SINGLE_AGENT"):
                baseline = [c for c in cases if c.category == "positive" and c.options.profile == profile and c.options.architecture == architecture]
                self.assertEqual(sum(c.options.rounds for c in baseline), 20)
            self.assertEqual({c.options.fault_target for c in cases if c.category == "negative_role" and c.options.profile == profile}, {"PLANNER", "DEVELOPER", "QA", "SECURITY"})
        self.assertEqual(len([c for c in cases if c.category == "boundary"]), 6)


class StrictActualHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_delivery_and_run_role_isolation(self):
        async with running_lab() as lab:
            checks = await protocol_checks(lab)
            self.assertEqual(len(checks), 4)
            self.assertTrue(all(check["passed"] for check in checks), checks)

    async def test_rich_profiles_keep_refs_across_roles_and_apply_body_limit(self):
        async with running_lab() as lab:
            for profile in ("signup", "documents", "mystery"):
                report = await lab.execute(CheckOptions(profile=profile, rounds=1, processing_ms=0), lambda event: None)
                self.assertEqual(report["summary"]["verifiedRequests"], 4)
                references = [r["observations"]["executorReceived"]["requirementRefs"] for r in report["rows"]]
                self.assertTrue(all(r == references[0] for r in references))
                self.assertTrue(all(r["wireBytes"] > 8000 for r in report["rows"]))
                self.assertTrue(all(r["checks"]["identity"] == "PASS" for r in report["rows"]))
            options = CheckOptions(profile="documents", architecture="SINGLE_AGENT", padding_kib=96, rounds=1)
            report = await lab.execute(options, lambda event: None)
            self.assertTrue(judge(Case("oversize", "boundary", options), report)["passed"])
            self.assertEqual(report["rows"][0]["httpStatus"], 413)
            self.assertIsNone(report["rows"][0]["receiverIdentity"]["taskId"])

    async def test_receiver_identity_catches_consistently_spoofed_ack_and_poll(self):
        async with running_lab() as lab:
            options = CheckOptions(profile="documents", architecture="SINGLE_AGENT", scenario="context_spoof", rounds=2)
            report = await lab.execute(options, lambda event: None)
            self.assertTrue(judge(Case("spoof", "negative", options), report)["passed"])
            for row in report["rows"]:
                self.assertEqual(row["checks"]["identity"], "FAIL")
                self.assertNotEqual(row["contextId"], row["receiverIdentity"]["contextId"])
            self.assertEqual(len({r["receiverIdentity"]["contextId"] for r in report["rows"]}), 1)
