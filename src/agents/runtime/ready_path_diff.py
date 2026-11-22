"""Compare claimed repair paths to observable Python edits, without executing product code.

A match only locates a candidate edit. Dynamic registrations and indirect calls that
cannot be resolved here remain open for validation on the submitted snapshot.
"""
import ast
from collections import defaultdict
from copy import copy
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import PurePosixPath

from agents.runtime.repair_locations import INVALID_INPUT
from orchestrator.core.security import redact_text
from orchestrator.domain.states import AgentRole
from orchestrator.workspaces.policy import authorize_path, WorkspaceAccess, WorkspaceAccessError


INPUT = 'RESPONSE_BEFORE_SIGNUP_HANDLER'
DATABASE = ('UNHANDLED_DATABASE_ERROR_AFTER_ENTRY', 'DATABASE_ERROR_RESPONSE')
LOG = 'EMITTED_APPLICATION_LOG_OR_TRACEBACK'
SUCCESS = 'SIGNUP_SUCCESS_RESPONSE'
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
DB_ERRORS = {'SQLAlchemyError', 'DatabaseError', 'DBAPIError', 'OperationalError', 'IntegrityError',
             'DataError', 'StatementError'}
# These modules expose the same class in the SCN-001 FastAPI dependency.
VALIDATION_ERROR_EXPORTS = {
    'fastapi.exceptions.RequestValidationError', 'fastapi.exception_handlers.RequestValidationError',
    'fastapi.routing.RequestValidationError', 'fastapi.applications.RequestValidationError',
}


def _nodes(statements):
    """Nested definitions do not run just because their parent is called."""
    stack = list(reversed(statements))
    while stack:
        node = stack.pop()
        if isinstance(node, (*FUNCTION, ast.ClassDef, ast.Lambda)):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + '.' + node.attr
    return ''


def _outputs(statements):
    """Response expressions, their guards, and values used to construct them."""
    nodes = list(_nodes(statements))
    outputs = [n for n in nodes if isinstance(n, (ast.Return, ast.Raise))]
    for node in nodes:
        if isinstance(node, ast.If) and any(isinstance(n, (ast.Return, ast.Raise)) for n in _nodes(node.body + node.orelse)):
            outputs.append(node.test)
    # Follow local names back to assignments, including a helper result stored before return.
    assignments = defaultdict(list)
    for node in nodes:
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    assignments[target.id].append(node)
    used = set()
    for expression in outputs:
        for name in _nodes([expression]):
            if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Load) and name.id not in used:
                used.add(name.id)
                outputs.extend(assignments.get(name.id, ()))
    return outputs


def _bounded_tree(tree):
    stack = [(tree, 0)]
    count = 0
    while stack:
        node, depth = stack.pop()
        count += 1
        if depth > 80 or count > 16000:
            return False
        stack.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return True


def _module(path):
    return path.removesuffix('.py').replace('/', '.').removesuffix('.__init__')


def _bound_names(nodes):
    """Names a statement can replace in its scope; an unknown binding invalidates import evidence."""
    pending = list(nodes)
    while pending:
        node = pending.pop()
        if isinstance(node, (*FUNCTION, ast.ClassDef)):
            yield node.name
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, (ast.Store, ast.Del)):
            yield _name(node)
        elif isinstance(node, ast.alias):
            yield node.asname or node.name.split('.')[0]
        elif isinstance(node, ast.arg):
            yield node.arg
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            yield node.name
        pending.extend(ast.iter_child_nodes(node))


def _registration(call, path):
    if path != INPUT:
        return call
    # An import spelling change alone is not evidence of an input-path repair.
    normalized = copy(call)
    normalized.args = [ast.Constant('fastapi.exceptions.RequestValidationError'), *call.args[1:]]
    return normalized


