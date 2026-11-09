"""Authenticated receiver receipts and the common A2A execution lifecycle."""

from abc import abstractmethod
from copy import deepcopy
import json

from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from diagnostics.communication.contracts import now


def message_data(message: dict) -> dict | None:
    parts = message.get("parts", [])
    return next((p["data"] for p in parts if isinstance(p.get("data"), dict)), None)


class ReceiverObserver:
    """Observe registered authenticated input without modifying content."""

    def __init__(self, app, records, bearer: str):
        self.app, self.records, self.authorization = app, records, ("Bearer " + bearer).encode()

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http" or scope.get("method") != "POST"
                or scope.get("path") != "/message:send"
                or dict(scope["headers"]).get(b"authorization") != self.authorization):
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] != "http.request":
                return
            body.extend(event.get("body", b""))
            if len(body) > 65536:
                await send({"type": "http.response.start", "status": 413, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
            if not event.get("more_body", False):
                break
        received_at = now()
        try:
            wire = json.loads(body)
            payload = message_data(wire.get("message", {}))
            row = self.records.get(payload.get("probeId")) if payload else None
        except (ValueError, AttributeError):
            row = None
        if row is not None:
            row.wire_received = deepcopy(payload)
            row.admitted = deepcopy(payload)
            row.points["serverReceived"] = received_at
            row.phase = "RECEIVED"
            row.changed()
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


class ProbeExecutor(AgentExecutor):
    """Receipt/storage lifecycle only; subclasses supply actual execution."""

    def __init__(self, records):
        self.records = records

    async def execute(self, context, event_queue):
        payload = message_data(MessageToDict(context.message))
        if not payload or payload.get("probeId") not in self.records:
            raise ValueError("UNREGISTERED_DIAGNOSTIC_PROBE")
        row = self.records[payload["probeId"]]
        row.executor_invocations += 1
        row.received = deepcopy(payload)
        row.metadata_received = deepcopy(context.metadata)
        row.receiver_task_id, row.receiver_context_id = context.task_id, context.context_id
        row.receiver_message_id = context.message.message_id
        row.task_id, row.context_id = context.task_id, context.context_id
        row.mark("executorStart", "PROCESSING")
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=context.metadata)
        result = await self.make_result(payload, row)
        row.response_sent = deepcopy(result)
        row.mark("responseReady", "RESULT_READY")
        await updater.add_artifact(parts=[new_data_part(result, media_type="application/json")], name="diagnostic-receipt")
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=context.metadata)

    @abstractmethod
    async def make_result(self, payload, row):
        raise NotImplementedError

    async def cancel(self, context, event_queue):
        # Production handler drains the running producer before it persists the
        # actual CANCELED state; do not publish a fabricated early success here.
        return None
