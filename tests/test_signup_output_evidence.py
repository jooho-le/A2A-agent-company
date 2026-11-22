"""Exercise the Host runner with handwritten fixtures. No saved product source is executed."""
from contextlib import contextmanager
import json
import logging

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Column, Integer, String, create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base
from sqlalchemy.pool import StaticPool

from agents.runtime.developer_input import _observed_paths
from agents.runtime.security_test_scope import EXPOSURE_CHANNELS, generated_output_coverage, generated_requirement_coverage
from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report, UnitReportError


class Body(BaseModel):
    email: str
    password: str


@contextmanager
def captured_fixture(tmp_path, password, *, unsafe=False, diagnostics=None):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    base = declarative_base()

    class User(base):
        __tablename__ = 'users'
        id = Column(Integer, primary_key=True)
        password_hash = Column(String)

    app = FastAPI()
    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        data = {'message': 'invalid'}
        if unsafe:
            data['received'] = exc.body
        return JSONResponse(data, status_code=422)

    @app.post('/api/auth/signup')
    def signup(body: Body):
        if body.email == 'fail':
            try:
                with engine.connect() as conn:
                    conn.execute(text('SELECT value FROM unavailable_table'))
            except OperationalError:
                if unsafe:
                    logging.getLogger('fixture.output').error('request=%s', body.password)
                return JSONResponse({'message': 'unavailable'}, status_code=503)
        if body.email == 'uncaught':
            with engine.connect() as conn:
                conn.execute(text('SELECT value FROM unavailable_table'))
        return {'id': 1}

    case_output = {}
    with unit_runner._app_test_api(tmp_path, case_output, {password}, diagnostics):
        from _host_app_test import AppTestCase
        tc = AppTestCase()
        with TestClient(app, raise_server_exceptions=False) as tc.client, Session(engine) as tc.db:
            tc.User = User
            yield tc, case_output
    engine.dispose()


def test_generated_requests_supply_real_channels_and_no_secret_bytes(tmp_path):
    secret = unit_runner._new_test_password()
    with captured_fixture(tmp_path, secret) as (case, checks):
        for payload, status in [({'password': secret, 'email': 'ok'}, 200), ({'password': secret}, 422),
                                ({'email': 'fail', 'password': secret}, 503)]:
            assert case.check_signup_output(payload, password=secret).status_code == status
        assert generated_output_coverage([{'outcome': 'PASS', 'outputChecks': checks[case]}]) == dict.fromkeys(EXPOSURE_CHANNELS, 'PASS')
        assert secret not in json.dumps(checks[case])


def test_exposures_and_uncaught_error_are_preserved(tmp_path):
    import unittest
    secret = unit_runner._new_test_password()
    with captured_fixture(tmp_path, secret, unsafe=True) as (case, checks):
        for payload in [{'password': secret}, {'email': 'fail', 'password': secret}]:
            with pytest.raises(AssertionError, match='^REQUEST_OUTPUT_EXPOSURE$'):
                case.check_signup_output(payload, password=secret)
        assert checks[case]['REJECTION_RESPONSE'] == 'EXPOSED'
        assert checks[case]['STORAGE_ERROR_REQUEST_LOGS'] == 'EXPOSED'
    with captured_fixture(tmp_path, secret) as (case, checks):
        with pytest.raises(unittest.SkipTest, match='REQUEST_RESPONSE_UNAVAILABLE'):
            case.check_signup_output({'email': 'uncaught', 'password': secret}, password=secret)
        assert checks[case]['STORAGE_ERROR_RESPONSE'] == 'INCOMPLETE'
        with pytest.raises(ValueError):
            case.check_signup_output({secret: 'not the value'}, password=secret)


