"""Isolated real TCP Agent servers and a bounded live diagnostic stream."""

import asyncio
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from importlib.resources import files
import json
from pathlib import Path
import secrets
import socket
from tempfile import TemporaryDirectory
from uuid import uuid4

import httpx
from anyio import CancelScope
from a2a.types import TaskState
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from google.protobuf.json_format import MessageToDict
from pydantic import SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware
import uvicorn

from agents.core.config import AgentSettings
from agents.main import create_app as create_agent_app
from diagnostics.communication.contracts import CheckOptions, ProbeRecord, now, summarize
from diagnostics.communication.probe import MockProbeExecutor, ReceiverObserver, ResponseFault, message_data
from diagnostics.communication.workloads import load_request, fixture_statistics
from orchestrator.a2a import A2AAgentClient, A2AWorkflowMetadata
from orchestrator.core.async_control import await_owned


ROLES = ("PLANNER", "DEVELOPER", "QA", "SECURITY", "SINGLE_AGENT")
TERMINAL = {TaskState.TASK_STATE_COMPLETED, TaskState.TASK_STATE_FAILED,
            TaskState.TASK_STATE_REJECTED, TaskState.TASK_STATE_CANCELED}


class EmbeddedServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # The dashboard server owns process signals. Embedded servers must not
        # replace one another's signal handlers on the shared event loop.
        yield


