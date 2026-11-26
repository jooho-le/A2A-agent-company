"""Service startup/encoding probe: shipped as fixed Python inside the Sandbox.

This module never executes Source on the Host. Build PASS is readiness and
interface compatibility, not functional/security acceptance.
"""
import json
import inspect
import ast

from orchestrator.domain.developer_artifacts import BuildDiagnostic
from orchestrator.domain.service_design import ServiceDesign

SERVICE_START_POLICY = "SERVICE_START_AND_SHARED_INTERFACE_V3"


def service_start_failure_code(error, current_code):
    """Return closed causes only; never publish exception text or values."""
    if isinstance(error, ModuleNotFoundError):
        return 'SERVICE_DEPENDENCY_IMPORT_FAILED'
    if current_code != 'SERVICE_START_FAILED':
        return current_code
    try:
        from sqlalchemy.exc import ArgumentError, SQLAlchemyError
        if isinstance(error, ArgumentError) and 'could not assemble any primary key columns' in ' '.join(str(error).split()):
            return 'SERVICE_MODEL_PRIMARY_KEY_REQUIRED'
        if isinstance(error, SQLAlchemyError):
            return 'SERVICE_MODEL_DEFINITION_INVALID'
    except ImportError:
        pass
    for kind, code in ((ImportError, 'SERVICE_IMPORT_FAILED'), (NameError, 'SERVICE_NAME_UNDEFINED'),
                       (TypeError, 'SERVICE_ARGUMENT_TYPE_INVALID'), (ValueError, 'SERVICE_ARGUMENT_VALUE_INVALID')):
        if isinstance(error, kind):
            return code
    return current_code


def service_interface_failure(schema, design, ready_status, content_type):
    """Pure checks over measured readiness/OpenAPI, also shipped in the probe."""
    if not 200 <= ready_status < 300:
        return 'SERVICE_READY_FAILED'
    if design['delivery'] == 'WEB' and 'text/html' not in content_type:
        return 'SERVICE_HTML_REQUIRED'
    for endpoint in design['endpoints']:
        if (endpoint['method'] == 'GET' and endpoint['path'] == '/'
                and endpoint['responseFormat'] == 'JSON'
                and not (content_type.split(';', 1)[0].strip().lower() == 'application/json'
                         or content_type.split(';', 1)[0].strip().lower().endswith('+json'))):
            return 'SERVICE_JSON_REQUIRED'
        operation = schema.get('paths', {}).get(endpoint['path'], {}).get(endpoint['method'].lower())
        if not isinstance(operation, dict):
            return 'SERVICE_ENDPOINT_MISSING'
        body = operation.get('requestBody', {}).get('content', {})
        wanted = {'JSON': ('application/json',), 'FORM': ('application/x-www-form-urlencoded', 'multipart/form-data')}.get(endpoint['bodyFormat'], ())
        # Request.json() and custom body parsers may omit OpenAPI body metadata.
        # Unknown is left to functional QA; explicit contradictory metadata fails.
        if wanted and body:
            media = next((body[value] for value in wanted if value in body), None)
            if media is None:
                return 'SERVICE_REQUEST_ENCODING_MISMATCH'
            model = media.get('schema', {})
            reference = model.get('$ref')
            if isinstance(reference, str) and reference.startswith('#/components/schemas/'):
                model = schema.get('components', {}).get('schemas', {}).get(reference.split('/')[-1], {})
            if isinstance(model.get('properties'), dict) and not set(endpoint['requestFields']) <= set(model['properties']):
                return 'SERVICE_REQUEST_FIELDS_MISMATCH'
    return None


def service_validation_requests(schema, design):
    """Known required JSON bodies with an explicit 400 rejection policy."""
    targets = []
    for endpoint in design['endpoints']:
        statuses = endpoint['rejectionStatuses']
        if (endpoint['bodyFormat'] != 'JSON' or endpoint['method'] not in ('POST', 'PUT', 'PATCH')
                or '{' in endpoint['path'] or 400 not in statuses or 422 in statuses):
            continue
        operation = schema.get('paths', {}).get(endpoint['path'], {}).get(endpoint['method'].lower(), {})
        model = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {})
        ref = model.get('$ref')
        if isinstance(ref, str) and ref.startswith('#/components/schemas/'):
            model = schema.get('components', {}).get('schemas', {}).get(ref.split('/')[-1], {})
        if isinstance(model.get('required'), list) and model['required']:
            targets.append((endpoint['method'], endpoint['path'], tuple(statuses)))
    return targets