def test_old_and_incomplete_evidence_cannot_grant_coverage():
    old = {'outcome': 'PASS', 'testId': 'test_no_password_exposure',
           'observationStatus': 'UNVERIFIED', 'observations': [], 'reason': 'DETAIL_RECORD_MISSING'}
    assert generated_output_coverage([old]) == dict.fromkeys(EXPOSURE_CHANNELS, 'UNVERIFIED')
    rows = [{'outcome': 'PASS', 'outputChecks': {'SUCCESS_RESPONSE': 'CHECKED'}},
            {'outcome': 'PASS', 'outputChecks': {'SUCCESS_RESPONSE': 'INCOMPLETE'}},
            {'outcome': 'FAIL', 'outputChecks': {'REJECTION_RESPONSE': 'EXPOSED'}}]
    assert generated_output_coverage(rows) == generated_output_coverage(rows[::-1])
    assert generated_output_coverage(rows)['SUCCESS_RESPONSE'] == 'UNVERIFIED'
    assert generated_output_coverage(rows)['REJECTION_RESPONSE'] == 'FAIL'
    data = {'format': 'unittest-v1','total': 1,'passed': 1,'failed': 0,'skipped': 0,
            'tests': [{'testId': 't.C.test_output', 'outcome': 'PASS', 'outputChecks': {'REQUEST_LOGS': 'private text'}}]}
    with pytest.raises(UnitReportError):
        parse_unit_report(json.dumps(data), 0)


def test_observed_paths_use_measured_preconditions_and_never_guess_from_422():
    cases = {'malformed': {'handlerEntry': 'NOT_ENTERED', 'failed': {'password_exposed': True}},
             'ambiguous': {'handlerEntry': 'UNKNOWN', 'confirmedConditions': {'response_status': 422},
                           'failed': {'password_exposed': True}},
             'db_error': {'handlerEntry': 'ENTERED', 'confirmedConditions': {'injected': True},
                          'unverified': {'response_status': 'UNHANDLED_APP_EXCEPTION'}},
             'application_logs': {'failed': {'hash_in_crash': True}}}
    assert _observed_paths(cases) == {'RESPONSE_BEFORE_SIGNUP_HANDLER': ['malformed'],
        'UNHANDLED_DATABASE_ERROR_AFTER_ENTRY': ['db_error'], 'EMITTED_APPLICATION_LOG_OR_TRACEBACK': ['application_logs']}
    cases['db_error']['confirmedConditions'] = {}
    assert 'UNHANDLED_DATABASE_ERROR_AFTER_ENTRY' not in _observed_paths(cases)


def test_only_receipt_bound_simple_assertions_are_identified():
    from agents.runtime.security_test_scope import simple_assertion_limits
    source = b'''class Signup(AppTestCase):
    def test_http(self):
        response = self.client.post("/signup", json={})
        self.assertTrue(response.status_code // 100 == 2)
    def test_hash(self):
        user = self.db.query(self.User).first()
        self.assertIsNotNone(user)
        self.assertTrue(user.password_hash.startswith("$argon2id$"))
    def test_other(self):
        self.assertTrue(self.check_database())
    def test_also_hash_strength(self):
        self.assertTrue(self.u.password_hash.startswith("$argon2id$"))
        self.assertEqual(self.params, self.target)
'''
    limits = simple_assertion_limits({'tests/test_signup.py': source, 'other/test_signup.py': source}, 'tests')
    assert limits == {'test_signup.Signup.test_http': 'HTTP_STATUS_ONLY', 'test_signup.Signup.test_hash': 'HASH_PREFIX_ONLY'}

    cases = [{'outcome': 'PASS', 'sourceAssertionScope': limit} for limit in limits.values()]
    assert generated_requirement_coverage('REQ-003', cases[:1]) == (None, ['DB_CONSTRAINT_NOT_OBSERVED'])
    assert generated_requirement_coverage('REQ-005', cases[1:]) == (None, ['HASH_PARAMETERS_NOT_OBSERVED'])
    assert generated_requirement_coverage('REQ-003', [*cases, {'outcome': 'PASS'}]) == (None, ['DB_CONSTRAINT_NOT_OBSERVED'])
    proof = {'outcome': 'PASS', 'signupChecks': {'canonicalEmailUniqueKey': True}}
    assert generated_requirement_coverage('REQ-003', [proof]) == (None, [])


