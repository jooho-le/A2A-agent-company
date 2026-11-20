"""Shared inert SCN-001 public build probe and bounded diagnostic decoder.

The probe runs only in a Host-approved Container. Importing this module does not
execute product code. Diagnostics never copy arbitrary stdout/stderr or secrets.
"""

import ast
from hashlib import sha256
import json
import re

from orchestrator.core.security import redact_text
from orchestrator.domain.developer_artifacts import BuildDiagnostic

# Only the frozen public connection contract, never hidden grading cases.
# Executed in the existing network-denied build Container, never on the Host.
# The signup route is probed over HTTP, not by scanning app.routes: FastAPI
# 0.142 keeps an included router as one _IncludedRouter entry without a path, so
# a route scan rejects correct apps that use APIRouter/include_router. 404/405
# for POST /api/auth/signup (any body) means the contract path is not served.
SCN001_BUILD_PROBE = '''import compileall, json, sys
from contextlib import ExitStack, closing
from inspect import isgenerator
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import Session
ok = compileall.compile_dir('/snapshot', quiet=2)
if not ok:
    print('SCN001_SOURCE_SYNTAX_INVALID')
    sys.exit(1)
sys.path.insert(0, '/snapshot')
try:
    import main, database, models
    assert isinstance(getattr(main, 'app', None), FastAPI), 'SCN001_MAIN_APP_REQUIRED'
    assert callable(getattr(database, 'get_db', None)), 'SCN001_DATABASE_DEPENDENCY_REQUIRED'
    assert getattr(models, 'User', None) is not None, 'SCN001_USER_MODEL_REQUIRED'
    with TestClient(main.app, raise_server_exceptions=False) as client:
        assert 200 <= client.get('/').status_code < 300, 'SCN001_READY_ENDPOINT_REQUIRED'
        # Inspect the product's actual synchronous dependency after startup.
        # Never create tables or insert accounts on the application's behalf.
        with ExitStack() as resources:
            dependency = database.get_db()
            if isgenerator(dependency):
                resources.enter_context(closing(dependency))
                session = next(dependency, None)
            else:
                session = dependency
                if isinstance(session, Session):
                    resources.enter_context(session)
            assert isinstance(session, Session), 'SCN001_DATABASE_SESSION_REQUIRED'
            table = sa_inspect(models.User).local_table
            connection = session.connection(bind_arguments={'mapper': models.User})
            assert sa_inspect(connection).has_table(table.name, schema=table.schema), 'SCN001_USER_TABLE_REQUIRED'
        assert client.post('/api/auth/signup', json={}).status_code not in (404, 405), 'SCN001_SIGNUP_ROUTE_REQUIRED'
except BaseException as error:
    codes = {'SCN001_MAIN_APP_REQUIRED', 'SCN001_DATABASE_DEPENDENCY_REQUIRED', 'SCN001_USER_MODEL_REQUIRED', 'SCN001_SIGNUP_ROUTE_REQUIRED', 'SCN001_READY_ENDPOINT_REQUIRED', 'SCN001_DATABASE_SESSION_REQUIRED', 'SCN001_USER_TABLE_REQUIRED'}
    code = str(error) if type(error) is AssertionError and str(error) in codes else 'SCN001_ENTRYPOINT_START_FAILED'
    if code == 'SCN001_ENTRYPOINT_START_FAILED':
        try:
            import sqlalchemy.exc as sa_exc
            if isinstance(error, sa_exc.ArgumentError) and 'primary key' in str(error):
                code = 'SCN001_MODEL_PRIMARY_KEY_REQUIRED'
            elif isinstance(error, sa_exc.SQLAlchemyError):
                code = 'SCN001_MODEL_DEFINITION_INVALID'
        except Exception:
            pass
    if isinstance(error, ModuleNotFoundError):
        code = 'SCN001_DEPENDENCY_IMPORT_FAILED'
    elif isinstance(error, ImportError):
        code = 'SCN001_IMPORT_FAILED'
    elif isinstance(error, NameError):
        code = 'SCN001_NAME_UNDEFINED'
    elif type(error) is AttributeError:
        code = 'SCN001_ATTRIBUTE_UNAVAILABLE'
    elif code == 'SCN001_ENTRYPOINT_START_FAILED' and isinstance(error, TypeError):
        code = 'SCN001_ARGUMENT_TYPE_INVALID'
    elif code == 'SCN001_ENTRYPOINT_START_FAILED' and isinstance(error, ValueError):
        code = 'SCN001_ARGUMENT_VALUE_INVALID'
    frame = error.__traceback__
    location = None
    while frame is not None:
        filename = frame.tb_frame.f_code.co_filename
        if filename.startswith('/snapshot/'):
            location = [filename[len('/snapshot/'):], frame.tb_lineno]
            if (frame.tb_next is None and type(error) in (NameError, AttributeError) and type(error.name) is str
                    and error.name.isascii() and error.name.isidentifier() and len(error.name) <= 64):
                location.append(error.name)
        frame = frame.tb_next
    if location is not None:
        print('SCN001_LOCATION:' + json.dumps(location))
    print(code)
    sys.exit(1)
print('SCN001_START_CONTRACT_OK')
'''


