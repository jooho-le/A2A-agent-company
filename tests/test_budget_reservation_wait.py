"""Codex review 2026-11-18 #2: a reservation held by the other role is not budget exhaustion.

Parallel QA and Security share one Run budget. When only the other call's pending token
reservation blocks a new call, the engine waits for it to settle within the Run deadline
instead of failing with LLM_BUDGET_EXHAUSTED. Real exhaustion still fails at once.
No cloud call; fake provider only.
"""
import asyncio
import unittest

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, TokenUsage
from agents.llm.engine import LLMEngine
import test_llm_runtime as runtime


class PendingReservationRules(unittest.TestCase):
    def budget(self, cap, output=100):
        return ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits(max_total_tokens=cap, max_output_tokens=output))

    def test_only_another_pending_reservation_means_wait(self):
        budget = self.budget(1000)
        sequence, _cap, _timeout = budget.reserve_model_call(input_tokens=600)
        with self.assertRaises(LLMRuntimeError):
            budget.reserve_model_call(input_tokens=300)
        self.assertTrue(budget.waits_for_pending_reservations(input_tokens=300))
        budget.account_usage(TokenUsage(100, 10, 110), sequence=sequence)
        self.assertEqual(budget.reserve_model_call(input_tokens=300)[0], 2)

    def test_real_exhaustion_does_not_wait(self):
        budget = self.budget(1000)
        first, _cap, _timeout = budget.reserve_model_call(input_tokens=100)
        budget.account_usage(TokenUsage(900, 50, 950), sequence=first)
        second, _cap, _timeout = budget.reserve_model_call(input_tokens=10)
        # Even after the pending call settles there is no room: exhaustion, not waiting.
        self.assertFalse(budget.waits_for_pending_reservations(input_tokens=500))
        budget.account_usage(TokenUsage(10, 1, 11), sequence=second)
        self.assertFalse(budget.waits_for_pending_reservations(input_tokens=10))  # nothing pending

    def test_no_wait_without_a_token_cap_or_after_the_call_limit(self):
        uncapped = ExecutionBudget(runtime_budget_ms=10000, limits=LLMLimits())
        self.assertFalse(uncapped.waits_for_pending_reservations(input_tokens=10))
        limited = ExecutionBudget(runtime_budget_ms=10000,
                                  limits=LLMLimits(max_total_tokens=1000, max_output_tokens=100, max_model_calls=1))
        limited.reserve_model_call(input_tokens=600)
        self.assertFalse(limited.waits_for_pending_reservations(input_tokens=300))


class ParallelRolesShareOneBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime.LLMRuntimeTests('runTest')
        self.f.setUp()

    async def test_second_role_waits_for_the_first_reservation_instead_of_failing(self):
        release = asyncio.Event()
        reserved = asyncio.Event()

        async def slow(_request):
            reserved.set()
            await release.wait()
            return runtime.text_response()

        first = runtime.FakeProvider(slow)
        second = runtime.FakeProvider(runtime.text_response())
        for provider in (first, second):
            provider.name = 'openai'
        model = self.f.model.model_copy(update={'provider': 'openai', 'seed': None})
        # Measure one call's reservation, then cap the budget so one fits but two at once do not.
        probe = self.f.budget(max_total_tokens=1_000_000)
        probe_provider = runtime.FakeProvider(runtime.text_response())
        probe_provider.name = 'openai'
        await self.f.run_engine(probe_provider, model=model, budget=probe)
        budget = self.f.budget(max_total_tokens=1_000_000)
        engine_a = LLMEngine(role=self.f.role, provider=first)
        run_a = asyncio.create_task(engine_a.run(prompt=self.f.prompt, model=model, output=self.f.output,
                                                 budget=budget, workspace_id=self.f.workspace_id))
        await reserved.wait()
        held = sum(budget.snapshot.pending_token_reservations.values()) if budget.snapshot else None
        pending = held if held is not None else sum(budget._pending_tokens.values())
        # Re-cap a fresh budget around the observed reservation (one fits, two at once do not).
        release.set()
        await run_a
        self.assertGreater(pending, 0)
        cap = budget.known_total_tokens + pending + pending // 2
        shared = self.f.budget(max_total_tokens=cap)
        release.clear()
        reserved.clear()
        first.script = [slow]
        second.script = [runtime.text_response()]
        run_a = asyncio.create_task(LLMEngine(role=self.f.role, provider=first).run(
            prompt=self.f.prompt, model=model, output=self.f.output, budget=shared,
            workspace_id=self.f.workspace_id))
        await reserved.wait()
        run_b = asyncio.create_task(LLMEngine(role=self.f.role, provider=second).run(
            prompt=self.f.prompt, model=model, output=self.f.output, budget=shared,
            workspace_id=self.f.workspace_id))
        await asyncio.sleep(0.2)
        self.assertFalse(run_b.done())       # waiting, not failed with LLM_BUDGET_EXHAUSTED
        self.assertEqual(second.requests, [])
        release.set()
        result_a, result_b = await asyncio.gather(run_a, run_b)
        self.assertEqual((result_a.data, result_b.data), ({'answer': 'done'}, {'answer': 'done'}))
        self.assertEqual(shared.model_calls, 2)

    async def test_retry_after_transient_error_also_waits_for_the_other_reservation(self):
        """Codex review 2026-11-18 #1: the re-send decision followed the old immediate rule and gave up
        while the other role's reservation was pending; it now waits and then re-sends once."""
        from unittest import mock
        from agents.llm import engine as engine_module
        patcher = mock.patch.object(engine_module, 'PROVIDER_RETRY_DELAY_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        release, reserved = asyncio.Event(), asyncio.Event()

        async def slow(_request):
            reserved.set()
            await release.wait()
            return runtime.text_response()
        model = self.f.model.model_copy(update={'provider': 'openai', 'seed': None})
        # One call's reservation R (measured), then a cap where: both first calls fit (2R); the re-send
        # is blocked only while the other reservation is pending; and fits after it settles.
        measure = self.f.budget(max_total_tokens=1_000_000)
        probe = runtime.FakeProvider(slow)
        probe.name = 'openai'
        task = asyncio.create_task(LLMEngine(role=self.f.role, provider=probe).run(
            prompt=self.f.prompt, model=model, output=self.f.output, budget=measure, workspace_id=self.f.workspace_id))
        await reserved.wait()
        reservation = sum(measure._pending_tokens.values())
        release.set()
        await task
        release.clear()
        reserved.clear()
        shared = self.f.budget(max_total_tokens=reservation * 5 // 2, max_provider_retries=1, max_model_calls=8)
        first = runtime.FakeProvider(slow)
        second = runtime.FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), runtime.text_response())
        for provider in (first, second):
            provider.name = 'openai'
        run_a = asyncio.create_task(LLMEngine(role=self.f.role, provider=first).run(
            prompt=self.f.prompt, model=model, output=self.f.output, budget=shared, workspace_id=self.f.workspace_id))
        await reserved.wait()
        run_b = asyncio.create_task(LLMEngine(role=self.f.role, provider=second).run(
            prompt=self.f.prompt, model=model, output=self.f.output, budget=shared, workspace_id=self.f.workspace_id))
        await asyncio.sleep(0.2)
        self.assertFalse(run_b.done())               # waiting to re-send, not given up
        self.assertEqual(len(second.requests), 1)
        release.set()
        result_a, result_b = await asyncio.gather(run_a, run_b)
        self.assertEqual(len(second.requests), 2)    # the single allowed re-send was actually sent
        self.assertEqual((result_a.data, result_b.data), ({'answer': 'done'}, {'answer': 'done'}))

    async def test_true_exhaustion_still_fails_immediately(self):
        provider = runtime.FakeProvider(runtime.text_response())
        provider.name = 'openai'
        model = self.f.model.model_copy(update={'provider': 'openai', 'seed': None})
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.f.run_engine(provider, model=model, budget=self.f.budget(max_total_tokens=100))
        self.assertEqual(raised.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(provider.requests, [])


if __name__ == '__main__':
    unittest.main()