def test_only_related_completed_protected_observations_can_fill_a_security_evidence_gap():
    from agents.runtime.security_test_scope import protected_evidence_covers
    def receipt(values):
        return {'outcome': 'PASS', 'observationStatus': 'COMPLETE', 'evidenceRefs': ['bound'],
                'observations': [{'caseId': case, 'itemId': name, 'status': 'PASS'}
                                 for case, names in values.items() for name in names]}
    unique = receipt({'canonical_variant': ['canonical', 'unique']})
    hashing = {'storage': ['argon2id', 'distinct_salts'], 'first': ['m', 't', 'p', 'correct_password']}
    assert protected_evidence_covers('REQ-003', [unique])
    assert not protected_evidence_covers('REQ-005', [unique, receipt(hashing)])
    assert protected_evidence_covers('REQ-005', [receipt({**hashing, 'second': hashing['first']})])
    channels = ['response_status', 'password_exposed', 'hash_exposed']
    outputs = {'success': channels, 'invalid_email': channels, 'application_logs': channels[1:],
               'db_error': [*channels, 'injected']}
    assert protected_evidence_covers('REQ-006', [receipt(outputs)])
    assert not protected_evidence_covers('REQ-006', [receipt({**outputs, 'db_error': channels})])
    assert not protected_evidence_covers('REQ-003', [{**unique, 'evidenceRefs': []}])
    assert not protected_evidence_covers('REQ-003', [{**unique, 'observationStatus': 'UNVERIFIED'}])


def test_qa_distinguishes_straight_line_http_from_concurrent_or_unknown_test_code():
    from agents.runtime.security_test_scope import simple_assertion_limits
    from agents.runtime.test_fix import TestFixRequired
    cases = '''from _host_app_test import AppTestCase
class Cases(AppTestCase):
    def test_sequential(self):
        self.client.post("/api/auth/signup", json={})
        self.client.post("/api/auth/signup", json={})
        self.assertEqual(self.signup_email_state("local@example.org")["totalRows"], 1)
    def test_concurrent(self):
        statuses = self.concurrent_signup_statuses({}, {})
        self.assertIn(201, statuses)
    def test_custom(self):
        self.client.post("/api/auth/signup", json={})
        self.client.post("/api/auth/signup", json={})
        other.run_cases()
'''
    assert simple_assertion_limits({'tests/test_cases.py': cases}, 'tests', qa=True) == {
        'test_cases.Cases.test_sequential': 'SEQUENTIAL_SIGNUPS_ONLY'}
    assert simple_assertion_limits({'tests/test_cases.py': cases.replace('_host_app_test', 'custom_fixture')},
                                   'tests', qa=True) == {}
    feedback = TestFixRequired(['CONCURRENT_SIGNUPS_NOT_OBSERVED'], ['qa-unit']).task_input({}, {})['testRunFeedback']
    assert 'concurrent_signup_statuses' in feedback['instruction']
    assert 'check_signup_output' not in feedback['instruction']


def test_registered_handler_fact_is_from_submitted_source_not_the_function_name():
    from agents.runtime.repair_progress import changed_handler_decorators
    current = {'main.py': b'@app.exception_handler(Exception)\ndef arbitrary_name(error): pass\n'}
    changed = {'main.py': ['arbitrary_name']}
    assert changed_handler_decorators(current, changed) == [
        {'path': 'main.py', 'function': 'arbitrary_name', 'exceptionName': 'Exception'}]
    assert changed_handler_decorators(current, {}) == []


