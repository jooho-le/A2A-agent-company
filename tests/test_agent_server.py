"""16번 Agent HTTP+JSON 계약 검사. ASGI 안에서만 실행하며 실제 LLM/MCP를 호출하지 않는다."""

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from a2a.helpers import new_task
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.cluster.task_store import VersionedTaskStore
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import TaskUpdater
from a2a.types import AgentCard, TaskState
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx

from agents.core.config import AgentSettings
from agents.main import create_app
from orchestrator.a2a import (
    A2AAgentClient,
    A2AWorkflowMetadata,
    build_send_message_request,
    validate_agent_card,
)
from orchestrator.domain.states import AgentRole


async def _announce_task(context: RequestContext, queue: EventQueue) -> TaskUpdater:
    """SDK V2에서 첫 상태 이벤트 이전에 SDK가 할당한 ID의 Task를 게시한다."""
    updater = TaskUpdater(queue, context.task_id, context.context_id)
    if context.current_task is None:
        task = new_task(
            task_id=context.task_id, context_id=context.context_id,
            state=TaskState.TASK_STATE_SUBMITTED,
            history=[context.message] if context.message is not None else [],
        )
        task.metadata.update(context.metadata)
        await queue.enqueue_event(task)
    return updater


class WaitingExecutor(AgentExecutor):
    """실제 작업 없이 WORKING을 유지하여 원격 취소 계약을 검사하는 fixture."""

    def __init__(self):
        self.wait = asyncio.Event()
        self.inputs = []
        self.canceled_task_ids = []

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.inputs.append(MessageToDict(context.message))
        updater = await _announce_task(context, event_queue)
        await updater.update_status(TaskState.TASK_STATE_WORKING, metadata=context.metadata)
        await self.wait.wait()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        self.canceled_task_ids.append(context.task_id)
        metadata = context.metadata
        if not metadata and context.current_task is not None:
            metadata = MessageToDict(context.current_task.metadata)
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.update_status(TaskState.TASK_STATE_CANCELED, metadata=metadata)


