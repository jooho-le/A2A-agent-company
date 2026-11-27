"""Read-only check of generated QA/Security test files before READY.

After READY the Host collects ``outputs/<role>/tests`` and runs every generated
scope with ``unittest`` discovery. Two model slips then fail the whole step
with no chance to fix them (handoff P5, expected points 4 and 6):

* no test file for a generated scope -> ``QA_TEST_INPUT_INVALID`` /
  ``SECURITY_TEST_INPUT_INVALID``;
* a test the runner reports that the draft did not declare as a case ->
  ``QA_REPORT_BINDING_INVALID`` / ``SECURITY_TEST_BINDING_INVALID``.

This module parses (never imports or runs) the files and mirrors discovery for
top-level files and inert Python packages with ``unittest.TestCase`` classes.
Non-package directories such as browser JSON never disable Python inspection.
Whenever it cannot be sure what the runner will
report (executable package initializers, ``load_tests``, unknown base classes, dynamic test
attributes, unreadable files) it reports nothing and leaves the decision to the
Host run, so it is never stricter than the real collection.

QA now also publishes statically known IDs as a Host catalog. Exhausted ID
binding corrections stop before executing an unbound draft; the Host may close
the Run as UNVERIFIED with recovery evidence. No ID is guessed or normalized.

Some test-file defects make the Host run a correct product as FAIL, or a broken
test as PASS. They are named here too (2026-11-17, Run 42f131fe and Codex's
offline review). Known defects now require a real test-file write within the
existing correction budget. An unchanged invalid draft stops as a validator
technical failure before product execution; it never becomes a product FAIL:

* ``GENERATED_TEST_SYNTAX_INVALID``: the file does not parse, so nothing runs;
* ``GENERATED_TEST_NONE``: a scope whose files show test code (a ``TestCase``
  class or a pytest-style ``def test_...`` function) but hold no test unittest
  would run. Files with no test code at all are left to the Host run;
* ``TEST_CLIENT_NOT_STARTED``: ``TestClient(app)`` made outside a ``with``. The
  frozen start code creates the tables in the app's lifespan, which only runs
  inside ``with TestClient(app) as client:``. Run 42f131fe's QA and Security
  tests did this; the correct v1 product then answered 400 (no table) and the
  QA scope reported 4 FAIL while the protected tests passed 9/9;
* ``ASYNC_TEST_IN_SYNC_CASE``: ``async def test_...`` in a plain ``TestCase``.
  unittest calls it without awaiting, so the body never runs and it PASSes;
* ``TEST_IMPORT_NAME_MISSING``: ``from <product module> import <name>`` where the
  product module certainly does not define the name (e.g. ``from main import
  get_db``). Collection then fails and the whole scope cannot run (2026-11-18).
  Only modules that exist in the product source and not in the tests folder are
  judged; wildcard or dynamic modules are left to the Host run.
"""
import ast
from pathlib import Path

from agents.runtime.generated_test_inventory import MAX_FILES, MAX_FILE_BYTES, unit_inventory, scope_root

TESTCASE_BASES = frozenset({'TestCase', 'IsolatedAsyncioTestCase', 'AppTestCase', 'ServiceTestCase'})


def _service_fixture_issues(path):
    try: tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, SyntaxError, UnicodeError): return set()
    issues = set()
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    service_classes = [node for node in classes if any(_base_name(base) == 'ServiceTestCase' for base in node.bases)]
    output_helpers = {alias.asname or alias.name for node in tree.body
                      if isinstance(node, ast.ImportFrom) and node.module == '_host_test_data'
                      for alias in node.names if alias.name == 'check_http_output'}
    if any(isinstance(node, ast.Call) and _base_name(node.func) == 'TestClient' for node in ast.walk(tree)):
        issues.add('TEST_SERVICE_FIXTURE_REQUIRED')
    if any(any(_base_name(base) == 'AppTestCase' for base in node.bases) for node in classes):
        issues.add('TEST_SERVICE_FIXTURE_REQUIRED')
    if service_classes:
        if any(isinstance(n, ast.ImportFrom) and n.module in {'main', 'database', 'models'}
               or isinstance(n, ast.Import) and any(a.name in {'main', 'database', 'models'} for a in n.names)
               for n in tree.body):
            issues.add('TEST_SERVICE_FIXTURE_REQUIRED')
        if any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in {
                'setUp', 'tearDown', 'setUpClass', 'tearDownClass', '_callSetUp', '_callTearDown', '_callCleanup', 'run'}
                for cls in service_classes for n in cls.body):
            issues.add('TEST_SERVICE_FIXTURE_REQUIRED')
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call): continue
        direct = isinstance(node.func, ast.Name) and node.func.id in output_helpers
        bound = (bool(service_classes) and isinstance(node.func, ast.Attribute)
                 and node.func.attr == 'check_http_output' and isinstance(node.func.value, ast.Name)
                 and node.func.value.id == 'self')
        if (not (direct or bound) or any(k.arg is None for k in node.keywords)
                or any(isinstance(arg, ast.Starred) for arg in node.args)): continue
        sample = next((k.value for k in node.keywords if k.arg == 'sensitive'), None)
        parameters = ('method', 'path') if bound else ('client', 'method', 'path')
        positionals = set(parameters[:len(node.args)])
        named = {k.arg for k in node.keywords if k.arg in parameters}
        empty_or_absent = isinstance(sample, ast.Constant) and (
            sample.value is None or type(sample.value) is str and sample.value == '')
        if (len(node.args) > len(parameters) or positionals & named
                or positionals | named != set(parameters) or sample is None
                or isinstance(sample, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)) and not empty_or_absent):
            issues.add('TEST_OUTPUT_ARGUMENT_INVALID')
    return issues