def test_generated_exposure_locates_only_the_selected_observed_path():
    from agents.runtime.developer_input import compact_fix_input
    from test_repair_feedback import qa_issue, row
    measured = {**row('sec', 'own', role='SECURITY'), 'observationStatus': 'UNVERIFIED',
                'outputChecks': {'REJECTION_RESPONSE': 'EXPOSED', 'STORAGE_ERROR_RESPONSE': 'INCOMPLETE'}}
    issue = {**qa_issue('sec'), 'category': 'SECURITY_REQUIREMENT_FAILURE',
             'description': json.dumps({'code': 'MEASURED_SECURITY_TESTS', 'tests': [{'testId': 'own', 'outcome': 'FAIL'}]})}
    inputs = {'fixRequest': {'issues': [issue], 'validationFeedback': [measured]}}
    files = {'entry.py': b'@app.exception_handler(Exception)\ndef errors(request, exc): pass\n',
             'secrets/config.py': b'@app.exception_handler(Exception)\ndef hidden(): pass\n',
             'bad.py': b'invalid source!'}
    from types import SimpleNamespace
    from agents.runtime.repair_baseline import with_repair_baseline
    checkpoint = SimpleNamespace(repair_baseline=None, repair_source_files=lambda: files)
    brief = with_repair_baseline(inputs, checkpoint)['activeRepairFocus']['brief']
    assert checkpoint.observed_output_repair
    assert brief['readyReportPaths'] == ['RESPONSE_BEFORE_SIGNUP_HANDLER']
    assert brief['sourceInspection']['byObservedPath'] == {'RESPONSE_BEFORE_SIGNUP_HANDLER': [
        {'toolPath': 'source/entry.py', 'line': 1, 'operation': 'EXCEPTION_HANDLER', 'registeredException': 'Exception'}]}
    assert brief['sourceInspection']['firstReadBatch'] == ['source/entry.py']
    many_files = {name + '.py': files['entry.py'] for name in ('a', 'b', 'c', 'd')}
    inspection = compact_fix_input(inputs, source_files=lambda: many_files)['activeRepairFocus']['brief']['sourceInspection']
    assert inspection['firstReadBatch'] == ['source/a.py', 'source/b.py']
    assert len(inspection['byObservedPath']['RESPONSE_BEFORE_SIGNUP_HANDLER']) == 4
    mixed = {'fixRequest': {'issues': [issue, qa_issue()], 'validationFeedback': [measured, row('qa', 'case-A')]}}
    with_repair_baseline(mixed, checkpoint)
    assert not checkpoint.observed_output_repair
    measured['reportArtifactId'] = 'different'
    assert 'sourceInspection' not in compact_fix_input(inputs, source_files=lambda: pytest.fail('excluded'))['activeRepairFocus']['brief']


def test_inspection_uses_restored_snapshot_and_ignores_outgoing_http_calls():
    from types import SimpleNamespace
    from agents.runtime.developer_workspace import GitDeveloperCheckpoint
    from agents.runtime.repair_locations import read_locations
    restored = b"@router.post('/signup')\ndef signup(): pass\nclient.post('/signup')\n"
    checkpoint = SimpleNamespace(_progress_base={'accounts.py': (restored, 0o644)},
                                 _snapshot_files=lambda _: pytest.fail('rejected snapshot'))
    files = GitDeveloperCheckpoint.repair_source_files(checkpoint)
    assert read_locations(['SIGNUP_SUCCESS_RESPONSE'], files) == {'SIGNUP_SUCCESS_RESPONSE': [
        {'toolPath': 'source/accounts.py', 'line': 1, 'operation': 'SIGNUP_ROUTE'}]}


@pytest.mark.parametrize(('ddl', 'direct'), [
    ('CREATE TABLE users (email TEXT COLLATE NOCASE PRIMARY KEY)', True),
    ('CREATE TABLE users (email TEXT COLLATE NOCASE UNIQUE)', True),
    ('CREATE UNIQUE INDEX unique_email ON users(email)', True),
    ('CREATE UNIQUE INDEX active_email ON users(email) WHERE id > 1', False),
    ('CREATE UNIQUE INDEX pair ON users(email, id)', False),
])
def test_email_state_reads_deployed_key_and_exact_committed_value(tmp_path, ddl, direct):
    engine = create_engine('sqlite:///' + str(tmp_path / 'app.sqlite'))
    class User(declarative_base()):
        __tablename__ = 'users'
        id = Column(Integer, primary_key=True)
        email = Column(String, unique=True)  # Metadata deliberately differs from some deployed schemas.
    with engine.begin() as conn:
        if ddl.startswith('CREATE UNIQUE INDEX'):
            conn.exec_driver_sql('CREATE TABLE users (id INTEGER PRIMARY KEY, email TEXT COLLATE NOCASE)')
        conn.exec_driver_sql(ddl)
        conn.exec_driver_sql("INSERT INTO users(email) VALUES ('Mixed@example.org')")
    with unit_runner._app_test_api(tmp_path):
        from _host_app_test import AppTestCase
        case = AppTestCase()
        with Session(engine) as case.db:
            case.User = User
            assert case.signup_email_state('mixed@example.org') == dict(totalRows=1, exactEmailRows=0, hasDirectEmailUniqueKey=direct)
            assert case.signup_email_state('Mixed@example.org')['exactEmailRows'] == 1
    engine.dispose()


