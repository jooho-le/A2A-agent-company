"""Durable admission guards around the official SDK's asynchronous handler."""

import asyncio

from a2a.server.agent_execution import RequestContext
from a2a.server.agent_execution.request_context_builder import RequestContextBuilder
from a2a.server.context import ServerCallContext
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.types import CancelTaskRequest, SendMessageRequest, Task, TaskState
from a2a.utils.errors import InvalidAgentResponseError, UnsupportedOperationError
from a2a.utils.input_mode_validator import validate_input_modes
from a2a.utils.proto_utils import validate_proto_required_fields
from a2a.utils.task import apply_history_length, validate_history_length
from google.protobuf.json_format import MessageToDict

from agents.api.sqlite_task_store import SQLiteAgentTaskStore
from agents.api.validation import parse_workflow_metadata, request_metadata


_RESERVED_IDS = "agents.reserved_task_context"


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
            stored = await self._store.get(params.id, context)
            if stored is not None and stored.task.status.state == TaskState.TASK_STATE_CANCELED:
                return stored.task
            return await super().on_cancel_task(params, context)

    async def aclose(self) -> None:
        self._closing = True
        async with self._admission:
            await super().aclose()
