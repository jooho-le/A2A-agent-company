"""SQLite persistence for Workflow runs, Agent state, and append-only traces."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, unquote_plus, urlsplit, urlunsplit
from uuid import UUID, uuid4

from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    MAX_CODE_FIX_ATTEMPTS,
    BuildReportArtifact,
    ChangeReportArtifact,
    CodeSnapshotArtifact,
    FinalVerdict,
    IssueRecord,
    QAReportArtifact,
    SecurityReportArtifact,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
    code_version_for_fix_attempt,
    requires_human_review,
    transition_run,
)
from orchestrator.domain.models import utc_now
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.core.security import redact_data, redact_text


class RunNotFoundError(LookupError):
    """Raised when a requested Workflow Run does not exist."""


class ActiveAgentTaskError(RuntimeError):
    """Raised when local cancellation cannot safely cancel an active remote Task."""


class RunDispatchConflict(RuntimeError):
    """Raised when a Run is not eligible for its one initial Planner dispatch."""


ProjectArtifact = (
    CodeSnapshotArtifact
    | ChangeReportArtifact
    | BuildReportArtifact
    | QAReportArtifact
    | SecurityReportArtifact
    | RequirementArtifact
)


class SQLiteWorkflowRepository:
    """Persist related workflow snapshots using short SQLite transactions.

    JSON payloads retain the validated Pydantic domain representation while
    indexed columns and foreign keys enforce identity and run-level ownership.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = Path(database_path)
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @property
    def database_path(self) -> Path:
        return self._database_path

    def create_run(
        self,
        run: WorkflowRun,
        steps: Sequence[WorkflowStep],
        events: Sequence[TraceEvent],
        *,
        run_configuration=None,
        workspace=None,
    ) -> None:
        _validate_bundle(run, steps, events)
        if run_configuration is None:
            from orchestrator.domain.run_configuration import RunConfigurationArtifact
            run_configuration = RunConfigurationArtifact(
                run_id=run.run_id, scenario_id=run.scenario_id, workspace_id=run.workspace_id,
            )
        if workspace is None:
            from orchestrator.domain.workspaces import WorkspaceRecord
            workspace = WorkspaceRecord(
                workspace_id=run.workspace_id, run_id=run.run_id,
                root_path=str((self.database_path.parent / "workspaces" / str(run.workspace_id)).resolve()),
            )
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO workflow_runs(run_id, status, created_at, updated_at, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                _run_row(run),
            )
            for step in steps:
                _insert_step(connection, step)
            for event in events:
                _insert_event(connection, event)
            if run_configuration is not None:
                if (run_configuration.run_id != run.run_id
                    or run_configuration.scenario_id != run.scenario_id
                    or run_configuration.workspace_id != run.workspace_id):
                    raise ValueError("Run Configuration belongs to another Run")
                if connection.execute(
                    "SELECT 1 FROM project_artifacts WHERE artifact_id=?",
                    (str(run_configuration.artifact_id),),
                ).fetchone() is not None:
                    raise ValueError("Run Configuration Artifact ID is already registered")
                connection.execute(
                    "INSERT INTO run_configurations(run_id, payload_json) VALUES (?, ?)",
                    (str(run.run_id), _sanitized_json(run_configuration)),
                )
                _insert_event(connection, TraceEvent(
                    run_id=run.run_id, event_type="ARTIFACT_REGISTERED",
                    actor="Orchestrator", attempt=0,
                    output_artifact_ids=[run_configuration.artifact_id],
                    workflow_state=run.status,
                ))
            if workspace is not None:
                if workspace.run_id != run.run_id or workspace.workspace_id != run.workspace_id:
                    raise ValueError("Workspace identity differs from the Run")
                connection.execute(
                    "INSERT INTO workspaces(workspace_id, run_id, payload_json) VALUES (?, ?, ?)",
                    (str(workspace.workspace_id), str(run.run_id), workspace.model_dump_json()),
                )

    def get_run_configuration(self, run_id: UUID):
        from orchestrator.domain.run_configuration import RunConfigurationArtifact
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM run_configurations WHERE run_id = ?", (str(run_id),)
            ).fetchone()
        return RunConfigurationArtifact.model_validate_json(row[0]) if row else None

    def get_workspace(self, workspace_id: UUID):
        from orchestrator.domain.workspaces import WorkspaceRecord
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workspaces WHERE workspace_id = ?", (str(workspace_id),)
            ).fetchone()
        return WorkspaceRecord.model_validate_json(row[0]) if row else None

    def get_planner_plan(self, run_id: UUID):
        from orchestrator.application.planner_output import PlannerPlan
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM project_artifacts WHERE run_id = ? "
                "AND artifact_type = 'REQUIREMENT' ORDER BY artifact_version DESC LIMIT 1",
                (str(run_id),),
            ).fetchone()
        return PlannerPlan.model_validate(RequirementArtifact.model_validate_json(row[0]).payload) if row else None

    def get_planning_artifact(self, run_id: UUID) -> RequirementArtifact | None:
        return next((artifact for artifact in self.list_project_artifacts(run_id)
                     if isinstance(artifact, RequirementArtifact)), None)

    def acquire_control(self, run_id: UUID) -> str:
        """Claim a human-controlled operation without permitting concurrent resumes."""
        token = str(uuid4())
        with self._transaction() as connection:
            if connection.execute("SELECT 1 FROM workflow_runs WHERE run_id = ?", (str(run_id),)).fetchone() is None:
                raise RunNotFoundError(str(run_id))
            existing = connection.execute(
                "SELECT token, owner_pid FROM control_leases WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if existing is not None:
                if existing[1] is None:
                    raise RunDispatchConflict("Run control owner is unknown; manual recovery is required")
                try:
                    os.kill(existing[1], 0)
                except ProcessLookupError:
                    pass
                except PermissionError:
                    raise RunDispatchConflict("Run control owner cannot be verified")
                else:
                    raise RunDispatchConflict("Run already has an active control operation")
            connection.execute(
                "INSERT INTO control_leases(run_id, token, owner_pid) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET token=excluded.token, owner_pid=excluded.owner_pid",
                (str(run_id), token, os.getpid()),
            )
        return token

    def release_control(self, run_id: UUID, token: str) -> None:
        with self._transaction() as connection:
            connection.execute("DELETE FROM control_leases WHERE run_id = ? AND token = ?", (str(run_id), token))

    def resume_run_with_step(
        self, run_id: UUID, step_id: UUID | None = None,
    ) -> tuple[WorkflowRun, WorkflowStep | None]:
        """Resume the recorded stage; preserve completed Tasks and the existing fix cycle."""
        with self._transaction() as connection:
            row = connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id = ?", (str(run_id),)).fetchone()
            if row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, row[0])
            if run.status not in (WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW):
                raise RunDispatchConflict("Run is not paused for safe resumption")
            step = None
            if step_id is not None:
                row = connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ? AND workflow_step_id = ?",
                    (str(run_id), str(step_id)),
                ).fetchone()
                if row is None:
                    raise RunDispatchConflict("Resume Step is not owned by this Run")
                step = _load_model(WorkflowStep, row[0])
                if step.status in (WorkflowStepStatus.CANCELED, WorkflowStepStatus.FAILED):
                    raise RunDispatchConflict("A terminal failed or canceled Task cannot be resumed")
            updated = transition_run(run, run.resume_state)
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, payload_json=? WHERE run_id=?",
                (updated.status.value, updated.updated_at.isoformat(), _json_model(updated), str(run_id)),
            )
            _insert_event(connection, TraceEvent(
                run_id=run_id, workflow_step_id=step_id, event_type="WORKFLOW_RESUMED",
                actor="Orchestrator", attempt=updated.fix_attempt, workflow_state=updated.status,
            ))
        return updated, step

    def complete_remote_cancellation(
        self, run_id: UUID, reason: str, remote_tasks: Mapping[UUID, A2ATaskState],
    ) -> WorkflowRun:
        """Abort only after every unresolved remote Task has confirmed CANCELED."""
        with self._transaction() as connection:
            row = connection.execute("SELECT payload_json FROM workflow_runs WHERE run_id = ?", (str(run_id),)).fetchone()
            if row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, row[0])
            steps = [_load_model(WorkflowStep, row[0]) for row in connection.execute(
                "SELECT payload_json FROM workflow_steps WHERE run_id = ?", (str(run_id),)
            ).fetchall()]
            unresolved = [step for step in steps if step.a2a_task_id is not None
                          and step.a2a_task_state not in _TERMINAL_TASK_STATES]
            if any(remote_tasks.get(step.workflow_step_id) != A2ATaskState.CANCELED for step in unresolved):
                raise ActiveAgentTaskError("Every unresolved remote Task requires confirmed cancellation")
            if any(step.status == WorkflowStepStatus.RUNNING and step.a2a_task_id is None for step in steps):
                raise ActiveAgentTaskError("An uncertain send without a Task ID cannot be canceled safely")
            updated = transition_run(run, WorkflowStatus.ABORTED, termination_reason=reason)
            for step in steps:
                if step in unresolved or step.status in (WorkflowStepStatus.PENDING, WorkflowStepStatus.WAITING_INPUT):
                    canceled = WorkflowStep.model_validate({
                        **step.model_dump(), "status": WorkflowStepStatus.CANCELED,
                        "a2a_task_state": A2ATaskState.CANCELED if step in unresolved else step.a2a_task_state,
                        "updated_at": utc_now(),
                    })
                    _upsert_step(connection, canceled)
                    _insert_event(connection, TraceEvent(
                        run_id=run_id, workflow_step_id=step.workflow_step_id,
                        a2a_task_id=step.a2a_task_id, agent_context_id=step.agent_context_id,
                        event_type="WORKFLOW_STEP_CANCELED", actor="Orchestrator",
                        attempt=step.attempt, a2a_task_state=canceled.a2a_task_state,
                        workflow_state=WorkflowStatus.ABORTED,
                    ))
            connection.execute(
                "UPDATE workflow_runs SET status=?, updated_at=?, payload_json=? WHERE run_id=?",
                (updated.status.value, updated.updated_at.isoformat(), _json_model(updated), str(run_id)),
            )
            _insert_event(connection, TraceEvent(
                run_id=run_id, event_type="RUN_ABORTED", actor="Orchestrator",
                attempt=run.fix_attempt, workflow_state=updated.status,
            ))
        return updated

    def ingest_tool_evidence(self, run_id: UUID, step_id: UUID, evidences: Sequence) -> None:
        """Persist each observed Tool attempt once, including its retry-safe evidence."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?",
                (str(run_id), str(step_id)),
            ).fetchone()
            if row is None:
                raise RunDispatchConflict("Tool evidence Step is not owned by this Run")
            step = _load_model(WorkflowStep, row[0])
            for evidence in evidences:
                if step.code_version is not None and evidence.execution_manifest.code_version != step.code_version:
                    raise ValueError("Tool execution evidence refers to another Step codeVersion")
                for attempt in evidence.attempts:
                    payload = _sanitize_storage_value({
                        "toolEvidence": evidence.model_dump(mode="json", by_alias=True),
                        "attempt": attempt.model_dump(mode="json", by_alias=True),
                    })
                    old = connection.execute(
                        "SELECT payload_json,workflow_step_id FROM tool_attempts WHERE run_id=? AND execution_id=? AND attempt=?",
                        (str(run_id), str(evidence.execution_id), attempt.attempt),
                    ).fetchone()
                    if old is not None:
                        # Re-observing a later report may add more attempts to the same execution;
                        # the already observed individual attempt must remain identical.
                        previous = _sanitize_storage_value(json.loads(old[0]))
                        identity = ("toolName", "executionManifest", "evidenceRef")
                        if (
                            old[1] != str(step_id)
                            or previous["attempt"] != payload["attempt"]
                            or any(previous["toolEvidence"][key] != payload["toolEvidence"][key] for key in identity)
                        ):
                            raise ValueError("Tool attempt evidence cannot be rewritten")
                        continue
                    connection.execute(
                        "INSERT INTO tool_attempts(run_id, workflow_step_id, execution_id, attempt, payload_json) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (str(run_id), str(step_id), str(evidence.execution_id), attempt.attempt, json.dumps(payload)),
                    )
                    for event_type in ("MCP_TOOL_CALLED", "MCP_TOOL_FINISHED"):
                        _insert_event(connection, TraceEvent(
                            run_id=run_id, workflow_step_id=step_id, event_type=event_type,
                            actor=step.agent_role.value, attempt=attempt.attempt,
                            requirement_ids=step.requirement_ids,
                            input_artifact_ids=step.input_artifact_ids,
                            code_version=step.code_version,
                            snapshot_sha256=evidence.execution_manifest.snapshot_sha256,
                            duration_ms=attempt.duration_ms if event_type == "MCP_TOOL_FINISHED" else None,
                        ))

    def list_tool_attempts(self, run_id: UUID) -> list[dict[str, object]]:
        with self._connection() as connection:
            rows = connection.execute("SELECT payload_json FROM tool_attempts WHERE run_id=? ORDER BY rowid", (str(run_id),)).fetchall()
        return [_sanitize_storage_value(json.loads(row[0])) for row in rows]

    def claim_planner_dispatch(
        self, run_id: UUID
    ) -> tuple[WorkflowRun, WorkflowStep]:
        """Atomically claim the initial Planner Step before making an A2A call."""
        with self._transaction() as connection:
            run_row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run_row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, run_row[0])
            run_steps = [
                _load_model(WorkflowStep, step_row[0])
                for step_row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            planner_steps = [
                step for step in run_steps if step.agent_role == AgentRole.PLANNER
            ]
            if len(planner_steps) != 1:
                raise RunDispatchConflict("Run must contain exactly one Planner Step")
            step = planner_steps[0]
            if (
                run.status != WorkflowStatus.RECEIVED
                or step.status != WorkflowStepStatus.PENDING
                or step.a2a_task_id is not None
                or step.a2a_task_state is not None
            ):
                raise RunDispatchConflict("Run is not eligible for initial Planner dispatch")

            updated_run = transition_run(run, WorkflowStatus.PLANNING)
            updated_step = step.model_copy(
                update={
                    "status": WorkflowStepStatus.RUNNING,
                    "updated_at": utc_now(),
                }
            )
            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    updated_run.status.value,
                    updated_run.updated_at.isoformat(),
                    _json_model(updated_run),
                    str(run_id),
                ),
            )
            _upsert_step(connection, updated_step)
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type="WORKFLOW_STATE_CHANGED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=updated_run.status,
                ),
            )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    workflow_step_id=updated_step.workflow_step_id,
                    event_type="WORKFLOW_STEP_DISPATCH_STARTED",
                    actor="Orchestrator",
                    attempt=updated_step.attempt,
                    workflow_state=updated_run.status,
                ),
            )
        return updated_run, updated_step

    def create_developer_step_from_plan(
        self,
        run_id: UUID,
        planner_step_id: UUID,
        *,
        a2a_artifact_id: str,
        requirement_ids: Sequence[UUID],
        project_artifact_id: UUID,
        developer_configured: bool,
        requirement_payload: dict[str, object] | None = None,
        artifact_version: int = 1,
        artifact_uri: str | None = None,
    ) -> tuple[WorkflowRun, WorkflowStep]:
        """Atomically link a validated Planner Artifact and create one Developer Step."""
        if not requirement_ids or len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("Developer Step requires unique Planner requirement IDs")

        with self._transaction() as connection:
            run_row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run_row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, run_row[0])
            steps = [
                _load_model(WorkflowStep, row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            planner_steps = [
                step for step in steps
                if step.workflow_step_id == planner_step_id
                and step.agent_role == AgentRole.PLANNER
            ]
            all_planner_steps = [
                step for step in steps if step.agent_role == AgentRole.PLANNER
            ]
            if (
                run.status != WorkflowStatus.PLANNING
                or len(all_planner_steps) != 1
                or len(planner_steps) != 1
                or planner_steps[0].status != WorkflowStepStatus.SUCCEEDED
                or planner_steps[0].a2a_task_state != A2ATaskState.COMPLETED
                or planner_steps[0].a2a_task_id is None
                or a2a_artifact_id not in planner_steps[0].a2a_artifact_ids
                or any(step.agent_role == AgentRole.DEVELOPER for step in steps)
            ):
                raise RunDispatchConflict(
                    "Run is not eligible for its first Developer Step"
                )

            implementing_run = transition_run(run, WorkflowStatus.IMPLEMENTING)
            final_run = (
                implementing_run
                if developer_configured
                else transition_run(implementing_run, WorkflowStatus.HUMAN_REVIEW)
            )
            planner_step = planner_steps[0]
            if requirement_payload is not None:
                requirement_artifact = RequirementArtifact(
                    artifact_id=project_artifact_id, artifact_version=artifact_version,
                    run_id=run_id, workflow_step_id=planner_step_id,
                    a2a_task_id=planner_step.a2a_task_id, a2a_artifact_id=a2a_artifact_id,
                    requirement_ids=tuple(requirement_ids),
                    artifact_uri=artifact_uri or f"artifact://{project_artifact_id}/requirements.json",
                    payload=requirement_payload,
                )
                _insert_artifact(connection, requirement_artifact, run.status)
            linked_artifact_ids = list(
                dict.fromkeys([*planner_step.output_artifact_ids, project_artifact_id])
            )
            updated_planner_step = WorkflowStep.model_validate(
                {
                    **planner_step.model_dump(mode="python"),
                    "output_artifact_ids": linked_artifact_ids,
                    "updated_at": utc_now(),
                }
            )
            developer_step = WorkflowStep(
                run_id=run_id,
                agent_role=AgentRole.DEVELOPER,
                status=(
                    WorkflowStepStatus.RUNNING
                    if developer_configured
                    else WorkflowStepStatus.PENDING
                ),
                requirement_ids=list(requirement_ids),
                code_version=code_version_for_fix_attempt(run.fix_attempt),
                input_artifact_ids=[project_artifact_id],
            )

            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    final_run.status.value,
                    final_run.updated_at.isoformat(),
                    _json_model(final_run),
                    str(run_id),
                ),
            )
            _upsert_step(connection, updated_planner_step)
            _insert_step(connection, developer_step)

            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    workflow_step_id=planner_step_id,
                    a2a_task_id=planner_step.a2a_task_id,
                    agent_context_id=planner_step.agent_context_id,
                    event_type="PLANNER_OUTPUT_VALIDATED",
                    actor="Orchestrator",
                    attempt=planner_step.attempt,
                    requirement_ids=list(requirement_ids),
                    output_artifact_ids=[project_artifact_id],
                    a2a_task_state=planner_step.a2a_task_state,
                    workflow_state=run.status,
                ),
            )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type="WORKFLOW_STATE_CHANGED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=implementing_run.status,
                ),
            )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    workflow_step_id=developer_step.workflow_step_id,
                    event_type="WORKFLOW_STEP_CREATED",
                    actor="Orchestrator",
                    attempt=developer_step.attempt,
                    requirement_ids=developer_step.requirement_ids,
                    input_artifact_ids=developer_step.input_artifact_ids,
                    workflow_state=implementing_run.status,
                ),
            )
            if developer_configured:
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=developer_step.workflow_step_id,
                        event_type="WORKFLOW_STEP_DISPATCH_STARTED",
                        actor="Orchestrator",
                        attempt=developer_step.attempt,
                        requirement_ids=developer_step.requirement_ids,
                        input_artifact_ids=developer_step.input_artifact_ids,
                        workflow_state=final_run.status,
                    ),
                )
            else:
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="WORKFLOW_STATE_CHANGED",
                        actor="Orchestrator",
                        attempt=0,
                        workflow_state=final_run.status,
                    ),
                )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=developer_step.workflow_step_id,
                        event_type="DEVELOPER_DISPATCH_NOT_CONFIGURED",
                        actor="Orchestrator",
                        attempt=developer_step.attempt,
                        requirement_ids=developer_step.requirement_ids,
                        input_artifact_ids=developer_step.input_artifact_ids,
                        workflow_state=final_run.status,
                    ),
                )

        return final_run, developer_step

    def record_developer_candidate(
        self,
        run_id: UUID,
        developer_step_id: UUID,
        *,
        source: CodeSnapshotArtifact,
        change_report: ChangeReportArtifact,
        build_report: BuildReportArtifact,
        validation_agents_configured: bool,
        validation_requirement_ids: dict[AgentRole, Sequence[UUID]] | None = None,
        detected_issues: Sequence[IssueRecord] = (),
    ) -> tuple[WorkflowRun, WorkflowStep, tuple[WorkflowStep, ...]]:
        """Atomically append Developer Artifacts and prepare the next workflow state."""
        artifacts: tuple[ProjectArtifact, ...] = (source, change_report, build_report)
        if len({artifact.artifact_id for artifact in artifacts}) != len(artifacts):
            raise ValueError("Developer project Artifact IDs must be distinct")
        if any(artifact.run_id != run_id for artifact in artifacts):
            raise ValueError("Developer Artifacts must belong to the current Run")
        if any(artifact.workflow_step_id != developer_step_id for artifact in artifacts):
            raise ValueError("Developer Artifacts must reference the Developer Step")
        if (
            change_report.a2a_task_id != source.a2a_task_id
            or build_report.a2a_task_id != source.a2a_task_id
            or build_report.source_artifact_id != source.artifact_id
            or build_report.execution_manifest != source.execution_manifest()
        ):
            raise ValueError("Developer Artifact references or execution Manifests disagree")

        with self._transaction() as connection:
            run_row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run_row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, run_row[0])
            steps = [
                _load_model(WorkflowStep, row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            developer_steps = [
                step for step in steps
                if step.workflow_step_id == developer_step_id
                and step.agent_role == AgentRole.DEVELOPER
            ]
            if (
                run.status not in (WorkflowStatus.IMPLEMENTING, WorkflowStatus.FIXING)
                or len(developer_steps) != 1
                or developer_steps[0].status != WorkflowStepStatus.SUCCEEDED
                or developer_steps[0].a2a_task_state != A2ATaskState.COMPLETED
                or developer_steps[0].a2a_task_id != source.a2a_task_id
            ):
                raise RunDispatchConflict("Run is not eligible to register a Developer candidate")

            developer_step = developer_steps[0]
            expected_requirements = set(developer_step.requirement_ids)
            if any(
                set(artifact.requirement_ids) != expected_requirements
                for artifact in artifacts
            ):
                raise ValueError("Developer Artifacts must preserve Step Requirement IDs")
            if source.code_version != code_version_for_fix_attempt(run.fix_attempt):
                raise ValueError("Source codeVersion does not match the Run fix attempt")
            if any(artifact.code_version != source.code_version for artifact in artifacts):
                raise ValueError("Developer Artifacts must refer to one codeVersion")
            if any(
                artifact.a2a_artifact_id not in developer_step.a2a_artifact_ids
                for artifact in artifacts
            ):
                raise ValueError("Developer Artifacts must reference IDs from its A2A Task")

            for artifact in artifacts:
                if artifact.previous_artifact_id is None:
                    continue
                previous = connection.execute(
                    "SELECT artifact_type, artifact_version FROM project_artifacts "
                    "WHERE artifact_id = ? AND run_id = ?",
                    (str(artifact.previous_artifact_id), str(run_id)),
                ).fetchone()
                if (
                    previous is None
                    or previous[0] != artifact.artifact_type
                    or previous[1] != artifact.artifact_version - 1
                ):
                    raise ValueError("Artifact predecessor must exist in the same Run and lineage")

            for artifact in artifacts:
                _insert_artifact(connection, artifact, run.status)

            updated_developer_step = WorkflowStep.model_validate(
                {
                    **developer_step.model_dump(mode="python"),
                    "code_version": source.code_version,
                    "output_artifact_ids": [str(artifact.artifact_id) for artifact in artifacts],
                    "updated_at": utc_now(),
                }
            )
            run_with_code_version = WorkflowRun.model_validate(
                {
                    **run.model_dump(mode="python"),
                    "code_version": source.code_version,
                }
            )
            _store_detected_issues(connection, run_with_code_version, detected_issues)
            _record_build_revalidation_results(connection, run_with_code_version, build_report)
            is_fix = run.status == WorkflowStatus.FIXING
            intermediate_state = (
                WorkflowStatus.REVALIDATING if is_fix else WorkflowStatus.SNAPSHOT_READY
            )
            intermediate_run = transition_run(run_with_code_version, intermediate_state)
            _insert_event(connection, TraceEvent(
                run_id=run_id, workflow_step_id=developer_step_id,
                event_type="SNAPSHOT_FROZEN", actor="Orchestrator",
                attempt=run.fix_attempt, requirement_ids=developer_step.requirement_ids,
                output_artifact_ids=[source.artifact_id], code_version=source.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=intermediate_run.status,
            ))
            if is_fix:
                _insert_event(connection, TraceEvent(
                    run_id=run_id, workflow_step_id=developer_step_id,
                    event_type="FIX_COMPLETED", actor="DEVELOPER",
                    attempt=run.fix_attempt, requirement_ids=developer_step.requirement_ids,
                    input_artifact_ids=developer_step.input_artifact_ids,
                    output_artifact_ids=[source.artifact_id], code_version=source.code_version,
                    snapshot_sha256=source.snapshot_sha256, workflow_state=intermediate_run.status,
                ))
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    workflow_step_id=developer_step_id,
                    a2a_task_id=developer_step.a2a_task_id,
                    event_type="DEVELOPER_ARTIFACTS_VALIDATED",
                    actor="Orchestrator",
                    attempt=developer_step.attempt,
                    requirement_ids=developer_step.requirement_ids,
                    output_artifact_ids=[artifact.artifact_id for artifact in artifacts],
                    a2a_task_state=developer_step.a2a_task_state,
                    code_version=source.code_version,
                    workflow_state=intermediate_run.status,
                ),
            )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type="WORKFLOW_STATE_CHANGED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=intermediate_run.status,
                ),
            )

            validation_steps: tuple[WorkflowStep, ...] = ()
            if getattr(build_report, "execution_outcome", None) == "UNVERIFIED":
                evidence = getattr(build_report, "tool_evidence", None)
                if evidence is not None and evidence.retries_exhausted:
                    ready_run = (
                        intermediate_run if is_fix
                        else transition_run(intermediate_run, WorkflowStatus.VALIDATING)
                    )
                    final_run = transition_run(
                        ready_run, WorkflowStatus.FINISHED, verdict=FinalVerdict.UNVERIFIED
                    )
                else:
                    final_run = transition_run(
                        intermediate_run, WorkflowStatus.HUMAN_REVIEW,
                        verdict=FinalVerdict.HUMAN_REVIEW,
                    )
                _insert_event(connection, TraceEvent(
                    run_id=run_id, workflow_step_id=developer_step_id,
                    event_type="BUILD_UNVERIFIED", actor="Orchestrator",
                    attempt=run.fix_attempt, output_artifact_ids=[build_report.artifact_id],
                    code_version=source.code_version, workflow_state=final_run.status,
                ))
            elif not build_report.passed:
                if intermediate_run.fix_attempt >= MAX_CODE_FIX_ATTEMPTS:
                    final_run = transition_run(
                        intermediate_run,
                        WorkflowStatus.FINISHED,
                        verdict=FinalVerdict.FAIL,
                    )
                    event_type = "BUILD_FAILED_FIX_LIMIT_REACHED"
                else:
                    final_run = transition_run(
                        intermediate_run, WorkflowStatus.FIX_REQUIRED
                    )
                    event_type = "BUILD_FAILED"
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=developer_step_id,
                        event_type=event_type,
                        actor="Orchestrator",
                        attempt=developer_step.attempt,
                        requirement_ids=developer_step.requirement_ids,
                        output_artifact_ids=[build_report.artifact_id],
                        code_version=source.code_version,
                        workflow_state=final_run.status,
                    ),
                )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="WORKFLOW_STATE_CHANGED",
                        actor="Orchestrator",
                        attempt=0,
                        workflow_state=final_run.status,
                    ),
                )
            else:
                validation_state = (
                    WorkflowStatus.REVALIDATING if is_fix else WorkflowStatus.VALIDATING
                )
                validating_run = (
                    intermediate_run
                    if is_fix
                    else transition_run(intermediate_run, validation_state)
                )
                required_by_role = validation_requirement_ids or {
                    AgentRole.QA: developer_step.requirement_ids,
                    AgentRole.SECURITY: developer_step.requirement_ids,
                }
                validation_steps = tuple(
                    WorkflowStep(
                        run_id=run_id,
                        agent_role=role,
                        status=(
                            WorkflowStepStatus.RUNNING
                            if validation_agents_configured
                            else WorkflowStepStatus.PENDING
                        ),
                        requirement_ids=list(required_by_role[role]),
                        code_version=source.code_version,
                        input_artifact_ids=[source.artifact_id],
                    )
                    for role in (AgentRole.QA, AgentRole.SECURITY)
                )
                final_run = (
                    validating_run
                    if validation_agents_configured
                    else transition_run(validating_run, WorkflowStatus.HUMAN_REVIEW)
                )
                _insert_event(connection, TraceEvent(
                    run_id=run_id, event_type=("REVALIDATION_STARTED" if is_fix else "VALIDATION_STARTED"),
                    actor="Orchestrator", attempt=run.fix_attempt,
                    requirement_ids=developer_step.requirement_ids,
                    input_artifact_ids=[source.artifact_id], code_version=source.code_version,
                    snapshot_sha256=source.snapshot_sha256, workflow_state=validating_run.status,
                ))
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=developer_step_id,
                        event_type="BUILD_PASSED",
                        actor="Orchestrator",
                        attempt=developer_step.attempt,
                        requirement_ids=developer_step.requirement_ids,
                        output_artifact_ids=[build_report.artifact_id],
                        code_version=source.code_version,
                        workflow_state=validating_run.status,
                    ),
                )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="WORKFLOW_STATE_CHANGED",
                        actor="Orchestrator",
                        attempt=0,
                        workflow_state=validating_run.status,
                    ),
                )
                for validation_step in validation_steps:
                    _insert_step(connection, validation_step)
                    if validation_agents_configured:
                        _insert_event(connection, TraceEvent(
                            run_id=run_id, workflow_step_id=validation_step.workflow_step_id,
                            event_type="WORKFLOW_STEP_CREATED", actor="Orchestrator",
                            attempt=run.fix_attempt, requirement_ids=validation_step.requirement_ids,
                            input_artifact_ids=[source.artifact_id], code_version=source.code_version,
                            workflow_state=final_run.status,
                        ))
                    _insert_event(
                        connection,
                        TraceEvent(
                            run_id=run_id,
                            workflow_step_id=validation_step.workflow_step_id,
                            event_type=(
                                "WORKFLOW_STEP_DISPATCH_STARTED"
                                if validation_agents_configured
                                else "WORKFLOW_STEP_CREATED"
                            ),
                            actor="Orchestrator",
                            attempt=validation_step.attempt,
                            requirement_ids=validation_step.requirement_ids,
                            input_artifact_ids=validation_step.input_artifact_ids,
                            code_version=validation_step.code_version,
                            workflow_state=final_run.status,
                        ),
                    )
                if not validation_agents_configured:
                    _insert_event(
                        connection,
                        TraceEvent(
                            run_id=run_id,
                            event_type="QA_SECURITY_DISPATCH_NOT_CONFIGURED",
                            actor="Orchestrator",
                            attempt=0,
                            code_version=source.code_version,
                            workflow_state=final_run.status,
                        ),
                    )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="WORKFLOW_STATE_CHANGED",
                        actor="Orchestrator",
                        attempt=0,
                        workflow_state=final_run.status,
                    ),
                )

            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    final_run.status.value,
                    final_run.updated_at.isoformat(),
                    _json_model(final_run),
                    str(run_id),
                ),
            )
            _upsert_step(connection, updated_developer_step)
            _record_terminal_events(connection, final_run)

        return final_run, updated_developer_step, validation_steps

    def record_detected_issues(
        self, run_id: UUID, issues: Sequence[IssueRecord], *, allow_terminal: bool = False,
    ) -> tuple[IssueRecord, ...]:
        """Keep failures even when a final limit or policy decision prevents another fix."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?", (str(run_id),)
            ).fetchone()
            if row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, row[0])
            if run.status == WorkflowStatus.ABORTED or (
                run.status == WorkflowStatus.FINISHED and not allow_terminal
            ):
                raise RunDispatchConflict("Run is not eligible for Issue detection")
            return _store_detected_issues(connection, run, issues)

    def start_fix_cycle(
        self,
        run_id: UUID,
        issues: Sequence[IssueRecord],
        *,
        developer_configured: bool,
    ) -> tuple[WorkflowRun, WorkflowStep | None, tuple[IssueRecord, ...], list[UUID]]:
        """Persist issues and atomically start a bounded Developer fix attempt."""
        if not issues:
            raise ValueError("a fix cycle requires at least one Issue")
        if any(issue.run_id != run_id for issue in issues):
            raise ValueError("all Issues must belong to the current Run")

        with self._transaction() as connection:
            run_row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run_row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, run_row[0])
            if run.status != WorkflowStatus.FIX_REQUIRED or run.code_version is None:
                raise RunDispatchConflict("Run is not ready for a Developer fix attempt")

            steps = [
                _load_model(WorkflowStep, row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            previous_developer = next(
                (step for step in reversed(steps) if step.agent_role == AgentRole.DEVELOPER),
                None,
            )
            if previous_developer is None:
                raise RunDispatchConflict("fix attempt has no prior Developer Step")

            stored_issues = list(_store_detected_issues(connection, run, issues))

            if any(requires_human_review(item.consecutive_repeat_count) for item in stored_issues):
                final_run = transition_run(
                    run, WorkflowStatus.HUMAN_REVIEW, verdict=FinalVerdict.HUMAN_REVIEW
                )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="SAME_ISSUE_REQUIRES_REVIEW",
                        actor="Orchestrator",
                        attempt=run.fix_attempt,
                        issue_id=next(
                            item.issue_id for item in stored_issues
                            if requires_human_review(item.consecutive_repeat_count)
                        ),
                        code_version=run.code_version,
                        workflow_state=final_run.status,
                    ),
                )
                step = None
                input_artifact_ids: list[UUID] = []
            elif not developer_configured:
                final_run = transition_run(
                    run, WorkflowStatus.HUMAN_REVIEW, verdict=FinalVerdict.HUMAN_REVIEW
                )
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        event_type="FIX_DISPATCH_NOT_CONFIGURED",
                        actor="Orchestrator",
                        attempt=run.fix_attempt,
                        code_version=run.code_version,
                        workflow_state=final_run.status,
                    ),
                )
                step = None
                input_artifact_ids = []
            else:
                artifact_rows = connection.execute(
                    "SELECT artifact_id, artifact_type FROM project_artifacts "
                    "WHERE run_id = ? AND artifact_version = ? "
                    "AND artifact_type IN ('SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT') "
                    "ORDER BY rowid",
                    (str(run_id), run.code_version),
                ).fetchall()
                by_type = {row[1]: UUID(row[0]) for row in artifact_rows}
                required_types = {"SOURCE", "CHANGE_REPORT", "BUILD_REPORT"}
                if not required_types.issubset(by_type):
                    raise RunDispatchConflict("fix cycle is missing prior candidate Artifacts")
                report_ids = [UUID(row[0]) for row in connection.execute(
                    "SELECT artifact_id FROM project_artifacts WHERE run_id=? "
                    "AND artifact_type IN ('QA_REPORT','SECURITY_REPORT') "
                    "AND json_extract(payload_json,'$.code_version')=? ORDER BY rowid",
                    (str(run_id), run.code_version),
                ).fetchall()]
                input_artifact_ids = list(by_type.values()) + report_ids
                fixing_run = transition_run(run, WorkflowStatus.FIXING)
                step = WorkflowStep(
                    run_id=run_id,
                    agent_role=AgentRole.DEVELOPER,
                    status=WorkflowStepStatus.RUNNING,
                    requirement_ids=previous_developer.requirement_ids,
                    code_version=code_version_for_fix_attempt(fixing_run.fix_attempt),
                    input_artifact_ids=input_artifact_ids,
                )
                final_run = fixing_run
                _insert_step(connection, step)
                for issue in stored_issues:
                    _insert_issue_event(connection, issue, "FIX_REQUESTED", {
                        "fixed_by": AgentRole.DEVELOPER.value,
                        "fix_workflow_step_id": str(step.workflow_step_id),
                        "previous_code_version": issue.code_version,
                        "new_code_version": step.code_version,
                    })
                    _insert_event(connection, TraceEvent(
                        run_id=run_id, workflow_step_id=step.workflow_step_id,
                        event_type="FIX_REQUESTED", actor="Orchestrator",
                        attempt=fixing_run.fix_attempt, issue_id=issue.issue_id,
                        requirement_ids=issue.requirement_ids,
                        input_artifact_ids=input_artifact_ids, code_version=step.code_version,
                        workflow_state=fixing_run.status,
                    ))
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=step.workflow_step_id,
                        event_type="FIX_ATTEMPT_STARTED",
                        actor="Orchestrator",
                        attempt=fixing_run.fix_attempt,
                        requirement_ids=step.requirement_ids,
                        input_artifact_ids=input_artifact_ids,
                        issue_id=stored_issues[0].issue_id,
                        code_version=step.code_version,
                        workflow_state=fixing_run.status,
                    ),
                )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type="WORKFLOW_STATE_CHANGED",
                    actor="Orchestrator",
                    attempt=final_run.fix_attempt,
                    workflow_state=final_run.status,
                ),
            )
            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    final_run.status.value, final_run.updated_at.isoformat(),
                    _json_model(final_run), str(run_id),
                ),
            )
            _record_terminal_events(connection, final_run)
        return final_run, step, tuple(stored_issues), input_artifact_ids

    def list_issue_records(self, run_id: UUID) -> list[IssueRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM issue_records WHERE run_id = ? "
                "ORDER BY created_at, issue_id",
                (str(run_id),),
            ).fetchall()
            updates = connection.execute(
                "SELECT issue_id, payload_json FROM issue_events WHERE run_id = ? ORDER BY sequence",
                (str(run_id),),
            ).fetchall()
        by_id = {issue.issue_id: issue for issue in (
            _load_model(IssueRecord, row[0]) for row in rows
        )}
        for event in updates:
            issue_id = UUID(event[0])
            if issue_id in by_id:
                by_id[issue_id] = IssueRecord.model_validate({
                    **by_id[issue_id].model_dump(), **_sanitize_storage_value(json.loads(event[1])),
                })
        return list(by_id.values())

    def list_project_artifacts(self, run_id: UUID) -> list[ProjectArtifact]:
        """Return append-only Artifact Registry metadata rows for one Run."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT artifact_type, payload_json FROM project_artifacts "
                "WHERE run_id = ? ORDER BY rowid",
                (str(run_id),),
            ).fetchall()
        model_by_type = {
            "SOURCE": CodeSnapshotArtifact,
            "CHANGE_REPORT": ChangeReportArtifact,
            "BUILD_REPORT": BuildReportArtifact,
            "QA_REPORT": QAReportArtifact,
            "SECURITY_REPORT": SecurityReportArtifact,
            "REQUIREMENT": RequirementArtifact,
        }
        return [
            _load_model(model_by_type[row[0]], row[1])
            for row in rows
        ]

    def record_validation_results(
        self,
        run_id: UUID,
        *,
        qa_report: QAReportArtifact,
        security_report: SecurityReportArtifact,
        decision_status: WorkflowStatus,
        verdict: FinalVerdict | None,
        detected_issues: Sequence[IssueRecord] = (),
    ) -> WorkflowRun:
        """Atomically append both validated reports and the derived Run Verdict."""
        artifacts: tuple[ProjectArtifact, ...] = (qa_report, security_report)
        if qa_report.run_id != run_id or security_report.run_id != run_id:
            raise ValueError("Validation Artifacts must belong to the current Run")
        if qa_report.artifact_id == security_report.artifact_id:
            raise ValueError("QA and Security Artifact IDs must be distinct")
        if qa_report.execution_manifest != security_report.execution_manifest:
            raise ValueError("QA and Security Execution Manifests must match")
        if decision_status == WorkflowStatus.FINISHED and verdict is None:
            raise ValueError("finished validation requires a final Verdict")
        if decision_status != WorkflowStatus.FINISHED and verdict not in (
            None,
            FinalVerdict.HUMAN_REVIEW,
        ):
            raise ValueError("non-terminal validation cannot store this final Verdict")

        with self._transaction() as connection:
            run_row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if run_row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, run_row[0])
            if run.status not in (WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING):
                raise RunDispatchConflict("Run is not awaiting validation results")

            steps = [
                _load_model(WorkflowStep, row[0])
                for row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            reports = (
                (AgentRole.QA, qa_report),
                (AgentRole.SECURITY, security_report),
            )
            updated_steps: list[WorkflowStep] = []
            for role, report in reports:
                step = next(
                    (
                        candidate
                        for candidate in steps
                        if candidate.workflow_step_id == report.workflow_step_id
                        and candidate.agent_role == role
                    ),
                    None,
                )
                if (
                    step is None
                    or step.status != WorkflowStepStatus.SUCCEEDED
                    or step.a2a_task_state != A2ATaskState.COMPLETED
                    or step.a2a_task_id != report.a2a_task_id
                    or step.workflow_step_id != report.workflow_step_id
                    or set(step.requirement_ids) != set(report.requirement_ids)
                    or step.code_version != report.code_version
                    or report.a2a_artifact_id not in step.a2a_artifact_ids
                ):
                    raise RunDispatchConflict(
                        f"{role.value} report does not match its completed WorkflowStep"
                    )
                source_row = connection.execute(
                    "SELECT payload_json FROM project_artifacts "
                    "WHERE run_id = ? AND artifact_type = 'SOURCE' AND artifact_version = ?",
                    (str(run_id), report.code_version),
                ).fetchone()
                build_row = connection.execute(
                    "SELECT payload_json FROM project_artifacts "
                    "WHERE run_id = ? AND artifact_type = 'BUILD_REPORT' AND artifact_version = ?",
                    (str(run_id), report.code_version),
                ).fetchone()
                if source_row is None or build_row is None:
                    raise RunDispatchConflict("validation has no matching Source/Build Artifact")
                source = _load_model(CodeSnapshotArtifact, source_row[0])
                build = _load_model(BuildReportArtifact, build_row[0])
                if (
                    not build.passed
                    or report.execution_manifest != source.execution_manifest()
                    or build.execution_manifest != report.execution_manifest
                    or report.execution_manifest.project_artifact_id != source.artifact_id
                ):
                    raise ValueError("validation report does not match the passed Build Snapshot")

                previous = connection.execute(
                    "SELECT artifact_id, artifact_version FROM project_artifacts "
                    "WHERE run_id = ? AND artifact_type = ? "
                    "ORDER BY artifact_version DESC LIMIT 1",
                    (str(run_id), report.artifact_type),
                ).fetchone()
                if previous is None:
                    if report.artifact_version != 1 or report.previous_artifact_id is not None:
                        raise ValueError("first validation report must start at version 1")
                elif (
                    report.artifact_version != previous[1] + 1
                    or str(report.previous_artifact_id) != previous[0]
                ):
                    raise ValueError("validation Artifact lineage is not consecutive")
                if report.workflow_step_id != step.workflow_step_id:
                    raise ValueError("validation Artifact Step reference mismatch")
                updated_steps.append(
                    WorkflowStep.model_validate(
                        {
                            **step.model_dump(mode="python"),
                            "output_artifact_ids": [str(report.artifact_id)],
                            "updated_at": utc_now(),
                        }
                    )
                )

            final_run = transition_run(run, decision_status, verdict=verdict)
            developer_step = next(
                (
                    step for step in steps
                    if step.agent_role == AgentRole.DEVELOPER
                    and step.code_version == qa_report.code_version
                ),
                None,
            )
            decision_requirement_ids = (
                developer_step.requirement_ids
                if developer_step is not None
                else list(dict.fromkeys([*qa_report.requirement_ids, *security_report.requirement_ids]))
            )
            for artifact in artifacts:
                _insert_artifact(connection, artifact, run.status)
            _store_detected_issues(connection, run, detected_issues)
            for step, report, event_type in (
                (updated_steps[0], qa_report, "QA_REPORT_VALIDATED"),
                (updated_steps[1], security_report, "SECURITY_REPORT_VALIDATED"),
            ):
                _upsert_step(connection, step)
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=step.workflow_step_id,
                        a2a_task_id=step.a2a_task_id,
                        event_type=event_type,
                        actor="Orchestrator",
                        attempt=step.attempt,
                        requirement_ids=step.requirement_ids,
                        input_artifact_ids=step.input_artifact_ids,
                        output_artifact_ids=[report.artifact_id],
                        a2a_task_state=step.a2a_task_state,
                        code_version=report.code_version,
                        snapshot_sha256=report.execution_manifest.snapshot_sha256,
                        workflow_state=final_run.status,
                    ),
                )
                _insert_event(connection, TraceEvent(
                    run_id=run_id, workflow_step_id=step.workflow_step_id,
                    event_type="VALIDATION_FINISHED", actor=step.agent_role.value,
                    attempt=run.fix_attempt, requirement_ids=step.requirement_ids,
                    input_artifact_ids=step.input_artifact_ids,
                    output_artifact_ids=[report.artifact_id], code_version=report.code_version,
                    snapshot_sha256=report.execution_manifest.snapshot_sha256,
                    workflow_state=final_run.status,
                ))
            _record_revalidation_results(connection, run, qa_report, security_report)
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type=f"VALIDATION_DECISION_{final_run.status.value}",
                    actor="Orchestrator",
                    attempt=run.fix_attempt,
                    requirement_ids=decision_requirement_ids,
                    output_artifact_ids=[qa_report.artifact_id, security_report.artifact_id],
                    code_version=qa_report.code_version,
                    snapshot_sha256=qa_report.execution_manifest.snapshot_sha256,
                    workflow_state=final_run.status,
                ),
            )
            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    final_run.status.value,
                    final_run.updated_at.isoformat(),
                    _json_model(final_run),
                    str(run_id),
                ),
            )
            _record_terminal_events(connection, final_run)
        return final_run

    def get_run(self, run_id: UUID) -> WorkflowRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
        return _load_model(WorkflowRun, row[0]) if row else None

    def list_steps(self, run_id: UUID) -> list[WorkflowStep]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM workflow_steps WHERE run_id = ? "
                "ORDER BY created_at, workflow_step_id",
                (str(run_id),),
            ).fetchall()
        return [_load_model(WorkflowStep, row[0]) for row in rows]

    def list_agent_contexts(self, run_id: UUID) -> list[AgentContext]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM agent_contexts WHERE run_id = ? "
                "ORDER BY agent_id",
                (str(run_id),),
            ).fetchall()
        return [_load_model(AgentContext, row[0]) for row in rows]

    def list_events(
        self, run_id: UUID, *, limit: int, offset: int
    ) -> tuple[list[TraceEvent], int]:
        with self._connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) FROM trace_events WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()[0]
            rows = connection.execute(
                "SELECT payload_json FROM trace_events WHERE run_id = ? "
                "ORDER BY sequence LIMIT ? OFFSET ?",
                (str(run_id), limit, offset),
            ).fetchall()
        return ([_load_model(TraceEvent, row[0]) for row in rows], total)

    def save_task_update(
        self,
        run: WorkflowRun,
        step: WorkflowStep,
        context: AgentContext,
        event: TraceEvent,
    ) -> None:
        """Atomically save one A2A Task snapshot, Agent mapping, and Trace event."""
        _validate_task_update(run, step, context, event)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run.run_id),),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(str(run.run_id))
            current_run = _load_model(WorkflowRun, row[0])
            if current_run.status in (WorkflowStatus.ABORTED, WorkflowStatus.FINISHED):
                raise RunDispatchConflict("A terminal Run cannot accept Task updates")
            step_row = connection.execute(
                "SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?",
                (str(run.run_id), str(step.workflow_step_id)),
            ).fetchone()
            if step_row is not None:
                stored_step = _load_model(WorkflowStep, step_row[0])
                if stored_step.a2a_task_state in _TERMINAL_TASK_STATES and (
                    step.a2a_task_state != stored_step.a2a_task_state
                    or step.a2a_task_id != stored_step.a2a_task_id
                ):
                    raise RunDispatchConflict("A terminal remote Task cannot change identity or state")
                if stored_step.status in (
                    WorkflowStepStatus.SUCCEEDED, WorkflowStepStatus.FAILED, WorkflowStepStatus.CANCELED,
                ) and step.status != stored_step.status:
                    raise RunDispatchConflict("A terminal WorkflowStep cannot change status")
            updated_run = current_run.model_copy(update={"updated_at": utc_now()})
            connection.execute(
                "UPDATE workflow_runs SET updated_at = ?, payload_json = ? WHERE run_id = ?",
                (
                    updated_run.updated_at.isoformat(),
                    _json_model(updated_run),
                    str(updated_run.run_id),
                ),
            )
            _upsert_step(connection, step)
            _upsert_context(connection, context)
            _insert_event(connection, event)

    def task_update_observer(
        self, run: WorkflowRun
    ) -> Callable[[WorkflowStep, AgentContext, TraceEvent], Awaitable[None]]:
        """Create an async callback directly usable by ``A2ATaskRunner``."""

        async def observe(
            step: WorkflowStep,
            context: AgentContext,
            event: TraceEvent,
        ) -> None:
            await asyncio.to_thread(self.save_task_update, run, step, context, event)

        return observe

    def save_run_update(
        self, run: WorkflowRun, events: Sequence[TraceEvent]
    ) -> None:
        """Atomically replace a Run snapshot and append its related events."""
        _validate_events_for_run(run, events)
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    run.status.value,
                    run.updated_at.isoformat(),
                    _json_model(run),
                    str(run.run_id),
                ),
            )
            if cursor.rowcount == 0:
                raise RunNotFoundError(str(run.run_id))
            for event in events:
                _insert_event(connection, event)
            _record_terminal_events(connection, run)

    def transition_run_and_record(
        self,
        run_id: UUID,
        target: WorkflowStatus,
        *,
        additional_event_types: Sequence[str] = (),
        workflow_step_id: UUID | None = None,
        attempt: int = 0,
        verdict: FinalVerdict | None = None,
    ) -> WorkflowRun:
        """Apply a validated Run transition and append its Trace atomically."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(str(run_id))
            current_run = _load_model(WorkflowRun, row[0])
            updated_run = transition_run(current_run, target, verdict=verdict)
            trace_events = [
                "WORKFLOW_STATE_CHANGED",
                *additional_event_types,
            ]
            for index, event_type in enumerate(trace_events):
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=workflow_step_id if index else None,
                        event_type=event_type,
                        actor="Orchestrator",
                        attempt=attempt if index else 0,
                        workflow_state=updated_run.status,
                    ),
                )
            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    updated_run.status.value,
                    updated_run.updated_at.isoformat(),
                    _json_model(updated_run),
                    str(run_id),
                ),
            )
            _record_terminal_events(connection, updated_run)
        return updated_run

    def cancel_run(self, run_id: UUID, reason: str) -> WorkflowRun:
        """Abort a run only when no remote A2A Task needs cancellation first."""
        reason = reason.strip()
        if not reason:
            raise ValueError("cancellation reason must not be blank")

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT payload_json FROM workflow_runs WHERE run_id = ?",
                (str(run_id),),
            ).fetchone()
            if row is None:
                raise RunNotFoundError(str(run_id))
            run = _load_model(WorkflowRun, row[0])
            steps = [
                _load_model(WorkflowStep, step_row[0])
                for step_row in connection.execute(
                    "SELECT payload_json FROM workflow_steps WHERE run_id = ?",
                    (str(run_id),),
                ).fetchall()
            ]
            active_steps = [
                step
                for step in steps
                if step.status == WorkflowStepStatus.RUNNING
                or step.a2a_task_state
                in (A2ATaskState.SUBMITTED, A2ATaskState.WORKING)
                or (step.a2a_task_id is not None and step.a2a_task_state not in _TERMINAL_TASK_STATES)
            ]
            if active_steps:
                raise ActiveAgentTaskError(
                    "Run has an active Agent Task; remote cancellation is not configured"
                )

            updated_run = transition_run(
                run,
                WorkflowStatus.ABORTED,
                termination_reason=reason,
            )
            event_list = [
                TraceEvent(
                    run_id=run.run_id,
                    workflow_step_id=None,
                    event_type="RUN_ABORTED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=WorkflowStatus.ABORTED,
                )
            ]
            for step in steps:
                if step.status not in (
                    WorkflowStepStatus.PENDING,
                    WorkflowStepStatus.WAITING_INPUT,
                ):
                    continue
                canceled = step.model_copy(
                    update={
                        "status": WorkflowStepStatus.CANCELED,
                        "updated_at": utc_now(),
                    }
                )
                _upsert_step(connection, canceled)
                event_list.append(
                    TraceEvent(
                        run_id=run.run_id,
                        workflow_step_id=step.workflow_step_id,
                        a2a_task_id=step.a2a_task_id,
                        agent_context_id=step.agent_context_id,
                        event_type="WORKFLOW_STEP_CANCELED",
                        actor="Orchestrator",
                        attempt=step.attempt,
                        requirement_ids=step.requirement_ids,
                        input_artifact_ids=step.input_artifact_ids,
                        output_artifact_ids=step.output_artifact_ids,
                        a2a_task_state=step.a2a_task_state,
                        workflow_state=WorkflowStatus.ABORTED,
                    )
                )

            connection.execute(
                "UPDATE workflow_runs SET status = ?, updated_at = ?, payload_json = ? "
                "WHERE run_id = ?",
                (
                    updated_run.status.value,
                    updated_run.updated_at.isoformat(),
                    _json_model(updated_run),
                    str(updated_run.run_id),
                ),
            )
            for event in event_list:
                _insert_event(connection, event)
        return updated_run

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._database_path, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    run_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE TABLE IF NOT EXISTS workflow_steps (
                    workflow_step_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE INDEX IF NOT EXISTS workflow_steps_by_run
                    ON workflow_steps(run_id, created_at, workflow_step_id);
                CREATE TABLE IF NOT EXISTS agent_contexts (
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    agent_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                    PRIMARY KEY(run_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS trace_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    occurred_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE INDEX IF NOT EXISTS trace_events_by_run
                    ON trace_events(run_id, sequence);
                CREATE TRIGGER IF NOT EXISTS trace_events_no_update
                    BEFORE UPDATE ON trace_events
                    BEGIN SELECT RAISE(ABORT, 'trace_events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS trace_events_no_delete
                    BEFORE DELETE ON trace_events
                    BEGIN SELECT RAISE(ABORT, 'trace_events are append-only'); END;
                CREATE TABLE IF NOT EXISTS run_configurations (
                    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE TRIGGER IF NOT EXISTS run_configurations_no_update
                    BEFORE UPDATE ON run_configurations
                    BEGIN SELECT RAISE(ABORT, 'run_configurations are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS run_configurations_no_delete
                    BEFORE DELETE ON run_configurations
                    BEGIN SELECT RAISE(ABORT, 'run_configurations are immutable'); END;
                CREATE UNIQUE INDEX IF NOT EXISTS run_configuration_artifact_ids
                    ON run_configurations(COALESCE(json_extract(payload_json,'$.artifact_id'),
                                                  json_extract(payload_json,'$.artifactId')));
                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL UNIQUE REFERENCES workflow_runs(run_id),
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE TRIGGER IF NOT EXISTS workspaces_no_update
                    BEFORE UPDATE ON workspaces
                    BEGIN SELECT RAISE(ABORT, 'workspaces are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS workspaces_no_delete
                    BEFORE DELETE ON workspaces
                    BEGIN SELECT RAISE(ABORT, 'workspaces are immutable'); END;
                CREATE TABLE IF NOT EXISTS control_leases (
                    run_id TEXT PRIMARY KEY REFERENCES workflow_runs(run_id),
                    token TEXT NOT NULL, owner_pid INTEGER
                );
                CREATE TABLE IF NOT EXISTS tool_attempts (
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    workflow_step_id TEXT NOT NULL REFERENCES workflow_steps(workflow_step_id),
                    execution_id TEXT NOT NULL, attempt INTEGER NOT NULL CHECK(attempt BETWEEN 0 AND 2),
                    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
                    PRIMARY KEY(run_id, execution_id, attempt)
                );
                CREATE TRIGGER IF NOT EXISTS tool_attempts_no_update
                    BEFORE UPDATE ON tool_attempts
                    BEGIN SELECT RAISE(ABORT, 'tool_attempts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS tool_attempts_no_delete
                    BEFORE DELETE ON tool_attempts
                    BEGIN SELECT RAISE(ABORT, 'tool_attempts are append-only'); END;
                CREATE TABLE IF NOT EXISTS issue_records (
                    issue_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    fingerprint TEXT NOT NULL,
                    code_version INTEGER NOT NULL CHECK (code_version >= 1),
                    consecutive_repeat_count INTEGER NOT NULL CHECK (
                        consecutive_repeat_count >= 0
                    ),
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json))
                );
                CREATE INDEX IF NOT EXISTS issue_records_by_run
                    ON issue_records(run_id, created_at, issue_id);
                CREATE INDEX IF NOT EXISTS issue_records_by_fingerprint
                    ON issue_records(run_id, fingerprint, code_version);
                CREATE TRIGGER IF NOT EXISTS issue_records_no_update
                    BEFORE UPDATE ON issue_records
                    BEGIN SELECT RAISE(ABORT, 'issue_records are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS issue_records_no_delete
                    BEFORE DELETE ON issue_records
                    BEGIN SELECT RAISE(ABORT, 'issue_records are append-only'); END;
                CREATE TABLE IF NOT EXISTS issue_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    issue_id TEXT NOT NULL REFERENCES issue_records(issue_id),
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL CHECK(json_valid(payload_json))
                );
                CREATE TRIGGER IF NOT EXISTS issue_events_no_update
                    BEFORE UPDATE ON issue_events
                    BEGIN SELECT RAISE(ABORT, 'issue_events are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS issue_events_no_delete
                    BEFORE DELETE ON issue_events
                    BEGIN SELECT RAISE(ABORT, 'issue_events are append-only'); END;
                CREATE TABLE IF NOT EXISTS project_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES workflow_runs(run_id),
                    artifact_type TEXT NOT NULL CHECK (
                        artifact_type IN ('SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT')
                    ),
                    artifact_version INTEGER NOT NULL CHECK (artifact_version >= 1),
                    payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
                    UNIQUE(run_id, artifact_type, artifact_version)
                );
                CREATE INDEX IF NOT EXISTS project_artifacts_by_run
                    ON project_artifacts(run_id, artifact_type, artifact_version);
                CREATE TRIGGER IF NOT EXISTS project_artifacts_no_update
                    BEFORE UPDATE ON project_artifacts
                    BEGIN SELECT RAISE(ABORT, 'project_artifacts are append-only'); END;
                CREATE TRIGGER IF NOT EXISTS project_artifacts_no_delete
                    BEFORE DELETE ON project_artifacts
                    BEGIN SELECT RAISE(ABORT, 'project_artifacts are append-only'); END;
                """
            )
            self._migrate_validation_artifact_types(connection)
            lease_columns = {row[1] for row in connection.execute("PRAGMA table_info(control_leases)")}
            if "owner_pid" not in lease_columns:
                connection.execute("ALTER TABLE control_leases ADD COLUMN owner_pid INTEGER")
            self._backfill_run_resources(connection)

    def _backfill_run_resources(self, connection: sqlite3.Connection) -> None:
        """Materialize a stable workspace ID once for payloads saved before this version."""
        from orchestrator.domain.run_configuration import RunConfigurationArtifact
        from orchestrator.domain.workspaces import WorkspaceRecord
        columns = {row[1] for row in connection.execute("PRAGMA table_info(workflow_runs)")}
        if "payload_json" not in columns:
            # Isolated Artifact migrations may reference a minimal historical Run table.
            return
        connection.execute("BEGIN IMMEDIATE")
        try:
            for row in connection.execute("SELECT run_id, payload_json FROM workflow_runs").fetchall():
                payload = json.loads(row[1])
                config_row = connection.execute(
                    "SELECT payload_json FROM run_configurations WHERE run_id=?", (row[0],),
                ).fetchone()
                config = RunConfigurationArtifact.model_validate_json(config_row[0]) if config_row else None
                workspace_row = connection.execute(
                    "SELECT workspace_id,payload_json FROM workspaces WHERE run_id=?", (row[0],),
                ).fetchone()
                workspace = WorkspaceRecord.model_validate_json(workspace_row[1]) if workspace_row else None
                missing_workspace_id = "workspace_id" not in payload
                if missing_workspace_id:
                    historical_ids = {
                        resource.workspace_id for resource in (config, workspace) if resource is not None
                    }
                    if len(historical_ids) > 1:
                        raise ValueError("Historical Run Configuration and Workspace identities disagree")
                    # Existing immutable resources are authoritative; do not invent a new
                    # ID when a legacy Run payload lacks a field already stored elsewhere.
                    payload["workspace_id"] = str(next(iter(historical_ids), uuid4()))
                run = WorkflowRun.model_validate(payload)
                if str(run.run_id) != row[0]:
                    raise ValueError("Historical Run payload disagrees with its database identity")
                if config is not None and (
                    config.run_id != run.run_id or config.scenario_id != run.scenario_id
                    or config.workspace_id != run.workspace_id
                ):
                    raise ValueError("Historical Run Configuration belongs to another Run or Workspace")
                if workspace is not None and (
                    workspace.run_id != run.run_id or workspace.workspace_id != run.workspace_id
                    or str(workspace.workspace_id) != workspace_row[0]
                ):
                    raise ValueError("Historical Workspace identity differs from the Run")
                if missing_workspace_id:
                    # Preserve legacy contents byte-for-byte apart from the new identity.
                    connection.execute("UPDATE workflow_runs SET payload_json=? WHERE run_id=?",
                                       (json.dumps(payload), row[0]))
                if config is None:
                    config = RunConfigurationArtifact(run_id=run.run_id, scenario_id=run.scenario_id,
                                                       workspace_id=run.workspace_id)
                    connection.execute("INSERT INTO run_configurations(run_id,payload_json) VALUES (?,?)",
                                       (row[0], _sanitized_json(config)))
                if workspace is None:
                    workspace = WorkspaceRecord(
                        run_id=run.run_id, workspace_id=run.workspace_id,
                        root_path=str((self.database_path.parent / "workspaces" / str(run.workspace_id)).resolve()),
                    )
                    connection.execute("INSERT INTO workspaces(workspace_id,run_id,payload_json) VALUES (?,?,?)",
                                       (str(workspace.workspace_id), row[0], workspace.model_dump_json()))
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _migrate_validation_artifact_types(connection: sqlite3.Connection) -> None:
        """Widen the append-only Artifact type check without losing existing rows."""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_artifacts'"
        ).fetchone()
        if row is None or all(name in row[0] for name in ("QA_REPORT", "SECURITY_REPORT", "REQUIREMENT")):
            return

        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("DROP TRIGGER IF EXISTS project_artifacts_no_update")
            connection.execute("DROP TRIGGER IF EXISTS project_artifacts_no_delete")
            connection.execute(
                "CREATE TABLE project_artifacts_v12 ("
                "artifact_id TEXT PRIMARY KEY, "
                "run_id TEXT NOT NULL REFERENCES workflow_runs(run_id), "
                "artifact_type TEXT NOT NULL CHECK (artifact_type IN ("
                "'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT', 'REQUIREMENT')), "
                "artifact_version INTEGER NOT NULL CHECK (artifact_version >= 1), "
                "payload_json TEXT NOT NULL CHECK (json_valid(payload_json)), "
                "UNIQUE(run_id, artifact_type, artifact_version))"
            )
            connection.execute(
                "INSERT INTO project_artifacts_v12 "
                "SELECT artifact_id, run_id, artifact_type, artifact_version, payload_json "
                "FROM project_artifacts"
            )
            connection.execute("DROP TABLE project_artifacts")
            connection.execute(
                "ALTER TABLE project_artifacts_v12 RENAME TO project_artifacts"
            )
            connection.execute(
                "CREATE INDEX project_artifacts_by_run "
                "ON project_artifacts(run_id, artifact_type, artifact_version)"
            )
            connection.execute(
                "CREATE TRIGGER project_artifacts_no_update BEFORE UPDATE ON project_artifacts "
                "BEGIN SELECT RAISE(ABORT, 'project_artifacts are append-only'); END"
            )
            connection.execute(
                "CREATE TRIGGER project_artifacts_no_delete BEFORE DELETE ON project_artifacts "
                "BEGIN SELECT RAISE(ABORT, 'project_artifacts are append-only'); END"
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise


_TERMINAL_TASK_STATES = frozenset({
    A2ATaskState.COMPLETED, A2ATaskState.FAILED,
    A2ATaskState.CANCELED, A2ATaskState.REJECTED,
})


def _store_detected_issues(
    connection: sqlite3.Connection, run: WorkflowRun, issues: Sequence[IssueRecord],
) -> tuple[IssueRecord, ...]:
    stored_issues = []
    for issue in issues:
        if issue.run_id != run.run_id or issue.code_version != run.code_version:
            raise ValueError("Issue identity must match the current Run candidate")
        existing = connection.execute("SELECT payload_json FROM issue_records WHERE issue_id=?", (str(issue.issue_id),)).fetchone()
        if existing is not None:
            stored = _load_model(IssueRecord, existing[0])
            if (stored.run_id, stored.fingerprint, stored.code_version) != (issue.run_id, issue.fingerprint, issue.code_version):
                raise ValueError("Issue identity cannot be rewritten")
            stored_issues.append(stored)
            continue
        source_row = connection.execute(
            "SELECT artifact_type,payload_json FROM project_artifacts WHERE run_id=? AND artifact_id=?",
            (str(run.run_id), str(issue.source_artifact_id)),
        ).fetchone()
        if source_row is None or source_row[0] != "SOURCE":
            raise ValueError("Issue Source must be registered in this Run")
        source = _load_model(CodeSnapshotArtifact, source_row[1])
        if source.code_version != issue.code_version:
            raise ValueError("Issue Source must identify the current failed candidate")
        if not set(issue.requirement_ids).issubset(set(source.requirement_ids)):
            raise ValueError("Issue references a Requirement outside the Plan")
        report_step_id = source.workflow_step_id
        if issue.report_artifact_id is not None:
            report_row = connection.execute(
                "SELECT artifact_type,payload_json FROM project_artifacts WHERE run_id=? AND artifact_id=?",
                (str(run.run_id), str(issue.report_artifact_id)),
            ).fetchone()
            expected_type = {
                AgentRole.DEVELOPER: "BUILD_REPORT", AgentRole.QA: "QA_REPORT",
                AgentRole.SECURITY: "SECURITY_REPORT",
            }.get(issue.reporter)
            if report_row is None or report_row[0] != expected_type:
                raise ValueError("Issue Report reference must match its reporter and candidate")
            report = json.loads(report_row[1])
            # Artifact lineage increments only when that report is produced. A prior
            # failed Build can leave the first QA report at version 1 for code 2.
            if report.get("code_version") != issue.code_version:
                raise ValueError("Issue Report reference must match its reporter and candidate")
            if report.get("execution_manifest", {}).get("project_artifact_id") != str(source.artifact_id):
                raise ValueError("Issue Report refers to another Source Snapshot")
            report_step_id = UUID(report["workflow_step_id"])
        previous = connection.execute(
            "SELECT consecutive_repeat_count,code_version FROM issue_records WHERE run_id=? AND fingerprint=? "
            "ORDER BY code_version DESC,created_at DESC,issue_id DESC LIMIT 1",
            (str(run.run_id), issue.fingerprint),
        ).fetchone()
        repeats = previous[0] + 1 if previous is not None and previous[1] == issue.code_version - 1 else 0
        stored = IssueRecord.model_validate({
            **_sanitize_storage_value(issue.model_dump(mode="json")),
            "consecutive_repeat_count": repeats,
        })
        connection.execute(
            "INSERT INTO issue_records(issue_id,run_id,fingerprint,code_version,consecutive_repeat_count,created_at,payload_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(stored.issue_id),str(run.run_id),stored.fingerprint,stored.code_version,
             stored.consecutive_repeat_count,stored.created_at.isoformat(),_sanitized_json(stored)),
        )
        for event_type in ("ISSUE_CREATED", "ISSUE_DETECTED"):
            _insert_event(connection, TraceEvent(
                run_id=run.run_id, workflow_step_id=report_step_id, event_type=event_type,
                actor=stored.reporter.value, attempt=run.fix_attempt,
                requirement_ids=stored.requirement_ids, input_artifact_ids=[stored.source_artifact_id],
                output_artifact_ids=[stored.report_artifact_id] if stored.report_artifact_id else [],
                issue_id=stored.issue_id, code_version=stored.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=run.status,
            ))
        stored_issues.append(stored)
    return tuple(stored_issues)


def _insert_issue_event(connection, issue: IssueRecord, event_type: str, changes: dict) -> None:
    # Validate each projected append-only update before writing its journal row.
    IssueRecord.model_validate({**issue.model_dump(), **changes})
    connection.execute(
        "INSERT INTO issue_events(issue_id,run_id,event_type,payload_json) VALUES (?,?,?,?)",
        (str(issue.issue_id), str(issue.run_id), event_type,
         json.dumps(_sanitize_storage_value(changes))),
    )


def _fixed_issues_for_candidate(connection, run, code_version):
    if run.fix_attempt == 0:
        return ()
    rows = connection.execute(
        "SELECT payload_json FROM issue_records WHERE run_id=? AND code_version < ?",
        (str(run.run_id), code_version),
    ).fetchall()
    issues = []
    for row in rows:
        issue = _load_model(IssueRecord, row[0])
        fix_event = connection.execute(
            "SELECT payload_json FROM issue_events WHERE issue_id=? AND event_type='FIX_REQUESTED' "
            "ORDER BY sequence DESC LIMIT 1", (str(issue.issue_id),),
        ).fetchone()
        if fix_event is None or json.loads(fix_event[0])["new_code_version"] != code_version:
            continue
        issues.append(issue)
    return tuple(issues)


def _record_build_revalidation_results(connection, run, build_report) -> None:
    evidence = build_report.tool_evidence
    outcome = build_report.execution_outcome.value
    # No guessed resolution from exitCode or a later QA dispatch: only the matching
    # actual Build execution can resolve the previous Build Issue.
    if evidence is None or evidence.outcome.value != "PASS":
        outcome = "UNVERIFIED"
    for issue in _fixed_issues_for_candidate(connection, run, build_report.code_version):
        if issue.reporter == AgentRole.DEVELOPER:
            _insert_issue_event(connection, issue, "REVALIDATION_FINISHED", {"revalidation_result": outcome})


def _security_finding_revalidation(connection, issue, security_report) -> str:
    original = None
    if issue.report_artifact_id is not None:
        report_row = connection.execute(
            "SELECT payload_json FROM project_artifacts WHERE run_id=? AND artifact_id=? AND artifact_type='SECURITY_REPORT'",
            (str(issue.run_id), str(issue.report_artifact_id)),
        ).fetchone()
        if report_row is not None:
            original_report = _load_model(SecurityReportArtifact, report_row[0])
            original = next((finding for finding in original_report.findings if finding.finding_id == issue.reference_id), None)
    if original is not None and original.rule_id:
        location = (original.normalized_location or issue.normalized_location).strip().casefold()
        matching = [finding for finding in security_report.findings
                    if finding.requirement_id == original.requirement_id
                    and finding.rule_id == original.rule_id
                    and (finding.normalized_location or finding.rule_id or finding.finding_id).strip().casefold() == location]
    else:
        # Older reports did not carry scanner rule identities; preserve their exact
        # finding-ID matching instead of pretending to infer a stable identity.
        matching = [finding for finding in security_report.findings if finding.finding_id == issue.reference_id]
    results = [result for result in security_report.requirement_results if result.requirement_id in issue.requirement_ids]
    # An unavailable or unproven scan cannot demonstrate that an absent finding
    # disappeared. A different, actually evaluated requirement failure remains a
    # separate Issue rather than turning this resolved finding into a false FAIL.
    if not results or any(
        result.outcome.value == "UNVERIFIED"
        or result.tool_evidence is None
        or result.tool_evidence.tool_name != "run_security_scan"
        or result.tool_evidence.execution_manifest != security_report.execution_manifest
        or result.tool_evidence.outcome.value != "PASS"
        for result in results
    ):
        return "UNVERIFIED"
    if any(finding.disposition.value == "CONFIRMED" for finding in matching):
        return "FAIL"
    if any(finding.disposition.value in {"SUSPECTED", "UNVERIFIED"} for finding in matching):
        return "UNVERIFIED"
    return "PASS"


def _record_revalidation_results(connection, run, qa_report, security_report) -> None:
    def proven_outcome(result, report, allowed_tools) -> str:
        evidence = result.tool_evidence
        if (
            result.outcome.value not in {"PASS", "FAIL"}
            or evidence is None
            or evidence.tool_name not in allowed_tools
            or evidence.execution_manifest != report.execution_manifest
            or evidence.outcome.value != "PASS"
        ):
            return "UNVERIFIED"
        # Tool PASS proves the check executed, not that its assertions passed.
        return result.outcome.value

    for issue in _fixed_issues_for_candidate(connection, run, qa_report.code_version):
        if issue.reporter == AgentRole.DEVELOPER:
            continue  # Already evaluated by the candidate's own Build transaction.
        if issue.reporter == AgentRole.QA:
            matching = [proven_outcome(test, qa_report, {"run_unit_tests", "run_browser_tests"})
                        for test in qa_report.tests
                        if test.requirement_id in issue.requirement_ids
                        and test.test_id == issue.reference_id]
            outcome = _combined_outcome(matching)
        elif issue.category.startswith("SECURITY_FINDING"):
            outcome = _security_finding_revalidation(connection, issue, security_report)
        else:
            matching = [proven_outcome(result, security_report, {"run_security_scan"})
                        for result in security_report.requirement_results
                        if result.requirement_id in issue.requirement_ids]
            outcome = _combined_outcome(matching)
        _insert_issue_event(connection, issue, "REVALIDATION_FINISHED", {"revalidation_result": outcome})


def _combined_outcome(outcomes: Sequence[str]) -> str:
    if "FAIL" in outcomes:
        return "FAIL"
    return "PASS" if outcomes and all(value == "PASS" for value in outcomes) else "UNVERIFIED"


def _insert_artifact(connection, artifact, workflow_state) -> None:
    if connection.execute(
        "SELECT 1 FROM run_configurations WHERE COALESCE(json_extract(payload_json,'$.artifact_id'),"
        "json_extract(payload_json,'$.artifactId'))=?",
        (str(artifact.artifact_id),),
    ).fetchone() is not None:
        raise ValueError("Project Artifact ID is already registered as a Run Configuration")
    connection.execute(
        "INSERT INTO project_artifacts(artifact_id,run_id,artifact_type,artifact_version,payload_json) VALUES (?,?,?,?,?)",
        (str(artifact.artifact_id), str(artifact.run_id), artifact.artifact_type,
         artifact.artifact_version, _sanitized_json(artifact)),
    )
    _insert_event(connection, TraceEvent(
        run_id=artifact.run_id, workflow_step_id=artifact.workflow_step_id,
        a2a_task_id=artifact.a2a_task_id, event_type="ARTIFACT_REGISTERED",
        actor=artifact.created_by.value, attempt=0,
        requirement_ids=list(artifact.requirement_ids), output_artifact_ids=[artifact.artifact_id],
        code_version=getattr(artifact, "code_version", None), workflow_state=workflow_state,
    ))


def _record_terminal_events(connection, run) -> None:
    if run.verdict is not None and run.status in (WorkflowStatus.FINISHED, WorkflowStatus.HUMAN_REVIEW):
        _insert_event(connection, TraceEvent(
            run_id=run.run_id, event_type="VERDICT_CREATED", actor="Orchestrator",
            attempt=run.fix_attempt, code_version=run.code_version, workflow_state=run.status,
        ))
    if run.status == WorkflowStatus.FINISHED:
        _insert_event(connection, TraceEvent(
            run_id=run.run_id, event_type="RUN_FINISHED", actor="Orchestrator",
            attempt=run.fix_attempt, code_version=run.code_version, workflow_state=run.status,
        ))


_OPAQUE_FIELDS = frozenset({
    "a2a_task_id", "a2aTaskId", "a2a_artifact_id", "a2aArtifactId", "agent_context_id", "agentContextId",
    "a2a_artifact_ids", "a2aArtifactIds", "latest_a2a_task_id", "agent_id", "agentId",
    "path", "root_path",
    "fingerprint", "snapshot_sha256", "snapshotSha256", "commit_hash", "commitHash", "tree_hash", "treeHash",
    "container_image_digest", "containerImageDigest", "dependency_lock_hash", "dependencyLockHash",
})

_REFERENCE_FIELDS = frozenset({
    "artifact_uri", "artifactUri", "evidence_refs", "evidenceRefs", "evidence_ref", "evidenceRef",
    "stdout_ref", "stdoutRef", "stderr_ref", "stderrRef",
})


def _redact_reference(value):
    """Keep routing paths intact, but never persist credential-bearing URI metadata."""
    if isinstance(value, (tuple, list)):
        return [_redact_reference(item) for item in value]
    if not isinstance(value, str):
        return value
    try:
        parts = urlsplit(value)
    except ValueError:
        return redact_text(value)
    if not parts.scheme:
        return redact_text(value)
    query_parts = []
    for parameter in parts.query.split("&"):
        name, separator, original = parameter.partition("=")
        if not separator:
            query_parts.append(parameter)
            continue
        decoded = unquote_plus(original)
        masked = redact_data({unquote_plus(name): decoded})[unquote_plus(name)]
        query_parts.append(parameter if masked == decoded else name + "=" + quote(masked, safe="[]"))
    netloc = parts.netloc
    if "@" in netloc:
        userinfo, host = netloc.rsplit("@", 1)
        username, separator, _ = userinfo.partition(":")
        if separator:
            netloc = username + ":[REDACTED]@" + host
    # Fragments can hold access-token assignments as well; ordinary bytes stay unchanged.
    fragment = redact_text(parts.fragment)
    if netloc == parts.netloc and "&".join(query_parts) == parts.query and fragment == parts.fragment:
        return value
    return urlunsplit((parts.scheme, netloc, parts.path, "&".join(query_parts), fragment))


def _sanitize_storage_value(value, key: str = ""):
    if key in _OPAQUE_FIELDS:
        return value
    if key in _REFERENCE_FIELDS:
        return _redact_reference(value)
    if isinstance(value, dict):
        result = {}
        for name, item in value.items():
            if name in _OPAQUE_FIELDS or name in _REFERENCE_FIELDS:
                result[name] = _sanitize_storage_value(item, name)
            elif redact_data({name: None})[name] is not None:
                result[name] = redact_data({name: item})[name]
            else:
                result[name] = _sanitize_storage_value(item, name)
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_storage_value(item, key) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def _sanitized_json(model) -> str:
    return json.dumps(_sanitize_storage_value(model.model_dump(mode="json")), separators=(",", ":"))


def _validate_bundle(
    run: WorkflowRun,
    steps: Sequence[WorkflowStep],
    events: Sequence[TraceEvent],
) -> None:
    step_ids: set[UUID] = set()
    for step in steps:
        if step.run_id != run.run_id:
            raise ValueError("all WorkflowSteps must belong to the created Run")
        if step.workflow_step_id in step_ids:
            raise ValueError("WorkflowStep IDs must be unique")
        step_ids.add(step.workflow_step_id)
    _validate_events_for_run(run, events)
    for event in events:
        if event.workflow_step_id is not None and event.workflow_step_id not in step_ids:
            raise ValueError("initial Trace events must reference an included WorkflowStep")


def _validate_events_for_run(run: WorkflowRun, events: Sequence[TraceEvent]) -> None:
    for event in events:
        if event.run_id != run.run_id:
            raise ValueError("Trace event must belong to the updated Run")


def _validate_task_update(
    run: WorkflowRun,
    step: WorkflowStep,
    context: AgentContext,
    event: TraceEvent,
) -> None:
    if step.run_id != run.run_id or context.run_id != run.run_id:
        raise ValueError("Run, WorkflowStep, and AgentContext must share run_id")
    if event.run_id != run.run_id or event.workflow_step_id != step.workflow_step_id:
        raise ValueError("Trace event must reference the same Run and WorkflowStep")
    if context.agent_context_id != step.agent_context_id:
        raise ValueError("WorkflowStep and AgentContext context IDs must match")
    if context.latest_a2a_task_id != step.a2a_task_id:
        raise ValueError("WorkflowStep and AgentContext Task IDs must match")


def _run_row(run: WorkflowRun) -> tuple[str, str, str, str, str]:
    return (
        str(run.run_id),
        run.status.value,
        run.created_at.isoformat(),
        run.updated_at.isoformat(),
        _json_model(run),
    )


def _insert_step(connection: sqlite3.Connection, step: WorkflowStep) -> None:
    connection.execute(
        "INSERT INTO workflow_steps(workflow_step_id, run_id, status, created_at, updated_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        _step_row(step),
    )


def _upsert_step(connection: sqlite3.Connection, step: WorkflowStep) -> None:
    connection.execute(
        "INSERT INTO workflow_steps(workflow_step_id, run_id, status, created_at, updated_at, payload_json) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(workflow_step_id) DO UPDATE SET "
        "status=excluded.status, updated_at=excluded.updated_at, payload_json=excluded.payload_json "
        "WHERE workflow_steps.run_id = excluded.run_id",
        _step_row(step),
    )
    row = connection.execute(
        "SELECT run_id FROM workflow_steps WHERE workflow_step_id = ?",
        (str(step.workflow_step_id),),
    ).fetchone()
    if row is None or row[0] != str(step.run_id):
        raise ValueError("WorkflowStep ID is already owned by another Run")


def _step_row(step: WorkflowStep) -> tuple[str, str, str, str, str, str]:
    return (
        str(step.workflow_step_id),
        str(step.run_id),
        step.status.value,
        step.created_at.isoformat(),
        step.updated_at.isoformat(),
        _json_model(step),
    )


def _upsert_context(connection: sqlite3.Connection, context: AgentContext) -> None:
    connection.execute(
        "INSERT INTO agent_contexts(run_id, agent_id, payload_json) VALUES (?, ?, ?) "
        "ON CONFLICT(run_id, agent_id) DO UPDATE SET payload_json=excluded.payload_json",
        (str(context.run_id), context.agent_id, _json_model(context)),
    )


def _insert_event(connection: sqlite3.Connection, event: TraceEvent) -> None:
    connection.execute(
        "INSERT INTO trace_events(event_id, run_id, occurred_at, event_type, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            str(event.event_id),
            str(event.run_id),
            event.occurred_at.isoformat(),
            event.event_type,
            json.dumps(_sanitize_storage_value(event.to_trace_json()), separators=(",", ":")),
        ),
    )


def _json_model(model: WorkflowRun | WorkflowStep | AgentContext) -> str:
    return _sanitized_json(model)


def _load_model(model_type, payload: str):
    return model_type.model_validate(_sanitize_storage_value(json.loads(payload)))
