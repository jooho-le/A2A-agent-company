"""Run-to-Agent dispatch for the initial Planner stage."""

import logging
from collections.abc import Callable
from uuid import UUID

from orchestrator.a2a import A2AAgentClient, A2AAgentRegistry
from orchestrator.application.a2a_tasks import A2ATaskRunner, TaskRunDisposition
from orchestrator.domain import AgentRole, WorkflowStatus
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
            # The Planner Task is complete. Parsing its plan into downstream Steps
            # is a separate orchestration stage and must not be guessed here.
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

    def _move_to_human_review(
        self, run_id: UUID, workflow_step_id: UUID, attempt: int
    ) -> None:
        try:
            self._repository.transition_run_and_record(
                run_id,
                WorkflowStatus.HUMAN_REVIEW,
                additional_event_types=("A2A_DISPATCH_REQUIRES_REVIEW",),
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
