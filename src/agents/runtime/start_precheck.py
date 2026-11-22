"""Read-only start-contract precheck of a Developer workspace before READY.

Live Run 4f7ec809 (2026-11-17) spent two of its three fix attempts on start
failures (``main.py`` without ``app``, then an entrypoint import failure) that
the model could only learn about from the Host build after READY. This precheck
reads the workspace source (never imports or runs it) and reports only problems
that make the Host build probe fail for certain:

* a Python file that does not compile (the probe runs ``compileall``),
* ``externalContract.app.module`` without a top-level ``app`` binding,
* ``externalContract.database.module`` without the dependency binding,
* ``externalContract.userModel.module`` without the model class binding,
* a model class whose only base is ``Base`` while ``primary_key`` appears nowhere
  in the model and database modules (SQLAlchemy cannot map it);
* no route anywhere for the contract's ready check (``externalContract.app.readyCheck``,
  e.g. ``GET /``): live Run 1825a436 v1 and v2 rewrote ``main.py`` without the
  starting code's ``GET /`` and both builds failed (2026-11-18);
* ``Depends("...")`` with a string on a route function: FastAPI cannot inspect a
  string while registering the route, so importing the app fails (Run 1825a436 v1);
* an unconditional exception-handler decorator on a standard APIRouter in the
  pinned FastAPI startup module or one of its direct imports;
* no route whose path could serve ``externalContract.signupApi`` (e.g. ``POST
  /api/auth/signup``): the gpt-4.1 diagnostic v1 (2026-11-18) wrote only models.py.
  Any route path that is a suffix of the contract path (a router prefix may supply
  the rest), a computed path, a mount or added route makes this rule pass.

It is never stricter than the probe: wildcard imports, ``globals()``/``setattr``
at module level, other base classes, mixins or any ``primary_key`` mention make
the related rule pass. The Host build stays the authority; a refused or failed
precheck simply lets the existing build/fix path decide.
"""
import ast
from collections.abc import Mapping
import importlib.util
import os
from pathlib import Path
import re

MAX_FILES = 400
# Recorded in the Host policy for both Single and Multi.
START_PRECHECK_POLICY = 'STATIC_START_PRECHECK_V5'
_ROUTE_METHODS = frozenset({'get', 'post', 'put', 'delete', 'patch', 'head', 'options', 'api_route', 'route'})
_READY_CHECK = re.compile(r'GET\s+(/\S*)')
MAX_FILE_BYTES = 1_048_576
SKIP_DIRECTORIES = frozenset({'.git', '__pycache__', '.venv', 'node_modules'})


def _module_file(root: Path, module: str) -> Path | None:
    if not isinstance(module, str) or not module or any(not part.isidentifier() for part in module.split('.')):
        return None
    base = root.joinpath(*module.split('.'))
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if candidate.is_file() and not candidate.is_symlink():
            return candidate
    return None


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _top_level(statements):
    """Module-scope statements, including those nested in if/try/with/for/while/match blocks."""
    stack = list(reversed(statements))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, _SCOPES):
            continue
        stack.extend(reversed([child for child in ast.iter_child_nodes(node)
                               if isinstance(child, (ast.stmt, ast.excepthandler, ast.match_case))]))


def _names(target):
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for item in target.elts:
            yield from _names(item)
    elif isinstance(target, ast.Starred):
        yield from _names(target.value)


def _bindings(tree) -> tuple[dict, bool]:
    """Top-level name -> defining node, and whether bindings can be dynamic."""
    bound, dynamic = {}, False
    for node in _top_level(tree.body):
        if isinstance(node, (ast.excepthandler, ast.match_case)):
            if isinstance(node, ast.excepthandler) and node.name:
                bound[node.name] = node
            else:
                dynamic = True  # match patterns can bind names; do not judge such modules
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound[node.name] = node
            if node.name == '__getattr__':
                dynamic = True  # PEP 562: attributes can be produced on access
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for name in _names(target):
                    bound[name] = node
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            bound[node.target.id] = node
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound[alias.asname or alias.name.split('.')[0]] = node
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == '*':
                    dynamic = True
                else:
                    bound[alias.asname or alias.name] = node
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.With, ast.AsyncWith)):
            for target in ([node.target] if isinstance(node, (ast.For, ast.AsyncFor))
                           else [item.optional_vars for item in node.items if item.optional_vars is not None]):
                for name in _names(target):
                    bound[name] = node
        for child in ast.walk(node) if not isinstance(node, _SCOPES) else ():
            if isinstance(child, ast.NamedExpr) and isinstance(child.target, ast.Name):
                bound[child.target.id] = node
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in (
                    'globals', 'setattr', 'exec', 'vars', 'locals'):
                dynamic = True
    for node in ast.walk(tree):
        if isinstance(node, ast.Global):  # a function may create the module name when called
            for name in node.names:
                bound.setdefault(name, node)
    return bound, dynamic


