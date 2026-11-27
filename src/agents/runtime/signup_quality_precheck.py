"""Public-contract source review before READY, never a product verdict.

Only local import-graph ASTs are read. This does not execute generated code,
repair it automatically, disclose protected cases, or prove safety by absence
of warnings. Unknown/custom implementations still require measured validation.
"""
import ast
from hashlib import sha256
from pathlib import Path

from agents.runtime.output_safety_precheck import inspect_output_safety
from agents.runtime import email_normalization
from orchestrator.core.security import redact_text

POLICY = 'SIGNUP_PUBLIC_CONTRACT_SOURCE_REVIEW_V6'
MAX_FILES, MAX_BYTES, MAX_TOTAL_BYTES, MAX_NODES = 64, 128_000, 1_048_576, 50_000


def _name(node):
    return node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else None


def _local_file(root, module):
    if not module or not all(p.isidentifier() for p in module.split('.')):
        return None
    base = root
    for part in module.split('.'):
        base /= part
        if base.is_symlink():
            return None
    for candidate in (base.with_suffix('.py'), base / '__init__.py'):
        if not candidate.is_symlink() and candidate.is_file():
            return candidate
    return None


def _import_module(module, path, node):
    if not node.level:
        return node.module or ''
    package = module.split('.') if path.name == '__init__.py' else module.split('.')[:-1]
    if node.level > len(package):
        return ''
    return '.'.join([*package[:len(package) - node.level + 1],
                     *(() if not node.module else node.module.split('.'))])


