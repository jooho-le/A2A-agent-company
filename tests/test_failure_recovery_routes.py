"""Fault injection at real engine boundaries; no provider/network/Docker calls."""
import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
import unittest

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.llm.corrections import CorrectionAllowance, signature, source_change_check
from agents.llm.engine import LLMEngine
import test_llm_runtime as fixture


def test_source_review_exhaustion_is_execution_failure_not_json_format():
    from agents.llm.recovery import failure_route
    route = failure_route(LLMRuntimeError(LLMErrorCode.SOURCE_REPAIR), sequence=1, operation_sequence=1)
    assert route.code == 'LLM_SOURCE_REPAIR_EXHAUSTED'
    assert route.category == 'MODEL_EXECUTION' and route.action == 'STOP'


class FailureRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.LLMRuntimeTests('runTest')
        self.f.setUp()
        self.routes = []

    async def run_case(self, provider, *, tools=(), executor=None, check=None, sink=None, cap=8):
        return await LLMEngine(role=self.f.role, provider=provider, tools=tools, tool_executor=executor).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_model_calls=cap, max_output_corrections=2),
            workspace_id=self.f.workspace_id, output_check=check,
            recovery_sink=self.routes.append if sink is None else sink)

    def retries(self):
        return [r for r in self.routes if r.kind == 'CALL' and r.mode == 'REGENERATION']

    async def test_broken_json_is_corrected_without_echoing_or_executing_partial_arguments(self):
        for raw in ('{"answer":', '{"answer":"private-raw", "answer":"duplicate"}', '```json\n{}\n```'):
            provider = fixture.FakeProvider(fixture.text_response(raw), fixture.text_response())
            result = await self.run_case(provider)
            self.assertEqual(result.data, {'answer': 'done'})
            self.assertNotIn('private-raw', provider.requests[1].input_items_json)
        broken = ToolCall('broken', 'read_project_file', '{"path":"private-raw"')
        provider = fixture.FakeProvider(fixture.tool_response(broken), fixture.text_response())
        executor = fixture.FakeToolExecutor()
        await self.run_case(provider, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(executor.calls, [])
        self.assertEqual(self.retries()[-1].code, 'TOOL_ARGUMENTS_INVALID')

    async def test_provider_and_uncertain_execution_stop_without_replay(self):
        for error in (LLMRuntimeError(LLMErrorCode.PROVIDER), asyncio.TimeoutError()):
            self.routes.clear()
            provider = fixture.FakeProvider(error, fixture.text_response())
            with self.assertRaises(LLMRuntimeError):
                await self.run_case(provider)
            self.assertEqual(len(provider.requests), 1)
            self.assertEqual(self.routes[-1].category, 'EXECUTION_UNCERTAIN')
            self.assertEqual(self.retries(), [])
        provider = fixture.FakeProvider(fixture.tool_response(self.f.call()), fixture.text_response())
        executor = fixture.FakeToolExecutor(RuntimeError('uncertain write'))
        with self.assertRaises(LLMRuntimeError):
            await self.run_case(provider, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(len(provider.requests), 1)

    async def test_tool_result_schema_error_is_host_failure_after_one_execution(self):
        provider = fixture.FakeProvider(fixture.tool_response(self.f.call()), fixture.text_response())
        executor = fixture.FakeToolExecutor({'answer': 123})
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_case(provider, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(raised.exception.validation.stage, 'TOOL_RESULT')
        self.assertEqual(self.routes[-1].category, 'HOST_CONFIGURATION')
        self.assertEqual((len(executor.calls), len(provider.requests)), (1, 1))

    async def test_host_checker_bug_is_not_a_model_typo(self):
        def broken(*_):
            raise RuntimeError('private Host bug')
        provider = fixture.FakeProvider(fixture.text_response(), fixture.text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_case(provider, check=broken)
        self.assertEqual(raised.exception.code, LLMErrorCode.CONFIGURATION)
        self.assertEqual(len(provider.requests), 1)

    async def test_unknown_host_checker_result_cannot_be_accepted(self):
        provider = fixture.FakeProvider(fixture.text_response(), fixture.text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_case(provider, check=lambda *_: {'code': 'UNKNOWN_CHECKER_RESULT'})
        self.assertEqual(raised.exception.code, LLMErrorCode.CONFIGURATION)
        self.assertEqual(len(provider.requests), 1)

    async def test_provider_continuation_is_not_a_model_typo(self):
        response = replace(fixture.tool_response(self.f.call()), output_items_json='not JSON')
        provider = fixture.FakeProvider(response, fixture.text_response())
        executor = fixture.FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError):
            await self.run_case(provider, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(provider.requests), 1)

    async def test_budget_stop_after_feedback_is_not_counted_as_a_retry(self):
        provider = fixture.FakeProvider(fixture.text_response('{"answer":'), fixture.text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_case(provider, cap=1)
        self.assertEqual(raised.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(self.retries(), [])
        self.assertEqual(self.routes[-1].action, 'REGENERATE')  # decision only, no subsequent call

    async def test_missing_recovery_accounting_stops_before_another_model_call(self):
        def sink(route):
            if route.kind == 'DECISION':
                raise RuntimeError('storage unavailable')
        provider = fixture.FakeProvider(fixture.text_response('{"answer":'), fixture.text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_case(provider, sink=sink)
        self.assertEqual(raised.exception.code, LLMErrorCode.CONFIGURATION)
        self.assertEqual(len(provider.requests), 1)

    async def test_unchanged_write_does_not_unlock_ready_but_changed_write_does(self):
        # Real final output is irrelevant here; the Host validator says READY.
        check = source_change_check(lambda data: SimpleNamespace(kind='READY'))
        def ready_check(data, progress):
            return check({'kind': 'READY'}, progress)
        tool = self.f.tool('write_source_file', output_schema=fixture.object_schema({'changed': {'type': 'boolean'}}))
        provider = fixture.FakeProvider(
            fixture.tool_response(self.f.call('unchanged', name='write_source_file')),
            fixture.text_response(),
            fixture.tool_response(self.f.call('changed', name='write_source_file')),
            fixture.text_response())
        executor = fixture.FakeToolExecutor({'changed': False}, {'changed': True})
        await self.run_case(provider, tools=(tool,), executor=executor, check=ready_check)
        self.assertEqual(len(executor.calls), 2)
        self.assertEqual([r.code for r in self.retries()], ['READY_WITHOUT_CHANGES'])
        self.assertEqual([r.mode for r in self.routes if r.kind == 'CALL'],
                         ['INITIAL', 'CONTINUATION', 'REGENERATION', 'CONTINUATION'])

    def test_reordered_duplicate_issues_cannot_refill_correction_allowance(self):
        issues = [{'fieldPath': '/answer', 'reasonCode': 'TYPE_INVALID'},
                  {'fieldPath': '/kind', 'reasonCode': 'REQUIRED_MISSING'}]
        allowance = CorrectionAllowance(3)
        self.assertTrue(allowance.take(signature('MODEL_OUTPUT_INVALID', 'MODEL_OUTPUT', None, issues)))
        self.assertFalse(allowance.take(signature('MODEL_OUTPUT_INVALID', 'MODEL_OUTPUT', None, issues[::-1] + issues)))
        self.assertEqual(allowance.last_reason, 'REPEATED')
