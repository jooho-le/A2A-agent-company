"""67: stored admission evidence, JSON fidelity and authenticated read API."""

from datetime import datetime, timezone
import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from a2a.types import Task, TaskState
from a2a.utils.constants import A2A_JSON_MEDIA_TYPE, VERSION_HEADER
from google.protobuf.json_format import MessageToDict, ParseDict
import httpx
from pydantic import ValidationError

import test_agent_server as server_tests
import test_agent_task_store as store_tests
from agents.api.sqlite_task_store import AgentTaskStoreError
from agents.core.config import AgentSettings
from orchestrator.a2a import A2AAgentClient, A2AWorkflowMetadata, build_send_message_request
from orchestrator.a2a.delivery import (
    DeliveryReceipt, DeliveryReceiptError, build_delivery_receipt, request_sha256, verify_delivery,
)
from orchestrator.a2a.requests import A2AProjectContractError
from orchestrator.domain.json_fingerprint import json_fingerprint


class DeliveryContractTests(unittest.TestCase):
    def setUp(self):
        self.metadata = A2AWorkflowMetadata(runId=uuid4(), workflowStepId=uuid4(), scenarioId=uuid4(), attempt=0)
        self.request = build_send_message_request({"value": 100, "text": "원문\n한글", "password": "DUMMY_ONLY"}, self.metadata)
        self.task = Task(id="remote/task?opaque#1", context_id="opaque context")
        self.receipt = build_delivery_receipt(self.request, task_id=self.task.id,
            context_id=self.task.context_id, receiver_role="PLANNER", received_at=datetime.now(timezone.utc))

    def check(self, receipt=None, request=None):
        return verify_delivery(request or self.request, self.task, receipt or self.receipt, receiver_role="PLANNER")

    def test_json_key_order_and_integral_numbers_are_equivalent_but_types_and_order_are_not(self):
        self.assertEqual(json_fingerprint({"가": 100, "b": [None, True]}),
                         json_fingerprint({"b": [None, True], "가": 100.0}))
        for first, second in [(False, 0), (True, 1), ("100", 100), ([1, 2], [2, 1]), ("é", "e\u0301")]:
            with self.subTest(first=first):
                self.assertNotEqual(json_fingerprint(first), json_fingerprint(second))

    def test_invalid_or_unbounded_json_is_rejected_with_safe_errors(self):
        nested = {}
        for _ in range(66):
            nested = {"a": nested}
        for value in [float("nan"), float("inf"), {1: "not a string key"}, object(), nested, "x" * 1_048_576]:
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(ValueError, "^JSON_FINGERPRINT_INVALID$"):
                json_fingerprint(value)

    def test_lossy_struct_integer_conversion_is_blocked_before_network(self):
        with self.assertRaises(A2AProjectContractError):
            build_send_message_request({"value": 2 ** 53 + 1}, self.metadata)
        with self.assertRaises(A2AProjectContractError):
            build_send_message_request({"value": 100}, self.metadata.model_copy(update={"attempt": 2 ** 53 + 1}))
        for value in [100, 100.0, 2 ** 53, 2 ** 60, False, "9007199254740993"]:
            request = build_send_message_request({"value": value}, self.metadata)
            self.assertEqual(json_fingerprint({"value": value}), json_fingerprint(MessageToDict(request.message.parts[0].data)))

    def test_receipt_matches_without_exposing_source_or_claiming_execution(self):
        self.assertEqual(self.check()["status"], "PASS")
        self.assertEqual(self.receipt.request_sha256, request_sha256(self.request))
        self.assertNotIn("DUMMY_ONLY", self.receipt.model_dump_json())
        self.assertNotIn("원문", self.receipt.model_dump_json())
        self.assertNotIn("status", self.receipt.model_dump(by_alias=True))
        self.assertEqual(self.receipt.task_id, "remote/task?opaque#1")

    def test_missing_receipt_is_unverified_not_proven_loss(self):
        self.assertEqual(verify_delivery(self.request, self.task, None, receiver_role="PLANNER"),
                         {"status": "UNVERIFIED", "differences": ["RECEIPT_NOT_OBSERVED"]})

    def test_each_binding_and_hash_mismatch_is_detected_without_echoing_values(self):
        changed = {"message_id": str(uuid4()), "receiver_role": "QA", "task_id": "private-task-value",
                   "context_id": "private-context-value", "run_id": uuid4(), "workflow_step_id": uuid4(),
                   "scenario_id": uuid4(), "attempt": 1, "code_version": 1,
                   "request_sha256": "0" * 64, "payload_sha256": "0" * 64, "metadata_sha256": "0" * 64}
        for field, value in changed.items():
            with self.subTest(field=field):
                result = self.check(self.receipt.model_copy(update={field: value}))
                self.assertEqual(result["status"], "FAIL")
                self.assertIn(DeliveryReceipt.model_fields[field].alias, result["differences"])
                self.assertNotIn("private-", json.dumps(result))

    def test_changed_payload_or_configuration_is_not_accepted(self):
        for mutate in (lambda req: ParseDict({**MessageToDict(req.message.parts[0]),
                           "data": {**MessageToDict(req.message.parts[0].data), "value": 101}}, req.message.parts[0]),
                       lambda req: setattr(req.configuration, "return_immediately", False)):
            request = type(self.request)()
            request.CopyFrom(self.request)
            mutate(request)
            self.assertEqual(self.check(request=request)["status"], "FAIL")
        self.assertEqual(self.check()["status"], "PASS")

    def test_copied_invalid_models_and_extra_or_coerced_fields_are_rejected(self):
        result = self.check(self.receipt.model_copy(update={"schema_version": 2}))
        self.assertEqual(result, {"status": "FAIL", "differences": ["RECEIPT_CONTRACT_INVALID"]})
        wire = self.receipt.model_dump(mode="json", by_alias=True)
        for changes in ({"attempt": True}, {"attempt": "0"}, {"schemaVersion": True}, {"extra": "bad"},
                        {"receivedAt": "2026-11-12T00:00:00"}, {"payloadSha256": "bad"}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                DeliveryReceipt.model_validate({**wire, **changes})


class DeliveryStoreTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = store_tests.AgentTaskStoreTests.asyncSetUp
    request = store_tests.AgentTaskStoreTests.request
    claim = store_tests.AgentTaskStoreTests.claim
    write_state = store_tests.AgentTaskStoreTests.write_state

    async def test_receipt_is_durable_read_only_and_does_not_execute_a_task(self):
        request = self.request({"request": "원문", "password": "DUMMY_RECEIPT_SECRET"})
        claim = await self.claim(request)
        before = await self.store.get(claim.task.id, self.context)
        receipt = await self.store.get_delivery_receipt(request.message.message_id, self.context)
        duplicate = await self.claim(request)
        again = await self.store.get_delivery_receipt(request.message.message_id, self.context)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(receipt, again)
        self.assertEqual(verify_delivery(request, claim.task, receipt, receiver_role="PLANNER")["status"], "PASS")
        self.assertNotIn("DUMMY_RECEIPT_SECRET", receipt.model_dump_json())
        after = await self.store.get(claim.task.id, self.context)
        self.assertEqual(before.version, after.version)
        self.assertEqual(after.task.status.state, TaskState.TASK_STATE_SUBMITTED)
        await self.store.aclose()
        await self.store.start()
        recovered = await self.store.get_delivery_receipt(request.message.message_id, self.context)
        self.assertEqual(receipt, recovered)

    async def test_continuation_preserves_original_and_new_message_receipts(self):
        request = self.request()
        claim = await self.claim(request)
        original = await self.store.get_delivery_receipt(request.message.message_id, self.context)
        await self.write_state(claim.task.id, TaskState.TASK_STATE_INPUT_REQUIRED)
        continuation = self.request({"request": "추가 답변"}, task_id=claim.task.id, context_id=claim.task.context_id,
                                    metadata=self.metadata.model_copy(update={"attempt": 1}))
        later = await self.claim(continuation)
        newer = await self.store.get_delivery_receipt(continuation.message.message_id, self.context)
        self.assertEqual(await self.store.get_delivery_receipt(request.message.message_id, self.context), original)
        self.assertEqual((original.attempt, newer.attempt), (0, 1))
        self.assertEqual(original.task_id, newer.task_id)
        self.assertNotEqual(original.request_sha256, newer.request_sha256)
        self.assertEqual(verify_delivery(continuation, later.task, newer, receiver_role="PLANNER")["status"], "PASS")

    async def test_unknown_and_other_owner_have_no_receipt(self):
        request = self.request()
        await self.claim(request)
        self.assertIsNone(await self.store.get_delivery_receipt(str(uuid4()), self.context))
        with patch("agents.api.sqlite_task_store.resolve_user_scope", return_value="other-owner"):
            self.assertIsNone(await self.store.get_delivery_receipt(request.message.message_id, self.context))

    async def test_integrity_or_lease_failure_is_safe_and_never_returns_a_receipt(self):
        request = self.request()
        await self.claim(request)
        with patch("agents.api.sqlite_task_store.request_sha256", return_value="0" * 64):
            with self.assertRaisesRegex(AgentTaskStoreError, "^DELIVERY_RECEIPT_UNAVAILABLE$"):
                await self.store.get_delivery_receipt(request.message.message_id, self.context)
        await self.store.aclose()
        with self.assertRaisesRegex(AgentTaskStoreError, "^DELIVERY_RECEIPT_UNAVAILABLE$"):
            await self.store.get_delivery_receipt(request.message.message_id, self.context)


class DeliveryAPITests(unittest.IsolatedAsyncioTestCase):
    setUp = server_tests.AgentServerTests.setUp
    client_for = server_tests.AgentServerTests.client_for
    wire_request = server_tests.AgentServerTests.wire_request
    headers = staticmethod(server_tests.AgentServerTests.headers)
    send_task = server_tests.AgentServerTests.send_task

    async def test_receipt_endpoint_and_sdk_client_use_real_store_without_reexecution(self):
        token = "DUMMY_RECEIPT_AUTH"
        settings = AgentSettings(role="PLANNER", bearer_token=token, _env_file=None)
        executor = server_tests.WaitingExecutor()
        async with self.client_for(settings=settings, executor=executor) as (_, client):
            wire = self.wire_request()
            task = await self.send_task(client, wire=wire, token=token)
            path = "/project/delivery-receipts/" + wire["message"]["messageId"]
            reply = await client.get(path, headers=self.headers(token=token))
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.headers[VERSION_HEADER], "1.0")
            self.assertEqual(reply.headers["content-type"].partition(";")[0], A2A_JSON_MEDIA_TYPE)
            self.assertEqual(reply.headers["cache-control"], "no-store")
            self.assertEqual(reply.json()["taskId"], task["id"])
            sdk = A2AAgentClient(settings.agent_base_url, httpx_client=client, trust_env=False)
            # Reuse the authenticated transport, just as the product client does.
            client.headers.update(self.headers(token=token))
            receipt = await sdk.get_delivery_receipt(wire["message"]["messageId"])
            self.assertEqual(receipt.model_dump(mode="json", by_alias=True), reply.json())
            self.assertIsNone(await sdk.get_delivery_receipt(str(uuid4())))
            self.assertLessEqual(len(executor.inputs), 1)
            self.assertNotIn(token, reply.text)

    async def test_auth_version_query_and_id_guards_apply_before_lookup(self):
        token = "DUMMY_RECEIPT_AUTH"
        settings = AgentSettings(role="PLANNER", bearer_token=token, _env_file=None)
        async with self.client_for(settings=settings, executor=server_tests.CompletedExecutor()) as (_, client):
            path = "/project/delivery-receipts/" + str(uuid4())
            for headers, expected in [(self.headers(), 401), (self.headers(token="wrong"), 401),
                                      ({"Authorization": "Bearer " + token}, 400)]:
                self.assertEqual((await client.get(path, headers=headers)).status_code, expected)
            headers = self.headers(token=token)
            self.assertEqual((await client.get(path, headers=headers)).status_code, 404)
            self.assertEqual((await client.get(path + "?owner=other", headers=headers)).status_code, 400)
            self.assertEqual((await client.get("/project/delivery-receipts/not-uuid", headers=headers)).status_code, 400)

    async def test_client_validates_response_contract_and_never_prints_provider_errors(self):
        fixture = DeliveryContractTests()
        fixture.setUp()
        valid = fixture.receipt.model_dump(mode="json", by_alias=True)
        for kind in ("wrong-version", "wrong-media", "invalid-body", "wrong-message", "server-error"):
            def respond(request):
                if request.url.path.endswith("agent-card.json"):
                    raise AssertionError("Card resolution was isolated in this client unit test")
                headers = {VERSION_HEADER: "2.0" if kind == "wrong-version" else "1.0",
                           "Content-Type": "application/json" if kind == "wrong-media" else A2A_JSON_MEDIA_TYPE}
                body = {"private": "DUMMY_PRIVATE_ERROR"} if kind in {"invalid-body", "server-error"} else valid
                if kind == "wrong-message":
                    body = {**valid, "messageId": str(uuid4())}
                return httpx.Response(503 if kind == "server-error" else 200, json=body, headers=headers)
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
                client = A2AAgentClient("https://agent.example.test", httpx_client=transport)
                client._interface_url = "https://agent.example.test"
                with patch.object(client, "_require_client", return_value=None):
                    with self.assertRaisesRegex(DeliveryReceiptError, "^DELIVERY_RECEIPT_INVALID$"):
                        await client.get_delivery_receipt(fixture.receipt.message_id)
