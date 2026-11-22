"""Inert public startup facts and bound build-repair guidance, shared by both actors.

Never executes Source, restores files, creates a table, or declares readiness.
An advisory observation is deliberately not another output-correction gate.
"""
import ast
from collections.abc import Mapping
from copy import deepcopy

POLICY = 'IMMUTABLE_STARTUP_FACTS_AND_BOUND_DATABASE_REPAIR_V1'
MAX_BYTES, MAX_NODES = 128_000, 10_000
_INSTRUCTION = (
    'Before rewriting the app, read the application, database and model modules. Preserve existing features '
    'and public entrypoints. Register the mapped User model on its actual Base before application-owned '
    'initialization runs. Preserve or restore a connected lifespan/startup hook (or equivalent initialization) '
    'using the same engine/database that the original get_db supplies. Keep DATABASE_URL configuration; '
    'do not hardcode another database, create tables in QA/test fixtures, or ask the Host to create them. '
    'AST sites describe starting code, not proof of execution. Only the actual Host build and subsequent '
    'QA/Security can establish recovery; alternative correct initialization is allowed.')


def _identifier(value):
    return isinstance(value, str) and len(value) <= 128 and value.isidentifier()


def _module_path(value):
    if not isinstance(value, str) or len(value) > 256 or not all(_identifier(p) for p in value.split('.')):
        return None
    return value.replace('.', '/') + '.py'


def _bindings(contract):
    external = contract.get('externalContract') if isinstance(contract, Mapping) else None
    if not isinstance(external, Mapping):
        return None
    app, db, model = (external.get(key) for key in ('app', 'database', 'userModel'))
    if (not all(isinstance(item, Mapping) for item in (app, db, model))
            or app.get('framework') != 'FastAPI' or model.get('orm') != 'SQLAlchemy'):
        return None
    specs = (('application', app, 'attribute'), ('database', db, 'dependency'), ('userModel', model, 'class'))
    result = {}
    for label, spec, name in specs:
        path = _module_path(spec.get('module'))
        if path is None or not _identifier(spec.get(name)):
            return None
        result[label] = {'toolPath': 'source/' + path, name: spec[name]}
    # The name of the environment variable is public. Its value is never read.
    if db.get('urlEnv') != 'DATABASE_URL':
        return None
    result['database']['urlEnv'] = 'DATABASE_URL'
    return result


def _name(node):
    if isinstance(node, ast.Name) and _identifier(node.id):
        return node.id
    if isinstance(node, ast.Attribute) and _identifier(node.attr):
        parent = _name(node.value)
        if parent and len(parent) < 256:
            return parent + '.' + node.attr
    return None


def _facts(files, bindings):
    """Only identifiers, coordinates and fixed field names leave the AST reader."""
    calls, hooks, module_initializers = [], [], 0
    for tool_path in dict.fromkeys(b['toolPath'] for b in bindings.values()):
        path = tool_path.removeprefix('source/')
        # Support either a Python module or its package __init__, with no filesystem access.
        actual_path = path if path in files else path[:-3] + '/__init__.py'
        raw = files.get(actual_path)
        if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
            return None
        try:
            tree = ast.parse(raw)
            nodes = list(ast.walk(tree))
            if len(nodes) > MAX_NODES:
                return None
        except (SyntaxError, UnicodeError, ValueError, RecursionError):
            return None
        parents = {child: node for node in nodes for child in ast.iter_child_nodes(node)}
        for node in nodes:
            if not isinstance(node, ast.Call):
                continue
            name = _name(node.func)
            if name and name.endswith('.metadata.create_all'):
                current = parents.get(node)
                scoped = False
                while current is not None:
                    scoped |= isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda))
                    current = parents.get(current)
                module_initializers += not scoped
                if len(calls) < 8:
                    row = {'toolPath': 'source/' + actual_path, 'lineNumber': node.lineno,
                           'call': name, 'scope': 'FUNCTION_OR_CLASS' if scoped else 'MODULE'}
                    bind = next((_name(kw.value) for kw in node.keywords if kw.arg == 'bind'), None)
                    if bind:
                        row['bindName'] = bind
                    calls.append(row)
            if tool_path != bindings['application']['toolPath']:
                continue
            parent = parents.get(node)
            targets = parent.targets if isinstance(parent, ast.Assign) else (
                [parent.target] if isinstance(parent, ast.AnnAssign) else [])
            owns_app = any(isinstance(t, ast.Name) and t.id == bindings['application']['attribute'] for t in targets)
            if owns_app:
                for kw in node.keywords:
                    if kw.arg == 'lifespan' and _name(kw.value) and len(hooks) < 8:
                        hooks.append({'toolPath': 'source/' + actual_path, 'lineNumber': node.lineno,
                                      'lifespanName': _name(kw.value)})
            if (name == bindings['application']['attribute'] + '.on_event' and node.args
                    and isinstance(node.args[0], ast.Constant) and node.args[0].value == 'startup' and len(hooks) < 8):
                hooks.append({'toolPath': 'source/' + actual_path, 'lineNumber': node.lineno, 'event': 'startup'})
    return {'metadataInitializationSites': calls, 'applicationLifecycleSites': hooks,
            'moduleInitializationObserved': bool(module_initializers)}


