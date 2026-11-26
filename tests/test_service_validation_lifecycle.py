"""Only hardcoded benign apps run here; never execute real Agent-generated Source."""
import ast
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

from agents.runtime.generated_tests_precheck import inspect_generated_tests, test_ids as discovered_ids
from agents.runtime.service_interface import service_interface_check, service_source_facts
from orchestrator.domain.scenario_registry import service_request_contract
from agents.runtime.test_guidance import unit_test_targets
from mcp_tools.tools import unit_runner
from mcp_tools.tools.build_config import BuildConfiguration
from orchestrator.sandbox.contracts import ExecutionProfile
from orchestrator.sandbox.service_start_contract import service_build_argv, service_interface_failure
from test_service_autonomy import service_design, openapi


TRUSTED_APP = '''
from contextlib import asynccontextmanager
import os
from pathlib import Path
import sqlite3
from fastapi import FastAPI

db_path = os.environ['DATABASE_URL'].removeprefix('sqlite:///')
@asynccontextmanager
async def lifespan(app):
    if START_FAIL:
        raise RuntimeError('trusted fixture startup failure')
    app.state.started = True
    app.state.db_path = db_path
    if CREATE_TABLE:
        with sqlite3.connect(db_path) as db:
            db.execute('CREATE TABLE items (title TEXT)')
    try:
        yield
    finally:
        app.state.started = False

app = FastAPI(lifespan=lifespan)
@app.get('/')
def home():
    with sqlite3.connect(db_path) as db:
        rows = db.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    return {'rows': rows, 'started': app.state.started}

@app.post('/items')
def add_item(payload: dict):
    with sqlite3.connect(db_path) as db:
        db.execute('INSERT INTO items VALUES (?)', ('benign fixture item',))
    return {'created': True}
'''


def run_trusted_app(tmp_path, *, create_table=True, start_fail=False, mode='ordinary'):
    source, tests = tmp_path / 'source', tmp_path / 'tests'
    source.mkdir(); tests.mkdir()
    (source / 'main.py').write_text(
        f'CREATE_TABLE = {create_table!r}\nSTART_FAIL = {start_fail!r}\n' + TRUSTED_APP)
    observed_apps = []

    def discover(*args, **kwargs):
        base = sys.modules[unit_runner.SERVICE_FIXTURE_MODULE].ServiceTestCase
        sample_factory = sys.modules[unit_runner.TEST_DATA_MODULE].new_password

        class TrustedCase(base):
            def test_first(self):
                observed_apps.append(self.app)
                result = self.client.get('/')
                self.assertEqual(result.status_code, 200)
                self.assertEqual(result.json(), {'rows': 0, 'started': True})
                if mode in {'output', 'bad_helper', 'forged_helper_error'}:
                    sample = sample_factory()
                    if mode == 'forged_helper_error':
                        raise ValueError('SUPPLY_CURRENT_SYNTHETIC_SECRET_PRESENT_IN_REQUEST')
                    result = self.check_http_output('POST', '/items', sensitive=sample,
                        json={'password': 'different-request-value' if mode == 'bad_helper' else sample})
                else:
                    result = self.client.post('/items', json={'title': 'fixture'})
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.client.get('/').json()['rows'], 1)

            def test_second(self):
                observed_apps.append(self.app)
                self.assertEqual(self.client.get('/').json()['rows'], 0)

        names = ['test_first'] if mode != 'ordinary' or not create_table or start_fail else ['test_first', 'test_second']
        return unittest.TestSuite(TrustedCase(name) for name in names)

    with patch.object(unittest.TestLoader, 'discover', side_effect=discover):
        report, code = unit_runner.run_tests(tests, source, capture_output_checks=mode != 'ordinary', fixture_usage_repair=True)
    return report, code, observed_apps


def test_generic_fixture_runs_original_lifespan_and_isolates_each_database(tmp_path, monkeypatch):
    monkeypatch.setenv('DATABASE_URL', 'previous-value-restored-without-reading-it')
    report, code, apps = run_trusted_app(tmp_path)
    assert code == 0 and report['passed'] == 2
    assert len({app.state.db_path for app in apps}) == 2
    assert all(not app.state.started and not Path(app.state.db_path).exists() for app in apps)
    assert os.environ['DATABASE_URL'] == 'previous-value-restored-without-reading-it'
    assert all(name not in sys.modules for name in ('main', unit_runner.SERVICE_FIXTURE_MODULE))


