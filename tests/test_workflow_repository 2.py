import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
    FinalVerdict,
)
from orchestrator.infrastructure import (
    ActiveAgentTaskError,
    RunDispatchConflict,
    SQLiteWorkflowRepository,
)


class SQLiteWorkflowRepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "workflow.sqlite3"
        self.repository = SQLiteWorkflowRepository(self.database_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def create_bundle(self, *, status: WorkflowStatus = WorkflowStatus.RECEIVED):
        run = WorkflowRun(
            scenario_id=uuid4(),
            request_text="Implement a feature",
            status=status,
        )
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        events = (
            TraceEvent(
                run_id=run.run_id,
                event_type="RUN_STARTED",
                actor="Orchestrator",
                attempt=0,
                workflow_state=run.status,
            ),
            TraceEvent(
                run_id=run.run_id,
                workflow_step_id=step.workflow_step_id,
                event_type="WORKFLOW_STEP_CREATED",
                actor="Orchestrator",
                attempt=0,
                workflow_state=run.status,
            ),
        )
        self.repository.create_run(run, (step,), events)
        return run, step, events

    def test_run_step_and_trace_survive_repository_reopen_in_order(self) -> None:
        run, step, events = self.create_bundle()

        reopened = SQLiteWorkflowRepository(self.database_path)

        self.assertEqual(reopened.get_run(run.run_id), run)
        self.assertEqual(reopened.list_steps(run.run_id), [step])
        stored_events, total = reopened.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual([event.event_id for event in stored_events[:2]], [e.event_id for e in events])
        self.assertEqual(stored_events[-1].event_type, "ARTIFACT_REGISTERED")

    def test_failed_initial_trace_insert_rolls_back_entire_run_creation(self) -> None:
        run = WorkflowRun(scenario_id=uuid4(), request_text="Atomic create")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        duplicate_event_id = uuid4()
        events = (
            TraceEvent(
                event_id=duplicate_event_id,
                run_id=run.run_id,
                event_type="RUN_STARTED",
                actor="Orchestrator",
                attempt=0,
            ),
            TraceEvent(
                event_id=duplicate_event_id,
                run_id=run.run_id,
                workflow_step_id=step.workflow_step_id,
                event_type="WORKFLOW_STEP_CREATED",
                actor="Orchestrator",
                attempt=0,
            ),
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.create_run(run, (step,), events)

        self.assertIsNone(self.repository.get_run(run.run_id))
        self.assertEqual(self.repository.list_steps(run.run_id), [])

    def test_planner_dispatch_claim_is_atomic_and_one_shot(self) -> None:
        run, step, _ = self.create_bundle()

        claimed_run, claimed_step = self.repository.claim_planner_dispatch(run.run_id)

        self.assertEqual(claimed_run.status, WorkflowStatus.PLANNING)
        self.assertEqual(claimed_step.workflow_step_id, step.workflow_step_id)
        self.assertEqual(claimed_step.status, WorkflowStepStatus.RUNNING)
        self.assertEqual(self.repository.get_run(run.run_id), claimed_run)
        self.assertEqual(self.repository.list_steps(run.run_id), [claimed_step])
        with self.assertRaises(RunDispatchConflict):
            self.repository.claim_planner_dispatch(run.run_id)

    def test_task_step_context_and_trace_write_atomically(self) -> None:
        run, step, _ = self.create_bundle()
        context_id = "planner-context-opaque"
        task_id = "planner-task-opaque"
        updated_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": task_id,
                "a2a_task_state": A2ATaskState.WORKING,
                "agent_context_id": context_id,
            }
        )
        context = AgentContext(
            run_id=run.run_id,
            agent_id="planner-agent",
            agent_context_id=context_id,
            latest_a2a_task_id=task_id,
        )
        event = TraceEvent(
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            a2a_task_id=task_id,
            agent_context_id=context_id,
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
            a2a_task_state=A2ATaskState.WORKING,
            workflow_state=run.status,
        )

        asyncio.run(
            self.repository.task_update_observer(run)(updated_step, context, event)
        )

        self.assertGreater(self.repository.get_run(run.run_id).updated_at, run.updated_at)
        self.assertEqual(self.repository.list_steps(run.run_id), [updated_step])
        self.assertEqual(self.repository.list_agent_contexts(run.run_id), [context])
        stored_events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 4)
        self.assertEqual(stored_events[-1].event_id, event.event_id)

    def test_failed_trace_insert_rolls_back_step_and_agent_context_changes(self) -> None:
        run, step, events = self.create_bundle()
        context_id = "planner-context-opaque"
        task_id = "planner-task-opaque"
        updated_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": task_id,
                "a2a_task_state": A2ATaskState.WORKING,
                "agent_context_id": context_id,
            }
        )
        context = AgentContext(
            run_id=run.run_id,
            agent_id="planner-agent",
            agent_context_id=context_id,
            latest_a2a_task_id=task_id,
        )
        duplicate_event = TraceEvent(
            event_id=events[0].event_id,
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.repository.save_task_update(run, updated_step, context, duplicate_event)

        self.assertEqual(self.repository.get_run(run.run_id), run)
        self.assertEqual(self.repository.list_steps(run.run_id), [step])
        self.assertEqual(self.repository.list_agent_contexts(run.run_id), [])
        stored_events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 3)
        self.assertEqual([event.event_id for event in stored_events[:2]], [e.event_id for e in events])

    def test_cancel_aborts_run_cancels_pending_step_and_records_trace(self) -> None:
        run, step, _ = self.create_bundle()

        canceled_run = self.repository.cancel_run(run.run_id, "USER_CANCELLED")

        self.assertEqual(canceled_run.status, WorkflowStatus.ABORTED)
        self.assertEqual(canceled_run.termination_reason, "USER_CANCELLED")
        self.assertIsNone(canceled_run.verdict)
        self.assertEqual(
            self.repository.list_steps(run.run_id)[0].status,
            WorkflowStepStatus.CANCELED,
        )
        events, total = self.repository.list_events(run.run_id, limit=10, offset=0)
        self.assertEqual(total, 5)
        self.assertEqual(events[-2].event_type, "RUN_ABORTED")
        self.assertEqual(events[-1].event_type, "WORKFLOW_STEP_CANCELED")

    def test_active_a2a_task_is_not_falsely_marked_canceled(self) -> None:
        run, step, _ = self.create_bundle(status=WorkflowStatus.IMPLEMENTING)
        active_step = step.model_copy(
            update={
                "status": WorkflowStepStatus.RUNNING,
                "a2a_task_id": "developer-task",
                "a2a_task_state": A2ATaskState.WORKING,
            }
        )
        self.repository.save_run_update(
            run,
            (
                TraceEvent(
                    run_id=run.run_id,
                    workflow_step_id=step.workflow_step_id,
                    event_type="WORKFLOW_STEP_UPDATED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=run.status,
                ),
            ),
        )
        # Persist the active Step through the same validated Task-update path.
        active_context = AgentContext(
            run_id=run.run_id,
            agent_id="developer-agent",
            latest_a2a_task_id="developer-task",
        )
        task_event = TraceEvent(
            run_id=run.run_id,
            workflow_step_id=step.workflow_step_id,
            a2a_task_id="developer-task",
            event_type="A2A_TASK_STATE_CHANGED",
            actor="Orchestrator",
            attempt=0,
            a2a_task_state=A2ATaskState.WORKING,
            workflow_state=run.status,
        )
        self.repository.save_task_update(run, active_step, active_context, task_event)

        with self.assertRaises(ActiveAgentTaskError):
            self.repository.cancel_run(run.run_id, "USER_CANCELLED")

        self.assertEqual(self.repository.get_run(run.run_id).status, WorkflowStatus.IMPLEMENTING)
        self.assertEqual(self.repository.list_steps(run.run_id)[0].status, WorkflowStepStatus.RUNNING)

    def test_configuration_and_workspace_are_frozen_and_survive_reopen(self) -> None:
        run, _, _ = self.create_bundle()
        configuration = self.repository.get_run_configuration(run.run_id)
        workspace = self.repository.get_workspace(run.workspace_id)
        self.assertEqual(configuration.workspace_id, run.workspace_id)
        self.assertEqual(workspace.run_id, run.run_id)
        reopened = SQLiteWorkflowRepository(self.database_path)
        self.assertEqual(reopened.get_run_configuration(run.run_id), configuration)
        self.assertEqual(reopened.get_workspace(run.workspace_id), workspace)
        with sqlite3.connect(self.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM run_configurations WHERE run_id=?", (str(run.run_id),))
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE workspaces SET payload_json='{}' WHERE workspace_id=?", (str(run.workspace_id),))

    def test_legacy_workspace_backfill_is_stable_and_preserves_historical_payload(self) -> None:
        run = WorkflowRun(scenario_id=uuid4(), request_text="legacy password=old-private-value")
        payload = run.model_dump(mode="json", exclude={"workspace_id"})
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT INTO workflow_runs(run_id,status,created_at,updated_at,payload_json) VALUES (?,?,?,?,?)",
                (str(run.run_id), run.status.value, run.created_at.isoformat(),run.updated_at.isoformat(),json.dumps(payload)),
            )
        reopened = SQLiteWorkflowRepository(self.database_path)
        migrated = reopened.get_run(run.run_id)
        again = SQLiteWorkflowRepository(self.database_path).get_run(run.run_id)
        self.assertEqual(migrated.workspace_id, again.workspace_id)
        self.assertNotIn("old-private-value", migrated.request_text)
        with sqlite3.connect(self.database_path) as connection:
            original = json.loads(connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (str(run.run_id),)).fetchone()[0])
            self.assertEqual(original["request_text"], payload["request_text"])

    def test_control_claim_cannot_be_stolen_or_released_with_another_token(self) -> None:
        run, _, _ = self.create_bundle()
        token = self.repository.acquire_control(run.run_id)
        with self.assertRaises(RunDispatchConflict):
            self.repository.acquire_control(run.run_id)
        self.repository.release_control(run.run_id, "wrong-token")
        with self.assertRaises(RunDispatchConflict):
            self.repository.acquire_control(run.run_id)
        self.repository.release_control(run.run_id, token)
        replacement = self.repository.acquire_control(run.run_id)
        self.assertNotEqual(replacement, token)

    def test_legacy_missing_workspace_id_uses_existing_immutable_resources(self) -> None:
        run, _, _ = self.create_bundle()
        configuration = self.repository.get_run_configuration(run.run_id)
        workspace = self.repository.get_workspace(run.workspace_id)
        with sqlite3.connect(self.database_path) as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (str(run.run_id),)).fetchone()[0])
            del payload["workspace_id"]
            connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?", (json.dumps(payload), str(run.run_id)))
        reopened = SQLiteWorkflowRepository(self.database_path)
        self.assertEqual(reopened.get_run(run.run_id).workspace_id, run.workspace_id)
        self.assertEqual(reopened.get_run_configuration(run.run_id), configuration)
        self.assertEqual(reopened.get_workspace(run.workspace_id), workspace)

    def test_backfill_fails_closed_for_conflicting_immutable_workspace_identity(self) -> None:
        run, _, _ = self.create_bundle()
        with sqlite3.connect(self.database_path) as connection:
            payload = json.loads(connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?", (str(run.run_id),)).fetchone()[0])
            payload["workspace_id"] = str(uuid4())
            connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?", (json.dumps(payload), str(run.run_id)))
        with self.assertRaisesRegex(ValueError, "Historical Run Configuration"):
            SQLiteWorkflowRepository(self.database_path)
        # Immutable records remain untouched; the mismatch needs explicit reconciliation.
        self.assertEqual(self.repository.get_run_configuration(run.run_id).workspace_id, run.workspace_id)
        self.assertEqual(self.repository.get_workspace(run.workspace_id).run_id, run.run_id)

    def test_safe_resume_preserves_third_fix_and_completed_task(self) -> None:
        run = WorkflowRun(scenario_id=uuid4(), request_text="resume", status=WorkflowStatus.HUMAN_REVIEW,
                          resume_state=WorkflowStatus.FIXING,fix_attempt=3)
        step = WorkflowStep(run_id=run.run_id,agent_role=AgentRole.DEVELOPER,
                            status=WorkflowStepStatus.SUCCEEDED,a2a_task_id="completed-task",
                            a2a_task_state=A2ATaskState.COMPLETED)
        self.repository.create_run(run,(step,),())
        resumed, stored_step = self.repository.resume_run_with_step(run.run_id,step.workflow_step_id)
        self.assertEqual(resumed.fix_attempt,3)
        self.assertEqual(stored_step.status,WorkflowStepStatus.SUCCEEDED)
        self.assertEqual(stored_step.a2a_task_id,"completed-task")
        with self.assertRaises(RunDispatchConflict):
            self.repository.resume_run_with_step(run.run_id,step.workflow_step_id)

    def test_interrupted_remote_task_requires_confirmed_cancel(self) -> None:
        run=WorkflowRun(scenario_id=uuid4(),request_text="paused",status=WorkflowStatus.WAITING_INPUT,
                        resume_state=WorkflowStatus.PLANNING)
        step=WorkflowStep(run_id=run.run_id,agent_role=AgentRole.PLANNER,status=WorkflowStepStatus.WAITING_INPUT,
                          a2a_task_id="interrupted-task",a2a_task_state=A2ATaskState.INPUT_REQUIRED)
        self.repository.create_run(run,(step,),())
        with self.assertRaises(ActiveAgentTaskError):
            self.repository.cancel_run(run.run_id,"USER_CANCELLED")
        with self.assertRaises(ActiveAgentTaskError):
            self.repository.complete_remote_cancellation(run.run_id,"USER_CANCELLED",{})
        canceled=self.repository.complete_remote_cancellation(
            run.run_id,"USER_CANCELLED",{step.workflow_step_id:A2ATaskState.CANCELED})
        self.assertEqual(canceled.status,WorkflowStatus.ABORTED)
        self.assertIsNone(canceled.verdict)
        self.assertEqual(self.repository.list_steps(run.run_id)[0].a2a_task_state,A2ATaskState.CANCELED)

    def test_storage_redacts_free_text_and_keeps_opaque_agent_identifiers(self) -> None:
        run=WorkflowRun(scenario_id=uuid4(),request_text="signup password=private-value")
        step=WorkflowStep(run_id=run.run_id,agent_role=AgentRole.PLANNER,status=WorkflowStepStatus.PENDING,
                          a2a_task_id="opaque-password=server-owned",agent_context_id="opaque-context")
        event=TraceEvent(run_id=run.run_id,event_type="RUN_STARTED",actor="password=actor-private",attempt=0)
        self.repository.create_run(run,(step,),(event,))
        self.assertNotIn("private-value",self.repository.get_run(run.run_id).request_text)
        self.assertEqual(self.repository.list_steps(run.run_id)[0].a2a_task_id,step.a2a_task_id)
        events,_=self.repository.list_events(run.run_id,limit=10,offset=0)
        self.assertNotIn("actor-private",events[0].actor)
        with sqlite3.connect(self.database_path) as connection:
            raw=connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id=?",(str(run.run_id),)).fetchone()[0]
            self.assertNotIn("private-value",raw)

    def test_trace_is_append_only_and_aborted_run_rejects_late_task_updates(self) -> None:
        run,step,_=self.create_bundle()
        with sqlite3.connect(self.database_path) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM trace_events WHERE run_id=?",(str(run.run_id),))
        self.repository.cancel_run(run.run_id,"USER_CANCELLED")
        context=AgentContext(run_id=run.run_id,agent_id="planner")
        event=TraceEvent(run_id=run.run_id,workflow_step_id=step.workflow_step_id,event_type="A2A_MESSAGE_SENT",
                         actor="Orchestrator",attempt=0)
        with self.assertRaises(RunDispatchConflict):
            self.repository.save_task_update(run,step,context,event)
        self.assertEqual(self.repository.get_run(run.run_id).status,WorkflowStatus.ABORTED)

    def test_tool_attempt_ledger_is_idempotent_and_retains_retry_history(self) -> None:
        from orchestrator.domain.snapshot_handoff import ExecutionManifest
        from orchestrator.domain.tool_evidence import ToolExecutionEvidence
        run,step,_=self.create_bundle()
        manifest=ExecutionManifest(repository_id="demo",code_version=1,project_artifact_id=uuid4(),
            commit_hash="a"*40,git_object_format="sha1",tree_hash="b"*40,snapshot_sha256="c"*64,
            container_image_digest="sha256:"+"d"*64,dependency_lock_hash="sha256:"+"e"*64)
        evidence=ToolExecutionEvidence(tool_name="run_build",execution_id=uuid4(),execution_manifest=manifest,
            evidence_ref="artifact://build/attempts",attempts=[
                {"attempt":0,"outcome":"UNVERIFIED","errorKind":"RESOURCE_BUSY","evidenceRef":"artifact://build/0"},
                {"attempt":1,"outcome":"PASS","evidenceRef":"artifact://build/1","durationMs":12},
            ])
        self.repository.ingest_tool_evidence(run.run_id,step.workflow_step_id,(evidence,))
        self.repository.ingest_tool_evidence(run.run_id,step.workflow_step_id,(evidence,))
        self.assertEqual(len(self.repository.list_tool_attempts(run.run_id)),2)
        events,_=self.repository.list_events(run.run_id,limit=20,offset=0)
        self.assertEqual(sum(event.event_type=="MCP_TOOL_CALLED" for event in events),2)
        self.assertEqual(sum(event.event_type=="MCP_TOOL_FINISHED" for event in events),2)
        self.assertEqual([event.duration_ms for event in events if event.event_type=="MCP_TOOL_FINISHED"],[0,12])

    def test_confirmed_cancellation_cannot_be_overwritten_by_late_polling(self) -> None:
        run=WorkflowRun(scenario_id=uuid4(),request_text="remote task",status=WorkflowStatus.VALIDATING)
        step=WorkflowStep(run_id=run.run_id,agent_role=AgentRole.QA,
                          status=WorkflowStepStatus.CANCELED,a2a_task_state=A2ATaskState.CANCELED,
                          a2a_task_id="canceled-task",agent_context_id="qa-context")
        self.repository.create_run(run,(step,),())
        context=AgentContext(run_id=run.run_id,agent_id="qa",agent_context_id="qa-context",
                             latest_a2a_task_id="canceled-task")
        for state, status in ((A2ATaskState.WORKING,WorkflowStepStatus.RUNNING),
                              (A2ATaskState.COMPLETED,WorkflowStepStatus.SUCCEEDED)):
            update=step.model_copy(update={"a2a_task_state":state,"status":status})
            event=TraceEvent(run_id=run.run_id,workflow_step_id=step.workflow_step_id,
                             event_type="A2A_TASK_STATE_CHANGED",actor="Orchestrator",attempt=0)
            with self.assertRaises(RunDispatchConflict):
                self.repository.save_task_update(run,update,context,event)
        self.assertEqual(self.repository.list_steps(run.run_id)[0],step)

    def test_tool_attempt_uri_credentials_are_redacted_without_changing_routing_ids(self) -> None:
        from orchestrator.domain.snapshot_handoff import ExecutionManifest
        from orchestrator.domain.tool_evidence import ToolExecutionEvidence
        run, step, _ = self.create_bundle()
        manifest = ExecutionManifest(repository_id="demo", code_version=1, project_artifact_id=uuid4(),
            commit_hash="a" * 40, git_object_format="sha1", tree_hash="b" * 40,
            snapshot_sha256="c" * 64, container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash="sha256:" + "e" * 64)
        reference = "artifact://build/opaque-password=path/log?token=private-query&safe=a%2Fb"
        evidence = ToolExecutionEvidence(tool_name="run_build", execution_id=uuid4(),
            execution_manifest=manifest, evidence_ref=reference, attempts=[
                {"attempt": 0, "outcome": "PASS", "evidenceRef": reference},
            ])
        self.repository.ingest_tool_evidence(run.run_id, step.workflow_step_id, (evidence,))
        self.repository.ingest_tool_evidence(run.run_id, step.workflow_step_id, (evidence,))
        attempts = self.repository.list_tool_attempts(run.run_id)
        self.assertEqual(len(attempts), 1)
        sanitized = attempts[0]["toolEvidence"]["evidenceRef"]
        self.assertEqual(sanitized,
            "artifact://build/opaque-password=path/log?token=[REDACTED]&safe=a%2Fb")
        self.assertEqual(attempts[0]["toolEvidence"]["executionManifest"], manifest.model_dump(mode="json", by_alias=True))
        with sqlite3.connect(self.database_path) as connection:
            raw = connection.execute("SELECT payload_json FROM tool_attempts WHERE run_id=?", (str(run.run_id),)).fetchone()[0]
        self.assertNotIn("private-query", raw)

    def _run_fake_pipeline(self, client):
        from orchestrator.a2a import A2AAgentRegistry
        from orchestrator.application import PlannerRunDispatcher
        from orchestrator.domain import SCN_001_ID
        run = WorkflowRun(scenario_id=SCN_001_ID, request_text="회원가입 기능 구현")
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(run, (step,), ())
        dispatcher = PlannerRunDispatcher(self.repository,
            A2AAgentRegistry({role: f"http://{role.value.lower()}.test" for role in AgentRole}),
            client_factory=lambda url: client)
        asyncio.run(dispatcher.dispatch_planner(run.run_id))
        return self.repository.get_run(run.run_id)

    def test_reappearing_security_finding_uses_stable_rule_and_location_not_report_id(self) -> None:
        from tests.test_dispatch import FakePlannerClient

        class ChangingFindingClient(FakePlannerClient):
            async def send_snapshot_handoff(self, handoff, recipient, request_text, **kwargs):
                if recipient == AgentRole.SECURITY:
                    version = handoff.execution_manifest.code_version
                    self.security_findings = [{
                        "findingId": f"scan-{version}-random-id", "ruleId": "AUTH-BYPASS",
                        "normalizedLocation": "Src/Signup.py:20" if version == 1 else "src/signup.py:20",
                        "severity": "HIGH", "disposition": "CONFIRMED",
                        "title": "권한 우회", "description": "동일한 코드 위치에서 권한 검사가 누락됨",
                    }]
                return await super().send_snapshot_handoff(handoff, recipient, request_text, **kwargs)

        run = self._run_fake_pipeline(ChangingFindingClient("TASK_STATE_COMPLETED"))
        issues = sorted(self.repository.list_issue_records(run.run_id), key=lambda issue: issue.code_version)
        self.assertEqual(len(issues), 3)
        self.assertEqual(len({issue.fingerprint for issue in issues}), 1)
        self.assertEqual([issue.revalidation_result for issue in issues], ["FAIL", "FAIL", None])
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)

    def test_build_fix_resolution_is_recorded_before_qa_from_actual_build_evidence(self) -> None:
        from tests.test_dispatch import FakePlannerClient

        class UnverifiedFixBuildClient(FakePlannerClient):
            def _developer_artifacts(self, metadata):
                if (metadata.code_version or 1) > 1:
                    self.build_infra_retries = 0
                return super()._developer_artifacts(metadata)

        clients = {
            "PASS": FakePlannerClient("TASK_STATE_COMPLETED", build_exit_code=1, build_exit_code_after_fix=0),
            "FAIL": FakePlannerClient("TASK_STATE_COMPLETED", build_exit_code=1),
            "UNVERIFIED": UnverifiedFixBuildClient("TASK_STATE_COMPLETED", build_exit_code=1),
        }
        for outcome, client in clients.items():
            with self.subTest(outcome=outcome):
                run = self._run_fake_pipeline(client)
                original = next(issue for issue in self.repository.list_issue_records(run.run_id) if issue.code_version == 1)
                self.assertEqual(original.revalidation_result, outcome)
                if outcome != "PASS":
                    self.assertFalse(client.handoffs)
                with sqlite3.connect(self.database_path) as connection:
                    events = connection.execute("SELECT payload_json FROM issue_events WHERE issue_id=? AND event_type='REVALIDATION_FINISHED'", (str(original.issue_id),)).fetchall()
                self.assertEqual(len(events), 1)
                self.assertEqual(json.loads(events[0][0])["revalidation_result"], outcome)

    def test_project_artifact_cannot_reuse_configuration_uuid_and_creation_rolls_back(self) -> None:
        run = WorkflowRun(scenario_id=uuid4(), request_text="Artifact identity", status=WorkflowStatus.PLANNING)
        step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.PLANNER,
            status=WorkflowStepStatus.SUCCEEDED, a2a_task_state=A2ATaskState.COMPLETED,
            a2a_task_id="planner-task", a2a_artifact_ids=["requirements-a2a"])
        self.repository.create_run(run, (step,), ())
        configuration = self.repository.get_run_configuration(run.run_id)
        _, event_count = self.repository.list_events(run.run_id, limit=100, offset=0)
        with self.assertRaisesRegex(ValueError, "already registered as a Run Configuration"):
            self.repository.create_developer_step_from_plan(run.run_id, step.workflow_step_id,
                project_artifact_id=configuration.artifact_id, a2a_artifact_id="requirements-a2a",
                requirement_ids=[uuid4()], developer_configured=True, requirement_payload={"requirements": []})
        self.assertEqual(self.repository.get_run(run.run_id), run)
        self.assertEqual(self.repository.list_steps(run.run_id), [step])
        self.assertEqual(self.repository.list_project_artifacts(run.run_id), [])
        self.assertEqual(self.repository.list_events(run.run_id, limit=100, offset=0)[1], event_count)
        requirement_artifact_id = uuid4()
        self.repository.create_developer_step_from_plan(run.run_id, step.workflow_step_id,
            project_artifact_id=requirement_artifact_id, a2a_artifact_id="requirements-a2a",
            requirement_ids=[uuid4()], developer_configured=False, requirement_payload={"requirements": []})
        from orchestrator.domain.run_configuration import RunConfigurationArtifact
        other_run = WorkflowRun(scenario_id=uuid4(), request_text="Reverse Artifact collision")
        other_configuration = RunConfigurationArtifact(artifact_id=requirement_artifact_id,
            run_id=other_run.run_id, scenario_id=other_run.scenario_id, workspace_id=other_run.workspace_id)
        with self.assertRaisesRegex(ValueError, "Artifact ID is already registered"):
            self.repository.create_run(other_run, (), (), run_configuration=other_configuration)
        self.assertIsNone(self.repository.get_run(other_run.run_id))
        self.assertIsNone(self.repository.get_run_configuration(other_run.run_id))
        self.assertIsNone(self.repository.get_workspace(other_run.workspace_id))


if __name__ == "__main__":
    unittest.main()