_PROBE = r'''
import ast, compileall, importlib, json, os, sys
from pathlib import Path
design = json.loads(sys.argv[1])
code, location, line, missing = 'SERVICE_START_FAILED', 'main.py', 1, None
saved = [os.dup(1), os.dup(2)]
streams = (sys.stdout, sys.stderr)
sink = open(os.devnull, 'w')
sys.stdout = sys.stderr = sink
null = os.open(os.devnull, os.O_WRONLY)
os.dup2(null, 1); os.dup2(null, 2)
try:
    if not compileall.compile_dir('/snapshot', quiet=2):
        code = 'SERVICE_SYNTAX_INVALID'
        raise ValueError()
    sys.path.insert(0, '/snapshot')
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    if design['storage'] == 'SQLITE':
        os.environ['DATABASE_URL'] = 'sqlite:////work/service-start.sqlite3'
    module = importlib.import_module('main')
    app = getattr(module, 'app', None)
    if not isinstance(app, FastAPI):
        code = 'SERVICE_APP_REQUIRED'
        raise ValueError()
    with TestClient(app, raise_server_exceptions=False) as client:
        if design['storage'] == 'SQLITE':
            code = 'SERVICE_DATABASE_UNAVAILABLE'
            from contextlib import ExitStack, closing
            from inspect import isgenerator
            import sqlite3
            from sqlalchemy import text
            from sqlalchemy.orm import Session
            database = importlib.import_module('database')
            with ExitStack() as stack:
                dependency = database.get_db()
                if isgenerator(dependency):
                    stack.enter_context(closing(dependency))
                    connection = next(dependency)
                else:
                    connection = stack.enter_context(closing(dependency))
                if isinstance(connection, Session): connection.execute(text('SELECT 1'))
                elif isinstance(connection, sqlite3.Connection): connection.execute('SELECT 1')
                else: raise ValueError()
            code = 'SERVICE_START_FAILED'
        response = client.get('/')
        problem = service_interface_failure(app.openapi(), design, response.status_code, response.headers.get('content-type', ''))
        if problem:
            code = problem
            raise ValueError()
        for method, path, accepted in service_validation_requests(app.openapi(), design):
            rejection = client.request(method, path, json={})
            if rejection.status_code not in accepted:
                code = 'SERVICE_INPUT_REJECTION_MISMATCH'
                raise ValueError()
    code = 'PASS'
except BaseException as error:
    code = service_start_failure_code(error, code)
    if isinstance(error, ModuleNotFoundError):
        name = getattr(error, 'name', '')
        if isinstance(name, str) and name.isidentifier() and len(name) <= 64:
            # Only a public import present in authenticated source may cross.
            known = set()
            for path in Path('/snapshot').glob('*.py'):
                try:
                    for node in ast.walk(ast.parse(path.read_bytes())):
                        if isinstance(node, ast.Import): known.update(a.name.split('.')[0] for a in node.names)
                        elif isinstance(node, ast.ImportFrom) and node.module: known.add(node.module.split('.')[0])
                except BaseException: pass
            if name in known: missing = name
    tb = error.__traceback__
    while tb:
        path = Path(tb.tb_frame.f_code.co_filename)
        try:
            relative = path.relative_to('/snapshot')
            if path.is_file() and not path.is_symlink(): location, line = relative.as_posix(), tb.tb_lineno
        except ValueError: pass
        tb = tb.tb_next
finally:
    os.dup2(saved[0], 1); os.dup2(saved[1], 2)
    for fd in (*saved, null): os.close(fd)
    sys.stdout, sys.stderr = streams
    sink.close()
print('SERVICE_START:' + json.dumps({'code': code, 'path': location, 'line': line, 'missing': missing}, separators=(',', ':')))
raise SystemExit(0 if code == 'PASS' else 1)
'''

