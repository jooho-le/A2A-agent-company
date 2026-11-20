"""No live API/Docker: real source gates plus synthetic provider recovery."""
from dataclasses import replace
import json
import unittest
from unittest import mock

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
from pydantic import SecretStr
import test_llm_runtime as fixture
import test_developer_workspace as workspace_fixture
from test_openai_responses import make_request

READY = fixture.text_response(json.dumps({"rejection": None, 'kind': 'READY', 'summary': 'Candidate ready', 'questions': []}))


class ProgressEngineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.LLMRuntimeTests('runTest')
        self.f.setUp()
        self.changed = False
        self.routes = []

    def write(self, identity='write'):
        return fixture.tool_response(self.f.call(identity, name='write_source_file'))

    async def run_engine(self, provider, *, corrections=2, executor=None, progress=None, cap=12):
        self.budget = self.f.budget(max_output_corrections=corrections, max_model_calls=cap)
        return await LLMEngine(role=self.f.role, provider=provider,
            tools=(self.f.tool('write_source_file'), self.f.tool()), tool_executor=executor or fixture.FakeToolExecutor()).run(
            prompt=self.f.prompt, model=self.f.model, output=build_developer_output_contract(),
            budget=self.budget, workspace_id=self.f.workspace_id, recovery_sink=self.routes.append,
            output_check=development_check(validate_developer_decision),
            source_progress_check=progress or (lambda: self.changed))

    async def test_no_change_ready_requires_actual_write_then_returns_to_normal(self):
        def apply(*_):
            self.changed = True
            return {'answer': 'done'}
        provider = fixture.FakeProvider(READY, self.write(), READY)
        result = await self.run_engine(provider, executor=fixture.FakeToolExecutor(apply))
        self.assertEqual(result.data['kind'], 'READY')
        self.assertEqual([r.required_tool for r in provider.requests], [None, 'write_source_file', None])
        self.assertEqual(self.budget.model_calls, 3)
        self.assertEqual(result.tool_calls, 1)

    async def test_unused_local_gets_specific_feedback_once_and_repeated_edit_stops(self):
        from agents.llm.contracts import SourceEditStatus
        provider = fixture.FakeProvider(READY, self.write(), READY)
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, progress=lambda: SourceEditStatus.UNUSED_TEXT_REWRITE)
        self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
        self.assertEqual(len(provider.requests), 2)
        history = json.loads(provider.requests[-1].input_items_json)
        correction = json.loads(history[-1]['content'])['hostCorrection']
        self.assertEqual(correction['issues'], [{'fieldPath':'/source','reasonCode':'UNUSED_TEXT_REWRITE'}])
        self.assertIn('never returned or logged', correction['instruction'])

    async def test_repeated_ready_or_question_cannot_escape_to_publication(self):
        question = fixture.text_response(json.dumps({"rejection": None, 'kind': 'INPUT_REQUIRED', 'summary': '', 'questions': ['How?']}))
        for response in (READY, question):
            with self.subTest(response=response.output_text):
                provider = fixture.FakeProvider(READY, response, READY)
                with self.assertRaises(LLMRuntimeError) as caught:
                    await self.run_engine(provider)
                self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
                self.assertEqual(len(provider.requests), 2)
                self.assertEqual(self.routes[-1].action, 'STOP')
                self.assertEqual(self.routes[-1].code, 'READY_WITHOUT_CHANGES')

    async def test_ignored_forced_write_does_not_execute_a_read_or_mixed_batch(self):
        for response in (fixture.tool_response(self.f.call()),
                         fixture.tool_response(self.f.call(), self.f.call('write', name='write_source_file'))):
            executor = fixture.FakeToolExecutor()
            with self.assertRaises(LLMRuntimeError) as caught:
                await self.run_engine(fixture.FakeProvider(READY, response), executor=executor)
            self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
            self.assertFalse(executor.calls)

    async def test_unchanged_write_stops_without_another_model_call(self):
        provider = fixture.FakeProvider(READY, self.write(), READY)
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider)
        self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
        self.assertEqual(len(provider.requests), 2)

    async def test_zero_corrections_is_a_closed_stop_not_unchecked_ready(self):
        provider = fixture.FakeProvider(READY)
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, corrections=0)
        self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
        self.assertEqual(self.routes[-1].reason, 'DISABLED')

    async def test_exhausted_model_budget_never_starts_corrective_call(self):
        provider = fixture.FakeProvider(READY, self.write())
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, cap=1)
        self.assertEqual(caught.exception.code, LLMErrorCode.BUDGET)
        self.assertEqual(len(provider.requests), 1)

    async def test_uncertain_tool_failure_keeps_original_error(self):
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(fixture.FakeProvider(READY, self.write()),
                executor=fixture.FakeToolExecutor(LLMRuntimeError(LLMErrorCode.TOOL_FAILED)))
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_FAILED)
        self.assertNotEqual(self.routes[-1].code, 'READY_WITHOUT_CHANGES')

    async def test_broken_host_diff_never_becomes_no_progress_evidence(self):
        def broken():
            raise RuntimeError('Host inspection failed')
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(fixture.FakeProvider(READY), progress=broken)
        self.assertEqual(caught.exception.code, LLMErrorCode.CONFIGURATION)

    async def test_write_then_revert_cannot_pass_using_an_earlier_changed_flag(self):
        def edited(*_):
            self.changed = True
            return {'answer': 'done'}
        def reverted(*_):
            self.changed = False
            return {'answer': 'done'}
        provider = fixture.FakeProvider(self.write('edit'), self.write('revert'), READY, READY)
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, executor=fixture.FakeToolExecutor(edited, reverted))
        self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
        self.assertEqual(provider.requests[-1].required_tool, 'write_source_file')

    async def test_workspace_policy_violation_keeps_precedence_over_forced_tool_mismatch(self):
        wrong = fixture.tool_response(self.f.call(workspaceId='unapproved-workspace'))
        executor = fixture.FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(fixture.FakeProvider(READY, wrong), executor=executor, corrections=1)
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_POLICY)
        self.assertFalse(executor.calls)

    async def test_provider_serializes_required_tool_and_disables_parallel_calls(self):
        provider = OpenAIResponsesProvider(SecretStr('SYNTHETIC_NOT_A_REAL_KEY'))
        try:
            for name in ('write_source_file', 'write_test_file'):
                request = make_request(tools=(self.f.tool(name),), required_tool=name)
                body = provider._request_body(request)
                self.assertEqual(body['tool_choice'], {'type': 'function', 'name': name})
                self.assertFalse(body['parallel_tool_calls'])
                self.assertNotIn('tool_choice', provider._request_body(replace(request, required_tool=None)))
                with self.assertRaises(LLMRuntimeError):
                    provider._request_body(replace(request, required_tool='unoffered_tool'))
        finally:
            await provider.aclose()


