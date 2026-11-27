"""Offline synthetic fixtures only; no provider calls or generated Source execution."""
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.engine import LLMEngine
from agents.runtime.test_guidance import UNIT_FIXTURE_GUIDANCE, unit_test_data_contract
from mcp_tools.tools import unit_runner
from orchestrator.core.source_credentials import (
    SourceCredentialError, source_credential_locations, validate_source_text,
)
from orchestrator.domain import AgentRole
import test_llm_runtime as fixture


SAFE_SOURCE = (
    'from _host_test_data import new_password\n'
    'password = new_password(length=12)\n'
    'short_password = new_password(length=7)\n'
    'empty_password = new_password(length=0)\n'
)
BAD_SOURCE = (
    'password = "SYNTHETIC_FIRST_BLOCKED"\n'
    '# all findings must be repaired in one rewrite\n'
    'short_password = "SYNTHETIC_SECOND_BLOCKED"\n'
)


class RuntimeDataTests(unittest.TestCase):
    def test_lengths_and_character_families_do_not_decide_acceptance(self):
        for length in (0, 1, 7, 8, 12, 1024, 4096):
            for kind in ('ascii', 'unicode', 'digits', 'spaces'):
                with self.subTest(length=length, kind=kind):
                    value = unit_runner._new_test_password(length, kind)
                    self.assertEqual(len(value), length)
                    if length:
                        if kind == 'ascii': self.assertTrue(value.isascii() and value.isalnum())
                        elif kind == 'unicode': self.assertFalse(value.isascii())
                        elif kind == 'digits': self.assertTrue(value.isdecimal())
                        else: self.assertEqual(set(value), {' '})

    def test_factory_uses_runtime_randomness_and_accepts_no_supplied_value(self):
        with patch.object(unit_runner.secrets, 'choice', side_effect=['a', 'b', 'c', 'd']) as random:
            self.assertEqual(unit_runner._new_test_password(2), 'ab')
            self.assertEqual(unit_runner._new_test_password(2), 'cd')
            self.assertEqual(random.call_count, 4)
        for length, kind in ((True, 'ascii'), (-1, 'ascii'), (4097, 'ascii'), ('7', 'ascii'),
                             (7.5, 'ascii'), (12, 'SYNTHETIC_SUPPLIED_VALUE'), (12, [])):
            with self.subTest(length=length, kind=kind), self.assertRaisesRegex(ValueError, '^TEST_DATA_ARGUMENT_INVALID$'):
                unit_runner._new_test_password(length, kind)
        with self.assertRaises(TypeError):
            unit_runner._new_test_password(value='SYNTHETIC_SUPPLIED_VALUE')

    def test_module_is_temporary_and_restores_previous_binding_after_error(self):
        old = ModuleType(unit_runner.TEST_DATA_MODULE)
        with patch.dict(sys.modules, {unit_runner.TEST_DATA_MODULE: old}):
            with self.assertRaisesRegex(RuntimeError, 'fixture'):
                with unit_runner._test_data_api():
                    self.assertIsNot(sys.modules[unit_runner.TEST_DATA_MODULE], old)
                    raise RuntimeError('fixture')
            self.assertIs(sys.modules[unit_runner.TEST_DATA_MODULE], old)
        with patch.dict(sys.modules):
            sys.modules.pop(unit_runner.TEST_DATA_MODULE, None)
            with unit_runner._test_data_api():
                self.assertIn(unit_runner.TEST_DATA_MODULE, sys.modules)
            self.assertNotIn(unit_runner.TEST_DATA_MODULE, sys.modules)

    def test_standalone_runner_executes_benign_factory_fixture_and_preserves_failure(self):
        # This is handwritten trusted fixture code, never saved live/model output.
        source = '''import unittest
from _host_test_data import new_password
password = new_password(length=12)
class Fixture(unittest.TestCase):
    def test_boundary(self):
        for length in (0, 7, 8, 12, 1024):
            with self.subTest(length=length):
                self.assertEqual(len(new_password(length=length)), length)
        self.assertEqual(len(password), 12)
    def test_real_failure(self):
        self.assertEqual(len(new_password(length=7)), 8)
'''
        validate_source_text(source)
        with TemporaryDirectory() as root:
            root = Path(root)
            tests, product = root / 'tests', root / 'product'
            tests.mkdir(); product.mkdir()
            (tests / 'test_factory_fixture.py').write_text(source)
            # Preloaded module must win over accidental product/test filename collisions.
            for folder in (tests, product):
                (folder / '_host_test_data.py').write_text('raise RuntimeError("shadowed")\n')
            program = ('import json, runpy, sys\n'
                'runner = runpy.run_path(sys.argv[1])\n'
                'report, code = runner["run_tests"](sys.argv[2], sys.argv[3])\n'
                'print(json.dumps(report))\nraise SystemExit(code)\n')
            result = subprocess.run([sys.executable, '-I', '-B', '-c', program,
                str(Path(unit_runner.__file__).resolve()), str(tests), str(product)],
                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        report = json.loads(result.stdout)
        self.assertEqual((report['total'], report['passed'], report['failed']), (2, 1, 1))
        self.assertEqual([row['outcome'] for row in report['tests']], ['PASS', 'FAIL'])

    def test_factory_does_not_exempt_literals_or_modify_guarded_source(self):
        validate_source_text(SAFE_SOURCE)
        validate_source_text(Path(unit_runner.__file__).read_text())
        for source in (BAD_SOURCE, 'value = "sk-proj-' + 'SYNTHETIC' * 5 + '"',
                       'url = "https://user:SYNTHETIC@example.invalid/"',
                       r'api\u005fkey = "SYNTHETIC_ESCAPED_VALUE"'):
            with self.subTest(source_kind=source.split('=')[0]), self.assertRaises(SourceCredentialError):
                validate_source_text(SAFE_SOURCE + source)
        with patch('orchestrator.core.source_credentials.contains_registered_secret', return_value=True):
            with self.assertRaises(SourceCredentialError):
                validate_source_text(SAFE_SOURCE)

    def test_all_original_locations_are_value_free_and_bounded(self):
        result = source_credential_locations(BAD_SOURCE)
        self.assertEqual(result, {'issues': [
            {'sourceRule': 'CREDENTIAL_ASSIGNMENT', 'sourceLine': 1},
            {'sourceRule': 'CREDENTIAL_ASSIGNMENT', 'sourceLine': 3}],
            'truncated': False, 'coverage': 'ORIGINAL_TEXT_PATTERNS'})
        self.assertNotIn('SYNTHETIC', json.dumps(result))
        many = source_credential_locations(BAD_SOURCE * 20)
        self.assertEqual(len(many['issues']), 16)
        self.assertTrue(many['truncated'])
        escaped = r'api\u005fkey = "SYNTHETIC_ESCAPED_VALUE"'
        self.assertEqual(source_credential_locations(escaped)['issues'], [])
        with self.assertRaises(SourceCredentialError): validate_source_text(escaped)
        for value in (None, {}, 'x' * (1024 * 1024 + 1)):
            self.assertTrue(source_credential_locations(value)['truncated'])

    def test_contract_matches_the_actual_runner_api(self):
        from orchestrator.core.security import redact_data
        data = unit_test_data_contract()
        self.assertEqual(redact_data(data), data)
        self.assertEqual(redact_data({'guidance': UNIT_FIXTURE_GUIDANCE})['guidance'], UNIT_FIXTURE_GUIDANCE)
        self.assertEqual(data['module'], unit_runner.TEST_DATA_MODULE)
        self.assertEqual(data['policy'], unit_runner.TEST_DATA_POLICY)
        with unit_runner._test_data_api():
            factory = getattr(sys.modules[data['module']], data['function'])
            self.assertEqual(len(factory()), data['parameters']['length']['default'])

    def test_static_import_check_recognizes_preloaded_host_module_only(self):
        from agents.runtime.generated_tests_precheck import _import_issues
        with TemporaryDirectory() as root:
            root = Path(root); tests = root / 'tests'; product = root / 'source'
            tests.mkdir(); product.mkdir()
            test_file = tests / 'test_fixture.py'
            test_file.write_text(SAFE_SOURCE)
            (product / '_host_test_data.py').write_text('different_name = 1\n')
            self.assertEqual(_import_issues(test_file, product, tests), set())
            test_file.write_text('from _host_test_data import nonexistent_factory\n')
            self.assertEqual(_import_issues(test_file, product, tests), {'TEST_IMPORT_NAME_MISSING'})
            (product / 'main.py').write_text('different_name = 1\n')
            test_file.write_text(SAFE_SOURCE + 'from main import missing_name\n')
            self.assertEqual(_import_issues(test_file, product, tests), {'TEST_IMPORT_NAME_MISSING'})


class RuntimeDataCorrectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fixture.LLMRuntimeTests('runTest'); self.f.setUp()
        self.tool = self.f.tool('write_test_file', source_argument_fields=('content',),
            input_schema=fixture.object_schema({'workspaceId': {'type': 'string'},
                'path': {'type': 'string'}, 'content': {'type': 'string'}}))

    def write(self, call_id, source):
        return fixture.tool_response(self.f.call(call_id, name='write_test_file',
            path='outputs/qa/tests/test_signup.py', content=source))

    async def execute(self, role, provider, executor):
        return await LLMEngine(role=role, provider=provider, tools=(self.tool,), tool_executor=executor).run(
            prompt=self.f.prepared(role), model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_model_calls=8, max_output_corrections=2), workspace_id=self.f.workspace_id)

    async def test_qa_and_security_correct_all_findings_before_any_write(self):
        for role in (AgentRole.QA, AgentRole.SECURITY):
            with self.subTest(role=role):
                executor = fixture.FakeToolExecutor()
                provider = fixture.FakeProvider(self.write('bad', BAD_SOURCE),
                    self.write('good', SAFE_SOURCE), fixture.text_response())
                result = await self.execute(role, provider, executor)
                self.assertEqual(len(result.records), 3)
                self.assertEqual(len(executor.calls), 1)
                self.assertEqual(executor.calls[0][1]['content'], SAFE_SOURCE)
                feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostToolFeedback']
                self.assertEqual([item['sourceLine'] for item in feedback['sourceIssues']], [1, 3])
                self.assertEqual(feedback['sourceLine'], 1)
                self.assertFalse(feedback['executed'])
                self.assertEqual(feedback['correctionsRemaining'], 0)
                self.assertIn('_host_test_data', feedback['instruction'])
                self.assertIn(unit_test_data_contract()['example'], feedback['instruction'])
                self.assertIn(unit_test_data_contract()['example'], UNIT_FIXTURE_GUIDANCE)
                self.assertNotIn('token_urlsafe', feedback['instruction'])
                for request in provider.requests:
                    self.assertNotIn('SYNTHETIC_FIRST_BLOCKED', request.input_items_json)
                    self.assertNotIn('SYNTHETIC_SECOND_BLOCKED', request.input_items_json)

    async def test_fixing_only_first_literal_still_stops_without_writing(self):
        provider = fixture.FakeProvider(self.write('bad', BAD_SOURCE),
            self.write('still-bad', SAFE_SOURCE + BAD_SOURCE.splitlines(True)[-1]), fixture.text_response())
        executor = fixture.FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError) as caught:
            await self.execute(AgentRole.QA, provider, executor)
        self.assertEqual(caught.exception.code, LLMErrorCode.TOOL_POLICY)
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(executor.calls, [])

    async def test_helper_import_does_not_allow_credentials_in_a_multi_call_batch(self):
        provider = fixture.FakeProvider(fixture.tool_response(
            self.write('safe', SAFE_SOURCE).tool_calls[0],
            self.write('bad', SAFE_SOURCE + BAD_SOURCE).tool_calls[0]))
        executor = fixture.FakeToolExecutor()
        with self.assertRaises(LLMRuntimeError):
            await self.execute(AgentRole.QA, provider, executor)
        self.assertEqual(executor.calls, [])