_MESSAGES = {
    "SERVICE_START_FAILED": "Start the actual main.app and its lifespan in the approved runtime. Read the reported source and preserve its initialization; test code must not repair the product.",
    "SERVICE_SYNTAX_INVALID": "Repair the Python syntax before submitting the Source.",
    "SERVICE_APP_REQUIRED": "Expose a FastAPI application as main.app.",
    "SERVICE_READY_FAILED": "After application startup, GET / must return a successful readiness response.",
    "SERVICE_HTML_REQUIRED": "The agreed WEB service must return usable HTML at GET /.",
    "SERVICE_JSON_REQUIRED": "GET / is agreed as JSON in serviceDesign but returns another format. Return an API readiness JSON response on the real main.app. Do not add an HTML signup form for an API-only request or change QA to accept HTML.",
    "SERVICE_ENDPOINT_MISSING": "Implement every method/path in the shared serviceDesign; do not change QA to target another route.",
    "SERVICE_REQUEST_ENCODING_MISMATCH": "The actual API request encoding differs from serviceDesign. Preserve the agreed JSON/FORM interface and fix the product.",
    "SERVICE_REQUEST_FIELDS_MISMATCH": "Expose the agreed serviceDesign request fields in the API body schema.",
    "SERVICE_INPUT_REJECTION_MISMATCH": "An empty required JSON body did not return an agreed rejectionStatuses response. FastAPI validates request models BEFORE entering the endpoint. Register a RequestValidationError handler on main.app returning the agreed value-free 400 response when 422 is not approved. An empty-field check inside the endpoint or an HTTPException-only handler cannot fix this earlier path. Preserve all business rejection rules, do not change QA expectations, and never echo exc.body, str(exc), raw exc.errors() or credentials.",
    "SERVICE_DATABASE_UNAVAILABLE": "The original database.get_db connection is not usable after the application's lifespan started. Restore initialization and the same isolated SQLite connection; do not make QA create product tables.",
    "SERVICE_DEPENDENCY_IMPORT_FAILED": "A required import is unavailable in the approved runtime. Use runtimeCapabilities and the immutable dependency lock; do not install a new package.",
    "SERVICE_MODEL_PRIMARY_KEY_REQUIRED": "A SQLAlchemy model has no primary key, so importing the model fails before lifespan or create_tables() can run. Define a real primary key on the reported model (for example an id column with primary_key=True, or an appropriate natural/composite key), preserving existing fields and constraints. Adding engine imports or a create_tables() function cannot repair the mapper definition.",
    "SERVICE_MODEL_DEFINITION_INVALID": "SQLAlchemy rejected a model or mapping while importing/starting the app. Inspect the reported model definition and its columns, relationships and table configuration; adding table creation cannot fix an invalid mapper.",
    "SERVICE_IMPORT_FAILED": "An application import failed. Inspect the reported import for unavailable symbols, incorrect module paths or circular imports.",
    "SERVICE_NAME_UNDEFINED": "Application startup referenced an undefined name at the reported Source location. Define or import that symbol before use.",
    "SERVICE_ARGUMENT_TYPE_INVALID": "Application import or startup passed incompatible argument types or names. Check the reported call against the approved installed library interface.",
    "SERVICE_ARGUMENT_VALUE_INVALID": "Application import or startup used an invalid value. Inspect the reported Source location and the approved library's accepted choices.",
}


def service_build_argv(design, python_executable="/usr/local/bin/python"):
    checked = design if isinstance(design, ServiceDesign) else ServiceDesign.model_validate(design)
    # The probe consumes only these machine fields. Free prose such as
    # "password_hash: str" is a plan description, not an executable argument;
    # sending it as argv also triggers the unchanged credential-assignment guard.
    # The complete design stays in the immutable Requirement and agent input.
    contract = checked.model_dump(mode="json", by_alias=True,
        include={'delivery', 'endpoints', 'storage'})
    payload = json.dumps(contract, separators=(",", ":"), ensure_ascii=False)
    # Data travels in a separate argv, never interpolated into source code.
    program = (inspect.getsource(service_start_failure_code) + '\n' + inspect.getsource(service_interface_failure)
               + '\n' + inspect.getsource(service_validation_requests) + '\n' + _PROBE)
    # Compact only fixed Host-owned code; never parse/evaluate product Source.
    # Keep the existing 8 KiB argument guard rather than enlarging it.
    tree = ast.parse(program)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant) and isinstance(node.body[0].value.value, str):
                node.body.pop(0)
    program = ast.unparse(tree)
    return (python_executable, "-I", "-X", "pycache_prefix=/work/pycache", "-c", f'exec({program!r})', payload)


def service_build_diagnostics(*, argv, exit_code, stdout, design, source_files):
    if exit_code != 1 or not isinstance(argv, (list, tuple)) or not argv or tuple(argv) != service_build_argv(design, argv[0]):
        return ()
    try:
        marker = stdout.splitlines()[-1]
        if not marker.startswith('SERVICE_START:'):
            return ()
        row = json.loads(marker[len('SERVICE_START:'):])
        if set(row) != {"code", "path", "line", "missing"} or row["code"] not in _MESSAGES:
            return ()
        path, line = row['path'], row['line']
        if path not in source_files or type(line) is not int or not 1 <= line <= len(source_files[path].splitlines()):
            return ()
        # Missing-name metadata is optional. Raw exception text is never read.
        missing = row['missing']
        if missing is not None and (not isinstance(missing, str) or not missing.isidentifier() or len(missing) > 64):
            return ()
        return (BuildDiagnostic(ruleId=row['code'], normalizedLocation=f'{path}:{line}',
                                message=_MESSAGES[row['code']], missingName=missing),)
    except (ValueError, TypeError, KeyError, IndexError):
        return ()
