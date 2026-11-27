"""Credential-write regression with synthetic sources/providers only.

The 41/50 line layout mirrors the recorded rejection coordinates; the rejected
live source was not retained and is never reconstructed or executed here.
"""
import json
from types import SimpleNamespace
import unittest

import pytest

from agents.core.identity import SingleAgentIdentity, SinglePhase
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.engine import LLMEngine
from agents.roles.prompts import prepare_role_prompt
from agents.runtime.file_access import constrain_workspace_file_tools
from agents.runtime.test_credential_repair import credential_repair_plan, MAX_SITES
from agents.runtime.test_guidance import (
    CREDENTIAL_TEST_AUTHORING_POLICY, PASSWORD_BOUNDARY_EXAMPLE,
    credential_test_instructions, unit_test_data_contract, unit_test_targets,
)
from agents.single.contracts import build_single_draft_contract
from agents.single.prompts import PreparedSinglePrompt, build_single_system_prompt
from mcp_tools.tools.unit_runner import MAX_TEST_PASSWORD_LENGTH
from orchestrator.core.security import redact_data
from orchestrator.core.source_credentials import SourceCredentialError, validate_source_text
from orchestrator.domain.scenario_registry import get_scenario, SCN_001_ID, RequirementValidator
from orchestrator.domain.states import AgentRole
from fixture_cleanup import set_up_borrowed
import test_llm_runtime as runtime

BAD = '# synthetic fixture\n' * 40 + 'password = "SYNTHETIC_CASE_FIRST"\n' + '# boundary\n' * 8 + 'short_password = "tiny"\n'
SAFE = ('from _host_test_data import new_password\n'
        'password = new_password(length=20)\n'
        'short_password = new_password(length=4)\n')


def target(role='qa'):
    scopes = (SimpleNamespace(kind='QA_TESTS', name='generated', source_directory='tests', pattern='test_*.py'),)
    return unit_test_targets(SimpleNamespace(scopes=scopes), role, scenario_key='SERVICE-WEB')[0]


def plan(source, path='outputs/qa/tests/test_case.py'):
    return credential_repair_plan({'path': path, 'content': source})


def test_both_recorded_line_positions_have_value_free_runtime_repair_hints():
    hints = plan(BAD)
    assert [row['sourceLine'] for row in hints['sites']] == [41, 50]
    assert [row['factory']['arguments']['length'] for row in hints['sites']] == [20, 4]
    assert all(row['instructionCode'] == 'USE_RUNTIME_PASSWORD_FACTORY' for row in hints['sites'])
    assert not hints['truncated']
    assert 'SYNTHETIC_CASE_FIRST' not in json.dumps(hints) and 'tiny' not in json.dumps(hints)
    assert redact_data(hints) == hints
    with pytest.raises(SourceCredentialError):
        validate_source_text(BAD)
    validate_source_text(SAFE)


@pytest.mark.parametrize('value,length,kind', [('""', 0, 'ascii'), ('"1234567"', 7, 'digits'),
    ('"abcdefgh"', 8, 'ascii'), ('"   "', 3, 'spaces'), ('"가나다"', 3, 'unicode')])
def test_recipes_preserve_length_and_character_family_not_values(value, length, kind):
    hints = plan('payload = {"password": ' + value + '}\n')
    assert hints['sites'][0]['siteKind'] == 'DICTIONARY_VALUE'
    assert hints['sites'][0]['factory']['arguments'] == {'length': length, 'kind': kind}