def scn001_build_argv(python_executable="/usr/local/bin/python"):
    # argv fields disallow control characters; repr retains literal escaped newlines.
    return [python_executable, '-I', '-X', 'pycache_prefix=/work/pycache', '-c', f'exec({SCN001_BUILD_PROBE!r})']


_DIAGNOSTICS = {
    "SCN001_MAIN_APP_REQUIRED": ("main.py", "main.py must export app as a FastAPI instance; an APIRouter alone does not satisfy the approved main:app entrypoint."),
    "SCN001_DATABASE_DEPENDENCY_REQUIRED": ("database.py", "database.py must export the callable get_db dependency required by the approved database interface."),
    "SCN001_DATABASE_SESSION_REQUIRED": ("database.py", "The original synchronous database.get_db dependency must yield or return a SQLAlchemy Session for the application database. Preserve its session cleanup and DATABASE_URL connection instead of replacing it with a test-only dependency."),
    "SCN001_USER_MODEL_REQUIRED": ("models.py", "models.py must export User as required by the approved user-model interface."),
    "SCN001_USER_TABLE_REQUIRED": ("main.py", "After the application's startup/lifespan and readiness check, the table mapped by models.User is absent from the database used by the original get_db. Preserve or restore the starting application's table initialization with the registered models and the same DATABASE_URL connection. The Host only inspected the database; do not move product table creation into QA or Security tests."),
    "SCN001_SIGNUP_ROUTE_REQUIRED": ("main.py", "POST /api/auth/signup returned 404 or 405 from main.app. Serve exactly this path with POST: define it on app or on a router included once, and add the /api/auth prefix only once (router prefix, include prefix and route path combine)."),
    "SCN001_READY_ENDPOINT_REQUIRED": ("main.py", "The app must return a 2xx status from GET / after its startup/lifespan completes."),
    "SCN001_MODEL_PRIMARY_KEY_REQUIRED": ("models.py", "A SQLAlchemy model has no primary key column, so its mapper cannot be configured and the app cannot start. Give the table a primary key column, for example id = Column(Integer, primary_key=True), and keep email UNIQUE."),
    "SCN001_MODEL_DEFINITION_INVALID": ("models.py", "SQLAlchemy rejected a model or table definition while the app was imported or started. Check the reported Source location for column, table, relationship or mapper configuration errors."),
    "SCN001_ENTRYPOINT_START_FAILED": ("main.py", "Importing or starting the approved main:app entrypoint failed; verify imports, startup and the declared database interface."),
    "SCN001_DEPENDENCY_IMPORT_FAILED": ("main.py", "A module required by the application is unavailable (ModuleNotFoundError). Check the import at the reported Source location against requirements.lock and local files; use installed packages without changing the frozen lock or environment."),
    "SCN001_IMPORT_FAILED": ("main.py", "An application import failed (ImportError). Check the reported Source location for unsupported symbols, incompatible imports or a circular dependency."),
    "SCN001_NAME_UNDEFINED": ("main.py", "Application import or startup stopped with NameError."),
    "SCN001_ATTRIBUTE_UNAVAILABLE": ("main.py", "Application import or startup stopped with AttributeError."),
    "SCN001_ARGUMENT_TYPE_INVALID": ("main.py", "Importing or starting the app raised TypeError. Check the call or expression at the reported Source location against the installed library interface: argument names, required parameters and value types. An enum parameter requires the library's enum member, not a string spelling its name."),
    "SCN001_ARGUMENT_VALUE_INVALID": ("main.py", "Importing or starting the app raised ValueError. Check initialization values at the reported Source location against the installed library's accepted ranges and choices. Do not change the frozen environment or claim the build passed."),
    "SCN001_SOURCE_SYNTAX_INVALID": ("source", "The frozen Source contains a Python syntax error; resolve it before startup and validation."),
}


