import copy
import unittest
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.application import PlannerOutputValidationError, parse_planner_output


class PlannerOutputTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = uuid4()
        self.workflow_step_id = uuid4()
        self.requirement_id = uuid4()
        self.project_artifact_id = uuid4()
        self.plan = {
            "schemaVersion": 1,
            "requirements": [
                {
                    "requirementId": str(self.requirement_id),
                    "key": "REQ-001",
                    "description": "유효한 가입 요청을 처리한다.",
                    "acceptanceCriteria": ["정상 입력이면 계정이 생성된다."],
                }
            ],
            "implementationPlan": [
                {
                    "taskId": "TASK-001",
                    "title": "가입 API 구현",
                    "description": "요구사항과 수용 기준에 맞춰 구현한다.",
                    "requirementIds": [str(self.requirement_id)],
                    "dependsOn": [],
                }
            ],
        }

    def make_task(
        self,
        *,
        plan=None,
        artifact_metadata=None,
        task_state="TASK_STATE_COMPLETED",
    ) -> Task:
        metadata = {
            "runId": str(self.run_id),
            "workflowStepId": str(self.workflow_step_id),
            "projectArtifactId": str(self.project_artifact_id),
            "artifactVersion": 1,
        }
        metadata.update(artifact_metadata or {})
        return ParseDict(
            {
                "id": "planner-task-opaque",
                "status": {"state": task_state},
                "artifacts": [
                    {
                        "artifactId": "planner-artifact-opaque",
                        "name": "requirements.json",
                        "parts": [
                            {
                                "data": plan or self.plan,
                                "mediaType": "application/json",
                            }
                        ],
                        "metadata": metadata,
                    }
                ],
            },
            Task(),
        )

    def parse(self, task: Task):
        return parse_planner_output(
            task,
            run_id=self.run_id,
            workflow_step_id=self.workflow_step_id,
        )

    def test_valid_plan_and_artifact_metadata_are_normalized(self) -> None:
        output = self.parse(self.make_task())

        self.assertEqual(output.a2a_artifact_id, "planner-artifact-opaque")
        self.assertEqual(output.project_artifact_id, self.project_artifact_id)
        self.assertEqual(output.artifact_version, 1)
        self.assertEqual(output.plan.requirements[0].requirement_id, self.requirement_id)

    def test_rejects_artifact_from_a_different_run_or_step(self) -> None:
        for metadata in (
            {"runId": str(uuid4())},
            {"workflowStepId": str(uuid4())},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(PlannerOutputValidationError):
                    self.parse(self.make_task(artifact_metadata=metadata))

    def test_rejects_invalid_requirement_and_acceptance_criteria(self) -> None:
        invalid_plan = copy.deepcopy(self.plan)
        invalid_plan["requirements"][0]["requirementId"] = str(uuid4())
        invalid_plan["requirements"][0]["acceptanceCriteria"] = ["  "]

        with self.assertRaises(PlannerOutputValidationError):
            self.parse(self.make_task(plan=invalid_plan))

    def test_rejects_unresolved_and_cyclic_implementation_dependencies(self) -> None:
        unknown_requirement_plan = copy.deepcopy(self.plan)
        unknown_requirement_plan["implementationPlan"][0]["requirementIds"] = [
            str(uuid4())
        ]
        cyclic_plan = copy.deepcopy(self.plan)
        cyclic_plan["implementationPlan"][0]["dependsOn"] = ["TASK-001"]

        for plan in (unknown_requirement_plan, cyclic_plan):
            with self.subTest(plan=plan):
                with self.assertRaises(PlannerOutputValidationError):
                    self.parse(self.make_task(plan=plan))

    def test_rejects_non_completed_planner_task(self) -> None:
        with self.assertRaises(PlannerOutputValidationError):
            self.parse(self.make_task(task_state="TASK_STATE_WORKING"))


if __name__ == "__main__":
    unittest.main()
