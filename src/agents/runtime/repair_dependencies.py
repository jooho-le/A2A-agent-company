"""Candidate edits to bindings used by a measured statement. Only a rerun can verify their effect."""
import ast
from functools import lru_cache
from hashlib import sha256
from importlib.util import resolve_name

from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import authorize_path, WorkspaceAccess, WorkspaceAccessError
from .normal_request_repair import _frame_statement
from .ready_path_diff import FUNCTION, _bounded_tree, _bound_names, _name


def _source_reader(files):
    remaining = 1_048_576

    @lru_cache(maxsize=128)
    def tree(path):
        nonlocal remaining
        source = files.get(path)
        if not source or len(source) > min(128_000, remaining) or redact_text(path) != path:
            return None
        if any(p in ('test', 'tests') or p.startswith('test_') for p in path.split('/')):
            return None
        try:
            authorize_path(AgentRole.DEVELOPER, 'source/' + path, WorkspaceAccess.READ)
            remaining -= len(source)
            result = ast.parse(source)
            return result if _bounded_tree(result) else None
        except (WorkspaceAccessError, SyntaxError, ValueError, RecursionError):
            return None
    return tree


def _binding(body, name, *, before_line=None):
    """Require a single unconditional binding; ambiguous local effects belong to the runtime check."""
    found = [node for node in body if (before_line is None or node.lineno < before_line)
             and name in _bound_names([node])]
    if len(found) != 1:
        return None
    node = found[0]
    if isinstance(node, (*FUNCTION, ast.Import, ast.ImportFrom)):
        return node
    if isinstance(node, (ast.Assign, ast.AnnAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            return node
    return None


def _fingerprint(node):
    if isinstance(node, FUNCTION):
        # A docstring edit cannot repair a measured failure.
        parts = [*node.decorator_list, node.args, *node.body]
        if ast.get_docstring(node, clean=False) is not None:
            parts.remove(node.body[0])
        return tuple(ast.dump(part) for part in parts)
    return ast.dump(node)


def _import_origin(node, name, path):
    if isinstance(node, ast.ImportFrom):
        base = node.module or ''
        if node.level:
            try:
                base = resolve_name('.' * node.level + base, path.rpartition('/')[0].replace('/', '.'))
            except (ValueError, ImportError):
                return None
    for alias in node.names:
        if (alias.asname or alias.name.split('.')[0]) == name:
            if isinstance(node, ast.ImportFrom):
                return base + '.' + alias.name
            return alias.name if alias.asname else name
    return None


def _bindings(read_tree, files, path, scope, statement):
    tree = read_tree(path)
    if tree is None:
        return {}
    local = None
    if scope != '@module':
        matches = [n for n in ast.walk(tree) if isinstance(n, FUNCTION) and n.name == scope]
        # Closures/class scopes may shadow a global even when the measured
        # function has no local binding. Leave these to runtime evidence.
        if len(matches) != 1 or matches[0] not in tree.body:
            return {}
        local = matches[0]
    expression = ast.dump(statement)
    matching = [n for n in ast.walk(local or tree) if type(n) is type(statement) and ast.dump(n) == expression]
    if len(matching) != 1:
        return {}
    line = matching[0].lineno
    local_names = set(_bound_names([local.args, *local.body])) if local is not None else set()
    arguments = set(_bound_names([local.args])) if local is not None else set()
    result = {}
    # Only follow bindings that the actual failing expression reads.
    names = {_name(node) for node in ast.walk(statement)
             if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load)}
    names.discard('')
    if len(names) > 32:
        return {}
    for dotted in sorted(names):
        name, _, tail = dotted.partition('.')
        is_local = name in local_names
        owner = scope if is_local else '@module'
        if name in arguments:
            continue
        binding = _binding(local.body if is_local else tree.body, name,
                           before_line=line if is_local or scope == '@module' else None)
        if binding is None:
            continue
        if isinstance(binding, FUNCTION):
            if not is_local:
                result[(path, binding.name, name)] = _fingerprint(binding)
            continue
        if not isinstance(binding, (ast.ImportFrom, ast.Import)):
            result[(path, owner, name)] = _fingerprint(binding)
            continue
        origin = _import_origin(binding, name, path)
        if not origin:
            continue
        result[(path, owner, name)] = origin
        module, _, target = (origin + ('.' + tail if tail else '')).rpartition('.')
        if module and all(segment.isidentifier() for segment in module.split('.')):
            prefix = module.replace('.', '/')
            # Python resolves a package before a homonymous module. Do not
            # fall back to the module when the package cannot be inspected.
            other = next((p for p in (prefix + '/__init__.py', prefix + '.py') if p in files), None)
            if other:
                imported = read_tree(other)
                if imported is not None:
                    definition = _binding(imported.body, target)
                    if isinstance(definition, FUNCTION):
                        result[(other, definition.name, target)] = _fingerprint(definition)
                    elif isinstance(definition, (ast.Assign, ast.AnnAssign)):
                        result[(other, '@module', target)] = _fingerprint(definition)
    return result


def indirect_frame_refs(before, after, sites):
    if not sites:
        return set()
    original, candidate = _source_reader(before), _source_reader(after)
    refs = set()
    for site in sites:
        path = site['toolPath'].removeprefix('source/')
        source = before.get(path)
        if (original(path) is None or candidate(path) is None
                or sha256(source).hexdigest() != site['sourceSha256']):
            continue
        try:
            located = _frame_statement(source, site['line'])
            if not located:
                continue
            scope, statement = located
            old = _bindings(original, before, path, scope, statement)
            new = _bindings(candidate, after, path, scope, statement)
            for (file, symbol, name), value in new.items():
                if before.get(file) != after.get(file) and old.get((file, symbol, name)) != value:
                    ref = f'source/{file}::{symbol}'
                    if len(ref) <= 400 and redact_text(ref) == ref:
                        refs.add(ref)
        except (RecursionError, ValueError, SyntaxError):
            continue
    return refs
