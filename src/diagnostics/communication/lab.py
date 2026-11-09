"""Isolated authenticated A2A servers for explicitly selected OpenAI execution."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
import json
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from anyio import CancelScope
from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict
from pydantic import SecretStr
import uvicorn

from agents.core.config import AgentSettings
from agents.main import create_app as create_agent_app
from diagnostics.communication.contracts import CheckOptions, ProbeRecord, now, summarize
from diagnostics.communication.probe import ReceiverObserver, message_data
from diagnostics.communication.workloads import load_request, fixture_statistics
from orchestrator.a2a import A2AAgentClient, A2AWorkflowMetadata
from orchestrator.core.async_control import await_owned


ROLES = ("PLANNER", "DEVELOPER", "QA", "SECURITY", "SINGLE_AGENT")
TERMINAL = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_CANCELED}


class EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The CLI owns process signals; embedded servers must not replace them.
        yield


class CommunicationLab:
    def __init__(self, model_runtime):
        if model_runtime is None:
            raise ValueError("LIVE_MODEL_RUNTIME_REQUIRED")
        self.records: dict[str, ProbeRecord] = {}
        self.endpoints = {}
        self.tokens = {}
        self.model_runtime = model_runtime

    @property
    def mode(self):
        return "REAL_HTTP_A2A_OPENAI"

    async def probe(self, row, options, run_id, scenario_id, upstream=None, context_id=None,
                    requirement_ids=None):
        row.mark("prepare", "PREPARING")
        doc = load_request(options.profile)
        row.payload = {
            "probeId": row.probe_id,
            "request": "첨부된 합성 사용자 요청·제약·미결정 항목으로 역할별 구조화 계약을 작성하세요. 실제 도구 실행은 하지 마세요.",
            "sample": {"value": options.value, "label": options.label,
                       "requirementIds": [item["id"] for item in doc["requirements"]],
                       "nested": {"enabled": True, "empty": [], "nothing": None}},
            "upstream": deepcopy(upstream), "profile": options.profile, "workRequest": doc,
        }
        references = requirement_ids or [uuid4() for _ in row.payload["sample"]["requirementIds"]]
        row.payload["requirementRefs"] = [{"externalId": external, "a2aId": str(a2a)}
                                          for external, a2a in zip(row.payload["sample"]["requirementIds"], references)]
        return await self.send_probe(row, run_id, scenario_id, references, context_id)

    async def send_probe(self, row, run_id, scenario_id, references, context_id=None):
        """Share the authenticated transport without imposing fixture answers."""
        metadata = A2AWorkflowMetadata(runId=run_id, workflowStepId=uuid4(),
                                       scenarioId=scenario_id, attempt=0, requirementIds=references)
        row.metadata_sent = metadata.to_a2a_json()
        row.changed()
        task = None

        async def request_hook(request):
            if request.method == "POST" and request.url.path == "/message:send":
                raw = await request.aread()
                row.wire_bytes = len(raw)
                wire = json.loads(raw)
                row.wire_sent = deepcopy(message_data(wire["message"]))
                row.message_id = wire["message"]["messageId"]
                row.mark("clientSend", "SENT")

        async def response_hook(response):
            if response.request.method == "POST" and response.request.url.path == "/message:send":
                row.response_http_status = response.status_code

        timeout_seconds = self.model_runtime.timeout_seconds + 10
        async with httpx.AsyncClient(timeout=8, trust_env=False,
                                     headers={"Authorization": "Bearer " + self.tokens[row.role]},
                                     event_hooks={"request": [request_hook], "response": [response_hook]}) as transport:
            client = A2AAgentClient(self.endpoints[row.role], httpx_client=transport, trust_env=False)
            try:
                row.mark("cardStart")
                await client.resolve_agent_card()
                row.mark("cardEnd")
                task = await client.send_task(row.payload, metadata, context_id=context_id)
                expected_task_id, expected_context_id = task.id, task.context_id
                row.task_id, row.context_id = task.id, task.context_id
                row.mark("ack")
                deadline = asyncio.get_running_loop().time() + timeout_seconds
                while task.status.state not in TERMINAL:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise TimeoutError
                    await asyncio.sleep(.02)
                    task = await client.get_task(expected_task_id)
                    if task.id != expected_task_id or task.context_id != expected_context_id:
                        row.identity_valid = False
                        raise ValueError("IDENTITY_CHANGED")
                row.identity_valid = (task.id == expected_task_id == row.receiver_task_id
                                      and task.context_id == expected_context_id == row.receiver_context_id
                                      and row.message_id == row.receiver_message_id)
                row.metadata_response = MessageToDict(task.metadata)
                if task.status.state != TaskState.TASK_STATE_COMPLETED:
                    row.error = "Agent Task가 완료되지 않았습니다: " + TaskState.Name(task.status.state)
                    if row.model_observation and row.model_observation.get("errorCode"):
                        row.error += " / " + row.model_observation["errorCode"]
                    row.mark("finish", "ERROR")
                    return None
                for artifact in task.artifacts:
                    data = message_data(MessageToDict(artifact))
                    if data and data.get("probeId") == row.probe_id:
                        row.response_received = deepcopy(data)
                        break
                if row.response_received is None:
                    raise ValueError("MISSING_RESULT_ARTIFACT")
                row.mark("observed")
                row.mark("finish", "COMPLETED")
                return row.response_received
            except TimeoutError:
                row.error = "응답 제한시간 초과. 미수신을 성공으로 처리하지 않습니다."
                row.mark("finish", "TIMEOUT")
            except asyncio.CancelledError:
                row.error = "사용자 중단 또는 실험 전체 제한시간 종료"
                row.mark("finish", "CANCELED")
                raise
            except Exception as error:
                row.error = type(error).__name__ + ": 진단 요청 실패 (예외 원문·인증정보는 출력하지 않음)"
                row.mark("finish", "ERROR")
            finally:
                # The receiver can start before send_task returns its ACK. A
                # disconnect in that window still owns the receiver-observed
                # SDK Task ID, even though the local `task` variable is None.
                cancel_id = row.receiver_task_id or (task.id if task is not None else row.task_id)
                if cancel_id is None and row.wire_sent is not None:
                    row.cleanup_state = "UNVERIFIED"
                    row.error = (row.error or "") + " / 전송 후 Task ID 미관측: 원격 취소 여부 미검증"
                if cancel_id and (task is None or task.status.state not in TERMINAL):
                    with CancelScope(shield=True):
                        try:
                            canceled = await asyncio.wait_for(client.cancel_task(cancel_id), timeout=3)
                            row.cleanup_state = TaskState.Name(canceled.status.state)
                            if canceled.status.state not in TERMINAL:
                                row.error = (row.error or "") + " / 원격 취소 확인 필요"
                        except Exception:
                            try:
                                latest = await asyncio.wait_for(client.get_task(cancel_id), timeout=2)
                                row.cleanup_state = TaskState.Name(latest.status.state)
                                if latest.status.state not in TERMINAL:
                                    row.error = (row.error or "") + " / 원격 취소 확인 실패"
                            except Exception:
                                row.cleanup_state = "UNVERIFIED"
                                row.error = (row.error or "") + " / 원격 취소 확인 실패"
                    row.changed()
        return None

    async def execute(self, options, emit):
        """One CLI caller owns a run within the explicit model-call budget."""
        self.model_runtime.admit(options)
        self.records.clear()
        roles = ROLES[:4] if options.architecture == "MULTI_AGENT" else ("SINGLE_AGENT",)
        rows = [ProbeRecord(role, turn + 1, turn * len(roles) + index)
                for turn in range(options.rounds) for index, role in enumerate(roles)]
        for row in rows:
            self.records[row.probe_id] = row
            row.notify = lambda changed: emit({"type": "row", "row": changed.view()})
        run_id, scenario_id = uuid4(), uuid4()
        requirement_count = fixture_statistics(options.profile)["requirements"]
        requirement_ids = [uuid4() for _ in range(requirement_count)]
        started = now()
        emit({"type": "start", "runId": str(run_id), "options": options.model_dump(),
              "mode": self.mode, "rows": [row.view() for row in rows]})

        def skip(row):
            row.error = "이전 단계 응답이 없어 이 단계 요청은 전송하지 않았습니다."
            row.phase = "SKIPPED"
            row.changed()

        async def work():
            single_context = None
            for turn in range(options.rounds):
                group = rows[turn * len(roles):(turn + 1) * len(roles)]
                result = await self.probe(group[0], options, run_id, scenario_id,
                                          context_id=single_context if len(group) == 1 else None,
                                          requirement_ids=requirement_ids)
                # Each role carries the same protected requirement references.
                if len(group) == 1:
                    single_context = group[0].receiver_context_id or group[0].context_id or single_context
                    continue
                if result is None:
                    for row in group[1:]:
                        skip(row)
                    continue
                result = await self.probe(group[1], options, run_id, scenario_id, upstream=result, requirement_ids=requirement_ids)
                if result is None:
                    for row in group[2:]:
                        skip(row)
                    continue
                await asyncio.gather(*(self.probe(row, options, run_id, scenario_id, upstream=result, requirement_ids=requirement_ids) for row in group[2:]))

        try:
            await asyncio.wait_for(work(), timeout=self.model_runtime.run_timeout_seconds)
        except TimeoutError:
            for row in rows:
                if row.phase == "PLANNED":
                    skip(row)
        finally:
            for row in rows:
                row.notify = None
        report = {"type": "done", "runId": str(run_id), "options": options.model_dump(),
                  "mode": self.mode,
                  "llmUsed": any(row.model_observation and row.model_observation.get("httpStatus") == 200 for row in rows),
                  "llmRequested": True,
                  "fixture": fixture_statistics(options.profile),
                  "elapsedMs": round((now() - started) / 1_000_000, 3),
                  "summary": summarize(rows), "rows": [row.view() for row in rows],
                  "measurement": {"clock": "shared-process perf_counter_ns",
                                  "pollIntervalMs": 20, "oneWayScope": "client dispatch + loopback HTTP + server admission",
                                  "resultObservationScope": "store + polling wait + HTTP + deserialization",
                                  "groundingScope": "fixed-fixture claims only; free text unverified"}}
        emit(report)
        return report


@asynccontextmanager
async def running_lab(model_runtime, *, lab=None, executor_factory=None):
    """Separate ephemeral TCP ports and DBs, with existing authenticated APIs."""
    lab = lab if lab is not None else CommunicationLab(model_runtime=model_runtime)
    if lab.model_runtime is not model_runtime:
        raise ValueError("DIAGNOSTIC_RUNTIME_MISMATCH")
    servers, tasks, sockets = [], [], []
    with TemporaryDirectory(prefix="a2a-communication-") as directory:
        try:
            for role in ROLES:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sockets.append(sock)
                sock.bind(("127.0.0.1", 0))
                sock.listen(64)
                port = sock.getsockname()[1]
                token = secrets.token_urlsafe(32)
                lab.tokens[role] = token
                lab.endpoints[role] = f"http://127.0.0.1:{port}"
                settings = AgentSettings(
                    _env_file=None, role=role, host="127.0.0.1", port=port,
                    environment="test", log_level="ERROR",
                    database_path=Path(directory) / (role.lower() + ".sqlite3"),
                    bearer_token=SecretStr(token), llm_api_key=None,
                    llm_provider=None, llm_model_id=None, llm_model_revision=None,
                )
                from diagnostics.communication.live import OpenAIProbeExecutor
                executor = (executor_factory or OpenAIProbeExecutor)(lab.records, model_runtime)
                app = create_agent_app(settings, executor=executor)
                observed = ReceiverObserver(app, lab.records, token)
                server = EmbeddedServer(uvicorn.Config(observed, host="127.0.0.1", port=port,
                                                       log_level="error", access_log=False, lifespan="on",
                                                       timeout_graceful_shutdown=3))
                servers.append(server)
                tasks.append(asyncio.create_task(server.serve(sockets=[sock])))
            for _ in range(200):
                if all(server.started for server in servers):
                    break
                if any(task.done() for task in tasks):
                    raise RuntimeError("DIAGNOSTIC_AGENT_START_FAILED")
                await asyncio.sleep(.025)
            else:
                raise RuntimeError("DIAGNOSTIC_AGENT_START_TIMEOUT")
            yield lab
        finally:
            async def shutdown():
                for server in servers:
                    server.should_exit = True
                if tasks:
                    try:
                        await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=10)
                    except TimeoutError:
                        for task in tasks:
                            task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                for sock in sockets:
                    sock.close()

            # Release each SDK's DB lease before TemporaryDirectory removes its
            # files, including if the CLI's main task receives cancellation.
            await await_owned(asyncio.create_task(shutdown()))
