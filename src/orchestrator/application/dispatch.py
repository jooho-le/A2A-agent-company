"""Run-to-Agent dispatch across Planner, Developer, QA, and Security stages."""

import asyncio
import json
import logging
from collections.abc import Callable
from uuid import UUID

from a2a.types import Task

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.a2a_tasks import A2ATaskRunner, TaskRunDisposition
from orchestrator.application.planner_output import (
    PlannerOutputValidationError,
    PlannerPlan,
    parse_planner_output,
)
from orchestrator.application.developer_output import (
    DeveloperOutputValidationError,
    parse_developer_output,
)
from orchestrator.domain import (
    AgentRole,
    SnapshotHandoff,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
)
from orchestrator.infrastructure import (
    RunDispatchConflict,
    RunNotFoundError,
    SQLiteWorkflowRepository,
)


logger = logging.getLogger(__name__)
ClientFactory = Callable[[str], A2AAgentClient]
_DEVELOPER_OUTPUT_CONTRACT: dict[str, object] = {
    "schemaVersion": 1,
    "requiredArtifactNames": [
        "source-snapshot.json",
        "change-report.json",
        "build-report.json",
    ],
    "partMediaType": "application/json",
    "artifactMetadataFields": [
        "runId",
        "workflowStepId",
        "projectArtifactId",
        "artifactVersion",
    ],
    "dataSchemas": {
        "source-snapshot.json": "schemas/project/developer_source_snapshot.schema.json",
        "change-report.json": "schemas/project/developer_change_report.schema.json",
        "build-report.json": "schemas/project/developer_build_report.schema.json",
    },
    "buildTool": {
        "name": "run_build",
        "requiredResultFields": ["exitCode", "durationMs", "executionManifestId"],
        "executor": "Developer Agent MCP client",
    },
    "requiredInvariants": [
        "All result Artifacts refer to the current Run, Developer Step, A2A Task, "
        "requirements and codeVersion.",
        "Build Report sourceArtifactId and executionManifest equal source-snapshot.json.",
        "Freeze the Source Snapshot before Build; do not mutate it after registration.",
    ],
    "executionBoundary": {
        "responsibility": (
            "Implement the validated Plan without changing the Requirements or "
            "Acceptance Criteria."
        ),
        "forbidden": [
            "Do not claim Build PASS without a successful run_build Tool result.",
            "Do not modify QA/Security protected tests or validation criteria.",
        ],
    },
}