def _modules(root, entry):
    """Read reachable local imports, capped in files, bytes and AST nodes."""
    pending, result, total, nodes = [entry], {}, 0, 0
    while pending and len(result) < MAX_FILES:
        module = pending.pop(0)
        if module in result:
            continue
        path = _local_file(root, module)
        if path is None:
            continue
        try:
            with path.open('rb') as stream:
                raw = stream.read(MAX_BYTES + 1)
            total += len(raw)
            if len(raw) > MAX_BYTES or total > MAX_TOTAL_BYTES:
                break
            tree = ast.parse(raw)
            nodes += sum(1 for _ in ast.walk(tree))
            if nodes > MAX_NODES:
                break
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            continue
        result[module] = (path, tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = _import_module(module, path, node)
                names = [base, *(base + '.' + a.name for a in node.names if a.name != '*')]
            else:
                continue
            for name in names:
                if name not in result and name not in pending and len(pending) < MAX_FILES:
                    if _local_file(root, name) is not None:
                        pending.append(name)
    return result


def _imported(tree, module, symbol):
    aliases = {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
               and n.module == module and not n.level for a in n.names if a.name == symbol}
    # A shadowed imported name is not evidence about the library API.
    if any(isinstance(n, ast.ImportFrom) and any(a.name == '*' for a in n.names) for n in tree.body):
        return set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            aliases.discard(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            for target in targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                        aliases.discard(name.id)
    return aliases


def _status(call):
    if not isinstance(call, ast.Call):
        return None
    status = next((k.value for k in call.keywords if k.arg == 'status_code'),
                  call.args[0] if call.args else None)
    if isinstance(status, ast.Constant) and type(status.value) is int:
        return status.value
    if isinstance(status, ast.Attribute) and status.attr.startswith('HTTP_'):
        part = status.attr.split('_')[1]
        return int(part) if part.isdigit() else None
    return None


def _raw_exception(node, variable):
    """Direct exception rendering; filtered comprehensions/helpers are unknown."""
    if isinstance(node, ast.Name):
        return node.id == variable
    if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
            and node.value.id == variable and node.attr in ('body', 'args')):
        return True
    if isinstance(node, (ast.Dict, ast.List, ast.Tuple, ast.JoinedStr, ast.FormattedValue, ast.BinOp)):
        return any(_raw_exception(child, variable) for child in ast.iter_child_nodes(node))
    if isinstance(node, ast.Call):
        return (_name(node.func) in ('str', 'repr', 'format', 'jsonable_encoder')
                and any(_raw_exception(arg, variable) for arg in node.args)
                or isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                and node.func.value.id == variable and node.func.attr == 'errors')
    return False


def _response_values(call):
    values = [k.value for k in call.keywords if k.arg in ('detail', 'content')]
    if _name(call.func) == 'JSONResponse' and call.args:
        values.append(call.args[0])
    if _name(call.func) == 'HTTPException' and len(call.args) > 1:
        values.append(call.args[1])
    return values


def _request_body_parameter(fn, model_names):
    """Known FastAPI body bindings, including a dictionary rejected before the endpoint.

    RequestValidationError can contain the entire malformed body even when a
    password field uses SecretStr. Catching HTTPException/Exception inside the
    endpoint does not handle that framework response. Unknown custom dependency
    or parameter markers remain a measured-validation concern.
    """
    positional = (*fn.args.posonlyargs, *fn.args.args)
    defaults = dict(zip((a.arg for a in positional[-len(fn.args.defaults):]), fn.args.defaults)) if fn.args.defaults else {}
    defaults.update((a.arg, value) for a, value in zip(fn.args.kwonlyargs, fn.args.kw_defaults) if value is not None)
    for argument in (*positional, *fn.args.kwonlyargs):
        default, annotation = defaults.get(argument.arg), argument.annotation
        if isinstance(default, ast.Call):
            if _name(default.func) == 'Body':
                return True
            # Depends/Query/etc. do not establish a request-body binding.
            continue
        if isinstance(annotation, ast.Subscript):
            annotation = annotation.value
        if _name(annotation) in model_names | {'dict', 'Dict', 'list', 'List'}:
            return True
    return False


def inspect_signup_quality(source_root, contract):
    if (not isinstance(contract, dict) or contract.get('scenarioKey') != 'SCN-001'
            or source_root is None):
        return []
    root = Path(source_root)
    if not root.is_absolute() or root.is_symlink():
        return []
    app = contract.get('externalContract', {}).get('app', {})
    if app.get('framework') != 'FastAPI':
        return []
    entry = app.get('module')
    if not isinstance(entry, str):
        return []
    modules = _modules(root, entry)
    keys = {r.get('key') for r in contract.get('requirements', ())}
    issues = list(inspect_output_safety(root, contract))
    if 'REQ-003' in keys:
        trees = {path.relative_to(root).as_posix(): tree for path, tree in modules.values()}
        for site in email_normalization.scan(trees, contract):
            if site['trimBeforeValidationObserved'] is False and len(issues) < 16:
                path = site['toolPath'].removeprefix('source/')
                issues.append({'fieldPath': '/source', 'reasonCode': email_normalization.REASON,
                    'requirementKey': 'REQ-003', 'sourcePath': path, 'repairSourcePath': path,
                    'lineNumber': site['line'], 'reviewRevision': site['reviewRevision'],
                    'emailNormalizationContext': email_normalization.context(site)})
    validation_context = None

    def add(code, path, node, key, *, repair_path=None):
        ref = path.relative_to(root).as_posix()
        if redact_text(ref) != ref:
            return
        row = {'fieldPath': '/source', 'reasonCode': code, 'requirementKey': key,
               'sourcePath': ref, 'lineNumber': node.lineno}
        repair_path = repair_path or path
        row['repairSourcePath'] = repair_path.relative_to(root).as_posix()
        # Host-derived revision, not a model assertion. A changed reviewed file
        # may use the remaining correction allowance; the total cap never grows.
        digest = sha256()
        for reviewed in dict.fromkeys((path, repair_path)):
            digest.update(reviewed.relative_to(root).as_posix().encode())
            digest.update(ast.dump(next(t for p, t in modules.values() if p == reviewed)).encode())
        row['reviewRevision'] = digest.hexdigest()
        if code in ('DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS', 'VALIDATION_ERRORS_FORWARD_INPUT') and validation_context is not None:
            row['validationErrorContext'] = dict(validation_context)
        if row not in issues and len(issues) < 16:
            issues.append(row)

    # Exception handlers in one file must not disable scanning the other files
    # or the DB/log paths. Only a validation handler or unknown middleware may
    # suppress the default-validation hint; forwarding raw errors is reviewed below.
    entry_tree = modules.get(entry, (None, None))[1]
    validation_handler, middleware = False, False
    simple_app = False
    app_constructors = []
    http_handler_lines = []
    if entry_tree is not None:
        factories = _imported(entry_tree, 'fastapi', 'FastAPI')
        app_constructors = [n.value for n in entry_tree.body
                            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                            and _name(n.value.func) in factories
                            and any(isinstance(t, ast.Name) and t.id == app.get('attribute') for t in n.targets)]
        simple_app = bool(app_constructors)
        validation_errors = {'RequestValidationError'} | _imported(entry_tree, 'fastapi.exceptions', 'RequestValidationError')
        http_errors = {'HTTPException'}
        for origin in ('fastapi', 'fastapi.exceptions', 'starlette.exceptions'):
            http_errors.update(_imported(entry_tree, origin, 'HTTPException'))
        for n in ast.walk(entry_tree):
            if isinstance(n, ast.Call):
                on_app = (isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                          and n.func.value.id == app.get('attribute'))
                if on_app and _name(n.func) in ('middleware', 'add_middleware'):
                    middleware = True
                if on_app and _name(n.func) in ('exception_handler', 'add_exception_handler') and n.args:
                    if _name(n.args[0]) in validation_errors:
                        validation_handler = True
                    if _name(n.args[0]) in http_errors:
                        http_handler_lines.append(n.lineno)
                if n in app_constructors:
                    for keyword in n.keywords:
                        if keyword.arg != 'exception_handlers':
                            continue
                        if isinstance(keyword.value, ast.Dict):
                            for exception in keyword.value.keys:
                                if _name(exception) in validation_errors:
                                    validation_handler = True
                                elif _name(exception) in http_errors:
                                    http_handler_lines.append(n.lineno)
                                elif exception is None or not isinstance(exception, ast.Constant):
                                    middleware = True  # unknown mapping: leave to measured validation
                        elif not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
                            middleware = True
                if n in app_constructors and any(k.arg == 'middleware' for k in n.keywords):
                    middleware = True
                if any(isinstance(a, ast.Name) and a.id == app.get('attribute') for a in n.args):
                    middleware = True  # unknown external installation, not proof of missing protection
            if (isinstance(n, ast.Attribute) and n.attr == 'exception_handlers'
                    and isinstance(n.value, ast.Name) and n.value.id == app.get('attribute')):
                middleware = True  # custom installation cannot be proved absent
        validation_context = {'applicationAttribute': app.get('attribute'),
                              'httpExceptionHandlerDetected': bool(http_handler_lines),
                              'requestValidationHandlerDetected': validation_handler,
                              'httpExceptionHandlerLines': sorted(set(http_handler_lines))[:8]}

    request_models = set()
    for module, (_path, tree) in modules.items():
        bases = _imported(tree, 'pydantic', 'BaseModel')
        for n in tree.body:
            if (isinstance(n, ast.ClassDef) and any(_name(b) in bases for b in n.bases)
                    and any(isinstance(f, ast.AnnAssign) and _name(f.target) == 'password' for f in n.body)):
                request_models.add((module, n.name))

    for module, (path, tree) in modules.items():
        conflict_types = {'IntegrityError', 'DBAPIError', 'DatabaseError', 'SQLAlchemyError', 'Exception'}
        for exception in tuple(conflict_types - {'Exception'}):
            conflict_types.update(_imported(tree, 'sqlalchemy.exc', exception))
        model_names = {name for m, name in request_models if m == module}
        for n in tree.body:
            if isinstance(n, ast.ImportFrom):
                origin = _import_module(module, path, n)
                model_names.update(a.asname or a.name for a in n.names if (origin, a.name) in request_models)
        log_modules = {a.asname or a.name for n in tree.body if isinstance(n, ast.Import)
                       for a in n.names if a.name == 'logging'}
        log_calls = _imported(tree, 'logging', 'error') | _imported(tree, 'logging', 'exception')
        logger_factories = _imported(tree, 'logging', 'getLogger')
        loggers = set(log_modules)
        for n in tree.body:
            if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                    and (isinstance(n.value.func, ast.Name) and n.value.func.id in logger_factories
                         or isinstance(n.value.func, ast.Attribute) and n.value.func.attr == 'getLogger'
                         and isinstance(n.value.func.value, ast.Name) and n.value.func.value.id in log_modules)):
                loggers.update(t.id for t in n.targets if isinstance(t, ast.Name))
        for fn in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
            is_signup = any(isinstance(d, ast.Call) and _name(d.func) == 'post' and d.args
                            and isinstance(d.args[0], ast.Constant) and isinstance(d.args[0].value, str)
                            and contract.get('externalContract', {}).get('signupApi', {}).get('path', '').endswith(d.args[0].value)
                            for d in fn.decorator_list)
            if (is_signup and 'REQ-006' in keys and simple_app and not validation_handler and not middleware
                    and _request_body_parameter(fn, model_names)):
                issues[:] = [r for r in issues if not (r['reasonCode'] == 'DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS'
                                                       and 'sourcePath' not in r)]
                # The handler belongs to the application entrypoint. Asking a
                # router module to import main.app can create a circular import.
                add('DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS', path, fn, 'REQ-006',
                    repair_path=modules[entry][0])
            if is_signup and 'REQ-003' in keys:
                for trial in (n for n in ast.walk(fn) if isinstance(n, ast.Try)):
                    commits = any(isinstance(n, ast.Call) and _name(n.func) == 'commit'
                                  for s in trial.body for n in ast.walk(s))
                    has_rejection = any(any(_name(t) in conflict_types
                                            for t in ast.walk(h.type))
                                        and any(_status(n) is not None and 400 <= _status(n) < 500
                                                for n in ast.walk(h) if isinstance(n, ast.Call))
                                        for h in trial.handlers if h.type is not None)
                    if commits and not has_rejection:
                        for h in trial.handlers:
                            if (_name(h.type) == 'Exception' and any(_status(n) == 500 for n in ast.walk(h)
                                                                    if isinstance(n, ast.Call))):
                                add('COMMIT_CONFLICT_MAPPED_TO_SERVER_ERROR', path, h, 'REQ-003')
            if 'REQ-006' not in keys:
                continue
            if is_signup:
                parents = {child: node for node in ast.walk(fn) for child in ast.iter_child_nodes(node)}
                integrity_names = {'IntegrityError'} | _imported(tree, 'sqlalchemy.exc', 'IntegrityError')
                broad_names = {'Exception', 'BaseException', 'SQLAlchemyError', 'DBAPIError'}
                for name in ('SQLAlchemyError', 'DBAPIError'):
                    broad_names.update(_imported(tree, 'sqlalchemy.exc', name))
                for trial in (n for n in ast.walk(fn) if isinstance(n, ast.Try)):
                    commits = [n for statement in trial.body for n in ast.walk(statement)
                               if isinstance(n, ast.Call) and _name(n.func) == 'commit']
                    handlers = {_name(t) for h in trial.handlers if h.type is not None for t in ast.walk(h.type)
                                if isinstance(t, (ast.Name, ast.Attribute))}
                    enclosing_catch = False
                    child = trial
                    while child in parents:
                        parent = parents[child]
                        if isinstance(parent, ast.Try) and child in parent.body:
                            names = {_name(t) for h in parent.handlers if h.type is not None for t in ast.walk(h.type)}
                            if names & broad_names or any(h.type is None for h in parent.handlers):
                                enclosing_catch = True
                                break
                        child = parent
                    # Exact known pattern: commit is protected only against uniqueness failures.
                    # Unknown handler types are left to measured validation, not guessed safe/unsafe.
                    if (commits and handlers and handlers <= integrity_names and not enclosing_catch
                            and not any(h.type is None for h in trial.handlers)
                            and not handlers & broad_names):
                        add('DB_ERROR_PATH_NOT_HANDLED', path, commits[0], 'REQ-006')
            for handler in (n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler) and n.name):
                for call in (n for n in ast.walk(handler) if isinstance(n, ast.Call)):
                    logging_call = (isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
                                    and call.func.value.id in loggers and call.func.attr in ('error', 'exception', 'warning', 'critical'))
                    logging_call |= isinstance(call.func, ast.Name) and call.func.id in log_calls
                    trace = _name(call.func) == 'exception' or any(k.arg == 'exc_info' and not
                        (isinstance(k.value, ast.Constant) and k.value.value is False) for k in call.keywords)
                    # Merely registering a filter does not prove exc_info, cached exc_text,
                    # exception arguments or SQL bind values were scrubbed.
                    if logging_call and (trace or any(_raw_exception(a, handler.name) for a in call.args)):
                        add('EXCEPTION_LOG_MAY_EXPOSE_DB_PARAMETERS', path, call, 'REQ-006')
                    if _name(call.func) in ('HTTPException', 'JSONResponse') and any(
                            _raw_exception(value, handler.name) for value in _response_values(call)):
                        add('RAW_EXCEPTION_IN_ERROR_RESPONSE', path, call, 'REQ-006')
            # Validation handlers use function arguments rather than except variables.
            if any(isinstance(d, ast.Call) and _name(d.func) in ('exception_handler', 'add_exception_handler')
                   for d in fn.decorator_list):
                variables = {a.arg for a in fn.args.args}
                for call in (n for n in ast.walk(fn) if isinstance(n, ast.Call) and _name(n.func) == 'JSONResponse'):
                    if any(_raw_exception(value, v) for value in _response_values(call) for v in variables):
                        add('VALIDATION_ERRORS_FORWARD_INPUT', path, call, 'REQ-006')
    return issues


def signup_quality_precheck_for(checkpoint, contract):
    return lambda: inspect_signup_quality(getattr(checkpoint, 'source_root', None), contract)