def test_fixture_never_creates_product_tables_or_masks_startup_failure(tmp_path, monkeypatch):
    monkeypatch.delenv('DATABASE_URL', raising=False)
    report, code, apps = run_trusted_app(tmp_path, create_table=False)
    assert code == 1 and report['failed'] == 1
    assert apps and all(not app.state.started for app in apps)
    assert 'DATABASE_URL' not in os.environ and 'main' not in sys.modules
    other = tmp_path / 'startup'; other.mkdir()
    report, code, apps = run_trusted_app(other, start_fail=True)
    assert code == 1 and report['failed'] == 1 and not apps
    assert 'DATABASE_URL' not in os.environ and 'main' not in sys.modules


def test_bound_output_helper_uses_the_actual_runtime_string(tmp_path):
    report, code, _ = run_trusted_app(tmp_path, mode='output')
    assert code == 0 and report['passed'] == 1
    checks = report['tests'][0]['outputChecks']
    assert checks == {'REQUEST_LOGS': 'CHECKED', 'OTHER_RESPONSE': 'CHECKED'}


def test_runtime_helper_misuse_is_not_run_and_same_words_cannot_forge_it(tmp_path):
    report, code, _ = run_trusted_app(tmp_path, mode='bad_helper')
    assert code == 0 and report['passed'] == 0 and report['skipped'] == 1
    assert report['tests'][0]['details'] == 'NOT_RUN_FIXTURE_ARGUMENT_INVALID'
    other = tmp_path / 'forged'; other.mkdir()
    report, code, _ = run_trusted_app(other, mode='forged_helper_error')
    assert code == 1 and report['failed'] == 1 and report['skipped'] == 0


@pytest.mark.parametrize('method', ['setUp', 'tearDown', 'setUpClass', '_callSetUp', '_callCleanup', 'run'])
def test_generic_fixture_refuses_lifecycle_overrides(tmp_path, method):
    with unit_runner._service_app_test_api(tmp_path):
        base = sys.modules[unit_runner.SERVICE_FIXTURE_MODULE].ServiceTestCase
        with pytest.raises(ValueError, match='HOST_SERVICE_LIFECYCLE_OVERRIDE'):
            type('BrokenTest', (base,), {method: lambda *_: None})


def precheck(tmp_path, text, *, declared=('test_case.ApiTests.test_request',), service=True):
    path = tmp_path / 'test_case.py'; path.write_text(text)
    rows = [{'selector': 'generated', 'testId': name} for name in declared]
    return inspect_generated_tests(tmp_path, [('generated', 'test_*.py')], rows, 'testCases', service_fixture=service)


PREFIX = 'from _host_service_test import ServiceTestCase\nclass ApiTests(ServiceTestCase):\n'


def test_service_fixture_is_discovered_and_preserves_case_binding(tmp_path):
    text = PREFIX + ' def test_request(self):\n  self.assertEqual(self.client.get("/").status_code, 200)\n'
    assert precheck(tmp_path, text) == []
    assert discovered_ids(tmp_path / 'test_case.py') == {'test_case.ApiTests.test_request'}
    assert precheck(tmp_path, text, declared=[]) == [
        {'fieldPath': '/testCases', 'reasonCode': 'TEST_IN_FILE_NOT_DECLARED'}]


@pytest.mark.parametrize('text', [
    'import unittest\nfrom fastapi.testclient import TestClient\nimport main\nclass ApiTests(unittest.TestCase):\n def setUp(self): self.client=TestClient(main.app)\n def test_request(self): pass\n',
    PREFIX + ' def setUp(self): pass\n def test_request(self): pass\n',
    'import main\n' + PREFIX + ' def test_request(self): pass\n',
])
def test_service_precheck_requires_fixture_without_conflicting_old_advice(tmp_path, text):
    issues = precheck(tmp_path, text)
    assert {'fieldPath': '/testCases', 'reasonCode': 'TEST_SERVICE_FIXTURE_REQUIRED'} in issues
    assert not any(row['reasonCode'] in {'TEST_CLIENT_NOT_STARTED', 'DECLARED_TEST_NOT_IN_FILE'} for row in issues)


@pytest.mark.parametrize('call', [
    'self.check_http_output("POST", "/items", sensitive=sample, json=payload)',
    'self.check_http_output(method="POST", path="/items", sensitive=sample, json=payload)',
    'observe(self.client, "POST", "/items", sensitive=sample, json=payload)',
    'observe(client=self.client, method="POST", path="/items", sensitive=sample, json=payload)',
])
def test_known_valid_output_helper_calls_pass_static_review(tmp_path, call):
    text = 'from _host_test_data import check_http_output as observe\n' + PREFIX
    text += ' def test_request(self):\n  ' + call + '\n'
    assert precheck(tmp_path, text) == []


