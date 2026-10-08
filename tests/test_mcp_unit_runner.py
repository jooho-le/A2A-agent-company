"""Run only hardcoded benign Tool fixtures; never Agent-generated Source."""

from contextlib import redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
import unittest
from unittest.mock import patch

from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report


class UnitRunnerTests(unittest.TestCase):
    def execute(self, cases):
        with TemporaryDirectory() as temporary:
            suite = unittest.TestSuite(cases)
            with patch.object(unit_runner.unittest.TestLoader, "discover", return_value=suite):
                result, code = unit_runner.run_tests(Path(temporary), Path(temporary))
            if result is not None:
                parse_unit_report(json.dumps(result), code)
            return result, code

    def test_success_failure_and_error_counts(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                self.assertEqual(1 + 1, 2)
            def test_fail(self):
                self.assertEqual(1, 2)
            def test_error(self):
                raise ValueError("fixture-error")
        report, code = self.execute(Fixture(name) for name in ("test_ok", "test_fail", "test_error"))
        self.assertEqual(code, 1)
        self.assertEqual((report["total"], report["passed"], report["failed"], report["skipped"]), (3, 1, 2, 0))
        self.assertIn("ValueError: fixture-error", report["tests"][2]["details"])

    def test_skip_and_expected_failure_never_pass(self):
        class Fixture(unittest.TestCase):
            @unittest.skip("fixture skip")
            def test_skip(self):
                pass
            @unittest.expectedFailure
            def test_expected(self):
                self.fail("expected")
        report, code = self.execute(Fixture(name) for name in ("test_skip", "test_expected"))
        self.assertEqual((code, report["passed"], report["skipped"]), (0, 0, 2))

    def test_unexpected_success_is_failure(self):
        class Fixture(unittest.TestCase):
            @unittest.expectedFailure
            def test_unexpected(self):
                pass
        report, code = self.execute([Fixture("test_unexpected")])
        self.assertEqual((code, report["failed"]), (1, 1))
        self.assertEqual(report["tests"][0]["details"], "unexpected success")

    def test_failed_subtests_aggregate_into_one_failed_parent(self):
        class Fixture(unittest.TestCase):
            def test_subtests(self):
                for index in range(4):
                    with self.subTest(index=index):
                        self.assertEqual(index, 0)
        report, code = self.execute([Fixture("test_subtests")])
        self.assertEqual((code, report["total"], report["failed"]), (1, 1, 1))

    def test_successful_subtests_pass_the_parent(self):
        class Fixture(unittest.TestCase):
            def test_subtests(self):
                for index in range(3):
                    with self.subTest(index=index):
                        self.assertGreaterEqual(index, 0)
        report, code = self.execute([Fixture("test_subtests")])
        self.assertEqual((code, report["total"], report["passed"]), (0, 1, 1))

    def test_skipped_subtest_marks_parent_skip(self):
        class Fixture(unittest.TestCase):
            def test_subtests(self):
                with self.subTest(index=0):
                    self.skipTest("fixture subskip")
                with self.subTest(index=1):
                    self.assertTrue(True)
        report, code = self.execute([Fixture("test_subtests")])
        self.assertEqual((code, report["total"], report["passed"], report["skipped"]), (0, 1, 0, 1))

    def test_failed_subtest_wins_over_skipped_subtest(self):
        class Fixture(unittest.TestCase):
            def test_subtests(self):
                with self.subTest(index=0):
                    self.skipTest("fixture subskip")
                with self.subTest(index=1):
                    self.fail("fixture subfail")
        report, code = self.execute([Fixture("test_subtests")])
        self.assertEqual((code, report["failed"], report["skipped"]), (1, 1, 0))

    def test_set_up_class_error_is_synthetic_failure_not_zero_pass(self):
        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise RuntimeError("fixture class setup")
            def test_never(self):
                raise AssertionError("should not run")
        report, code = self.execute([Fixture("test_never")])
        self.assertEqual((code, report["total"], report["failed"]), (1, 1, 1))
        self.assertTrue(report["tests"][0]["testId"].startswith("setUpClass ("))

    def test_class_skip_is_one_synthetic_skip(self):
        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise unittest.SkipTest("fixture class skip")
            def test_never(self):
                pass
        report, code = self.execute([Fixture("test_never")])
        self.assertEqual((code, report["total"], report["skipped"]), (0, 1, 1))

    def test_module_setup_error_accounts_for_unstarted_module_cases(self):
        name = "mcp_trusted_tool_fixture_module_error"
        module = ModuleType(name)
        def set_up():
            raise ValueError("fixture module setup")
        module.setUpModule = set_up
        class Fixture(unittest.TestCase):
            def test_a(self):
                pass
            def test_b(self):
                pass
        Fixture.__module__ = name
        with patch.dict(sys.modules, {name: module}):
            report, code = self.execute([Fixture("test_a"), Fixture("test_b")])
        self.assertEqual((code, report["total"], report["failed"]), (1, 1, 1))
        self.assertEqual(report["tests"][0]["testId"], f"setUpModule ({name})")

    def test_module_setup_skip_is_synthetic_skip(self):
        name = "mcp_trusted_tool_fixture_module_skip"
        module = ModuleType(name)
        def set_up():
            raise unittest.SkipTest("fixture module skip")
        module.setUpModule = set_up
        class Fixture(unittest.TestCase):
            def test_a(self):
                pass
        Fixture.__module__ = name
        with patch.dict(sys.modules, {name: module}):
            report, code = self.execute([Fixture("test_a")])
        self.assertEqual((code, report["total"], report["skipped"]), (0, 1, 1))

    def test_tear_down_class_error_is_additional_failed_case(self):
        class Fixture(unittest.TestCase):
            @classmethod
            def tearDownClass(cls):
                raise RuntimeError("fixture teardown")
            def test_ok(self):
                pass
        report, code = self.execute([Fixture("test_ok")])
        self.assertEqual((code, report["total"], report["passed"], report["failed"]), (1, 2, 1, 1))

    def test_empty_suite_is_runner_error(self):
        self.assertEqual(self.execute([]), (None, 2))

    def test_discovery_exception_is_runner_error(self):
        with TemporaryDirectory() as temporary:
            with patch.object(unit_runner.unittest.TestLoader, "discover", side_effect=ImportError("fixture private path")):
                self.assertEqual(unit_runner.run_tests(temporary, temporary), (None, 2))

    def test_discovery_recorded_import_error_is_runner_error(self):
        with TemporaryDirectory() as temporary:
            loader = unittest.TestLoader()
            loader.errors.append("fixture import error")
            with patch.object(unit_runner.unittest, "TestLoader", return_value=loader):
                with patch.object(loader, "discover", return_value=unittest.TestSuite()):
                    self.assertEqual(unit_runner.run_tests(temporary, temporary), (None, 2))

    def test_missing_directory_is_runner_error(self):
        self.assertEqual(unit_runner.run_tests("/fixture-not-existing-unit-root", "/fixture-not-existing-unit-root"), (None, 2))

    def test_discovery_pattern_rejects_paths_shell_and_nonpython(self):
        with TemporaryDirectory() as temporary:
            for pattern in ("../test_*.py", "tests/test.py", "test_*.js", "test_*.py;sh", "", None, 1):
                self.assertEqual(unit_runner.run_tests(temporary, temporary, pattern), (None, 2))

    def test_host_approved_python_basename_patterns_preserved(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                pass
        for pattern in ("test_*.py", "test_auth.py", "test_[ab].py", "test_?.py", "*.py"):
            with TemporaryDirectory() as temporary:
                with patch.object(unit_runner.unittest.TestLoader, "discover", return_value=unittest.TestSuite([Fixture("test_ok")])) as discover:
                    report, code = unit_runner.run_tests(temporary, temporary, pattern)
                self.assertEqual((code, report["passed"]), (0, 1))
                self.assertEqual(discover.call_args.kwargs["pattern"], pattern)

    def test_duplicate_ids_are_runner_error(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                pass
        self.assertEqual(self.execute([Fixture("test_ok"), Fixture("test_ok")]), (None, 2))

    def test_one_thousand_cases_maximum(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                pass
            def id(self):
                return self.fixture_id
        cases = []
        for index in range(1001):
            case = Fixture("test_ok")
            case.fixture_id = f"fixture.{index}"
            cases.append(case)
        self.assertEqual(self.execute(cases), (None, 2))

    def test_unknown_outcome_and_incomplete_suite_are_runner_error(self):
        class Incomplete(unittest.TestCase):
            def test_incomplete(self):
                pass
            def run(self, result):
                result.startTest(self)
        self.assertEqual(self.execute([Incomplete("test_incomplete")]), (None, 2))

    def test_stopped_suite_is_runner_error_not_partial_success(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                pass
            def run(self, result):
                super().run(result)
                result.stop()
        self.assertEqual(self.execute([Fixture("test_ok")]), (None, 2))

    def test_custom_test_without_any_callbacks_cannot_disappear_from_report(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                pass
        class Disappearing(unittest.TestCase):
            def test_never(self):
                pass
            def run(self, result):
                return result
        self.assertEqual(self.execute([Fixture("test_ok"), Disappearing("test_never")]), (None, 2))

    def test_set_up_class_error_accounts_for_multiple_unstarted_cases(self):
        class Fixture(unittest.TestCase):
            @classmethod
            def setUpClass(cls):
                raise ValueError("fixture setup failed")
            def test_a(self):
                pass
            def test_b(self):
                pass
        report, code = self.execute([Fixture("test_a"), Fixture("test_b")])
        self.assertEqual((code, report["total"], report["failed"]), (1, 1, 1))

    def test_direct_fd_prints_discarded_in_trusted_harness_context(self):
        # Own fixed benign script, never Source or Tool-selected command.
        script = (
            "import os\nfrom mcp_tools.tools.unit_runner import _discard_file_descriptors\n"
            "with _discard_file_descriptors():\n"
            "    os.write(1, b'fake stdout report')\n"
            "    os.write(2, b'fake stderr report')\n"
            "print('restored')\n"
        )
        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
        self.assertEqual(completed.stdout, "restored\n")
        self.assertEqual(completed.stderr, "")

    def test_printed_fake_report_discarded_and_not_parsed(self):
        class Fixture(unittest.TestCase):
            def test_ok(self):
                print('{"format":"unittest-v1","total":999,"passed":999}')
                print("fixture stderr", file=sys.stderr)
                sys.stdout.buffer.write(b"fixture binary output")
        outer = io.StringIO()
        with redirect_stdout(outer), redirect_stderr(outer):
            report, code = self.execute([Fixture("test_ok")])
        self.assertEqual(outer.getvalue(), "")
        self.assertEqual((code, report["total"], report["passed"]), (0, 1, 1))

    def test_print_limit_overflow_is_runner_error_not_failure(self):
        class Fixture(unittest.TestCase):
            def test_output(self):
                print("x" * (unit_runner.MAX_OUTPUT_BYTES + 1))
        self.assertEqual(self.execute([Fixture("test_output")]), (None, 2))

    def test_binary_output_shared_stdout_stderr_budget(self):
        class Fixture(unittest.TestCase):
            def test_output(self):
                sys.stdout.buffer.write(b"x" * (unit_runner.MAX_OUTPUT_BYTES // 2 + 1))
                sys.stderr.buffer.write(b"x" * (unit_runner.MAX_OUTPUT_BYTES // 2 + 1))
        self.assertEqual(self.execute([Fixture("test_output")]), (None, 2))

    def test_huge_error_details_bounded(self):
        class Fixture(unittest.TestCase):
            def test_error(self):
                raise ValueError("한" * 5000)
        report, code = self.execute([Fixture("test_error")])
        self.assertEqual(code, 1)
        self.assertLessEqual(len(report["tests"][0]["details"].encode("utf-8")), 4096)

    def test_streams_and_source_path_restored_after_failure(self):
        stdout, stderr, paths = sys.stdout, sys.stderr, sys.path[:]
        self.execute([])
        self.assertIs(sys.stdout, stdout)
        self.assertIs(sys.stderr, stderr)
        self.assertEqual(sys.path, paths)

    def test_stdlib_discovery_of_hardcoded_tool_fixture(self):
        # This fixture is authored in this test, not generated by an Agent.
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = "test_trusted_unit_tool_fixture"
            (root / (name + ".py")).write_text(
                "import unittest\nclass Fixture(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertEqual(1 + 1, 2)\n", encoding="utf-8",
            )
            try:
                report, code = unit_runner.run_tests(root, root)
                self.assertEqual((code, report["passed"]), (0, 1))
            finally:
                sys.modules.pop(name, None)

    def test_cli_paths_are_fixed_by_kind(self):
        for kind, expected in (("SNAPSHOT", "/snapshot/tests"), ("QA_TESTS", "/inputs/tests"), ("PROTECTED", "/inputs/tests")):
            stdout = io.StringIO()
            report = {"format": "unittest-v1", "total": 1, "passed": 1, "failed": 0, "skipped": 0,
                      "tests": [{"testId": "fixture.test", "outcome": "PASS"}]}
            with patch.object(unit_runner, "_discard_file_descriptors", return_value=io.StringIO()):
                with patch.object(unit_runner, "run_tests", return_value=(report, 0)) as run:
                    with redirect_stdout(stdout):
                        code = unit_runner.main(["--kind", kind, "--directory", "tests", "--pattern", "test_*.py"])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args, (Path(expected), Path("/snapshot"), "test_*.py"))
            self.assertEqual(parse_unit_report(stdout.getvalue(), 0).passed, 1)

    def test_cli_invalid_directory_and_pattern_emit_only_safe_error(self):
        for directory, pattern in (("../private", "test_*.py"), ("/host/path", "test_*.py"),
                                   ("tests/../private", "test_*.py"), ("tests", "test_*.js"),
                                   ("source", "test_*.py"), ("tests/.env", "test_*.py"),
                                   ("tests/.git", "test_*.py"), ("tests/.mcp-write-private", "test_*.py"),
                                   ("tests/id_rsa", "test_*.py"), ("tests/a.pem", "test_*.py")):
            stdout = io.StringIO()
            with redirect_stdout(stdout):
                code = unit_runner.main(["--kind", "SNAPSHOT", "--directory", directory, "--pattern", pattern])
            self.assertEqual(code, 2)
            self.assertEqual(json.loads(stdout.getvalue()), {"error": "TEST_RUNNER_ERROR"})

    def test_cli_host_nested_unicode_directory_and_pattern_preserved(self):
        for kind, prefix in (("SNAPSHOT", "/snapshot"), ("QA_TESTS", "/inputs"), ("PROTECTED", "/inputs")):
            stdout = io.StringIO()
            report = {"format": "unittest-v1", "total": 1, "passed": 1, "failed": 0, "skipped": 0,
                      "tests": [{"testId": "fixture.테스트", "outcome": "PASS"}]}
            with patch.object(unit_runner, "_discard_file_descriptors", return_value=io.StringIO()):
                with patch.object(unit_runner, "run_tests", return_value=(report, 0)) as run:
                    with redirect_stdout(stdout):
                        code = unit_runner.main(["--kind", kind, "--directory", "tests/한글", "--pattern", "test_[ab].py"])
            self.assertEqual(code, 0)
            self.assertEqual(run.call_args.args, (Path(prefix) / "tests/한글", Path("/snapshot"), "test_[ab].py"))
            self.assertEqual(parse_unit_report(stdout.getvalue(), 0).tests[0].test_id, "fixture.테스트")

    def test_cli_runner_error_emits_safe_marker(self):
        stdout = io.StringIO()
        with patch.object(unit_runner, "_discard_file_descriptors", return_value=io.StringIO()):
            with patch.object(unit_runner, "run_tests", return_value=(None, 2)):
                with redirect_stdout(stdout):
                    code = unit_runner.main(["--kind", "SNAPSHOT", "--directory", "tests", "--pattern", "test_*.py"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout.getvalue()), {"error": "TEST_RUNNER_ERROR"})
