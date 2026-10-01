"""Run-to-Agent dispatch for the initial Planner stage."""

import logging
from collections.abc import Callable
from uuid import UUID

from a2a.types import Task

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.a2a_tasks import A2ATaskRunner, TaskRunDisposition
from orchestrator.application.planner_output import (
    PlannerOutputValidationError,
    parse_planner_output,
)
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStatus
from orchestrator.infrastructure import (
    RunDispatchConflict,
    RunNotFoundError,
    SQLiteWorkflowRepository,
)


logger = logging.getLogger(__name__)
ClientFactory = Callable[[str], A2AAgentClient]


class PlannerRunDispatcher:
    """Dispatch one newly submitted Run to its configured Planner Agent."""

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
            "Could not persist Planner disposition for Run %s (error type: %s)",
            run_id,
            type(error).__name__,
        )
