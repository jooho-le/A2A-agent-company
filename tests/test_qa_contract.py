"""QA Draft admission is inert; execution and receipt assembly stay with Host."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID, uuid1, uuid4

from agents.roles.qa_contract import (
    MAX_QA_CASES, MAX_QA_EXPECTED_RESULT_LENGTH, MAX_QA_JSON_BYTES,
    MAX_QA_QUESTIONS, MAX_QA_QUESTION_LENGTH, MAX_QA_SELECTORS_PER_TOOL,
    MAX_QA_TEST_ID_BYTES, MAX_QA_TITLE_LENGTH,
    QACaseBinding, QAContractError, QADecision,
    build_qa_output_contract, validate_qa_decision,
)


REQUIREMENT_IDS = tuple(uuid4() for _ in range(5))
SELECTORS = {"run_unit_tests": ("protected", "qa-tests"), "run_browser_tests": ("signup-browser",)}


def case(index=0, **changes):
    values = {"toolName": "run_unit_tests", "selector": "protected",
              "testId": f"test_signup.SignupTests.test_requirement_{index}",
              "requirementId": str(REQUIREMENT_IDS[index]),
              "title": f"회원가입 기준 {index}", "expectedResult": "요구사항의 승인 기준을 만족한다."}
    values.update(changes)
    return values


def decision(**changes):
    values = {"kind": "READY", "cases": [case(index) for index in range(5)], "questions": []}
    values.update(changes)
    return values


def binding(data=None):
    data = case() if data is None else data
    return QACaseBinding(tool_name=data["toolName"], selector=data["selector"], test_id=data["testId"],
                         requirement_id=UUID(data["requirementId"]), title=data["title"],
                         expected_result=data["expectedResult"])


class QAContractTests(unittest.TestCase):
    def validate(self, data=None, requirement_ids=REQUIREMENT_IDS, selectors=None):
        return validate_qa_decision(decision() if data is None else data,
                                    requirement_ids, SELECTORS if selectors is None else selectors)

    def assert_invalid(self, operation):
        with self.assertRaises(QAContractError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, "QA_OUTPUT_INVALID")
        self.assertEqual(error.args, ("QA_OUTPUT_INVALID",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(repr(error), "QAContractError('QA_OUTPUT_INVALID')")

    def reject(self, data):
        self.assert_invalid(lambda: validate_qa_decision(data, REQUIREMENT_IDS, SELECTORS))

    def test_closed_all_required_strict_provider_contract(self):
        contract = build_qa_output_contract(REQUIREMENT_IDS, SELECTORS)
        self.assertEqual(contract.name, "qa_decision")
        contract.schema.require_openai_strict()
        schema = contract.schema.to_dict()
        self.assertEqual(set(schema["properties"]), {"kind", "cases", "questions"})
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        item = schema["properties"]["cases"]["items"]
        self.assertEqual(set(item["properties"]), {"toolName", "selector", "testId", "requirementId", "title", "expectedResult"})
        self.assertEqual(set(item["required"]), set(item["properties"]))
        self.assertFalse(item["additionalProperties"])
        self.assertEqual(item["properties"]["requirementId"]["enum"], list(map(str, REQUIREMENT_IDS)))
        self.assertEqual(item["properties"]["toolName"]["enum"], ["run_unit_tests", "run_browser_tests"])
        self.assertEqual(item["properties"]["selector"]["enum"], ["protected", "qa-tests", "signup-browser"])
        for forbidden in ('"$id"', '"$ref"', '"uniqueItems"', '"if"', '"then"', '"allOf"'):
            self.assertNotIn(forbidden, contract.schema.schema_json)

    def test_ready_binds_all_five_independent_requirement_ids(self):
        result = self.validate()
        self.assertEqual(result.kind, "READY")
        self.assertEqual(tuple(case.requirement_id for case in result.cases), REQUIREMENT_IDS)
        self.assertEqual(result.questions, ())
        self.assertEqual(result.cases[0].test_id, case()["testId"])
        self.assertFalse(hasattr(result.cases[0], "outcome"))
        self.assertFalse(hasattr(result.cases[0], "tool_evidence"))
        self.assertFalse(hasattr(result, "report"))

    def test_requirement_ids_are_dynamic_not_registry_constants(self):
        ids = (uuid4(), uuid4())
        data = decision(cases=[case(0, requirementId=str(ids[0])), case(1, requirementId=str(ids[1]))])
        result = self.validate(data, requirement_ids=ids)
        self.assertEqual(tuple(row.requirement_id for row in result.cases), ids)
        self.assert_invalid(lambda: self.validate(data))

    def test_single_tool_contract_contains_only_offered_names(self):
        selectors = {"run_browser_tests": ("protected-browser",)}
        schema = build_qa_output_contract(REQUIREMENT_IDS, selectors).schema.to_dict()
        properties = schema["properties"]["cases"]["items"]["properties"]
        self.assertEqual(properties["toolName"]["enum"], ["run_browser_tests"])
        self.assertEqual(properties["selector"]["enum"], ["protected-browser"])
        data = decision(cases=[case(index, toolName="run_browser_tests", selector="protected-browser") for index in range(5)])
        self.assertEqual(self.validate(data, selectors=selectors).kind, "READY")
        self.assert_invalid(lambda: self.validate(selectors=selectors))

    def test_every_root_and_case_field_is_required(self):
        for field in decision():
            self.reject({key: value for key, value in decision().items() if key != field})
        for field in case():
            data = decision()
            del data["cases"][0][field]
            self.reject(data)

    def test_model_cannot_supply_pass_fail_evidence_or_runtime_results(self):
        for field in ("outcome", "actualResult", "details", "toolEvidence", "report", "tests", "coverage",
                      "passed", "failed", "total", "exitCode", "durationMs", "executionManifest",
                      "executionManifestId", "executionId", "stdoutRef", "stderrRef", "evidenceRef", "finalVerdict"):
            self.reject(decision(**{field: "PASS"}))
            data = decision()
            data["cases"][0][field] = "PASS"
            self.reject(data)

    def test_model_cannot_supply_source_credentials_commands_or_identity_fields(self):
        for field in ("source", "sourceCode", "codeSource", "content", "patch", "fileChanges", "command", "argv",
                      "environment", "snapshotId", "snapshotSha256", "commitHash", "runId", "workflowStepId",
                      "workspaceId", "taskId", "contextId", "projectArtifactId", "artifactVersion", "createdAt",
                      "model", "limits", "configuration", "requirementIds", "requirements", "acceptanceCriteria"):
            self.reject(decision(**{field: "private"}))
            data = decision()
            data["cases"][0][field] = "private"
            self.reject(data)

    def test_kinds_are_case_sensitive_and_never_coerced(self):
        for value in (None, True, 1, "ready", "READY ", "COMPLETED", "PASS", "AUTH_REQUIRED", "FAILED"):
            self.reject(decision(kind=value))

    def test_ready_requires_nonempty_cases_and_no_questions(self):
        self.reject(decision(cases=[]))
        self.reject(decision(questions=["확인 질문"]))

    def test_ready_requires_exact_requirement_coverage(self):
        self.reject(decision(cases=[case(index) for index in range(4)]))
        data = decision()
        data["cases"][4]["requirementId"] = str(uuid4())
        self.reject(data)
        data["cases"][4]["requirementId"] = str(REQUIREMENT_IDS[0])
        self.reject(data)

    def test_multiple_distinct_cases_may_cover_the_same_requirement(self):
        data = decision()
        data["cases"].append(case(0, testId="additional.SignupTests.test_edge_case"))
        result = self.validate(data)
        self.assertEqual(len(result.cases), 6)
        self.assertEqual(result.cases[0].requirement_id, result.cases[-1].requirement_id)

    def test_duplicate_actual_case_cannot_cover_two_requirements(self):
        data = decision()
        data["cases"].append(case(0, requirementId=str(REQUIREMENT_IDS[1])))
        self.reject(data)
        data = decision()
        data["cases"].append(deepcopy(data["cases"][0]))
        self.reject(data)

    def test_same_test_id_in_distinct_approved_scopes_is_not_merged(self):
        data = decision()
        data["cases"].append(case(0, selector="qa-tests"))
        data["cases"].append(case(0, toolName="run_browser_tests", selector="signup-browser"))
        result = self.validate(data)
        self.assertEqual(len(result.cases), 7)

    def test_tool_selector_pair_checked_locally_not_only_union_schema(self):
        for tool, selector in (("run_unit_tests", "signup-browser"), ("run_browser_tests", "protected"),
                               ("run_security_scan", "protected"), ("run_unit_tests", "unapproved"),
                               (True, "protected"), ("run_unit_tests", None)):
            data = decision()
            data["cases"][0].update(toolName=tool, selector=selector)
            self.reject(data)

    def test_shared_selector_name_is_valid_for_each_independently_offered_tool(self):
        selectors = {"run_unit_tests": ("shared",), "run_browser_tests": ("shared",)}
        data = decision(cases=[case(index, selector="shared",
            toolName="run_browser_tests" if index % 2 else "run_unit_tests") for index in range(5)])
        result = self.validate(data, selectors=selectors)
        self.assertEqual(len(result.cases), 5)

    def test_requirement_ids_are_exact_canonical_enum_strings(self):
        for value in (None, True, 1, REQUIREMENT_IDS[0], str(REQUIREMENT_IDS[0]).upper(),
                      "{" + str(REQUIREMENT_IDS[0]) + "}", str(uuid1()), "REQ-001", "private"):
            data = decision()
            data["cases"][0]["requirementId"] = value
            self.reject(data)

    def test_input_required_accepts_one_through_eight_questions(self):
        data = decision(kind="INPUT_REQUIRED", cases=[], questions=["어떤 승인된 테스트를 사용할까요?"])
        result = self.validate(data)
        self.assertEqual(result.cases, ())
        self.assertEqual(result.questions, tuple(data["questions"]))
        data["questions"] = [f"확인 질문 {index}" for index in range(MAX_QA_QUESTIONS)]
        self.assertEqual(len(self.validate(data).questions), MAX_QA_QUESTIONS)
        for values in ([], [""], ["   "], [None], [True], [1], ["질문"] * 9):
            self.reject(decision(kind="INPUT_REQUIRED", cases=[], questions=values))

    def test_input_required_cannot_publish_case_bindings(self):
        self.reject(decision(kind="INPUT_REQUIRED", questions=["확인 질문"]))

    def test_rejected_accepts_neither_cases_questions_nor_model_reason(self):
        result = self.validate(decision(kind="REJECTED", cases=[]))
        self.assertEqual((result.kind, result.cases, result.questions), ("REJECTED", (), ()))
        self.reject(decision(kind="REJECTED"))
        self.reject(decision(kind="REJECTED", cases=[], questions=["질문"]))
        self.reject(decision(kind="REJECTED", cases=[], reason="private"))

    def test_cases_and_questions_must_be_native_arrays(self):
        for value in (None, (), {}, "private", True, [None], [1], [True], [[]]):
            self.reject(decision(cases=value))
        for value in (None, (), {}, "private", True, [None], [1], [True], [[]], [{}]):
            self.reject(decision(kind="INPUT_REQUIRED", cases=[], questions=value))

    def test_maximum_case_count_is_256(self):
        ids = (REQUIREMENT_IDS[0],)
        data = decision(cases=[case(0, testId=f"case_{index}") for index in range(MAX_QA_CASES)])
        self.assertEqual(len(self.validate(data, requirement_ids=ids).cases), MAX_QA_CASES)
        data["cases"].append(case(0, testId="case_256"))
        self.assert_invalid(lambda: self.validate(data, requirement_ids=ids))

    def test_text_fields_are_nonblank_native_strings(self):
        for field in ("testId", "title", "expectedResult"):
            for value in (None, True, 1, "", "   ", [], {}, float("nan"), float("inf")):
                data = decision()
                data["cases"][0][field] = value
                self.reject(data)

    def test_character_and_byte_limits_are_enforced(self):
        for field, maximum in (("testId", MAX_QA_TEST_ID_BYTES), ("title", MAX_QA_TITLE_LENGTH),
                               ("expectedResult", MAX_QA_EXPECTED_RESULT_LENGTH)):
            data = decision()
            data["cases"][0][field] = "x" * maximum
            self.assertEqual(len(getattr(self.validate(data).cases[0],
                {"testId": "test_id", "title": "title", "expectedResult": "expected_result"}[field])), maximum)
            data["cases"][0][field] += "x"
            self.reject(data)
        data = decision()
        data["cases"][0]["testId"] = "😀" * (MAX_QA_TEST_ID_BYTES // 4)
        self.assertEqual(self.validate(data).cases[0].test_id, data["cases"][0]["testId"])
        data["cases"][0]["testId"] += "😀"
        self.reject(data)

    def test_multibyte_title_expected_result_and_question_limits(self):
        data = decision()
        data["cases"][0].update(title="😀" * MAX_QA_TITLE_LENGTH,
                                expectedResult="😀" * MAX_QA_EXPECTED_RESULT_LENGTH)
        result = self.validate(data)
        self.assertEqual(len(result.cases[0].title.encode("utf-8")), 4096)
        self.assertEqual(len(result.cases[0].expected_result.encode("utf-8")), 16384)
        data = decision(kind="INPUT_REQUIRED", cases=[], questions=["😀" * MAX_QA_QUESTION_LENGTH])
        self.assertEqual(len(self.validate(data).questions[0].encode("utf-8")), 4096)
        data["questions"][0] += "😀"
        self.reject(data)

    def test_total_json_size_is_bounded_after_individual_fields(self):
        self.assertEqual(MAX_QA_JSON_BYTES, 1_048_576)
        with patch("agents.roles.qa_contract.MAX_QA_JSON_BYTES", 32):
            self.reject(decision())
        data = decision(cases=[case(0, testId=f"case_{index}",
            expectedResult="😀" * MAX_QA_EXPECTED_RESULT_LENGTH) for index in range(65)])
        self.assert_invalid(lambda: self.validate(data, requirement_ids=(REQUIREMENT_IDS[0],)))

    def test_all_ascii_del_and_c1_controls_are_rejected(self):
        for character in ("\x00", "\x01", "\t", "\n", "\x08", "\x0b", "\x0c", "\r", "\x1f", "\x7f", "\x80", "\x9f"):
            for field in ("testId", "title", "expectedResult"):
                data = decision()
                data["cases"][0][field] = "private" + character
                self.reject(data)
            self.reject(decision(kind="INPUT_REQUIRED", cases=[], questions=["private" + character]))

    def test_original_noncontrol_whitespace_and_expectations_are_preserved(self):
        data = decision()
        data["cases"][0].update(title="  정상 회원가입  ", expectedResult="  PASS 기준을 만족한다.  ")
        result = self.validate(data)
        self.assertEqual(result.cases[0].title, data["cases"][0]["title"])
        self.assertEqual(result.cases[0].expected_result, data["cases"][0]["expectedResult"])
        self.assertFalse(hasattr(result.cases[0], "outcome"))

    def test_recognizable_credentials_are_rejected_not_rewritten(self):
        values = ("password='private-test-password'", "api_key=private-test-key",
                  "Authorization: Bearer private-test-token", "Bearer private-test-token",
                  "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA", "$2b$12$" + "A" * 53,
                  "eyJabc.example.signature")
        for text in values:
            for field in ("testId", "title", "expectedResult"):
                data = decision()
                data["cases"][0][field] = text
                self.reject(data)
            self.reject(decision(kind="INPUT_REQUIRED", cases=[], questions=[text]))

    def test_redacted_text_is_allowed_without_arbitrary_secret_guarantee(self):
        data = decision()
        data["cases"][0]["expectedResult"] = "[REDACTED] 설정을 노출하지 않는다."
        self.assertEqual(self.validate(data).cases[0].expected_result, data["cases"][0]["expectedResult"])

    def test_invalid_json_types_surrogates_and_native_subclasses_fail_safely(self):
        class CustomDict(dict):
            pass

        class CustomList(list):
            pass

        class CustomStr(str):
            pass

        for value in (None, [], (), True, "private", {1: "private"}, object(), CustomDict(decision())):
            self.reject(value)
        self.reject(decision(kind=CustomStr("READY")))
        self.reject(decision(cases=CustomList(decision()["cases"])))
        self.reject(decision(kind="REJECTED", cases=[], questions=CustomList()))
        for field in case():
            data = decision()
            data["cases"][0][field] = CustomStr(case()[field])
            self.reject(data)
        data = decision()
        data["cases"][0] = CustomDict(data["cases"][0])
        self.reject(data)
        for field in ("testId", "title", "expectedResult"):
            data = decision()
            data["cases"][0][field] = "\ud800private"
            self.reject(data)
        self.reject(decision(kind="INPUT_REQUIRED", cases=[], questions=["\ud800private"]))

    def test_host_requirement_declaration_has_strict_bounded_uuidv4_values(self):
        for ids in (None, [], (), REQUIREMENT_IDS + (REQUIREMENT_IDS[0],), (uuid1(),),
                    (str(REQUIREMENT_IDS[0]),), (True,), tuple(uuid4() for _ in range(MAX_QA_CASES + 1))):
            self.assert_invalid(lambda: build_qa_output_contract(ids, SELECTORS))
            self.assert_invalid(lambda: self.validate(requirement_ids=ids))

    def test_host_selectors_are_only_bounded_native_approved_tool_scope_names(self):
        for selectors in (None, [], {}, {"run_security_scan": ("protected",)}, {True: ("protected",)},
                          {"run_unit_tests": []}, {"run_unit_tests": ()}, {"run_unit_tests": "protected"},
                          {"run_unit_tests": ("protected", "protected")}, {"run_unit_tests": (True,)},
                          {"run_unit_tests": ("Protected",)}, {"run_unit_tests": ("../scope",)},
                          {"run_unit_tests": ("private\x00scope",)}, {"run_unit_tests": ("a" * 65,)},
                          {"run_unit_tests": tuple(f"scope-{index}" for index in range(MAX_QA_SELECTORS_PER_TOOL + 1))}):
            self.assert_invalid(lambda: build_qa_output_contract(REQUIREMENT_IDS, selectors))
            self.assert_invalid(lambda: validate_qa_decision(decision(), REQUIREMENT_IDS, selectors))

    def test_mutating_input_lists_and_host_mapping_does_not_change_decision_or_schema(self):
        selectors = deepcopy(SELECTORS)
        data = decision()
        contract = build_qa_output_contract(REQUIREMENT_IDS, selectors)
        result = self.validate(data, selectors=selectors)
        data["cases"][0].update(title="changed", requirementId=str(uuid4()))
        data["cases"].clear()
        selectors.clear()
        self.assertEqual(result.cases[0].title, case()["title"])
        self.assertEqual(result.cases[0].requirement_id, REQUIREMENT_IDS[0])
        self.assertEqual(len(result.cases), 5)
        self.assertIn("protected", contract.schema.to_dict()["properties"]["cases"]["items"]["properties"]["selector"]["enum"])
        data = decision(kind="INPUT_REQUIRED", cases=[], questions=["원래 질문"])
        result = self.validate(data)
        data["questions"][0] = "changed"
        self.assertEqual(result.questions, ("원래 질문",))

    def test_decision_and_case_are_frozen_with_private_repr(self):
        result = self.validate()
        self.assertEqual(repr(result), "QADecision(kind='READY')")
        self.assertEqual(repr(result.cases[0]), "QACaseBinding()")
        with self.assertRaises(FrozenInstanceError):
            result.cases[0].title = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.cases += (result.cases[0],)
        with self.assertRaises(FrozenInstanceError):
            result.kind = "REJECTED"
        question = self.validate(decision(kind="INPUT_REQUIRED", cases=[], questions=["비공개 질문"]))
        self.assertEqual(repr(question), "QADecision(kind='INPUT_REQUIRED')")

    def test_direct_case_constructor_requires_all_inert_valid_fields(self):
        self.assertEqual(binding().tool_name, "run_unit_tests")
        fields = {"tool_name": "run_unit_tests", "selector": "protected", "test_id": "tests.case",
                  "requirement_id": REQUIREMENT_IDS[0], "title": "title", "expected_result": "expected"}
        for field, invalid_values in {
            "tool_name": (True, "run_security_scan", "run_unit_tests "),
            "selector": (True, "../scope", "UPPER", "a" * 65),
            "test_id": (True, "", "private\n", "😀" * 129, "\ud800private"),
            "requirement_id": (str(REQUIREMENT_IDS[0]), uuid1(), True),
            "title": (True, "", " ", "x" * 1025, "password='private'"),
            "expected_result": (True, "", " ", "x" * 4097, "api_key=private"),
        }.items():
            for value in invalid_values:
                self.assert_invalid(lambda: QACaseBinding(**{**fields, field: value}))

    def test_direct_decision_constructor_validates_branch_and_unique_case_identity(self):
        row = binding()
        self.assertEqual(QADecision(kind="READY", cases=(row,), questions=()).kind, "READY")
        self.assertEqual(QADecision(kind="INPUT_REQUIRED", cases=(), questions=("question",)).kind, "INPUT_REQUIRED")
        self.assertEqual(QADecision(kind="REJECTED", cases=(), questions=()).kind, "REJECTED")
        for fields in (
            {"kind": "READY", "cases": (), "questions": ()},
            {"kind": "READY", "cases": [row], "questions": ()},
            {"kind": "READY", "cases": ({},), "questions": ()},
            {"kind": "READY", "cases": (row, row), "questions": ()},
            {"kind": "READY", "cases": (row,), "questions": ("question",)},
            {"kind": "INPUT_REQUIRED", "cases": (row,), "questions": ("question",)},
            {"kind": "INPUT_REQUIRED", "cases": (), "questions": ()},
            {"kind": "INPUT_REQUIRED", "cases": (), "questions": ["question"]},
            {"kind": "INPUT_REQUIRED", "cases": (), "questions": ("question",) * 9},
            {"kind": "INPUT_REQUIRED", "cases": (), "questions": ("\ud800private",)},
            {"kind": "REJECTED", "cases": (row,), "questions": ()},
            {"kind": "REJECTED", "cases": (), "questions": ("question",)},
            {"kind": "INVALID", "cases": (), "questions": ()},
            {"kind": True, "cases": (), "questions": ()},
        ):
            self.assert_invalid(lambda: QADecision(**fields))

    def test_case_and_decision_constructors_are_keyword_only(self):
        with self.assertRaises(TypeError):
            QACaseBinding("run_unit_tests", "protected", "case", REQUIREMENT_IDS[0], "title", "expected")
        with self.assertRaises(TypeError):
            QADecision("REJECTED", (), ())

    def test_schema_copies_are_independent_and_admission_fails_closed(self):
        contract = build_qa_output_contract(REQUIREMENT_IDS, SELECTORS)
        changed = contract.schema.to_dict()
        changed["properties"]["kind"]["enum"].append("PASS")
        self.assertNotIn("PASS", build_qa_output_contract(REQUIREMENT_IDS, SELECTORS).schema.to_dict()["properties"]["kind"]["enum"])
        with patch("agents.roles.qa_contract.JsonSchema.validate", side_effect=ValueError("private schema failure")):
            self.reject(decision())
        with patch("agents.roles.qa_contract.JsonSchema.require_openai_strict", side_effect=ValueError("private strict failure")):
            self.assert_invalid(lambda: build_qa_output_contract(REQUIREMENT_IDS, SELECTORS))

    def test_no_files_network_tools_or_processes_are_used(self):
        with patch("builtins.open", side_effect=AssertionError("unexpected file I/O")), \
                patch.object(Path, "read_text", side_effect=AssertionError("unexpected schema asset read")), \
                patch("subprocess.run", side_effect=AssertionError("unexpected execution")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            build_qa_output_contract(REQUIREMENT_IDS, SELECTORS)
            self.assertEqual(self.validate().kind, "READY")
            self.assertEqual(self.validate(decision(kind="INPUT_REQUIRED", cases=[], questions=["질문"])).kind, "INPUT_REQUIRED")
            self.assertEqual(self.validate(decision(kind="REJECTED", cases=[])).kind, "REJECTED")
            self.assertEqual(QADecision(kind="READY", cases=(binding(),), questions=()).kind, "READY")


if __name__ == "__main__":
    unittest.main()
