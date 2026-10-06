"""Project ownership guards around the SDK's common Agent request handler."""

from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import CancelTaskRequest, SendMessageRequest, Task, TaskState
from a2a.utils.errors import InvalidAgentResponseError, InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict

from agents.api.validation import parse_workflow_metadata, request_metadata


class ProjectRequestHandler(DefaultRequestHandler):
    """Memory-only Context ownership. Durable lifecycle/dedup belongs to step 17."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._context_bindings: dict[str, tuple[str, str]] = {}

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task:
        metadata = request_metadata(params)
        binding = (str(metadata.run_id), str(metadata.scenario_id))
        message = params.message
        if message.context_id:
            if self._context_bindings.get(message.context_id) != binding:
                raise InvalidParamsError(message="Context does not belong to this Agent/Run")
        if message.task_id:
            task = await self.task_store.get(message.task_id, context)
            if task is None:
                raise TaskNotFoundError()
            previous = parse_workflow_metadata(MessageToDict(task.metadata))
            if (
                previous != metadata
                or not message.context_id
                or message.context_id != task.context_id
            ):
                raise InvalidParamsError(message="Continuation must preserve Task ownership")

        result = await super().on_message_send(params, context)
        if not isinstance(result, Task):
            raise InvalidAgentResponseError(message="Project workflow requires a Task response")
        if (
            not result.context_id
            or parse_workflow_metadata(MessageToDict(result.metadata)) != metadata
            or (message.context_id and result.context_id != message.context_id)
        ):
            raise InvalidAgentResponseError(message="Task response must preserve workflow metadata")
        self._context_bindings[result.context_id] = binding
        return result

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        task = await self.task_store.get(params.id, context)
        if task is not None and task.status.state == TaskState.TASK_STATE_CANCELED:
            # Idempotent confirmation; do not cancel/restart an already terminal Task.
            return task
        return await super().on_cancel_task(params, context)