class CompletedExecutor(AgentExecutor):
    """터미널 Task의 재시작·취소 금지를 검사할 뿐 제품 성공을 뜻하지 않는 fixture."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = await _announce_task(context, event_queue)
        await updater.update_status(TaskState.TASK_STATE_COMPLETED, metadata=context.metadata)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise AssertionError("Completed Tasks must not reach executor.cancel")


class AgentServerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.metadata = A2AWorkflowMetadata(
            runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(),
            attempt=0, requirementIds=(uuid4(),),
        )

    @asynccontextmanager
    async def client_for(self, *, settings=None, executor=None):
        settings = settings or AgentSettings(role="PLANNER", _env_file=None)
        with TemporaryDirectory() as temporary:
            values = {name: getattr(settings, name) for name in AgentSettings.model_fields}
            values["database_path"] = Path(temporary) / f"{settings.role.value.lower()}.sqlite3"
            isolated = AgentSettings(_env_file=None, **values)
            app = create_app(isolated, executor=executor)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url=isolated.agent_base_url,
                ) as client:
                    yield app, client

    def wire_request(self, *, metadata=None, context_id=None, task_id=None):
        request = build_send_message_request(
            {"request": "Analyze the signup requirements"}, metadata or self.metadata,
            context_id=context_id, task_id=task_id,
        )
        return MessageToDict(request)

    @staticmethod
    def headers(*, token=None):
        headers = {VERSION_HEADER: "1.0", "Content-Type": A2A_JSON_MEDIA_TYPE}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def send_task(self, client, *, wire=None, token=None):
        response = await client.post(
            "/message:send", json=wire if wire is not None else self.wire_request(),
            headers=self.headers(token=token),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers[VERSION_HEADER], "1.0")
        self.assertEqual(response.headers["content-type"].partition(";")[0], A2A_JSON_MEDIA_TYPE)
        self.assertIn("task", response.json())
        return response.json()["task"]

    async def poll_state(self, client, task_id, state, *, token=None):
        for _ in range(50):
            response = await client.get(f"/tasks/{task_id}", headers=self.headers(token=token))
            self.assertEqual(response.status_code, 200, response.text)
            task = response.json()
            if task["status"]["state"] == state:
                return task
            await asyncio.sleep(0)
        self.fail(f"Task did not reach {state}; last state: {task['status']['state']}")

    def assert_protocol_error(self, response, *, status=None, absent=()):
        if status is None:
            self.assertGreaterEqual(response.status_code, 400, response.text)
            self.assertLess(response.status_code, 500, response.text)
        else:
            self.assertEqual(response.status_code, status, response.text)
        error = response.json()["error"]
        self.assertEqual(error["code"], response.status_code)
        self.assertIsInstance(error["status"], str)
        self.assertIsInstance(error["message"], str)
        self.assertNotIn("traceback", response.text.lower())
        for value in absent:
            self.assertNotIn(value, response.text)

    async def test_four_role_cards_use_official_protocol_and_role_ports(self):
        for role in AgentRole:
            with self.subTest(role=role):
                settings = AgentSettings(role=role, _env_file=None)
                async with self.client_for(settings=settings) as (_, client):
                    response = await client.get("/.well-known/agent-card.json")
                self.assertEqual(response.status_code, 200)
                card = ParseDict(response.json(), AgentCard())
                self.assertEqual(card.name, f"{role.value} Agent")
                self.assertEqual(card.version, "0.1.0")
                self.assertEqual(validate_agent_card(card), settings.agent_base_url)
                self.assertEqual(card.supported_interfaces[0].protocol_version, "1.0")
                self.assertEqual(card.supported_interfaces[0].protocol_binding, "HTTP+JSON")
                self.assertEqual(list(card.default_input_modes), ["application/json"])
                self.assertEqual(list(card.default_output_modes), ["application/json"])
                self.assertFalse(card.capabilities.streaming)
                self.assertFalse(card.capabilities.push_notifications)
                self.assertEqual(len(card.skills), 0)
                self.assertIn("not configured", card.description)

    async def test_health_is_honest_and_app_reuses_sdk_handler_store_without_settings_state(self):
        async with self.client_for() as (app, client):
            response = await client.get("/health")
            self.assertEqual(response.json(), {
                "status": "ok", "role": "PLANNER", "executionReady": False,
            })
            self.assertIsInstance(app.state.request_handler, DefaultRequestHandler)
            self.assertIsInstance(app.state.task_store, VersionedTaskStore)
            self.assertFalse(hasattr(app.state, "settings"))

    async def test_bootstrap_rejects_work_without_artifacts_and_preserves_workflow_identity(self):
        async with self.client_for() as (_, client):
            submitted = await self.send_task(client)
            task = await self.poll_state(client, submitted["id"], "TASK_STATE_REJECTED")
        self.assertNotEqual(task["id"], str(self.metadata.workflow_step_id))
        self.assertEqual(UUID(task["id"]).version, 4)
        self.assertEqual(UUID(task["contextId"]).version, 4)
        self.assertEqual(task["metadata"], self.metadata.to_a2a_json())
        self.assertFalse(task.get("artifacts"))
        self.assertEqual(task["status"]["message"]["parts"][0]["data"]["code"],
                         "AGENT_RUNTIME_NOT_CONFIGURED")

    async def test_model_selection_does_not_make_bootstrap_pretend_to_execute(self):
        settings = AgentSettings(
            role="DEVELOPER", llm_provider="local-fixture", llm_model_id="fixture-model",
            _env_file=None,
        )
        async with self.client_for(settings=settings) as (_, client):
            self.assertFalse((await client.get("/health")).json()["executionReady"])
            submitted = await self.send_task(client)
            task = await self.poll_state(client, submitted["id"], "TASK_STATE_REJECTED")
        self.assertFalse(task.get("artifacts"))

    async def test_missing_or_unsupported_protocol_version_is_rejected(self):
        async with self.client_for() as (_, client):
            for version in (None, "1.1", "0.3"):
                with self.subTest(version=version):
                    headers = self.headers()
                    if version is None:
                        headers.pop(VERSION_HEADER)
                    else:
                        headers[VERSION_HEADER] = version
                    response = await client.post("/message:send", json=self.wire_request(), headers=headers)
                    self.assert_protocol_error(response)

    async def test_unsupported_http_content_type_is_rejected(self):
        async with self.client_for() as (_, client):
            headers = self.headers()
            headers["Content-Type"] = "application/json"
            response = await client.post("/message:send", json=self.wire_request(), headers=headers)
            self.assert_protocol_error(response)

    async def test_invalid_json_duplicate_fields_and_nonfinite_numbers_are_not_echoed(self):
        bodies = (
            '{"DUMMY_INVALID_JSON_SECRET":',
            '["DUMMY_BODY_ARRAY_SECRET"]',
            '{"message":{},"message":{"password":"DUMMY_DUPLICATE_SECRET"}}',
            '{"message":{},"metadata":{"attempt":NaN,"password":"DUMMY_NONFINITE_SECRET"}}',
        )
        async with self.client_for() as (_, client):
            for body in bodies:
                with self.subTest(body_kind=body[:15]):
                    response = await client.post("/message:send", content=body, headers=self.headers())
                    self.assert_protocol_error(response, absent=(
                        "DUMMY_INVALID_JSON_SECRET", "DUMMY_BODY_ARRAY_SECRET",
                        "DUMMY_DUPLICATE_SECRET", "DUMMY_NONFINITE_SECRET",
                    ))

    async def test_non_user_role_or_non_uuid4_message_id_is_rejected(self):
        async with self.client_for() as (_, client):
            for field, value in (
                ("role", "ROLE_AGENT"), ("messageId", "DUMMY_BAD_MESSAGE_ID"),
                ("messageId", str(UUID(int=1, version=1))),
            ):
                with self.subTest(field=field, value=value):
                    wire = self.wire_request()
                    wire["message"][field] = value
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response, absent=("DUMMY_BAD_MESSAGE_ID",))

    async def test_required_configuration_boolean_and_json_output_mode_are_enforced(self):
        variants = (
            None, {}, {"returnImmediately": False, "acceptedOutputModes": ["application/json"]},
            {"returnImmediately": 1, "acceptedOutputModes": ["application/json"]},
            {"returnImmediately": "true", "acceptedOutputModes": ["application/json"]},
            {"returnImmediately": True, "acceptedOutputModes": ["text/plain"]},
        )
        async with self.client_for() as (_, client):
            for configuration in variants:
                with self.subTest(configuration=configuration):
                    wire = self.wire_request()
                    if configuration is None:
                        wire.pop("configuration")
                    else:
                        wire["configuration"] = configuration
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response)

    async def test_input_requires_nonempty_json_data_parts(self):
        variants = (
            [], [{"text": "DUMMY_BAD_TEXT_PART", "mediaType": "text/plain"}],
            [{"data": [], "mediaType": "application/json"}],
            [{"data": {}, "mediaType": "application/json"}],
            [{"data": {"request": "x"}, "mediaType": "text/plain"}],
            [{"data": {"request": "x"}, "text": "DUMMY_BAD_TEXT_PART", "mediaType": "application/json"}],
        )
        async with self.client_for() as (_, client):
            for parts in variants:
                with self.subTest(parts=parts):
                    wire = self.wire_request()
                    wire["message"]["parts"] = parts
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response, absent=("DUMMY_BAD_TEXT_PART",))

    async def test_invalid_or_unknown_metadata_is_rejected_without_input_reflection(self):
        async with self.client_for() as (_, client):
            for mutation in ("missing", "invalid_uuid", "unknown_field", "duplicate_requirements"):
                with self.subTest(mutation=mutation):
                    wire = self.wire_request()
                    if mutation == "missing":
                        wire.pop("metadata")
                    elif mutation == "invalid_uuid":
                        wire["metadata"]["runId"] = "DUMMY_BAD_RUN_SECRET"
                    elif mutation == "unknown_field":
                        wire["metadata"]["untrusted"] = "DUMMY_BAD_METADATA_SECRET"
                    else:
                        ids = wire["metadata"]["requirementIds"]
                        wire["metadata"]["requirementIds"] = ids + ids
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response, absent=("DUMMY_BAD_RUN_SECRET", "DUMMY_BAD_METADATA_SECRET"))

    async def test_boolean_fractional_string_and_negative_attempts_are_rejected(self):
        async with self.client_for() as (_, client):
            for attempt in (True, False, 0.5, -1, "0"):
                with self.subTest(attempt=attempt):
                    wire = self.wire_request()
                    wire["metadata"]["attempt"] = attempt
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response)

    async def test_protobuf_integral_float_metadata_remains_compatible(self):
        wire = self.wire_request()
        wire["metadata"]["attempt"] = 0.0
        wire["metadata"]["codeVersion"] = 1.0
        async with self.client_for() as (_, client):
            submitted = await self.send_task(client, wire=wire)
            task = await self.poll_state(client, submitted["id"], "TASK_STATE_REJECTED")
        self.assertEqual(task["metadata"]["attempt"], 0)
        self.assertEqual(task["metadata"]["codeVersion"], 1)

    async def test_unknown_official_field_or_blank_routing_reference_is_rejected(self):
        async with self.client_for() as (_, client):
            for field, value in (
                ("contextId", " "), ("taskId", ""), ("unknownOfficialField", "DUMMY_SCHEMA_SECRET"),
            ):
                with self.subTest(field=field):
                    wire = self.wire_request()
                    wire["message"][field] = value
                    response = await client.post("/message:send", json=wire, headers=self.headers())
                    self.assert_protocol_error(response, absent=("DUMMY_SCHEMA_SECRET",))

    async def test_bearer_auth_is_required_for_card_send_poll_and_cancel(self):
        secret = "DUMMY_SERVER_BEARER"
        settings = AgentSettings(role="QA", bearer_token=secret, _env_file=None)
        async with self.client_for(settings=settings) as (_, client):
            for authorization in (None, "Bearer DUMMY_WRONG_BEARER", f"Basic {secret}"):
                headers = self.headers()
                if authorization is not None:
                    headers["Authorization"] = authorization
                for method, path in (
                    ("GET", "/.well-known/agent-card.json"), ("POST", "/message:send"),
                    ("GET", "/tasks/unknown"), ("POST", "/tasks/unknown:cancel"),
                ):
                    with self.subTest(authorization_kind=authorization is not None, path=path):
                        response = await client.request(method, path, headers=headers, json=self.wire_request())
                        self.assert_protocol_error(response, status=401, absent=(secret, "DUMMY_WRONG_BEARER"))
                        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")

    async def test_authorized_card_and_task_do_not_expose_token_or_api_key(self):
        settings = AgentSettings(
            role="SECURITY", bearer_token="DUMMY_AUTHORIZED_TOKEN",
            llm_api_key="DUMMY_CONFIG_API_KEY", _env_file=None,
        )
        async with self.client_for(settings=settings) as (_, client):
            card = await client.get("/.well-known/agent-card.json", headers=self.headers(token="DUMMY_AUTHORIZED_TOKEN"))
            self.assertEqual(card.status_code, 200)
            self.assertIn("bearerAuth", card.json()["securitySchemes"])
            submitted = await self.send_task(client, token="DUMMY_AUTHORIZED_TOKEN")
            task = await self.poll_state(client, submitted["id"], "TASK_STATE_REJECTED", token="DUMMY_AUTHORIZED_TOKEN")
            self.assertEqual((await client.get("/health")).status_code, 200)
        for output in (card.text, json.dumps(task)):
            self.assertNotIn("DUMMY_AUTHORIZED_TOKEN", output)
            self.assertNotIn("DUMMY_CONFIG_API_KEY", output)
        self.assertEqual(task["metadata"], self.metadata.to_a2a_json())

    async def test_raw_input_secrets_are_redacted_before_executor_storage_and_response(self):
        executor = WaitingExecutor()
        wire = self.wire_request()
        wire["message"]["parts"][0]["data"].update(
            password="DUMMY_RAW_PASSWORD", passwordHash="DUMMY_RAW_HASH",
            authorization="Bearer DUMMY_RAW_AUTH",
        )
        async with self.client_for(executor=executor) as (app, client):
            submitted = await self.send_task(client, wire=wire)
            task = await self.poll_state(client, submitted["id"], "TASK_STATE_WORKING")
            stored = await app.state.task_store.get(submitted["id"], ServerCallContext())
            self.assertIsNotNone(stored)
            outputs = (json.dumps(task), json.dumps(executor.inputs), json.dumps(MessageToDict(stored.task)))
            for output in outputs:
                for secret in ("DUMMY_RAW_PASSWORD", "DUMMY_RAW_HASH", "DUMMY_RAW_AUTH"):
                    self.assertNotIn(secret, output)
            self.assertIn("[REDACTED]", json.dumps(executor.inputs))

    async def test_agent_apps_do_not_share_task_or_context_identity(self):
        async with self.client_for() as (_, first):
            submitted = await self.send_task(first)
            await self.poll_state(first, submitted["id"], "TASK_STATE_REJECTED")
            async with self.client_for() as (_, second):
                get_response = await second.get(f"/tasks/{submitted['id']}", headers=self.headers())
                self.assert_protocol_error(get_response, status=404)
                cancel_response = await second.post(f"/tasks/{submitted['id']}:cancel", headers=self.headers())
                self.assert_protocol_error(cancel_response, status=404)
                context_response = await second.post(
                    "/message:send", json=self.wire_request(context_id=submitted["contextId"]), headers=self.headers(),
                )
                self.assert_protocol_error(context_response, status=400)

    async def test_unknown_task_get_and_cancel_use_protocol_not_found_errors(self):
        async with self.client_for() as (_, client):
            task_id = str(uuid4())
            for method, path in (("GET", f"/tasks/{task_id}"), ("POST", f"/tasks/{task_id}:cancel")):
                with self.subTest(method=method):
                    response = await client.request(method, path, headers=self.headers())
                    self.assert_protocol_error(response, status=404)

    async def test_same_run_scenario_can_reuse_context_for_a_new_step_and_task(self):
        async with self.client_for() as (_, client):
            first = await self.send_task(client)
            await self.poll_state(client, first["id"], "TASK_STATE_REJECTED")
            metadata = self.metadata.model_copy(update={"workflow_step_id": uuid4(), "attempt": 1})
            second = await self.send_task(
                client, wire=self.wire_request(metadata=metadata, context_id=first["contextId"]),
            )
            task = await self.poll_state(client, second["id"], "TASK_STATE_REJECTED")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(first["contextId"], second["contextId"])
        self.assertEqual(task["metadata"], metadata.to_a2a_json())

    async def test_context_cannot_cross_run_or_scenario_boundaries(self):
        async with self.client_for() as (_, client):
            first = await self.send_task(client)
            await self.poll_state(client, first["id"], "TASK_STATE_REJECTED")
            for field in ("run_id", "scenario_id"):
                with self.subTest(field=field):
                    metadata = self.metadata.model_copy(update={field: uuid4()})
                    response = await client.post(
                        "/message:send", json=self.wire_request(metadata=metadata, context_id=first["contextId"]),
                        headers=self.headers(),
                    )
                    self.assert_protocol_error(response, status=400)

    async def test_caller_cannot_invent_a_context_or_task(self):
        async with self.client_for() as (_, client):
            response = await client.post(
                "/message:send", json=self.wire_request(context_id="caller-invented-context"), headers=self.headers(),
            )
            self.assert_protocol_error(response, status=400)
            response = await client.post(
                "/message:send", json=self.wire_request(task_id="caller-invented-task"), headers=self.headers(),
            )
            self.assert_protocol_error(response, status=404)

    async def test_terminal_task_cannot_be_restarted_by_send_message(self):
        for executor, state in ((None, "TASK_STATE_REJECTED"), (CompletedExecutor(), "TASK_STATE_COMPLETED")):
            with self.subTest(state=state):
                async with self.client_for(executor=executor) as (_, client):
                    submitted = await self.send_task(client)
                    await self.poll_state(client, submitted["id"], state)
                    response = await client.post(
                        "/message:send", json=self.wire_request(task_id=submitted["id"], context_id=submitted["contextId"]),
                        headers=self.headers(),
                    )
                    self.assert_protocol_error(response, status=400)

    async def test_existing_task_continuation_cannot_change_workflow_identity(self):
        async with self.client_for(executor=WaitingExecutor()) as (_, client):
            submitted = await self.send_task(client)
            await self.poll_state(client, submitted["id"], "TASK_STATE_WORKING")
            for changes in ({"workflow_step_id": uuid4()}, {"attempt": 1}):
                with self.subTest(changed_fields=tuple(changes)):
                    metadata = self.metadata.model_copy(update=changes)
                    response = await client.post(
                        "/message:send", json=self.wire_request(
                            metadata=metadata, task_id=submitted["id"], context_id=submitted["contextId"],
                        ), headers=self.headers(),
                    )
                    self.assert_protocol_error(response, status=400)

    async def test_cancel_confirms_working_task_state_and_preserves_metadata(self):
        executor = WaitingExecutor()
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send_task(client)
            await self.poll_state(client, submitted["id"], "TASK_STATE_WORKING")
            response = await client.post(f"/tasks/{submitted['id']}:cancel", headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            canceled = response.json()
            self.assertEqual(canceled["id"], submitted["id"])
            self.assertEqual(canceled["contextId"], submitted["contextId"])
            self.assertEqual(canceled["status"]["state"], "TASK_STATE_CANCELED")
            self.assertEqual(canceled["metadata"], self.metadata.to_a2a_json())
            await self.poll_state(client, submitted["id"], "TASK_STATE_CANCELED")
        self.assertEqual(executor.canceled_task_ids, [submitted["id"]])

    async def test_cancel_is_idempotent_after_confirmed_cancellation(self):
        executor = WaitingExecutor()
        async with self.client_for(executor=executor) as (_, client):
            submitted = await self.send_task(client)
            await self.poll_state(client, submitted["id"], "TASK_STATE_WORKING")
            path = f"/tasks/{submitted['id']}:cancel"
            first = await client.post(path, headers=self.headers())
            second = await client.post(path, headers=self.headers())
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(second.status_code, 200, second.text)
            self.assertEqual(first.json(), second.json())
        self.assertEqual(executor.canceled_task_ids, [submitted["id"]])

    async def test_rejected_and_completed_tasks_cannot_be_canceled(self):
        for executor, state in ((None, "TASK_STATE_REJECTED"), (CompletedExecutor(), "TASK_STATE_COMPLETED")):
            with self.subTest(state=state):
                async with self.client_for(executor=executor) as (_, client):
                    submitted = await self.send_task(client)
                    await self.poll_state(client, submitted["id"], state)
                    response = await client.post(f"/tasks/{submitted['id']}:cancel", headers=self.headers())
                    self.assert_protocol_error(response, status=400)
                    await self.poll_state(client, submitted["id"], state)

    async def test_existing_orchestrator_sdk_client_resolves_sends_polls_and_cancels(self):
        token = "DUMMY_CLIENT_BEARER"
        settings = AgentSettings(role="QA", bearer_token=token, _env_file=None)
        async with self.client_for(settings=settings, executor=WaitingExecutor()) as (_, http_client):
            async with A2AAgentClient(
                settings.agent_base_url, httpx_client=http_client,
                headers={"Authorization": f"Bearer {token}"},
            ) as client:
                card = await client.resolve_agent_card()
                self.assertEqual(card.name, "QA Agent")
                task = await client.send_task({"request": "Run a fixture check"}, self.metadata)
                for _ in range(50):
                    task = await client.get_task(task.id)
                    if task.status.state == TaskState.TASK_STATE_WORKING:
                        break
                    await asyncio.sleep(0)
                self.assertEqual(task.status.state, TaskState.TASK_STATE_WORKING)
                self.assertEqual(MessageToDict(task.metadata), self.metadata.to_a2a_json())
                canceled = await client.cancel_task(task.id)
                self.assertEqual(canceled.id, task.id)
                self.assertEqual(canceled.status.state, TaskState.TASK_STATE_CANCELED)
                self.assertEqual(MessageToDict(canceled.metadata), self.metadata.to_a2a_json())
                fetched = await client.get_task(task.id)
                self.assertEqual(fetched.status.state, TaskState.TASK_STATE_CANCELED)

    async def test_separate_submissions_receive_distinct_agent_owned_ids(self):
        async with self.client_for() as (_, client):
            first = await self.send_task(client)
            second = await self.send_task(client)
            await self.poll_state(client, first["id"], "TASK_STATE_REJECTED")
            await self.poll_state(client, second["id"], "TASK_STATE_REJECTED")
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(first["contextId"], second["contextId"])

    async def test_streaming_endpoints_are_explicitly_unsupported(self):
        async with self.client_for() as (_, client):
            for method, path in (("POST", "/message:stream"), ("GET", "/tasks/unknown:subscribe")):
                with self.subTest(path=path):
                    response = await client.request(method, path, headers=self.headers())
                    self.assert_protocol_error(response)
                    self.assertEqual(
                        response.json()["error"]["details"][0]["reason"],
                        "UNSUPPORTED_OPERATION",
                    )


if __name__ == "__main__":
    unittest.main()
