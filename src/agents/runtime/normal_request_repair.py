"""Prioritize a measured normal-request failure ahead of dependent validation failures."""
import ast
import re
import sysconfig
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from agents.runtime.library_contracts import call_parameters, public_import
from agents.runtime.repair_locations import diagnostic_sites
from orchestrator.core.security import redact_text
from orchestrator.domain.validation_feedback import observed_argument_type, server_status_evidence


NORMAL_REQUEST = 'NORMAL_REQUEST_RUNTIME_FAILURE'


def _http_status(observation):
    value = observation.get('actual')
    if type(value) in (int, float) and 100 <= value < 600 and int(value) == value:
        return int(value)
    return None


def _blocked_setup_statuses(observations):
    if not any(o.get('status') == 'NOT_RUN' and o.get('reason') == 'PRECONDITION_OR_OBSERVATION_UNAVAILABLE'
               for o in observations):
        return set()
    return {status for obs in observations
            if obs.get('kind') == 'precondition' and obs.get('itemId') == 'http_status' and obs.get('status') == 'FAIL'
            and (obs.get('caseId') == 'precondition' or re.fullmatch(r'account_\d+_setup', obs.get('caseId') or ''))
            and (status := _http_status(obs)) is not None and status >= 300}


def measured_prerequisite(rows):
    statuses, exceptions, frames, blocked = set(), set(), {}, set()
    failed_creation = []
    observed_types = {}
    selected_snapshots = set()
    for row in rows:
        if row.get('hostDecision') != 'FIX_TARGET':
            continue
        observations = row.get('observations') or []
        setup = _blocked_setup_statuses(observations)
        if setup:
            statuses.update(setup)
            blocked.add((row.get('reportArtifactId'), row.get('testId')))
        source = row.get('sourceArtifactId'), row.get('codeVersion')
        if all(source):
            selected_snapshots.add(source)
        missing_row = {}
        for obs in observations:
            if (obs.get('kind') == 'assertion' and obs.get('itemId') in ('user_delta', 'account_count')
                    and obs.get('status') == 'FAIL' and type(obs.get('actual')) in (int, float) and obs['actual'] == 0):
                missing_row.setdefault(obs.get('caseId'), {})[obs['itemId']] = 0
        for obs in observations:
            case = obs.get('caseId') or ''
            # Positive case labels are from the measured feedback. A missing row
            # during a duplicate or invalid request can mean a different defect.
            if (obs.get('kind') == 'assertion' and obs.get('itemId') == 'http_status'
                    and (case == 'success' or re.fullmatch(r'(?:valid_email|accept_length)_\d+', case))):
                status = _http_status(obs)
                if status is not None and (case in missing_row or (obs.get('status') == 'FAIL' and status >= 300)):
                    facts = {'reportArtifactId': row.get('reportArtifactId'), 'testId': row.get('testId'),
                             'caseId': case, 'httpStatus': status, **server_status_evidence(obs)}
                    facts.update(missing_row.get(case, {}))
                    failed_creation.append(facts)
                    statuses.add(status)
            if (case == 'success' and obs.get('kind') == 'precondition' and obs.get('itemId') == 'response_status'
                    and obs.get('status') == 'UNVERIFIED' and obs.get('reason') == 'UNHANDLED_APP_EXCEPTION'):
                diagnostic = obs.get('diagnostic') or {}
                if diagnostic.get('escapedToClient') is True:
                    if diagnostic.get('exceptionType'):
                        exceptions.add(diagnostic['exceptionType'])
                    frame = diagnostic.get('appFrame')
                    if frame:
                        key = frame['sourceSha256'], frame['line']
                        frames[key] = frame
                        actual_type = observed_argument_type(diagnostic.get('observedCause'))
                        if diagnostic.get('exceptionType') == 'builtins.TypeError' and actual_type:
                            observed_types.setdefault(key, set()).add(actual_type)
    if not (statuses or exceptions or frames):
        return None, []
    # Checks skipped after the same measured prerequisite provide context only.
    # They do not select a new fix target or invent a cause for an excluded test.
    for row in rows:
        if (row.get('outcome') == 'UNVERIFIED'
                and (row.get('sourceArtifactId'), row.get('codeVersion')) in selected_snapshots
                and statuses & _blocked_setup_statuses(row.get('observations') or [])):
            blocked.add((row.get('reportArtifactId'), row.get('testId')))
    # Keep historical evidence unchanged. A cause is attributable to a measured
    # frame only if its type is unambiguous across the supplied observations.
    diagnostic_frames = []
    for key, frame in frames.items():
        types = observed_types.get(key, ())
        cause = {'code': 'ARGUMENT_TYPE_MISMATCH', 'actualType': next(iter(types))} if len(types) == 1 else None
        diagnostic_frames.append({**frame, 'observedCause': cause} if cause else frame)
    return {
        'httpStatuses': sorted(statuses), 'exceptionTypes': sorted(exceptions),
        'blockedCheckCount': len(blocked), 'failedCreationCases': failed_creation,
        'observedAppFrames': list(frames.values()),
        'instruction': ('Fix the normal request first. Inspect the failing operation in the measured source '
                        'before changing error responses or downstream checks. statementExcerpt shows source syntax '
                        'with values masked; observedCause is supplied only for a recognized measured exception. '
                        'Use the source coordinates to inspect the original statement. A different exception handler '
                        'does not repair the operation that raised. Preserve all other feedback for revalidation; '
                        'a candidate edit is not proof the request succeeds.'),
    }, diagnostic_frames


