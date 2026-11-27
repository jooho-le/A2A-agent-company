"""Detect literal calls that the exact installed library rejects, without executing source."""
import ast

from agents.runtime.library_contracts import declared_parameter_types, public_import
from agents.runtime.ready_path_diff import _bounded_tree
from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import WorkspaceAccess, WorkspaceAccessError, authorize_path


# In this exact image argon2-cffi 25.1.0's PasswordHasher.__init__ explicitly
# checks these seven positional-or-keyword parameters with isinstance and raises TypeError on a mismatch.
# A public annotation by itself does not prove that a library rejects a value.
_VERIFIED_IMAGE = 'sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a'
_VERIFIED_LOCK = 'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0'
_STRICT_PARAMETERS = ('time_cost', 'memory_cost', 'parallelism', 'hash_len', 'salt_len', 'encoding', 'type')
_BINDING = ('argon2', 'PasswordHasher')
_ACCEPTED_LITERALS = {'int': {'bool', 'int'}, 'str': {'str'}, 'argon2.low_level.Type': set()}
_MAX_SOURCE_BYTES = 128_000
_MAX_TOTAL_BYTES = 1_048_576
_MAX_MATCHES = 6


def _literal_type(node):
    if isinstance(node, ast.Constant):
        return type(node.value).__name__
    if isinstance(node, (ast.Dict, ast.Tuple, ast.Set, ast.List)):
        return type(node).__name__.lower()
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub))
            and isinstance(node.operand, ast.Constant) and type(node.operand.value) in (int, float, complex)):
        return type(node.operand.value).__name__
    return None


def _product_python(path):
    parts = path.split('/')
    if (not path.endswith('.py') or any(p in ('test', 'tests', 'node_modules') or p.startswith('.') for p in parts)
            or parts[-1].startswith('test_') or parts[-1].endswith('_test.py') or parts[-1] == 'conftest.py'
            or redact_text(path) != path):
        return False
    try:
        authorize_path(AgentRole.DEVELOPER, 'source/' + path, WorkspaceAccess.READ)
    except WorkspaceAccessError:
        return False
    return True


def _public_call_names(tree):
    # Only a speed filter. public_import verifies the actual binding, including shadowing.
    names = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.update((a.asname or a.name, 'PasswordHasher') for a in node.names if a.name == 'argon2')
        if isinstance(node, ast.ImportFrom) and not node.level and node.module == 'argon2':
            names.update((a.asname or a.name, None) for a in node.names if a.name == 'PasswordHasher')
    return names


def _bind_arguments(call):
    if (len(call.args) + len(call.keywords) > len(_STRICT_PARAMETERS)
            or any(isinstance(arg, ast.Starred) for arg in call.args)):
        return {}
    bound = dict(zip(_STRICT_PARAMETERS, call.args))
    for keyword in call.keywords:
        if keyword.arg not in _STRICT_PARAMETERS or keyword.arg in bound:
            return {}  # **kwargs, duplicate/unsupported argument, or an ambiguous signature.
        bound[keyword.arg] = keyword.value
    return bound


def _may_replace_library(tree):
    """Avoid case-specific assertions when product code can replace a public import or its constructor."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            if node.attr in ('PasswordHasher', '__new__', '__init__', '__call__'):
                return True
        if isinstance(node, ast.Subscript) and not isinstance(node.ctx, ast.Load):
            if isinstance(node.value, ast.Attribute) and node.value.attr in ('modules', '__dict__'):
                return True
        if isinstance(node, ast.Call):
            name = getattr(node.func, 'id', None) or getattr(node.func, 'attr', None)
            if name in ('setattr', 'delattr', 'patch', 'exec', 'eval', '__import__', 'import_module'):
                return True
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'object':  # patch.object, including aliases
                return True
    return False


def ready_library_type_mismatches(files, environment, *, changed_paths=None):
    """Read the current snapshot; inspect selected product modules for explicit literal arguments."""
    if (not isinstance(environment, dict) or environment.get('containerImageDigest') != _VERIFIED_IMAGE
            or environment.get('dependencyLockHash') != _VERIFIED_LOCK):
        return []
    declarations = declared_parameter_types(environment, _BINDING)
    selected = None if changed_paths is None else set(changed_paths)
    findings = []
    remaining_bytes = _MAX_TOTAL_BYTES
    remaining_bindings = 32
    for path, source in sorted(files.items()):
        if not _product_python(path):
            continue
        # Another application module can change the public library at startup.
        # Inspect all product modules that mention this dependency, even when
        # the edit is confined to one file. Excessive input leaves the build in charge.
        if not isinstance(source, bytes) or len(source) > min(_MAX_SOURCE_BYTES, remaining_bytes):
            return []
        remaining_bytes -= len(source)
        if b'argon2' not in source and b'PasswordHasher' not in source:
            continue
        try:
            tree = ast.parse(source)
        except (SyntaxError, ValueError, RecursionError):
            return []
        if not _bounded_tree(tree) or _may_replace_library(tree):
            return []
        if selected is not None and path not in selected:
            continue
        names = _public_call_names(tree)
        if not names or len(findings) >= _MAX_MATCHES:
            continue
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if isinstance(func, ast.Name):
                name = (func.id, None)
            elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                name = (func.value.id, func.attr)
            else:
                continue
            if name not in names:
                continue
            bad = []
            for parameter, arg in _bind_arguments(call).items():
                actual = _literal_type(arg)
                required = declarations.get(parameter)
                if actual and required in _ACCEPTED_LITERALS and actual not in _ACCEPTED_LITERALS[required]:
                    bad.append({'toolPath': 'source/' + path, 'line': call.lineno, 'callable': '.'.join(_BINDING),
                                'parameter': parameter, 'requiredType': required, 'actualType': actual})
            if bad and remaining_bindings:
                remaining_bindings -= 1
                if public_import(call, tree, files) == _BINDING:
                    findings.extend(bad[:_MAX_MATCHES - len(findings)])
                    if len(findings) == _MAX_MATCHES:
                        break
    return findings