def _base_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _starts_test_client(tree) -> bool:
    """False when a ``TestClient(...)`` is made outside a ``with`` and nothing else enters it."""
    in_with, made, bindings = set(), [], {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Call) and _base_name(value.func) == 'TestClient':
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    bindings[ast.dump(target, include_attributes=False).replace('Store()', 'Load()')] = id(value)
    def entered(expression):
        if isinstance(expression, ast.Call) and _base_name(expression.func) == 'TestClient':
            in_with.add(id(expression))
        else:
            identity = bindings.get(ast.dump(expression, include_attributes=False))
            if identity is not None:
                in_with.add(identity)
    for node in ast.walk(tree):
        if isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                entered(item.context_expr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == '__enter__':
                entered(node.func.value)
            elif node.func.attr in ('enter_context', 'enterContext', 'enterClassContext') and node.args:
                entered(node.args[0])
        if isinstance(node, ast.Call) and _base_name(node.func) == 'TestClient':
            made.append(node)
    return all(id(call) in in_with for call in made)


def _analyze(path: Path, module=None):
    """``(ids or None when unsure, reason codes, shows test code)`` for one generated test file."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None, set(), False
        module = module or path.stem
        if not all(part.isidentifier() for part in module.split('.')):
            return None, set(), False
        text = path.read_text(encoding='utf-8')
    except (OSError, ValueError, UnicodeDecodeError):
        return None, set(), False
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return None, {'GENERATED_TEST_SYNTAX_INVALID'}, True
    codes = set() if _starts_test_client(tree) else {'TEST_CLIENT_NOT_STARTED'}
    classes = {}  # name -> (is_testcase, is_async_case, test method names incl. inherited, async test names)
    deleted = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == 'load_tests':
            return None, codes, True
        if isinstance(node, ast.Delete):
            # ``del Base`` after its subclasses: discovery no longer sees Base itself.
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in classes:
                    deleted.add(target.id)
            continue
        if not isinstance(node, ast.ClassDef):
            if any(isinstance(child, ast.ClassDef) for child in ast.walk(node)
                   if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                return None, codes, True  # classes created conditionally or in loops
            targets = node.targets if isinstance(node, ast.Assign) else \
                [node.target] if isinstance(node, (ast.AnnAssign, ast.AugAssign)) else []
            if any(isinstance(target, ast.Name) and target.id in classes for target in targets):
                return None, codes, True  # a class name rebound at module level
            continue
        is_testcase, is_async_case, methods, async_methods = False, False, set(), set()
        for base in node.bases:
            name = _base_name(base)
            if name in TESTCASE_BASES:
                is_testcase = True
                is_async_case = is_async_case or name == 'IsolatedAsyncioTestCase'
            elif name in classes and name not in deleted:
                inherited = classes[name]
                is_testcase = is_testcase or inherited[0]
                is_async_case = is_async_case or inherited[1]
                methods |= inherited[2]
                async_methods |= inherited[3]
            elif name != 'object':
                return None, codes, True  # base defined elsewhere: its tests are unknown here
        if node.keywords:
            return None, codes, True
        for item in node.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith('test'):
                methods.add(item.name)
                if isinstance(item, ast.AsyncFunctionDef):
                    async_methods.add(item.name)
                else:
                    async_methods.discard(item.name)
            elif isinstance(item, (ast.Assign, ast.AnnAssign)):
                targets = item.targets if isinstance(item, ast.Assign) else [item.target]
                if any(isinstance(target, ast.Name) and target.id.startswith('test') for target in targets):
                    return None, codes, True  # test attributes assigned dynamically
        classes[node.name] = (is_testcase, is_async_case, methods, async_methods)
        deleted.discard(node.name)
    if any(case and not async_case and async_methods
           for case, async_case, _methods, async_methods in classes.values()):
        codes.add('ASYNC_TEST_IN_SYNC_CASE')
    shows_tests = any(entry[0] for entry in classes.values()) or any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith('test') for node in tree.body)
    return {f'{module}.{name}.{method}' for name, (case, _async, methods, _a) in classes.items()
            if case and name not in deleted for method in methods}, codes, shows_tests


def _fixture_issues(path, source_root):
    if source_root is None:
        return set()
    root = Path(source_root)
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, SyntaxError, UnicodeDecodeError):
        return set()
    if all((root / name).is_file() for name in ('main.py', 'database.py', 'models.py')):
        cases = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        if any(any(_base_name(base) in {'TestCase', 'IsolatedAsyncioTestCase', 'ServiceTestCase'} for base in node.bases)
               for node in cases):
            return {'TEST_HOST_FIXTURE_REQUIRED'}
        if any(any(_base_name(base) == 'AppTestCase' for base in node.bases) for node in cases):
            if any(isinstance(node, (ast.Import, ast.ImportFrom)) and
                   (isinstance(node, ast.ImportFrom) and node.module in {'main', 'database', 'models'}
                    or isinstance(node, ast.Import) and any(a.name in {'main', 'database', 'models'} for a in node.names))
                   for node in tree.body):
                return {'TEST_HOST_FIXTURE_REQUIRED'}
            if any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in
                   {'setUp', 'tearDown', 'setUpClass', 'tearDownClass', '_callSetUp', '_callTearDown', 'run'}
                   for case in cases for node in case.body):
                return {'TEST_HOST_FIXTURE_REQUIRED'}
    return set()


def _fixture_api_issues(path):
    from mcp_tools.tools.unit_runner import app_fixture_source_issues
    try:
        if path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return set()
        return {item['reasonCode'] for item in app_fixture_source_issues(path.read_text(encoding='utf-8'))}
    except (OSError, ValueError, UnicodeDecodeError):
        return set()


def _import_issues(path: Path, source_root, tests_root: Path) -> set:
    """``TEST_IMPORT_NAME_MISSING`` when a product-module import certainly fails at collection."""
    from agents.runtime.start_precheck import _bindings, _module_file, _parse
    from mcp_tools.tools.unit_runner import (TEST_DATA_MODULE, TEST_DATA_EXPORTS, APP_FIXTURE_MODULE,
        APP_FIXTURE_EXPORTS, SERVICE_FIXTURE_MODULE, SERVICE_FIXTURE_EXPORTS)
    if source_root is None:
        return set()
    try:
        tree = ast.parse(path.read_text(encoding='utf-8'))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return set()
    root = Path(source_root)
    for node in tree.body:
        if not isinstance(node, ast.ImportFrom) or node.level or not node.module or '.' in node.module:
            continue
        if node.module == TEST_DATA_MODULE:
            if any(alias.name not in (*TEST_DATA_EXPORTS, '*') for alias in node.names):
                return {'TEST_IMPORT_NAME_MISSING'}
            continue  # the Host runner preloads this module before product/test paths
        if node.module == APP_FIXTURE_MODULE:
            if any(alias.name not in (*APP_FIXTURE_EXPORTS, '*') for alias in node.names):
                return {'TEST_IMPORT_NAME_MISSING'}
            continue
        if node.module == SERVICE_FIXTURE_MODULE:
            if any(alias.name not in (*SERVICE_FIXTURE_EXPORTS, '*') for alias in node.names):
                return {'TEST_IMPORT_NAME_MISSING'}
            continue
        if _module_file(tests_root, node.module) is not None:
            continue  # a helper next to the tests shadows the product module
        module = _module_file(root, node.module)
        if module is None:
            continue
        try:
            parsed, ok = _parse(module)
        except OSError:
            continue
        if parsed is None or not ok:
            continue
        bound, dynamic = _bindings(parsed)
        if dynamic:
            continue
        for alias in node.names:
            if alias.name != '*' and alias.name not in bound and _module_file(root, node.module + '.' + alias.name) is None:
                return {'TEST_IMPORT_NAME_MISSING'}
    return set()


def test_ids(path: Path, module=None) -> set[str] | None:
    """``module.Class.method`` ids unittest would report for this file, or None when unsure."""
    return _analyze(path, module)[0]


def inspect_generated_tests(tests_dir, scopes, rows, field, source_root=None, *, host_app_fixture=False, service_fixture=False) -> list[dict]:
    """Closed issues for generated scopes ``[(selector, pattern)]`` against declared case rows."""
    scopes = [(s[0], s[1], s[2] if len(s) == 3 else 'tests') for s in scopes
              if len(s) in (2, 3) and isinstance(s[0], str) and isinstance(s[1], str)]
    if not scopes:
        return []
    root = Path(tests_dir)
    if not root.is_absolute():
        return []
    if not root.is_dir():
        return [{'fieldPath': '/' + field, 'reasonCode': 'GENERATED_TEST_FILE_MISSING'}]
    issues = []
    for selector, pattern, directory in scopes:
        selected_root = scope_root(root, directory)
        if not selected_root.is_dir():
            issues.append({'fieldPath': '/' + field, 'reasonCode': 'GENERATED_TEST_FILE_MISSING'})
            continue
        inventory = unit_inventory(selected_root, pattern)
        if not inventory.complete:
            continue  # dynamic packages remain authoritative only at isolated collection
        if not inventory.files:
            issues.append({'fieldPath': '/' + field, 'reasonCode': 'GENERATED_TEST_FILE_MISSING'})
            continue
        found, codes, shows_tests = set(), set(), False
        for path, module in inventory.files:
            ids, file_codes, file_shows_tests = _analyze(path, module)
            codes |= file_codes | _import_issues(path, source_root, selected_root)
            if host_app_fixture:
                codes |= _fixture_issues(path, source_root) | _fixture_api_issues(path)
            if service_fixture and 'GENERATED_TEST_SYNTAX_INVALID' not in file_codes:
                codes |= _service_fixture_issues(path)
            shows_tests = shows_tests or file_shows_tests
            found = None if found is None or ids is None else found | ids
        if service_fixture and 'TEST_SERVICE_FIXTURE_REQUIRED' in codes:
            codes.discard('TEST_CLIENT_NOT_STARTED')
        for code in sorted(codes):
            if {'fieldPath': '/' + field, 'reasonCode': code} not in issues:
                issues.append({'fieldPath': '/' + field, 'reasonCode': code})
        if found is None:
            continue
        if not found:
            if shows_tests:
                issues.append({'fieldPath': '/' + field, 'reasonCode': 'GENERATED_TEST_NONE'})
            continue
        declared = {row.get('testId') for row in rows if isinstance(row, dict) and row.get('selector') == selector}
        if found - declared:
            issues.append({'fieldPath': '/' + field, 'reasonCode': 'TEST_IN_FILE_NOT_DECLARED'})
        if declared - found:
            issues.append({'fieldPath': '/' + field, 'reasonCode': 'DECLARED_TEST_NOT_IN_FILE'})
    return issues


def inspect_generated_browser_tests(output_root, suites, rows, field) -> list[dict]:
    """Inspect every configured generated browser suite before accepting READY.

    Finalization runs all configured suites, including a suite absent from the
    model's case declarations. Match that obligation here, without running
    product code, relaxing coverage or exposing test contents in corrections.
    """
    from pathlib import PurePosixPath
    from mcp_tools.tools.browser_contract import BrowserContractError, parse_browser_suite

    root = Path(output_root)
    if not root.is_absolute():
        return []
    issues = []

    def add(code):
        item = {"fieldPath": "/" + field, "reasonCode": code}
        if item not in issues:
            issues.append(item)

    for selector, suite_path in suites:
        relative = PurePosixPath(suite_path)
        if (relative.is_absolute() or not relative.parts or relative.parts[0] != "tests"
                or any(part in (".", "..") for part in relative.parts) or "\\" in suite_path):
            add("BROWSER_SUITE_PATH_INVALID")
            continue
        target = root
        unsafe = root.is_symlink()
        for part in relative.parts:
            target = target / part
            unsafe = unsafe or target.is_symlink()
        if unsafe:
            add("BROWSER_SUITE_PATH_INVALID")
            continue
        if not target.is_file():
            add("GENERATED_TEST_FILE_MISSING")
            continue
        try:
            with target.open("rb") as stream:
                raw = stream.read(MAX_FILE_BYTES + 1)
            if len(raw) > MAX_FILE_BYTES:
                raise ValueError
            suite = parse_browser_suite(raw.decode("utf-8"), selector)
        except (OSError, UnicodeError, ValueError, BrowserContractError):
            add("BROWSER_SUITE_INVALID")
            continue
        actual = {case["testId"] for case in suite["tests"]}
        declared = {row.get("testId") for row in rows if isinstance(row, dict)
                    and row.get("toolName") == "run_browser_tests" and row.get("selector") == selector}
        if actual - declared:
            add("TEST_IN_FILE_NOT_DECLARED")
        if declared - actual:
            add("DECLARED_TEST_NOT_IN_FILE")
    return issues
