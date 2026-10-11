"""Developer Draft admission with inert text; no LLM, MCP, DB or execution."""

from dataclasses import FrozenInstanceError
from pathlib import Path
import unittest
from unittest.mock import patch

from agents.roles.developer_contract import (
    MAX_DEVELOPER_JSON_BYTES, MAX_DEVELOPER_QUESTIONS,
    MAX_DEVELOPER_QUESTION_LENGTH, MAX_DEVELOPER_SUMMARY_LENGTH,
    DeveloperContractError, DeveloperDecision,
    build_developer_output_contract, validate_developer_decision,
)


def decision(**changes):
    values = {"kind": "READY", "summary": "할당된 회원가입 기능을 구현했습니다.", "questions": []}
    values.update(changes)
    return values


class DeveloperContractTests(unittest.TestCase):
    def validate(self, data=None):
        return validate_developer_decision(decision() if data is None else data)

    def assert_invalid(self, operation):
        with self.assertRaises(DeveloperContractError) as caught:
            operation()
        error = caught.exception
        self.assertEqual(error.code, "DEVELOPER_OUTPUT_INVALID")
        self.assertEqual(error.args, ("DEVELOPER_OUTPUT_INVALID",))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        self.assertEqual(repr(error), "DeveloperContractError('DEVELOPER_OUTPUT_INVALID')")

    def reject(self, data):
        self.assert_invalid(lambda: validate_developer_decision(data))

    def test_closed_all_required_provider_strict_schema(self):
        contract = build_developer_output_contract()
        self.assertEqual(contract.name, "developer_decision")
        contract.schema.require_openai_strict()
        schema = contract.schema.to_dict()
        self.assertEqual(set(schema["properties"]), {"kind", "summary", "questions"})
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schema["properties"]["kind"]["enum"], ["READY", "INPUT_REQUIRED", "REJECTED"])
        for forbidden in ('"$id"', '"$ref"', '"uniqueItems"', '"if"', '"then"', '"allOf"'):
            self.assertNotIn(forbidden, contract.schema.schema_json)

    def test_ready_has_summary_but_no_execution_claim_fields(self):
        result = self.validate()
        self.assertEqual(result.kind, "READY")
        self.assertEqual(result.summary, decision()["summary"])
        self.assertEqual(result.questions, ())
        self.assertFalse(hasattr(result, "source"))
        self.assertFalse(hasattr(result, "build_report"))
        self.assertFalse(hasattr(result, "tool_evidence"))

    def test_input_required_has_questions_and_empty_summary(self):
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=["어떤 API 경로를 사용할까요?"]))
        self.assertEqual(result.kind, "INPUT_REQUIRED")
        self.assertEqual(result.summary, "")
        self.assertEqual(result.questions, ("어떤 API 경로를 사용할까요?",))

    def test_rejected_has_neither_summary_nor_model_reason(self):
        result = self.validate(decision(kind="REJECTED", summary=""))
        self.assertEqual((result.kind, result.summary, result.questions), ("REJECTED", "", ()))
        self.reject(decision(kind="REJECTED", summary="", reason="private explanation"))

    def test_every_model_field_is_required(self):
        original = decision()
        for field in original:
            self.reject({key: value for key, value in original.items() if key != field})

    def test_model_cannot_supply_source_file_changes_or_hashes(self):
        for field in ("source", "sourceCode", "content", "patch", "fileChanges", "changedFiles",
                "snapshotId", "snapshotSha256", "commitHash", "treeHash", "gitObjectFormat",
                "repositoryId", "containerImageDigest", "dependencyLockHash", "sourceArtifactId"):
            self.reject(decision(**{field: "private-value"}))

    def test_model_cannot_supply_tool_execution_results_or_verdict(self):
        for field in ("exitCode", "durationMs", "stdoutRef", "stderrRef", "executionManifestId",
                "executionManifest", "executionOutcome", "failureKind", "toolEvidence",
                "executionId", "evidenceRef", "attempts", "buildReport", "testResults", "finalVerdict"):
            self.reject(decision(**{field: "private-value"}))

    def test_model_cannot_supply_host_identity_baseline_or_runtime_settings(self):
        for field in ("runId", "workflowStepId", "scenarioId", "workspaceId", "taskId", "contextId",
                "a2aTaskId", "artifactId", "a2aArtifactId", "projectArtifactId", "artifactVersion",
                "previousArtifactId", "createdAt", "metadata", "requirementIds", "requirements",
                "acceptanceCriteria", "implementationPlan", "codeVersion", "model", "limits",
                "configuration", "command", "questionsCount"):
            self.reject(decision(**{field: "private-value"}))

    def test_kinds_are_case_sensitive_and_never_coerced(self):
        for value in (None, True, 1, "ready", "READY ", "COMPLETED", "PASS", "AUTH_REQUIRED", "FAILED"):
            self.reject(decision(kind=value))

    def test_ready_requires_a_nonblank_summary(self):
        for value in ("", " ", "\t\n", None, True, 1, [], {}):
            self.reject(decision(summary=value))

    def test_ready_cannot_have_questions(self):
        self.reject(decision(questions=["질문"] ))

    def test_input_required_has_one_through_eight_questions(self):
        for values in ([], [""], [" \t\n"], [None], [True], [1], ["질문"] * 9):
            self.reject(decision(kind="INPUT_REQUIRED", summary="", questions=values))
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="",
            questions=[f"질문 {number}" for number in range(MAX_DEVELOPER_QUESTIONS)]))
        self.assertEqual(len(result.questions), MAX_DEVELOPER_QUESTIONS)

    def test_nonready_branches_require_exactly_empty_summary(self):
        for value in ("work", " ", "\n", None, True):
            self.reject(decision(kind="INPUT_REQUIRED", summary=value, questions=["질문"]))
            self.reject(decision(kind="REJECTED", summary=value))

    def test_rejected_cannot_have_questions(self):
        self.reject(decision(kind="REJECTED", summary="", questions=["질문"]))

    def test_questions_must_be_a_native_array_of_strings(self):
        for value in (None, (), ("question",), True, "question", {}, [{"text": "private"}], [["private"]]):
            self.reject(decision(questions=value))

    def test_text_character_limits_are_enforced(self):
        self.reject(decision(summary="x" * (MAX_DEVELOPER_SUMMARY_LENGTH + 1)))
        self.assertEqual(len(self.validate(decision(summary="x" * MAX_DEVELOPER_SUMMARY_LENGTH)).summary), MAX_DEVELOPER_SUMMARY_LENGTH)
        self.reject(decision(kind="INPUT_REQUIRED", summary="", questions=["x" * (MAX_DEVELOPER_QUESTION_LENGTH + 1)]))
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=["x" * MAX_DEVELOPER_QUESTION_LENGTH]))
        self.assertEqual(len(result.questions[0]), MAX_DEVELOPER_QUESTION_LENGTH)

    def test_multibyte_text_at_bounded_limit_is_preserved(self):
        summary = "😀" * MAX_DEVELOPER_SUMMARY_LENGTH
        result = self.validate(decision(summary=summary))
        self.assertEqual(result.summary, summary)
        self.assertEqual(len(result.summary.encode("utf-8")), 16_384)
        question = "😀" * MAX_DEVELOPER_QUESTION_LENGTH
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=[question]))
        self.assertEqual(result.questions, (question,))

    def test_total_json_byte_limit_is_applied_even_after_field_validation(self):
        self.assertEqual(MAX_DEVELOPER_JSON_BYTES, 1_048_576)
        with patch("agents.roles.developer_contract.MAX_DEVELOPER_JSON_BYTES", 32):
            self.reject(decision())

    def test_ascii_controls_del_and_c1_are_rejected(self):
        for character in ("\x00", "\x01", "\x08", "\x0b", "\x0c", "\r", "\x1f", "\x7f", "\x80", "\x9f"):
            self.reject(decision(summary="text" + character))
            self.reject(decision(kind="INPUT_REQUIRED", summary="", questions=["text" + character]))

    def test_newline_tab_and_original_whitespace_are_preserved(self):
        summary = "  변경 요약\n\t설명  "
        self.assertEqual(self.validate(decision(summary=summary)).summary, summary)
        question = "  확인 질문\n\t선택할까요?  "
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=[question]))
        self.assertEqual(result.questions, (question,))

    def test_recognized_credentials_are_rejected_not_rewritten(self):
        texts = (
            "password='private-test-password'", "api_key=private-test-key",
            "Authorization: Bearer private-test-token", "Bearer private-test-token",
            "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA",
            "$2b$12$" + "A" * 53, "eyJabc.example.signature",
        )
        for text in texts:
            self.reject(decision(summary=text))
            self.reject(decision(kind="INPUT_REQUIRED", summary="", questions=[text]))

    def test_redacted_text_can_be_accepted_without_secret_guarantee(self):
        result = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=["[REDACTED] 설정을 확인할까요?"]))
        self.assertEqual(result.questions, ("[REDACTED] 설정을 확인할까요?",))

    def test_invalid_json_types_and_surrogates_have_safe_errors(self):
        for value in (None, [], (), True, "private-content", {1: "private"}, object()):
            self.reject(value)
        for value in (float("nan"), float("inf"), float("-inf"), "\ud800private"):
            self.reject(decision(summary=value))
            self.reject(decision(kind="INPUT_REQUIRED", summary="", questions=[value]))

    def test_custom_native_subclasses_are_not_model_json(self):
        class CustomDict(dict):
            pass

        class CustomList(list):
            pass

        class CustomStr(str):
            pass

        self.reject(CustomDict(decision()))
        self.reject(decision(summary=CustomStr("summary")))
        self.reject(decision(kind=CustomStr("READY")))
        self.reject(decision(questions=CustomList()))

    def test_mutating_submitted_question_list_does_not_change_decision(self):
        data = decision(kind="INPUT_REQUIRED", summary="", questions=["처음 질문"])
        result = self.validate(data)
        data["questions"][0] = "changed"
        data["questions"].append("new")
        data["kind"] = "READY"
        self.assertEqual(result.kind, "INPUT_REQUIRED")
        self.assertEqual(result.questions, ("처음 질문",))

    def test_decision_is_deeply_immutable_and_repr_hides_text(self):
        result = self.validate()
        self.assertEqual(repr(result), "DeveloperDecision(kind='READY')")
        with self.assertRaises(FrozenInstanceError):
            result.summary = "changed"
        with self.assertRaises(FrozenInstanceError):
            result.kind = "REJECTED"
        question = self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=["비공개 질문"]))
        self.assertEqual(repr(question), "DeveloperDecision(kind='INPUT_REQUIRED')")
        self.assertNotIn("비공개", repr(question))
        with self.assertRaises(FrozenInstanceError):
            question.questions += ("changed",)

    def test_direct_constructor_validates_the_same_state_and_type_contract(self):
        for fields in (
            {"kind": "READY", "summary": "", "questions": ()},
            {"kind": "READY", "summary": "summary", "questions": ("question",)},
            {"kind": "INPUT_REQUIRED", "summary": "", "questions": ()},
            {"kind": "INPUT_REQUIRED", "summary": "summary", "questions": ("question",)},
            {"kind": "REJECTED", "summary": "summary", "questions": ()},
            {"kind": "REJECTED", "summary": "", "questions": ("question",)},
            {"kind": "UNKNOWN", "summary": "", "questions": ()},
            {"kind": True, "summary": "", "questions": ()},
            {"kind": "READY", "summary": "summary", "questions": []},
            {"kind": "READY", "summary": "password='private-test'", "questions": ()},
            {"kind": "READY", "summary": "\ud800private", "questions": ()},
            {"kind": "INPUT_REQUIRED", "summary": "", "questions": ("q",) * 9},
            {"kind": "INPUT_REQUIRED", "summary": "", "questions": ("\x00private",)},
        ):
            self.assert_invalid(lambda: DeveloperDecision(**fields))

    def test_direct_constructor_accepts_only_consistent_branches(self):
        self.assertEqual(DeveloperDecision(kind="READY", summary="summary", questions=()).kind, "READY")
        self.assertEqual(DeveloperDecision(kind="INPUT_REQUIRED", summary="", questions=("question",)).kind, "INPUT_REQUIRED")
        self.assertEqual(DeveloperDecision(kind="REJECTED", summary="", questions=()).kind, "REJECTED")

    def test_builder_returns_independent_schema_copies(self):
        first = build_developer_output_contract().schema.to_dict()
        first["properties"]["kind"]["enum"].append("PASS")
        second = build_developer_output_contract().schema.to_dict()
        self.assertNotIn("PASS", second["properties"]["kind"]["enum"])
        self.reject(decision(kind="PASS"))

    def test_schema_admission_is_actually_applied_and_fails_closed(self):
        with patch("agents.roles.developer_contract.JsonSchema.validate", side_effect=ValueError("private provider error")):
            self.reject(decision())
        with patch("agents.roles.developer_contract.JsonSchema.require_openai_strict", side_effect=ValueError("private schema error")):
            self.assert_invalid(build_developer_output_contract)

    def test_no_files_network_tools_or_processes_are_used(self):
        with patch("builtins.open", side_effect=AssertionError("unexpected file I/O")), \
                patch.object(Path, "read_text", side_effect=AssertionError("unexpected schema asset read")), \
                patch("subprocess.run", side_effect=AssertionError("unexpected execution")), \
                patch("socket.create_connection", side_effect=AssertionError("unexpected network")):
            build_developer_output_contract()
            self.assertEqual(self.validate().kind, "READY")
            self.assertEqual(self.validate(decision(kind="INPUT_REQUIRED", summary="", questions=["질문"])).kind, "INPUT_REQUIRED")
            self.assertEqual(self.validate(decision(kind="REJECTED", summary="")).kind, "REJECTED")


if __name__ == "__main__":
    unittest.main()
