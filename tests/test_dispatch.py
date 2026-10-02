import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from a2a.types import Task
from google.protobuf.json_format import ParseDict

from orchestrator.a2a import A2AAgentRegistry, AgentNotConfiguredError
from orchestrator.application import PlannerRunDispatcher, TaskRunDisposition
from orchestrator.core.config import Settings
from orchestrator.domain import (
    AgentRole,
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
        include_developer_artifacts: bool = True,
        build_exit_code: int = 0,
        mismatch_build_manifest: bool = False,
        qa_outcome: str = "PASS",
        security_outcome: str = "PASS",
        security_findings: list[dict[str, object]] | None = None,
        include_validation_artifacts: bool = True,
    ) -> None:
        self.state = state
        self.include_artifact = include_artifact
        self.include_developer_artifacts = include_developer_artifacts
        self.build_exit_code = build_exit_code
        self.mismatch_build_manifest = mismatch_build_manifest
        self.qa_outcome = qa_outcome
        self.security_outcome = security_outcome
        self.security_findings = security_findings or []
        self.include_validation_artifacts = include_validation_artifacts
        self.sent: list[tuple[dict[str, object], object, str | None]] = []
        self.handoffs: list[tuple[object, object, str]] = []
        self.closed = False
        self.card_resolved = False
        self.requirement_id = uuid4()
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
                                    "requirements": [
                                        {
                                            "requirementId": str(self.requirement_id),
                                            "key": "REQ-001",
                                            "description": "가입 요청을 처리한다.",
                                            "acceptanceCriteria": [
                                                "유효한 요청은 계정을 생성한다."
                                            ],
                                        }
                                    ],
                                    "implementationPlan": [
                                        {
                                            "taskId": "TASK-001",
                                            "title": "가입 API 구현",
                                            "description": (
                                                "검증 기준에 맞춰 가입 API를 만든다."
                                            ),
                                            "requirementIds": [str(self.requirement_id)],
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
                "id": "developer-task-opaque",
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
        manifest = {
            "repositoryId": "a2a-agent-company",
            "codeVersion": code_version,
            "projectArtifactId": str(self.source_artifact_id),
            "commitHash": "a" * 40,
            "gitObjectFormat": "sha1",
            "treeHash": "b" * 40,
            "snapshotSha256": "c" * 64,
            "containerImageDigest": "sha256:" + "d" * 64,
            "dependencyLockHash": "sha256:" + "e" * 64,
        }
        build_manifest = dict(manifest)
        if self.mismatch_build_manifest:
            build_manifest["treeHash"] = "f" * 40
        requirement_ids = [str(item) for item in metadata.requirement_ids]
        values = [
            (
                "source-snapshot.json", "developer-source-a2a", self.source_artifact_id,
                {
                    "artifactId": str(self.source_artifact_id), "artifactType": "SOURCE",
                    "artifactVersion": 1, "previousArtifactId": None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": "developer-task-opaque", "a2aArtifactId": "developer-source-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version, "repositoryId": "a2a-agent-company",
                    "commitHash": "a" * 40, "gitObjectFormat": "sha1", "treeHash": "b" * 40,
                    "snapshotSha256": "c" * 64,
                    "artifactUri": f"registry://source/{self.source_artifact_id}/v1",
                    "containerImageDigest": "sha256:" + "d" * 64,
                    "dependencyLockHash": "sha256:" + "e" * 64, "createdAt": now,
                },
            ),
            (
                "change-report.json", "developer-change-a2a", self.change_artifact_id,
                {
                    "artifactId": str(self.change_artifact_id), "artifactType": "CHANGE_REPORT",
                    "artifactVersion": 1, "previousArtifactId": None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": "developer-task-opaque", "a2aArtifactId": "developer-change-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version,
                    "summary": "가입 API와 입력 검증을 구현했다.",
                    "fileChanges": [{"path": "src/signup.py", "action": "ADDED"}],
                    "createdAt": now,
                },
            ),
            (
                "build-report.json", "developer-build-a2a", self.build_artifact_id,
                {
                    "artifactId": str(self.build_artifact_id), "artifactType": "BUILD_REPORT",
                    "artifactVersion": 1, "previousArtifactId": None,
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "a2aTaskId": "developer-task-opaque", "a2aArtifactId": "developer-build-a2a",
                    "createdBy": "DEVELOPER", "requirementIds": requirement_ids,
                    "codeVersion": code_version,
                    "sourceArtifactId": str(self.source_artifact_id),
                    "exitCode": self.build_exit_code, "durationMs": 1200,
                    "executionManifestId": str(uuid4()), "executionManifest": build_manifest,
                    "stdoutRef": None, "stderrRef": None, "createdAt": now,
                },
            ),
        ]
        return [
            {
                "artifactId": a2a_artifact_id, "name": name,
                "parts": [{"data": data, "mediaType": "application/json"}],
                "metadata": {
                    "runId": str(metadata.run_id),
                    "workflowStepId": str(metadata.workflow_step_id),
                    "projectArtifactId": str(project_artifact_id), "artifactVersion": 1,
                },
            }
            for name, a2a_artifact_id, project_artifact_id, data in values
        ]

    async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
        self.handoffs.append((handoff, recipient, request_text))
        task_id = f"{recipient.value.lower()}-task-opaque"
        a2a_artifact_id = f"{recipient.value.lower()}-report-a2a"
        project_artifact_id = uuid4()
        requirement_ids = [str(value) for value in kwargs["requirement_ids"]]
        outcome = self.qa_outcome if recipient == AgentRole.QA else self.security_outcome
        common = {
            "artifactId": str(project_artifact_id),
            "artifactType": "QA_REPORT" if recipient == AgentRole.QA else "SECURITY_REPORT",
            "artifactVersion": 1,
            "previousArtifactId": None,
            "runId": str(handoff.run_id),
            "workflowStepId": str(kwargs["workflow_step_id"]),
            "a2aTaskId": task_id,
            "a2aArtifactId": a2a_artifact_id,
            "createdBy": recipient.value,
            "requirementIds": requirement_ids,
            "codeVersion": handoff.execution_manifest.code_version,
            "executionManifest": handoff.execution_manifest.model_dump(
                mode="json", by_alias=True
            ),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        if recipient == AgentRole.QA:
            common["tests"] = [
                {
                    "testId": "QA-001",
                    "requirementId": requirement_ids[0],
                    "outcome": outcome,
                    "title": "요구사항 수락 테스트",
                }
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
                        "artifactVersion": 1,
                    },
                }
            ]
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
            scenario_id=uuid4(),
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
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.code_version, 1)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)
        self.assertEqual(planner_step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(planner_step.a2a_task_id, "planner-task-opaque")
        self.assertEqual(planner_step.output_artifact_ids, [client.project_artifact_id])
        self.assertEqual(developer_step.status, WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(developer_step.a2a_task_id, "developer-task-opaque")
        self.assertEqual(developer_step.code_version, 1)
        self.assertEqual(len(developer_step.output_artifact_ids), 3)
        self.assertEqual(developer_step.requirement_ids, [client.requirement_id])
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
        self.assertEqual(client.sent[0][0], {"request": self.run.request_text})
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
        self.assertEqual(developer_metadata.requirement_ids, (client.requirement_id,))
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
        self.assertTrue(all("REQ-001" in handoff[2] for handoff in client.handoffs))
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
        self.assertIn("VALIDATION_DECISION_HUMAN_REVIEW", [event.event_type for event in events])
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

    async def test_build_failure_is_recorded_without_qa_or_security_dispatch(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", build_exit_code=1)

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        steps = self.repository.list_steps(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.FIX_REQUIRED)
        self.assertEqual(run.code_version, 1)
        self.assertEqual(len(self.repository.list_project_artifacts(self.run.run_id)), 3)
        self.assertFalse(
            any(step.agent_role in (AgentRole.QA, AgentRole.SECURITY) for step in steps)
        )
        self.assertFalse(client.handoffs)
        self.assertIn("BUILD_FAILED", [event.event_type for event in events])

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

    async def test_validation_failure_is_recorded_as_fix_required_not_final_fail(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", qa_outcome="FAIL")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
        self.assertEqual(run.status, WorkflowStatus.FIX_REQUIRED)
        self.assertIsNone(run.verdict)
        self.assertEqual(len(self.repository.list_project_artifacts(self.run.run_id)), 5)
        self.assertIn("VALIDATION_DECISION_FIX_REQUIRED", [e.event_type for e in events])

    async def test_unverified_validation_requires_review_not_product_failure(self) -> None:
        client = FakePlannerClient("TASK_STATE_COMPLETED", security_outcome="UNVERIFIED")

        await self.dispatcher_for(client).dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(run.verdict.value, "HUMAN_REVIEW")
        self.assertEqual(run.resume_state, WorkflowStatus.VALIDATING)

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
        self.assertEqual(run.status, WorkflowStatus.FIX_REQUIRED)
        self.assertIsNone(run.verdict)

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
        events, _ = self.repository.list_events(self.run.run_id, limit=50, offset=0)
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