def _source_files(root: Path):
    count = 0
    for directory, subdirectories, files in os.walk(root, followlinks=False):
        subdirectories[:] = sorted(name for name in subdirectories if name not in SKIP_DIRECTORIES)
        for name in sorted(files):
            if not name.endswith('.py'):
                continue
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            count += 1
            if count > MAX_FILES:
                return
            yield path


def _parse(path: Path):
    """(tree, compiled_ok). Mirrors compileall: decode by the file's coding cookie, then compile."""
    data = path.read_bytes()
    if len(data) > MAX_FILE_BYTES:
        return None, True  # too large to judge here; let the Host build decide
    try:
        text = importlib.util.decode_source(data)
        compile(text, str(path), 'exec', dont_inherit=True)
        return ast.parse(text), True
    except (SyntaxError, ValueError, UnicodeDecodeError):
        return None, False


def _ready_route_missing(trees, ready_path) -> bool:
    """True only when no route is declared for the ready path and nothing can serve it dynamically.

    Routes are decorator calls such as ``@app.get("/")`` or ``app.add_api_route("/", ...)``.
    A computed path, a mount or another dynamic registration makes this return False.
    """
    def path_of(call):
        value = call.args[0] if call.args else next(
            (keyword.value for keyword in call.keywords if keyword.arg == 'path'), None)
        return value.value if isinstance(value, ast.Constant) and isinstance(value.value, str) else None

    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for item in node.decorator_list:
                    if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                            and item.func.attr in _ROUTE_METHODS):
                        path = path_of(item)
                        if path is None or path in (ready_path, ready_path.rstrip('/')):
                            return False
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ('mount', 'add_route', 'add_websocket_route', 'include_router'):
                    return False  # routers with prefixes or mounts: not judged statically
                if node.func.attr == 'add_api_route':
                    path = path_of(node)
                    if path is None or path in (ready_path, ready_path.rstrip('/')):
                        return False
    return True


def _signup_route_missing(trees, path, method) -> bool:
    """True only when no declared route could serve the contract's signup method and path."""
    wanted = {method.lower(), 'api_route', 'route'}
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for item in node.decorator_list:
                    if (isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute)
                            and item.func.attr in _ROUTE_METHODS):
                        value = item.args[0] if item.args else next(
                            (k.value for k in item.keywords if k.arg == 'path'), None)
                        if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                            return False
                        route = value.value
                        if item.func.attr in wanted and (route == path or route in ('', '/') or (
                                route.startswith('/') and path.endswith(route))):
                            return False
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr in ('mount', 'add_route', 'add_api_route', 'add_websocket_route')):
                return False
    return True


def _string_depends(trees):
    """(path, function) of route functions whose default is ``Depends("...")``."""
    found = []
    for path, tree in trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or not any(
                    isinstance(item, ast.Call) and isinstance(item.func, ast.Attribute) and item.func.attr in _ROUTE_METHODS
                    for item in node.decorator_list):
                continue
            for default in [*node.args.defaults, *[value for value in node.args.kw_defaults if value is not None]]:
                if (isinstance(default, ast.Call)
                        and (getattr(default.func, 'id', None) == 'Depends' or getattr(default.func, 'attr', None) == 'Depends')
                        and default.args and isinstance(default.args[0], ast.Constant)
                        and isinstance(default.args[0].value, str)):
                    found.append(path)
    return found


def _standard_router_before(statements, receiver):
    """Resolve a direct, unmodified APIRouter binding before an unconditional decorator."""
    prefix = ast.Module(body=statements, type_ignores=[])
    bound, dynamic = _bindings(prefix)
    assignment = bound.get(receiver)
    if (dynamic or not isinstance(assignment, ast.Assign) or assignment not in statements
            or len(assignment.targets) != 1 or not isinstance(assignment.targets[0], ast.Name)
            or not isinstance(assignment.value, ast.Call) or not isinstance(assignment.value.func, ast.Name)):
        return False
    constructor = assignment.value.func
    imported = bound.get(constructor.id)
    if (not isinstance(imported, ast.ImportFrom) or imported not in statements
            or imported.lineno >= assignment.lineno or imported.level
            or imported.module not in ('fastapi', 'fastapi.routing')
            or not any(a.name == 'APIRouter' and (a.asname or a.name) == constructor.id for a in imported.names)):
        return False
    # Allow normal route declarations. A receiver/constructor passed to a helper,
    # aliased, mutated or otherwise rebound makes its interface uncertain.
    allowed = {assignment.targets[0], constructor}
    for node in statements:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr in _ROUTE_METHODS):
                    allowed.add(decorator.func.value)
    return not any(isinstance(n, ast.Name) and n.id in (receiver, constructor.id) and n not in allowed
                   for n in ast.walk(prefix))