class PlannerRunDispatcher:
    """Dispatch one Run through its configured Agents in Workflow order."""

    def __init__(
        self,
        repository: SQLiteWorkflowRepository,
        agent_registry: A2AAgentRegistry,
        *,
        client_factory: ClientFactory = A2AAgentClient,
    ) -> None:
        self._repository = repository
        self._agent_registry = agent_registry
        self._client_factory = client_factory

    async def dispatch_planner(self, run_id: UUID) -> None:
        """Claim the Planner Step, execute/poll its A2A Task, then map disposition."""
        try:
            run, step = self._repository.claim_planner_dispatch(run_id)
        except (RunDispatchConflict, RunNotFoundError):
            # Background delivery may be duplicated. The durable claim is one-shot.
            logger.info("Planner dispatch skipped for Run %s", run_id)
            return

        try:
            agent_url = self._agent_registry.require_base_url(AgentRole.PLANNER)
            async with self._client_factory(agent_url) as client:
                await client.resolve_agent_card()
                runner = A2ATaskRunner(client)
                result = await runner.submit_and_wait(
                    run,
                    step,
                    agent_id="planner",
                    payload={"request": run.request_text},
                    observer=self._repository.task_update_observer(run),
                )
        except Exception as exc:
            # Do not retry an uncertain SendMessage side effect automatically.
            logger.error(
                "Planner dispatch requires review for Run %s (error type: %s)",
                run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(run_id, step.workflow_step_id, step.attempt)
            return

        if result.disposition == TaskRunDisposition.COMPLETED:
            await self._advance_to_developer(run, result.step.workflow_step_id, result.task)
            return
        if result.disposition == TaskRunDisposition.WAITING_INPUT:
            try:
                self._repository.transition_run_and_record(
                    run_id,
                    WorkflowStatus.WAITING_INPUT,
                )
            except Exception as exc:
                self._log_transition_error(run_id, exc)
            return

        if result.disposition in (
            TaskRunDisposition.HUMAN_REVIEW,
            TaskRunDisposition.AGENT_FAILED,
            TaskRunDisposition.CANCELED,
            TaskRunDisposition.PROTOCOL_ERROR,
            TaskRunDisposition.POLLING_TIMEOUT,
        ):
            self._move_to_human_review(
                run_id,
                result.step.workflow_step_id,
                result.step.attempt,
            )

    async def _advance_to_developer(
        self, run: WorkflowRun, planner_step_id: UUID, planner_task: Task
    ) -> None:
        try:
            planner_output = parse_planner_output(
                planner_task,
                run_id=run.run_id,
                workflow_step_id=planner_step_id,
            )
        except PlannerOutputValidationError as exc:
            logger.warning(
                "Planner output requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc.__cause__ or exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                planner_step_id,
                0,
                additional_event_types=("PLANNER_OUTPUT_REJECTED",),
            )
            return

        developer_url = self._agent_registry.get_base_url(AgentRole.DEVELOPER)
        try:
            developer_run, developer_step = self._repository.create_developer_step_from_plan(
                run.run_id,
                planner_step_id,
                a2a_artifact_id=planner_output.a2a_artifact_id,
                requirement_ids=[
                    requirement.requirement_id
                    for requirement in planner_output.plan.requirements
                ],
                project_artifact_id=planner_output.project_artifact_id,
                developer_configured=developer_url is not None,
            )
        except Exception as exc:
            logger.error(
                "Could not create Developer Step for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                planner_step_id,
                0,
                additional_event_types=("PLANNER_OUTPUT_HANDOFF_FAILED",),
            )
            return

        if developer_url is None:
            logger.info("Developer dispatch requires configuration for Run %s", run.run_id)
            return

        payload = {
            "plan": planner_output.plan.model_dump(mode="json", by_alias=True),
            "sourceArtifact": {
                "a2aArtifactId": planner_output.a2a_artifact_id,
                "projectArtifactId": str(planner_output.project_artifact_id),
                "artifactVersion": planner_output.artifact_version,
            },
            "outputContract": _DEVELOPER_OUTPUT_CONTRACT,
        }
        try:
            async with self._client_factory(developer_url) as client:
                await client.resolve_agent_card()
                result = await A2ATaskRunner(client).submit_and_wait(
                    developer_run,
                    developer_step,
                    agent_id="developer",
                    payload=payload,
                    observer=self._repository.task_update_observer(developer_run),
                )
        except Exception as exc:
            logger.error(
                "Developer dispatch requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
            )
            return

        if result.disposition == TaskRunDisposition.WAITING_INPUT:
            try:
                self._repository.transition_run_and_record(
                    run.run_id,
                    WorkflowStatus.WAITING_INPUT,
                    workflow_step_id=result.step.workflow_step_id,
                    attempt=result.step.attempt,
                )
            except Exception as exc:
                self._log_transition_error(run.run_id, exc)
            return
        if result.disposition == TaskRunDisposition.COMPLETED:
            await self._advance_to_validation(
                developer_run,
                result.step,
                result.task,
                planner_output.plan,
            )
            return
        if result.disposition in (
            TaskRunDisposition.HUMAN_REVIEW,
            TaskRunDisposition.AGENT_FAILED,
            TaskRunDisposition.CANCELED,
            TaskRunDisposition.PROTOCOL_ERROR,
            TaskRunDisposition.POLLING_TIMEOUT,
        ):
            self._move_to_human_review(
                run.run_id,
                result.step.workflow_step_id,
                result.step.attempt,
            )

    async def _advance_to_validation(
        self,
        run: WorkflowRun,
        developer_step: WorkflowStep,
        developer_task: Task,
        plan: PlannerPlan,
    ) -> None:
        try:
            output = parse_developer_output(
                developer_task,
                run=run,
                step=developer_step,
            )
        except DeveloperOutputValidationError as exc:
            logger.warning(
                "Developer output requires review for Run %s (error type: %s)",
                run.run_id,
                type(exc.__cause__ or exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
                additional_event_types=("DEVELOPER_OUTPUT_REJECTED",),
            )
            return

        qa_url = self._agent_registry.get_base_url(AgentRole.QA)
        security_url = self._agent_registry.get_base_url(AgentRole.SECURITY)
        validators_configured = qa_url is not None and security_url is not None
        try:
            updated_run, _, validation_steps = self._repository.record_developer_candidate(
                run.run_id,
                developer_step.workflow_step_id,
                source=output.source,
                change_report=output.change_report,
                build_report=output.build_report,
                validation_agents_configured=validators_configured,
            )
        except Exception as exc:
            logger.error(
                "Could not register Developer Artifacts for Run %s (error type: %s)",
                run.run_id,
                type(exc).__name__,
            )
            self._move_to_human_review(
                run.run_id,
                developer_step.workflow_step_id,
                developer_step.attempt,
                additional_event_types=("DEVELOPER_ARTIFACT_REGISTRATION_FAILED",),
            )
            return

        if not output.build_report.passed or not validators_configured:
            return

        criteria = [
            {
                "requirementId": str(requirement.requirement_id),
                "key": requirement.key,
                "description": requirement.description,
                "acceptanceCriteria": requirement.acceptance_criteria,
            }
            for requirement in plan.requirements
        ]
        request_context = json.dumps(criteria, ensure_ascii=False, separators=(",", ":"))
        handoff = SnapshotHandoff.from_snapshot(output.source)
        await self._dispatch_validation_agents(
            updated_run,
            validation_steps,
            handoff,
            request_context=request_context,
            agent_urls={AgentRole.QA: qa_url, AgentRole.SECURITY: security_url},
        )

    async def _dispatch_validation_agents(
        self,
        run: WorkflowRun,
        steps: tuple[WorkflowStep, ...],
        handoff: SnapshotHandoff,
        *,
        request_context: str,
        agent_urls: dict[AgentRole, str | None],
    ) -> None:
        async def dispatch_one(step: WorkflowStep) -> TaskRunDisposition | Exception:
            role = step.agent_role
            agent_url = agent_urls[role]
            if agent_url is None:
                return RuntimeError(f"{role.value} Agent is not configured")
            duty = (
                "기능 테스트를 수행하고 QA 결과를 보고한다."
                if role == AgentRole.QA
                else "보안 취약점을 점검하고 Security 결과를 보고한다."
            )
            request_text = (
                "검증 대상은 전달된 불변 Source Snapshot이다. 파일을 수정하지 말고 "
                "READ_ONLY로 접근한다. 요구사항과 Acceptance Criteria를 확인해 "
                f"{duty} 요구사항: {request_context}"
            )
            try:
                async with self._client_factory(agent_url) as client:
                    await client.resolve_agent_card()
                    result = await A2ATaskRunner(client).submit_snapshot_and_wait(
                        run,
                        step,
                        handoff,
                        agent_id=role.value.lower(),
                        recipient=role,
                        request_text=request_text,
                        observer=self._repository.task_update_observer(run),
                    )
                return result.disposition
            except Exception as exc:
                logger.error(
                    "%s dispatch requires review for Run %s (error type: %s)",
                    role.value,
                    run.run_id,
                    type(exc).__name__,
                )
                return exc

        dispositions = await asyncio.gather(*(dispatch_one(step) for step in steps))
        if any(
            isinstance(outcome, Exception)
            or outcome != TaskRunDisposition.COMPLETED
            for outcome in dispositions
        ):
            step_id = next(
                (
                    step.workflow_step_id
                    for step, outcome in zip(steps, dispositions, strict=True)
                    if isinstance(outcome, Exception)
                    or outcome != TaskRunDisposition.COMPLETED
                ),
                steps[0].workflow_step_id,
            )
            self._move_to_human_review(
                run.run_id,
                step_id,
                0,
                additional_event_types=("QA_SECURITY_DISPATCH_REQUIRES_REVIEW",),
            )

    def _move_to_human_review(
        self,
        run_id: UUID,
        workflow_step_id: UUID,
        attempt: int,
        *,
        additional_event_types: tuple[str, ...] = ("A2A_DISPATCH_REQUIRES_REVIEW",),
    ) -> None:
        try:
            self._repository.transition_run_and_record(
                run_id,
                WorkflowStatus.HUMAN_REVIEW,
                additional_event_types=additional_event_types,
                workflow_step_id=workflow_step_id,
                attempt=attempt,
            )
        except Exception as exc:
            self._log_transition_error(run_id, exc)

    @staticmethod
    def _log_transition_error(run_id: UUID, error: Exception) -> None:
        logger.error(
            "Could not persist workflow disposition for Run %s (error type: %s)",
            run_id,
            type(error).__name__,
        )