def _frame_statement(source, line):
    """Locate one unambiguous executable statement, with a reportable enclosing symbol."""
    tree = ast.parse(source)
    scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.lineno <= line <= n.end_lineno]
    scope = min(scopes, key=lambda n: n.end_lineno - n.lineno) if scopes else tree
    statements = [n for n in ast.walk(scope) if isinstance(n, ast.stmt) and n.lineno <= line <= n.end_lineno
                  and not any(field in n._fields for field in ('body', 'handlers', 'cases'))]
    if len(statements) != 1:
        return None
    return getattr(scope, 'name', '@module'), statements[0]


@lru_cache(maxsize=64)
def _public_exports(module):
    """Read a literal __all__ from installed library source, without importing any module."""
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}(?:\.[A-Za-z][A-Za-z0-9_]{0,63}){0,5}', module):
        return ()
    for library in sorted({sysconfig.get_path('purelib'), sysconfig.get_path('platlib')}):
        root = Path(library).resolve()
        target = root.joinpath(*module.split('.'))
        for source in (target / '__init__.py', target.with_suffix('.py')):
            try:
                if not source.is_file() or not source.resolve().is_relative_to(root) or source.stat().st_size > 128000:
                    continue
                tree = ast.parse(source.read_bytes())
                for node in tree.body:
                    if (isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__all__' for t in node.targets)
                            and isinstance(node.value, (ast.Tuple, ast.List))):
                        return tuple(item.value for item in node.value.elts
                                     if isinstance(item, ast.Constant) and type(item.value) is str
                                     and re.fullmatch(r'[A-Za-z][A-Za-z_0-9]{0,63}', item.value))
            except (OSError, SyntaxError, ValueError, RuntimeError):
                continue
    return ()


def _public_callee(call, source, files):
    """Show canonical public library names only for simple, unambiguous source imports."""
    binding = public_import(call, ast.parse(source), files)
    if binding:
        module, exported = binding
        if exported in _public_exports(module) and redact_text(exported) == exported:
            return exported
    return '<call>'