class _SourceView:
    def __init__(self, trees, route, entry, input_anchors=None):
        self.units = {}
        self.aliases = {}
        self.import_history = {}
        self.marks = defaultdict(lambda: defaultdict(list))
        self.seen_helpers = set()
        self.active_refs = set()
        self.route_units = set()
        self.exception_decorators = set()
        self.module_refs = {}
        self.input_anchors = input_anchors
        imports = defaultdict(set)
        for path, tree in trees.items():
            module = _module(path)
            self.aliases[module] = aliases = {}
            self.import_history[module] = history = defaultdict(list)
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for name in node.names:
                        aliases[name.asname or name.name.split('.')[0]] = name.name if name.asname else name.name.split('.')[0]
                        imports[module].add(name.name)
                elif isinstance(node, ast.ImportFrom):
                    package = module.split('.') if path.endswith('/__init__.py') else module.split('.')[:-1]
                    if node.level > len(package):
                        continue
                    base = package[:len(package) + 1 - node.level] if node.level else []
                    origin = '.'.join([*base, *([node.module] if node.module else [])])
                    imports[module].add(origin)
                    for name in node.names:
                        aliases[name.asname or name.name] = origin + '.' + name.name
                        imports[module].add(origin + '.' + name.name)
                elif isinstance(node, FUNCTION):
                    self.units[module + '.' + node.name] = (path, node)
                for local in _bound_names([node]):
                    origin = aliases.get(local) if isinstance(node, (ast.Import, ast.ImportFrom)) else None
                    history[local].append(((node.lineno, node.col_offset), origin))
            self.module_refs[module] = 'source/' + path + '::@module'
        self.app = '.'.join(entry)
        modules = self.reachable([entry[0]], imports)
        module_bodies = {_module(p): t.body for p, t in trees.items() if _module(p) in modules}
        self.routes = self.route_prefixes(module_bodies)
        calls = defaultdict(set)
        handlers = set()
        signup = set()
        for name, (file, fn) in self.units.items():
            module = _module(file)
            if module not in modules:
                continue
            for deco in fn.decorator_list:
                if not isinstance(deco, ast.Call) or not isinstance(deco.func, ast.Attribute):
                    continue
                receiver = self.resolve(module, deco.func.value)
                arg = deco.args[0] if deco.args else None
                value = arg.value if isinstance(arg, ast.Constant) else None
                if receiver == self.app and deco.func.attr in ('exception_handler', 'middleware', 'on_event'):
                    handlers.add(name)
                if receiver in self.routes and isinstance(value, str) and route == (deco.func.attr.upper(),
                        self.routes[receiver] + value):
                    signup.add(name)
            calls[name] = {self.resolve(module, n.func) for n in _nodes(fn.body) if isinstance(n, ast.Call)}
        for module, body in module_bodies.items():
            for n in _nodes(body):
                if self.app_call(module, n, 'add_exception_handler') and len(n.args) >= 2:
                    handlers.add(self.resolve(module, n.args[1]))
        signup_graph = self.reachable(signup, calls)
        active = self.reachable(handlers | signup, calls)
        for module, body in module_bodies.items():
            self.active_refs.add(self.module_refs[module])
            self.inspect(module, self.module_refs[module], body, (), False)
        for name in sorted(active & self.units.keys()):
            file, fn = self.units[name]
            module = _module(file)
            ref = 'source/' + file + '::' + fn.name
            self.active_refs.add(ref)
            if name in signup:
                self.route_units.add(name)
                self.record(SUCCESS, ref, module, [*fn.decorator_list, *_outputs(fn.body)])
            self.inspect(module, ref, fn.body, fn.decorator_list, name in signup_graph, fn.args)

    @staticmethod
    def reachable(roots, edges):
        found, todo = set(), list(roots)
        while todo:
            name = todo.pop()
            if name not in found:
                found.add(name)
                todo.extend(edges.get(name, ()))
        return found

    def app_call(self, module, node, method):
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == method and self.resolve(module, node.func.value) == self.app)

    def route_prefixes(self, modules):
        # Only routers connected to the configured app with literal prefixes.
        prefixes = {self.app: ''}
        included, constructors = [], {}
        for module, body in modules.items():
            for n in body:
                if (isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                        and self.resolve(module, n.value.func) in ('fastapi.APIRouter', 'fastapi.routing.APIRouter')):
                    for target in n.targets:
                        constructors[self.resolve(module, target)] = self.prefix(n.value)
                if (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and n.value.args
                        and isinstance(n.value.func, ast.Attribute) and n.value.func.attr == 'include_router'):
                    included.append((self.resolve(module, n.value.func.value), self.resolve(module, n.value.args[0]),
                                     self.prefix(n.value)))
        for _ in range(len(included)):
            additions = {child: prefixes[parent] + prefix + constructors[child]
                         for parent, child, prefix in included if parent in prefixes and child not in prefixes
                         and prefix is not None and constructors.get(child) is not None}
            if not additions:
                break
            prefixes.update(additions)
        return prefixes

    @staticmethod
    def prefix(call):
        for kw in call.keywords:
            if kw.arg == 'prefix':
                return kw.value.value if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str) else None
        return ''

    def resolve(self, module, node):
        dotted = _name(node)
        first, dot, tail = dotted.partition('.')
        target = self.aliases[module].get(first, module + '.' + first)
        return target + dot + tail

    def record(self, path, ref, module, expressions):
        """Attribute a called helper only when it feeds the selected observable expression."""
        work = [(ref, module, expressions)]
        while work:
            ref, module, expressions = work.pop()
            for expr in expressions:
                self.marks[path][ref].append(ast.dump(expr))
                for call in _nodes([expr]):
                    if not isinstance(call, ast.Call):
                        continue
                    target = self.resolve(module, call.func)
                    if target not in self.units or (path, target) in self.seen_helpers:
                        continue
                    self.seen_helpers.add((path, target))
                    file, fn = self.units[target]
                    work.append(('source/' + file + '::' + fn.name, _module(file), _outputs(fn.body)))

    def validation_error_import(self, module, exc, local_names):
        """Follow explicit project re-exports to a known framework binding, without importing app files."""
        name = _name(exc)
        end = (float('inf'), 0)
        position = (exc.lineno, exc.col_offset) if local_names is None else end
        seen = set()
        while (module, name) not in seen and len(seen) < 32:
            seen.add((module, name))
            tail = name.partition('.')[2]
            parts = name.split('.')
            prefixes = ['.'.join(parts[:length]) for length in range(1, len(parts) + 1)]
            if local_names and any(key in local_names for key in prefixes):
                return False
            history = self.import_history[module]
            bindings = (event for key in (*prefixes, '*') for event in history.get(key, ()) if event[0] <= position)
            _, origin = max(bindings, key=lambda event: event[0], default=((0, 0), None))
            if not origin or origin.endswith('.*'):
                return False
            target = origin + ('.' + tail if tail else '')
            if target in VALIDATION_ERROR_EXPORTS:
                return 'fastapi' not in self.import_history and target.rsplit('.', 1)[0] not in self.import_history
            # The imported name can itself be exported by a local module or package.
            module, _, name = target.rpartition('.')
            while module and module not in self.import_history:
                module, _, suffix = module.rpartition('.')
                name = suffix + '.' + name
            if not module:
                return False
            position, local_names = end, None
        return False

    def handler_paths(self, module, exc, local_names=None):
        resolved = self.resolve(module, exc)
        if exc is not None and self.validation_error_import(module, exc, local_names):
            return (INPUT,)
        if resolved.startswith(('sqlalchemy.', 'sqlite3.')) and resolved.split('.')[-1] in DB_ERRORS:
            # A duplicate-key response already handled at signup cannot establish
            # coverage of the distinct exception observed escaping the application.
            return (DATABASE[1],) if resolved.endswith('.IntegrityError') else DATABASE
        return ()

    def inspect(self, module, ref, body, decorators, signup_reachable, args=None):
        nodes = list(_nodes(body))
        if signup_reachable and self.input_anchors is not None:
            self.inspect_invalid_input(module, ref, body, nodes)
        for deco in decorators:
            if not isinstance(deco, ast.Call) or not deco.args:
                continue
            arg = deco.args[0]
            if self.app_call(module, deco, 'exception_handler'):
                if isinstance(arg, ast.Name) and arg.id == 'Exception':
                    # Record the spelling at this decorator; this is not dispatch evidence.
                    self.exception_decorators.add(ref)
                for path in self.handler_paths(module, arg):
                    self.record(path, ref, module, [_registration(deco, path), *_outputs(body)])
            if self.app_call(module, deco, 'middleware') and isinstance(arg, ast.Constant) and arg.value == 'http':
                self.record(INPUT, ref, module, [deco, *_outputs(body)])
        local_names = set(_bound_names([args, *body])) if args is not None else None
        for node in nodes:
            if isinstance(node, ast.Assign) and any(self.resolve(module, t) == 'sys.excepthook' for t in node.targets):
                self.record(LOG, ref, module, [node])
            if isinstance(node, ast.Call):
                name = _name(node.func).split('.')[-1]
                if self.app_call(module, node, 'add_exception_handler') and len(node.args) >= 2:
                    target = self.units.get(self.resolve(module, node.args[1]))
                    for path in self.handler_paths(module, node.args[0], local_names):
                        self.record(path, ref, module, [_registration(node, path)])
                        if target:
                            file, fn = target
                            self.record(path, 'source/' + file + '::' + fn.name, _module(file), _outputs(fn.body))
                if name in {'addFilter', 'setFormatter', 'addHandler', 'basicConfig', 'dictConfig',
                            'error', 'exception', 'warning', 'critical', 'info', 'debug', 'print', 'print_exc'}:
                    self.record(LOG, ref, module, [node])
                elif name == 'create_engine':
                    self.record(LOG, ref, module, [kw for kw in node.keywords if kw.arg in ('echo', 'hide_parameters')])
            if signup_reachable and isinstance(node, ast.Try):
                has_transaction = any(isinstance(n, ast.Call) and _name(n.func).split('.')[-1] in
                                      {'commit', 'flush', 'execute', 'begin'} for n in _nodes(node.body))
                for handler in node.handlers:
                    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                    db_paths = {p for t in types for p in self.handler_paths(module, t) if p in DATABASE}
                    catches_transaction = has_transaction and any(
                        t is None or _name(t) in ('Exception', 'BaseException') for t in types)
                    if catches_transaction:
                        db_paths.update(DATABASE)
                    if db_paths:
                        outcomes = _outputs(handler.body)
                        if outcomes:
                            for path in db_paths:
                                self.record(path, ref, module, [t for t in types if t] + outcomes)

    def inspect_invalid_input(self, module, ref, body, nodes):
        # A helper/caller tied to an observed frame, or the registered email validator.
        # Only compare its inputs, early exit guards and surrounding error responses.
        anchors = self.input_anchors
        own = anchors.get(module + '.' + ref.rsplit('::', 1)[1], ())
        targets = anchors.keys() | {'email_validator.validate_email'}
        calls = [n for n in nodes if isinstance(n, ast.Call)
                 and self.resolve(module, n.func) in targets]
        points = calls + [n for n in nodes if isinstance(n, ast.stmt) and own and ast.dump(n) in own]
        if not points:
            return
        # Participating calls in source order distinguish validation before vs after an observed failure.
        self.marks[INVALID_INPUT][ref].extend(ast.dump(n) for n in sorted(calls, key=lambda n: (n.lineno, n.col_offset)))
        point_set = set(points)
        point_reads = [(point.lineno, _operand_names(point)) for point in points]
        for node in body:
            # Restrict preceding early returns to the same function body and same input.
            # A nested guard in an unrelated branch is not evidence of changed dispatch.
            if isinstance(node, ast.If):
                outputs = _outputs(node.body)
                tested = _operand_names(node.test)
                if outputs and any(node.lineno <= line and tested & reads for line, reads in point_reads):
                    self.record(INVALID_INPUT, ref, module, [node.test, *outputs])
        for node in nodes:
            if isinstance(node, ast.Try) and not point_set.isdisjoint(_nodes(node.body)):
                for handler in node.handlers:
                    types = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                    if any(t is None or self.resolve(module, t) in ('email_validator.EmailNotValidError',
                            'builtins.ValueError', 'builtins.Exception') or _name(t) in ('ValueError', 'Exception') for t in types):
                        self.record(INVALID_INPUT, ref, module, [t for t in types if t] + _outputs(handler.body))