def _starting(checkpoint, contract):
    bindings = _bindings(contract)
    reader = getattr(checkpoint, 'starting_source_files', None)
    if bindings is None or not callable(reader):
        return None
    files = reader()
    for binding in bindings.values():
        path = binding['toolPath'].removeprefix('source/')
        package = path[:-3] + '/__init__.py'
        if path not in files and package in files:
            binding['toolPath'] = 'source/' + package
    facts = _facts(files, bindings)
    if facts is None:
        return None
    return {'policy': POLICY, 'bindings': bindings, 'observedStartingSource': facts,
            'instruction': _INSTRUCTION}


def with_startup_context(task_input, checkpoint, contract):
    context = _starting(checkpoint, contract)
    if context is None:
        return task_input
    value = {**task_input, 'startingSourceStartup': context}
    fix = task_input.get('fixRequest', {})
    brief = task_input.get('activeRepairFocus', {}).get('brief', {})
    selected = {t.get('issueId') for t in brief.get('targets', [])}
    # Reuse the exact Host report/source/version/failure-kind checks. Never promote
    # a diagnostic from old/rejected history, a model summary or another snapshot.
    from agents.runtime.developer_input import _build_failure
    targets = []
    for issue in fix.get('issues', []):
        if not isinstance(issue, dict) or issue.get('issueId') not in selected:
            continue
        measured = _build_failure(issue, fix.get('inputArtifacts', []))
        if measured is None or not any(d.get('ruleId') == 'SCN001_USER_TABLE_REQUIRED'
                                      for d in measured['buildDiagnostics']):
            continue
        targets.append({'issueId': issue['issueId'], 'ruleId': 'SCN001_USER_TABLE_REQUIRED',
            **{k: measured[k] for k in ('codeVersion', 'sourceArtifactId', 'reportArtifactId')},
            'bindings': context['bindings'], 'readBeforeEditing': [b['toolPath'] for b in context['bindings'].values()],
            'instruction': 'The actual Host build observed no mapped user table through get_db after startup. ' + _INSTRUCTION})
    if targets:
        value['activeRepairFocus'] = {**task_input['activeRepairFocus'],
            'brief': {**brief, 'startupRepairTargets': targets}}
    return value


def preservation_notice_for(checkpoint, contract, *, deadline_monotonic=None):
    context = _starting(checkpoint, contract)
    reader = getattr(checkpoint, 'review_source_files', None)
    if context is None or not callable(reader) or not context['observedStartingSource']['metadataInitializationSites']:
        return None
    watched = {b['toolPath'] for b in context['bindings'].values()}

    def notice(changed_paths):
        if not watched.intersection(changed_paths):
            return None
        try:
            current = _facts(reader(deadline_monotonic=deadline_monotonic), context['bindings'])
        except Exception:
            # Optional advice cannot hide a build failure or become an independent retry.
            # Checkpoint and actual build still validate files/budget/identity.
            return None
        if current is None:
            return None
        if not current['metadataInitializationSites']:
            reason = 'STARTING_DATABASE_INITIALIZATION_NOT_OBSERVED'
        elif (context['observedStartingSource']['applicationLifecycleSites']
                and not current['applicationLifecycleSites'] and not current['moduleInitializationObserved']):
            reason = 'STARTING_LIFECYCLE_CONNECTION_NOT_OBSERVED'
        else:
            return None
        return {'startupPreservationNotice': {'policy': POLICY, 'reasonCode': reason, 'verdict': 'NOT_EVALUATED',
            'readBeforeEditing': list(context['bindings'][key]['toolPath'] for key in context['bindings']),
            'observedStartingSource': deepcopy(context['observedStartingSource']), 'observedCurrentSource': current,
            'instruction': _INSTRUCTION}}
    return notice