def test_concurrent_signup_releases_both_requests_and_preserves_real_server_error(tmp_path):
    from threading import Barrier
    app = FastAPI()
    both_inside = Barrier(2, timeout=5)

    @app.post('/api/auth/signup')
    def signup(data: dict):
        both_inside.wait()
        return JSONResponse({}, status_code=data['status'])

    with unit_runner._app_test_api(tmp_path):
        from _host_app_test import AppTestCase
        case = AppTestCase()
        with TestClient(app, raise_server_exceptions=False) as case.client:
            assert case.concurrent_signup_statuses({'status': 201}, {'status': 500}) == (201, 500)


def test_only_proven_concurrency_helper_misuse_gets_the_bounded_test_repair(tmp_path):
    import sys
    from types import SimpleNamespace
    from agents.runtime.test_guidance import CONCURRENT_SIGNUP_GUIDANCE
    from agents.runtime.test_fix import TestFixRequired, technical_reasons
    from orchestrator.domain import test_not_run

    with unit_runner._app_test_api(tmp_path):
        from _host_app_test import AppTestCase

        class Cases(AppTestCase):
            def test_archived_misuse(self):
                statuses, row_info = self.concurrent_signup_statuses({}, {})
                self.assertIn(2, statuses)

            def test_unrelated_integer(self):
                statuses, row_info = self.concurrent_signup_statuses({}, {})
                self.assertIn(2, self.client.post('/other').status_code)

            def test_unrelated_error(self):
                self.concurrent_signup_statuses({}, {})
                raise TypeError("argument of type 'int' is not iterable")

        for method, protected, expected in [('test_archived_misuse', False, 'SKIP'),
                                            ('test_archived_misuse', True, 'FAIL'),
                                            ('test_unrelated_integer', False, 'FAIL'),
                                            ('test_unrelated_error', False, 'FAIL')]:
            case = Cases(method)
            case.client = SimpleNamespace(post=lambda *args, **kwargs: SimpleNamespace(status_code=201))
            result = unit_runner._Result({case.id(): ('Cases', __name__)}, capture_observations=protected)
            result.startTest(case)
            try:
                getattr(case, method)()
            except TypeError:
                result.addError(case, sys.exc_info())
            result.stopTest(case)
            row = SimpleNamespace(**result.report()['tests'][0])
            assert row.outcome == expected
            reasons = technical_reasons([], [row])
            if expected == 'SKIP':
                assert reasons == {test_not_run.HELPER_ASSERTION}
                feedback = TestFixRequired(reasons, ['qa-unit']).task_input({}, {})['testRunFeedback']
                assert feedback['code'] == 'GENERATED_TESTS_NOT_RUN'
                assert CONCURRENT_SIGNUP_GUIDANCE in feedback['instruction']
            else:
                assert not reasons

        case = Cases('test_archived_misuse')
        result = unit_runner._Result({case.id(): ('Cases', __name__)})
        result.startTest(case)
        result.addSkip(case, test_not_run.HELPER_ASSERTION)
        result.stopTest(case)
        assert not technical_reasons([], [SimpleNamespace(**result.report()['tests'][0])])


@pytest.mark.parametrize('duplicate_status', [409, 500])
def test_duplicate_signup_observes_both_responses_and_one_committed_account(tmp_path, duplicate_status):
    from sqlalchemy.exc import IntegrityError
    engine = create_engine('sqlite:///' + str(tmp_path / 'signup.db'))
    base = declarative_base()
    class User(base):
        __tablename__ = 'users'
        email = Column(String, primary_key=True)
    base.metadata.create_all(engine)
    app = FastAPI()

    @app.post('/api/auth/signup')
    def signup(body: Body):
        with Session(engine) as db:
            db.add(User(email=body.email.strip().lower()))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return JSONResponse({}, status_code=duplicate_status)
        return JSONResponse({}, status_code=201)

    marks = {}
    sample = unit_runner._new_test_password()
    with unit_runner._app_test_api(tmp_path, issued_passwords={sample}, signup_checks=marks):
        from _host_app_test import AppTestCase
        case = AppTestCase()
        case.User = User
        with TestClient(app) as case.client, Session(engine) as case.db:
            statuses = case.concurrent_signup_statuses({'email': ' Test@Example.org ', 'password': sample},
                                                      {'email': 'test@example.org', 'password': sample})
            assert sorted(statuses) == [201, duplicate_status]
            assert case.signup_email_state('test@example.org') == dict(totalRows=1, exactEmailRows=1, hasDirectEmailUniqueKey=True)
            assert marks[case] == {'concurrentCanonicalSignup': duplicate_status == 409, 'canonicalEmailUniqueKey': True}
    engine.dispose()


