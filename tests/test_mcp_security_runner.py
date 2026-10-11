"""Trusted fake-Bandit AST harness checks; Source is never executed/imported."""

import ast
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from mcp_tools.tools import security_contract, security_runner
from mcp_tools.tools.security_report import parse_security_report


def host(**changes):
    data = {"scanner": "bandit", "profile_name": "default", "scanner_version": "1.8.6",
            "rule_ids": ["B102", "B307"], "profile_ref": "artifact://policy/security.json",
            "ignore_nosec": True, "scan_scope": "ALL_PYTHON"}
    data.update(changes)
    return data


def plugin(rule, module="bandit.plugins.fixture", config=None):
    return SimpleNamespace(plugin=SimpleNamespace(_test_id=rule, __module__=module, _config=config))


class FakeManager:
    def __init__(self, selected):
        self.files_list, self.excluded_files, self.skipped, self.results, self.baseline = [], [], [], [], []
        self.ignore_nosec = True
        self.discover_hook = None
        self.run_hook = None
        extensions = [plugin(rule) for rule in sorted(selected) if rule not in {"B307", "B401"}]
        blacklists = [{"id": rule} for rule in sorted(selected) if rule in {"B307", "B401"}]
        if blacklists:
            extensions.append(plugin("B001", "bandit.core.blacklisting", {"Call": blacklists}))
        self.b_ts = SimpleNamespace(plugins=extensions)

    def discover_files(self, targets, recursive=False):
        self.discovery = (list(targets), recursive)
        self.files_list = list(targets)
        if self.discover_hook:
            self.discover_hook(self)

    def get_skipped(self):
        return list(self.skipped)

    def run_tests(self):
        # Only trusted fixture AST is parsed in tests, never exec/import.
        for path in self.files_list:
            try:
                ast.parse(Path(path).read_bytes())
            except (SyntaxError, UnicodeError):
                self.skipped.append((path, "syntax error"))
        if self.run_hook:
            self.run_hook(self)


class SecurityRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "app.py").write_text("raise RuntimeError('must not execute fixture')\n", encoding="utf-8")
        (self.root / "z.py").write_text("value = 1\n", encoding="utf-8")
        self.configuration = Mock(return_value=SimpleNamespace(config_file=None))
        self.manager = FakeManager({"B102", "B307"})
        self.manager_type = Mock(return_value=self.manager)
        self.registry = SimpleNamespace(
            plugins_by_id={rule: plugin(rule) for rule in ("B102", "B105", "B602", "B608")},
            blacklist_by_id={rule: {"id": rule} for rule in ("B307", "B401")},
        )
        self.loader = Mock(return_value=(self.configuration, self.manager_type, self.registry))
        self.version = Mock(return_value="1.8.6")

    def scan(self, config=None, **changes):
        options = dict(contract=security_contract, bandit_loader=self.loader, version_provider=self.version)
        options.update(changes)
        return security_runner.run_security_scan(config or host(), self.root, **options)

    def error(self, result):
        self.assertEqual(result, ({"error": "SCANNER_ERROR"}, 2))

    def issue(self, **changes):
        fields = dict(fname=str(self.root / "app.py"), test_id="B102", test="exec_used", lineno=1,
                      col_offset=0, severity="MEDIUM", confidence="HIGH", text="unexported raw snippet",
                      fdata=b"secret fixture payload", cwe=999)
        fields.update(changes)
        return SimpleNamespace(**fields)

    def test_import_is_inert_and_uses_no_host_bandit_dependency(self):
        self.loader.assert_not_called()
        self.manager_type.assert_not_called()
        self.assertNotIn("bandit.core.manager", sys.modules)

    def test_zero_findings_is_receipt_not_pass_verdict(self):
        data, code = self.scan()
        report = parse_security_report(json.dumps(data), code)
        self.assertEqual(report.scanned_files, ("app.py", "z.py"))
        self.assertEqual(report.findings, ())
        self.assertNotIn("PASS", json.dumps(data))
        self.version.assert_called_once_with("bandit")

    def test_findings_exit_one_metadata_only_no_issue_text_or_source(self):
        self.manager.results = [self.issue()]
        data, code = self.scan()
        report = parse_security_report(json.dumps(data), code)
        self.assertEqual(code, 1)
        self.assertEqual(report.findings[0].rule_id, "B102")
        self.assertEqual(report.findings[0].to_dict()["status"], "SUSPECTED")
        self.assertNotIn("snippet", json.dumps(data))
        self.assertNotIn("secret", json.dumps(data))
        self.assertNotIn("must not execute", json.dumps(data))
        self.assertNotIn(str(self.root), json.dumps(data))

    def test_fixed_fresh_config_explicit_scope_debug_failclosed_and_ignore_nosec(self):
        self.scan()
        self.configuration.assert_called_once_with()
        args, kwargs = self.manager_type.call_args
        self.assertEqual(args[1], "file")
        self.assertEqual(kwargs, {"debug": True, "verbose": False, "quiet": True,
                                  "profile": {"include": {"B102", "B307"}, "exclude": set()}, "ignore_nosec": True})
        self.assertEqual(self.manager.discovery, ([str(self.root / "app.py"), str(self.root / "z.py")], False))

    def test_source_nosec_project_config_baseline_plugins_are_not_honored(self):
        (self.root / "app.py").write_text("exec('fixture')  # nosec\n", encoding="utf-8")
        for name in (".bandit", "pyproject.toml", "bandit.yaml", "baseline.json"):
            (self.root / name).write_text("fixture unsupported config", encoding="utf-8")
        self.manager.results = [self.issue()]
        data, code = self.scan()
        self.assertEqual(parse_security_report(json.dumps(data), code).findings[0].rule_id, "B102")
        self.configuration.assert_called_once_with()
        self.assertEqual(self.manager.discovery[0], [str(self.root / "app.py"), str(self.root / "z.py")])

    def test_selected_blacklist_rules_are_available_and_effectively_selected(self):
        self.manager.results = [self.issue(test_id="B307", test="blacklist")]
        data, code = self.scan()
        self.assertEqual(parse_security_report(json.dumps(data), code).findings[0].rule_id, "B307")

    def test_unknown_and_umbrella_rules_never_silently_clean(self):
        self.error(self.scan(host(rule_ids=["B999"])))
        self.manager_type.assert_not_called()
        self.error(self.scan(host(rule_ids=["B001"])))

    def test_unapproved_plugin_module_and_rule_identifier_mismatch_rejected(self):
        self.registry.plugins_by_id["B102"] = plugin("B102", "source_plugin")
        self.error(self.scan())
        self.manager_type.assert_not_called()
        self.registry.plugins_by_id["B102"] = plugin("B105")
        self.error(self.scan())

    def test_effective_missing_extra_and_bad_blacklist_plugins_failclosed(self):
        self.manager.b_ts.plugins = [plugin("B102")]
        self.error(self.scan())
        self.manager.b_ts.plugins = [plugin("B102"), plugin("B307"), plugin("B105")]
        self.error(self.scan())
        self.manager.b_ts.plugins = [plugin("B102"), plugin("B001", "source.blacklist", {"Call": [{"id": "B307"}]})]
        self.error(self.scan())

    def test_version_mismatch_missing_metadata_and_dependency_errors_failclosed(self):
        for version in (None, "1.8.5", "1.8.6-beta"):
            self.error(self.scan(version_provider=Mock(return_value=version)))
        self.loader.assert_not_called()
        self.error(self.scan(version_provider=Mock(side_effect=ImportError("raw dependency error"))))
        self.error(self.scan(bandit_loader=Mock(side_effect=ImportError("raw missing dependency"))))

    def test_syntax_error_is_scanner_error_not_clean(self):
        (self.root / "app.py").write_text("def fixture(:\n", encoding="utf-8")
        self.error(self.scan())

    def test_skipped_unreadable_excluded_or_partial_discovery_failclosed(self):
        for kind in ("skip", "exclude", "missing"):
            def discovery(manager, kind=kind):
                if kind == "skip":
                    manager.skipped = [(manager.files_list[0], "unreadable")]
                elif kind == "exclude":
                    manager.excluded_files = [manager.files_list[0]]
                else:
                    manager.files_list.pop()
            self.manager.discover_hook = discovery
            self.error(self.scan())
            self.manager.skipped, self.manager.excluded_files = [], []

    def test_skipped_excluded_baseline_or_partial_scan_failclosed(self):
        for kind in ("skip", "exclude", "baseline", "missing"):
            def scanning(manager, kind=kind):
                if kind == "skip":
                    manager.skipped = [(manager.files_list[0], "plugin failed")]
                elif kind == "exclude":
                    manager.excluded_files = [manager.files_list[0]]
                elif kind == "baseline":
                    manager.baseline = ["fixture baseline must not hide issues"]
                else:
                    manager.files_list.pop()
            self.manager.run_hook = scanning
            self.error(self.scan())
            self.manager.skipped, self.manager.excluded_files, self.manager.baseline = [], [], []

    def test_swallowed_plugin_logging_error_never_clean_and_handler_restored(self):
        handlers = list(logging.getLogger().handlers)
        disabled = logging.root.manager.disable
        self.manager.run_hook = lambda _manager: logging.getLogger("bandit.core.tester").error("fixture plugin failure")
        self.error(self.scan())
        self.assertEqual(logging.getLogger().handlers, handlers)
        self.assertEqual(logging.root.manager.disable, disabled)

    def test_dependency_loader_logging_error_cannot_be_ignored(self):
        def loader():
            logging.getLogger("stevedore.extension").error("fixture extension failed")
            return self.configuration, self.manager_type, self.registry
        self.error(self.scan(bandit_loader=loader))

    def test_config_manager_and_engine_exceptions_return_only_fixed_error(self):
        self.error(self.scan(bandit_loader=lambda: (Mock(side_effect=ValueError("raw config")), self.manager_type, self.registry)))
        self.error(self.scan(bandit_loader=lambda: (self.configuration, Mock(side_effect=ValueError("raw manager")), self.registry)))
        self.manager.run_hook = Mock(side_effect=RuntimeError("raw plugin traceback"))
        self.error(self.scan())

    def test_no_python_inventory_never_constructs_scanner(self):
        (self.root / "app.py").unlink()
        (self.root / "z.py").unlink()
        (self.root / "README.md").write_text("fixture", encoding="utf-8")
        self.error(self.scan())
        self.loader.assert_not_called()

    def test_unicode_nested_sorted_inventory_and_nonpython_exclusion(self):
        directory = self.root / "한글"
        directory.mkdir()
        (directory / "a.py").write_text("value = 2\n", encoding="utf-8")
        (directory / "b%20.py").write_text("value = 3\n", encoding="utf-8")
        (self.root / "ignore.pyw").write_text("fixture-not-py", encoding="utf-8")
        data, code = self.scan()
        self.assertEqual(parse_security_report(json.dumps(data), code).scanned_files,
                         ("app.py", "z.py", "한글/a.py", "한글/b%20.py"))

    def test_symlinks_and_hardlinks_are_rejected_without_scanner(self):
        (self.root / "link.py").symlink_to(self.root / "app.py")
        self.error(self.scan())
        (self.root / "link.py").unlink()
        os.link(self.root / "app.py", self.root / "hard.py")
        self.error(self.scan())
        self.loader.assert_not_called()

    def test_root_symlink_and_secret_subdirectories_are_rejected(self):
        (self.root / "secret-link").symlink_to(self.root, target_is_directory=True)
        self.error(self.scan())
        (self.root / "secret-link").unlink()
        (self.root / ".env").mkdir()
        self.error(self.scan())

    def test_source_unreadable_special_and_too_large_files_failclosed(self):
        with patch.object(security_runner.os, "open", side_effect=PermissionError("raw source path")):
            self.error(self.scan())
        (self.root / "big.txt").write_bytes(b"x" * (1024 * 1024 + 1))
        self.error(self.scan())
        (self.root / "big.txt").unlink()
        os.mkfifo(self.root / "pipe")
        self.error(self.scan())

    def test_inventory_total_size_file_count_and_directory_limits(self):
        with patch.object(security_runner, "MAX_FILES", 1):
            self.error(self.scan())
        with patch.object(security_runner, "MAX_TOTAL_BYTES", 1):
            self.error(self.scan())
        (self.root / "nested").mkdir()
        with patch.object(security_runner, "MAX_DIRECTORIES", 1):
            self.error(self.scan())

    def test_source_changed_or_removed_during_scan_rejected(self):
        self.manager.run_hook = lambda _manager: (self.root / "app.py").write_text("changed = 1\n", encoding="utf-8")
        self.error(self.scan())
        self.manager.run_hook = lambda _manager: (self.root / "app.py").unlink()
        self.error(self.scan())

    def test_non_python_source_member_change_during_scan_is_not_same_snapshot(self):
        (self.root / "config.txt").write_text("fixture original", encoding="utf-8")
        self.manager.run_hook = lambda _manager: (self.root / "config.txt").write_text("fixture changed", encoding="utf-8")
        self.error(self.scan())

    def test_out_of_inventory_or_unselected_findings_failclosed(self):
        for changes in (dict(fname="/other/private.py"), dict(fname=str(self.root / "../private.py")),
                        dict(fname="app.py"), dict(fname=str(self.root / "missing.py")), dict(test_id="B608")):
            self.manager.results = [self.issue(**changes)]
            self.error(self.scan())

    def test_invalid_finding_metadata_never_echoed(self):
        for name, values in {
            "test": (None, "Exec_used", "raw source text", "a" * 129),
            "lineno": (None, True, 0, -1, 1.0, 1024 * 1024 + 1),
            "col_offset": (None, True, -1, 1.0, 1024 * 1024 + 1),
            "severity": (None, "CRITICAL", "INFO", "medium"),
            "confidence": (None, "UNDEFINED", "high"),
        }.items():
            for value in values:
                self.manager.results = [self.issue(**{name: value})]
                self.error(self.scan())

    def test_findings_canonical_sort_duplicates_and_count_bound(self):
        self.manager.results = [self.issue(lineno=2), self.issue(lineno=1)]
        data, code = self.scan()
        self.assertEqual([finding.line for finding in parse_security_report(json.dumps(data), code).findings], [1, 2])
        self.manager.results = [self.issue(), self.issue()]
        self.error(self.scan())
        self.manager.results = [self.issue(lineno=index + 1) for index in range(1001)]
        self.error(self.scan())

    def test_host_scope_rule_config_and_nosec_overrides_rejected(self):
        for data in (host(ignore_nosec=False), host(ignore_nosec=1), host(scan_scope="CHANGED_FILES"),
                     host(exclude=["app.py"]), host(baseline="fixture"), host(config=".bandit"),
                     host(scanner="semgrep"), host(rule_ids=[])):
            self.error(self.scan(data))
        self.loader.assert_not_called()

    def test_error_counter_never_formats_or_retains_log_content(self):
        counter = security_runner._ErrorCounter()
        record = logging.LogRecord("bandit", logging.ERROR, "fixture", 1, "raw private %s", (object(),), None)
        counter.emit(record)
        self.assertEqual(counter.count, 1)
        self.assertFalse(any(value is record for value in vars(counter).values()))
        self.assertNotIn("raw private", repr(vars(counter)))

    def test_cli_arguments_rejected_with_safe_stdout_only(self):
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = security_runner.main(["--Source-override"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output.getvalue()), {"error": "SCANNER_ERROR"})
        self.assertEqual(errors.getvalue(), "")

    def test_cli_reads_only_host_json_imports_only_trusted_contract_and_suppresses_engine_output(self):
        payload, exit_code = self.scan()
        def noisy(*args, **kwargs):
            self.assertEqual(args[1], Path("/snapshot"))
            print("raw engine source output")
            os.write(1, b"raw fd output")
            os.write(2, b"raw fd exception")
            return payload, exit_code
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(sys.modules, {"_security_contract": security_contract}), \
                patch.object(Path, "open", return_value=io.BytesIO(json.dumps(host()).encode("utf-8"))), \
                patch.object(security_runner, "run_security_scan", side_effect=noisy), \
                redirect_stdout(output), redirect_stderr(errors):
            original_path = list(sys.path)
            code = security_runner.main([])
            self.assertEqual(sys.path, original_path)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), payload)
        self.assertNotIn("raw", output.getvalue())
        self.assertEqual(errors.getvalue(), "")

    def test_runner_does_not_import_source_or_install_scan_dependencies(self):
        text = Path(security_runner.__file__).read_text(encoding="utf-8")
        self.assertNotIn('sys.path.insert(0, "/snapshot")', text)
        self.assertNotIn("eval(", text)
        self.assertNotIn("exec(", text)
        self.assertNotIn("pip install", text)
        self.assertNotIn("populate_baseline(", text)
        self.assertNotIn("output_results(", text)


if __name__ == "__main__":
    unittest.main()