def _operand_names(node):
    """data.email and data.password have different roles even though both start with data."""
    reads = set()
    stack = [node]
    while stack:
        expr = stack.pop()
        if isinstance(expr, ast.Call):
            stack.extend([*expr.args, *(kw.value for kw in expr.keywords)])
            if isinstance(expr.func, ast.Attribute):
                stack.append(expr.func.value)
        elif isinstance(expr, (ast.Name, ast.Attribute)) and isinstance(expr.ctx, ast.Load):
            name = _name(expr)
            if name:
                reads.add(name)
        else:
            stack.extend(ast.iter_child_nodes(expr))
    return reads


def _input_anchors(before, trees, sites):
    from agents.runtime.normal_request_repair import _frame_statement
    anchors = {}
    bound = []
    for site in sites:
        file = site['toolPath'].removeprefix('source/')
        source = before.get(file)
        if file not in trees or sha256(source).hexdigest() != site['sourceSha256']:
            continue
        located = _frame_statement(source, site['line'])
        if located and site.get('sourceRef') == 'source/' + file + '::' + located[0]:
            anchors.setdefault(_module(file) + '.' + located[0], set()).add(ast.dump(located[1]))
            bound.append(site)
    return anchors, bound


def _changed_source_refs(file, old, new):
    """Identify bounded current symbols with an AST diff. This alone never supplies a path link."""
    before = {n.name: n for n in old.body if isinstance(n, FUNCTION)}
    for node in new.body:
        if isinstance(node, FUNCTION) and redact_text(node.name) == node.name:
            previous = before.get(node.name)
            if previous is None or ast.dump(previous) != ast.dump(node):
                yield 'source/' + file + '::' + node.name
    old_body = [ast.dump(n) for n in old.body if not isinstance(n, FUNCTION)]
    new_body = [ast.dump(n) for n in new.body if not isinstance(n, FUNCTION)]
    if old_body != new_body:
        yield 'source/' + file + '::@module'