def test_stored_hash_observes_committed_parameters_and_salts_without_private_values(tmp_path):
    from argon2 import PasswordHasher
    from agents.runtime.security_test_scope import measured_hash_complies
    from orchestrator.domain.validation_feedback import case_feedback
    engine = create_engine('sqlite:///' + str(tmp_path / 'accounts.sqlite'))
    class User(declarative_base()):
        __tablename__ = 'users'
        email = Column(String, primary_key=True)
        password_hash = Column(String)
    User.metadata.create_all(engine)
    password = unit_runner._new_test_password()
    hasher = PasswordHasher(time_cost=2, memory_cost=8192, parallelism=1)
    addresses = ['user1@example.org', 'user2@example.org', 'user3@example.org']
    encodings = [hasher.hash(password), hasher.hash(password)]
    marks = {}
    policy = {'algorithm': 'argon2id', 'memoryKiB': 8192, 'iterations': 2, 'parallelism': 1, 'uniqueSalt': True}
    with unit_runner._app_test_api(tmp_path, issued_passwords={password}, signup_checks=marks):
        from _host_app_test import AppTestCase
        case = AppTestCase()
        case.User = User
        with Session(engine) as case.db:
            case.db.add_all([User(email=name, password_hash=value) for name, value in zip(addresses, [*encodings, encodings[0]])])
            case.db.commit()
            first = case.signup_hash_parameters(addresses[0], password=password)
            assert measured_hash_complies(first, policy) and 'distinctSalts' not in first
            row = {'outcome': 'PASS', 'signupChecks': marks[case]}
            assert generated_requirement_coverage('REQ-005', [row], policy)[1] == ['HASH_PARAMETERS_NOT_OBSERVED']
            second = case.signup_hash_parameters(addresses[1], password=password)
            assert second == {'argon2id': True, 'verified': True, 'memoryKiB': 8192,
                              'iterations': 2, 'parallelism': 1, 'distinctSalts': True}
            assert generated_requirement_coverage('REQ-005', [row], policy)[1] == []
            assert not measured_hash_complies(second, {**policy, 'memoryKiB': 19456})
            assert case.signup_hash_parameters(addresses[2], password=password)['distinctSalts'] is False
            assert not measured_hash_complies(marks[case]['storedHashParameters'], policy)
        wire = {'format': 'unittest-v1', 'total': 1, 'passed': 1, 'failed': 0, 'skipped': 0,
                'tests': [{'testId': 't.C.test_storage', 'outcome': 'PASS', 'signupChecks': marks[case]}]}
        report = parse_unit_report(json.dumps(wire), 0)
        assert case_feedback(report.tests[0])['signupChecks'] == marks[case]
        assert all(value not in json.dumps(wire) for value in [*addresses, *encodings, password])
        wire['tests'][0]['signupChecks'] = {'storedHashParameters': {**second, 'encoded': encodings[0]}}
        with pytest.raises(UnitReportError):
            parse_unit_report(json.dumps(wire), 0)
    engine.dispose()


def test_runner_emits_only_closed_observations_for_the_executing_case(tmp_path):
    # Run handwritten source and generated-test-shaped fixtures in an isolated interpreter.
    import subprocess
    import sys
    product = tmp_path / 'product'
    tests = tmp_path / 'tests'
    product.mkdir()
    tests.mkdir()
    (product / 'database.py').write_text("""import os
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
engine = create_engine(os.environ['DATABASE_URL'])
def get_db():
    with Session(engine) as session:
        yield session
""")
    (product / 'models.py').write_text("""from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import declarative_base
Base = declarative_base()
class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    password_hash = Column(String)
""")
    (product / 'main.py').write_text("""from fastapi import FastAPI
app = FastAPI()
@app.post('/api/auth/signup')
def signup(body: dict):
    return {'ok': True}
""")
    (tests / 'test_app.py').write_text("""from _host_app_test import AppTestCase
from _host_test_data import new_password
class Outputs(AppTestCase):
    def test_request(self):
        sample = new_password()
        self.assertTrue(self.check_signup_output({'password': sample}, password=sample).is_success)
    def test_nothing(self):
        self.assertTrue(True)
""")
    command = ('import json,runpy,sys; fn=runpy.run_path(sys.argv[1])["run_tests"]; '
               'r,c=fn(sys.argv[2],sys.argv[3],capture_output_checks=True); print(json.dumps(r)); sys.exit(c)')
    process = subprocess.run([sys.executable, '-I', '-B', '-c', command, unit_runner.__file__, str(tests), str(product)],
                             capture_output=True, text=True, timeout=20)
    assert process.returncode == 0, process.stdout + process.stderr
    report = parse_unit_report(process.stdout, 0)
    cases = {row.test_id.rsplit('.', 1)[1]: dict(row.output_checks) for row in report.tests}
    assert cases == {'test_request': {'SUCCESS_RESPONSE': 'CHECKED', 'REQUEST_LOGS': 'CHECKED'}, 'test_nothing': {}}


