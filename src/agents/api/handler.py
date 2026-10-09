"""Durable admission guards around the official SDK's asynchronous handler."""

import asyncio

from a2a.server.agent_execution import RequestContext
from a2a.server.agent_execution.request_context_builder import RequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import CancelTaskRequest, SendMessageRequest, Task, TaskState
from a2a.utils.errors import (
    InvalidAgentResponseError, TaskNotCancelableError, TaskNotFoundError,
    UnsupportedOperationError,
)
from a2a.utils.input_mode_validator import validate_input_modes
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import apply_history_length, validate_history_length
from google.protobuf.json_format import MessageToDict

from agents.api.sqlite_task_store import SQLiteAgentTaskStore
from agents.api.validation import parse_workflow_metadata, request_metadata
from orchestrator.core.async_control import await_owned


_RESERVED_IDS = "agents.reserved_task_context"
_TERMINAL = frozenset({
    TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_CANCELED,
})


class _CancellationEvents(EventQueue):
    """Cleanup acknowledgements cannot publish an early terminal Task.

    The handler alone persists CANCELED, after the SDK producer and consumer
    have drained. A cleanup hook is not a new Agent execution or Artifact.
    """

    async def enqueue_event(self, event) -> None:
        pass


class ReservedTaskContextBuilder(RequestContextBuilder):
    """Use IDs issued in the atomic receipt claim, not client-invented IDs."""

    async def build(
        self, context: ServerCallContext, params: SendMessageRequest | None = None,
        task_id: str | None = None, context_id: str | None = None,
        task: Task | None = None,
    ) -> RequestContext:
        reserved = context.state.get(_RESERVED_IDS)
        if params is not None:
            if reserved is None:
                raise InvalidAgentResponseError(message="Task admission is required")
            task_id, context_id = reserved
        return RequestContext(
            call_context=context, request=params, task_id=task_id,
            context_id=context_id, task=task,
        )


class ProjectRequestHandler(DefaultRequestHandler):
    """One SDK worker owner per DB, with persistent message/context admission.

    Admission is serialized through the first SDK response, not through the
    entire Agent execution. Store CAS protects subsequent background updates.
    A receipt survives a lost response: it is never deleted to retry execution.
    """

    def __init__(self, *, task_store: SQLiteAgentTaskStore, **kwargs) -> None:
        super().__init__(
            task_store=task_store,
            request_context_builder=ReservedTaskContextBuilder(), **kwargs,
        )
        self._store = task_store
        self._admission = asyncio.Lock()
        self._closing = False

    def _require_open(self) -> None:
        if self._closing:
            raise UnsupportedOperationError(message="Agent server is stopping")

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Task:
        # Validate everything the SDK can reject before creating a receipt.
        validate_proto_required_fields(params)
        validate_history_length(params.configuration)
        if self._validate_input_modes:
            validate_input_modes(params.message, self._agent_card)
        metadata = request_metadata(params)
        async with self._admission:
            self._require_open()
            claim = await self._store.claim_message(params, context)
            if claim.duplicate:
                # A repeated message is a read, including terminal Tasks.
                return apply_history_length(claim.task, params.configuration)
            context.state[_RESERVED_IDS] = (claim.task.id, claim.task.context_id)
            result = await super().on_message_send(params, context)
            if not isinstance(result, Task):
                raise InvalidAgentResponseError(message="Project workflow requires a Task response")
            if (
                result.id != claim.task.id
                or result.context_id != claim.task.context_id
                or parse_workflow_metadata(MessageToDict(result.metadata)) != metadata
            ):
                raise InvalidAgentResponseError(message="Task response must preserve workflow identity")
            return result

    async def on_cancel_task(
        self, params: CancelTaskRequest, context: ServerCallContext
    ) -> Task | None:
        validate_proto_required_fields(params)
        async with self._admission:
            self._require_open()
            operation = asyncio.create_task(self._cancel_drained(params.id, context))
            return await await_owned(operation)

    async def _cancel_drained(self, task_id, context) -> Task:
        stored = await self._store.get(task_id, context)
        if stored is None:
            raise TaskNotFoundError()
        if stored.task.status.state == TaskState.TASK_STATE_CANCELED:
            return stored.task
        if stored.task.status.state in _TERMINAL:
            raise TaskNotCancelableError()
        active = await self._active_task_registry.get(task_id)
        if active is not None:
            # Public SDK teardown awaits producer finally/MCP teardown. The
            # default SDK cancel publishes the executor acknowledgement first;
            # this single-owner Host must not expose CANCELED during cleanup.
            await active.aclose()
        stored = await self._store.get(task_id, context)
        if stored is None:
            raise TaskNotFoundError()
        if stored.task.status.state == TaskState.TASK_STATE_CANCELED:
            return stored.task
        if stored.task.status.state in _TERMINAL:
            raise TaskNotCancelableError()
        await self.agent_executor.cancel(RequestContext(
            call_context=context, task_id=task_id,
            context_id=stored.task.context_id, task=stored.task,
        ), _CancellationEvents())
        # SQLiteAgentTaskStore's live per-DB lease excludes another producer.
        # Use the pinned SDK's versioned write after draining, preserving the
        # newest approved metadata/history/artifacts rather than replacing it.
        return await self._write_cancel(task_id, stored.task, stored.version, context)

    async def aclose(self) -> None:
        self._closing = True
        async with self._admission:
            await super().aclose()
