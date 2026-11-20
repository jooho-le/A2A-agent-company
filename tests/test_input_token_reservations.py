"""Pre-send input allowance, concurrency, actual billing and durable accounting."""
import unittest

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.llm.contracts import LLMRuntimeError, TokenUsage
from agents.platform.telemetry_store import TelemetryStoreError
import test_agent_durable_budgets as durable
import test_llm_runtime as runtime


class TokenReservationTests(unittest.TestCase):
    def test_next_input_is_reserved_before_call_and_not_counted_as_actual_usage(self):
        budget=ExecutionBudget(runtime_budget_ms=10000,limits=LLMLimits(max_total_tokens=500000,max_output_tokens=8192))
        seq,_,_=budget.reserve_model_call(input_tokens=100000)
        budget.account_usage(TokenUsage(488000,1000,489000),sequence=seq)
        with self.assertRaises(LLMRuntimeError): budget.reserve_model_call(input_tokens=72431)
        self.assertEqual(budget.model_calls,1);self.assertEqual(budget.total_tokens,489000)

    def test_pending_calls_cannot_reserve_the_same_tokens_twice(self):
        budget=ExecutionBudget(runtime_budget_ms=10000,limits=LLMLimits(max_total_tokens=1000,max_output_tokens=100))
        seq,cap,_=budget.reserve_model_call(input_tokens=600)
        self.assertEqual(cap,100)
        with self.assertRaises(LLMRuntimeError): budget.reserve_model_call(input_tokens=300)
        self.assertEqual(budget.model_calls,1)
        budget.account_usage(TokenUsage(100,10,110),sequence=seq)
        self.assertEqual(budget.reserve_model_call(input_tokens=600)[0],2)

    def test_receipt_must_identify_its_input_reservation(self):
        budget=ExecutionBudget(runtime_budget_ms=10000,limits=LLMLimits(max_total_tokens=1000))
        seq,_,_=budget.reserve_model_call(input_tokens=100)
        with self.assertRaises(LLMRuntimeError): budget.account_usage(TokenUsage(100,10,110))
        self.assertEqual(budget.total_tokens,0)
        budget.account_usage(TokenUsage(100,10,110),sequence=seq)
        budget.account_usage(TokenUsage(100,10,110),sequence=seq)
        self.assertEqual(budget.total_tokens,110)

    def test_reservation_is_persisted_then_released_against_real_receipt(self):
        f=durable.DurableBudgetTests('runTest');f.setUp()
        try:
            budget=f.registry.admit(f.run.run_id)
            seq,cap,_=budget.reserve_model_call(input_tokens=60)
            self.assertEqual(cap,68)
            self.assertEqual(budget.snapshot.pending_token_reservations,{seq:128})
            f.account(budget,seq,TokenUsage(30,5,35))
            self.assertEqual(budget.snapshot.pending_token_reservations,{})
            self.assertEqual(f.restart().resolve(f.configuration).total_tokens,35)
        finally: f.doCleanups()

    def test_pending_reservation_cannot_be_reduced_in_persistent_ledger(self):
        f=durable.DurableBudgetTests('runTest');f.setUp()
        try:
            budget=f.registry.admit(f.run.run_id)
            seq,_,_=budget.reserve_model_call(input_tokens=60)
            snapshot=budget.snapshot
            tampered=snapshot.model_copy(update={'pending_token_reservations':{seq:1}})
            with self.assertRaises(TelemetryStoreError): f.store.save_budget(tampered,snapshot.revision)
            self.assertEqual(f.store.load_budget(f.run.run_id).pending_token_reservations,{seq:128})
        finally: f.doCleanups()


class EngineInputAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_large_openai_input_is_rejected_before_provider_call_or_counter(self):
        f=runtime.LLMRuntimeTests('runTest');f.setUp()
        provider=runtime.FakeProvider(runtime.text_response());provider.name='openai'
        model=f.model.model_copy(update={'provider':'openai','seed':None})
        budget=f.budget(max_total_tokens=100)
        with self.assertRaises(LLMRuntimeError): await f.run_engine(provider,model=model,budget=budget)
        self.assertEqual(provider.requests,[]);self.assertEqual(budget.model_calls,0)

    async def test_allowance_is_not_reported_as_measured_openai_usage(self):
        f=runtime.LLMRuntimeTests('runTest');f.setUp()
        provider=runtime.FakeProvider(runtime.text_response());provider.name='openai'
        model=f.model.model_copy(update={'provider':'openai','seed':None})
        budget=f.budget(max_total_tokens=100000)
        result=await f.run_engine(provider,model=model,budget=budget)
        self.assertEqual(budget.total_tokens,15);self.assertEqual(result.records[0].usage.total_tokens,15)
