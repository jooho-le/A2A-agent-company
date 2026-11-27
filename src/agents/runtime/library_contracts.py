"""Public parameter annotations sampled from an immutable runtime image.

The catalog contains public declarations, never defaults, examples or enum
members. Lookups describe the API named by a directly imported call in source.
They are not evidence of an exception or of a successful product request.
"""
import ast

# Verified in this image without loading product code. argon2-cffi 25.1.0:
# inspect.signature(argon2.PasswordHasher).parameters and
# typing.get_type_hints(argon2.PasswordHasher.__init__) in the locked runtime.
_CATALOG = {
    ('sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a',
     'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0'): {
        ('argon2', 'PasswordHasher'): {
            'time_cost': 'int', 'memory_cost': 'int', 'parallelism': 'int',
            'hash_len': 'int', 'salt_len': 'int', 'encoding': 'str', 'type': 'argon2.low_level.Type',
        },
    },
}


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def public_import(call, tree, files):
    """Identify a direct top-level import without disclosing application names."""
    if isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name):
        name, attr = call.func.value.id, call.func.attr
    elif isinstance(call.func, ast.Name):
        name, attr = call.func.id, None
    else:
        return None

    nodes = tuple(ast.walk(tree))
    binding = None
    for node in nodes:
        if (isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load)
                or isinstance(node, ast.arg) and node.arg == name
                or isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name == name
                or isinstance(node, ast.MatchMapping) and node.rest == name
                or isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler))
                and node.name == name):
            return None
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == '*':
                    return None
                if (alias.asname or alias.name.split('.')[0]) != name:
                    continue
                if binding or node not in tree.body or node.lineno >= call.lineno:
                    return None
                if isinstance(node, ast.ImportFrom) and not node.level and node.module and attr is None:
                    binding = node.module, alias.name
                elif isinstance(node, ast.Import) and attr and (alias.asname or '.' not in alias.name):
                    binding = alias.name, attr
                else:
                    return None
    if not binding:
        return None
    package = binding[0].split('.')[0]
    if any(package + '.py' in path.split('/') or package in path.split('/') for path in files):
        return None

    # Aliases of the same package can replace its exports. Omit the optional
    # declaration for modules with explicit dynamic imports, rebinding or hooks.
    package_aliases = {name, package, 'sys'}
    for node in nodes:
        if isinstance(node, ast.Import):
            package_aliases.update(alias.asname or alias.name.split('.')[0] for alias in node.names
                                   if alias.name.split('.')[0] in (package, 'sys'))
    for node in nodes:
        if isinstance(node, (ast.Attribute, ast.Subscript)) and not isinstance(node.ctx, ast.Load):
            if _root_name(node) in package_aliases:
                return None
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ('exec', 'eval', 'globals', 'locals', 'vars', '__import__'):
                return None
            if isinstance(fn, ast.Attribute) and (fn.attr in ('setattr', 'delattr', 'patch', 'import_module')
                                                  or _root_name(fn) in package_aliases and fn.attr in ('update','insert','append')):
                return None
            if isinstance(fn, ast.Name) and fn.id in ('setattr', 'delattr') and node.args and _root_name(node.args[0]) in package_aliases:
                return None
    return binding


def declared_parameter_types(environment, binding):
    """Return the declared keyword types for an already resolved public import."""
    if not isinstance(environment, dict):
        return {}
    image, lock = environment.get('containerImageDigest'), environment.get('dependencyLockHash')
    if not isinstance(image, str) or not isinstance(lock, str):
        return {}
    return dict(_CATALOG.get((image, lock), {}).get(binding, {}))


def call_parameters(call, source, files, environment):
    """Return only declared names and types for explicit keyword arguments at this site."""
    if (not isinstance(environment, dict) or not isinstance(call, ast.Call)
            or not source or len(source) > 128000 or not call.keywords or len(call.keywords) > 32):
        return None
    if any(k.arg is None for k in call.keywords) or len({k.arg for k in call.keywords}) != len(call.keywords):
        return None
    try:
        tree = ast.parse(source)
        # The caller passes an AST from a measured source location. Verify that its
        # coordinates and expression actually exist in the supplied snapshot file.
        if not any(isinstance(node, ast.Call) and (node.lineno, node.col_offset) == (call.lineno, call.col_offset)
                   and ast.dump(node) == ast.dump(call) for node in ast.walk(tree)):
            return None
        binding = public_import(call, tree, files)
    except (AttributeError, SyntaxError, ValueError, TypeError, RecursionError):
        return None
    declarations = declared_parameter_types(environment, binding)
    if not declarations:
        return None
    parameters = {name: annotation for name, annotation in declarations.items()
                  if any(keyword.arg == name for keyword in call.keywords)}
    if parameters:
        return {'imageDigest': environment['containerImageDigest'], 'dependencyLockHash': environment['dependencyLockHash'],
                'callable': '.'.join(binding),
                'parameters': parameters}
    return None
