import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.a2a import A2AAgentRegistry, AgentNotConfiguredError
from orchestrator.application import PlannerRunDispatcher, TaskRunDisposition
from orchestrator.core.config import Settings
from orchestrator.domain import (
    AgentRole,
    SCN_001_ID,
    SCENARIO_REGISTRY,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository


class FakePlannerClient:
    def __init__(
        self,
        state: str,
        *,
        include_artifact: bool = True,
        weaken_planner_criteria: bool = False,
        include_developer_artifacts: bool = True,
        build_exit_code: int = 0,
        mismatch_build_manifest: bool = False,
        qa_outcome: str = "PASS",
        qa_outcome_after_fix: str | None = None,
        change_qa_issue_identity_by_version: bool = False,
        security_outcome: str = "PASS",
        build_exit_code_after_fix: int | None = None,
        security_findings: list[dict[str, object]] | None = None,
        include_validation_artifacts: bool = True,
    ) -> None:
        self.state = state
        self.include_artifact = include_artifact
        self.weaken_planner_criteria = weaken_planner_criteria
        self.include_developer_artifacts = include_developer_artifacts
        self.build_exit_code = build_exit_code
        self.mismatch_build_manifest = mismatch_build_manifest
        self.qa_outcome = qa_outcome
        self.qa_outcome_after_fix = qa_outcome_after_fix
        self.change_qa_issue_identity_by_version = change_qa_issue_identity_by_version
        self.security_outcome = security_outcome
        self.build_exit_code_after_fix = build_exit_code_after_fix
        self.security_findings = security_findings or []
        self.include_validation_artifacts = include_validation_artifacts
        self.sent: list[tuple[dict[str, object], object, str | None]] = []
        self.handoffs: list[tuple[object, object, str]] = []
        self.closed = False
        self.card_resolved = False
        self.requirements = [
            {
                "requirementId": item["requirementId"],
                "key": item["key"],
                "description": item["description"],
                "acceptanceCriteria": item["acceptanceCriteria"],
            }
            for item in SCENARIO_REGISTRY[SCN_001_ID].planner_contract()["requirements"]
        ]
        if self.weaken_planner_criteria:
            self.requirements[0]["acceptanceCriteria"] = ["요구사항을 일부만 확인"]
        self.requirement_id = UUID(self.requirements[0]["requirementId"])
        self.previous_developer_artifacts: dict[str, object] = {}
        self.previous_validation_artifacts: dict[AgentRole, object] = {}
        self.project_artifact_id = uuid4()
        self.source_artifact_id = uuid4()
        self.change_artifact_id = uuid4()
        self.build_artifact_id = uuid4()

    async def __aenter__(self) -> "FakePlannerClient":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    async def resolve_agent_card(self) -> None:
        self.card_resolved = True

    async def send_task(self, payload, metadata, *, context_id=None):
        self.sent.append((dict(payload), metadata, context_id))
        if "request" in payload:
            if isinstance(self.state, Exception):
                raise self.state
            response = {
                "id": "planner-task-opaque",
                "contextId": "planner-context-opaque",
                "status": {"state": self.state},
            }
            if self.include_artifact:
                response["artifacts"] = [
                    {
                        "artifactId": "planner-artifact-opaque",
                        "name": "requirements.json",
                        "parts": [
                            {
                                "data": {
                                    "schemaVersion": 1,
                                    "requirements": self.requirements,
                                    "implementationPlan": [
                                        {
                                            "taskId": "TASK-001",
                                            "title": "가입 API 구현",
                                            "description": (
                                                "검증 기준에 맞춰 가입 API를 만든다."
                                            ),
                                            "requirementIds": [
                                                item["requirementId"] for item in self.requirements
                                            ],
                                            "dependsOn": [],
                                        }
                                    ],
                                },
                                "mediaType": "application/json",
                            }
                        ],
                        "metadata": {
                            "runId": str(metadata.run_id),
                            "workflowStepId": str(metadata.workflow_step_id),
                            "projectArtifactId": str(self.project_artifact_id),
                            "artifactVersion": 1,
                        },
                    }
                ]
        elif "plan" in payload:
            response = {
                "id": (
                    "developer-task-opaque"
                    if (metadata.code_version or 1) == 1
                    else f"developer-task-v{metadata.code_version}-opaque"
                ),
                "contextId": "developer-context-opaque",
                "status": {"state": "TASK_STATE_COMPLETED"},
            }
            if self.include_developer_artifacts:
                response["artifacts"] = self._developer_artifacts(metadata)
        else:
            raise AssertionError(f"unexpected A2A payload: {payload}")
        return ParseDict(
            response,
            Task(),
        )

    async def get_task(self, task_id: str) -> Task:
        raise AssertionError("terminal/interrupted submission must not be polled")

    def _developer_artifacts(self, metadata):
        now = datetime.now(timezone.utc).isoformat()
        code_version = metadata.code_version or 1
        task_id = (
            "developer-task-opaque"
            if code_version == 1
            else f"developer-task-v{code_version}-opaque"
        )
        source_id, change_id, build_id = uuid4(), uuid4(), uuid4()
        previous = self.previous_developer_artifacts
        exit_code = (
            self.build_exit_code_after_fix
            if code_version > 1 and self.build_exit_code_after_fix is not None
            else self.build_exit_code
        )
        commit_hash = chr(96 + code_version) * 40
        tree_hash = chr(97 + code_version) * 40
        snapshot_hash = chr(98 + code_version) * 64
        manifest = {
            "repositoryId": "a2a-agent-company",
            "codeVersion": code_version,
            "projectArtifactId": str(source_id),
            "commitHash": commit_hash,
            "gitObjectFormat": "sha1",
            "treeHash": tree_hash,
            "snapshotSha256": snapshot_hash,
            "containerImageDigest": "sha256:" + "d" * 64,
            "dependencyLockHash": "sha256:" + "e" * 64,
        }
        build_manifest = dict(manifest)
        if self.mismatch_build_manifest:
            build_manifest["treeHash"] = "f" * 40
        requirement_ids = [str(item) for item in metadata.requirement_ids]
        values = [
            (
                "source-snapshot.json", f"developer-source-v{code_version}-a2a", source_id,
                {
                    "artifactId": str(source_id), "artifactType": "SOURCE",
                    "artifactVersion": code_version,
                    "previousArtifactId": str(previous["SOURCE"]) if code_version > 1 else None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": task_id,
                    "a2aArtifactId": f"developer-source-v{code_version}-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version, "repositoryId": "a2a-agent-company",
                    "commitHash": commit_hash, "gitObjectFormat": "sha1", "treeHash": tree_hash,
                    "snapshotSha256": snapshot_hash,
                    "artifactUri": f"registry://source/{source_id}/v{code_version}",
                    "containerImageDigest": "sha256:" + "d" * 64,
                    "dependencyLockHash": "sha256:" + "e" * 64, "createdAt": now,
                },
            ),
            (
                "change-report.json", f"developer-change-v{code_version}-a2a", change_id,
                {
                    "artifactId": str(change_id), "artifactType": "CHANGE_REPORT",
                    "artifactVersion": code_version,
                    "previousArtifactId": str(previous["CHANGE_REPORT"]) if code_version > 1 else None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": task_id,
                    "a2aArtifactId": f"developer-change-v{code_version}-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version,
                    "summary": "가입 API와 입력 검증을 구현했다.",
                    "fileChanges": [{"path": "src/signup.py", "action": "ADDED"}],
                    "createdAt": now,
                },
            ),
            (
                "build-report.json", f"developer-build-v{code_version}-a2a", build_id,
                {
                    "artifactId": str(build_id), "artifactType": "BUILD_REPORT",
                    "artifactVersion": code_version,
                    "previousArtifactId": str(previous["BUILD_REPORT"]) if code_version > 1 else None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": task_id,
                    "a2aArtifactId": f"developer-build-v{code_version}-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version,
                    "sourceArtifactId": str(source_id),
                    "exitCode": exit_code, "durationMs": 1200,
                    "executionManifestId": str(uuid4()), "executionManifest": build_manifest,
                    "stdoutRef": None, "stderrRef": None, "createdAt": now,
                },
            ),
        ]
        self.previous_developer_artifacts = {
            "SOURCE": source_id,
            "CHANGE_REPORT": change_id,
            "BUILD_REPORT": build_id,
        }
        self.source_artifact_id = source_id
        self.change_artifact_id = change_id
        self.build_artifact_id = build_id
        return [
            {
                "artifactId": a2a_artifact_id, "name": name,
                "parts": [{"data": data, "mediaType": "application/json"}],
                "metadata": {
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "projectArtifactId": str(project_artifact_id), "artifactVersion": code_version,
                },
            }
            for name, a2a_artifact_id, project_artifact_id, data in values
        ]
    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        self.handoffs.append((handoff, recipient, request_text))
        code_version = handoff.execution_manifest.code_version
        task_id = f"{recipient.value.lower()}-task-v{code_version}-opaque"
        a2a_artifact_id = f"{recipient.value.lower()}-report-v{code_version}-a2a"
        project_artifact_id = uuid4()
        previous_report = self.previous_validation_artifacts.get(recipient)
        requirement_ids = [str(value) for value in kwargs["requirement_ids"]]
        outcome = (
            self.qa_outcome_after_fix
            if recipient == AgentRole.QA
            and code_version > 1
            and self.qa_outcome_after_fix is not None
            else self.qa_outcome if recipient == AgentRole.QA else self.security_outcome
        )
        common = {
            "artifactId": str(project_artifact_id),
            "artifactType": "QA_REPORT" if recipient == AgentRole.QA else "SECURITY_REPORT",
            "artifactVersion": code_version,
            "previousArtifactId": str(previous_report) if previous_report is not None else None,
            "runId": str(handoff.run_id),
            "workflowStepId": str(kwargs["workflow_step_id"]),
            "a2aTaskId": task_id,
            "a2aArtifactId": a2a_artifact_id,
            "createdBy": recipient.value,
            "requirementIds": requirement_ids,
            "codeVersion": code_version,
            "executionManifest": handoff.execution_manifest.model_dump(
                mode="json", by_alias=True
            ),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        if recipient == AgentRole.QA:
            common["tests"] = [
                {
                    "testId": (
                        f"QA-v{code_version}-{index + 1:03}"
                        if self.change_qa_issue_identity_by_version
                        else f"QA-{index + 1:03}"
                    ),
                    "requirementId": requirement_id,
                    "outcome": outcome,
                    "title": "요구사항 수락 테스트",
                }
                for index, requirement_id in enumerate(requirement_ids)
            ]
            name = "qa-report.json"
        else:
            common["requirementResults"] = [
                {"requirementId": requirement_id, "outcome": outcome}
                for requirement_id in requirement_ids
            ]
            common["findings"] = self.security_findings
            name = "security-report.json"
        artifacts = []
        if self.include_validation_artifacts:
            artifacts = [
                {
                    "artifactId": a2a_artifact_id,
                    "name": name,
                    "parts": [{"data": common, "mediaType": "application/json"}],
                    "metadata": {
                        "runId": str(handoff.run_id),
                        "workflowStepId": str(kwargs["workflow_step_id"]),
                        "projectArtifactId": str(project_artifact_id),
                        "artifactVersion": code_version,
                    },
                }
            ]
            self.previous_validation_artifacts[recipient] = project_artifact_id
        return ParseDict(
            {
                "id": task_id,
                "contextId": f"{recipient.value.lower()}-context-opaque",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": artifacts,
            },
            Task(),
        )


class PlannerDispatchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.repository = SQLiteWorkflowRepository(
            Path(self.temp_dir.name) / "dispatch.sqlite3"
        )
        self.run = WorkflowRun(
            scenario_id=SCN_001_ID,
            request_text="회원가입 기능을 계획해줘.",
        )
        self.step = WorkflowStep(
            run_id=self.run.run_id,
            agent_role=AgentRole.PLANNER,
        )
        self.repository.create_run(
            self.run,
            (self.step,),
            (
                TraceEvent(
                    run_id=self.run.run_id,
                    event_type="RUN_STARTED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=self.run.status,
                ),
                TraceEvent(
                    run_id=self.run.run_id,
                    workflow_step_id=self.step.workflow_step_id,
                    event_type="WORKFLOW_STEP_CREATED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=self.run.status,
                ),
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def dispatcher_for(
        self,
        client: FakePlannerClient,
        *,
        developer_configured: bool = True,
        validators_configured: bool = True,
    ) -> PlannerRunDispatcher:
        return PlannerRunDispatcher(
            self.repository,
            A2AAgentRegistry(
                {
                    AgentRole.PLANNER: "http://planner.test",
                    AgentRole.DEVELOPER: (
                        "http://developer.test" if developer_configured else None
                    ),
                    AgentRole.QA: "http://qa.test" if validators_configured else None,
                    AgentRole.SECURITY: (
                        "http://security.test" if validators_configured else None
                    ),
                }
            ),
            client_factory=lambda url: client,  # type: ignore[arg-type]
        )

    async def test_valid_planner_plan_creates_and_dispatches_developer_step(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        planner_step = next(step for step in steps if step.agent_role == AgentRole.PLANNER)
        developer_step = next(step for step in steps if step.agent_role == AgentRole.DEVELOPER)
        validation_steps = [
            step for step in steps if step.agent_role in (AgentRole.QA, AgentRole.SECURITY)
        ]
        contexts = {
            context.agent_id: context
            for context in self.repository.list_agent_contexts(self.run.run_id)
        }
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.FINISHED)
        self.assertEqual(run.code_version, 1)
        self.assertEqual(run.verdict.value, "SUCCESS")
        self.assertIsNone(run.resume_state)
        self.assertEqual(planner_step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(planner_step.a2a_task_id, "planner-task-opaque")
        self.assertEqual(planner_step.output_artifact_ids, [client.project_artifact_id])
        self.assertEqual(developer_step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(developer_step.a2a_task_id, "developer-task-opaque")
        self.assertEqual(developer_step.code_version, 1)
        self.assertEqual(len(developer_step.output_artifact_ids), 3)
        self.assertEqual(
            set(developer_step.requirement_ids),
            {UUID(item["requirementId"]) for item in client.requirements},
        )
        self.assertEqual(developer_step.input_artifact_ids, [client.project_artifact_id])
        self.assertEqual(contexts["planner"].latest_a2a_task_id, planner_step.a2a_task_id)
        self.assertEqual(contexts["developer"].latest_a2a_task_id, developer_step.a2a_task_id)
        self.assertEqual(
            {step.agent_role for step in validation_steps},
            {AgentRole.QA, AgentRole.SECURITY},
        )
        self.assertTrue(
            all(step.status == WorkflowStepStatus.SUCCEEDED for step in validation_steps)
        )
        self.assertTrue(all(step.code_version == 1 for step in validation_steps))
        self.assertTrue(
            all(
                step.input_artifact_ids == [client.source_artifact_id]
                for step in validation_steps
            )
        )
        self.assertNotEqual(
            contexts["planner"].agent_context_id,
            contexts["developer"].agent_context_id,
        )
        self.assertEqual(
            len({context.agent_context_id for context in contexts.values()}), 4
        )
        self.assertEqual(client.sent[0][0]["request"], self.run.request_text)
        self.assertEqual(
            client.sent[0][0]["scenarioContract"]["scenarioKey"], "SCN-001"
        )
        self.assertIsNone(client.sent[0][2])
        developer_payload, developer_metadata, developer_context_id = client.sent[1]
        self.assertEqual(developer_payload["plan"]["requirements"][0]["key"], "REQ-001")
        self.assertEqual(
            developer_payload["outputContract"]["requiredArtifactNames"],
            [
                "source-snapshot.json",
                "change-report.json",
                "build-report.json",
            ],
        )
        self.assertEqual(
            developer_payload["sourceArtifact"]["projectArtifactId"],
            str(client.project_artifact_id),
        )
        self.assertEqual(
            set(developer_metadata.requirement_ids),
            {UUID(item["requirementId"]) for item in client.requirements},
        )
        self.assertEqual(developer_metadata.code_version, 1)
        self.assertEqual(
            developer_metadata.project_artifact_ids, (client.project_artifact_id,)
        )
        self.assertIsNone(developer_context_id)
        self.assertEqual(len(client.handoffs), 2)
        self.assertEqual(
            client.handoffs[0][0].execution_manifest,
            client.handoffs[1][0].execution_manifest,
        )
        self.assertEqual(
            {handoff[1] for handoff in client.handoffs},
            {AgentRole.QA, AgentRole.SECURITY},
        )
        self.assertTrue(
            all(
                {grant.access for grant in handoff[0].grants} == {"READ_ONLY"}
                for handoff in client.handoffs
            )
        )
        handoff_text = {role: text for _, role, text in client.handoffs}
        self.assertIn("REQ-001", handoff_text[AgentRole.QA])
        self.assertNotIn("REQ-001", handoff_text[AgentRole.SECURITY])
        self.assertIn("REQ-005", handoff_text[AgentRole.SECURITY])
        self.assertTrue(client.card_resolved)
        self.assertTrue(client.closed)
        self.assertIn("A2A_TASK_RECEIVED", [event.event_type for event in events])
        self.assertIn("PLANNER_OUTPUT_VALIDATED", [event.event_type for event in events])
        self.assertIn("WORKFLOW_STEP_DISPATCH_STARTED", [event.event_type for event in events])
        self.assertIn("DEVELOPER_ARTIFACTS_VALIDATED", [event.event_type for event in events])
        project_artifacts = self.repository.list_project_artifacts(self.run.run_id)
        self.assertEqual(len(project_artifacts), 5)
        self.assertEqual(
            {artifact.artifact_type for artifact in project_artifacts},
            {"SOURCE", "CHANGE_REPORT", "BUILD_REPORT", "QA_REPORT", "SECURITY_REPORT"},
        )
        self.assertIn("QA_REPORT_VALIDATED", [event.event_type for event in events])
        self.assertIn("SECURITY_REPORT_VALIDATED", [event.event_type for event in events])
        self.assertIn("VALIDATION_DECISION_FINISHED", [event.event_type for event in events])
        with sqlite3.connect(self.repository.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_artifacts SET artifact_version = 2 WHERE run_id = ?",
                    (str(self.run.run_id),),
                )

    async def test_invalid_developer_artifacts_are_rejected_without_registry_rows(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED", include_developer_artifacts=False
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.IMPLEMENTING)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        self.assertIn("DEVELOPER_OUTPUT_REJECTED", [event.event_type for event in events])
        self.assertFalse(client.handoffs)

    async def test_build_manifest_mismatch_is_rejected_before_artifact_registration(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED", mismatch_build_manifest=True
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(self.repository.list_project_artifacts(self.run.run_id), [])
        self.assertFalse(client.handoffs)

    async def test_repeated_build_issue_stops_the_fix_loop(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", build_exit_code=1)

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=200, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")
        self.assertEqual(run.fix_attempt, 2)
        self.assertEqual(run.code_version, 3)
        self.assertEqual(len(self.repository.list_project_artifacts(self.run.run_id)), 9)
        self.assertEqual(len(self.repository.list_issue_records(self.run.run_id)), 3)
        self.assertFalse(
            any(step.agent_role in (AgentRole.QA, AgentRole.SECURITY) for step in steps)
        )
        self.assertFalse(client.handoffs)
        self.assertIn("SAME_ISSUE_REQUIRES_REVIEW", [event.event_type for event in events])

    async def test_missing_validator_endpoint_preserves_candidate_for_review(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED")

        await self.dispatcher_for(
            client, validators_configured=False
        ).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        validation_steps = [
            step for step in steps if step.agent_role in (AgentRole.QA, AgentRole.SECURITY)
        ]
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)
        self.assertEqual(
            {step.agent_role for step in validation_steps},
            {AgentRole.QA, AgentRole.SECURITY},
        )
        self.assertTrue(
            all(step.status == WorkflowStepStatus.PENDING for step in validation_steps)
        )
        self.assertFalse(client.handoffs)

    async def test_repeated_validation_failure_stops_for_human_review(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", qa_outcome="FAIL")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=200, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.fix_attempt, 2)
        self.assertEqual(len(self.repository.list_issue_records(self.run.run_id)), 15)
        self.assertIn("SAME_ISSUE_REQUIRES_REVIEW", [e.event_type for e in events])

    async def test_new_issues_can_exhaust_the_three_fix_attempt_limit(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED",
            qa_outcome="FAIL",
            change_qa_issue_identity_by_version=True,
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.FINISHED)
        self.assertEqual(run.verdict.value, "FAIL")
        self.assertEqual(run.fix_attempt, 3)
        self.assertEqual(run.code_version, 4)
        self.assertEqual(len(self.repository.list_issue_records(self.run.run_id)), 15)

    async def test_unverified_validation_requires_review_not_product_failure(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", security_outcome="UNVERIFIED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)

    async def test_failed_qa_issue_is_fixed_and_revalidated_on_new_snapshot(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED",
            qa_outcome="FAIL",
            qa_outcome_after_fix="PASS",
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        artifacts = self.repository.list_project_artifacts(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=200, offset=0)
        self.assertEqual(run.status, WorkflowStatus.FINISHED)
        self.assertEqual(run.verdict.value, "SUCCESS")
        self.assertEqual(run.fix_attempt, 1)
        self.assertEqual(run.code_version, 2)
        self.assertEqual(
            [artifact.artifact_version for artifact in artifacts if artifact.artifact_type == "SOURCE"],
            [1, 2],
        )
        self.assertEqual(len(self.repository.list_issue_records(self.run.run_id)), 5)
        self.assertEqual(
            sum(event.event_type == "FIX_ATTEMPT_STARTED" for event in events), 1
        )
        self.assertIn("FIX_ATTEMPT_STARTED", [event.event_type for event in events])
        self.assertEqual(
            sum(step.agent_role == AgentRole.DEVELOPER for step in steps), 2
        )
        fix_payload = next(
            payload for payload, metadata, _ in client.sent
            if "fixRequest" in payload
        )
        self.assertEqual(fix_payload["fixRequest"]["attempt"], 1)
        self.assertEqual(fix_payload["fixRequest"]["issues"][0]["codeVersion"], 1)
        self.assertIn("REQ-001", fix_payload["plan"]["requirements"][0]["key"])
        self.assertNotIn("description", fix_payload["fixRequest"]["issues"][0])
        with sqlite3.connect(self.repository.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE issue_records SET fingerprint = ? WHERE run_id = ?",
                    ("0" * 64, str(self.run.run_id)),
                )

    async def test_confirmed_high_security_finding_requires_fix(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED",
            security_findings=[
                {
                    "findingId": "SEC-001",
                    "severity": "HIGH",
                    "disposition": "CONFIRMED",
                    "title": "권한 우회",
                    "description": "인증되지 않은 사용자가 보호된 기능에 접근한다.",
                }
            ],
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.fix_attempt, 2)
        self.assertEqual(len(self.repository.list_issue_records(self.run.run_id)), 3)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")

    async def test_confirmed_medium_security_finding_requires_policy_review(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED",
            security_findings=[
                {
                    "findingId": "SEC-002",
                    "severity": "MEDIUM",
                    "disposition": "CONFIRMED",
                    "title": "보안 정책 결정 필요",
                    "description": "팀의 중간 심각도 차단 정책이 확정되지 않았다.",
                }
            ],
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")

    async def test_missing_validation_report_is_rejected_and_not_registered(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED", include_validation_artifacts=False
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=200, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)
        self.assertEqual(len(self.repository.list_project_artifacts(self.run.run_id)), 3)
        self.assertIn("QA_SECURITY_OUTPUT_REJECTED", [e.event_type for e in events])

    async def test_planner_input_required_pauses_workflow(self) -> None:
        client = FakePlannerClient("TASK_STATE_INPUT_REQUIRED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        step = self.repository.list_steps(self.run.run_id)[0]
        self.assertEqual(run.status, WorkflowStatus.WAITING_INPUT)
        self.assertEqual(run.resume_state, WorkflowStatus.PLANNING)
        self.assertEqual(step.status, WorkflowStepStatus.WAITING_INPUT)

    async def test_invalid_planner_artifact_is_reviewed_without_creating_developer_step(
        self,
    ) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", include_artifact=False)

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.PLANNING)
        self.assertEqual(len(steps), 1)
        self.assertIn("PLANNER_OUTPUT_REJECTED", [event.event_type for event in events])
        self.assertEqual(len(client.sent), 1)

    async def test_planner_cannot_weaken_scenario_acceptance_criteria(self) -> None:
        client = FakePlannerClient(
            "TASK_STATE_COMPLETED", weaken_planner_criteria=True
        )

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(len(steps), 1)
        self.assertIn(
            "PLANNER_REQUIREMENTS_NOT_CANONICAL",
            [event.event_type for event in events],
        )

    async def test_missing_developer_endpoint_keeps_step_pending_for_review(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED")

        await self.dispatcher_for(client, developer_configured=False).dispatch_planner(
            self.run.run_id
        )

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        developer_step = next(step for step in steps if step.agent_role == AgentRole.DEVELOPER)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.IMPLEMENTING)
        self.assertEqual(developer_step.status, WorkflowStepStatus.PENDING)
        self.assertEqual(len(client.sent), 1)

    async def test_uncertain_send_failure_requires_review_without_retry(self) -> None:
        client = FakePlannerClient(httpx.ConnectError("connection interrupted"))

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        step = self.repository.list_steps(self.run.run_id)[0]
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.resume_state, WorkflowStatus.PLANNING)
        self.assertIsNone(run.verdict)
        self.assertEqual(step.status, WorkflowStepStatus.RUNNING)
        self.assertIsNone(step.a2a_task_id)
        self.assertEqual(len(client.sent), 1)
        self.assertIn(
            "A2A_DISPATCH_REQUIRES_REVIEW",
            [event.event_type for event in events],
        )


class AgentRegistryTests(unittest.TestCase):
    def test_settings_map_each_agent_role_to_its_own_url(self) -> None:
        registry = A2AAgentRegistry.from_settings(
            Settings(
                planner_agent_url="http://planner.test",
                developer_agent_url="http://developer.test",
                qa_agent_url="http://qa.test",
                security_agent_url="http://security.test",
            )
        )

        self.assertEqual(registry.require_base_url(AgentRole.PLANNER), "http://planner.test")
        self.assertEqual(registry.require_base_url(AgentRole.DEVELOPER), "http://developer.test")
        self.assertEqual(registry.require_base_url(AgentRole.QA), "http://qa.test")
        self.assertEqual(registry.require_base_url(AgentRole.SECURITY), "http://security.test")

    def test_missing_agent_url_is_reported_without_fallback(self) -> None:
        registry = A2AAgentRegistry({AgentRole.PLANNER: None})

        with self.assertRaises(AgentNotConfiguredError):
            registry.require_base_url(AgentRole.PLANNER)
        self.assertIsNone(registry.get_base_url(AgentRole.QA))


if __name__ == "__main__":
    unittest.main()
