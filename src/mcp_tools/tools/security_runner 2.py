"""Trusted standalone Container-only Bandit AST runner.

Import is inert. The fixed CLI imports only a Host-shipped policy contract from
/inputs; it never imports/executes Source, its config, nosec or baseline. Bandit
and approved installed plugins must already exist in the frozen image.
"""

from contextlib import contextmanager
import hashlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import re
import stat
import sys


MAX_BYTES = 1024 * 1024
MAX_FILES = 1000
MAX_DIRECTORIES = 4096
MAX_TOTAL_BYTES = 16 * 1024 * 1024
MAX_FINDINGS = 1000
_TEST_NAME = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_RANKS = frozenset({"LOW", "MEDIUM", "HIGH"})


class _RunnerError(Exception):
    pass


class _ErrorCounter(logging.Handler):
    """Count engine errors without formatting or retaining a raw log message."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.count = 0

    def emit(self, record):
        if record.levelno >= logging.ERROR:
            self.count += 1


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _inventory(source_root, path_validator):
    """No-follow bounded inventory, including checks on non-Python members."""
    root = Path(source_root)
    root_info = root.lstat()
    if not root.is_absolute() or not stat.S_ISDIR(root_info.st_mode):
        raise _RunnerError()
    rows, pending, directories, files, total = {}, [root], 0, 0, 0
    while pending:
        directory = pending.pop()
        before = directory.lstat()
        if not stat.S_ISDIR(before.st_mode):
            raise _RunnerError()
        directories += 1
        if directories > MAX_DIRECTORIES:
            raise _RunnerError()
        with os.scandir(directory) as entries:
            children = []
            for entry in entries:
                children.append(entry)
                if len(children) > MAX_FILES + MAX_DIRECTORIES:
                    raise _RunnerError()
            children.sort(key=lambda entry: entry.name)
        for entry in children:
            path = directory / entry.name
            relative = path_validator(path.relative_to(root).as_posix())
            info = entry.stat(follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                pending.append(path)
            elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                files += 1
                total += info.st_size
                if files > MAX_FILES or not 0 <= info.st_size <= MAX_BYTES or total > MAX_TOTAL_BYTES:
                    raise _RunnerError()
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    if _identity(os.fstat(descriptor)) != _identity(info):
                        raise _RunnerError()
                    digest = hashlib.sha256()
                    actual_size = 0
                    while True:
                        chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - actual_size))
                        if not chunk:
                            break
                        actual_size += len(chunk)
                        if actual_size > MAX_BYTES:
                            raise _RunnerError()
                        digest.update(chunk)
                    if actual_size != info.st_size or _identity(os.fstat(descriptor)) != _identity(info):
                        raise _RunnerError()
                finally:
                    os.close(descriptor)
                rows[relative] = (str(path), _identity(info), digest.hexdigest())
            else:
                raise _RunnerError()
        if _identity(directory.lstat()) != _identity(before):
            raise _RunnerError()
    if not rows:
        raise _RunnerError()
    return dict(sorted(rows.items()))


def _load_bandit():
    # Explicit runtime-only approved dependency imports. Never prepend Source
    # or use Bandit's CLI project/config discovery and external formatters.
    from bandit.core.config import BanditConfig
    from bandit.core.manager import BanditManager
    from bandit.core.extension_loader import MANAGER
    return BanditConfig, BanditManager, MANAGER


def _available_rules(extension_manager, selected):
    plugins = extension_manager.plugins_by_id
    blacklists = extension_manager.blacklist_by_id
    if not isinstance(plugins, dict) or not isinstance(blacklists, dict):
        raise _RunnerError()
    for rule in selected:
        if rule in plugins:
            plugin = plugins[rule].plugin
            if (plugin._test_id != rule or type(plugin.__module__) is not str
                    or not plugin.__module__.startswith("bandit.plugins.")):
                raise _RunnerError()
        elif rule not in blacklists or blacklists[rule]["id"] != rule:
            raise _RunnerError()


def _effective_rules(manager):
    """Check the concrete detectors selected by Bandit's internal test set."""
    rules = set()
    for extension in manager.b_ts.plugins:
        plugin = extension.plugin
        if plugin._test_id == "B001":
            if plugin.__module__ != "bandit.core.blacklisting" or type(plugin._config) is not dict:
                raise _RunnerError()
            for entries in plugin._config.values():
                for entry in entries:
                    rules.add(entry["id"])
        else:
            rules.add(plugin._test_id)
    return rules


