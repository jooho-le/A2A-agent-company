"""Role-output contract tests with synthetic A2A Artifacts, not real Tool runs."""

import copy
import json
import traceback
import unittest
from pathlib import Path
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import MessageToDict, ParseDict
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

from agents.roles.outputs import RoleOutputContractError, validate_completed_role_output
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.application.developer_output import (
    ValidatedDeveloperOutput,
    parse_developer_output,
)
from orchestrator.application.planner_output import ValidatedPlannerOutput
from orchestrator.domain import (
    A2ATaskState,
    AgentRole,
    QAReportArtifact,
    SCENARIO_REGISTRY,
    SCN_001_ID,
    SecurityReportArtifact,
    SnapshotHandoff,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.domain.scenario_registry import RequirementValidator
from test_dispatch import FakePlannerClient


class AgentRoleOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.scenario = SCENARIO_REGISTRY[SCN_001_ID]
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 구현")
        step, task = await self.fixture(AgentRole.DEVELOPER)
        self.source = parse_developer_output(task, run=self.run, step=step).source

    async def fixture(self, role, **client_options):
        requirement_ids = list(self.scenario.requirement_ids)
        if role in (AgentRole.QA, AgentRole.SECURITY):
            validator = RequirementValidator.QA if role == AgentRole.QA else RequirementValidator.SECURITY
            requirement_ids = list(self.scenario.requirement_ids_for(validator))
        step = WorkflowStep(
            run_id=self.run.run_id,
            agent_role=role,
            status=WorkflowStepStatus.SUCCEEDED,
            a2a_task_state=A2ATaskState.COMPLETED,
            requirement_ids=requirement_ids,
            code_version=1,
            input_artifact_ids=[self.source.artifact_id] if role in (AgentRole.QA, AgentRole.SECURITY) else [],
        )
        client = FakePlannerClient("TASK_STATE_COMPLETED", **client_options)
        if role in (AgentRole.PLANNER, AgentRole.DEVELOPER):
            metadata = A2AWorkflowMetadata(
                run_id=self.run.run_id,
                workflow_step_id=step.workflow_step_id,
                scenario_id=self.run.scenario_id,
                requirement_ids=requirement_ids,
                attempt=0,
                code_version=1,
            )
            payload = {"request": "회원가입 구현"} if role == AgentRole.PLANNER else {"plan": {}}
            task = await client.send_task(payload, metadata)
        else:
            task = await client.send_snapshot_handoff(
                SnapshotHandoff.from_snapshot(self.source), role, "동일 Snapshot 검증",
                workflow_step_id=step.workflow_step_id,
                requirement_ids=requirement_ids,
            )
        step.a2a_task_id = task.id
        step.agent_context_id = task.context_id
        return step, task

    def validate(self, role, step, task, **overrides):
        arguments = {"run": self.run, "step": step, "task": task}
        if role == AgentRole.PLANNER:
            arguments["scenario"] = self.scenario
        elif role in (AgentRole.QA, AgentRole.SECURITY):
            arguments["source"] = self.source
        arguments.update(overrides)
        return validate_completed_role_output(role, **arguments)

    @staticmethod
    def wire(task):
        return copy.deepcopy(MessageToDict(task))

    @staticmethod
    def payload(wire, index=0):
        return wire["artifacts"][index]["parts"][0]["data"]

    def reject(self, role, step, wire, **overrides):
        with self.assertRaises(RoleOutputContractError):
            self.validate(role, step, ParseDict(wire, Task()), **overrides)

    def wire_with_metadata(self, step, task):
        wire = self.wire(task)
        wire["metadata"] = A2AWorkflowMetadata(
            run_id=self.run.run_id,
            workflow_step_id=step.workflow_step_id,
            scenario_id=self.run.scenario_id,
            attempt=step.attempt,
            requirement_ids=tuple(step.requirement_ids) or None,
            code_version=step.code_version,
            project_artifact_ids=tuple(step.input_artifact_ids) or None,
        ).to_a2a_json()
        return wire

    async def test_planner_output_preserves_the_protected_baseline(self):
        step, task = await self.fixture(AgentRole.PLANNER)
        result = self.validate(AgentRole.PLANNER, step, task)
        self.assertIsInstance(result, ValidatedPlannerOutput)
        self.assertEqual({item.requirement_id for item in result.plan.requirements}, set(self.scenario.requirement_ids))

    async def test_developer_output_has_three_cross_linked_artifacts(self):
        step, task = await self.fixture(AgentRole.DEVELOPER)
        result = self.validate(AgentRole.DEVELOPER, step, task)
        self.assertIsInstance(result, ValidatedDeveloperOutput)
        self.assertEqual(result.source.artifact_id, result.build_report.source_artifact_id)
        self.assertEqual(result.source.execution_manifest(), result.build_report.execution_manifest)

    async def test_qa_output_has_its_assigned_requirement_coverage(self):
        step, task = await self.fixture(AgentRole.QA)
        report = self.validate(AgentRole.QA, step, task)
        self.assertIsInstance(report, QAReportArtifact)
        self.assertEqual({test.requirement_id for test in report.tests}, set(step.requirement_ids))

    async def test_security_output_has_its_assigned_requirement_coverage(self):
        step, task = await self.fixture(AgentRole.SECURITY)
        report = self.validate(AgentRole.SECURITY, step, task)
        self.assertIsInstance(report, SecurityReportArtifact)
        self.assertEqual({result.requirement_id for result in report.requirement_results}, set(step.requirement_ids))

    async def test_role_must_match_the_step(self):
        step, task = await self.fixture(AgentRole.DEVELOPER)
        with self.assertRaises(RoleOutputContractError):
            self.validate(AgentRole.QA, step, task)

    async def test_step_must_belong_to_the_current_run(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            step.run_id = uuid4()
            with self.subTest(role=role), self.assertRaises(RoleOutputContractError):
                self.validate(role, step, task)

    async def test_step_and_recorded_a2a_state_must_both_be_completed(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            for changes in ({"status": WorkflowStepStatus.RUNNING}, {"a2a_task_state": A2ATaskState.WORKING}):
                changed = step.model_copy(update=changes)
                with self.subTest(role=role, changes=changes), self.assertRaises(RoleOutputContractError):
                    self.validate(role, changed, task)

    async def test_task_must_be_completed(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            wire = self.wire(task)
            wire["status"]["state"] = "TASK_STATE_WORKING"
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_task_id_must_match_its_step(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            wire = self.wire(task)
            wire["id"] = "another-opaque-task"
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_saved_context_id_must_match_the_task(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            wire = self.wire(task)
            wire["contextId"] = "another-opaque-context"
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_whitespace_only_task_and_context_ids_are_rejected(self):
        for field in ("id", "contextId"):
            step, task = await self.fixture(AgentRole.PLANNER)
            wire = self.wire(task)
            wire[field] = " \t "
            if field == "id":
                step.a2a_task_id = wire[field]
            else:
                step.agent_context_id = wire[field]
            with self.subTest(field=field):
                self.reject(AgentRole.PLANNER, step, wire)

    async def test_context_can_be_bound_when_not_previously_saved(self):
        step, task = await self.fixture(AgentRole.PLANNER)
        step.agent_context_id = None
        self.assertIsInstance(self.validate(AgentRole.PLANNER, step, task), ValidatedPlannerOutput)

    async def test_valid_opaque_task_context_and_artifact_ids_are_not_trimmed(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            wire = self.wire(task)
            wire["id"] = " " + wire["id"] + " "
            wire["contextId"] = " " + wire["contextId"] + " "
            step.a2a_task_id = wire["id"]
            step.agent_context_id = wire["contextId"]
            for artifact in wire["artifacts"]:
                artifact["artifactId"] = " " + artifact["artifactId"] + " "
                payload = artifact["parts"][0]["data"]
                if "a2aTaskId" in payload:
                    payload["a2aTaskId"] = wire["id"]
                    payload["a2aArtifactId"] = artifact["artifactId"]
            with self.subTest(role=role):
                report = self.validate(role, step, ParseDict(wire, Task()))
                first = report.source if role == AgentRole.DEVELOPER else report
                self.assertEqual(first.a2a_artifact_id, wire["artifacts"][0]["artifactId"])

    async def test_present_metadata_is_validated_with_protojson_integral_numbers(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            wire = self.wire_with_metadata(step, task)
            wire["metadata"]["attempt"] = float(step.attempt)
            wire["metadata"]["codeVersion"] = float(step.code_version)
            task = ParseDict(wire, Task())
            with self.subTest(role=role):
                self.assertIs(type(MessageToDict(task.metadata)["attempt"]), float)
                self.assertIsNotNone(self.validate(role, step, task))

    async def test_present_metadata_cannot_change_fixed_workflow_identity(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            base = self.wire_with_metadata(step, task)
            for field in ("runId", "workflowStepId", "scenarioId", "attempt"):
                wire = copy.deepcopy(base)
                wire["metadata"][field] = step.attempt + 1 if field == "attempt" else str(uuid4())
                with self.subTest(role=role, field=field):
                    self.reject(role, step, wire)

    async def test_present_metadata_cannot_change_requirement_snapshot_and_artifact_references(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            base = self.wire_with_metadata(step, task)
            for field, replacement in (
                ("requirementIds", [str(uuid4())]),
                ("codeVersion", step.code_version + 1),
                ("projectArtifactIds", [str(uuid4())]),
            ):
                wire = copy.deepcopy(base)
                wire["metadata"][field] = replacement
                with self.subTest(role=role, field=field):
                    self.reject(role, step, wire)

    async def test_present_metadata_cannot_omit_required_host_references(self):
        for role in AgentRole:
            step, task = await self.fixture(role)
            base = self.wire_with_metadata(step, task)
            for field in tuple(base["metadata"]):
                wire = copy.deepcopy(base)
                del wire["metadata"][field]
                with self.subTest(role=role, field=field):
                    self.reject(role, step, wire)

    async def test_present_metadata_rejects_bad_integer_types_uuid_duplicates_and_unknown_fields(self):
        step, task = await self.fixture(AgentRole.QA)
        base = self.wire_with_metadata(step, task)
        invalid_fields = [
            ("attempt", True), ("attempt", "0"), ("attempt", 0.5),
            ("codeVersion", True), ("codeVersion", "1"), ("codeVersion", 1.5),
            ("runId", "not-a-uuid"),
            ("requirementIds", [base["metadata"]["requirementIds"][0]] * 2),
            ("projectArtifactIds", [base["metadata"]["projectArtifactIds"][0]] * 2),
            ("unexpectedField", "test-only-value"),
        ]
        for field, invalid in invalid_fields:
            wire = copy.deepcopy(base)
            wire["metadata"][field] = invalid
            with self.subTest(field=field, invalid=invalid):
                self.reject(AgentRole.QA, step, wire)

    async def test_planner_requires_an_authoritative_scenario(self):
        step, task = await self.fixture(AgentRole.PLANNER)
        with self.assertRaises(RoleOutputContractError):
            self.validate(AgentRole.PLANNER, step, task, scenario=None)

    async def test_planner_scenario_must_match_the_run(self):
        step, task = await self.fixture(AgentRole.PLANNER)
        changed_run = self.run.model_copy(update={"scenario_id": uuid4()})
        with self.assertRaises(RoleOutputContractError):
            self.validate(AgentRole.PLANNER, step, task, run=changed_run)

    async def test_planner_cannot_weaken_acceptance_criteria(self):
        step, task = await self.fixture(AgentRole.PLANNER, weaken_planner_criteria=True)
        with self.assertRaises(RoleOutputContractError):
            self.validate(AgentRole.PLANNER, step, task)

    async def test_planner_cannot_omit_a_canonical_requirement(self):
        step, task = await self.fixture(AgentRole.PLANNER)
        wire = self.wire(task)
        plan = self.payload(wire)
        removed = plan["requirements"].pop()["requirementId"]
        plan["implementationPlan"][0]["requirementIds"].remove(removed)
        self.reject(AgentRole.PLANNER, step, wire)

    async def test_developer_build_pass_and_product_fail_require_tool_evidence(self):
        for exit_code in (0, 1):
            step, task = await self.fixture(AgentRole.DEVELOPER, build_exit_code=exit_code, include_tool_evidence=False)
            with self.subTest(exit_code=exit_code), self.assertRaises(RoleOutputContractError):
                self.validate(AgentRole.DEVELOPER, step, task)

    async def test_successful_build_tool_may_report_a_product_build_failure(self):
        step, task = await self.fixture(AgentRole.DEVELOPER, build_exit_code=1)
        output = self.validate(AgentRole.DEVELOPER, step, task)
        self.assertEqual(output.build_report.execution_outcome.value, "FAIL")
        self.assertEqual(output.build_report.tool_evidence.outcome.value, "PASS")

    async def test_developer_build_evidence_requires_the_correct_tool_and_manifest(self):
        for field, value in (("toolName", "run_unit_tests"), ("treeHash", "f" * 40)):
            step, task = await self.fixture(AgentRole.DEVELOPER)
            wire = self.wire(task)
            evidence = self.payload(wire, 2)["toolEvidence"]
            if field == "toolName":
                evidence[field] = value
            else:
                evidence["executionManifest"][field] = value
            with self.subTest(field=field):
                self.reject(AgentRole.DEVELOPER, step, wire)

    async def test_unverified_build_can_be_recorded_without_a_pass_claim(self):
        step, task = await self.fixture(AgentRole.DEVELOPER, build_infra_retries=0, include_tool_evidence=False)
        output = self.validate(AgentRole.DEVELOPER, step, task)
        self.assertEqual(output.build_report.execution_outcome.value, "UNVERIFIED")
        self.assertIsNone(output.build_report.tool_evidence)

    async def test_validation_roles_require_a_source_snapshot(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step, task = await self.fixture(role)
            with self.subTest(role=role), self.assertRaises(RoleOutputContractError):
                self.validate(role, step, task, source=None)

    async def test_validation_snapshot_must_belong_to_the_same_run(self):
        changed_source = self.source.model_copy(update={"run_id": uuid4()})
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step, task = await self.fixture(role)
            with self.subTest(role=role), self.assertRaises(RoleOutputContractError):
                self.validate(role, step, task, source=changed_source)

    async def test_validation_manifest_must_match_the_received_snapshot(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step, task = await self.fixture(role)
            wire = self.wire(task)
            self.payload(wire)["executionManifest"]["treeHash"] = "f" * 40
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_validation_report_cannot_omit_assigned_requirement_results(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step, task = await self.fixture(role)
            wire = self.wire(task)
            field = "tests" if role == AgentRole.QA else "requirementResults"
            self.payload(wire)[field].pop()
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_validation_pass_and_fail_both_require_tool_evidence(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            for outcome in ("PASS", "FAIL"):
                options = {"qa_outcome" if role == AgentRole.QA else "security_outcome": outcome}
                step, task = await self.fixture(role, include_tool_evidence=False, **options)
                with self.subTest(role=role, outcome=outcome), self.assertRaises(RoleOutputContractError):
                    self.validate(role, step, task)

    async def test_successful_validation_tools_can_report_product_failures(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            options = {"qa_outcome" if role == AgentRole.QA else "security_outcome": "FAIL"}
            step, task = await self.fixture(role, **options)
            report = self.validate(role, step, task)
            results = report.tests if role == AgentRole.QA else report.requirement_results
            with self.subTest(role=role):
                self.assertTrue(all(item.outcome.value == "FAIL" for item in results))
                self.assertTrue(all(item.tool_evidence.outcome.value == "PASS" for item in results))

    async def test_validation_tool_evidence_cannot_come_from_another_role(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            step, task = await self.fixture(role)
            wire = self.wire(task)
            field = "tests" if role == AgentRole.QA else "requirementResults"
            self.payload(wire)[field][0]["toolEvidence"]["toolName"] = "run_build"
            with self.subTest(role=role):
                self.reject(role, step, wire)

    async def test_unverified_validation_without_evidence_is_not_a_pass_claim(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            options = {"qa_outcome" if role == AgentRole.QA else "security_outcome": "UNVERIFIED"}
            step, task = await self.fixture(role, include_tool_evidence=False, **options)
            report = self.validate(role, step, task)
            results = report.tests if role == AgentRole.QA else report.requirement_results
            with self.subTest(role=role):
                self.assertTrue(all(item.outcome.value == "UNVERIFIED" for item in results))
                self.assertTrue(all(item.tool_evidence is None for item in results))

    async def test_errors_do_not_reveal_invalid_payload_or_nested_validation_exception(self):
        sentinel = "test-only-password-that-must-not-be-disclosed"
        step, task = await self.fixture(AgentRole.DEVELOPER)
        wire = self.wire(task)
        self.payload(wire)["password"] = sentinel
        try:
            self.validate(AgentRole.DEVELOPER, step, ParseDict(wire, Task()))
        except RoleOutputContractError as error:
            self.assertNotIn(sentinel, str(error))
            self.assertNotIn(sentinel, "".join(traceback.format_exception(error)))
            self.assertIsNone(error.__cause__)
            self.assertTrue(error.__suppress_context__)
        else:
            self.fail("Unexpected sensitive payload was accepted")

    async def test_synthetic_valid_outputs_match_the_existing_project_json_schemas(self):
        directory = Path(__file__).resolve().parents[1] / "schemas" / "project"
        resources = []
        for path in directory.glob("*.schema.json"):
            schema = json.loads(path.read_text())
            resource = Resource.from_contents(schema)
            resources.extend((
                (schema["$id"], resource),
                ("https://a2a-agent-company.local/schemas/project/" + path.name, resource),
            ))
        registry = Registry().with_resources(resources)
        by_name = {
            "requirements.json": "planner_output.schema.json",
            "source-snapshot.json": "developer_source_snapshot.schema.json",
            "change-report.json": "developer_change_report.schema.json",
            "build-report.json": "developer_build_report.schema.json",
            "qa-report.json": "qa_report.schema.json",
            "security-report.json": "security_report.schema.json",
        }
        for role in AgentRole:
            _, task = await self.fixture(role)
            for artifact in self.wire(task)["artifacts"]:
                schema = json.loads((directory / by_name[artifact["name"]]).read_text())
                with self.subTest(role=role, artifact=artifact["name"]):
                    Draft202012Validator(schema, registry=registry, format_checker=FormatChecker()).validate(artifact["parts"][0]["data"])


if __name__ == "__main__":
    unittest.main()