@pytest.mark.parametrize('call', [
    'self.check_http_output("POST", "/items", sensitive="password")',
    'self.check_http_output("POST", "/items", sensitive=[sample])',
    'self.check_http_output("POST", "/items", password=sample)',
    'observe(self.client, "/items", sensitive=sample)',
])
def test_output_helper_argument_mistakes_are_caught_before_execution(tmp_path, call):
    text = 'from _host_test_data import check_http_output as observe\n' + PREFIX
    text += ' def test_request(self):\n  ' + call + '\n'
    assert {'fieldPath': '/testCases', 'reasonCode': 'TEST_OUTPUT_ARGUMENT_INVALID'} in precheck(tmp_path, text)


def test_pure_unit_tests_keep_their_original_lifecycle_and_all_files_bind(tmp_path):
    text = 'import unittest\nclass ApiTests(unittest.TestCase):\n def setUp(self): self.value=1\n def test_request(self): self.assertEqual(self.value,1)\n'
    (tmp_path / 'test_other.py').write_text(text.replace('ApiTests', 'OtherTests'))
    assert precheck(tmp_path, text, declared=('test_case.ApiTests.test_request', 'test_other.OtherTests.test_request')) == []
    assert precheck(tmp_path, text, declared=('test_case.ApiTests.test_request', 'test_other.OtherTests.test_request'), service=False) == []


def test_both_validation_roles_receive_the_generic_fixture_not_experiment_helpers():
    configuration = SimpleNamespace(scopes=[SimpleNamespace(name='generated', pattern='test_*.py', source_directory='tests', kind='QA_TESTS')])
    for role in ('qa', 'security'):
        target = unit_test_targets(configuration, role, scenario_key='SERVICE-WEB')[0]
        assert target['appFixture']['class'] == 'ServiceTestCase'
        assert not target['appFixture']['createsProductTables']
        assert 'self.User' not in json.dumps(target['appFixture'])
        assert 'sensitive' in target['appFixture']['methods']['check_http_output']['keywordOnly']


def test_api_readiness_requires_agreed_json_and_fixed_probe_stays_under_argv_limit():
    design = service_design()
    assert service_interface_failure(openapi(), design, 200, 'text/html') == 'SERVICE_JSON_REQUIRED'
    assert service_interface_failure(openapi(), design, 200, 'application/problem+json; charset=utf-8') is None
    web = service_design('WEB')
    assert service_interface_failure(openapi(), web, 200, 'text/html') is None
    argv = service_build_argv(design)
    assert all(len(arg.encode()) <= 8192 for arg in argv)
    BuildConfiguration(profile=ExecutionProfile(name='service-start', tool_name='run_build', argv=argv))
    wrapper = ast.parse(argv[-2])
    fixed_program = ast.literal_eval(wrapper.body[0].value.args[0])
    ast.parse(fixed_program)
    assert 'with TestClient' in fixed_program


def test_known_html_return_is_flagged_for_json_api_without_executing_source(tmp_path):
    (tmp_path / 'main.py').write_text('from unavailable import FastAPI, HTMLResponse\napp=FastAPI()\n@app.get("/")\ndef ready():\n return HTMLResponse("<html>wrong API format</html>")\n')
    check = service_interface_check(SimpleNamespace(source_root=tmp_path), service_request_contract('API 만들어'), service_design())
    assert check() == [{'fieldPath': '/source', 'reasonCode': 'SERVICE_JSON_REQUIRED', 'sourcePath': 'main.py', 'lineNumber': 4}]


def test_response_field_facts_are_value_free_and_never_claim_runtime_or_new_requirements():
    facts = service_source_facts({'main.py': b'from missing import app, JSONResponse\n@app.post("/signup")\ndef signup():\n return JSONResponse(status_code=201, content={"message":"never-export-this-value"})\n'})
    assert facts['staticResponseFields'] == [{'path': 'main.py', 'method': 'POST', 'route': '/signup', 'fields': ['message'], 'basis': 'STATIC_SOURCE_ONLY'}]
    assert not facts['isLiveDOM']
    assert 'never-export-this-value' not in json.dumps(facts) and 'email' not in json.dumps(facts)
