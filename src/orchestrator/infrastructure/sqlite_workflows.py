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
        validation_requirement_ids: dict[AgentRole, Sequence[UUID]] | None = None,
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
            is_fix = run.status == WorkflowStatus.FIXING
            intermediate_state = (
                WorkflowStatus.REVALIDATING if is_fix else WorkflowStatus.SNAPSHOT_READY
            )
            intermediate_run = transition_run(run_with_code_version, intermediate_state)
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
            if not build_report.passed:
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

            stored_issues: list[IssueRecord] = []
            for issue in issues:
                previous_issue = connection.execute(
                    "SELECT fingerprint, consecutive_repeat_count, code_version "
                    "FROM issue_records WHERE run_id = ? AND fingerprint = ? "
                    "ORDER BY code_version DESC, created_at DESC, issue_id DESC LIMIT 1",
                    (str(run_id), issue.fingerprint),
                ).fetchone()
                if issue.code_version != run.code_version:
                    raise ValueError("Issue codeVersion must match the current failed candidate")
                if not set(issue.requirement_ids).issubset(set(previous_developer.requirement_ids)):
                    raise ValueError("Issue references a Requirement outside the Plan")
                source_row = connection.execute(
                    "SELECT artifact_type, artifact_version FROM project_artifacts "
                    "WHERE run_id = ? AND artifact_id = ?",
                    (str(run_id), str(issue.source_artifact_id)),
                ).fetchone()
                if source_row is None or tuple(source_row) != ("SOURCE", run.code_version):
                    raise ValueError("Issue Source reference must be the current Run candidate")
                if issue.report_artifact_id is not None:
                    report_row = connection.execute(
                        "SELECT artifact_type, artifact_version FROM project_artifacts "
                        "WHERE run_id = ? AND artifact_id = ?",
                        (str(run_id), str(issue.report_artifact_id)),
                    ).fetchone()
                    expected_report_types = {
                        AgentRole.DEVELOPER: {"BUILD_REPORT"},
                        AgentRole.QA: {"QA_REPORT"},
                        AgentRole.SECURITY: {"SECURITY_REPORT"},
                    }[issue.reporter]
                    if (
                        report_row is None
                        or report_row[0] not in expected_report_types
                        or report_row[1] != run.code_version
                    ):
                        raise ValueError("Issue Report reference must match its reporter and candidate")
                repeat_count = (
                    int(previous_issue[1]) + 1
                    if previous_issue is not None
                    and previous_issue[0] == issue.fingerprint
                    and int(previous_issue[2]) == run.code_version - 1
                    else 0
                )
                stored = IssueRecord.model_validate(
                    {
                        **issue.model_dump(mode="python", by_alias=False),
                        "consecutive_repeat_count": repeat_count,
                    }
                )
                connection.execute(
                    "INSERT INTO issue_records(issue_id, run_id, fingerprint, code_version, "
                    "consecutive_repeat_count, created_at, payload_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(stored.issue_id), str(run_id), stored.fingerprint,
                        stored.code_version, stored.consecutive_repeat_count,
                        stored.created_at.isoformat(),
                        stored.model_dump_json(by_alias=True),
                    ),
                )
                stored_issues.append(stored)
                _insert_event(
                    connection,
                    TraceEvent(
                        run_id=run_id,
                        workflow_step_id=previous_developer.workflow_step_id,
                        event_type="ISSUE_CREATED",
                        actor=stored.reporter.value,
                        attempt=run.fix_attempt,
                        requirement_ids=stored.requirement_ids,
                        input_artifact_ids=[stored.source_artifact_id],
                        output_artifact_ids=(
                            [stored.report_artifact_id]
                            if stored.report_artifact_id is not None else []
                        ),
                        issue_id=stored.issue_id,
                        code_version=stored.code_version,
                        workflow_state=run.status,
                    ),
                )

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
                input_artifact_ids = list(by_type.values())
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
        return final_run, step, tuple(stored_issues), input_artifact_ids

    def list_issue_records(self, run_id: UUID) -> list[IssueRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM issue_records WHERE run_id = ? "
                "ORDER BY created_at, issue_id",
                (str(run_id),),
            ).fetchall()
        return [IssueRecord.model_validate_json(row[0]) for row in rows]

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
                connection.execute(
                    "INSERT INTO project_artifacts("
                    "artifact_id, run_id, artifact_type, artifact_version, payload_json) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        str(artifact.artifact_id), str(run_id), artifact.artifact_type,
                        artifact.artifact_version, artifact.model_dump_json(),
                    ),
                )
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

    @staticmethod
    def _migrate_validation_artifact_types(connection: sqlite3.Connection) -> None:
        """Widen the append-only Artifact type check without losing existing rows."""
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'project_artifacts'"
        ).fetchone()
        if row is None or "QA_REPORT" in row[0] and "SECURITY_REPORT" in row[0]:
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
                "'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT')), "
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
