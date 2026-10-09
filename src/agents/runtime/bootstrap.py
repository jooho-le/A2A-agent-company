"""Reject unconfigured work explicitly without pretending to execute an Agent."""

from google.protobuf.json_format import MessageToDict

from a2a.helpers import new_data_part, new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from a2a.utils.errors import InvalidParamsError


class BootstrapAgentExecutor(AgentExecutor):
    """Transport scaffold only, even when provider/model settings are supplied."""

    @staticmethod
    def _updater(context: RequestContext, event_queue: EventQueue) -> TaskUpdater:
        if not context.task_id or not context.context_id:
            raise InvalidParamsError("SDK-assigned Task and Context IDs are required")
        return TaskUpdater(event_queue, context.task_id, context.context_id)

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = self._updater(context, event_queue)
        metadata = context.metadata
        if context.current_task is None:
            # SDK V2 requires a Task event before its first status update.
            # Use the IDs already assigned by RequestContext, never new IDs.
            task = new_task(
                task_id=updater.task_id,
                context_id=updater.context_id,
                state=TaskState.TASK_STATE_SUBMITTED,
                history=[context.message] if context.message is not None else [],
            )
            task.metadata.update(metadata)
            task.status.timestamp.GetCurrentTime()
            await event_queue.enqueue_event(task)
        # The SDK does not copy request metadata into newly created Tasks.
        # The first event must carry it, including for returnImmediately calls.
        await updater.update_status(
            TaskState.TASK_STATE_SUBMITTED, metadata=metadata,
        )
        reason = new_data_part(
            {
                "code": "AGENT_RUNTIME_NOT_CONFIGURED",
                "message": "Real Agent execution is not implemented in this bootstrap server.",
            },
            media_type="application/json",
        )
        await updater.update_status(
            TaskState.TASK_STATE_REJECTED,
            message=updater.new_agent_message(parts=[reason]),
            metadata=metadata,
        )

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = self._updater(context, event_queue)
        # Cancel RequestContexts have no SendMessageRequest. Keep the metadata
        # of the already stored Task rather than creating new workflow identity.
        metadata = context.metadata
        if not metadata and context.current_task is not None:
            metadata = MessageToDict(context.current_task.metadata)
        await updater.update_status(
            TaskState.TASK_STATE_CANCELED, metadata=metadata,
        )
