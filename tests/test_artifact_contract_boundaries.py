import copy
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict
from pydantic import ValidationError

from test_dispatch import FakePlannerClient
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.a2a.registry import A2AAgentRegistry
from orchestrator.application.dispatch import PlannerRunDispatcher
from orchestrator.application.developer_output import DeveloperOutputValidationError, parse_developer_output
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.application.validation_output import ValidationOutputError, parse_validation_output
from orchestrator.domain import (
    A2ATaskState, AgentRole, CodeSnapshotArtifact, SCN_001_ID, SnapshotHandoff,
    FinalVerdict, TraceEvent, WorkflowRun, WorkflowStep, WorkflowStepStatus,
)
from orchestrator.domain.contract_validation import require_json_integer
from orchestrator.infrastructure import SQLiteWorkflowRepository


class PaddedOpaqueClient(FakePlannerClient):
    @staticmethod
    def preserve_noncanonical_ids(task):
        wire = MessageToDict(task)
        wire["id"] = " " + wire["id"] + " "
        for artifact in wire.get("artifacts", []):
            artifact["artifactId"] = " " + artifact["artifactId"] + " "
            payload = artifact["parts"][0]["data"]
            if "a2aTaskId" in payload:
                payload["a2aTaskId"] = wire["id"]
            if "a2aArtifactId" in payload:
                payload["a2aArtifactId"] = artifact["artifactId"]
        return ParseDict(wire, Task())

    async def send_task(self, *args, **kwargs):
        return self.preserve_noncanonical_ids(await super().send_task(*args, **kwargs))

    async def send_snapshot_handoff(self, *args, **kwargs):
        return self.preserve_noncanonical_ids(await super().send_snapshot_handoff(*args, **kwargs))


class ArtifactContractBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
        self.requirement_ids = [uuid4()]
        self.step = WorkflowStep(
            run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
            status=WorkflowStepStatus.SUCCEEDED, a2a_task_id="developer-task-opaque",
            a2a_task_state=A2ATaskState.COMPLETED, requirement_ids=self.requirement_ids,
        )
        metadata = A2AWorkflowMetadata(
            run_id=self.run.run_id, workflow_step_id=self.step.workflow_step_id,
            scenario_id=self.run.scenario_id, attempt=0,
            requirement_ids=self.requirement_ids, code_version=1,
        )
        client = FakePlannerClient("TASK_STATE_COMPLETED")
        self.wire = {
            "id": self.step.a2a_task_id, "contextId": "developer-context-opaque",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": client._developer_artifacts(metadata),
        }

    def parse_developer(self, wire):
        return parse_developer_output(ParseDict(wire, Task()), run=self.run, step=self.step)

    async def test_developer_report_versions_reject_boolean_and_string_coercion(self):
        for index in range(3):
            for field in ("artifactVersion", "codeVersion"):
                for invalid in (True, False, "1", 1.5):
                    with self.subTest(index=index, field=field, invalid=invalid):
                        wire = copy.deepcopy(self.wire)
                        wire["artifacts"][index]["parts"][0]["data"][field] = invalid
                        with self.assertRaises(DeveloperOutputValidationError):
                            self.parse_developer(wire)

    async def test_manifest_versions_reject_boolean_and_string_coercion(self):
        for invalid in (True, "1", 1.5):
            wire = copy.deepcopy(self.wire)
            wire["artifacts"][2]["parts"][0]["data"]["executionManifest"]["codeVersion"] = invalid
            with self.subTest(invalid=invalid), self.assertRaises(DeveloperOutputValidationError):
                self.parse_developer(wire)

    async def test_integral_protojson_numbers_remain_valid(self):
        wire = copy.deepcopy(self.wire)
        for artifact in wire["artifacts"]:
            artifact["metadata"]["artifactVersion"] = 1.0
            artifact["parts"][0]["data"]["artifactVersion"] = 1.0
            artifact["parts"][0]["data"]["codeVersion"] = 1.0
        result = self.parse_developer(wire)
        self.assertEqual(result.source.code_version, 1)
        self.assertIs(type(result.build_report.artifact_version), int)

    async def test_opaque_developer_ids_are_preserved_exactly(self):
        wire = copy.deepcopy(self.wire)
        task_id = " developer-task opaque/?. "
        wire["id"] = task_id
        self.step.a2a_task_id = task_id
        for artifact in wire["artifacts"]:
            artifact["artifactId"] = " " + artifact["artifactId"] + " "
            payload = artifact["parts"][0]["data"]
            payload["a2aTaskId"] = task_id
            payload["a2aArtifactId"] = artifact["artifactId"]
        result = self.parse_developer(wire)
        for record, artifact in zip((result.source, result.change_report, result.build_report), wire["artifacts"]):
            self.assertEqual(record.a2a_task_id, task_id)
            self.assertEqual(record.a2a_artifact_id, artifact["artifactId"])

    async def test_blank_opaque_source_ids_are_still_rejected(self):
        values = self.parse_developer(self.wire).source.model_dump()
        for field in ("a2a_task_id", "a2a_artifact_id"):
            with self.subTest(field=field), self.assertRaises(ValidationError):
                CodeSnapshotArtifact.model_validate({**values, field: " \t "})

    async def validation_fixture(self, role):
        source = self.parse_developer(self.wire).source
        step = WorkflowStep(
            run_id=self.run.run_id, agent_role=role, status=WorkflowStepStatus.SUCCEEDED,
            a2a_task_id=f"{role.value.lower()}-task-v1-opaque",
            a2a_task_state=A2ATaskState.COMPLETED,
            requirement_ids=self.requirement_ids, code_version=1,
        )
        task = await FakePlannerClient("TASK_STATE_COMPLETED").send_snapshot_handoff(
            SnapshotHandoff.from_snapshot(source), role, "고정 Snapshot 검증",
            workflow_step_id=step.workflow_step_id, requirement_ids=self.requirement_ids,
        )
        return source, step, MessageToDict(task)

    async def test_qa_security_report_versions_reject_invalid_json_integer_types(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            source, step, base = await self.validation_fixture(role)
            for field in ("artifactVersion", "codeVersion"):
                for invalid in (True, "1", 1.5):
                    wire = copy.deepcopy(base)
                    wire["artifacts"][0]["parts"][0]["data"][field] = invalid
                    with self.subTest(role=role, field=field, invalid=invalid), self.assertRaises(ValidationOutputError):
                        parse_validation_output(ParseDict(wire, Task()), run=self.run, step=step, source=source)

    async def test_qa_security_opaque_ids_are_preserved(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            source, step, wire = await self.validation_fixture(role)
            task_id = " " + wire["id"] + " "
            wire["id"] = task_id
            step.a2a_task_id = task_id
            artifact = wire["artifacts"][0]
            artifact["artifactId"] = " " + artifact["artifactId"] + " "
            artifact["parts"][0]["data"]["a2aTaskId"] = task_id
            artifact["parts"][0]["data"]["a2aArtifactId"] = artifact["artifactId"]
            with self.subTest(role=role):
                report = parse_validation_output(ParseDict(wire, Task()), run=self.run, step=step, source=source)
                self.assertEqual(report.a2a_task_id, task_id)
                self.assertEqual(report.a2a_artifact_id, artifact["artifactId"])

    async def test_planner_schema_version_is_a_real_json_integer(self):
        requirement_id = str(self.requirement_ids[0])
        plan = {
            "schemaVersion": 1,
            "requirements": [{"requirementId": requirement_id, "key": "REQ-001", "description": "가입", "acceptanceCriteria": ["가입 성공"]}],
            "implementationPlan": [{"taskId": "TASK-001", "title": "구현", "description": "가입 구현", "requirementIds": [requirement_id]}],
        }
        self.assertEqual(PlannerPlan.model_validate(plan).schema_version, 1)
        for invalid in (True, "1", 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                PlannerPlan.model_validate({**plan, "schemaVersion": invalid})

    async def test_integer_helper_rejects_nonfinite_values_without_overflowing_ints(self):
        for invalid in (True, "1", float("nan"), float("inf"), float("-inf"), 1.5):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                require_json_integer(invalid)
        self.assertEqual(require_json_integer(10 ** 400), 10 ** 400)
        self.assertEqual(require_json_integer(1.0), 1)

    async def test_opaque_ids_survive_full_dispatch_and_registry_storage(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = SQLiteWorkflowRepository(Path(directory) / "opaque.sqlite3")
            run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입")
            step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
            repository.create_run(run, (step,), (
                TraceEvent(run_id=run.run_id, event_type="RUN_STARTED",
                           actor="Orchestrator", attempt=0, workflow_state=run.status),
                TraceEvent(run_id=run.run_id, workflow_step_id=step.workflow_step_id,
                           event_type="WORKFLOW_STEP_CREATED", actor="Orchestrator",
                           attempt=0, workflow_state=run.status),
            ))
            client = PaddedOpaqueClient("TASK_STATE_COMPLETED")
            registry = A2AAgentRegistry({role: f"http://{role.value.lower()}.test" for role in AgentRole})
            await PlannerRunDispatcher(repository, registry, client_factory=lambda url: client).dispatch_planner(run.run_id)
            self.assertEqual(repository.get_run(run.run_id).verdict, FinalVerdict.SUCCESS)
            artifacts = repository.list_project_artifacts(run.run_id)
            self.assertEqual(len(artifacts), 6)
            for artifact in artifacts:
                self.assertTrue(artifact.a2a_task_id.startswith(" "))
                self.assertTrue(artifact.a2a_task_id.endswith(" "))
                self.assertTrue(artifact.a2a_artifact_id.startswith(" "))
                self.assertTrue(artifact.a2a_artifact_id.endswith(" "))
            for saved_step in repository.list_steps(run.run_id):
                self.assertTrue(saved_step.a2a_task_id.startswith(" "))
                self.assertTrue(saved_step.a2a_task_id.endswith(" "))