def _findings(results, inventory, selected, source_root, contract):
    if type(results) is not list or len(results) > MAX_FINDINGS:
        raise _RunnerError()
    output = []
    for issue in results:
        absolute = Path(issue.fname)
        if not absolute.is_absolute():
            raise _RunnerError()
        path = contract._source_path(absolute.relative_to(source_root).as_posix())
        rule = contract._rule(issue.test_id)
        name, line, column = issue.test, issue.lineno, issue.col_offset
        if (path not in inventory or rule not in selected or type(name) is not str
                or _TEST_NAME.fullmatch(name) is None or type(line) is not int or not 1 <= line <= MAX_BYTES
                or type(column) is not int or not 0 <= column <= MAX_BYTES
                or type(issue.severity) is not str or issue.severity not in _RANKS
                or type(issue.confidence) is not str or issue.confidence not in _RANKS):
            raise _RunnerError()
        output.append({"ruleId": rule, "testName": name, "path": path, "line": line, "column": column,
                       "severity": issue.severity, "confidence": issue.confidence, "status": "SUSPECTED"})
    def key(row):
        return (row["path"], row["line"], row["column"], row["ruleId"], row["testName"], row["severity"], row["confidence"])
    output.sort(key=key)
    if len({key(row) for row in output}) != len(output):
        raise _RunnerError()
    return output


def run_security_scan(host_payload, source_root, *, contract, bandit_loader=None, version_provider=None):
    """Explicit AST-only scan, injectable trusted fakes for tool verification.

    A successful receipt exits0 if empty or1 if suspected findings exist. Any
    dependency, profile, syntax, plugin, unreadable, partial inventory or engine
    failure exits2 SCANNER_ERROR. An empty finding list is not a PASS verdict.
    """
    counter = _ErrorCounter()
    root_logger = logging.getLogger()
    previous_disable = logging.root.manager.disable
    root_logger.addHandler(counter)
    logging.disable(logging.NOTSET)
    try:
        host = contract.validate_security_host_payload(host_payload)
        source_inventory = _inventory(source_root, contract._source_path)
        inventory = {path: value for path, value in source_inventory.items() if path.endswith(".py")}
        if not inventory:
            raise _RunnerError()
        actual_version = (version_provider or importlib.metadata.version)("bandit")
        if actual_version != host["scanner_version"]:
            raise _RunnerError()
        configuration_type, manager_type, extension_manager = (bandit_loader or _load_bandit)()
        selected = set(host["rule_ids"])
        _available_rules(extension_manager, selected)
        # A fresh default configuration cannot honor .bandit, pyproject.toml,
        # exclusion, baseline or nosec settings from the generated Source.
        configuration = configuration_type()
        manager = manager_type(configuration, "file", debug=True, verbose=False, quiet=True,
                               profile={"include": selected, "exclude": set()}, ignore_nosec=True)
        if _effective_rules(manager) != selected:
            raise _RunnerError()
        targets = [row[0] for row in inventory.values()]
        manager.discover_files(targets, recursive=False)
        if (list(manager.files_list) != targets or manager.excluded_files or manager.get_skipped()
                or manager.baseline or not manager.ignore_nosec or counter.count):
            raise _RunnerError()
        manager.run_tests()
        if (list(manager.files_list) != targets or manager.excluded_files or manager.get_skipped()
                or manager.baseline or counter.count or _inventory(source_root, contract._source_path) != source_inventory):
            raise _RunnerError()
        findings = _findings(manager.results, inventory, selected, Path(source_root), contract)
        report = {"format": "bandit-v1", "profileName": host["profile_name"], "scanner": "bandit",
                  "scannerVersion": actual_version, "ruleIds": list(host["rule_ids"]), "profileRef": host["profile_ref"],
                  "scannedFiles": list(inventory), "findings": findings}
        contract.canonical_json(report, max_bytes=MAX_BYTES)
        return report, 1 if findings else 0
    except BaseException:
        return {"error": "SCANNER_ERROR"}, 2
    finally:
        root_logger.removeHandler(counter)
        counter.close()
        logging.disable(previous_disable)


@contextmanager
def _discard_output():
    original_stdout, original_stderr = sys.stdout, sys.stderr
    saved_stdout = saved_stderr = devnull = stream = None
    try:
        saved_stdout = os.dup(1)
        saved_stderr = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        stream = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stderr = stream
        yield
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
        if stream is not None:
            stream.close()
        if saved_stdout is not None:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)
        if saved_stderr is not None:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        if devnull is not None:
            os.close(devnull)


def main(argv=None):
    try:
        if argv is not None and argv or argv is None and len(sys.argv) != 1:
            raise _RunnerError()
        with _discard_output():
            sys.path.insert(0, "/inputs")
            try:
                import _security_contract as contract
            finally:
                sys.path.remove("/inputs")
            with Path("/inputs/_security_host.json").open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise _RunnerError()
            host = contract.parse_json(raw.decode("utf-8"))
            payload, exit_code = run_security_scan(host, Path("/snapshot"), contract=contract)
    except BaseException:
        payload, exit_code = {"error": "SCANNER_ERROR"}, 2
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
