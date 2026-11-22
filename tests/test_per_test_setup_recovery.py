"""Per-case preparation is not execution of the test body.

Trusted, handwritten fixtures only; no model output executes on the Host.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from agents.runtime.test_fix import technical_reasons
from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report
from orchestrator.domain import test_not_run


def execute(cases):
    with TemporaryDirectory() as temporary:
        suite = unittest.TestSuite(cases)
        with patch.object(unit_runner.unittest.TestLoader, 'discover', return_value=suite):
            report, code = unit_runner.run_tests(Path(temporary), Path(temporary))
        assert report is not None
        return parse_unit_report(json.dumps(report), code), code


class PerTestSetupRecoveryTests(unittest.TestCase):
    def test_setup_failure_keeps_evidence_and_runs_independent_cases(self):
        bodies = []

        class Fixture(unittest.TestCase):
            def setUp(self):
                if self._testMethodName == 'test_blocked':
                    raise RuntimeError('fixture database not started')

            def test_blocked(self):
                bodies.append('blocked')

            def test_ok(self):
                bodies.append('ok')

        blocked = Fixture('test_blocked')
        report, code = execute([blocked, Fixture('test_ok')])
        rows = {row.test_id: row for row in report.tests}
        self.assertEqual(bodies, ['ok'])
        self.assertEqual((code, report.passed, report.failed, report.skipped), (1, 1, 1, 1))
        self.assertEqual((rows[blocked.id()].outcome, rows[blocked.id()].details),
                         ('SKIP', test_not_run.SETUP_FAILED))
        preparation = rows['setUp (' + blocked.id() + ')']
        self.assertEqual(preparation.outcome, 'FAIL')
        self.assertIn('RuntimeError: fixture database not started', preparation.details)
        self.assertIn('[phase=SETUP]', preparation.details)
        self.assertIn('[origin=', preparation.details)
        self.assertEqual(technical_reasons([], report.tests), {test_not_run.SETUP_FAILED})

    def test_setup_assertion_is_also_not_run(self):
        class Fixture(unittest.TestCase):
            def setUp(self):
                self.fail('setup precondition')

            def test_never(self):
                raise RuntimeError('body must not execute')

        report, _ = execute([Fixture('test_never')])
        self.assertEqual((report.failed, report.skipped), (1, 1))
        self.assertEqual(technical_reasons([], report.tests), {test_not_run.SETUP_FAILED})

    def test_body_error_and_teardown_error_remain_failures(self):
        class BodyFailure(unittest.TestCase):
            def test_error(self):
                raise RuntimeError('body failure')

        class TeardownFailure(unittest.TestCase):
            def test_ok(self):
                pass

            def tearDown(self):
                raise RuntimeError('cleanup failure')

        report, code = execute([BodyFailure('test_error'), TeardownFailure('test_ok')])
        self.assertEqual((code, report.failed, report.skipped), (1, 2, 0))
        self.assertEqual(technical_reasons([], report.tests), set())

    def test_intentional_setup_skip_is_not_a_technical_repair(self):
        class Fixture(unittest.TestCase):
            def setUp(self):
                self.skipTest('not supported')

            def test_never(self):
                self.fail('must not execute')

        report, code = execute([Fixture('test_never')])
        self.assertEqual((code, report.failed, report.skipped), (0, 0, 1))
        self.assertEqual(technical_reasons([], report.tests), set())

    def test_body_calling_setup_helper_is_still_an_executed_failure(self):
        class Fixture(unittest.TestCase):
            def setUp(self):
                if getattr(self, 'in_body', False):
                    raise RuntimeError('failure after body started')

            def test_error(self):
                self.in_body = True
                self._callSetUp()

        report, _ = execute([Fixture('test_error')])
        self.assertEqual((report.failed, report.skipped), (1, 0))
        self.assertEqual(technical_reasons([], report.tests), set())

    def test_product_origin_is_preserved_without_declaring_a_test_defect(self):
        class Fixture(unittest.TestCase):
            def setUp(self):
                raise RuntimeError('product startup failure')

            def test_never(self):
                self.fail('must not execute')

        with patch.object(unit_runner._Result, '_origin', return_value='PRODUCT'):
            report, _ = execute([Fixture('test_never')])
        preparation = next(row for row in report.tests if row.test_id.startswith('setUp ('))
        self.assertIn('[origin=PRODUCT]', preparation.details)
        self.assertFalse(any(row.outcome == 'PASS' for row in report.tests))

    def test_only_closed_fixture_id_shapes_are_accepted(self):
        self.assertTrue(test_not_run.is_fixture_row('setUp (test_signup.SignupTests.test_success)'))
        self.assertFalse(test_not_run.is_fixture_row('test_signup.SignupTests.test_success'))
        self.assertFalse(test_not_run.is_fixture_row('setUp (../test_signup.py)'))


class AsyncSetupRecoveryTests(unittest.TestCase):
    def test_async_setup_failure_is_not_run(self):
        class Fixture(unittest.IsolatedAsyncioTestCase):
            async def asyncSetUp(self):
                raise RuntimeError('async setup failure')

            async def test_never(self):
                self.fail('must not execute')

        report, _ = execute([Fixture('test_never')])
        self.assertEqual((report.failed, report.skipped), (1, 1))
        self.assertEqual(technical_reasons([], report.tests), {test_not_run.SETUP_FAILED})