def _returns_errors_in_json_content(handler):
    """Describe a direct return expression. Only an executed Security check can establish disclosure."""
    args = [*handler.args.posonlyargs, *handler.args.args]
    if len(args) != 2:
        return False
    for statement in handler.body:
        if not isinstance(statement, ast.Return) or not isinstance(statement.value, ast.Call):
            continue
        call = statement.value
        if not isinstance(call.func, ast.Name) or call.func.id != 'JSONResponse':
            continue
        for kw in call.keywords:
            if kw.arg != 'content':
                continue
            values = kw.value.values if isinstance(kw.value, ast.Dict) else [kw.value]
            for value in values:
                if (isinstance(value, ast.Call) and not value.args and not value.keywords
                        and isinstance(value.func, ast.Attribute) and value.func.attr == 'errors'
                        and isinstance(value.func.value, ast.Name) and value.func.value.id == args[1].arg):
                    return True
    return False


def _router_handler_issues(root, trees, entry):
    """Limit this framework-specific rule to the pinned image and startup imports we can prove."""
    if entry not in trees or (root / 'fastapi.py').exists() or (root / 'fastapi').exists():
        return []
    paths = {entry}
    for node in trees[entry].body:
        if isinstance(node, ast.ImportFrom) and node.level == 0:
            paths.add(_module_file(root, node.module))
        elif isinstance(node, ast.Import):
            paths.update(_module_file(root, alias.name) for alias in node.names)
    candidates = []
    for path in sorted(p for p in paths if p in trees):
        body = trees[path].body
        for index, node in enumerate(body):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in node.decorator_list:
                if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == 'exception_handler' and isinstance(decorator.func.value, ast.Name)
                        and _standard_router_before(body[:index], decorator.func.value.id)):
                    issue = {'fieldPath': path.relative_to(root).as_posix(),
                             'reasonCode': 'APIROUTER_EXCEPTION_HANDLER_UNAVAILABLE',
                             'lineNumber': decorator.func.lineno,
                             'sourceExpression': decorator.func.value.id + '.exception_handler'}
                    if _returns_errors_in_json_content(node):
                        issue['handlerReturnShape'] = 'ERRORS_CALL_IN_JSONRESPONSE_CONTENT'
                    candidates.append(issue)
    if not candidates:
        return []
    lock = root / 'requirements.lock'
    if lock.is_symlink() or not lock.is_file() or lock.stat().st_size > MAX_FILE_BYTES:
        return []
    if not re.search(rb'^fastapi==0\.142\.2(?:[ \t]+--hash=|[ \t]*\r?$)', lock.read_bytes(), re.MULTILINE):
        return []
    # A project-local extension can install the attribute on the router class.
    for tree in trees.values():
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr == 'exception_handler'
                    and isinstance(node.ctx, (ast.Store, ast.Del))):
                return []
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'setattr'
                    and len(node.args) >= 2 and (not isinstance(node.args[1], ast.Constant)
                                                 or node.args[1].value == 'exception_handler')):
                return []
    return candidates


def start_precheck_for(checkpoint, scenario_contract):
    """A no-argument precheck bound to one Developer workspace, or None when it cannot apply."""
    root = getattr(checkpoint, 'source_root', None)
    external = scenario_contract.get('externalContract') if isinstance(scenario_contract, Mapping) else None
    if root is None or not isinstance(external, Mapping):
        return None
    external = {key: dict(value) if isinstance(value, Mapping) else value for key, value in external.items()}
    return lambda: inspect(root, external)


