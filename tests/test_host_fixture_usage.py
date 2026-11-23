"""Handwritten, offline fixtures only; never import or execute saved Agent Source."""
import inspect
import json
import sys
from types import SimpleNamespace as N

import pytest

from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.runtime.test_fix import TestFixRequired, generated_test_feedback, technical_reasons
from agents.runtime.test_guidance import unit_test_targets
from mcp_tools.tools import unit_runner as runner
from mcp_tools.tools.unit_report import parse_unit_report
from orchestrator.domain import test_not_run


def source(body, *, prefix='from _host_app_test import AppTestCase\n', base='AppTestCase', helpers=''):
    return prefix + f'class Cases({base}):\n' + helpers + '    def test_case(self):\n' + body


def codes(text):
    return {r['reasonCode'] for r in runner.app_fixture_source_issues(text)}


def test_public_contract_matches_actual_signatures_and_has_independent_copies(tmp_path):
    contract = runner.app_fixture_contract()
    with runner._app_test_api(tmp_path):
        cls = sys.modules[contract['module']].AppTestCase
        for name, spec in contract['methods'].items():
            assert {a['name'] for a in spec['arguments']} == set(spec['parameters']) | set(spec['keywordOnly'])
            parameters = inspect.signature(getattr(cls, name)).parameters
            assert [k for k, v in parameters.items() if k != 'self' and v.kind != v.KEYWORD_ONLY] == spec['parameters']
            assert [k for k, v in parameters.items() if v.kind == v.KEYWORD_ONLY] == spec['keywordOnly']
        assert all(not hasattr(cls, name) for name in contract['notProvided'])
    for role in ('qa', 'security'):
        config = N(scopes=[N(kind='QA_TESTS', name='generated', source_directory='tests', pattern='test_*.py')])
        assert unit_test_targets(config, role)[0]['appFixture'] == contract
    contract['methods']['signup_email_state']['returns']['properties'].clear()
    assert len(runner.app_fixture_contract()['methods']['signup_email_state']['returns']['properties']) == 3


@pytest.mark.parametrize('expression', ['self.fresh_email', 'self.fresh_email()', 'self.new_password(length=12)'])
def test_missing_known_host_member_is_found_before_ready(expression):
    assert codes(source(f'        value = {expression}\n')) == {'TEST_HOST_MEMBER_MISSING'}


def test_only_numeric_use_of_unpacked_state_is_flagged():
    unpack = '        total, exact, key = self.signup_email_state(email)\n'
    assert codes(source(unpack + '        self.assertEqual(exact, 0)\n')) == {'TEST_HOST_RETURN_MISUSED'}
    assert not codes(source(unpack + '        self.assertEqual(exact, "exactEmailRows")\n'))
    assert not codes(source('        state = self.signup_email_state(email)\n'
                           '        self.assertEqual(state["exactEmailRows"], 0)\n'))


def test_concurrent_return_is_a_pair_of_integers_not_containers():
    assert codes(source('        first, second = self.concurrent_signup_statuses(a, b)\n'
                        '        self.assertIn(201, first)\n')) == {'TEST_HOST_RETURN_MISUSED'}
    assert not codes(source('        statuses = self.concurrent_signup_statuses(a, b)\n'
                            '        self.assertIn(201, statuses)\n'))