@pytest.mark.parametrize('role', ['QA', 'SECURITY'])
def test_generated_observations_reach_fix_input_without_raw_values(tmp_path, role):
    import unittest
    from agents.runtime.developer_input import _focus_check
    from orchestrator.domain.validation_feedback import case_feedback, _feedback

    password = unit_runner._new_test_password()
    diagnostics = {}
    with captured_fixture(tmp_path, password, unsafe=True, diagnostics=diagnostics) as (case, checks):
        with pytest.raises(AssertionError, match='REQUEST_OUTPUT_EXPOSURE'):
            case.check_signup_output({'password': password}, password=password)
        with pytest.raises(unittest.SkipTest):
            case.check_signup_output({'email': 'uncaught', 'password': password}, password=password)
        row = {'testId': 'suite.C.test_request', 'outcome': 'FAIL',
               'outputChecks': checks[case], 'outputDiagnostics': diagnostics[case]}
        report = {'format': 'unittest-v1', 'total': 1, 'passed': 0, 'failed': 1, 'skipped': 0, 'tests': [row]}
        parsed, = parse_unit_report(json.dumps(report), 1).tests
    version = 'inputs:' + '1' * 64
    feedback = _feedback({**case_feedback(parsed), 'testVersion': version})
    assert feedback['testVersion'] == version
    _, brief = _focus_check({**feedback, 'role': role, 'testId': row['testId'], 'reportArtifactId': 'report'}, {})
    detail = brief['outputDiagnostics']
    assert detail['REJECTION_RESPONSE'] == [{'locations': ['BODY.*.password']}]
    assert detail['STORAGE_ERROR_RESPONSE'] == [{'escapedToClient': True, 'responseStarted': True,
                                                'exceptionType': 'sqlalchemy.exc.OperationalError'}]
    assert brief['observedFailurePaths']['UNHANDLED_DATABASE_ERROR_AFTER_ENTRY'] == ['STORAGE_ERROR_RESPONSE']
    assert feedback['observations'] == []
    assert all(text not in json.dumps(brief) for text in (password, 'unavailable_table', 'received', str(tmp_path), 'expected'))


def test_response_coordinates_hide_dynamic_keys_and_ignore_handled_errors(tmp_path):
    from starlette.responses import JSONResponse
    from agents.runtime.repair_locations import diagnostic_sites
    from hashlib import sha256

    marker = unit_runner._new_test_password()
    async def app(scope, receive, send):
        data = {'detail': [{'input': {marker: marker}}]}
        response = JSONResponse(data, headers={'x-fixture': marker}, status_code=422)
        await response(scope, receive, send)
    client = TestClient(app)
    with unit_runner._observe_request(client, tmp_path) as probe:
        response = client.get('/')
    assert response.status_code == 422
    assert probe.locations(lambda text: marker in text) == ['HEADERS', 'BODY.detail[0].input.*']
    assert probe.escaped is None
    client.close()

    source = b"async def fail(scope, receive, send):\n    raise RuntimeError('private fixture detail')\n"
    filename = tmp_path / 'main.py'
    filename.write_bytes(source)
    fixture = {}
    exec(compile(source, str(filename), 'exec'), fixture)
    client = TestClient(fixture['fail'])
    with unit_runner._observe_request(client, tmp_path) as probe, pytest.raises(RuntimeError):
        client.get('/')
    frame = {'sourceSha256': sha256(source).hexdigest(), 'line': 2}
    assert probe.escaped == {'escapedToClient': True, 'responseStarted': False,
                             'exceptionType': 'builtins.RuntimeError', 'appFrame': frame}
    assert diagnostic_sites([frame], {'main.py': source}) == [{**frame, 'toolPath': 'source/main.py'}]
    assert diagnostic_sites([frame], {'main.py': b'changed'}) == []
    assert diagnostic_sites([frame], {'one.py': source, 'two.py': source}) == []
    client.close()


