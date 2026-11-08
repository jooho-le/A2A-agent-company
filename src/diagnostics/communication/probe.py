"""Receiver-side receipts, deliberate faults, and a no-LLM test executor."""

import asyncio
from copy import deepcopy
import json

from a2a.helpers import new_data_part
from a2a.server.agent_execution import AgentExecutor
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from diagnostics.communication.contracts import fingerprint, now
from diagnostics.communication.faults import alter_request, alter_agent_claims, is_target
from diagnostics.communication.workloads import mock_specification


def message_data(message: dict) -> dict | None:
    parts = message.get("parts", [])
    return next((p["data"] for p in parts if isinstance(p.get("data"), dict)), None)


class ReceiverObserver:
    """Real ASGI receive boundary; faults only affect a known diagnostic probe.

    Input corruption is after actual wire receipt, before the production parser.
    That boundary is separately recorded so network integrity cannot falsely be
    blamed. Only authenticated, registered lab requests can change receipts.
    """

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
            injection = alter_request(wire, row)
            if injection:
                row.admitted = deepcopy(message_data(wire["message"]))
                row.injection = injection
                body = bytearray(json.dumps(wire, ensure_ascii=False).encode())
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

        # A changed body has its own accurate content-length for downstream code.
        scope = dict(scope)
        scope["headers"] = [(key, value) for key, value in scope["headers"] if key != b"content-length"] + [(b"content-length", str(len(body)).encode())]
        await self.app(scope, replay, send)


class MockProbeExecutor(AgentExecutor):
    """Observe production RequestContext and return declared, checkable facts."""

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
        if is_target(row) and row.scenario == "no_response":
            row.injection = "진단 실행기 결과를 의도적으로 보류 → 제한시간 후 실제 A2A 취소"
            row.changed()
            await asyncio.Event().wait()
        await asyncio.sleep(row.injected_processing_ms / 1000)
        sample = payload["sample"]
        claims = {"value": sample["value"], "label": sample["label"],
                  "requirementCount": len(sample["requirementIds"]),
                  "upstreamHash": fingerprint(payload["upstream"]) if payload["upstream"] is not None else None}
        if payload.get("profile", "basic") != "basic":
            claims["specification"] = mock_specification(payload["workRequest"], row.role)
        result = {"probeId": row.probe_id, "claims": claims,
                  "freeText": "진단용 목업 응답이며 실제 LLM·개발·검사·배포를 수행하지 않았습니다.",
                  "runtime": "MOCK_NO_LLM"}
        alter_agent_claims(result, row)
        row.response_sent = deepcopy(result)
        row.mark("responseReady", "RESULT_READY")
        await updater.add_artifact(parts=[new_data_part(result, media_type="application/json")], name="diagnostic-receipt")
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=context.metadata)

    async def cancel(self, context, event_queue):
        # Production handler drains the running producer before it persists the
        # actual CANCELED state; do not publish a fabricated early success here.
        return None


class ResponseFault:
    """Opt-in server egress corruption, independent of the executor receipt."""

    def __init__(self, app, records, bearer):
        self.app, self.records = app, records
        self.authorization = ("Bearer " + bearer).encode()

    async def __call__(self, scope, receive, send):
        if (scope["type"] != "http"
                or dict(scope["headers"]).get(b"authorization") != self.authorization
                or not (scope.get("path", "").startswith("/tasks/") or scope.get("path") == "/message:send")):
            return await self.app(scope, receive, send)
        start, chunks = None, bytearray()

        async def intercept(event):
            nonlocal start
            if event["type"] == "http.response.start":
                start = event
                return
            if event["type"] != "http.response.body":
                return await send(event)
            chunks.extend(event.get("body", b""))
            if event.get("more_body", False):
                return
            body = bytes(chunks)
            try:
                wire = json.loads(body)
                task = wire.get("task", wire)
                row = next((r for r in self.records.values() if r.task_id == task.get("id")), None)
                if row and row.scenario == "context_spoof" and is_target(row):
                    task["contextId"] = "unrelated-context-for-negative-test"
                    row.injection = "응답 미들웨어: ACK·조회 응답 Context를 같은 잘못된 값으로 위장"
                    row.changed()
                    body = json.dumps(wire, ensure_ascii=False).encode()
                if row and row.scenario == "response_change" and is_target(row):
                    for artifact in task.get("artifacts", []):
                        for part in artifact.get("parts", []):
                            data = part.get("data", {})
                            if data.get("probeId") == row.probe_id:
                                data["claims"]["value"] += 1
                                row.injection = "서버 응답 미들웨어에서 claims.value + 1 주입 (실행기 결과 생성 이후)"
                                row.changed()
                                body = json.dumps(wire, ensure_ascii=False).encode()
            except (ValueError, AttributeError, KeyError):
                pass
            headers = [(key, value) for key, value in start["headers"] if key.lower() != b"content-length"]
            await send({**start, "headers": headers + [(b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, intercept)