def _name_context(content, line, name, *, attribute=False):
    """Only disclose a unique dotted identifier present on the failing source line."""
    if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,63}', name)
            or redact_text(name) != name or len(content) > 1_048_576):
        return {}
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    # Keep the outermost attribute chain. Calls, subscripts, arguments and literals never cross.
    nodes = list(ast.walk(tree))
    inner = {node.value for node in nodes if isinstance(node, ast.Attribute)}
    expressions = set()
    for node in nodes:
        if attribute:
            # Match the failing member itself, including intermediate members of longer chains.
            if not isinstance(node, ast.Attribute) or node.attr != name or node.end_lineno != line:
                continue
        elif node in inner or not isinstance(node, (ast.Name, ast.Attribute)):
            continue
        parts = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if attribute and not isinstance(node, ast.Name):
            return {}
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and (attribute or (node.lineno == line and node.id == name)):
            expressions.add('.'.join([node.id, *reversed(parts)]))
    if not expressions:
        return {}
    result = {} if attribute else {'missingName': name}
    if len(expressions) == 1:
        expression, = expressions
        if len(expression) <= 192 and expression.isascii() and redact_text(expression) == expression:
            result['sourceExpression'] = expression
    return result


def scn001_build_diagnostics(*, argv, exit_code, stdout, source_files=None):
    """Decode only the matching Host probe's final fixed failure marker.

    Call only after the private receipt, Source and execution profile are verified.
    Other commands or unrecognized output keep the existing unknown diagnostic.
    """
    if (type(exit_code) is not int or exit_code != 1 or type(stdout) is not str
            or not isinstance(argv, (tuple, list)) or not argv):
        return ()
    expected = scn001_build_argv(argv[0])
    current = list(argv) == expected
    if list(argv[:-1]) != expected[:-1] or not isinstance(argv[-1], str):
        return ()
    legacy = sha256(argv[-1].encode()).hexdigest()
    name_probe = legacy == '7e88ae49c3fe33eec062adcfa4fa9ac9ed9bcbf15b420fd41bc267b0c6589aad'
    if not (current or name_probe or legacy == '5d870c48e01623b8d80b61a55727e764fdfd9c940ac72fd3d2cc01564e2c865f'):
        return ()
    lines = stdout.splitlines()
    code = lines[-1].strip() if lines else ""
    attribute = code == 'SCN001_ATTRIBUTE_UNAVAILABLE'
    if code not in _DIAGNOSTICS or (attribute and not current):
        return ()
    location, message = _DIAGNOSTICS[code]
    context = {}
    # Paths/line numbers must belong to the authenticated frozen Source. Never
    # publish exception messages, source lines, host paths or unknown JSON fields.
    if source_files is not None and len(lines) >= 2 and lines[-2].startswith('SCN001_LOCATION:'):
        try:
            row = json.loads(lines[-2][len('SCN001_LOCATION:'):])
            named = type(row) is list and len(row) == 3 and (
                attribute or (code == 'SCN001_NAME_UNDEFINED' and (current or name_probe)))
            if (type(row) is list and (len(row) == 2 or named) and type(row[0]) is str
                    and type(row[1]) is int and row[0] in source_files
                    and 1 <= row[1] <= len(source_files[row[0]].splitlines())):
                location = f'{row[0]}:{row[1]}'
                if named:
                    context = _name_context(source_files[row[0]], row[1], row[2], attribute=attribute)
        except (ValueError, TypeError, RecursionError):
            pass
    return (BuildDiagnostic(ruleId=code, normalizedLocation=location, message=message, **context),)


