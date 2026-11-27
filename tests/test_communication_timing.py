"""69: deterministic local time, streamed bodies, failure and cancellation."""

import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx
from pydantic import SecretStr

from agents.llm.contracts import JsonSchema, LLMRuntimeError
from diagnostics.communication.contracts import ProbeRecord
from diagnostics.communication.live import LiveRuntime
from diagnostics.communication.lab import CommunicationLab
from diagnostics.communication.timing import span, timing_view
import test_communication_live as fixtures


class ManualClock:
    def __init__(self):
        self.value = 1_000_000_000

    def now(self):
        return self.value

    def advance(self, milliseconds):
        self.value += milliseconds * 1_000_000


class TimedBody(httpx.AsyncByteStream):
    def __init__(self, clock, payload, *, fail=False, gate=None):
        self.clock, self.payload, self.fail, self.gate = clock, payload, fail, gate
        self.closed = False

    async def __aiter__(self):
        self.clock.advance(30)
        yield self.payload[:len(self.payload) // 2]
        if self.gate is not None:
            self.gate.set()
            await asyncio.Event().wait()
        if self.fail:
            raise httpx.ReadError("DUMMY_PRIVATE_BODY_ERROR")
        self.clock.advance(40)
        yield self.payload[len(self.payload) // 2:]

    async def aclose(self):
        self.closed = True


class TimedTransport(httpx.AsyncBaseTransport):
    def __init__(self, clock, *, fail=False, gate=None):
        self.clock, self.fail, self.gate = clock, fail, gate
        self.requests, self.body, self.closed = [], None, False

    async def handle_async_request(self, request):
        body = json.loads(request.content)
        self.requests.append(body)
        self.clock.advance(20)
        self.body = TimedBody(self.clock, json.dumps(fixtures.provider_response(body)).encode(),
                              fail=self.fail, gate=self.gate)
        return httpx.Response(200, stream=self.body)

    async def aclose(self):
        self.closed = True
        self.clock.advance(3)


class TimingContractTests(unittest.TestCase):
    def test_missing_reverse_and_invalid_points_are_never_reported_as_zero(self):
        self.assertEqual(span({}, "a", "b"), {"status": "UNVERIFIED", "durationMs": None})
        for points in ({"a": 2, "b": 1}, {"a": True, "b": 2}, {"a": -1, "b": 1},
                       {"a": float("nan"), "b": 2}, {"a": "1", "b": 2}):
            with self.subTest(points=points):
                self.assertEqual(span(points, "a", "b"), {"status": "INVALID", "durationMs": None})
        self.assertEqual(span({"a": 1, "b": 1}, "a", "b"), {"status": "MEASURED", "durationMs": 0.0})

    def test_zero_origin_and_shared_relative_intervals_are_valid(self):
        result = timing_view({"prepare": 1_000_000, "finish": 5_000_000}, origin_ns=0, clock_id="shared")
        self.assertEqual(result["interval"], {"status": "MEASURED", "startMs": 1.0, "endMs": 5.0})
        self.assertEqual(result["phases"]["totalMs"]["durationMs"], 4.0)
        self.assertNotIn("origin_ns", result)

    def test_local_spans_do_not_invent_an_origin_for_old_or_standalone_records(self):
        result = timing_view({"modelStart": 1, "modelFinish": 11})
        self.assertEqual(result["interval"]["status"], "UNVERIFIED")
        self.assertEqual(result["phases"]["modelTotalMs"]["status"], "MEASURED")

    def test_cross_phase_clock_reversal_is_detected_even_with_positive_total(self):
        result = timing_view({"prepare": 0, "executorStart": 100, "modelStart": 90, "finish": 200},
                             origin_ns=0, clock_id="shared")
        self.assertEqual(result["ordering"], "FAIL")
        self.assertEqual(result["interval"]["status"], "INVALID")
        self.assertIsNone(result["phases"]["executorBeforeModelMs"]["durationMs"])

    def test_internal_marks_do_not_trigger_observer_or_change_phase(self):
        row = ProbeRecord("DEVELOPER", 1, 0)
        row.notify = lambda _: self.fail("internal model timing must not emit UI events")
        row.mark_time("modelStart")
        self.assertEqual(row.phase, "PLANNED")
        self.assertEqual(row.observer_overhead_ns, 0)


class ProviderTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reusing_probe_does_not_replay_model_or_mix_attempt_timings(self):
        seen = []
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL,
                              transport_factory=fixtures.transport_factory(seen))
        row = ProbeRecord("DEVELOPER", 1, 0)
        await runtime.complete(fixtures.payload(), row)
        original_points = dict(row.points)
        original_observation = dict(row.model_observation)
        with self.assertRaises(LLMRuntimeError):
            await runtime.complete(fixtures.payload(), row)
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(runtime.calls), 1)
        self.assertEqual(row.points, original_points)
        self.assertEqual(row.model_observation, original_observation)

    async def test_receipt_lookup_uses_existing_task_deadline_and_preserves_cancellation_id(self):
        from a2a.helpers import new_task
        from a2a.types import TaskState
        clock = [0.0]
        cancellations = []

        class Peer:
            def __init__(self, *args, **kwargs):
                pass

            async def resolve_agent_card(self):
                pass

            async def send_task(self, payload, metadata, **kwargs):
                task = new_task(task_id="opaque-task", context_id="opaque-context", state=TaskState.TASK_STATE_WORKING)
                task.metadata.update(metadata.to_a2a_json())
                return task

            async def get_delivery_receipt(self, message_id):
                clock[0] = 30.0
                return None

            async def get_task(self, task_id):
                raise AssertionError("expired observation deadline must not start another poll")

            async def cancel_task(self, task_id):
                cancellations.append(task_id)
                return new_task(task_id=task_id, context_id="opaque-context", state=TaskState.TASK_STATE_CANCELED)

        lab = CommunicationLab(SimpleNamespace(timeout_seconds=5))
        lab.endpoints["DEVELOPER"], lab.tokens["DEVELOPER"] = "https://local.invalid", "DUMMY_LOCAL_TOKEN"
        row = ProbeRecord("DEVELOPER", 1, 0)
        row.payload = {"probeId": row.probe_id}
        local_asyncio = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
        local_asyncio.get_running_loop = lambda: SimpleNamespace(time=lambda: clock[0])
        with patch("diagnostics.communication.lab.A2AAgentClient", new=Peer), \
                patch("diagnostics.communication.lab.asyncio", new=local_asyncio):
            result = await lab.send_probe(row, uuid4(), uuid4(), [uuid4()])
        self.assertIsNone(result)
        self.assertEqual(row.phase, "TIMEOUT")
        self.assertEqual(row.task_id, "opaque-task")
        self.assertEqual(cancellations, ["opaque-task"])

    async def test_body_transfer_is_not_confused_with_headers_or_local_validation(self):
        clock = ManualClock()
        delegate = TimedTransport(clock)
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL, transport_factory=lambda: delegate)
        row = ProbeRecord("DEVELOPER", 1, 0)
        original_contract, original_validate = runtime.output_contract, JsonSchema.validate

        def contract(payload, role):
            clock.advance(12)
            return original_contract(payload, role)

        def validate(schema, value):
            clock.advance(5)
            return original_validate(schema, value)

        with patch("diagnostics.communication.contracts.now", side_effect=clock.now), \
                patch.object(runtime, "output_contract", side_effect=contract), \
                patch.object(JsonSchema, "validate", new=validate):
            result = await runtime.complete(fixtures.payload(), row)
        self.assertEqual(result["claims"]["value"], 100)
        times = row.model_observation["timings"]
        self.assertEqual(times["modelPrepareMs"], 12)
        self.assertEqual(times["providerHeadersMs"], 20)
        self.assertEqual(times["providerExchangeMs"], 90)
        self.assertEqual(times["providerDecodeMs"], 0)
        self.assertEqual(times["modelValidationMs"], 5)
        self.assertEqual(times["modelCleanupMs"], 3)
        self.assertEqual(times["modelTotalMs"], 110)
        self.assertEqual(row.model_observation["apiRoundTripMs"], 95)
        self.assertEqual(len(delegate.requests), 1)
        self.assertTrue(delegate.closed and delegate.body.closed)
        body = delegate.requests[0]
        self.assertFalse(body["stream"])
        self.assertFalse(body["store"])
        self.assertEqual(body["tools"], [])
        self.assertEqual(json.loads(body["input"][0]["content"]), {"received": fixtures.payload()})
        self.assertNotIn(fixtures.KEY, json.dumps(runtime.calls))

    async def test_buffered_custom_response_has_a_real_body_observation(self):
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL,
                              transport_factory=fixtures.transport_factory([]))
        row = ProbeRecord("DEVELOPER", 1, 0)
        await runtime.complete(fixtures.payload(), row)
        self.assertIsNotNone(row.model_observation["timings"]["providerExchangeMs"])
        self.assertLessEqual(row.points["apiHeaders"], row.points["apiBodyRead"])

    async def test_partial_body_failure_preserves_failure_and_does_not_invent_receive_end(self):
        clock = ManualClock()
        delegate = TimedTransport(clock, fail=True)
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL, transport_factory=lambda: delegate)
        row = ProbeRecord("DEVELOPER", 1, 0)
        with patch("diagnostics.communication.contracts.now", side_effect=clock.now):
            with self.assertRaises(LLMRuntimeError):
                await runtime.complete(fixtures.payload(), row)
        self.assertEqual(row.model_observation["status"], "FAILED")
        self.assertIsNone(row.model_observation["timings"]["providerExchangeMs"])
        self.assertNotIn("apiBodyRead", row.points)
        self.assertNotIn("DUMMY_PRIVATE_BODY_ERROR", json.dumps(runtime.calls))
        self.assertEqual(len(delegate.requests), 1)
        self.assertTrue(delegate.closed and delegate.body.closed)

    async def test_cancellation_closes_stream_and_keeps_exchange_unverified(self):
        clock, entered = ManualClock(), asyncio.Event()
        delegate = TimedTransport(clock, gate=entered)
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL, transport_factory=lambda: delegate)
        row = ProbeRecord("DEVELOPER", 1, 0)
        with patch("diagnostics.communication.contracts.now", side_effect=clock.now):
            work = asyncio.create_task(runtime.complete(fixtures.payload(), row))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                work.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await work
            finally:
                if not work.done():
                    work.cancel()
                    await asyncio.gather(work, return_exceptions=True)
        self.assertEqual(row.model_observation["status"], "CANCELED")
        self.assertIsNone(row.model_observation["timings"]["providerExchangeMs"])
        self.assertIsNotNone(row.model_observation["timings"]["modelTotalMs"])
        self.assertTrue(delegate.closed and delegate.body.closed)

    async def test_preparation_error_has_no_fake_api_duration_or_extra_calls(self):
        runtime = LiveRuntime(SecretStr(fixtures.KEY), fixtures.MODEL,
                              transport_factory=lambda: self.fail("provider must not be created"))
        row = ProbeRecord("DEVELOPER", 1, 0)
        with patch.object(runtime, "output_contract", side_effect=ValueError("DUMMY_PRIVATE_PREP_ERROR")):
            with self.assertRaises(ValueError):
                await runtime.complete(fixtures.payload(), row)
        self.assertEqual(row.model_observation["status"], "FAILED")
        self.assertIsNone(row.model_observation["apiRoundTripMs"])
        self.assertIsNone(row.model_observation["timings"]["providerExchangeMs"])
        self.assertNotIn("DUMMY_PRIVATE_PREP_ERROR", json.dumps(runtime.calls))
