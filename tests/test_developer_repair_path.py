"""Codex review 2026-11-18 (Developer repair path) — offline, fake model/Docker only.

#2 after a READY without changes the next response may fix one fragment (edit_source_file) instead of
   rewriting the whole file; any other call is still no progress.
#3 consecutive partial edits: after a Host-applied edit or write the new file version is known,
   so a follow-up edit needs no extra read; any other change in between is still refused.
"""
from dataclasses import replace
from hashlib import sha256
import unittest

from pydantic import SecretStr

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.roles.developer_contract import build_developer_output_contract, validate_developer_decision
import test_completion_paths as completion
import test_development_progress as progress_fixture
import test_llm_runtime as fixture
import test_mcp_files as file_fixture
from test_openai_responses import make_request


class NoChangeReadyAllowsEdit(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.LLMRuntimeTests('runTest')
        self.f.setUp()
        self.changed = False

    def executor(self):
        def apply(call, *_):
            if call.name in ('edit_source_file', 'write_source_file'):
                self.changed = True
            return {'answer': 'done'}
        return fixture.FakeToolExecutor(*([apply] * 8))

    async def run_engine(self, provider, executor):
        self.budget = self.f.budget(max_output_corrections=2, max_model_calls=12)
        self.routes = []
        tools = (self.f.tool('write_source_file'), self.f.tool('edit_source_file'), self.f.tool())
        return await LLMEngine(role=self.f.role, provider=provider, tools=tools, tool_executor=executor).run(
            prompt=self.f.prompt, model=self.f.model, output=build_developer_output_contract(),
            budget=self.budget, workspace_id=self.f.workspace_id, recovery_sink=self.routes.append,
            output_check=development_check(validate_developer_decision), source_progress_check=lambda: self.changed)

    def stop(self):
        stops = [r for r in self.routes if getattr(r, 'kind', None) == 'DECISION' and r.action == 'STOP']
        return stops[-1]

    async def test_the_stop_record_tells_a_repeated_ready_from_an_unchanged_edit(self):
        """Codex review 2026-11-19 #4: the record shows what the Host required and what the response did."""
        read = fixture.tool_response(self.f.call('read'))
        provider = fixture.FakeProvider(read, progress_fixture.READY, progress_fixture.READY)
        with self.assertRaises(LLMRuntimeError):
            await self.run_engine(provider, self.executor())
        observation = self.stop().observation
        self.assertEqual((observation.required_tools, observation.response_kind, observation.executed,
                          observation.refusal),
                         (('write_source_file', 'edit_source_file'), 'FINAL_OUTPUT', False,
                          'FINAL_OUTPUT_INSTEAD_OF_REQUIRED_TOOL'))
        no_change = fixture.tool_response(self.f.call('fix', name='edit_source_file'))
        provider = fixture.FakeProvider(read, progress_fixture.READY, no_change)
        with self.assertRaises(LLMRuntimeError):
            await self.run_engine(provider, fixture.FakeToolExecutor())  # edits nothing
        observation = self.stop().observation
        self.assertEqual((observation.requested_tools, observation.executed, observation.refusal),
                         (('edit_source_file',), True, 'NO_NET_SOURCE_CHANGE'))
        public = self.stop().public()
        self.assertEqual(set(public['observation']), {'requiredTools', 'responseKind', 'requestedTools', 'executed',
                                                      'refusal'})

    async def test_ready_before_any_read_gets_one_read_then_the_change(self):
        """Codex review 2026-11-19 #2 (replay c8x4dxb7): READY with no read → one forced read → forced change."""
        read = fixture.tool_response(self.f.call('read'))
        edit = fixture.tool_response(self.f.call('fix', name='edit_source_file'))
        provider = fixture.FakeProvider(progress_fixture.READY, read, edit, progress_fixture.READY)
        result = await self.run_engine(provider, self.executor())
        self.assertEqual(result.data['kind'], 'READY')
        self.assertEqual([(r.required_tool, r.required_tool_alternatives) for r in provider.requests],
                         [(None, ()), ('read_project_file', ()), ('write_source_file', ('edit_source_file',)),
                          (None, ())])

    async def test_ready_after_a_read_requires_the_change_at_once(self):
        read = fixture.tool_response(self.f.call('read'))
        edit = fixture.tool_response(self.f.call('fix', name='edit_source_file'))
        provider = fixture.FakeProvider(read, progress_fixture.READY, edit, progress_fixture.READY)
        result = await self.run_engine(provider, self.executor())
        self.assertEqual(result.data['kind'], 'READY')
        self.assertEqual([r.required_tool for r in provider.requests],
                         [None, None, 'write_source_file', None])

    async def test_still_no_change_after_the_read_is_no_progress(self):
        read = fixture.tool_response(self.f.call('read'))
        provider = fixture.FakeProvider(progress_fixture.READY, read, progress_fixture.READY)
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, self.executor())
        self.assertEqual(caught.exception.code, LLMErrorCode.NO_PROGRESS)
        self.assertEqual(len(provider.requests), 3)  # READY, the one read, then no change → stop

    async def test_partial_edit_satisfies_the_no_change_correction(self):
        read = fixture.tool_response(self.f.call('read'))
        edit = fixture.tool_response(self.f.call('fix', name='edit_source_file'))
        provider = fixture.FakeProvider(read, progress_fixture.READY, edit, progress_fixture.READY)
        result = await self.run_engine(provider, self.executor())
        self.assertEqual(result.data['kind'], 'READY')
        forced = provider.requests[2]
        self.assertEqual((forced.required_tool, forced.required_tool_alternatives),
                         ('write_source_file', ('edit_source_file',)))
        self.assertEqual({tool.name for tool in forced.tools}, {'write_source_file', 'edit_source_file'})

    async def test_a_read_instead_of_a_change_is_still_no_progress(self):
        # After a read in this step, the forced turn offers only write/edit; another read is no progress.
        read = fixture.tool_response(self.f.call('read'))
        provider = fixture.FakeProvider(read, progress_fixture.READY, read, progress_fixture.READY)
        executor = self.executor()
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(provider, executor)
        # The forced turn offers only write/edit, so the read is refused before execution.
        self.assertIn(caught.exception.code, (LLMErrorCode.NO_PROGRESS, LLMErrorCode.TOOL_POLICY))
        self.assertEqual(len(executor.calls), 1)  # only the first, ordinary read ran

    async def test_provider_sends_required_choice_only_with_offered_alternatives(self):
        provider = OpenAIResponsesProvider(SecretStr('SYNTHETIC_NOT_A_REAL_KEY'))
        try:
            tools = (self.f.tool('write_source_file'), self.f.tool('edit_source_file'))
            request = make_request(tools=tools, required_tool='write_source_file',
                                   required_tool_alternatives=('edit_source_file',))
            body = provider._request_body(request)
            self.assertEqual(body['tool_choice'], 'required')
            self.assertFalse(body['parallel_tool_calls'])
            single = provider._request_body(replace(request, required_tool_alternatives=()))
            self.assertEqual(single['tool_choice'], {'type': 'function', 'name': 'write_source_file'})
            with self.assertRaises(LLMRuntimeError):
                provider._request_body(replace(request, required_tool_alternatives=('unoffered_tool',)))
        finally:
            await provider.aclose()


class ConsecutiveEditTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        f = file_fixture.FileToolTests('runTest')
        f.setUp()
        completion.adopt_cleanups(self, f)
        return f, f.dispatcher()

    async def test_second_edit_with_the_returned_hash_needs_no_reread(self):
        f, dispatcher = self.fixture()
        await dispatcher.call_tool('read_project_file', f.arguments())
        first = await dispatcher.call_tool('edit_source_file', f.arguments(
            oldText="'initial'", newText="'fixed'", expectedSha256=sha256(f.original).hexdigest()))
        self.assertIsNone(first.error_code)
        after_first = f.file.read_bytes()
        second = await dispatcher.call_tool('edit_source_file', f.arguments(
            oldText="'fixed'", newText="'fixed-again'", expectedSha256=sha256(after_first).hexdigest()))
        self.assertIsNone(second.error_code)
        self.assertEqual(f.file.read_bytes(), after_first.replace(b"'fixed'", b"'fixed-again'"))

    async def test_a_change_in_between_is_still_refused(self):
        f, dispatcher = self.fixture()
        await dispatcher.call_tool('read_project_file', f.arguments())
        await dispatcher.call_tool('edit_source_file', f.arguments(
            oldText="'initial'", newText="'fixed'", expectedSha256=sha256(f.original).hexdigest()))
        changed = f.file.read_bytes() + b'# changed outside the edit tool\n'
        f.file.write_bytes(changed)
        result = await dispatcher.call_tool('edit_source_file', f.arguments(
            oldText="'fixed'", newText="'other'", expectedSha256=sha256(changed).hexdigest()))
        self.assertEqual(result.error_code, 'EDIT_STALE_READ')
        self.assertEqual(f.file.read_bytes(), changed)

    async def test_edit_after_own_full_write_needs_no_reread(self):
        f, dispatcher = self.fixture()
        content = "value = 'written'\n"
        written = await dispatcher.call_tool('write_source_file', f.arguments(content=content))
        self.assertIsNone(written.error_code)
        result = await dispatcher.call_tool('edit_source_file', f.arguments(
            oldText="'written'", newText="'edited'", expectedSha256=sha256(content.encode()).hexdigest()))
        self.assertIsNone(result.error_code)
        self.assertEqual(f.file.read_text(), "value = 'edited'\n")