def _statement_excerpt(statement, source, files):
    """Show fixed syntax, verified public callees and literal types, omitting source values."""
    value = getattr(statement, 'value', None)
    if isinstance(statement, ast.Raise):
        value = statement.exc
    if isinstance(value, ast.Await):
        value = value.value
        prefix = 'await '
    else:
        prefix = ''
    if isinstance(statement, (ast.Assign, ast.AnnAssign)):
        prefix = '<target> = ' + prefix
    elif isinstance(statement, (ast.Return, ast.Raise)):
        prefix = ('return ' if isinstance(statement, ast.Return) else 'raise ') + prefix

    def operand(expr):
        if isinstance(expr, ast.Constant):
            return '<' + type(expr.value).__name__ + '>'
        if isinstance(expr, ast.JoinedStr):
            return '<f-string>'
        return '<expression>'

    if not isinstance(value, ast.Call):
        if isinstance(value, ast.BinOp):
            operators = {ast.Add: '+', ast.Sub: '-', ast.Mult: '*', ast.Div: '/', ast.FloorDiv: '//',
                         ast.Mod: '%', ast.Pow: '**', ast.MatMult: '@', ast.BitOr: '|', ast.BitAnd: '&',
                         ast.BitXor: '^', ast.LShift: '<<', ast.RShift: '>>'}
            return prefix + f'{operand(value.left)} {operators[type(value.op)]} {operand(value.right)}'
        if isinstance(value, ast.Subscript):
            return prefix + f'{operand(value.value)}[{operand(value.slice)}]'
        if isinstance(value, ast.Attribute):
            return prefix + '<expression>.<attribute>'
        return None

    # Positional and keyword slots mirror the code, but comments, names and
    # literals (including SQL and interpolated strings) are never copied.
    args = [operand(arg) for arg in value.args[:8]]
    for kw in value.keywords[:8-len(args)]:
        label = '**' if kw.arg is None else ('type=' if kw.arg == 'type' else '<keyword>=')
        args.append(label + operand(kw.value))
    if len(value.args) + len(value.keywords) > len(args):
        args.append('...')
    return prefix + _public_callee(value, source, files) + '(' + ', '.join(args) + ')'


def priority_repair_evidence(task_input):
    """Compact already-delivered facts for the same Developer's one correction."""
    blocker = task_input.get('activeRepairFocus', {}).get('brief', {}).get('blockingPrerequisite') or {}
    sites = blocker.get('sourceSites') or []
    if not sites:
        return None
    case = next((row for row in blocker.get('failedCreationCases', ())
                 if 'account_count' in row or 'user_delta' in row), None)
    fields = ('toolPath','line','sourceRef','sourceSha256','statementExcerpt','observedCause','libraryContract')
    result = {'sourceSites': [{key: site[key] for key in fields if key in site} for site in sites[:3]],
              'httpStatuses': blocker['httpStatuses'][:4],
              'exceptionTypes': blocker['exceptionTypes'][:4], 'blockedCheckCount': blocker['blockedCheckCount']}
    if case:
        result['observedCreation'] = {key: case[key] for key in ('httpStatus','statusOrigin','user_delta','account_count')
                                      if key in case}
    return result


def linkable_sites(frames, files, *, environment=None):
    sites = []
    causes = {(frame['sourceSha256'], frame['line']): frame['observedCause']
              for frame in frames if 'observedCause' in frame}
    for site in diagnostic_sites(frames, files):
        try:
            site['line'] = int(site['line'])
            source = files[site['toolPath'].removeprefix('source/')]
            located = _frame_statement(source, site['line'])
            if located:
                ref = f"{site['toolPath']}::{located[0]}"
                if len(ref) <= 400 and redact_text(ref) == ref:
                    entry = {**site, 'sourceRef': ref}
                    excerpt = _statement_excerpt(located[1], source, files)
                    if excerpt:
                        entry['statementExcerpt'] = excerpt
                    value = located[1].exc if isinstance(located[1], ast.Raise) else getattr(located[1], 'value', None)
                    call = value.value if isinstance(value, ast.Await) else value
                    contract = call_parameters(call, source, files, environment)
                    if contract:
                        entry['libraryContract'] = contract
                    cause = causes.get((site['sourceSha256'], site['line']))
                    if cause:
                        entry['observedCause'] = cause
                    sites.append(entry)
        except (SyntaxError, ValueError, RecursionError):
            continue
    return sites


def changed_frame_refs(before, after, sites):
    """A statement still present after reformatting or an edit elsewhere provides no new evidence."""
    refs = set()
    for site in sites:
        path = site['toolPath'].removeprefix('source/')
        old, new = before.get(path), after.get(path)
        if (not old or not new or old == new or max(len(old), len(new)) > 128000
                or sha256(old).hexdigest() != site['sourceSha256']):
            continue
        try:
            located = _frame_statement(old, site['line'])
            if not located:
                continue
            name, statement = located
            fingerprint = ast.dump(statement)
            tree = ast.parse(new)
            scopes = [tree] if name == '@module' else [
                n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
            if len(scopes) != 1:
                continue
            if all(ast.dump(n) != fingerprint for n in ast.walk(scopes[0]) if type(n) is type(statement)):
                refs.add(f"{site['toolPath']}::{name}")
        except (SyntaxError, ValueError, RecursionError):
            continue
    return refs