def test_user_helpers_aliases_and_unknown_dynamic_cases_are_preserved():
    good = source('        self.fresh_email()\n', helpers='    def fresh_email(self):\n        return None\n')
    assert not codes(good)
    assert not codes(source('        self.fresh_email\n', helpers='    fresh_email = None\n'))
    assert not codes(source('        self.fresh_email = None\n        self.fresh_email\n'))
    assert not codes(source('        self.fresh_email()\n', base='ExternalMixin'))
    assert not codes(source('        self.fresh_email()\n', helpers='    def __getattr__(self, name):\n        return None\n'))
    assert not codes(source('        setattr(self, "fresh_email", None)\n        self.fresh_email\n'))
    assert not codes(source('        self.fresh_email\n', prefix='class AppTestCase: pass\n'))
    alias = source('        self.fresh_email\n', prefix='from _host_app_test import AppTestCase as HostCase\n', base='HostCase')
    assert codes(alias) == {'TEST_HOST_MEMBER_MISSING'}
    assert not codes(alias.replace('class Cases', 'from other_module import HostCase\nclass Cases'))
    assert not codes(alias.replace('class Cases', 'def HostCase(): pass\nclass Cases'))
    assert not codes(alias + 'HostCase.fresh_email = lambda self: None\n')
    inherited = good.replace('        self.fresh_email()', '        pass') + 'class Child(Cases):\n    def test_child(self):\n        self.fresh_email()\n'
    assert not codes(inherited)
    custom_state = source('        a, b, c = self.signup_email_state(email)\n        self.assertEqual(a, 0)\n',
                          helpers='    def signup_email_state(self, value):\n        return (0, 0, False)\n')
    assert not codes(custom_state)


def test_precheck_reports_closed_codes_only_and_respects_fixture_opt_in(tmp_path):
    tests = tmp_path / 'outputs' / 'qa' / 'tests'
    tests.mkdir(parents=True)
    (tests / 'test_case.py').write_text(source('        self.fresh_email\n'), encoding='utf-8')
    scopes = [('qa-unit', 'test_*.py')]
    rows = [{'selector': 'qa-unit', 'testId': 'test_case.Cases.test_case'}]
    assert inspect_generated_tests(tests, scopes, rows, 'cases', host_app_fixture=True) == [
        {'fieldPath': '/cases', 'reasonCode': 'TEST_HOST_MEMBER_MISSING'}]
    assert inspect_generated_tests(tests, scopes, rows, 'cases') == []


@pytest.fixture
def cases(tmp_path):
    # Public helper reads a real, isolated DB made by this trusted test fixture.
    from sqlalchemy import Column, Integer, String, create_engine
    from sqlalchemy.orm import Session, declarative_base
    class User(declarative_base()):
        __tablename__ = 'users'
        id = Column(Integer, primary_key=True)
        email = Column(String, unique=True)
        password_hash = Column(String)
    engine = create_engine('sqlite:///' + str(tmp_path / 'fixture.db'))
    User.metadata.create_all(engine)
    issued = set()
    with runner._app_test_api(tmp_path, exposure_checks={}, issued_passwords=issued, signup_checks={}):
        AppTestCase = sys.modules[runner.APP_FIXTURE_MODULE].AppTestCase

        class Cases(AppTestCase):
            def test_missing_member(self):
                value = self.fresh_email

            def test_missing_factory(self):
                self.new_password(length=12)

            def test_invalid_password_argument(self):
                self.check_signup_output({'password': self.sample}, password=[self.sample])

            def test_invalid_hash_argument(self):
                self.signup_hash_parameters('sample@example.org', password=[self.sample])

            def test_short_password_argument(self):
                sample = runner._new_test_password(length=7)
                self.check_signup_output({'password': sample}, password=sample)

            def test_unknown_hash_result_key(self):
                state = self.signup_hash_parameters('sample@example.org', password=self.sample)
                self.assertEqual(state['algorithm'], 'argon2id')

            def test_forged_guard_text(self):
                raise ValueError('SUPPLY_SYNTHETIC_PASSWORD_PRESENT_IN_REQUEST')

            def test_misread_dict(self):
                total, exact, unique = self.signup_email_state('unused@example.org')
                self.assertEqual(exact, 0)

            def test_wrong_product_rows(self):
                state = self.signup_email_state('unused@example.org')
                self.assertEqual(state['exactEmailRows'], 1)

            def test_wrong_product_status(self):
                self.assertEqual(self.client.status_code, 201)

            def test_unrelated_attribute(self):
                return self.client.fresh_email

            def test_unrelated_assertion(self):
                total, exact, unique = self.signup_email_state('unused@example.org')
                intervening_statement = True
                self.assertEqual(exact, 0)

            def test_intentional_key_failure(self):
                total, exact, unique = self.signup_email_state('unused@example.org')
                self.assertEqual(exact, 'someOtherKey')

        with Session(engine) as db:
            def make(method):
                case = Cases(method)
                case.db, case.User, case.client = db, User, N(status_code=500)
                case.sample = runner._new_test_password()
                issued.add(case.sample)
                if method == 'test_unknown_hash_result_key':
                    from argon2 import PasswordHasher
                    db.query(User).delete()
                    db.add(User(email='sample@example.org', password_hash=PasswordHasher().hash(case.sample)))
                    db.commit()
                return case
            yield make
    engine.dispose()


