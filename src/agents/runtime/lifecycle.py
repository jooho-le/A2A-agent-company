"""Credential-safe exception boundary for trusted, injected Agent executors."""

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState


class SafeAgentExecutor(AgentExecutor):
    """Do not let SDK failure messages persist arbitrary executor exceptions.

    Cancellation (a BaseException) is intentionally not converted to success or
    failure here. The SDK owns worker termination; the store records abandoned
    active Tasks only after those workers have stopped.
    """

    def __init__(self, executor: AgentExecutor) -> None:
        self._executor = executor

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            # Admission has already durably created SUBMITTED with its input.
            # Publish that accepted state before an executor can await an LLM
            # or Tool without emitting an event. Send/Cancel/Shutdown must not
            # remain blocked on that executor's first event.
            await TaskUpdater(event_queue, context.task_id, context.context_id).update_status(
                TaskState.TASK_STATE_SUBMITTED, metadata=context.metadata,
            )
            await self._executor.execute(context, event_queue)
        except Exception:
            raise RuntimeError("AGENT_EXECUTION_FAILED") from None

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        try:
            await self._executor.cancel(context, event_queue)
        except Exception:
            raise RuntimeError("AGENT_CANCELLATION_FAILED") from None