def inspect(source_root, external_contract) -> list[dict]:
    """Closed issues ({fieldPath, reasonCode}) for certain start failures; [] when nothing is certain."""
    root = Path(source_root)
    if not root.is_absolute() or not root.is_dir() or not isinstance(external_contract, dict):
        return []
    issues, trees = [], {}
    for path in _source_files(root):
        tree, ok = _parse(path)
        relative = path.relative_to(root).as_posix()
        if not ok:
            issues.append({'fieldPath': relative, 'reasonCode': 'SOURCE_SYNTAX_INVALID'})
        elif tree is not None:
            trees[path] = tree
    if issues:
        return issues  # the probe stops at compileall; nothing else is reached

    def binding(section, key):
        spec = external_contract.get(section)
        if not isinstance(spec, dict):
            return None
        module, name = spec.get('module'), spec.get(key)
        if not isinstance(name, str) or not name.isidentifier():
            return None
        path = _module_file(root, module)
        if path is None:
            return module, name, None, None, False
        tree = trees.get(path)
        if tree is None:
            return None
        bound, dynamic = _bindings(tree)
        return module, name, path, bound.get(name), dynamic

    for section, key, code in (('app', 'attribute', 'APP_ATTRIBUTE_MISSING'),
                               ('database', 'dependency', 'DB_DEPENDENCY_MISSING'),
                               ('userModel', 'class', 'USER_MODEL_MISSING')):
        found = binding(section, key)
        if found is None:
            continue
        module, name, path, node, dynamic = found
        if path is None:
            # Missing module file: the probe's import fails for certain.
            issues.append({'fieldPath': module.replace('.', '/') + '.py', 'reasonCode': code})
        elif node is None and not dynamic:
            issues.append({'fieldPath': path.relative_to(root).as_posix(), 'reasonCode': code})
        elif (isinstance(node, ast.ImportFrom) and not node.level and node.module == module
              and not dynamic and node in trees[path].body):
            # An import binding alone does not create app. In the failed frozen
            # Source, main.py imported main.app before defining it. Never import
            # the generated module on the Host to find this out.
            before = ast.Module(body=trees[path].body[:trees[path].body.index(node)], type_ignores=[])
            previous, uncertain = _bindings(before)
            imported = next((a.name for a in node.names if (a.asname or a.name) == name), None)
            if imported and imported not in previous and not uncertain:
                issues.append({'fieldPath': path.relative_to(root).as_posix(),
                               'reasonCode': 'ENTRYPOINT_SELF_IMPORT', 'sourcePath': path.relative_to(root).as_posix(),
                               'lineNumber': node.lineno})
    # Keep going: report every certain problem at once so one correction can fix them all
    # (live Run 8b49bebd v1 had both main.py without app and a User without a primary key).

    model = binding('userModel', 'class')
    database = binding('database', 'dependency')
    if model is not None and model[2] is not None and isinstance(model[3], ast.ClassDef):
        node = model[3]
        texts = [model[2].read_text(encoding='utf-8', errors='replace')]
        if database is not None and database[2] is not None:
            texts.append(database[2].read_text(encoding='utf-8', errors='replace'))
        # Base must come from the contract's database module or be defined in the model module;
        # a Base imported from anywhere else may carry its own primary key.
        base_origin = _bindings(trees[model[2]])[0].get('Base')
        database_module = (external_contract.get('database') or {}).get('module')
        known_base = (isinstance(base_origin, ast.ClassDef) or isinstance(base_origin, ast.ImportFrom)
                      and base_origin.level == 0 and base_origin.module == database_module)
        only_base = (known_base and len(node.bases) == 1 and isinstance(node.bases[0], ast.Name)
                     and node.bases[0].id == 'Base' and not node.keywords)
        mapped_elsewhere = any(word in text for text in texts
                               for word in ('primary_key', 'PrimaryKeyConstraint', '__table__', '__mapper_args__',
                                            '__abstract__'))
        if only_base and not mapped_elsewhere:
            issues.append({'fieldPath': model[2].relative_to(root).as_posix(), 'reasonCode': 'MODEL_PRIMARY_KEY_MISSING'})
    for path in dict.fromkeys(_string_depends(trees)):
        issues.append({'fieldPath': path.relative_to(root).as_posix(), 'reasonCode': 'DEPENDS_TARGET_INVALID'})
    app = external_contract.get('app')
    ready = _READY_CHECK.match(app.get('readyCheck', '')) if isinstance(app, dict) and isinstance(
        app.get('readyCheck'), str) else None
    app_found = binding('app', 'attribute')
    if app_found and app.get('framework') == 'FastAPI':
        issues.extend(_router_handler_issues(root, trees, app_found[2]))
    # Without the app the probe already fails at import; the ready route is then not reached.
    app_missing = any(issue['reasonCode'] == 'APP_ATTRIBUTE_MISSING' for issue in issues)
    if (ready is not None and trees and not app_missing and app_found is not None and app_found[2] is not None
            and _ready_route_missing(trees, ready.group(1))):
        issues.append({'fieldPath': app_found[2].relative_to(root).as_posix(), 'reasonCode': 'READY_ROUTE_MISSING'})
    signup = external_contract.get('signupApi')
    if (isinstance(signup, dict) and isinstance(signup.get('path'), str) and signup['path'].startswith('/')
            and isinstance(signup.get('method'), str) and signup['method'].isalpha()
            and trees and not app_missing and app_found is not None and app_found[2] is not None
            and _signup_route_missing(trees, signup['path'], signup['method'])):
        issues.append({'fieldPath': app_found[2].relative_to(root).as_posix(), 'reasonCode': 'SIGNUP_ROUTE_MISSING'})
    return issues