class CommunicationLab:
    def __init__(self):
        self.records: dict[str, ProbeRecord] = {}
        self.endpoints = {}
        self.tokens = {}
        self.busy = False

    async def probe(self, row, options, run_id, scenario_id, upstream=None, first=False, context_id=None,
                    requirement_ids=None):
        row.mark("prepare", "PREPARING")
        row.payload = {
            "probeId": row.probe_id,
            "request": "진단용 데이터 수신과 지정 필드 근거 대조. 실제 제품 개발은 수행하지 마세요.",
            "sample": {"value": options.value, "label": options.label,
                       "requirementIds": ["REQ-DEMO-001", "REQ-DEMO-002"],
                       "nested": {"enabled": True, "empty": [], "nothing": None}},
            "upstream": deepcopy(upstream),
        }
        row.payload["profile"] = options.profile
        if options.profile != "basic":
            doc = load_request(options.profile)
            row.payload["workRequest"] = doc
            row.payload["request"] = "첨부된 합성 사용자 요청·제약·미결정 항목으로 역할별 구조화 계약을 작성하세요. 실제 도구 실행은 하지 마세요."
            row.payload["sample"]["requirementIds"] = [item["id"] for item in doc["requirements"]]
        if options.padding_kib:
            text = "참고자료의 제목·경로·권한·한도를 임의로 바꾸지 마세요.\n"
            raw = (text * (options.padding_kib * 1024 // len(text.encode()) + 1)).encode()
            row.payload["transportPadding"] = raw[:options.padding_kib * 1024].decode("utf-8", errors="ignore")
        references = requirement_ids or [uuid4() for _ in row.payload["sample"]["requirementIds"]]
        row.payload["requirementRefs"] = [{"externalId": external, "a2aId": str(a2a)}
                                          for external, a2a in zip(row.payload["sample"]["requirementIds"], references)]
        metadata = A2AWorkflowMetadata(runId=run_id, workflowStepId=uuid4(),
                                       scenarioId=scenario_id, attempt=0, requirementIds=references)
        row.metadata_sent = metadata.to_a2a_json()
        row.injected_processing_ms = 1000 if options.scenario == "slow_agent" and row.role in {options.fault_target, "SINGLE_AGENT"} else options.processing_ms
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

        timeout_seconds = 1.5 if options.scenario == "no_response" else 5.0
        async with httpx.AsyncClient(timeout=8, trust_env=False,
                                     headers={"Authorization": "Bearer " + self.tokens[row.role]},
                                     event_hooks={"request": [request_hook], "response": [response_hook]}) as transport:
            client = A2AAgentClient(self.endpoints[row.role], httpx_client=transport, trust_env=False)
            try:
                delay = (10_000 if options.scenario == "slow_sender" else options.sender_delay_ms) if first else 0
                if delay:
                    row.injection = f"Orchestrator 첫 요청 준비 단계에 {delay}ms 대기 주입"
                    row.changed()
                    await asyncio.sleep(delay / 1000)
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
        """One caller owns this bounded run; the HTTP entry point admits one."""
        self.records.clear()
        roles = ROLES[:4] if options.architecture == "MULTI_AGENT" else ("SINGLE_AGENT",)
        rows = [ProbeRecord(role, turn + 1, turn * len(roles) + index, options.scenario, fault_target=options.fault_target)
                for turn in range(options.rounds) for index, role in enumerate(roles)]
        for row in rows:
            self.records[row.probe_id] = row
            row.notify = lambda changed: emit({"type": "row", "row": changed.view()})
        run_id, scenario_id = uuid4(), uuid4()
        requirement_count = fixture_statistics(options.profile)["requirements"]
        requirement_ids = [uuid4() for _ in range(requirement_count)]
        started = now()
        emit({"type": "start", "runId": str(run_id), "options": options.model_dump(),
              "mode": "REAL_HTTP_A2A_MOCK_EXECUTOR", "rows": [row.view() for row in rows]})

        def skip(row):
            row.error = "이전 단계 응답이 없어 이 단계 요청은 전송하지 않았습니다."
            row.phase = "SKIPPED"
            row.changed()

        async def work():
            single_context = None
            for turn in range(options.rounds):
                group = rows[turn * len(roles):(turn + 1) * len(roles)]
                result = await self.probe(group[0], options, run_id, scenario_id, first=turn == 0,
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
            await asyncio.wait_for(work(), timeout=45)
        except TimeoutError:
            for row in rows:
                if row.phase == "PLANNED":
                    skip(row)
        finally:
            for row in rows:
                row.notify = None
        report = {"type": "done", "runId": str(run_id), "options": options.model_dump(),
                  "mode": "REAL_HTTP_A2A_MOCK_EXECUTOR", "llmUsed": False,
                  "fixture": fixture_statistics(options.profile),
                  "elapsedMs": round((now() - started) / 1_000_000, 3),
                  "summary": summarize(rows), "rows": [row.view() for row in rows],
                  "measurement": {"clock": "shared-process perf_counter_ns",
                                  "pollIntervalMs": 20, "oneWayScope": "client dispatch + loopback HTTP + server admission",
                                  "resultObservationScope": "store + polling wait + HTTP + deserialization",
                                  "groundingScope": "declared fields only; free text and real LLM unverified"}}
        emit(report)
        return report


@asynccontextmanager
async def running_lab():
    """Separate ephemeral TCP ports and DBs, with existing authenticated APIs."""
    lab = CommunicationLab()
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
                app = create_agent_app(settings, executor=MockProbeExecutor(lab.records))
                observed = ResponseFault(ReceiverObserver(app, lab.records, token), lab.records, token)
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


def create_dashboard(lab: CommunicationLab, port: int) -> FastAPI:
    app = FastAPI(title="A2A 통신 검증실", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])
    allowed_origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        return HTMLResponse(files("diagnostics.communication").joinpath("dashboard.html").read_text(encoding="utf-8"),
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"})

    @app.get("/api/status")
    async def status():
        return {"mode": "REAL_HTTP_A2A_MOCK_EXECUTOR", "llmUsed": False, "busy": lab.busy,
                "agents": [{"role": role, "url": url} for role, url in lab.endpoints.items()]}

    @app.get("/api/fixtures/{profile}")
    async def fixture_preview(profile: str):
        if profile not in {"signup", "documents", "mystery"}:
            raise HTTPException(404, "진단 자료를 찾을 수 없습니다.")
        # The reference answer stays on the host; only the request is exposed.
        return {"statistics": fixture_statistics(profile), "request": load_request(profile)}

    @app.post("/api/check")
    async def check(request: Request):
        if request.headers.get("x-lab-action") != "check" or request.headers.get("origin") not in allowed_origins:
            raise HTTPException(403, "진단 화면의 같은 출처 요청만 허용합니다.")
        if not request.headers.get("content-type", "").startswith("application/json"):
            raise HTTPException(415, "JSON이 필요합니다.")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "진단 입력 크기를 초과했습니다.")
        try:
            options = CheckOptions.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, "값·반복 횟수·지연 설정을 확인해 주세요.") from None
        if lab.busy:
            raise HTTPException(409, "현재 진단이 진행 중입니다. 완료하거나 중단 후 실행하세요.")
        lab.busy = True

        async def stream():
            queue = asyncio.Queue(maxsize=512)

            def emit(event):
                queue.put_nowait((json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n").encode())

            async def produce():
                try:
                    await lab.execute(options, emit)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    emit({"type": "error", "message": "진단 실행 중 오류가 발생했습니다. 완료로 처리하지 않습니다."})

            task = asyncio.create_task(produce())
            try:
                while True:
                    line = await queue.get()
                    yield line
                    if json.loads(line)["type"] in {"done", "error"}:
                        break
                await task
            finally:
                # Starlette can cancel the stream inside an AnyIO cancel scope.
                # Drain the owned producer and its remote A2A cancellation even
                # after browser disconnect; otherwise busy stays true forever.
                with CancelScope(shield=True):
                    try:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    finally:
                        lab.busy = False

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
