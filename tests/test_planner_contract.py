"""Planner decision tests with inert data; no LLM, MCP, DB or product execution."""

from dataclasses import FrozenInstanceError, replace
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import uuid4

from agents.roles.planner_contract import (
    MAX_PLANNER_TASKS, PlannerContractError, PlannerDecision,
    build_planner_output_contract, validate_planner_decision,
)
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain.scenario_registry import SCENARIO_REGISTRY, SCN_001_ID


SCENARIO = SCENARIO_REGISTRY[SCN_001_ID]


def task(**changes):
    values = {"taskId": "TASK-001", "title": "회원가입 API 구현", "description": "보호된 요구사항을 구현한다.",
        "requirementIds": [str(value) for value in SCENARIO.requirement_ids], "dependsOn": []}
    values.update(changes)
    return values


def decision(**changes):
    values = {"kind": "PLAN", "implementationPlan": [task()], "questions": []}
    values.update(changes)
    return values


class PlannerContractTests(unittest.TestCase):
    def validate(self, data=None, scenario=SCENARIO):
        return validate_planner_decision(decision() if data is None else data, scenario)

    def assert_invalid(self, operation):
        with self.assertRaises(PlannerContractError) as caught:
            operation()
        self.assertEqual(caught.exception.code, "PLANNER_OUTPUT_INVALID")
        self.assertEqual(caught.exception.args, ("PLANNER_OUTPUT_INVALID",))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def reject(self, data):
        self.assert_invalid(lambda: self.validate(data))

    def test_model_contract_name_and_provider_strict_schema(self):
        contract = build_planner_output_contract(SCENARIO)
        self.assertEqual(contract.name, "planner_decision")
        contract.schema.require_openai_strict()
        schema = contract.schema.to_dict()
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])
        task_schema = schema["properties"]["implementationPlan"]["items"]
        self.assertEqual(set(task_schema["required"]), set(task_schema["properties"]))
        self.assertFalse(task_schema["additionalProperties"])
        encoded = contract.schema.schema_json
        for forbidden in ('"$id"', '"uniqueItems"', '"requirements"', '"schemaVersion"', '"artifactVersion"'):
            self.assertNotIn(forbidden, encoded)

    def test_uuid_enum_comes_from_host_only_and_exact_case(self):
        schema = build_planner_output_contract(SCENARIO).schema.to_dict()
        item = schema["properties"]["implementationPlan"]["items"]["properties"]["requirementIds"]["items"]
        self.assertEqual(item["enum"], [str(value) for value in SCENARIO.requirement_ids])
        for value in (str(uuid4()), str(SCENARIO.requirement_ids[0]).upper()):
            self.reject(decision(implementationPlan=[task(requirementIds=[value])]))

    def test_plan_uses_canonical_host_requirements_not_model_rewrites(self):
        result = self.validate()
        self.assertEqual(result.kind, "PLAN")
        self.assertIsInstance(result.plan, PlannerPlan)
        self.assertEqual(result.questions, ())
        payload = result.plan.model_dump(mode="json", by_alias=True)
        self.assertEqual(payload["schemaVersion"], 1)
        self.assertEqual(payload["requirements"], [{
            "requirementId": str(value.requirement_id), "key": value.key,
            "description": value.description, "acceptanceCriteria": list(value.acceptance_criteria),
        } for value in SCENARIO.requirements])
        SCENARIO.validate_planner_requirements(result.plan.requirements)

    def test_case_sensitive_canonical_text_is_copied_exactly(self):
        changed = replace(SCENARIO.requirements[0], description="Keep Canonical EMAIL Case",
            acceptance_criteria=("Exact PASS text", "SECOND Criterion"))
        scenario = replace(SCENARIO, requirements=(changed, *SCENARIO.requirements[1:]))
        result = self.validate(scenario=scenario)
        self.assertEqual(result.plan.requirements[0].description, changed.description)
        self.assertEqual(result.plan.requirements[0].acceptance_criteria, list(changed.acceptance_criteria))
        result.plan.requirements[0].description = changed.description.lower()
        with self.assertRaises(ValueError):
            scenario.validate_planner_requirements(result.plan.requirements)

    def test_mvp_all_eight_requirements_including_trace_are_preserved(self):
        result = self.validate()
        self.assertEqual([value.key for value in result.plan.requirements], [f"REQ-{index:03d}" for index in range(1, 9)])
        self.assertIn("DB UNIQUE", result.plan.requirements[2].acceptance_criteria[0])
        self.assertIn("Trace", result.plan.requirements[7].acceptance_criteria[0])

    def test_plan_can_be_split_with_valid_dependency_order(self):
        first = task(taskId="TASK-DATA", requirementIds=[str(value) for value in SCENARIO.requirement_ids[:4]])
        second = task(taskId="TASK-API", requirementIds=[str(value) for value in SCENARIO.requirement_ids[4:]],
            dependsOn=["TASK-DATA"])
        result = self.validate(decision(implementationPlan=[second, first]))
        self.assertEqual(result.plan.implementation_plan[0].depends_on, ["TASK-DATA"])

    def test_input_required_has_bounded_questions_and_no_plan(self):
        result = self.validate(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=["사용할 언어가 무엇인가요?"]))
        self.assertEqual(result.kind, "INPUT_REQUIRED")
        self.assertIsNone(result.plan)
        self.assertEqual(result.questions, ("사용할 언어가 무엇인가요?",))

    def test_rejected_contains_no_plan_or_model_reason(self):
        result = self.validate(decision(kind="REJECTED", implementationPlan=[]))
        self.assertEqual(result.kind, "REJECTED")
        self.assertIsNone(result.plan)
        self.assertEqual(result.questions, ())
        self.reject(decision(kind="REJECTED", implementationPlan=[], reason="private model explanation"))

    def test_decision_is_frozen_and_model_contents_hidden_in_repr(self):
        result = self.validate(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=["비공개 질문입니다."]))
        self.assertEqual(repr(result), "PlannerDecision(kind='INPUT_REQUIRED')")
        with self.assertRaises(FrozenInstanceError):
            result.kind = "PLAN"
        self.assertNotIn("비공개", repr(result))
        self.assertNotIn("회원가입", repr(self.validate()))

    def test_decision_constructor_rejects_inconsistent_state_and_types(self):
        for fields in (
            {"kind": "PLAN", "plan": None, "questions": ()},
            {"kind": "INPUT_REQUIRED", "plan": None, "questions": ()},
            {"kind": "INPUT_REQUIRED", "plan": None, "questions": []},
            {"kind": "REJECTED", "plan": self.validate().plan, "questions": ()},
            {"kind": "REJECTED", "plan": None, "questions": ("question",)},
            {"kind": "UNKNOWN", "plan": None, "questions": ()},
            {"kind": True, "plan": None, "questions": ()},
        ):
            self.assert_invalid(lambda: PlannerDecision(**fields))

    def test_model_root_cannot_supply_requirements_ids_metadata_or_verdict(self):
        for field in ("requirements", "runId", "workflowStepId", "scenarioId", "schemaVersion", "workspaceId",
                "projectArtifactId", "artifactVersion", "model", "executionManifest", "toolEvidence", "finalVerdict", "source"):
            self.reject(decision(**{field: "private-value"}))

    def test_model_task_cannot_supply_source_or_tool_output(self):
        for field in ("content", "patch", "command", "exitCode", "metadata", "requirement", "acceptanceCriteria"):
            self.reject(decision(implementationPlan=[task(**{field: "private-value"})]))

    def test_all_root_and_task_fields_are_required(self):
        original = decision()
        for field in original:
            self.reject({key: value for key, value in original.items() if key != field})
        original_task = task()
        for field in original_task:
            self.reject(decision(implementationPlan=[{key: value for key, value in original_task.items() if key != field}]))

    def test_kind_values_are_exact_not_coerced_or_case_folded(self):
        for value in (None, True, 1, "plan", "PLAN ", "TASK_STATE_COMPLETED", "AUTH_REQUIRED"):
            self.reject(decision(kind=value))

    def test_plan_must_have_tasks_without_questions(self):
        self.reject(decision(implementationPlan=[]))
        self.reject(decision(questions=["질문"] ))

    def test_nonplan_branches_never_contain_tasks(self):
        self.reject(decision(kind="INPUT_REQUIRED", questions=["질문"]))
        self.reject(decision(kind="REJECTED"))
        self.reject(decision(kind="REJECTED", implementationPlan=[], questions=["질문"]))

    def test_input_questions_require_one_through_eight_nonblank_strings(self):
        for questions in ([], [""], [" \t\n"], [None], [True], ["질문"] * 9):
            self.reject(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=questions))
        result = self.validate(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=[f"질문 {value}" for value in range(8)]))
        self.assertEqual(len(result.questions), 8)

    def test_question_and_task_text_lengths_bounded(self):
        for changes in ({"title": "x" * 257}, {"description": "x" * 4097}, {"taskId": "TASK-" + "X" * 65}):
            self.reject(decision(implementationPlan=[task(**changes)]))
        self.reject(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=["x" * 1025]))
        self.assertEqual(self.validate(decision(implementationPlan=[task(title="x" * 256, description="x" * 4096)])).kind, "PLAN")

    def test_total_json_limit_is_utf8_bytes_not_only_individual_characters(self):
        tasks = [task(taskId=f"TASK-{index}", description="한" * 4096) for index in range(128)]
        self.reject(decision(implementationPlan=tasks))

    def test_contract_construction_and_control_decisions_do_not_read_schema_files(self):
        with patch.object(Path, "read_text", side_effect=AssertionError("must not read in construction")):
            self.assertEqual(build_planner_output_contract(SCENARIO).name, "planner_decision")
            result = self.validate(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=["질문이 있습니다."]))
            self.assertIsNone(result.plan)

    def test_tasks_limited_to_128(self):
        tasks = [task(taskId=f"TASK-{index}") for index in range(MAX_PLANNER_TASKS)]
        self.assertEqual(len(self.validate(decision(implementationPlan=tasks)).plan.implementation_plan), 128)
        self.reject(decision(implementationPlan=tasks + [task(taskId="TASK-EXTRA")]))

    def test_task_strings_reject_blank_and_controls_without_normalizing_identity(self):
        for field in ("title", "description"):
            for value in ("", " \n\t", "private\x00text", "private\x7ftext", "private\x85text"):
                self.reject(decision(implementationPlan=[task(**{field: value})]))
        for value in ("task-001", "TASK-001\n", " TASK-001", "TASK-../X"):
            self.reject(decision(implementationPlan=[task(taskId=value)]))

    def test_duplicate_task_ids_and_requirement_refs_rejected_by_existing_plan(self):
        self.reject(decision(implementationPlan=[task(), task()]))
        self.reject(decision(implementationPlan=[task(requirementIds=[str(value) for value in SCENARIO.requirement_ids] + [str(SCENARIO.requirement_ids[0])])]))
        # Keep the list within its maximum so graph semantic uniqueness also
        # rejects duplicates, not only the JSON array cardinality guard.
        self.reject(decision(implementationPlan=[task(requirementIds=[str(SCENARIO.requirement_ids[0])] * 2)]))

    def test_omitted_requirement_coverage_is_not_hidden_by_host_copy(self):
        self.reject(decision(implementationPlan=[task(requirementIds=[str(value) for value in SCENARIO.requirement_ids[:-1]])]))

    def test_unknown_self_duplicate_and_case_mismatched_dependencies_rejected(self):
        for dependencies in (["TASK-MISSING"], ["TASK-001"], ["task-001"], ["TASK-002", "TASK-002"]):
            self.reject(decision(implementationPlan=[task(dependsOn=dependencies), task(taskId="TASK-002")]))

    def test_dependency_cycles_rejected_without_topological_order_requirement(self):
        self.reject(decision(implementationPlan=[task(dependsOn=["TASK-002"]),
            task(taskId="TASK-002", dependsOn=["TASK-003"]), task(taskId="TASK-003", dependsOn=["TASK-001"])]))

    def test_unknown_uuid_null_and_nonjson_decisions_rejected(self):
        for value in (None, [], (), True, "private-source", {1: "private-value"}, object()):
            self.assert_invalid(lambda: validate_planner_decision(value, SCENARIO))
        for values in (None, (), [True], [str(uuid4())], []):
            self.reject(decision(implementationPlan=[task(requirementIds=values)]))

    def test_decoded_data_cannot_alias_returned_plan_or_questions(self):
        data = decision()
        result = self.validate(data)
        data["implementationPlan"][0]["title"] = "changed"
        data["implementationPlan"][0]["requirementIds"].clear()
        self.assertNotEqual(result.plan.implementation_plan[0].title, "changed")
        self.assertEqual(len(result.plan.implementation_plan[0].requirement_ids), 8)

    def test_recognized_credentials_are_rejected_not_silently_rewritten(self):
        for text in ("password='never-print-this-test-credential'", "api_key=private-test-key",
                "Bearer private-test-token", "$argon2id$v=19$m=19456,t=2,p=1$c2FsdA$aGFzaA"):
            self.reject(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=[text]))
            self.reject(decision(implementationPlan=[task(description=text)]))

    def test_redacted_question_can_be_accepted_but_has_no_secret_guarantee(self):
        result = self.validate(decision(kind="INPUT_REQUIRED", implementationPlan=[], questions=["[REDACTED] 값을 다시 확인할까요?"]))
        self.assertEqual(result.questions[0], "[REDACTED] 값을 다시 확인할까요?")

    def test_invalid_host_scenario_baseline_fails_closed(self):
        for scenario in (None, {}, replace(SCENARIO, requirements=()),
                replace(SCENARIO, requirements=SCENARIO.requirements + (SCENARIO.requirements[0],))):
            self.assert_invalid(lambda: build_planner_output_contract(scenario))
            self.assert_invalid(lambda: self.validate(scenario=scenario))

    def test_canonical_schema_is_actually_applied_and_offline(self):
        with patch("urllib.request.urlopen", side_effect=AssertionError("external schema fetch")):
            self.assertEqual(self.validate().kind, "PLAN")
        with patch("agents.roles.planner_contract._canonical_schema", return_value={"type": "object", "required": ["notCanonical"]}):
            self.assert_invalid(lambda: self.validate())

    def test_missing_or_remote_shipped_schema_is_not_silently_bypassed(self):
        with patch.object(Path, "read_text", side_effect=OSError("private-host-path")):
            self.assert_invalid(lambda: self.validate())
        with patch.object(Path, "read_text", return_value=json.dumps({"type": "object", "$ref": "https://unsafe.invalid/schema"})):
            self.assert_invalid(lambda: self.validate())

    def test_bad_json_nonfinite_and_string_surrogate_errors_do_not_leak(self):
        self.reject(decision(implementationPlan=[task(title=float("nan"))]))
        self.reject(decision(implementationPlan=[task(description="\ud800private")]))


if __name__ == "__main__":
    unittest.main()
