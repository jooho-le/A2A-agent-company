import json
import re
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    FinalVerdict,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)


class WorkflowRunTests(unittest.TestCase):
    def make_run(self, **updates: object) -> WorkflowRun:
        values: dict[str, object] = {
            "scenario_id": uuid4(),
            "request_text": "회원가입 기능을 구현한다.",
        }
        values.update(updates)
        return WorkflowRun(**values)

    def test_ids_are_uuid4_and_fix_count_is_separate_from_a2a_attempt(self) -> None:
        run = self.make_run()
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER)

        self.assertEqual(run.run_id.version, 4)
        self.assertEqual(step.workflow_step_id.version, 4)
        self.assertEqual(run.fix_attempt, 0)
        self.assertEqual(step.attempt, 0)

    def test_finished_run_requires_final_verdict(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_run(status=WorkflowStatus.FINISHED)

        finished = self.make_run().with_outcome(
            status=WorkflowStatus.FINISHED,
            verdict=FinalVerdict.SUCCESS,
        )
        self.assertEqual(finished.status, WorkflowStatus.FINISHED)
        self.assertEqual(finished.verdict, FinalVerdict.SUCCESS)

    def test_aborted_run_has_no_verdict_and_requires_reason(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_run(status=WorkflowStatus.ABORTED)

        aborted = self.make_run().with_outcome(
            status=WorkflowStatus.ABORTED,
            termination_reason="USER_CANCELLED",
        )
        self.assertIsNone(aborted.verdict)
        self.assertEqual(aborted.termination_reason, "USER_CANCELLED")

        with self.assertRaises(ValidationError):
            self.make_run(
                status=WorkflowStatus.ABORTED,
                verdict=FinalVerdict.SUCCESS,
                termination_reason="USER_CANCELLED",
            )

    def test_fix_attempt_is_limited_to_three(self) -> None:
        self.assertEqual(self.make_run(fix_attempt=3).fix_attempt, 3)
        with self.assertRaises(ValidationError):
            self.make_run(fix_attempt=4)

    def test_fixing_is_a_distinct_workflow_state(self) -> None:
        self.assertEqual(WorkflowStatus.FIXING.value, "FIXING")


class A2AContractModelTests(unittest.TestCase):
    def test_a2a_state_value_keeps_official_prefix(self) -> None:
        self.assertEqual(A2ATaskState.COMPLETED.value, "TASK_STATE_COMPLETED")

    def test_agent_context_keeps_server_ids_opaque(self) -> None:
        context = AgentContext(
            run_id=uuid4(),
            agent_id="planner",
            agent_context_id="opaque-context",
            latest_a2a_task_id="opaque-task",
        )
        self.assertEqual(context.agent_context_id, "opaque-context")
        self.assertEqual(context.latest_a2a_task_id, "opaque-task")

    def test_metadata_schema_requires_all_four_contract_fields(self) -> None:
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "schemas"
            / "project"
            / "workflow_metadata.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            set(schema["required"]),
            {"runId", "workflowStepId", "scenarioId", "attempt"},
        )
        self.assertNotIn("requirementKeys", schema["properties"])

    def test_send_message_example_has_top_level_metadata_and_attempt(self) -> None:
        contract_path = Path(__file__).resolve().parents[1] / "docs" / "01-orchestrator-contracts.md"
        contract = contract_path.read_text(encoding="utf-8")
        match = re.search(r"```json\s*(.*?)\s*```", contract, re.DOTALL)
        self.assertIsNotNone(match)
        request = json.loads(match.group(1))

        self.assertEqual(request["metadata"]["attempt"], 0)
        self.assertNotIn("metadata", request["message"])


if __name__ == "__main__":
    unittest.main()
