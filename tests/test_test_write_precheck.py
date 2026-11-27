"""Offline proposed-source/engine tests; no saved Agent source or provider is executed."""
import asyncio
import json

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import INSTRUCTIONS
from agents.llm.engine import LLMEngine
from agents.runtime.test_guidance import UNIT_FIXTURE_GUIDANCE
from agents.runtime.test_write_precheck import inspect_test_write
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from mcp_tools.tools.unit_runner import app_fixture_contract
from orchestrator.core.security import redact_data
from orchestrator.domain import AgentRole
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID
from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response
import test_llm_runtime as runtime_fixture


PREFIX = 'from _host_app_test import AppTestCase\nclass Checks(AppTestCase):\n    def test_case(self):\n'
SAFE = PREFIX + '        self.assertTrue(True)\n'
BAD = PREFIX + '        self.check_signup_output(payload, password=[sample])\n'


def inspect(source, path='outputs/qa/tests/test_case.py'):
    return inspect_test_write({'path': path, 'content': source})


@pytest.mark.parametrize(('source', 'column', 'kind'), [
    ('value = "\x00"\n', 10, 'NULL_BYTE'),
    ('def incomplete(\n', 15, 'MISSING_DELIMITER'),
    ('value = "PRIVATE_UNTERMINATED\n', 9, 'UNTERMINATED_STRING'),
    ('def incomplete()\n', 17, 'EXPECTED_COLON'),
    ('def example():\npass\n', 1, 'INDENTATION'),
])
def test_invalid_python_is_detected_with_only_safe_coordinates(source, column, kind):
    issues = inspect(source)
    assert issues == [{'fieldPath': '/content', 'reasonCode': 'GENERATED_TEST_SYNTAX_INVALID',
                      'sourcePath': 'outputs/qa/tests/test_case.py',
                      'lineNumber': 2 if kind == 'INDENTATION' else 1,
                      'columnNumber': column, 'syntaxKind': kind}]
    assert '\x00' not in json.dumps(issues)
    assert 'PRIVATE_UNTERMINATED' not in json.dumps(issues)


def test_runtime_control_character_data_and_browser_json_are_preserved():
    assert inspect(PREFIX + '        data = chr(0)\n') == []
    assert inspect('value = "\\x00"\n') == []
    assert inspect('not python\x00', 'outputs/qa/tests/browser.json') == []


def test_all_proven_helper_errors_are_reported_together():
    source = BAD + '        state = self.signup_hash_parameters(email, password=sample)\n'
    source += '        self.assertEqual(state["algorithm"], "argon2id")\n'
    assert {i['reasonCode'] for i in inspect(source)} == {'TEST_HOST_ARGUMENT_INVALID', 'TEST_HOST_RESULT_KEY_UNKNOWN'}
    assert all(set(i) == {'fieldPath', 'reasonCode', 'sourcePath', 'lineNumber'} for i in inspect(source))
    assert inspect(BAD.replace('[sample]', 'sample')) == []  # unknown variable type remains a runtime question
    assert inspect(source.replace('[sample]', 'sample').replace('["algorithm"]', '["argon2id"]')) == []


@pytest.mark.parametrize('expression', [
    'self.check_signup_output(payload, sample)',
    'self.check_signup_output(payload)',
    'self.signup_email_state([])',
    'self.concurrent_signup_statuses(first_payload=a, second_payload=b)',
])
def test_known_host_signature_misuse_is_rejected_before_execution(expression):
    assert {i['reasonCode'] for i in inspect(PREFIX + '        ' + expression + '\n')} == {'TEST_HOST_ARGUMENT_INVALID'}


def test_unknown_dynamic_arguments_remain_for_runtime_validation():
    assert inspect(PREFIX + '        self.check_signup_output(**options)\n') == []


def test_custom_helpers_and_mutating_unknown_calls_are_not_reinterpreted():
    own = PREFIX.replace('    def test_case', '    def check_signup_output(self, *args, **kw): pass\n    def test_case')
    assert inspect(own + '        self.check_signup_output(payload, password=[sample])\n') == []
    # A user callback can legitimately add the key before it is read. Do not guess through it.
    mutation = PREFIX + '        state = self.signup_hash_parameters(email, password=sample)\n'
    assert inspect(mutation + '        use(add_key(state), state["custom"])\n') == []


def test_public_guidance_and_argument_metadata_survive_normal_secret_redaction():
    values = {'usage': UNIT_FIXTURE_GUIDANCE, 'fixture': app_fixture_contract(),
              'correction': INSTRUCTIONS['GENERATED_TESTS_INVALID']}
    assert redact_data(values) == values
    args = app_fixture_contract()['methods']['check_signup_output']['arguments']
    assert next(a for a in args if a['name'] == 'password')['type'] == 'str'


