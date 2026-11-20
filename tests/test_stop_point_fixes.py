"""Stop points found by code review (docs/live-api/03-problems.md P5 points 2–6, P2 point 1; Codex review 2026-11-17).

Each test pins one place where a small model slip used to end the step with no
chance to fix it, and checks that the Host now names the problem and allows one
bounded correction, while decisions stay with the existing validators. All
fixtures are synthetic and trusted; generated code never runs on the host.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from uuid import uuid4

import httpx
from pydantic import SecretStr

from fixture_cleanup import set_up_borrowed

from agents.llm import engine as engine_module
from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, TokenUsage, ToolDefinition
from agents.llm.engine import LLMEngine
from agents.llm.openai_responses import OpenAIResponsesProvider
from agents.roles.contract_explain import qa_issues, security_issues
from agents.roles.qa_contract import validate_qa_decision
from agents.roles.security_contract import validate_security_decision
from agents.runtime.file_access import constrain_frozen_read_tools
from agents.runtime.generated_tests_precheck import inspect_generated_tests, test_ids
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.execution_runtime import TrackedMCPError
import test_llm_runtime as runtime_fixture
from test_llm_runtime import FakeProvider, FakeToolExecutor, object_schema, text_response, tool_response
from test_openai_responses import KEY, make_request
from test_security_agent_contract import FINDING_IDS, REQUIREMENT_IDS, SOURCE_PATHS, decision, reference, requirement

test_ids.__test__ = False  # a helper named test_*, not a test


class _Engine(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = runtime_fixture.LLMRuntimeTests('runTest')
        self.f.setUp()
        patcher = mock.patch.object(engine_module, 'PROVIDER_RETRY_DELAY_SECONDS', 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_engine(self, provider, *, tools=(), executor=None, check=None, routes=None, **limits):
        return await LLMEngine(role=self.f.role, provider=provider, tools=tools, tool_executor=executor).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output,
            budget=self.f.budget(max_model_calls=8, **limits), workspace_id=self.f.workspace_id,
            output_check=check, recovery_sink=None if routes is None else routes.append)


# ------------------------------------------------- Codex #1: different fieldless contract errors

class FieldlessContractErrorTests(_Engine):
    async def test_different_unexplained_rejections_are_different_mistakes(self):
        check = lambda data, executed=(): {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': []}
        provider = FakeProvider(*(text_response(json.dumps({'answer': value})) for value in 'abc'))
        await self.run_engine(provider, check=check, max_output_corrections=2)
        self.assertEqual(len(provider.requests), 3)           # a and b each corrected; c returns to the caller

    async def test_the_very_same_output_twice_is_still_one_mistake(self):
        check = lambda data, executed=(): {'code': 'OUTPUT_CONTRACT_INVALID', 'issues': []}
        same = json.dumps({'answer': 'a'})
        provider = FakeProvider(text_response(same), text_response(same), text_response())
        await self.run_engine(provider, check=check, max_output_corrections=2)
        self.assertEqual(len(provider.requests), 2)

    async def test_coded_mistakes_keep_same_mistake_once(self):
        # READY now requires a source write; its terminal path is covered by
        # test_development_progress. Keep this test about coded corrections.
        check = lambda data, executed=(): {'code': 'CASE_EXPECTATION_INVALID', 'issues': []}
        provider = FakeProvider(*(text_response(json.dumps({'answer': value})) for value in 'abc'))
        await self.run_engine(provider, check=check, max_output_corrections=2)
        self.assertEqual(len(provider.requests), 2)


class ContractExplainerTests(unittest.TestCase):
    """Explainers never flag a valid decision, and every flagged mutation is one the validator rejects."""

    def security(self, data):
        try:
            validate_security_decision(data, REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)
            rejected = False
        except ValueError:
            rejected = True
        return rejected, {(i['fieldPath'], i['reasonCode']) for i in
                          security_issues(data, REQUIREMENT_IDS, FINDING_IDS, SOURCE_PATHS)}

    def test_security(self):
        self.assertEqual(self.security(decision()), (False, set()))
        cases = {
            'missing review': (decision(requirementReviews=[requirement(0), requirement(1)]),
                               ('/requirementReviews', 'ASSIGNED_REQUIREMENT_WITHOUT_REVIEW')),
            'unknown path': (decision(requirementReviews=[requirement(0, references=[reference(path='other.py')]),
                                                          requirement(1), requirement(2)]),
                             ('/requirementReviews/0/references/0/path', 'REFERENCE_PATH_NOT_MEASURED')),
            'bad range': (decision(requirementReviews=[requirement(0, references=[reference(startLine=9, endLine=3)]),
                                                       requirement(1), requirement(2)]),
                          ('/requirementReviews/0/references/0/startLine', 'REFERENCE_LINES_INVALID')),
            'outcome without proof': (decision(requirementReviews=[requirement(0, proposedOutcome='FAIL'),
                                                                   requirement(1), requirement(2)]),
                                      ('/requirementReviews/0/references', 'REFERENCES_REQUIRED_FOR_OUTCOME')),
            'questions with READY': (decision(questions=['why?']), ('/questions', 'QUESTIONS_NOT_ALLOWED_WHEN_READY')),
        }
        for name, (data, expected) in cases.items():
            with self.subTest(name):
                rejected, found = self.security(data)
                self.assertTrue(rejected)
                self.assertIn(expected, found)

    def test_qa(self):
        ids = (uuid4(), uuid4())
        selectors = {'run_unit_tests': ('qa-unit',)}

        def case(index, **changes):
            row = {'toolName': 'run_unit_tests', 'selector': 'qa-unit', 'testId': f'test_a.A.test_{index}',
                   'requirementId': str(ids[index]), 'title': '정상 가입', 'expectedResult': '가입된다'}
            return {**row, **changes}

        def judge(data):
            try:
                validate_qa_decision(data, ids, selectors)
                rejected = False
            except ValueError:
                rejected = True
            return rejected, {(i['fieldPath'], i['reasonCode']) for i in qa_issues(data, ids, selectors)}

        valid = {'kind': 'READY', 'questions': [], 'cases': [case(0), case(1)]}
        self.assertEqual(judge(valid), (False, set()))
        for name, data, expected in (
                ('missing case', {**valid, 'cases': [case(0)]}, ('/cases', 'ASSIGNED_REQUIREMENT_WITHOUT_CASE')),
                ('selector', {**valid, 'cases': [case(0, selector='qa-other'), case(1)]},
                 ('/cases/0/selector', 'SELECTOR_NOT_ALLOWED')),
                ('secret-like text', {**valid, 'cases': [case(0, title='password = "hunter2-fixture-secret"'), case(1)]},
                 ('/cases/0/title', 'TEXT_LOOKS_LIKE_SECRET'))):
            with self.subTest(name):
                rejected, found = judge(data)
                self.assertTrue(rejected)
                self.assertIn(expected, found)


# ------------------------------------------------- point 2: Developer credential literal

class DeveloperCredentialTests(_Engine):
    def write_tool(self):
        return self.f.tool('write_source_file', input_schema=object_schema({
            'workspaceId': {'type': 'string'}, 'path': {'type': 'string'}, 'content': {'type': 'string'}}),
            source_argument_fields=('content',))

    async def test_one_rewrite_when_corrections_are_on_and_nothing_in_the_batch_runs(self):
        bad = self.f.call('bad', name='write_source_file', content='password = "private-fixture-value"')
        read = self.f.call('read', path='src/main.py')
        good = self.f.call('good', name='write_source_file', content='import os\nKEY = os.environ.get("KEY")\n')
        provider = FakeProvider(tool_response(read, bad), tool_response(good), text_response())
        executor = FakeToolExecutor()
        await self.run_engine(provider, tools=(self.f.tool(), self.write_tool()), executor=executor,
                              max_output_corrections=1)
        self.assertEqual([call.call_id for call, _args, _ctx in executor.calls], ['good'])
        feedback = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['content'])['hostToolFeedback']
        self.assertEqual((feedback['code'], feedback['toolName'], feedback['executed']),
                         ('SOURCE_CREDENTIAL_DENIED', 'write_source_file', False))
        self.assertNotIn('private-fixture', provider.requests[1].input_items_json)

    async def test_product_default_still_fails_closed(self):
        bad = self.f.call('bad', name='write_source_file', content='password = "private-fixture-value"')
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(FakeProvider(tool_response(bad)), tools=(self.write_tool(),),
                                  executor=FakeToolExecutor())
        self.assertEqual(raised.exception.code, LLMErrorCode.TOOL_POLICY)


# ------------------------------------------------- point 3: settled read rejections

def _read_error(kind='FILE_NOT_FOUND', unknown=False):
    return TrackedMCPError('MCP_EXECUTION_FAILED', error_kind=kind, result_unknown=unknown)


class ReadOnlyToolErrorTests(_Engine):
    async def test_missing_file_is_reported_to_the_model_instead_of_ending_the_step(self):
        provider = FakeProvider(tool_response(self.f.call('missing', path='src/nope.py')),
                                tool_response(self.f.call('real')), text_response())
        executor = FakeToolExecutor(_read_error())
        result = await self.run_engine(provider, tools=(self.f.tool(),), executor=executor, max_output_corrections=1)
        self.assertEqual(result.data, {'answer': 'done'})
        output = json.loads(json.loads(provider.requests[1].input_items_json)[-1]['output'])['toolError']
        self.assertEqual((output['code'], output['changedState']), ('FILE_NOT_FOUND', False))
        self.assertEqual(len(executor.calls), 2)

    async def test_off_uncertain_writes_and_exhausted_allowance_fail_closed(self):
        write = self.f.tool('write_source_file')
        cases = (
            ({}, self.f.tool(), _read_error(), 'read_project_file'),                         # product default
            ({'max_output_corrections': 1}, self.f.tool(), _read_error(unknown=True), 'read_project_file'),
            ({'max_output_corrections': 1}, write, _read_error(), 'write_source_file'),
        )
        for limits, tool, error, name in cases:
            with self.subTest(limits=limits, name=name, unknown=error.result_unknown):
                provider = FakeProvider(tool_response(self.f.call('x', name=name)), text_response())
                with self.assertRaises(LLMRuntimeError) as raised:
                    await self.run_engine(provider, tools=(tool,), executor=FakeToolExecutor(error), **limits)
                self.assertEqual(raised.exception.code, LLMErrorCode.TOOL_FAILED)
        provider = FakeProvider(tool_response(self.f.call('a')), tool_response(self.f.call('b')), text_response())
        with self.assertRaises(LLMRuntimeError):
            await self.run_engine(provider, tools=(self.f.tool(),), executor=FakeToolExecutor(_read_error(), _read_error()),
                                  max_output_corrections=1)

    def test_security_report_reference_is_limited_to_measured_reports(self):
        report = ToolDefinition('read_security_report', 'reports',
                                object_schema({'workspaceId': {'type': 'string'}, 'reportRef': {'type': 'string'}}),
                                object_schema({'answer': {'type': 'string'}}))
        read = runtime_fixture.LLMRuntimeTests('runTest')
        read.setUp()
        constrained = constrain_frozen_read_tools((read.tool(), report), ('main.py',), ['artifact://scan/report.json'])
        schema = next(tool for tool in constrained if tool.name == 'read_security_report').input_schema.to_dict()
        self.assertEqual(schema['properties']['reportRef']['enum'], ['artifact://scan/report.json'])
        untouched = next(tool for tool in constrain_frozen_read_tools((read.tool(), report), ('main.py',))
                         if tool.name == 'read_security_report').input_schema.to_dict()
        self.assertNotIn('enum', untouched['properties']['reportRef'])


# ------------------------------------------------- point 5: Security code references

class SecurityReferenceTests(unittest.TestCase):
    def test_unread_file_and_lines_past_the_end_are_named(self):
        measured = SimpleNamespace(_token='t', source_files={'models.py': b'a\nb\nc\n', 'main.py': b'x\n'})
        services = SimpleNamespace(_reads={'t': {'models.py': object()}})
        data = {'requirementReviews': [{'references': [
            {'path': 'models.py', 'startLine': 1, 'endLine': 3},      # exactly the last line: fine
            {'path': 'models.py', 'startLine': 2, 'endLine': 4},      # past the end
            {'path': 'main.py', 'startLine': 1, 'endLine': 1},        # not read in this step
            {'path': 'elsewhere.py', 'startLine': 1, 'endLine': 1},   # the contract's business
        ]}], 'findingReviews': []}
        issues = SecurityRuntimeServices.reference_issues(services, measured, data)
        self.assertEqual(issues, [
            {'fieldPath': '/requirementReviews/0/references/1/endLine', 'reasonCode': 'REFERENCE_PAST_END_OF_FILE'},
            {'fieldPath': '/requirementReviews/0/references/2/path', 'reasonCode': 'REFERENCE_FILE_NOT_READ'}])


# ------------------------------------------------- points 4·6: generated test files

PLAIN = 'import unittest\nclass Signup(unittest.TestCase):\n    def test_a(self):\n        pass\n    def test_b(self):\n        pass\n    def helper(self):\n        pass\n'
INHERITED = ('import unittest\nclass Base(unittest.TestCase):\n    def test_base(self):\n        pass\n'
             'class Child(Base):\n    def test_child(self):\n        pass\n')
MIXIN = ('import unittest\nfrom unittest import TestCase\nclass Checks:\n    def test_mixin(self):\n        pass\n'
         'class Real(Checks, TestCase):\n    @unittest.skip("x")\n    def test_skipped(self):\n        pass\n'
         '    async def test_async(self):\n        pass\n')
UNSURE = {
    'load_tests': 'import unittest\ndef load_tests(loader, tests, pattern):\n    return tests\n',
    'foreign base': 'from helpers import BaseTests\nclass T(BaseTests):\n    def test_x(self):\n        pass\n',
    'dynamic attr': 'import unittest\nclass T(unittest.TestCase):\n    test_x = lambda self: None\n',
}


def _runner_ids(directory):
    script = ('import sys, unittest\nsuite = unittest.TestLoader().discover(sys.argv[1], pattern="test_*.py", '
              'top_level_dir=sys.argv[1])\n'
              'def walk(s):\n    for t in s:\n        yield from (walk(t) if isinstance(t, unittest.TestSuite) else [t.id()])\n'
              'print("\\n".join(sorted(walk(suite))))\n')
    done = subprocess.run([sys.executable, '-I', '-c', script, str(directory)], capture_output=True, text=True,
                          timeout=60, cwd=directory)
    return set(filter(None, done.stdout.split('\n')))


class GeneratedTestsTests(unittest.TestCase):
    def tree(self, files):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        for name, text in files.items():
            (root / name).write_text(text)
        return root

    def test_static_ids_equal_real_discovery_for_the_plain_layouts(self):
        for name, text in (('plain', PLAIN), ('inherited', INHERITED), ('mixin', MIXIN)):
            with self.subTest(name):
                root = self.tree({'test_signup.py': text})
                self.assertEqual(test_ids(root / 'test_signup.py'), _runner_ids(root))

    def test_unsure_files_are_not_judged(self):
        for name, text in UNSURE.items():
            with self.subTest(name):
                root = self.tree({'test_signup.py': text})
                self.assertIsNone(test_ids(root / 'test_signup.py'))
                self.assertEqual(inspect_generated_tests(root, [('qa-unit', 'test_*.py')], [], 'cases'), [])

    def test_missing_file_and_undeclared_test_are_named(self):
        scopes = [('qa-unit', 'test_*.py')]
        declared = [{'selector': 'qa-unit', 'testId': 'test_signup.Signup.test_a'},
                    {'selector': 'qa-unit', 'testId': 'test_signup.Signup.test_b'}]
        root = self.tree({'test_signup.py': PLAIN})
        self.assertEqual(inspect_generated_tests(root, scopes, declared, 'cases'), [])
        self.assertEqual(inspect_generated_tests(root, scopes, declared[:1], 'cases'),
                         [{'fieldPath': '/cases', 'reasonCode': 'TEST_IN_FILE_NOT_DECLARED'}])
        self.assertEqual(inspect_generated_tests(self.tree({'notes.txt': 'x'}), scopes, declared, 'testCases'),
                         [{'fieldPath': '/testCases', 'reasonCode': 'GENERATED_TEST_FILE_MISSING'}])
        self.assertEqual(inspect_generated_tests(root / 'absent', scopes, declared, 'cases'),
                         [{'fieldPath': '/cases', 'reasonCode': 'GENERATED_TEST_FILE_MISSING'}])
        nested = self.tree({'test_signup.py': PLAIN})
        (nested / 'pkg').mkdir()
        self.assertEqual(inspect_generated_tests(nested, scopes, declared[:1], 'cases'), [])   # package layout: unsure


# ------------------------------------------------- generated test defects (Run 42f131fe, Codex offline review)

APP_TEST = 'import unittest\nfrom fastapi.testclient import TestClient\nfrom main import app\n'
RUN6_SHAPE = APP_TEST + ('class SignupTests(unittest.TestCase):\n    def setUp(self):\n        self.client = TestClient(app)\n'
                         '    def test_signup(self):\n        self.client.post("/api/auth/signup", json={})\n')
STARTED = {
    'with': APP_TEST + 'class T(unittest.TestCase):\n    def test_a(self):\n        with TestClient(app) as client:\n            client.get("/")\n',
    'enter': APP_TEST + ('class T(unittest.TestCase):\n    def setUp(self):\n        self._c = TestClient(app)\n'
                         '        self.client = self._c.__enter__()\n        self.addCleanup(self._c.__exit__, None, None, None)\n'
                         '    def test_a(self):\n        self.client.get("/")\n'),
    'with name': APP_TEST + 'CLIENT = TestClient(app)\nclass T(unittest.TestCase):\n    def test_a(self):\n        with CLIENT:\n            CLIENT.get("/")\n',
    'enterContext': APP_TEST + ('class T(unittest.TestCase):\n    def setUp(self):\n        self.client = self.enterContext(TestClient(app))\n'
                                '    def test_a(self):\n        self.client.get("/")\n'),
    'no client': PLAIN,
}
ASYNC_IN_SYNC = 'import unittest\nclass T(unittest.TestCase):\n    async def test_a(self):\n        raise AssertionError("body ran")\n'
ASYNC_OK = 'import unittest\nclass T(unittest.IsolatedAsyncioTestCase):\n    async def test_a(self):\n        raise AssertionError("body ran")\n'
ASYNC_OK_INHERITED = ('import unittest\nclass Base(unittest.IsolatedAsyncioTestCase):\n    pass\n'
                      'class T(Base):\n    async def test_a(self):\n        pass\n')
DELETED_BASE = ('import unittest\nclass Base(unittest.TestCase):\n    def test_common(self):\n        pass\n'
                'class Child(Base):\n    def test_child(self):\n        pass\ndel Base\n')
REBOUND_BASE = ('import unittest\nclass Base(unittest.TestCase):\n    def test_common(self):\n        pass\n'
                'class Child(Base):\n    pass\nBase = None\n')


def _run_outcome(directory):
    """Real unittest run of trusted synthetic files: (testsRun, wasSuccessful)."""
    script = ('import sys, unittest, warnings\nwarnings.simplefilter("ignore")\n'
              'suite = unittest.TestLoader().discover(sys.argv[1], pattern="test_*.py", top_level_dir=sys.argv[1])\n'
              'r = unittest.TextTestRunner(stream=open("/dev/null", "w")).run(suite)\n'
              'print(r.testsRun, r.wasSuccessful())\n')
    done = subprocess.run([sys.executable, '-I', '-c', script, str(directory)], capture_output=True, text=True,
                          timeout=60, cwd=directory)
    count, ok = done.stdout.split()
    return int(count), ok == 'True'


class GeneratedTestDefectTests(unittest.TestCase):
    SCOPES = [('qa-unit', 'test_*.py')]
    tree = GeneratedTestsTests.tree

    def codes(self, text, declared=None):
        root = self.tree({'test_signup.py': text})
        ids = test_ids(root / 'test_signup.py') or set()
        rows = [{'selector': 'qa-unit', 'testId': value} for value in (ids if declared is None else declared)]
        return [issue['reasonCode'] for issue in inspect_generated_tests(root, self.SCOPES, rows, 'cases')]

    def test_test_client_outside_with_is_named_like_run_42f131fe(self):
        self.assertEqual(self.codes(RUN6_SHAPE), ['TEST_CLIENT_NOT_STARTED'])
        for name, text in STARTED.items():
            with self.subTest(name):
                self.assertEqual(self.codes(text), [])

    def test_async_test_in_plain_testcase_passes_without_running_and_is_named(self):
        root = self.tree({'test_signup.py': ASYNC_IN_SYNC})
        self.assertEqual(_run_outcome(root), (1, True))     # the defect: the failing body never ran
        self.assertEqual(self.codes(ASYNC_IN_SYNC), ['ASYNC_TEST_IN_SYNC_CASE'])
        root = self.tree({'test_signup.py': ASYNC_OK})
        self.assertEqual(_run_outcome(root), (1, False))    # the right class runs the body
        self.assertEqual(self.codes(ASYNC_OK), [])
        self.assertEqual(self.codes(ASYNC_OK_INHERITED), [])
        self.assertIn('ASYNC_TEST_IN_SYNC_CASE', self.codes(MIXIN))

    def test_syntax_error_and_no_test_are_named(self):
        self.assertEqual(self.codes('import unittest\nclass T(unittest.TestCase:\n    pass\n'),
                         ['GENERATED_TEST_SYNTAX_INVALID'])
        self.assertEqual(self.codes('import unittest\nclass T(unittest.TestCase):\n    def helper(self):\n        pass\n'),
                         ['GENERATED_TEST_NONE'])
        self.assertEqual(self.codes('def test_signup():\n    assert True\n'), ['GENERATED_TEST_NONE'])  # pytest style
        self.assertEqual(self.codes('# no test code at all\n'), [])        # left to the Host run, as before
        self.assertEqual(self.codes('def helper():\n    return 1\n'), [])
        self.assertEqual(self.codes(PLAIN), [])
        for text in UNSURE.values():                         # unsure stays unjudged, never "no test"
            self.assertEqual(self.codes(text, declared=()), [])

    def test_deleted_base_matches_real_discovery_and_rebinding_is_unsure(self):
        root = self.tree({'test_signup.py': DELETED_BASE})
        self.assertEqual(test_ids(root / 'test_signup.py'), _runner_ids(root))
        self.assertEqual(test_ids(root / 'test_signup.py'),
                         {'test_signup.Child.test_common', 'test_signup.Child.test_child'})
        self.assertEqual(self.codes(DELETED_BASE), [])
        self.assertIsNone(test_ids(self.tree({'test_signup.py': REBOUND_BASE}) / 'test_signup.py'))


class WorkspacePinTests(unittest.TestCase):
    def test_pin_only_changes_the_workspace_field(self):
        from agents.llm.contracts import JsonSchema, ToolDefinition
        from agents.llm.tool_wire import pin_workspace_argument, strict_tool_schema
        schema = JsonSchema.from_dict({'type': 'object', 'additionalProperties': False, 'required': ['workspaceId', 'path'],
            'properties': {'workspaceId': {'type': 'string', 'format': 'uuid'}, 'path': {'type': 'string'}}})
        tool = ToolDefinition(name='read_project_file', description='d', input_schema=schema,
                              output_schema=JsonSchema.from_dict({'type': 'object'}))
        pinned = pin_workspace_argument(tool, 'a2c1e9f0-0000-4000-8000-000000000001')
        wire = strict_tool_schema(pinned.input_schema).to_dict()['properties']
        self.assertEqual(wire['workspaceId']['enum'], ['a2c1e9f0-0000-4000-8000-000000000001'])
        self.assertEqual(wire['path'], {'type': 'string'})
        self.assertEqual(tool.input_schema.to_dict()['properties']['workspaceId'], {'type': 'string', 'format': 'uuid'})
        self.assertIs(pin_workspace_argument(tool, None), tool)
        no_workspace = ToolDefinition(name='x', description='d', output_schema=tool.output_schema,
            input_schema=JsonSchema.from_dict({'type': 'object', 'properties': {'path': {'type': 'string'}}}))
        self.assertIs(pin_workspace_argument(no_workspace, 'w'), no_workspace)


class GeneratedTestImportTests(unittest.TestCase):
    """2026-11-18: a product-module import that certainly fails at collection is named before READY."""
    tree = GeneratedTestsTests.tree
    SCOPES = [('qa-unit', 'test_*.py')]
    SOURCE = {'main.py': 'from fastapi import FastAPI\nfrom database import get_db\napp = FastAPI()\n',
              'database.py': 'def get_db():\n    yield None\n',
              'wild.py': 'from database import *\n'}

    def codes(self, test_text, extra_tests=None):
        source = self.tree(self.SOURCE)
        tests = self.tree({'test_signup.py': test_text, **(extra_tests or {})})
        ids = test_ids(tests / 'test_signup.py') or set()
        rows = [{'selector': 'qa-unit', 'testId': value} for value in ids]
        return [i['reasonCode'] for i in inspect_generated_tests(tests, self.SCOPES, rows, 'cases', source_root=source)]

    def test_missing_product_name_is_named(self):
        self.assertEqual(self.codes('from models import User\n' + PLAIN), [])  # no models.py in the source: not judged
        self.assertEqual(self.codes('from database import SessionLocal\n' + PLAIN), ['TEST_IMPORT_NAME_MISSING'])

    def test_valid_and_unjudgeable_imports_pass(self):
        for text in ('from main import app\n', 'from main import get_db\n',  # re-exported name is bound
                     'from database import get_db\n', 'from wild import anything\n',  # wildcard: dynamic
                     'from fastapi.testclient import TestClient\n', 'import main\n'):
            with self.subTest(text):
                self.assertEqual(self.codes(text + PLAIN), [])
        self.assertEqual(self.codes('from helpers import thing\n' + PLAIN, {'helpers.py': 'thing = 1\n'}), [])
        self.assertEqual(self.codes('from main import nothing\n' + PLAIN, {'main.py': 'nothing = 1\n'}), [])  # shadowed

    def test_without_source_root_nothing_changes(self):
        tests = self.tree({'test_signup.py': 'from database import SessionLocal\n' + PLAIN})
        rows = [{'selector': 'qa-unit', 'testId': value} for value in test_ids(tests / 'test_signup.py')]
        self.assertEqual(inspect_generated_tests(tests, self.SCOPES, rows, 'cases'), [])


# ------------------------------------------------- P2 point 1 (Codex #5): re-send vs token cap

class ProviderRetryBudgetTests(_Engine):
    async def test_unknown_usage_with_a_token_cap_books_an_upper_bound_and_retries_once(self):
        # 2026-11-18 (#29): an HTTP 5xx without usage no longer poisons the token budget. The budget books
        # request bytes + output cap (never below the real usage) and the same request is sent once more.
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        result = await self.run_engine(provider, max_provider_retries=1, max_total_tokens=100_000)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 2)
        # Codex review 2026-11-18: the result attribute is ``records``; the old conditional on a
        # non-existent ``usage_records`` silently skipped this check.
        failed, answered = result.records
        self.assertIsNone(failed.usage)          # the record itself stays "unknown"
        self.assertIsNotNone(answered.usage)

    async def test_unknown_usage_bound_is_never_reported_as_usage(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        budget = self.f.budget(max_model_calls=8, max_provider_retries=1, max_total_tokens=100_000)
        result = await LLMEngine(role=self.f.role, provider=provider).run(
            prompt=self.f.prompt, model=self.f.model, output=self.f.output, budget=budget,
            workspace_id=self.f.workspace_id)
        self.assertIsNone(result.records[0].usage)
        self.assertIsNone(budget.total_tokens)   # measured total stays unknown, never the estimate
        self.assertEqual(budget.known_total_tokens, result.records[1].usage.total_tokens)

    async def test_unknown_usage_bound_that_exceeds_the_cap_still_refuses(self):
        routes = []
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, routes=routes, max_provider_retries=1, max_total_tokens=5_000)
        self.assertIn(raised.exception.code, (LLMErrorCode.PROVIDER, LLMErrorCode.BUDGET))
        self.assertEqual(len(provider.requests), 1)

    async def test_retry_after_unknown_usage_persists_ledger_and_usage_records(self):
        # Codex review 2026-11-18 (reproduced): with the durable ledger and usage store connected, the
        # estimate booked as "usage" contradicted the unknown usage record, so the store refused it
        # and the retry ended with a misleading format error. Now both stay unknown and match.
        import test_agent_durable_budgets as durable
        from agents.llm.budget import LLMLimits
        from orchestrator.domain import AgentRole
        f = durable.DurableBudgetTests('runTest')
        f.setUp()
        self.addCleanup(f.doCleanups)
        limits = LLMLimits(max_model_calls=5, max_tool_calls=5, max_total_tokens=100_000, max_provider_retries=1)
        budget = f.restart(limits=limits).admit(f.run.run_id)
        stored = []

        def sink(record):
            stored.append(f.store.append_usage(f.binding, record))
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        result = await LLMEngine(role=AgentRole.PLANNER, provider=provider).run(
            prompt=self.f.prepared(AgentRole.PLANNER), model=f.model, output=self.f.output, budget=budget,
            workspace_id=self.f.workspace_id, usage_sink=sink)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual((len(provider.requests), len(stored)), (2, 2))
        snapshot = f.store.load_budget(f.run.run_id)
        self.assertIsNone(snapshot.accounted_usage[1])
        self.assertGreater(snapshot.unknown_usage_bounds[1], 0)
        self.assertFalse(snapshot.usage_complete)          # billing stays honestly incomplete
        self.assertEqual(snapshot.known_total_tokens, result.records[1].usage.total_tokens)
        # A restarted Host keeps the bound and may still call within the cap.
        restored = f.restart(limits=limits).resolve(f.configuration)
        restored.check_model_call()
        # Codex review 2026-11-18 #5: the status view agrees with the budget (not blocked) while the
        # usage itself is still reported unknown.
        summary = f.store.usage_summary(f.run.run_id)
        self.assertFalse(summary['executionBlocked'])
        self.assertFalse(summary['usageComplete'])
        self.assertIsNone(summary['totalTokens'])
        self.assertEqual(summary['unknownUsageBoundSequences'], [1])
        lowered = snapshot.model_copy(update={'unknown_usage_bounds': {1: 1}})
        from agents.platform.telemetry_store import TelemetryStoreError
        with self.assertRaises(TelemetryStoreError):
            f.store.save_budget(lowered, snapshot.revision)

    async def test_without_retries_unknown_usage_stays_unknown(self):
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True), text_response())
        with self.assertRaises(LLMRuntimeError) as raised:
            await self.run_engine(provider, max_provider_retries=0, max_total_tokens=100_000)
        self.assertEqual(raised.exception.code, LLMErrorCode.PROVIDER)
        self.assertEqual(len(provider.requests), 1)

    async def test_a_request_that_was_never_sent_still_retries_under_a_token_cap(self):
        zero = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
        provider = FakeProvider(LLMRuntimeError(LLMErrorCode.PROVIDER, transient=True, usage=zero), text_response())
        result = await self.run_engine(provider, max_provider_retries=1, max_total_tokens=100_000)
        self.assertEqual(result.data, {'answer': 'done'})
        self.assertEqual(len(provider.requests), 2)


class AdapterUsageTests(unittest.IsolatedAsyncioTestCase):
    async def error(self, handler):
        provider = OpenAIResponsesProvider(SecretStr(KEY), transport=httpx.MockTransport(handler))
        try:
            with self.assertRaises(LLMRuntimeError) as caught:
                await provider.complete(make_request())
        finally:
            await provider.aclose()
        return caught.exception

    async def test_only_unsent_requests_report_known_zero_usage(self):
        def refuse(request):
            raise httpx.ConnectError('synthetic', request=request)
        self.assertEqual((await self.error(refuse)).usage, TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0))
        self.assertIsNone((await self.error(lambda request: httpx.Response(429, text='x'))).usage)
        self.assertIsNone((await self.error(lambda request: httpx.Response(503, text='x'))).usage)


class GeneratedTestsPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Real A2A/Host/workspace with a fake model: the QA draft wrote a test method it did not declare.
    The pre-READY check reads the real workspace file and the model gets one correction. The Run then
    reaches the same end as the clean path (this fixture's fake runner ends it at its own terminal state)
    with exactly the two extra QA calls, instead of failing QA after the tests ran."""

    async def run_qa(self, extra):
        from uuid import UUID
        from agents.llm.contracts import ToolCall
        from orchestrator.domain.scenario_registry import RequirementValidator
        import test_owned_agent_pipeline as multi_fixture
        patcher = mock.patch('agents.runtime.developer.start_precheck_for', lambda *args, **kwargs: None)
        patcher.start()  # the fixture seed has no main.py (see test_completion_fixes)
        self.addCleanup(patcher.stop)
        base = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        count = len(base.scenario.requirement_ids_for(RequirementValidator.QA))
        methods = ''.join(f'    def test_req_{index}(self):\n        pass\n' for index in range(1, count + 1))

        def write(call_id, with_extra):
            content = 'import unittest\nclass Signup(unittest.TestCase):\n' + methods + (
                '    def test_extra(self):\n        pass\n' if with_extra else '')
            return lambda request: tool_response(ToolCall(call_id, 'write_test_file', json.dumps({
                'workspaceId': multi_fixture._workspace_id(request), 'path': 'outputs/qa/tests/test_signup.py',
                'content': content})))

        providers = base.providers()
        qa = providers[multi_fixture.AgentRole.QA]
        ready = qa.script[-1]
        qa.script[:] = ([write('qa-bad', True), ready, write('qa-good', False), ready] if extra
                        else [write('qa-good', False), ready])
        async with base.running_platform(providers, model_cap=32, corrections=2) as (client, _):
            response = await client.post('/api/v1/runs', json=base.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = base.repository.get_run(UUID(response.json()['run']['runId']))
            steps = {step.agent_role: step.status.value for step in base.repository.list_steps(run.run_id)}
        return (run.status, getattr(run.verdict, 'value', None)), steps[multi_fixture.AgentRole.QA], qa

    async def test_qa_undeclared_test_method_is_corrected_once(self):
        end, qa_status, clean = await self.run_qa(extra=False)
        self.assertEqual(qa_status, 'SUCCEEDED')
        slipped_end, slipped_status, slipped = await self.run_qa(extra=True)
        self.assertEqual((slipped_end, slipped_status), (end, 'SUCCEEDED'))
        self.assertEqual(len(slipped.requests), len(clean.requests) + 2)
        notes = [json.loads(item['content'])['hostCorrection'] for request in slipped.requests
                 for item in json.loads(request.input_items_json)
                 if isinstance(item, dict) and 'hostCorrection' in str(item.get('content', ''))]
        self.assertEqual(notes[0]['code'], 'GENERATED_TESTS_INVALID')
        self.assertEqual(notes[0]['issues'], [{'fieldPath': '/cases', 'reasonCode': 'TEST_IN_FILE_NOT_DECLARED'}])


class RealExecutorMissingFileTests(unittest.IsolatedAsyncioTestCase):
    """Point 3 through the real TrackedMCPExecutor and MCP file tool (not a fake executor): a Developer
    read of a file that does not exist used to fail the step. With corrections on, the settled
    FILE_NOT_FOUND comes back as a toolError output and the Run reaches the same end as the clean path."""

    async def run_dev(self, missing, corrections):
        from uuid import UUID
        from agents.llm.contracts import ToolCall
        import test_owned_agent_pipeline as multi_fixture
        patcher = mock.patch('agents.runtime.developer.start_precheck_for', lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        base = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        providers = base.providers()
        developer = providers[multi_fixture.AgentRole.DEVELOPER]
        if missing:
            developer.script.insert(0, lambda request: tool_response(ToolCall('read-missing', 'read_project_file',
                json.dumps({'workspaceId': multi_fixture._workspace_id(request), 'path': 'source/routers/nope.py'}))))
        async with base.running_platform(providers, model_cap=32, corrections=corrections) as (client, _):
            response = await client.post('/api/v1/runs', json=base.submission())
            run = base.repository.get_run(UUID(response.json()['run']['runId']))
            steps = {step.agent_role: step.status.value for step in base.repository.list_steps(run.run_id)}
        return (run.status, getattr(run.verdict, 'value', None)), steps, developer

    async def test_settled_missing_file_is_reported_and_the_run_continues(self):
        clean_end, clean_steps, _ = await self.run_dev(missing=False, corrections=2)
        end, steps, developer = await self.run_dev(missing=True, corrections=2)
        self.assertEqual((end, steps), (clean_end, clean_steps))
        outputs = [item for item in json.loads(developer.requests[1].input_items_json)
                   if isinstance(item, dict) and item.get('type') == 'function_call_output']
        error = json.loads(outputs[-1]['output'])['toolError']
        self.assertEqual((error['code'], error['changedState']), ('FILE_NOT_FOUND', False))
        _, failed_steps, _ = await self.run_dev(missing=True, corrections=0)       # product default: unchanged
        self.assertEqual(failed_steps[next(iter(failed_steps)).__class__.DEVELOPER], 'FAILED')


class RealExecutorWriteConflictTests(RealExecutorMissingFileTests):
    """gpt-4.1 diagnostic b8e44504 (2026-11-18): a write with a stale expectedSha256 failed with WRITE_CONFLICT
    and ended the Developer step. Nothing was written, so the settled conflict now returns as a toolError."""

    async def run_conflict(self, corrections):
        from uuid import UUID
        from agents.llm.contracts import ToolCall
        import test_owned_agent_pipeline as multi_fixture
        patcher = mock.patch('agents.runtime.developer.start_precheck_for', lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        base = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        providers = base.providers()
        developer = providers[multi_fixture.AgentRole.DEVELOPER]
        developer.script.insert(0, lambda request: tool_response(ToolCall('write-stale', 'write_source_file',
            json.dumps({'workspaceId': multi_fixture._workspace_id(request), 'path': 'source/signup.py',
                        'content': 'stale\n', 'expectedSha256': '0' * 64}))))
        async with base.running_platform(providers, model_cap=32, corrections=corrections) as (client, _):
            response = await client.post('/api/v1/runs', json=base.submission())
            run = base.repository.get_run(UUID(response.json()['run']['runId']))
            steps = {step.agent_role: step.status.value for step in base.repository.list_steps(run.run_id)}
        return (run.status, getattr(run.verdict, 'value', None)), steps, developer

    async def test_settled_write_conflict_is_reported_and_the_run_continues(self):
        clean_end, clean_steps, _ = await self.run_dev(missing=False, corrections=2)
        end, steps, developer = await self.run_conflict(corrections=2)
        self.assertEqual((end, steps), (clean_end, clean_steps))
        outputs = [item for item in json.loads(developer.requests[1].input_items_json)
                   if isinstance(item, dict) and item.get('type') == 'function_call_output']
        error = json.loads(outputs[-1]['output'])['toolError']
        self.assertEqual((error['code'], error['changedState']), ('WRITE_CONFLICT', False))
        _, failed_steps, _ = await self.run_conflict(corrections=0)       # product default: unchanged
        self.assertIn('FAILED', failed_steps.values())

    async def test_settled_missing_file_is_reported_and_the_run_continues(self):
        pass  # covered by the parent class; not repeated here


# ------------------------------------------------- P3 cause: the middle dot typed as U+0000

class ExpectedResultEnumTests(unittest.TestCase):
    """Live Runs 3754980f and 42f131fe: gpt-4.1-mini wrote U+0000 where REQ-005's criterion has '·'
    (once, then 2,469 times until the output cap). The strict schema now lists the criteria as an enum."""

    def contract(self):
        from agents.roles.qa_contract import build_qa_output_contract
        from agents.roles.expected_results import with_expected_results
        ids = (uuid4(), uuid4())
        base = build_qa_output_contract(ids, {'run_unit_tests': ('qa-unit',)})
        criteria = ('승인한 알고리즘·파라미터 충족, 평문/일반 SHA 계열 저장 없음', '7자 이하는 거부, 8자 이상 정상 처리')
        return ids, base, with_expected_results(base, 'cases', criteria + criteria[:1]), criteria

    def test_only_the_listed_sentences_are_accepted_on_the_wire(self):
        ids, _base, narrowed, criteria = self.contract()
        item = narrowed.schema.to_dict()['properties']['cases']['items']['properties']['expectedResult']
        self.assertEqual(item, {'type': 'string', 'enum': list(criteria)})

        def data(text):
            return {'kind': 'READY', 'questions': [], 'cases': [{'toolName': 'run_unit_tests', 'selector': 'qa-unit',
                    'testId': 'test_a.A.test_x', 'requirementId': str(ids[0]), 'title': 't', 'expectedResult': text}]}
        narrowed.schema.validate(data(criteria[0]))
        with self.assertRaises(LLMRuntimeError):
            narrowed.schema.validate(data(criteria[0].replace('·', '\x00')))

    def test_no_enum_when_unsafe_or_not_applicable(self):
        from agents.roles.expected_results import with_expected_results
        _ids, base, _narrowed, _criteria = self.contract()
        self.assertIs(with_expected_results(base, 'cases', ['say "hi"']), base)      # batch4: quotes break strict enums
        self.assertIs(with_expected_results(base, 'cases', []), base)
        self.assertIs(with_expected_results(base, 'testCases', ['x']), base)


class ExpectedResultEnumPipelineTests(unittest.IsolatedAsyncioTestCase):
    """The enum reaches the model requests of Multi QA, Multi Security and the Single QA phase when
    corrections are on (measurement settings), and the product default keeps its original schema."""

    def enum_of(self, request, field):
        return request.output.schema.to_dict()['properties'][field]['items']['properties']['expectedResult'].get('enum')

    async def multi_requests(self, corrections):
        from uuid import UUID
        import test_owned_agent_pipeline as multi_fixture
        patcher = mock.patch('agents.runtime.developer.start_precheck_for', lambda *args, **kwargs: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        base = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        base.security_tests = True
        providers = base.providers()
        async with base.running_platform(providers, model_cap=32, corrections=corrections) as (client, _):
            response = await client.post('/api/v1/runs', json=base.submission())
            UUID(response.json()['run']['runId'])
        qa = next(request for request in providers[multi_fixture.AgentRole.QA].requests
                  if 'cases' in request.output.schema.to_dict()['properties'])
        security = next(request for request in providers[multi_fixture.AgentRole.SECURITY].requests
                        if 'testCases' in request.output.schema.to_dict()['properties'])
        return base, qa, security

    async def test_multi_qa_and_security_requests_carry_the_assigned_criteria(self):
        from orchestrator.domain.scenario_registry import RequirementValidator
        base, qa, security = await self.multi_requests(corrections=2)

        def criteria(validator):
            return [c for r in base.scenario.requirements if validator in r.validators for c in r.acceptance_criteria]
        self.assertEqual(self.enum_of(qa, 'cases'), criteria(RequirementValidator.QA))
        self.assertEqual(self.enum_of(security, 'testCases'), criteria(RequirementValidator.SECURITY))
        self.assertIn('승인한 알고리즘·파라미터 충족, 평문/일반 SHA 계열 저장 없음', self.enum_of(security, 'testCases'))
        _base, qa, security = await self.multi_requests(corrections=0)                 # product default
        self.assertIsNone(self.enum_of(qa, 'cases'))
        self.assertIsNone(self.enum_of(security, 'testCases'))

    async def single_qa_request(self, corrections):
        from semantic_helpers import SemanticFakeProvider
        import test_single_agent_pipeline as single_fixture
        patcher = mock.patch('agents.single.runtime.start_precheck_for', lambda *args, **kwargs: None)
        patcher.start()  # the fixture seed has no main.py (see test_completion_fixes)
        self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        set_up_borrowed(self, base)
        f = base.f
        providers = f.providers()
        provider = SemanticFakeProvider(*(action for role in single_fixture.AgentRole
                                          for action in providers[role].script))
        async with base.running(cap=32, provider=provider, corrections=corrections) as (client, _):
            await client.post('/api/v1/runs', json=f.submission())
        return f, next(request for request in provider.requests if 'cases' in request.output.schema.to_dict()['properties'])

    async def test_single_qa_phase_request_carries_the_assigned_criteria(self):
        from orchestrator.domain.scenario_registry import RequirementValidator
        f, qa = await self.single_qa_request(corrections=2)
        self.assertEqual(self.enum_of(qa, 'cases'),
                         [c for r in f.scenario.requirements if RequirementValidator.QA in r.validators
                          for c in r.acceptance_criteria])
        _f, qa = await self.single_qa_request(corrections=0)
        self.assertIsNone(self.enum_of(qa, 'cases'))