@dataclass
class ReadySourceComparison:
    """Host-owned static observations on exactly the same two trees as the READY gate."""
    known_refs: set = field(default_factory=set)
    unchanged_frame_functions: set = field(default_factory=set)
    unchanged_route_functions: set = field(default_factory=set)
    changed_exception_decorators: set = field(default_factory=set)


def changed_path_refs(before, after, paths, route, entry, *, invalid_input_sites=(), changed_refs=None,
                      comparison=None):
    """Compute symmetric, line-number-independent differences on the same bounded file set."""
    result = {path: set() for path in paths}
    if not paths or not all(isinstance(part, str) and part for part in entry):
        return result
    old, new = {}, {}
    remaining = 1_048_576
    for file in sorted(before.keys() | after.keys()):
        parts = PurePosixPath(file).parts
        if (not file.endswith('.py') or {'test', 'tests'} & set(parts) or parts[-1].startswith('test_')
                or redact_text(file) != file):
            continue
        try:
            authorize_path(AgentRole.DEVELOPER, 'source/' + file, WorkspaceAccess.READ)
        except WorkspaceAccessError:
            continue
        a, b = before.get(file, b''), after.get(file, b'')
        size = max(len(a), len(b))
        if size > min(128000, remaining):
            continue
        remaining -= size
        try:
            first = ast.parse(a)
            second = first if a == b else ast.parse(b)
        except (SyntaxError, ValueError, RecursionError):
            continue
        if _bounded_tree(first) and (first is second or _bounded_tree(second)):
            old[file], new[file] = first, second
            if changed_refs is not None and first is not second and b and len(changed_refs) < 4:
                for ref in _changed_source_refs(file, first, second):
                    if len(ref) <= 400:
                        changed_refs.add(ref)
                    if len(changed_refs) == 4:
                        break
    anchors, bound_sites = _input_anchors(before, old, invalid_input_sites) if INVALID_INPUT in paths else (None, ())
    previous, current = _SourceView(old, route, entry, anchors), _SourceView(new, route, entry, anchors)
    if comparison is not None:
        site_refs = {s['sourceRef'] for s in bound_sites}
        comparison.known_refs.update(current.active_refs)
        for name, (file, node) in current.units.items():
            ref = 'source/' + file + '::' + node.name
            if ref not in current.active_refs:
                continue
            older = previous.units.get(name)
            if ref in site_refs or name in current.route_units or ref in current.exception_decorators:
                unchanged = older and older[0] == file and ast.dump(older[1]) == ast.dump(node)
                if ref in site_refs and unchanged:
                    comparison.unchanged_frame_functions.add(ref)
                if name in previous.route_units and name in current.route_units and unchanged:
                    comparison.unchanged_route_functions.add(ref)
                if ref in current.exception_decorators and not unchanged:
                    comparison.changed_exception_decorators.add(ref)
    for path in paths:
        a, b = previous.marks[path], current.marks[path]
        for ref in a.keys() | b.keys():
            # Removing the only log write from a still-connected function is a candidate edit.
            # A detached handler or a deleted source location cannot substantiate current dispatch.
            if ref not in b and (path != LOG or ref not in current.active_refs):
                continue
            if a.get(ref, []) == b.get(ref, []):
                continue
            file, symbol = ref.removeprefix('source/').rsplit('::', 1)
            if before.get(file) == after.get(file):
                continue
            if symbol != '@module':
                key = _module(file) + '.' + symbol
                left, right = previous.units.get(key), current.units.get(key)
                if left and right and ast.dump(left[1]) == ast.dump(right[1]):
                    continue
            result[path].add(ref)
    if anchors:
        from agents.runtime.normal_request_repair import changed_frame_refs
        from agents.runtime.repair_dependencies import indirect_frame_refs
        sites = [s for s in bound_sites if s.get('sourceRef') in previous.active_refs
                 and s.get('sourceRef') in current.active_refs]
        edits = changed_frame_refs(before, after, sites) | indirect_frame_refs(before, after, sites)
        result[INVALID_INPUT].update(edits & current.active_refs)
    return result


