"""Bounded correction of pre-side-effect model mistakes and opt-in raw capture.

Offline: fake provider/executor only. Checks that the feature is off by default,
that each correctable mistake costs one normal model call on the same budget,
that the same mistake is never corrected twice, that nothing runs before a
corrected batch, that feedback never echoes submitted values, and that provider
rejections, executed Tool failures and credential findings stay fail-closed.
"""
import json
from dataclasses import replace
import os
from pathlib import Path
import stat
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

from agents.llm import raw_capture
from agents.llm.corrections import CorrectionAllowance, checked_rejection, contract_check, source_change_check
from agents.llm.contracts import LLMErrorCode, LLMResponse, LLMRuntimeError, TokenUsage
from agents.llm.engine import LLMEngine
from agents.roles.planner_contract import PlannerContractError
from agents.roles.developer_contract import DeveloperContractError
import test_llm_runtime as runtime_fixture
from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response

SECRET_LIKE = 'private-submitted-value-7731'


class CorrectionRuleTests(unittest.TestCase):
    def test_allowance_caps_total_and_refuses_the_same_mistake_twice(self):
        allowance = CorrectionAllowance(2)
        self.assertTrue(allowance.take(('A',)))
        self.assertFalse(allowance.take(('A',)))
        self.assertTrue(allowance.take(('B',)))
        self.assertFalse(allowance.take(('C',)))
        self.assertEqual(CorrectionAllowance(0).remaining, 0)

    def test_checked_rejection_drops_unknown_codes_values_and_free_text(self):
        self.assertIsNone(checked_rejection({'code': 'ANYTHING_ELSE', 'issues': []}))
        self.assertIsNone(checked_rejection('OUTPUT_CONTRACT_INVALID'))
        normalized = checked_rejection({'code': 'OUTPUT_CONTRACT_INVALID', 'issues': [
            {'fieldPath': '/tasks/0/requirementIds', 'reasonCode': 'REQUIREMENT_MISSING', 'requirementKey': 'REQ-003',
             'reason': 'free text ' + SECRET_LIKE},
            {'fieldPath': 'has spaces ' + SECRET_LIKE, 'reasonCode': 'X_Y'},
            {'fieldPath': '/a', 'reasonCode': 'lower-case'}]})
        self.assertEqual(normalized['issues'], [
            {'fieldPath': '/tasks/0/requirementIds', 'reasonCode': 'REQUIREMENT_MISSING', 'requirementKey': 'REQ-003'}])
        self.assertNotIn(SECRET_LIKE, json.dumps(normalized))

    def test_contract_check_maps_role_errors_to_one_neutral_code(self):
        def planner(_data):
            raise PlannerContractError()
        def developer(_data):
            raise DeveloperContractError()
        def unexpected(_data):
            raise RuntimeError('checker bug')
        self.assertEqual(contract_check(planner)({})['code'], 'OUTPUT_CONTRACT_INVALID')
        self.assertEqual(contract_check(developer)({}), {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': []})
        with self.assertRaises(RuntimeError):
            contract_check(unexpected)({})
        self.assertIsNone(contract_check(lambda data: data)({}))


class SourceChangeRuleTests(unittest.TestCase):
    def test_ready_without_any_source_write_is_rejected_once_and_writes_pass(self):
        check = source_change_check(lambda data: SimpleNamespace(kind=data['kind']))
        ready = {'kind': 'READY'}
        self.assertEqual(check(ready, ('read_project_file',)), {'code': 'READY_WITHOUT_CHANGES', 'issues': []})
        self.assertEqual(check(ready, ()), {'code': 'READY_WITHOUT_CHANGES', 'issues': []})
        self.assertIsNone(check(ready, ('read_project_file', 'write_source_file')))
        self.assertIsNone(check(ready, ('apply_patch',)))
        self.assertEqual(check({'kind': 'INPUT_REQUIRED'}, ())['code'], 'QUESTIONS_NOT_ANSWERED')
        self.assertIsNone(check({'kind': 'REJECTED'}, ()))

    def test_contract_errors_win_over_the_change_rule(self):
        def broken(_data):
            raise DeveloperContractError()
        self.assertEqual(source_change_check(broken)({'kind': 'READY'}, ())['code'], 'OUTPUT_CONTRACT_INVALID')


class EngineCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()

    async def run_engine(self, provider, *, corrections=0, tools=(), executor=None, output=None, check=None,
                         sink=None, max_model_calls=8):
        return await LLMEngine(role=self.f.role, provider=provider, tools=tools, tool_executor=executor).run(
            prompt=self.f.prompt, model=self.f.model, output=output or self.f.output,
            budget=self.f.budget(max_output_corrections=corrections, max_model_calls=max_model_calls),
            workspace_id=self.f.workspace_id, validation_failed_sink=sink, output_check=check)

    def strict_path_tool(self):
        return self.f.tool(input_schema=object_schema({
            'workspaceId': {'type': 'string'}, 'path': {'type': 'string', 'pattern': '^source/'}}))

    async def test_default_is_fail_closed_exactly_as_before(self):
        provider = FakeProvider(tool_response(self.f.call(path='main.py')), text_response())
        executor = FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, tools=(self.strict_path_tool(),), executor=executor)
        self.assertEqual(raised.exception.code, LLMErrorCode.SCHEMA)
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual(executor.calls, [])

    async def test_wrong_tool_path_gets_one_value_free_correction_and_nothing_runs_first(self):
        bad = self.f.call('bad', path='main.py-' + SECRET_LIKE)
        good = self.f.call('good', path='source/main.py')
        provider = FakeProvider(tool_response(bad), tool_response(good), text_response())
        executor = FakeToolExecutor()
        reports = []
        result = await self.run_engine(provider, corrections=2, tools=(self.strict_path_tool(),),
                                       executor=executor, sink=reports.append)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 3)          # bad, corrected, final
        self.assertEqual(len(result.records), 3)              # every regeneration is a counted model call
        self.assertEqual([call[1]['path'] for call in executor.calls], ['source/main.py'])
        feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostCorrection']
        self.assertEqual(feedback['code'], 'TOOL_ARGUMENTS_INVALID')
        self.assertEqual(feedback['issues'], [{'fieldPath': '/path', 'reasonCode': 'PATTERN_INVALID'}])
        self.assertFalse(feedback['executed'])
        self.assertEqual(feedback['correctionsRemaining'], 1)
        self.assertNotIn(SECRET_LIKE, provider.requests[1].input_items_json)
        self.assertNotIn(SECRET_LIKE, provider.requests[2].input_items_json)
        self.assertEqual(len(reports), 1)

    async def test_the_same_mistake_is_never_corrected_twice(self):
        provider = FakeProvider(tool_response(self.f.call('a', path='main.py')),
                                tool_response(self.f.call('b', path='main.py')), text_response())
        executor = FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=3, tools=(self.strict_path_tool(),), executor=executor)
        self.assertEqual(raised.exception.code, LLMErrorCode.SCHEMA)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(executor.calls, [])

    async def test_a_corrected_batch_is_never_partially_executed(self):
        provider = FakeProvider(tool_response(self.f.call('ok', path='source/a.py'), self.f.call('bad', path='a.py')),
                                tool_response(self.f.call('ok2', path='source/a.py')), text_response())
        executor = FakeToolExecutor()
        await self.run_engine(provider, corrections=1, tools=(self.strict_path_tool(),), executor=executor)
        self.assertEqual([call[0].call_id for call in executor.calls], ['ok2'])

    async def test_reused_call_ids_of_a_rejected_batch_stay_rejected(self):
        provider = FakeProvider(tool_response(self.f.call('same', path='a.py')),
                                tool_response(self.f.call('same', path='source/a.py')))
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=2, tools=(self.strict_path_tool(),), executor=FakeToolExecutor())
        self.assertEqual(raised.exception.public_validation()['reasonCode'], 'CALL_ID_REUSED')

    async def test_unoffered_tool_is_correctable_but_credentials_and_call_ids_are_not(self):
        provider = FakeProvider(tool_response(self.f.call('x', name='private-unoffered-tool')),
                                tool_response(self.f.call('y', path='src/a.py')), text_response())
        executor = FakeToolExecutor()
        await self.run_engine(provider, corrections=1, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(len(executor.calls), 1)
        for call, reason in ((self.f.call('p', path='password=' + SECRET_LIKE), 'ARGUMENT_CREDENTIAL_DENIED'),):
            provider = FakeProvider(tool_response(call), text_response())
            with self.assertRaises(LLMRuntimeError) as raised:
                await self.run_engine(provider, corrections=3, tools=(self.f.tool(),), executor=FakeToolExecutor())
            self.assertEqual(raised.exception.public_validation()['reasonCode'], reason)
            self.assertEqual(len(provider.requests), 1)

    async def test_final_json_schema_mistake_is_regenerated_once(self):
        provider = FakeProvider(text_response(json.dumps({'answer': 42, SECRET_LIKE: 1})), text_response())
        result = await self.run_engine(provider, corrections=1)
        self.assertEqual(result.data, {'answer': 'done'})
        feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostCorrection']
        self.assertEqual(feedback['code'], 'MODEL_OUTPUT_INVALID')
        self.assertEqual(feedback['stage'], 'MODEL_OUTPUT')
        self.assertNotIn(SECRET_LIKE, provider.requests[1].input_items_json)

    async def test_output_check_buys_one_regeneration_then_returns_for_the_callers_validator(self):
        seen = []

        def check(data, executed_tools=()):
            seen.append(data)
            return {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': [{'fieldPath': '/answer', 'reasonCode': 'REQUIREMENT_MISSING'}]}

        provider = FakeProvider(text_response('{"answer":"first"}'), text_response('{"answer":"second"}'))
        result = await self.run_engine(provider, corrections=2, check=check)
        # Same rejection signature twice: the second is not corrected; the data is
        # returned so the caller's own validator decides exactly as before.
        self.assertEqual(result.data, {'answer': 'second'})
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(len(seen), 2)

    async def test_output_check_is_ignored_when_corrections_are_off(self):
        provider = FakeProvider(text_response())
        result = await self.run_engine(provider, check=lambda data, executed_tools=(): {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': []})
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 1)

    async def test_provider_rejection_and_executed_tool_failure_are_never_corrected(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER), text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=3)
        self.assertEqual(raised.exception.code, LLMErrorCode.PROVIDER)
        self.assertEqual(len(provider.requests), 1)
        provider = FakeProvider(tool_response(self.f.call('c', path='src/a.py')), text_response())
        executor = FakeToolExecutor(RuntimeError('tool failed'))
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=3, tools=(self.f.tool(),), executor=executor)
        self.assertEqual(raised.exception.code, LLMErrorCode.TOOL_FAILED)
        self.assertEqual(len(provider.requests), 1)

    async def test_question_reminder_uses_its_own_limit_and_only_once(self):
        asked = {'code': 'QUESTIONS_NOT_ANSWERED', 'issues': []}
        # Off by default: the question is returned immediately even if format corrections are on.
        provider = FakeProvider(text_response('{"answer":"question"}'), text_response())
        result = await self.run_engine(provider, corrections=2, check=lambda data, executed_tools=(): asked)
        self.assertEqual((result.data, len(provider.requests)), ({'answer': 'question'}, 1))
        # On: one fixed reminder, then a second question is returned for the caller to pause.
        provider = FakeProvider(text_response('{"answer":"q1"}'), text_response('{"answer":"q2"}'))
        result = await LLMEngine(role=self.f.role, provider=provider).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_question_reminders=1), workspace_id=self.f.workspace_id,
            output_check=lambda data, executed_tools=(): asked)
        self.assertEqual((result.data, len(provider.requests)), ({'answer': 'q2'}, 2))
        reminder = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostCorrection']
        self.assertEqual(reminder['code'], 'QUESTIONS_NOT_ANSWERED')
        self.assertIn('No human will answer', reminder['instruction'])

    async def test_output_check_sees_the_tools_executed_in_this_run(self):
        seen = []
        provider = FakeProvider(tool_response(self.f.call('r', path='src/a.py')), text_response())
        await self.run_engine(provider, corrections=1, tools=(self.f.tool(),), executor=FakeToolExecutor(),
                              check=lambda data, executed_tools=(): seen.append(executed_tools))
        self.assertEqual(seen, [('read_project_file',)])

    async def test_corrections_spend_the_same_model_call_budget(self):
        provider = FakeProvider(text_response('{"answer":1}'), text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=2, max_model_calls=1)
        self.assertEqual(raised.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(len(provider.requests), 1)


CAP = 64


def runaway(*calls, output_tokens=CAP, text='{"answer":"aaaa'):
    """An incomplete response, like live Run 4f7ec809 seq 26 (Security, 8,192 of 8,192 output tokens)."""
    return LLMResponse(status='incomplete', model_id='fake-model',
                       usage=TokenUsage(input_tokens=10, output_tokens=output_tokens, total_tokens=10 + output_tokens),
                       output_text=None if calls else text, tool_calls=tuple(calls))


class OutputLimitCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()

    async def run_engine(self, provider, *, corrections, executor=None, recovery_sink=None):
        return await LLMEngine(role=self.f.role, provider=provider, tools=(self.f.tool(),),
                               tool_executor=executor or FakeToolExecutor()).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_output_tokens=CAP, max_output_corrections=corrections, max_model_calls=8),
            workspace_id=self.f.workspace_id, recovery_sink=recovery_sink)

    async def test_a_runaway_at_the_cap_is_discarded_and_regenerated_once(self):
        partial = self.f.call('partial', path='src/' + SECRET_LIKE)
        provider = FakeProvider(runaway(partial), text_response())
        executor = FakeToolExecutor()
        result = await self.run_engine(provider, corrections=2, executor=executor)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual([record.outcome for record in result.records], ['incomplete', 'completed'])
        self.assertEqual(executor.calls, [])                       # the cut-off Tool call never runs
        sent = provider.requests[1].input_items_json
        self.assertNotIn(SECRET_LIKE, sent)                        # nor reaches the next request
        feedback = json.loads(json.loads(sent)[-1]['content'])['hostCorrection']
        self.assertEqual((feedback['code'], feedback['executed'], feedback['issues'], feedback['correctionsRemaining']),
                         ('OUTPUT_LIMIT_REACHED', False, [], 1))

    async def test_off_by_default_and_a_second_runaway_fails_closed(self):
        for corrections, responses, requests in ((0, (runaway(), text_response()), 1),
                                                 (3, (runaway(), runaway(), text_response()), 2)):
            provider = FakeProvider(*responses)
            with self.subTest(corrections=corrections), self.assertRaises(LLMRuntimeError) as raised:
                await self.run_engine(provider, corrections=corrections)
            self.assertEqual(raised.exception.code, LLMErrorCode.INCOMPLETE)
            self.assertEqual(len(provider.requests), requests)

    async def test_an_incomplete_stop_below_the_cap_is_never_corrected(self):
        provider = FakeProvider(runaway(output_tokens=CAP - 1), text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, corrections=3)
        self.assertEqual(raised.exception.code, LLMErrorCode.INCOMPLETE)
        self.assertEqual(len(provider.requests), 1)

    async def test_provider_reason_precedes_token_count_and_filtered_output_stays_unexecuted(self):
        provider = FakeProvider(replace(runaway(output_tokens=CAP-1), incomplete_reason='max_output_tokens'), text_response())
        self.assertEqual((await self.run_engine(provider, corrections=1)).data, {'answer':'done'})
        self.assertEqual(len(provider.requests), 2)
        for reason in ('content_filter', 'max_messages', 'steered', 'other'):
            provider = FakeProvider(replace(runaway(), incomplete_reason=reason), text_response())
            routes = []
            with self.assertRaises(LLMRuntimeError):
                await self.run_engine(provider, corrections=2, recovery_sink=routes.append)
            self.assertEqual(len(provider.requests), 1)
            decision, = [r.public() for r in routes if r.kind == 'DECISION']
            self.assertEqual((decision['action'], decision['incompleteReason']), ('STOP', reason))

    async def test_both_kinds_of_incomplete_are_captured_when_capture_is_on(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {raw_capture.ENV: directory}):
            await self.run_engine(FakeProvider(runaway(), text_response()), corrections=1)
            with self.assertRaises(LLMRuntimeError):
                await self.run_engine(FakeProvider(runaway(output_tokens=1)), corrections=1)
            records = [json.loads(path.read_text()) for path in Path(directory).iterdir()]
        self.assertEqual(sorted((record['kind'], record['corrected']) for record in records),
                         [('OUTPUT_LIMIT_REACHED', True), ('RESPONSE_INCOMPLETE', False)])
        self.assertTrue(all(record['outputText'] == '{"answer":"aaaa' for record in records))


class RawCaptureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()

    async def test_capture_is_off_by_default_and_private_when_enabled(self):
        bad = self.f.call('bad', path='main.py')
        tool = self.f.tool(input_schema=object_schema({'workspaceId': {'type': 'string'},
                                                       'path': {'type': 'string', 'pattern': '^source/'}}))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'raw'
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(raw_capture.ENV, None)
                with self.assertRaises(LLMRuntimeError):
                    await LLMEngine(role=self.f.role, provider=FakeProvider(tool_response(bad)), tools=(tool,),
                                    tool_executor=FakeToolExecutor()).run(
                        prompt=self.f.prompt, model=self.f.model, output=self.f.output, budget=self.f.budget(),
                        workspace_id=self.f.workspace_id)
            self.assertFalse(target.exists())
            with mock.patch.dict(os.environ, {raw_capture.ENV: str(target)}):
                with self.assertRaises(LLMRuntimeError):
                    await LLMEngine(role=self.f.role, provider=FakeProvider(tool_response(bad)), tools=(tool,),
                                    tool_executor=FakeToolExecutor()).run(
                        prompt=self.f.prompt, model=self.f.model, output=self.f.output, budget=self.f.budget(),
                        workspace_id=self.f.workspace_id)
            files = list(target.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(files[0].stat().st_mode), 0o600)
            record = json.loads(files[0].read_text())
            self.assertFalse(record['corrected'])
            self.assertEqual(json.loads(record['toolCalls'][0]['arguments'])['path'], 'main.py')
            self.assertEqual(record['report']['stage'], 'TOOL_ARGUMENTS')

    def test_relative_directory_and_write_failures_never_raise(self):
        with mock.patch.dict(os.environ, {raw_capture.ENV: 'relative/dir'}):
            self.assertFalse(raw_capture.capture(role='QA', phase=None, sequence=1, kind='X', corrected=False))
        with mock.patch.dict(os.environ, {raw_capture.ENV: '/dev/null/not-a-dir'}):
            self.assertFalse(raw_capture.capture(role='QA', phase=None, sequence=1, kind='X', corrected=False))

    def test_captured_text_is_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {raw_capture.ENV: directory}):
                self.assertTrue(raw_capture.capture(role='QA', phase=None, sequence=2, kind='MODEL_OUTPUT_INVALID',
                                                    corrected=True, output_text='{"x":"password=hunter2-secret"}'))
            text = next(Path(directory).iterdir()).read_text()
        self.assertNotIn('hunter2-secret', text)


if __name__ == '__main__':
    unittest.main()
