"""Read coordinates for a measured failure. Static syntax is never execution evidence."""
import ast
from hashlib import sha256

from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import authorize_path, WorkspaceAccess, WorkspaceAccessError


INVALID_INPUT = 'INVALID_INPUT_SERVER_ERROR'
_INPUT = ('EXCEPTION_HANDLER', 'INPUT_VALIDATOR', 'SIGNUP_ROUTE')
_STORAGE = ('TRANSACTION', 'EXCEPTION_HANDLER', 'SIGNUP_ROUTE')
_LOGS = ('LOG_WRITE', 'ENGINE_CONFIGURATION', 'TRANSACTION')
_OPERATIONS = {
    'RESPONSE_BEFORE_SIGNUP_HANDLER': _INPUT,
    INVALID_INPUT: ('INPUT_VALIDATOR', 'SIGNUP_ROUTE'),
    'UNHANDLED_DATABASE_ERROR_AFTER_ENTRY': _STORAGE,
    'DATABASE_ERROR_RESPONSE': _STORAGE,
    'EMITTED_APPLICATION_LOG_OR_TRACEBACK': _LOGS,
    'SIGNUP_SUCCESS_RESPONSE': ('SIGNUP_ROUTE',),
}


def _operation(call, decorators):
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, 'id', None)
    if name == 'add_exception_handler' or name == 'exception_handler' and call in decorators:
        return 'EXCEPTION_HANDLER'
    if call in decorators and name in ('field_validator', 'model_validator', 'validator', 'root_validator'):
        return 'INPUT_VALIDATOR'
    if call in decorators and name == 'post' and call.args and isinstance(call.args[0], ast.Constant):
        path = call.args[0].value
        if isinstance(path, str) and path.rstrip('/').endswith('/signup'):
            return 'SIGNUP_ROUTE'
    if name in ('commit', 'rollback', 'flush', 'begin'):
        return 'TRANSACTION'
    if name in ('error', 'critical', 'exception', 'warning', 'info', 'debug', 'log', 'print', 'print_exc'):
        return 'LOG_WRITE'
    if name == 'create_engine':
        return 'ENGINE_CONFIGURATION'
    return None


def read_locations(failures, files):
    """Only coordinates from the active pre-edit Snapshot, bounded even on unusual inputs."""
    wanted = {op for failure in failures for op in _OPERATIONS.get(failure, ())}
    sites = {op: [] for op in wanted}
    remaining = 1_048_576
    for path, source in sorted(files.items()):
        if (not path.endswith('.py') or len(source) > min(128_000, remaining)
                or redact_text(path) != path):
            continue
        tool_path = 'source/' + path
        try:
            authorize_path(AgentRole.DEVELOPER, tool_path, WorkspaceAccess.READ)
        except WorkspaceAccessError:
            continue
        remaining -= len(source)
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            continue
        decorators = {decorator for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                      for decorator in node.decorator_list}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            operation = _operation(node, decorators)
            if operation in wanted and len(sites[operation]) < 4:
                site = {'toolPath': tool_path, 'line': node.lineno, 'operation': operation}
                if operation == 'EXCEPTION_HANDLER' and node.args:
                    exc = node.args[0]
                    name = exc.id if isinstance(exc, ast.Name) else getattr(exc, 'attr', None)
                    if isinstance(name, str) and len(name) <= 80 and redact_text(name) == name:
                        site['registeredException'] = name
                if site not in sites[operation]:
                    sites[operation].append(site)
    return {failure: [{**site, 'operation': 'ROUTE_BOUNDARY_ONLY'}
                      if failure == 'RESPONSE_BEFORE_SIGNUP_HANDLER' and op == 'SIGNUP_ROUTE' else site
                      for op in _OPERATIONS[failure] for site in sites[op]]
            for failure in sorted(failures) if failure in _OPERATIONS}


def diagnostic_sites(frames, files):
    """Resolve only observed coordinates belonging to unique, readable files on this working baseline."""
    wanted = {(f['sourceSha256'], f['line']) for f in frames}
    if not wanted:
        return []
    locations = {}
    for path, content in files.items():
        if not path.endswith('.py') or len(content) > 128000 or redact_text(path) != path:
            continue
        name = 'source/' + path
        try:
            authorize_path(AgentRole.DEVELOPER, name, WorkspaceAccess.READ)
        except WorkspaceAccessError:
            continue
        digest = sha256(content).hexdigest()
        for key in wanted:
            if digest == key[0] and key[1] <= len(content.splitlines()):
                locations[key] = None if key in locations else name
    return [{'sourceSha256': digest, 'line': line, 'toolPath': name}
            for (digest, line), name in sorted(locations.items()) if name]