def settings(actor):
    f = runtime_fixture.LLMRuntimeTests(); f.setUp()
    if isinstance(actor, AgentRole):
        return f, actor, None, f.prepared(actor), f.output, {'answer': 'done'}
    phase = actor
    draft = build_single_draft_contract(phase, scenario=get_scenario(SCN_001_ID),
        selectors={'run_unit_tests': ('qa-unit',)}, source_paths=('main.py',),
        security_test_selectors={'run_unit_tests': ('security-unit',)})
    prompt = PreparedSinglePrompt(phase, draft, build_single_system_prompt(), json.dumps({
        'trusted': {'phase': phase.value}, 'contextId': 'offline', 'history': [], 'taskInput': {}}), 'offline')
    fields = draft.output.schema.to_dict()['properties']
    final = {name: [] for name in fields}
    final.update(kind='INPUT_REQUIRED', questions=['시험 입력 확인이 필요합니다.'])
    return f, SingleAgentIdentity.SINGLE_AGENT, phase, prompt, draft.output, final


@pytest.mark.parametrize('actor', [AgentRole.QA, AgentRole.SECURITY, SinglePhase.QA, SinglePhase.SECURITY])
@pytest.mark.parametrize('source', [BAD, PREFIX + '        data = "\x00"\n'])
def test_whole_batch_is_rejected_before_first_side_effect_then_corrected(actor, source):
    f, role, phase, prompt, output, final = settings(actor)
    tool = f.tool('write_test_file', input_schema=object_schema({k: {'type': 'string'} for k in
        ('workspaceId', 'path', 'content')}), source_argument_fields=('content',))
    read = f.tool('read_project_file')
    source += '        # PRIVATE_CONTENT_SENTINEL\n'
    provider = FakeProvider(tool_response(f.call('read-before-invalid'), f.call('bad-write', name='write_test_file',
        path='outputs/qa/tests/test_case.py', content=source)),
        tool_response(f.call('good-write', name='write_test_file', path='outputs/qa/tests/test_case.py', content=SAFE)),
        text_response(json.dumps(final)))
    executor = FakeToolExecutor()
    budget = f.budget(max_model_calls=4, max_tool_calls=2, max_output_corrections=1)
    result = asyncio.run(LLMEngine(role=role, single_phase=phase, provider=provider,
        tools=(read, tool), tool_executor=executor).run(prompt=prompt, model=f.model, output=output,
        budget=budget, workspace_id=f.workspace_id))
    assert result.data == final
    assert all(record.role is role for record in result.records)
    assert len(executor.calls) == 1 and executor.calls[0][1]['content'] == SAFE
    assert budget.tool_calls == 1 and budget.model_calls == 3
    assert 'PRIVATE_CONTENT_SENTINEL' not in provider.requests[1].input_items_json
    assert 'GENERATED_TESTS_INVALID' in provider.requests[1].input_items_json
    if '\x00' in source:
        feedbacks = [json.loads(row['content'])['hostCorrection']
                     for row in json.loads(provider.requests[1].input_items_json)
                     if row.get('role') == 'user' and 'hostCorrection' in row.get('content', '')]
        syntax = next(row for row in feedbacks[-1]['issues']
                      if row['reasonCode'] == 'GENERATED_TEST_SYNTAX_INVALID')
        assert syntax['syntaxKind'] == 'NULL_BYTE'
        assert syntax['lineNumber'] == 4 and syntax['columnNumber'] > 0


@pytest.mark.parametrize('corrections,model_calls', [(0, 1), (1, 2)])
def test_bad_source_never_writes_when_correction_is_disabled_or_spent(corrections, model_calls):
    f, role, phase, prompt, output, _ = settings(AgentRole.QA)
    tool = f.tool('write_test_file', input_schema=object_schema({k: {'type': 'string'} for k in
        ('workspaceId', 'path', 'content')}), source_argument_fields=('content',))
    provider = FakeProvider(*(tool_response(f.call(str(i), name='write_test_file',
        path='outputs/qa/tests/test_case.py', content=BAD)) for i in range(model_calls)))
    executor = FakeToolExecutor()
    with pytest.raises(LLMRuntimeError) as caught:
        asyncio.run(LLMEngine(role=role, provider=provider, tools=(tool,), tool_executor=executor).run(
            prompt=prompt, model=f.model, output=output, workspace_id=f.workspace_id,
            budget=f.budget(max_output_corrections=corrections, max_model_calls=3)))
    assert caught.value.code is LLMErrorCode.VALIDATION_DRAFT
    assert caught.value.validation.reason_code == 'TEST_SOURCE_INVALID'
    assert len(provider.requests) == model_calls and executor.calls == []


def test_workspace_authorization_still_precedes_test_source_diagnostics():
    f, role, phase, prompt, output, _ = settings(AgentRole.QA)
    tool = f.tool('write_test_file', input_schema=object_schema({k: {'type': 'string'} for k in
        ('workspaceId', 'path', 'content')}), source_argument_fields=('content',))
    provider = FakeProvider(tool_response(f.call('wrong-workspace', name='write_test_file',
        workspaceId='another-workspace', path='outputs/qa/tests/test_case.py', content='bad\x00')))
    executor = FakeToolExecutor()
    with pytest.raises(LLMRuntimeError) as caught:
        asyncio.run(LLMEngine(role=role, provider=provider, tools=(tool,), tool_executor=executor).run(
            prompt=prompt, model=f.model, output=output, workspace_id=f.workspace_id, budget=f.budget()))
    assert caught.value.validation.reason_code == 'WORKSPACE_MISMATCH'
    assert executor.calls == []