@pytest.mark.parametrize('bad', [
    {'locations': ['BODY.detail.untrustedKey']}, {'locations': ['BODY'] , 'value': 'secret'},
    {'exceptionType': 'builtins.RuntimeError', 'escapedToClient': True, 'responseStarted': 1},
    {'escapedToClient': True, 'responseStarted': False, 'exceptionType': 'builtins.RuntimeError',
     'appFrame': {'path': '/private/file.py', 'line': 1}},
])
def test_report_rejects_open_ended_diagnostics(bad):
    from orchestrator.domain.validation_feedback import validate_diagnostic, validated_output_diagnostics
    with pytest.raises(ValueError):
        validate_diagnostic(bad)
    with pytest.raises(ValueError):
        validated_output_diagnostics({'SUCCESS_RESPONSE': [{'locations': ['BODY']}]}, {'SUCCESS_RESPONSE': 'CHECKED'})


def test_measured_frame_precedes_static_candidates_in_the_actual_fix_request():
    from hashlib import sha256
    from types import SimpleNamespace
    from agents.runtime.repair_baseline import with_repair_baseline
    from test_repair_feedback import qa_issue, row

    content = b'def commit_change():\n    raise RuntimeError()\n'
    frame = {'sourceSha256': sha256(content).hexdigest(), 'line': 2}
    receipt = {**row('qa', 'case-A'), 'observations': [], 'observationStatus': 'UNVERIFIED',
               'outputChecks': {'STORAGE_ERROR_RESPONSE': 'INCOMPLETE', 'STORAGE_ERROR_REQUEST_LOGS': 'EXPOSED'},
               'outputDiagnostics': {'STORAGE_ERROR_RESPONSE': [
                   {'escapedToClient': True, 'responseStarted': True, 'exceptionType': 'builtins.RuntimeError', 'appFrame': frame}]}}
    request = {'fixRequest': {'issues': [qa_issue()], 'validationFeedback': [receipt]}}
    files = {'database_boundary.py': content, 'main.py': b"@app.post('/signup')\ndef signup(): pass\n"}
    checkpoint = SimpleNamespace(repair_baseline=None, repair_source_files=lambda: files)
    brief = with_repair_baseline(request, checkpoint)['activeRepairFocus']['brief']
    assert brief['observedAppFrames'] == [{**frame, 'toolPath': 'source/database_boundary.py'}]
    assert brief['sourceInspection']['firstReadBatch'][0] == 'source/database_boundary.py'


def test_type_mismatch_records_only_closed_cause_and_rejects_payload(tmp_path):
    from orchestrator.domain.validation_feedback import validate_diagnostic
    source = tmp_path / 'main.py'
    source.write_text('def fail(message):\n    raise TypeError(message)\n')
    scope = {}
    exec(compile(source.read_bytes(), str(source), 'exec'), scope)
    def observe(message):
        with pytest.raises(TypeError) as caught:
            scope['fail'](message)
        result = unit_runner._escaped_exception(caught.value, tmp_path, False)
        validate_diagnostic(result)
        return result
    diag = observe("'private_id' must be a SecretClass (got str).")
    assert diag['observedCause'] == {'code': 'ARGUMENT_TYPE_MISMATCH','actualType':'str'}
    assert 'private' not in repr(diag) and 'SecretClass' not in repr(diag)
    for msg in ("'private_id' must be a Type (got PrivateType).",
                "'private_id' must be a Type (got str). sql=private", object()):
        assert 'observedCause' not in observe(msg)
    for invalid in ({'actualType':'secret','code':'ARGUMENT_TYPE_MISMATCH'},
                    {**diag['observedCause'],'message':'forbidden'}):
        with pytest.raises(ValueError):
            validate_diagnostic({**diag, 'observedCause':invalid})
    with pytest.raises(ValueError):
        validate_diagnostic({**diag, 'exceptionType':'builtins.RuntimeError'})
