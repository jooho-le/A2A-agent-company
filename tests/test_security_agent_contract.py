"""Security model proposals are inert and cannot substitute Host proof."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid1, uuid4

from agents.roles.security_contract import (
    MAX_SECURITY_DECISION_JSON_BYTES, MAX_SECURITY_FINDING_ID_BYTES,
    MAX_SECURITY_FINDING_REVIEWS, MAX_SECURITY_LINE, MAX_SECURITY_QUESTIONS,
    MAX_SECURITY_QUESTION_LENGTH, MAX_SECURITY_RATIONALE_LENGTH,
    MAX_SECURITY_REFERENCE_LINES, MAX_SECURITY_REFERENCES,
    MAX_SECURITY_REQUIREMENT_REVIEWS, MAX_SECURITY_SOURCE_PATHS,
    SecurityCodeReference, SecurityContractError, SecurityDecision,
    SecurityFindingReview, SecurityRequirementReview,
    build_security_output_contract, validate_security_decision,
)


REQUIREMENT_IDS = (uuid4(), uuid4(), uuid4())
FINDING_IDS = ("bandit-profile:0", "bandit-profile:1")
SOURCE_PATHS = ("app/auth.py", "app/db.py", "package-lock.json")


def reference(**changes):
    values = {"path": SOURCE_PATHS[0], "startLine": 1, "endLine": 5}
    values.update(changes)
    return values


def requirement(index=0, **changes):
    values = {"requirementId": str(REQUIREMENT_IDS[index]), "proposedOutcome": "UNVERIFIED",
              "rationale": "검사 근거만으로 요구사항 충족을 확정할 수 없습니다.", "references": []}
    values.update(changes)
    return values


def finding(index=0, **changes):
    values = {"findingId": FINDING_IDS[index], "proposedDisposition": "SUSPECTED",
              "rationale": "정적 검사 경고이며 독립적인 확인이 필요합니다.", "references": []}
    values.update(changes)
    return values


def decision(**changes):
    values = {"kind": "READY", "requirementReviews": [requirement(index) for index in range(3)],
              "findingReviews": [finding(index) for index in range(2)], "questions": []}
    values.update(changes)
    return values


def code_reference(data=None):
    data = reference() if data is None else data
    return SecurityCodeReference(path=data["path"], start_line=data["startLine"], end_line=data["endLine"])


class SecurityAgentContractTests(unittest.TestCase):
    def validate(self, data=None, *, requirement_ids=REQUIREMENT_IDS,
                 finding_ids=FINDING_IDS, source_paths=SOURCE_PATHS):
        return validate_security_decision(decision() if data is None else data,
                                           requirement_ids, finding_ids, source_paths)

    def assert_invalid(self, operation):
        with self.assertRaises(SecurityContractError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, "SECURITY_OUTPUT_INVALID")
        self.assertEqual(error.args, ("SECURITY_OUTPUT_INVALID",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(repr(error), "SecurityContractError('SECURITY_OUTPUT_INVALID')")

    def reject(self, data):
        self.assert_invalid(lambda: validate_security_decision(data, REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS))

    def test_closed_all_required_provider_strict_schema(self):
        contract = build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)
        self.assertEqual(contract.name, "security_decision")
        contract.schema.require_openai_strict()
        root = contract.schema.to_dict()
        self.assertEqual(set(root["properties"]), {"kind", "requirementReviews", "findingReviews", "questions"})
        self.assertEqual(set(root["required"]), set(root["properties"]))
        self.assertFalse(root["additionalProperties"])
        requirements = root["properties"]["requirementReviews"]["items"]
        findings = root["properties"]["findingReviews"]["items"]
        self.assertEqual(set(requirements["properties"]), {"requirementId", "proposedOutcome", "rationale", "references"})
        self.assertEqual(set(findings["properties"]), {"findingId", "proposedDisposition", "rationale", "references"})
        for review in (requirements, findings):
            self.assertEqual(set(review["properties"]), set(review["required"]))
            self.assertFalse(review["additionalProperties"])
            anchor = review["properties"]["references"]["items"]
            self.assertEqual(set(anchor["properties"]), {"path", "startLine", "endLine"})
            self.assertEqual(set(anchor["properties"]), set(anchor["required"]))
            self.assertFalse(anchor["additionalProperties"])
            self.assertEqual(anchor["properties"]["path"]["enum"], list(SOURCE_PATHS))
        self.assertEqual(requirements["properties"]["requirementId"]["enum"], list(map(str, REQUIREMENT_IDS)))
        self.assertEqual(findings["properties"]["findingId"]["enum"], list(FINDING_IDS))
        for forbidden in ('"$id"', '"$ref"', '"uniqueItems"', '"if"', '"then"', '"allOf"'):
            self.assertNotIn(forbidden, contract.schema.schema_json)

    def test_default_ready_is_unverified_and_suspected_proposals_only(self):
        result = self.validate()
        self.assertEqual(result.kind, "READY")
        self.assertEqual(tuple(row.requirement_id for row in result.requirement_reviews), REQUIREMENT_IDS)
        self.assertEqual(tuple(row.finding_id for row in result.finding_reviews), FINDING_IDS)
        self.assertEqual(result.requirement_reviews[0].proposed_outcome, "UNVERIFIED")
        self.assertEqual(result.finding_reviews[0].proposed_disposition, "SUSPECTED")
        self.assertFalse(hasattr(result.requirement_reviews[0], "outcome"))
        self.assertFalse(hasattr(result.finding_reviews[0], "disposition"))
        self.assertFalse(hasattr(result, "artifact"))
        self.assertFalse(hasattr(result, "proof"))
        self.assertFalse(hasattr(result, "tool_evidence"))

    def test_strong_proposals_with_anchors_are_not_host_verified_results(self):
        data = decision()
        data["requirementReviews"][0].update(proposedOutcome="PASS", references=[reference()])
        data["requirementReviews"][1].update(proposedOutcome="FAIL", references=[reference(path=SOURCE_PATHS[1])])
        data["findingReviews"][0].update(proposedDisposition="CONFIRMED", references=[reference()])
        data["findingReviews"][1].update(proposedDisposition="FALSE_POSITIVE", references=[reference()])
        result = self.validate(data)
        self.assertEqual(result.requirement_reviews[0].proposed_outcome, "PASS")
        self.assertEqual(result.requirement_reviews[1].proposed_outcome, "FAIL")
        self.assertEqual(result.finding_reviews[0].proposed_disposition, "CONFIRMED")
        self.assertEqual(result.finding_reviews[1].proposed_disposition, "FALSE_POSITIVE")
        for row in (*result.requirement_reviews, *result.finding_reviews):
            self.assertFalse(hasattr(row, "verified"))
            self.assertFalse(hasattr(row, "evidence_ref"))

    def test_strong_proposals_require_nonempty_source_anchors(self):
        for proposed in ("PASS", "FAIL"):
            data = decision()
            data["requirementReviews"][0]["proposedOutcome"] = proposed
            self.reject(data)
        for proposed in ("CONFIRMED", "FALSE_POSITIVE"):
            data = decision()
            data["findingReviews"][0]["proposedDisposition"] = proposed
            self.reject(data)

    def test_unverified_and_suspected_can_have_or_lack_anchors(self):
        data = decision()
        data["requirementReviews"][0]["references"] = [reference()]
        data["findingReviews"][0]["references"] = [reference()]
        data["findingReviews"][1]["proposedDisposition"] = "UNVERIFIED"
        result = self.validate(data)
        self.assertEqual(len(result.requirement_reviews[0].references), 1)
        self.assertEqual(result.requirement_reviews[1].references, ())
        self.assertEqual(len(result.finding_reviews[0].references), 1)
        self.assertEqual(result.finding_reviews[1].references, ())

    def test_ready_covers_exactly_all_requirement_and_actual_finding_ids_once(self):
        self.reject(decision(requirementReviews=decision()["requirementReviews"][:2]))
        self.reject(decision(findingReviews=decision()["findingReviews"][:1]))
        self.reject(decision(requirementReviews=[]))
        for key in ("requirementReviews", "findingReviews"):
            data = decision()
            data[key].append(deepcopy(data[key][0]))
            self.reject(data)
        data = decision()
        data["requirementReviews"][1] = deepcopy(data["requirementReviews"][0])
        self.reject(data)
        data = decision()
        data["findingReviews"][1] = deepcopy(data["findingReviews"][0])
        self.reject(data)

    def test_dynamic_host_ids_have_no_scenario_or_finding_constants(self):
        ids, findings = (uuid4(),), ("new-profile:53",)
        data = decision(requirementReviews=[requirement(0, requirementId=str(ids[0]))],
                        findingReviews=[finding(0, findingId=findings[0])])
        result = self.validate(data, requirement_ids=ids, finding_ids=findings)
        self.assertEqual(result.requirement_reviews[0].requirement_id, ids[0])
        self.assertEqual(result.finding_reviews[0].finding_id, findings[0])
        self.reject(data)

    def test_zero_scanner_findings_has_no_invalid_empty_enum_and_is_not_a_pass(self):
        contract = build_security_output_contract(REQUIREMENT_IDS, (), SOURCE_PATHS)
        contract.schema.require_openai_strict()
        schema = contract.schema.to_dict()["properties"]["findingReviews"]
        self.assertEqual(schema["maxItems"], 0)
        self.assertNotIn("enum", schema["items"]["properties"]["findingId"])
        result = self.validate(decision(findingReviews=[]), finding_ids=())
        self.assertEqual(result.finding_reviews, ())
        self.assertTrue(all(review.proposed_outcome == "UNVERIFIED" for review in result.requirement_reviews))
        self.assert_invalid(lambda: self.validate(finding_ids=()))

    def test_every_root_review_and_reference_field_is_required(self):
        for key in decision():
            self.reject({name: value for name, value in decision().items() if name != key})
        for field, row in (("requirementReviews", requirement()), ("findingReviews", finding())):
            for key in row:
                data = decision()
                del data[field][0][key]
                self.reject(data)
        for key in reference():
            data = decision()
            data["requirementReviews"][0]["references"] = [{name: value for name, value in reference().items() if name != key}]
            self.reject(data)

    def test_model_cannot_submit_results_evidence_severity_or_runtime_identity(self):
        fields = ("outcome", "disposition", "severity", "confidence", "ruleId", "title", "description",
                  "toolEvidence", "evidenceRef", "proof", "verified", "executionManifest", "executionManifestId",
                  "report", "findings", "requirementResults", "exitCode", "stdoutRef", "stderrRef", "runId",
                  "workflowStepId", "workspaceId", "artifactId", "artifactVersion", "previousArtifactId", "createdAt",
                  "taskId", "contextId", "model", "configuration", "limits", "finalVerdict")
        for key in fields:
            self.reject(decision(**{key: "private-value"}))
            for array in ("requirementReviews", "findingReviews"):
                data = decision()
                data[array][0][key] = "private-value"
                self.reject(data)

    def test_model_cannot_submit_raw_snippets_hashes_uris_or_commands(self):
        for key in ("source", "sourceCode", "codeSource", "content", "snippet", "rawSnippet", "hash", "sha256",
                    "commitHash", "snapshotSha256", "uri", "url", "command", "argv", "patch", "environment"):
            self.reject(decision(**{key: "private-value"}))
            for array in ("requirementReviews", "findingReviews"):
                data = decision()
                data[array][0][key] = "private-value"
                self.reject(data)
                data = decision()
                data[array][0]["references"] = [reference(**{key: "private-value"})]
                self.reject(data)

    def test_enum_values_are_case_sensitive_native_strings(self):
        for value in (None, True, 1, "ready", "READY ", "COMPLETED", "PASS", "FAILED"):
            self.reject(decision(kind=value))
        for value in (None, True, 1, "pass", "PASS ", "SUSPECTED", "AUTH_REQUIRED"):
            data = decision()
            data["requirementReviews"][0]["proposedOutcome"] = value
            self.reject(data)
        for value in (None, True, 1, "confirmed", "FALSE_POSITIVE ", "PASS", "AUTH_REQUIRED"):
            data = decision()
            data["findingReviews"][0]["proposedDisposition"] = value
            self.reject(data)

    def test_requirement_enum_strings_are_exact_canonical_uuidv4(self):
        for value in (None, True, 1, REQUIREMENT_IDS[0], str(REQUIREMENT_IDS[0]).upper(),
                      "{" + str(REQUIREMENT_IDS[0]) + "}", str(uuid1()), str(uuid4()), "REQ-005"):
            data = decision()
            data["requirementReviews"][0]["requirementId"] = value
            self.reject(data)

    def test_finding_ids_must_exactly_match_host_issued_inventory(self):
        for value in (None, True, 1, "B105", "bandit-profile:2", "bandit-profile:0 ", "private"):
            data = decision()
            data["findingReviews"][0]["findingId"] = value
            self.reject(data)

    def test_source_references_require_exact_approved_paths(self):
        for value in (None, True, 1, "auth.py", "source/app/auth.py", "app/AUTH.py", "app/auth.py ", "unapproved.py"):
            data = decision()
            data["requirementReviews"][0]["references"] = [reference(path=value)]
            self.reject(data)

    def test_host_and_direct_reference_paths_reject_traversal_secrets_uris_and_compatibility_disguises(self):
        invalid = ("", " ", "/app/auth.py", "../auth.py", "app/../auth.py", "app//auth.py", "app/./auth.py",
                   "app/auth.py/", "C:/auth.py", "app\\auth.py", "https://example.test/auth.py", "file:///tmp/auth.py",
                   ".git/config", "app/.env", "app/.env.test", "app/credentials.json", "app/private.key",
                   "app/.mcp-write-hidden", "app/．ｅｎｖ", "app/．．/auth.py", "app／auth.py", "app/\x80auth.py",
                   "app/\x00auth.py", "app/\ud800auth.py", "a/" * 128 + "auth.py", "a" * 4097)
        for value in invalid:
            self.assert_invalid(lambda: SecurityCodeReference(path=value, start_line=1, end_line=1))
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, (value,)))

    def test_unicode_host_source_paths_are_not_renamed_or_resolved(self):
        path = "앱/가입.py"
        data = decision()
        data["requirementReviews"][0]["references"] = [reference(path=path)]
        result = self.validate(data, source_paths=(path,))
        self.assertEqual(result.requirement_reviews[0].references[0].path, path)

    def test_lines_are_native_integers_not_proto_like_float_or_boolean(self):
        for field in ("startLine", "endLine"):
            for value in (None, True, False, "1", 1.0, float("nan"), float("inf"), [], {}):
                data = decision()
                data["requirementReviews"][0]["references"] = [reference(**{field: value})]
                self.reject(data)

    def test_reference_lines_positive_ordered_and_inclusive_range_bounded(self):
        for start, end in ((0, 1), (-1, 1), (2, 1), (1, MAX_SECURITY_REFERENCE_LINES + 1),
                           (MAX_SECURITY_LINE, MAX_SECURITY_LINE + 1)):
            data = decision()
            data["requirementReviews"][0]["references"] = [reference(startLine=start, endLine=end)]
            self.reject(data)
        for start, end in ((1, 1), (1, MAX_SECURITY_REFERENCE_LINES), (MAX_SECURITY_LINE, MAX_SECURITY_LINE)):
            data = decision()
            data["requirementReviews"][0]["references"] = [reference(startLine=start, endLine=end)]
            accepted = self.validate(data).requirement_reviews[0].references[0]
            self.assertEqual((accepted.start_line, accepted.end_line), (start, end))

    def test_anchors_do_not_claim_actual_line_existence_or_source_reads(self):
        data = decision()
        data["requirementReviews"][0].update(proposedOutcome="PASS",
            references=[reference(startLine=MAX_SECURITY_LINE, endLine=MAX_SECURITY_LINE)])
        result = self.validate(data)
        self.assertEqual(result.requirement_reviews[0].proposed_outcome, "PASS")
        self.assertFalse(hasattr(result.requirement_reviews[0].references[0], "read_execution_id"))

    def test_reference_limit_and_duplicate_exact_anchors_are_enforced(self):
        for array in ("requirementReviews", "findingReviews"):
            data = decision()
            data[array][0]["references"] = [reference(startLine=index + 1, endLine=index + 1)
                                             for index in range(MAX_SECURITY_REFERENCES)]
            result = self.validate(data)
            rows = result.requirement_reviews if array == "requirementReviews" else result.finding_reviews
            self.assertEqual(len(rows[0].references), MAX_SECURITY_REFERENCES)
            data[array][0]["references"].append(reference(startLine=100, endLine=100))
            self.reject(data)
            data[array][0]["references"] = [reference(), reference()]
            self.reject(data)

    def test_reference_list_and_rows_must_be_native_json(self):
        for array in ("requirementReviews", "findingReviews"):
            for value in (None, (), {}, "private", True, [None], [True], [[]], [{}]):
                data = decision()
                data[array][0]["references"] = value
                self.reject(data)

    def test_all_reviews_require_nonblank_bounded_rationale(self):
        for array in ("requirementReviews", "findingReviews"):
            for value in (None, True, 1, "", "  ", [], {}, "x" * (MAX_SECURITY_RATIONALE_LENGTH + 1)):
                data = decision()
                data[array][0]["rationale"] = value
                self.reject(data)
            data = decision()
            data[array][0]["rationale"] = "😀" * MAX_SECURITY_RATIONALE_LENGTH
            result = self.validate(data)
            rows = result.requirement_reviews if array == "requirementReviews" else result.finding_reviews
            self.assertEqual(len(rows[0].rationale.encode("utf-8")), 16384)

    def test_original_noncontrol_whitespace_is_preserved(self):
        data = decision()
        data["requirementReviews"][0]["rationale"] = "  독립적인 검증이 필요합니다.  "
        self.assertEqual(self.validate(data).requirement_reviews[0].rationale,
                         data["requirementReviews"][0]["rationale"])

    def test_ascii_del_and_c1_controls_are_rejected_in_every_prose_field(self):
        for character in ("\x00", "\x01", "\t", "\n", "\r", "\x1f", "\x7f", "\x80", "\x9f"):
            for array in ("requirementReviews", "findingReviews"):
                data = decision()
                data[array][0]["rationale"] = "private" + character
                self.reject(data)
            self.reject(decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[],
                                 questions=["private" + character]))
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS,
                ("private" + character,), SOURCE_PATHS))

    def test_recognizable_credentials_are_rejected_not_silently_rewritten(self):
        texts = ("password='private-test-password'", "api_key=private-test-key", "Bearer private-test-token",
                 "Authorization: Bearer private-test-token", "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
                 "$2b$12$" + "A" * 53, "eyJabc.example.signature")
        for text in texts:
            for array in ("requirementReviews", "findingReviews"):
                data = decision()
                data[array][0]["rationale"] = text
                self.reject(data)
            self.reject(decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[], questions=[text]))
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, (text,), SOURCE_PATHS))
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, ("app/" + text,)))

    def test_redacted_text_is_allowed_without_an_arbitrary_secret_guarantee(self):
        data = decision()
        data["requirementReviews"][0]["rationale"] = "[REDACTED] 정책의 독립적 확인이 필요합니다."
        self.assertEqual(self.validate(data).requirement_reviews[0].rationale,
                         data["requirementReviews"][0]["rationale"])

    def test_input_required_has_only_one_through_eight_questions(self):
        data = decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[], questions=["어떤 근거를 승인할까요?"])
        result = self.validate(data)
        self.assertEqual(result.requirement_reviews, ())
        self.assertEqual(result.finding_reviews, ())
        self.assertEqual(result.questions, tuple(data["questions"]))
        data["questions"] = [f"질문 {index}" for index in range(MAX_SECURITY_QUESTIONS)]
        self.assertEqual(len(self.validate(data).questions), MAX_SECURITY_QUESTIONS)
        for questions in ([], [""], ["  "], [None], [True], [1], ["질문"] * 9):
            self.reject(decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[], questions=questions))
        self.reject(decision(kind="INPUT_REQUIRED", questions=["질문"]))
        self.reject(decision(kind="INPUT_REQUIRED", requirementReviews=[], questions=["질문"]))
        self.reject(decision(kind="READY", questions=["질문"]))

    def test_question_character_and_byte_limits(self):
        data = decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[],
                        questions=["😀" * MAX_SECURITY_QUESTION_LENGTH])
        self.assertEqual(len(self.validate(data).questions[0].encode("utf-8")), 4096)
        data["questions"][0] += "😀"
        self.reject(data)

    def test_rejected_has_no_reviews_questions_or_model_reason(self):
        result = self.validate(decision(kind="REJECTED", requirementReviews=[], findingReviews=[]))
        self.assertEqual((result.kind, result.requirement_reviews, result.finding_reviews, result.questions),
                         ("REJECTED", (), (), ()))
        self.reject(decision(kind="REJECTED"))
        self.reject(decision(kind="REJECTED", requirementReviews=[]))
        self.reject(decision(kind="REJECTED", requirementReviews=[], findingReviews=[], questions=["질문"]))
        self.reject(decision(kind="REJECTED", requirementReviews=[], findingReviews=[], reason="private"))

    def test_all_review_and_question_collections_must_be_native_arrays(self):
        for field in ("requirementReviews", "findingReviews", "questions"):
            for value in (None, (), {}, "private", True, [None], [1], [True], [[]]):
                self.reject(decision(**{field: value}))

    def test_host_requirement_scope_is_native_unique_uuidv4_and_bounded(self):
        for ids in (None, [], (), (uuid1(),), (str(REQUIREMENT_IDS[0]),), (True,),
                    REQUIREMENT_IDS + (REQUIREMENT_IDS[0],), tuple(uuid4() for _ in range(MAX_SECURITY_REQUIREMENT_REVIEWS + 1))):
            self.assert_invalid(lambda: build_security_output_contract(ids, FINDING_IDS, SOURCE_PATHS))
            self.assert_invalid(lambda: self.validate(requirement_ids=ids))

    def test_host_finding_inventory_is_native_unique_bounded_safe_text(self):
        for ids in (None, [], {}, "private", (True,), (1,), ("",), ("   ",), ("\ud800private",),
                    FINDING_IDS + (FINDING_IDS[0],), ("x" * (MAX_SECURITY_FINDING_ID_BYTES + 1),),
                    ("😀" * (MAX_SECURITY_FINDING_ID_BYTES // 4 + 1),),
                    tuple(f"finding-{index}" for index in range(MAX_SECURITY_FINDING_REVIEWS + 1))):
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, ids, SOURCE_PATHS))
            self.assert_invalid(lambda: self.validate(finding_ids=ids))

    def test_host_source_inventory_is_native_unique_bounded_without_alias_collisions(self):
        for paths in (None, [], (), {}, "private", (True,), (1,), SOURCE_PATHS + (SOURCE_PATHS[0],),
                      ("app/auth.py", "APP/AUTH.py"), ("app/auth.py", "ａｐｐ/auth.py"),
                      tuple(f"file_{index}.py" for index in range(MAX_SECURITY_SOURCE_PATHS + 1))):
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, paths))
            self.assert_invalid(lambda: self.validate(source_paths=paths))

    def test_maximum_review_inventory_can_be_admitted(self):
        ids = tuple(uuid4() for _ in range(MAX_SECURITY_REQUIREMENT_REVIEWS))
        findings = tuple(f"scanner:{index}" for index in range(MAX_SECURITY_FINDING_REVIEWS))
        data = decision(requirementReviews=[requirement(0, requirementId=str(value)) for value in ids],
                        findingReviews=[finding(0, findingId=value) for value in findings])
        result = self.validate(data, requirement_ids=ids, finding_ids=findings)
        self.assertEqual(len(result.requirement_reviews), MAX_SECURITY_REQUIREMENT_REVIEWS)
        self.assertEqual(len(result.finding_reviews), MAX_SECURITY_FINDING_REVIEWS)
        data["findingReviews"].append(finding(0, findingId="extra"))
        self.assert_invalid(lambda: self.validate(data, requirement_ids=ids, finding_ids=findings))

    def test_total_json_byte_limit_is_enforced_after_individual_field_validation(self):
        self.assertEqual(MAX_SECURITY_DECISION_JSON_BYTES, 1_048_576)
        with patch("agents.roles.security_contract.MAX_SECURITY_DECISION_JSON_BYTES", 32):
            self.reject(decision())
        findings = tuple(f"scanner:{index}" for index in range(65))
        data = decision(findingReviews=[finding(0, findingId=value, rationale="😀" * MAX_SECURITY_RATIONALE_LENGTH)
                                       for value in findings])
        self.assert_invalid(lambda: self.validate(data, finding_ids=findings))

    def test_invalid_native_json_subclasses_and_surrogates_fail_with_safe_errors(self):
        class CustomDict(dict):
            pass

        class CustomList(list):
            pass

        class CustomStr(str):
            pass

        for value in (None, [], (), True, "private", {1: "private"}, object(), CustomDict(decision())):
            self.reject(value)
        self.reject(decision(kind=CustomStr("READY")))
        self.reject(decision(requirementReviews=CustomList(decision()["requirementReviews"])))
        for array in ("requirementReviews", "findingReviews"):
            data = decision()
            data[array][0] = CustomDict(data[array][0])
            self.reject(data)
            for key in data[array][0]:
                data = decision()
                if key == "references":
                    data[array][0][key] = CustomList()
                else:
                    data[array][0][key] = CustomStr(data[array][0][key])
                self.reject(data)
            data = decision()
            data[array][0]["rationale"] = "\ud800private"
            self.reject(data)
            data[array][0]["references"] = [CustomDict(reference())]
            self.reject(data)
        self.reject(decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[], questions=["\ud800private"]))

    def test_mutating_submitted_nested_objects_cannot_change_admitted_review(self):
        data = decision()
        data["requirementReviews"][0]["references"] = [reference()]
        result = self.validate(data)
        data["requirementReviews"][0]["references"][0].update(path="private.py", startLine=999)
        data["requirementReviews"][0]["rationale"] = "changed"
        data["findingReviews"].clear()
        data["kind"] = "REJECTED"
        self.assertEqual(result.kind, "READY")
        self.assertEqual(result.requirement_reviews[0].references[0].path, SOURCE_PATHS[0])
        self.assertEqual(result.requirement_reviews[0].references[0].start_line, 1)
        self.assertEqual(len(result.finding_reviews), 2)
        self.assertEqual(result.requirement_reviews[0].rationale, requirement()["rationale"])

    def test_decisions_reviews_and_code_references_are_deeply_frozen_and_hide_private_text(self):
        data = decision()
        data["requirementReviews"][0]["references"] = [reference()]
        result = self.validate(data)
        self.assertEqual(repr(result), "SecurityDecision(kind='READY')")
        self.assertEqual(repr(result.requirement_reviews[0]), "SecurityRequirementReview(proposed_outcome='UNVERIFIED')")
        self.assertEqual(repr(result.finding_reviews[0]), "SecurityFindingReview(proposed_disposition='SUSPECTED')")
        self.assertEqual(repr(result.requirement_reviews[0].references[0]), "SecurityCodeReference()")
        for obj, field, value in ((result, "kind", "REJECTED"),
                                  (result.requirement_reviews[0], "rationale", "changed"),
                                  (result.finding_reviews[0], "finding_id", "changed"),
                                  (result.requirement_reviews[0].references[0], "path", "changed.py")):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)

    def test_direct_reference_constructor_checks_range_without_any_source_read(self):
        self.assertEqual(code_reference().path, SOURCE_PATHS[0])
        for changes in ({"start_line": True}, {"end_line": 5.0}, {"start_line": 0}, {"end_line": 0},
                        {"start_line": 6}, {"end_line": 201}, {"path": "../private"}, {"path": "\ud800private"}):
            values = {"path": SOURCE_PATHS[0], "start_line": 1, "end_line": 5, **changes}
            self.assert_invalid(lambda: SecurityCodeReference(**values))

    def test_direct_requirement_review_requires_native_fields_and_strong_proposal_anchors(self):
        values = {"requirement_id": REQUIREMENT_IDS[0], "proposed_outcome": "UNVERIFIED",
                  "rationale": "review", "references": ()}
        self.assertEqual(SecurityRequirementReview(**values).proposed_outcome, "UNVERIFIED")
        for changes in ({"requirement_id": str(REQUIREMENT_IDS[0])}, {"requirement_id": uuid1()},
                        {"proposed_outcome": True}, {"proposed_outcome": "pass"}, {"proposed_outcome": "PASS"},
                        {"proposed_outcome": "FAIL"}, {"rationale": ""}, {"rationale": "password=private"},
                        {"references": []}, {"references": ({},)}, {"references": (code_reference(),) * 2}):
            self.assert_invalid(lambda: SecurityRequirementReview(**{**values, **changes}))
        self.assertEqual(SecurityRequirementReview(**{**values, "proposed_outcome": "PASS",
            "references": (code_reference(),)}).proposed_outcome, "PASS")

    def test_direct_finding_review_requires_native_fields_and_strong_proposal_anchors(self):
        values = {"finding_id": FINDING_IDS[0], "proposed_disposition": "SUSPECTED",
                  "rationale": "review", "references": ()}
        self.assertEqual(SecurityFindingReview(**values).proposed_disposition, "SUSPECTED")
        for changes in ({"finding_id": True}, {"finding_id": ""}, {"finding_id": "Bearer private"},
                        {"proposed_disposition": True}, {"proposed_disposition": "confirmed"},
                        {"proposed_disposition": "CONFIRMED"}, {"proposed_disposition": "FALSE_POSITIVE"},
                        {"rationale": ""}, {"rationale": "\ud800private"}, {"references": []},
                        {"references": ({},)}, {"references": (code_reference(),) * 2}):
            self.assert_invalid(lambda: SecurityFindingReview(**{**values, **changes}))
        self.assertEqual(SecurityFindingReview(**{**values, "proposed_disposition": "CONFIRMED",
            "references": (code_reference(),)}).proposed_disposition, "CONFIRMED")

    def test_direct_decision_constructor_checks_branch_native_types_and_unique_ids(self):
        req = SecurityRequirementReview(requirement_id=REQUIREMENT_IDS[0], proposed_outcome="UNVERIFIED",
                                        rationale="review", references=())
        item = SecurityFindingReview(finding_id=FINDING_IDS[0], proposed_disposition="SUSPECTED",
                                     rationale="review", references=())
        values = {"kind": "READY", "requirement_reviews": (req,), "finding_reviews": (item,), "questions": ()}
        self.assertEqual(SecurityDecision(**values).kind, "READY")
        for changes in ({"kind": True}, {"kind": "PASS"}, {"requirement_reviews": []},
                        {"requirement_reviews": ()}, {"requirement_reviews": ({},)},
                        {"requirement_reviews": (req, req)}, {"finding_reviews": (item, item)},
                        {"finding_reviews": []}, {"questions": []}, {"questions": ("question",)},
                        {"kind": "INPUT_REQUIRED", "questions": ("question",)}, {"kind": "REJECTED"}):
            self.assert_invalid(lambda: SecurityDecision(**{**values, **changes}))
        self.assertEqual(SecurityDecision(kind="INPUT_REQUIRED", requirement_reviews=(), finding_reviews=(),
            questions=("question",)).kind, "INPUT_REQUIRED")
        self.assertEqual(SecurityDecision(kind="REJECTED", requirement_reviews=(), finding_reviews=(),
            questions=()).kind, "REJECTED")

    def test_all_public_decision_objects_are_keyword_only(self):
        for constructor, args in (
            (SecurityCodeReference, (SOURCE_PATHS[0], 1, 1)),
            (SecurityRequirementReview, (REQUIREMENT_IDS[0], "UNVERIFIED", "review", ())),
            (SecurityFindingReview, (FINDING_IDS[0], "SUSPECTED", "review", ())),
            (SecurityDecision, ("REJECTED", (), (), ())),
        ):
            with self.assertRaises(TypeError):
                constructor(*args)

    def test_schema_copies_are_independent_and_admission_fails_closed(self):
        original = build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)
        changed = original.schema.to_dict()
        changed["properties"]["kind"]["enum"].append("PASS")
        self.assertNotIn("PASS", build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)
                         .schema.to_dict()["properties"]["kind"]["enum"])
        with patch("agents.roles.security_contract.JsonSchema.validate", side_effect=ValueError("private validation")):
            self.reject(decision())
        with patch("agents.roles.security_contract.JsonSchema.require_openai_strict", side_effect=ValueError("private schema")):
            self.assert_invalid(lambda: build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS))

    def test_no_files_network_scanner_tools_or_processes_are_used(self):
        with patch("builtins.open", side_effect=AssertionError("unexpected file I/O")), \
                patch.object(Path, "read_text", side_effect=AssertionError("unexpected asset read")), \
                patch("subprocess.run", side_effect=AssertionError("unexpected execution")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            build_security_output_contract(REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)
            self.assertEqual(self.validate().kind, "READY")
            self.assertEqual(self.validate(decision(findingReviews=[]), finding_ids=()).kind, "READY")
            self.assertEqual(self.validate(decision(kind="INPUT_REQUIRED", requirementReviews=[], findingReviews=[],
                questions=["질문"])).kind, "INPUT_REQUIRED")
            self.assertEqual(self.validate(decision(kind="REJECTED", requirementReviews=[], findingReviews=[])).kind, "REJECTED")
            self.assertEqual(code_reference().path, SOURCE_PATHS[0])


if __name__ == "__main__":
    unittest.main()