class NetSourceProgressTests(unittest.TestCase):
    def setUp(self):
        self.f = workspace_fixture.DeveloperWorkspaceTests('runTest')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.f.checkpoint.prepare()

    def test_runtime_repair_rejects_explanatory_comments_at_ready_and_checkpoint(self):
        path = self.f.source / 'app.py'
        original = path.read_bytes()
        repair = self.f.make_checkpoint(runtime_repair=True)
        repair.prepare()
        path.write_bytes(b'# descriptive comment\n' + original.rstrip(b'\n') + b'  # claimed fix\n')
        self.assertFalse(repair.has_source_changes())
        with self.assertRaises(workspace_fixture.DeveloperCheckpointError) as caught:
            repair.checkpoint()
        self.assertEqual(caught.exception.code, 'DEVELOPER_CHECKPOINT_NO_CHANGES')

    def test_runtime_repair_accepts_code_and_runtime_relevant_strings(self):
        from agents.runtime.developer_workspace import _repair_changes_code
        for before, after in [
            (b'x = False\n', b'x = True\n'),
            (b'x = """first\n# before\n"""\n', b'x = """first\n# after\n"""\n'),
            (b'#!/usr/bin/env python\n', b'#!/usr/bin/python3\n'),
            (b'x = get()\n', b'x = get()  # type: ignore\n'),
        ]:
            with self.subTest(before=before):
                self.assertTrue(_repair_changes_code({'app.py': (before, 0o644)}, {'app.py': (after, 0o644)}))

    def test_real_diff_same_write_and_write_then_revert(self):
        path = self.f.source / 'app.py'
        original = path.read_bytes()
        with mock.patch.object(self.f.checkpoint, '_commit', side_effect=AssertionError('read only')):
            self.assertFalse(self.f.checkpoint.has_source_changes())
            path.write_bytes(original)
            self.assertFalse(self.f.checkpoint.has_source_changes())
            path.write_bytes(b'# changed fixture\n')
            self.assertTrue(self.f.checkpoint.has_source_changes())
            path.write_bytes(original)
            self.assertFalse(self.f.checkpoint.has_source_changes())

    def test_lock_violation_is_not_reported_as_no_progress(self):
        (self.f.source / 'requirements.lock').write_bytes(b'changed lock\n')
        with self.assertRaises(workspace_fixture.DeveloperCheckpointError):
            self.f.checkpoint.has_source_changes()

    def test_parent_snapshot_is_the_previous_candidate_not_the_starter(self):
        (self.f.source / 'app.py').write_bytes(b'# first candidate\n')
        candidate = self.f.checkpoint.checkpoint()
        next_step = self.f.make_checkpoint(parent_commit_hash=candidate.commit_hash)
        next_step.prepare()
        self.assertFalse(next_step.has_source_changes())
        (self.f.source / 'app.py').write_bytes(b'# second candidate\n')
        self.assertTrue(next_step.has_source_changes())