def measure(case, *, protected=False, generated=True):
    result = runner._Result({case.id(): ('Cases', __name__)}, capture_observations=protected,
                            fixture_usage_repair=generated)
    result.startTest(case)
    try:
        getattr(case, case._testMethodName)()
    except AssertionError:
        result.addFailure(case, sys.exc_info())
    except Exception:
        result.addError(case, sys.exc_info())
    else:
        result.addSuccess(case)
    result.stopTest(case)
    raw = result.report()
    # Exercise the real receipt parser as well as the runner.
    return parse_unit_report(json.dumps(raw), int(bool(raw['failed']))).tests[0]


@pytest.mark.parametrize('method,reason', [
    ('test_missing_member', test_not_run.FIXTURE_MEMBER_MISSING),
    ('test_missing_factory', test_not_run.FIXTURE_MEMBER_MISSING),
    ('test_misread_dict', test_not_run.FIXTURE_RETURN_MISUSED),
    ('test_invalid_password_argument', test_not_run.FIXTURE_ARGUMENT_INVALID),
    ('test_invalid_hash_argument', test_not_run.FIXTURE_ARGUMENT_INVALID),
    ('test_short_password_argument', test_not_run.FIXTURE_ARGUMENT_INVALID),
    ('test_unknown_hash_result_key', test_not_run.FIXTURE_RETURN_MISUSED),
])
def test_proven_errors_get_same_case_repair_with_safe_feedback(cases, method, reason):
    case = cases(method)
    row = measure(case)
    assert row.test_id == case.id() and row.outcome == 'SKIP' and row.details == reason
    assert technical_reasons([], [row]) == {reason}
    receipt = N(report=N(tests=[row]), report_ref='artifact://fixture/generated.json')
    observed = generated_test_feedback({'qa-unit': (receipt, None)}, [('run_unit_tests', 'qa-unit')])
    feedback = TestFixRequired([reason], ['qa-unit'], observed_cases=observed).task_input({}, {})['testRunFeedback']
    assert feedback['code'] == 'GENERATED_TESTS_NOT_RUN'
    assert feedback['observedCases'][0]['reasonCode'] == reason
    assert feedback['observedCases'][0]['caseOutcome'] == 'UNVERIFIED'
    assert 'read state' in feedback['instruction']
    assert 'unused@example.org' not in json.dumps(feedback)
    assert measure(cases(method), protected=True).outcome == 'FAIL'
    assert measure(cases(method), generated=False).outcome == 'FAIL'


@pytest.mark.parametrize('method', ['test_wrong_product_rows', 'test_wrong_product_status',
    'test_unrelated_attribute', 'test_unrelated_assertion', 'test_intentional_key_failure', 'test_forged_guard_text'])
def test_real_or_uncertain_failures_remain_fail(cases, method):
    row = measure(cases(method))
    assert row.outcome == 'FAIL' and not technical_reasons([], [row])


def test_overridden_return_shape_is_not_reclassified(cases):
    case = cases('test_misread_dict')
    case.signup_email_state = lambda _: (0, 1, False)
    row = measure(case)
    assert row.outcome == 'FAIL' and not technical_reasons([], [row])


@pytest.mark.parametrize('reason', list(test_not_run.FIXTURE_USAGE))
def test_test_authored_skip_cannot_claim_verified_misuse(cases, reason):
    case = cases('test_missing_member')
    result = runner._Result({case.id(): ('Cases', __name__)})
    result.startTest(case)
    result.addSkip(case, reason)
    result.stopTest(case)
    row = N(**result.report()['tests'][0])
    assert row.outcome == 'SKIP' and not technical_reasons([], [row])