if __name__ == '__main__':
    unittest.main()


class RoleToolGuidance(unittest.TestCase):
    """Codex review 2026-11-18 #1: the system prompt shows only the Tools the Runtime offers the model,
    and the Developer's Build responsibility is the Host's (role contract v4)."""

    def test_developer_prompt_lists_offered_tools_and_host_build(self):
        from agents.roles.prompts import build_system_prompt
        from orchestrator.domain import AgentRole
        prompt = build_system_prompt(AgentRole.DEVELOPER)
        line = next(item for item in prompt.splitlines() if item.startswith('허용 Tool:'))
        for name in ('read_project_file', 'write_source_file', 'edit_source_file'):
            self.assertIn(name, line)
        for name in ('run_build', 'run_unit_tests', 'apply_patch'):
            self.assertNotIn(name, line)
        self.assertNotIn('Build와 필요한 자체 Unit Test를 허용된 MCP Tool로 요청한다', prompt)
        self.assertIn('Build 실행, 결과 기록은 Host', prompt)

    def test_model_tools_are_within_the_mcp_policy(self):
        from agents.roles.contracts import MODEL_TOOL_NAMES
        from mcp_tools.core.policy import ROLE_TOOL_NAMES
        for role, names in MODEL_TOOL_NAMES.items():
            self.assertTrue(set(names) <= set(ROLE_TOOL_NAMES[role]), role)


class SecurityCitesOnlyReadFiles(unittest.TestCase):
    """Codex review 2026-11-18 #5 (#21): the answer may cite only anchors of files read in this step."""

    def setUp(self):
        from uuid import uuid4
        from agents.roles.security_contract import build_security_output_contract
        from agents.roles.security_draft import SecurityTestDraft
        self.files = {'models.py': b'line\n' * 10, 'main.py': b'line\n' * 30}
        self.wire = SecurityTestDraft(self.files, 'a' * 64)
        self.base = self.wire.output(build_security_output_contract(
            (uuid4(),), ('finding-1',), tuple(sorted(self.files)), ('security-unit',)))

    def enum(self, output, field='requirementReviews'):
        references = output.schema.to_dict()['properties'][field]['items']['properties']['references']
        return references['items']['properties']['anchorId']['enum'], references.get('maxItems')

    def test_only_read_files_are_citable_and_the_catalog_stays_complete(self):
        read = self.wire.restrict_to_read(self.base, frozenset({'models.py'}))
        anchors = self.wire.catalog['anchors']
        allowed, limit = self.enum(read)
        self.assertEqual({anchors[a]['path'] for a in allowed}, {'models.py'})
        self.assertIsNone(limit)
        self.assertEqual({a['path'] for a in anchors.values()}, {'models.py', 'main.py'})  # exploration list
        self.assertEqual(self.enum(read, 'findingReviews')[0], allowed)

    def test_nothing_is_citable_before_any_read(self):
        allowed, limit = self.enum(self.wire.restrict_to_read(self.base, frozenset()))
        self.assertEqual((allowed, limit), (['NO_SOURCE_ANCHOR'], 0))
