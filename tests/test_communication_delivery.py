"""68: distinguish unobserved delivery from proven mismatch; no paid calls."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from diagnostics.communication.contracts import ProbeRecord, summarize
from diagnostics.communication.natural import NaturalRuntime, run
from orchestrator.a2a import A2AAgentClient
import test_communication_natural as natural_tests
import test_communication_live as live_tests


def observed_row():
    row = ProbeRecord("PLANNER", 1, 0)
    row.phase = "COMPLETED"
    row.payload = row.wire_sent = row.wire_received = row.admitted = row.received = live_tests.payload()
    row.metadata_sent = row.metadata_received = row.metadata_response = {"attempt": 0}
    row.response_sent = row.response_received = {"answer": "observed"}
    row.identity_valid = True
    row.delivery_check = {"status": "PASS", "differences": []}
    return row


class DeliveryObservationTests(unittest.TestCase):
    def test_stage_distinguishes_unsent_unobserved_persisted_executed_and_result(self):
        self.assertEqual(ProbeRecord("PLANNER", 1, 0).view()["delivery"]["stage"], "NOT_DISPATCHED")
        row = observed_row()
        self.assertEqual(row.view()["delivery"]["stage"], "RESPONSE_VERIFIED")
        row.delivery_check = {"status": "UNVERIFIED", "differences": ["RECEIPT_NOT_OBSERVED"]}
        self.assertEqual(row.view()["delivery"]["stage"], "DELIVERY_UNVERIFIED")
        row.delivery_check = {"status": "PASS", "differences": []}
        row.response_received = None
        self.assertEqual(row.view()["delivery"]["stage"], "EXECUTOR_OBSERVED_RESULT_UNVERIFIED")
        row.received = None
        self.assertEqual(row.view()["delivery"]["stage"], "PERSISTED_ONLY")
        self.assertIsNone(row.view()["timings"]["receiptObservationMs"])

    def test_mismatch_keeps_raw_evidence_and_is_not_counted_as_verified(self):
        row = observed_row()
        row.delivery_check = {"status": "FAIL", "differences": ["payloadSha256"]}
        original = deepcopy(row.response_received)
        self.assertEqual(row.view()["delivery"]["stage"], "INTEGRITY_MISMATCH")
        summary = summarize([row])
        self.assertEqual(summary["verifiedRequests"], 0)
        self.assertEqual(summary["deliveryStages"], {"INTEGRITY_MISMATCH": 1})
        self.assertIn("deliveryReceipt", summary["issues"][0]["checks"])
        self.assertEqual(row.response_received, original)

    def test_content_and_delivery_are_independent(self):
        row = observed_row()
        view = row.view()
        self.assertEqual(view["delivery"]["stage"], "RESPONSE_VERIFIED")
        self.assertEqual(view["checks"]["sourceGrounding"]["status"], "FAIL")


class DeliveryFaultWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_absence_mismatch_and_lookup_failure_block_only_downstream_work(self):
        original_getter = A2AAgentClient.get_delivery_receipt
        for fault in ("missing", "mismatch", "unavailable"):
            with self.subTest(fault=fault):
                seen = []

                def respond(request):
                    body = json.loads(request.content)
                    received = json.loads(body["input"][0]["content"])["received"]
                    role = body["text"]["format"]["name"].removeprefix("natural_language_handoff_v2_").upper()
                    seen.append(role)
                    result = natural_tests.answer(received["requestText"], role)
                    return httpx.Response(200, json={"id": "resp_synthetic", "status": "completed",
                        "model": natural_tests.MODEL, "output": [{"id": "msg_synthetic", "type": "message",
                            "role": "assistant", "status": "completed", "content": [{"type": "output_text",
                                "text": json.dumps(result, ensure_ascii=False), "annotations": []}]}],
                        "usage": {"input_tokens": 10, "output_tokens": 10, "total_tokens": 20}})

                async def getter(client, message_id):
                    if fault == "unavailable":
                        raise ValueError("DUMMY_PRIVATE_LOOKUP_ERROR")
                    receipt = await original_getter(client, message_id)
                    self.assertIsNotNone(receipt)
                    return None if fault == "missing" else receipt.model_copy(update={"payload_sha256": "0" * 64})

                runtime = NaturalRuntime(SecretStr("SYNTHETIC_TEST_ONLY"), natural_tests.MODEL,
                    max_model_calls=15, transport_factory=lambda: httpx.MockTransport(respond))
                with TemporaryDirectory() as directory, patch.object(A2AAgentClient, "get_delivery_receipt", new=getter):
                    report = await run(SimpleNamespace(model=natural_tests.MODEL, out=Path(directory) / "evidence"), runtime=runtime)
                self.assertEqual(seen, ["PLANNER", "SINGLE_AGENT"] * 3)
                self.assertNotIn("DUMMY_PRIVATE_LOOKUP_ERROR", json.dumps(report))
                for group in report["runs"]:
                    row = group["rows"][0]
                    self.assertEqual(row["phase"], "COMPLETED")
                    self.assertIsNotNone(row["taskId"])
                    self.assertEqual(row["checks"]["request"]["endToEnd"]["status"], "PASS")
                    self.assertIsNotNone(row["observations"]["responseReceived"])
                    self.assertFalse(row["handoff"]["eligible"])
                    self.assertEqual(row["delivery"]["check"]["status"], "FAIL" if fault == "mismatch" else "UNVERIFIED")
                    self.assertEqual(row["delivery"]["stage"], "INTEGRITY_MISMATCH" if fault == "mismatch" else "DELIVERY_UNVERIFIED")
                    for skipped in group["rows"][1:]:
                        self.assertEqual(skipped["phase"], "SKIPPED")
                        self.assertEqual(skipped["error"], "UPSTREAM_DELIVERY_NOT_VERIFIED")