def claim_issues(rows, links, changed_refs=(), comparison=None):
    """Compare every affirmative claim. Open paths still go to the same-snapshot rechecks."""
    if set(rows) != set(links):
        raise ValueError("Host repair path set changed")
    known = set(changed_refs)
    for refs in links.values():
        known.update(refs)
    if comparison is not None:
        known.update(comparison.known_refs)
    issues = []
    for path, row in rows.items():
        if row['edit'] is None:
            continue
        attempted = row['sourceRef']
        if attempted in links[path]:
            continue
        issue = {'fieldPath': '/repairPaths/' + path + '/sourceRef', 'reasonCode': 'NO_LINKED_CODE_CHANGE',
                 'candidateRefs': sorted(links[path])[:4]}
        if changed_refs:
            issue['changedRefs'] = sorted(changed_refs)[:4]
        # An invented sourceRef is free model text. Only echo symbols also found in the Host inventory.
        if attempted in known:
            issue['attemptedSourceRef'] = attempted
        else:
            issue['attemptedRefStatus'] = 'NOT_VERIFIED_IN_SOURCE'
        if comparison is not None and not links[path]:
            if path == INVALID_INPUT:
                issue['unchangedRelatedFunctions'] = sorted(comparison.unchanged_frame_functions)[:4]
                issue['unchangedRouteFunctions'] = sorted(comparison.unchanged_route_functions)[:4]
            if path in (INPUT, INVALID_INPUT):
                issue['changedExceptionDecorators'] = sorted(comparison.changed_exception_decorators)[:4]
        issues.append(issue)
    return issues