def test_defaults_keywords_and_composed_values_are_described_without_copying_them():
    source = ('def fixture(password="SYNTHETIC_DEFAULT", *, token="SYNTHETIC_TOKEN"):\n'
              '    return None\n'
              'request(password="SYNTHETIC_KEYWORD")\n'
              'password = f"SYNTHETIC_PREFIX{value}"\n'
              'stored_password_hash = "SYNTHETIC_HASH"\n')
    hints = plan(source)
    assert {r['siteKind'] for r in hints['sites']} == {'DEFAULT_ARGUMENT', 'KEYWORD_ARGUMENT', 'ASSIGNMENT'}
    assert {r['credentialKind'] for r in hints['sites']} == {'PASSWORD', 'TOKEN', 'HASH'}
    assert any(r['valueCategory'] == 'COMPOSED_STRING' and 'factory' not in r for r in hints['sites'])
    assert all(marker not in json.dumps(hints) for marker in (
        'SYNTHETIC_DEFAULT', 'SYNTHETIC_TOKEN', 'SYNTHETIC_KEYWORD', 'SYNTHETIC_PREFIX', 'SYNTHETIC_HASH'))
    assert all('factory' not in r for r in hints['sites'] if r['credentialKind'] != 'PASSWORD')


def test_multiple_credential_sites_on_one_line_are_not_reduced_to_one_hint():
    hints = plan('payload = {"password": "SYNTHETIC_DATA", "token": "SYNTHETIC_TOKEN"}\n')
    assert [(r['sourceLine'], r['credentialKind']) for r in hints['sites']] == [(1, 'PASSWORD'), (1, 'TOKEN')]


def test_ordinary_error_labels_are_not_mistaken_for_credential_values():
    source = 'password_error = "Invalid input"\nlabel_password = "label only"\n'
    # label_password is a credential-style name under the existing rule;
    # password_error is not. Hints must use precisely that same distinction.
    assert [r['sourceLine'] for r in plan(source)['sites']] == [2]
    validate_source_text(source.splitlines(True)[0])
    assert not plan('password.error_label = "Invalid input"\n')['sites']
    assert not plan('token.title = "label only"\n')['sites']


def test_ast_diagnostics_are_bounded_and_never_execute_or_rewrite_source():
    hints = plan('raise RuntimeError("never execute")\n' + 'password = "SYNTHETIC_VALUE"\n' * 40)
    assert len(hints['sites']) == MAX_SITES and hints['truncated']
    assert plan('password = (')['truncated']
    assert not plan('{"password": "SYNTHETIC_JSON"}', path='outputs/qa/tests/browser/suite.json')['sites']
    assert not plan('password = response_value\n')['sites']
    assert not plan('password = None\n')['sites']  # wrong-type cases keep their original intent
    over = plan('password = ' + repr('x' * (MAX_TEST_PASSWORD_LENGTH + 1)))
    assert 'factory' not in over['sites'][0]
    assert 'password' not in json.dumps(plan('x' * 1_100_000))


def test_general_targets_use_the_name_advertised_by_the_tool_and_keep_old_alias():
    for role in ('qa', 'security'):
        data = target(role)
        assert data['testData'] == data['testDataFactory'] == unit_test_data_contract()
        assert data['testData']['authoringPolicy'] == CREDENTIAL_TEST_AUTHORING_POLICY
        assert PASSWORD_BOUNDARY_EXAMPLE in data['fixtureGuidance']
        assert 'AppTestCase' not in data.get('appFixture', {})
        data['testData']['parameters']['length']['maximum'] = 1
        assert target(role)['testData']['parameters']['length']['maximum'] == MAX_TEST_PASSWORD_LENGTH


def test_only_python_unit_targets_advertise_the_host_factory_instructions():
    original = {'runtimeInstructions': 'original', 'testTargets': [target()]}
    instructions = credential_test_instructions(original)
    assert original['runtimeInstructions'] == 'original'
    assert PASSWORD_BOUNDARY_EXAMPLE in instructions and CREDENTIAL_TEST_AUTHORING_POLICY in instructions
    browser = {'runtimeInstructions': 'browser rules', 'testTargets': [{'toolName': 'run_browser_tests', 'format': 'browser-suite-v1'}]}
    assert credential_test_instructions(browser) == 'browser rules'


class CredentialRewriteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime.LLMRuntimeTests('runTest')
        set_up_borrowed(self, self.f)
        self.tool = self.f.tool('write_test_file', source_argument_fields=('content',),
            input_schema=runtime.object_schema({'workspaceId': {'type': 'string'},
                'path': {'type': 'string'}, 'content': {'type': 'string'}}))

    def write(self, identity, content):
        return runtime.tool_response(self.f.call(identity, name='write_test_file',
            path='outputs/qa/tests/test_case.py', content=content))

    async def run_engine(self, role, provider, executor, *, prompt=None, output=None, phase=None):
        return await LLMEngine(role=role, provider=provider, tools=(self.tool,), tool_executor=executor,
                              single_phase=phase).run(
            prompt=prompt or self.f.prepared(role), model=self.f.model, output=output or self.f.output,
            budget=self.f.budget(max_model_calls=6, max_tool_calls=4, max_output_corrections=2),
            workspace_id=self.f.workspace_id)

    def check_feedback(self, provider):
        feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostToolFeedback']
        assert feedback['testData'] == unit_test_data_contract()
        assert [s['sourceLine'] for s in feedback['credentialRepairPlan']['sites']] == [41, 50]
        assert feedback['executed'] is False and feedback['correctionsRemaining'] == 0
        assert 'SYNTHETIC_CASE_FIRST' not in json.dumps(feedback) and 'tiny' not in json.dumps(feedback)

    async def test_multi_qa_and_security_write_only_the_clean_replacement(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            provider = runtime.FakeProvider(self.write('rejected', BAD), self.write('fixed', SAFE), runtime.text_response())
            executor = runtime.FakeToolExecutor()
            await self.run_engine(role, provider, executor)
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(executor.calls[0][1]['content'], SAFE)
            self.check_feedback(provider)

    async def test_a_second_credential_rejection_still_stops_before_any_write(self):
        provider = runtime.FakeProvider(self.write('rejected', BAD), self.write('not-fixed', BAD))
        executor = runtime.FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.run_engine(AgentRole.QA, provider, executor)
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_POLICY)
        self.assertEqual(executor.calls, [])
        self.assertEqual(len(provider.requests), 2)

    async def test_single_qa_uses_the_same_guard_repair_recipe_and_one_write(self):
        scenario = get_scenario(SCN_001_ID)
        contract = build_single_draft_contract(SinglePhase.QA, scenario=scenario,
            selectors={'run_unit_tests': ('generated',)})
        prompt = PreparedSinglePrompt(SinglePhase.QA, contract, build_single_system_prompt(),
            json.dumps({'trusted': {'metadata': self.f.metadata.to_a2a_json()}, 'taskInput': {}}), 'single-context')
        final = {'kind': 'READY', 'questions': [], 'cases': [{'toolName': 'run_unit_tests',
            'selector': 'generated', 'testId': f'test_case.Tests.test_{index}',
            'requirementId': str(requirement), 'title': 'fixture check', 'expectedResult': 'assigned criterion'}
            for index, requirement in enumerate(scenario.requirement_ids_for(RequirementValidator.QA))]}
        provider = runtime.FakeProvider(self.write('rejected', BAD), self.write('fixed', SAFE), runtime.text_response(json.dumps(final)))
        executor = runtime.FakeToolExecutor()
        await self.run_engine(SingleAgentIdentity.SINGLE_AGENT, provider, executor, prompt=prompt,
                              output=contract.output, phase=SinglePhase.QA)
        self.assertEqual(len(executor.calls), 1)
        self.check_feedback(provider)

    def test_tool_schema_and_role_prompts_advertise_the_factory_without_mcp_acl_changes(self):
        original = self.tool.input_schema.to_dict()
        for role in (AgentRole.QA, AgentRole.SECURITY):
            view, = constrain_workspace_file_tools((self.tool,), role)
            description = view.input_schema.to_dict()['properties']['content']['description']
            self.assertIn('testTargets.testData', description)
            self.assertIn('new_password', description)
            self.assertEqual(view.source_argument_fields, ('content',))
            payload = {'runtimeInstructions': 'existing rules', 'testTargets': [target(role.value.lower())]}
            prompt = prepare_role_prompt(role, task_input=payload, metadata=self.f.metadata)
            self.assertIn(PASSWORD_BOUNDARY_EXAMPLE, json.loads(prompt.input_json)['taskInput']['runtimeInstructions'])
        self.assertEqual(self.tool.input_schema.to_dict(), original)
