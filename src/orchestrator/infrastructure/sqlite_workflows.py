"""SQLite persistence for Workflow runs, Agent state, and append-only traces."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Awaitable, Callable, Iterator, Sequence
from uuid import UUID

from orchestrator.domain import (
    A2ATaskState,
    AgentContext,
    AgentRole,
    MAX_CODE_FIX_ATTEMPTS,
    BuildReportArtifact,
    ChangeReportArtifact,
    CodeSnapshotArtifact,
    TraceEvent,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
    code_version_for_fix_attempt,
    transition_run,
)
from orchestrator.domain.models import utc_now


class RunNotFoundError(LookupError):
    """Raised when a requested Workflow Run does not exist."""


class ActiveAgentTaskError(RuntimeError):
    """Raised when local cancellation cannot safely cancel an active remote Task."""


class RunDispatchConflict(RuntimeError):
    """Raised when a Run is not eligible for its one initial Planner dispatch."""


ProjectArtifact = CodeSnapshotArtifact | ChangeReportArtifact | BuildReportArtifact


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
    ) -> None:
        _validate_bundle(run, steps, events)
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
                run.status != WorkflowStatus.IMPLEMENTING
                or len(developer_steps) != 1
                or developer_steps[0].status != WorkflowStepStatus.SUCCEEDED
                or developer_steps[0].a2a_task_state != A2ATaskState.COMPLETED
                or developer_steps[0].a2a_task_id != source.a2a_task_id
                or any(step.agent_role in (AgentRole.QA, AgentRole.SECURITY) for step in steps)
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
                connection.execute(
                    "INSERT INTO project_artifacts("
                    "artifact_id, run_id, artifact_type, artifact_version, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(artifact.artifact_id),
                        str(run_id),
                        artifact.artifact_type,
                        artifact.artifact_version,
                        artifact.model_dump_json(),
                    ),
                )

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
            snapshot_ready_run = transition_run(
                run_with_code_version, WorkflowStatus.SNAPSHOT_READY
            )
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
                    workflow_state=snapshot_ready_run.status,
                ),
            )
            _insert_event(
                connection,
                TraceEvent(
                    run_id=run_id,
                    event_type="WORKFLOW_STATE_CHANGED",
                    actor="Orchestrator",
                    attempt=0,
                    workflow_state=snapshot_ready_run.status,
                ),
            )

            validation_steps: tuple[WorkflowStep, ...] = ()
            if not build_report.passed:
                if snapshot_ready_run.fix_attempt >= MAX_CODE_FIX_ATTEMPTS:
                    final_run = transition_run(
                        snapshot_ready_run, WorkflowStatus.HUMAN_REVIEW
                    )
                    event_type = "BUILD_FAILED_FIX_LIMIT_REACHED"
                else:
                    final_run = transition_run(
                        snapshot_ready_run, WorkflowStatus.FIX_REQUIRED
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
                validating_run = transition_run(
                    snapshot_ready_run, WorkflowStatus.VALIDATING
                )
                validation_steps = tuple(
                    WorkflowStep(
                        run_id=run_id,
                        agent_role=role,
                        status=(
                            WorkflowStepStatus.RUNNING
                            if validation_agents_configured
                            else WorkflowStepStatus.PENDING
                        ),
                        requirement_ids=developer_step.requirement_ids,
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

        return final_run, updated_developer_step, validation_steps

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
        }
        return [
            _load_model(model_by_type[row[0]], row[1])
            for row in rows
        ]

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

    def transition_run_and_record(
        self,
        run_id: UUID,
        target: WorkflowStatus,
        *,
        additional_event_types: Sequence[str] = (),
        workflow_step_id: UUID | None = None,
        attempt: int = 0,
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
            updated_run = transition_run(current_run, target)
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
            json.dumps(event.to_trace_json(), separators=(",", ":")),
        ),
    )


def _json_model(model: WorkflowRun | WorkflowStep | AgentContext) -> str:
    return model.model_dump_json()


def _load_model(model_type, payload: str):
    return model_type.model_validate_json(payload)
