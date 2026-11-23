"""Inert public startup facts and bound build-repair guidance, shared by both actors.

Never executes Source, restores files, creates a table, or declares readiness.
An advisory observation is deliberately not another output-correction gate.
"""
import ast
import re
from collections.abc import Mapping
from copy import deepcopy

POLICY = 'IMMUTABLE_STARTUP_FACTS_AND_BOUND_DATABASE_REPAIR_V2'
MAX_BYTES, MAX_NODES = 128_000, 10_000
_INSTRUCTION = (
    'Before rewriting the app, read the application, database and model modules. Preserve existing features '
    'and public entrypoints. Register the mapped User model on its actual Base before application-owned '
    'initialization runs. Preserve or restore a connected lifespan/startup hook (or equivalent initialization) '
    'using the same engine/database that the original get_db supplies. Keep DATABASE_URL configuration; '
    'keep the approved GET readiness path on the actual entrypoint app. A catch-all mount at / registered '
    'before that handler takes the request first; preserve the readiness handler before such a mount, or '
    'include a compatible APIRouter while preserving its routes and application lifecycle. '
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


def _facts(files, bindings, ready_path=None):
    """Only identifiers, coordinates and fixed field names leave the AST reader."""
    calls, hooks, module_initializers, ready_sites, root_mounts = [], [], 0, [], []
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
            if (ready_path is not None and isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == bindings['application']['attribute']):
                arg = node.args[0] if node.args else next((k.value for k in node.keywords if k.arg == 'path'), None)
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    site = {'toolPath': 'source/' + actual_path, 'lineNumber': node.lineno}
                    if node.func.attr == 'get' and arg.value == ready_path and len(ready_sites) < 8:
                        ready_sites.append(site)
                    elif node.func.attr == 'mount' and arg.value in ('', '/') and len(root_mounts) < 8:
                        root_mounts.append(site)
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
            'moduleInitializationObserved': bool(module_initializers),
            'readyRouteSites': ready_sites, 'rootMountSites': root_mounts,
            'rootMountBeforeReadyRoute': bool(ready_sites and root_mounts and min(s['lineNumber'] for s in root_mounts)
                                              < min(s['lineNumber'] for s in ready_sites))}


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
    ready_check = contract.get('externalContract', {}).get('app', {}).get('readyCheck')
    ready = re.match(r'GET\s+(/\S*)', ready_check) if isinstance(ready_check, str) else None
    facts = _facts(files, bindings, ready.group(1) if ready else None)
    if facts is None:
        return None
    return {'policy': POLICY, 'bindings': bindings, 'readyCheck': 'GET ' + ready.group(1) if ready else None,
            'observedStartingSource': facts,
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
        rules = [] if measured is None else [d.get('ruleId') for d in measured['buildDiagnostics']
                 if d.get('ruleId') in ('SCN001_USER_TABLE_REQUIRED', 'SCN001_READY_ENDPOINT_REQUIRED')]
        if not rules:
            continue
        for rule in dict.fromkeys(rules):
            observation = ('The actual Host build observed no mapped user table through get_db after startup. '
                           if rule == 'SCN001_USER_TABLE_REQUIRED' else
                           'The actual Host build observed a non-2xx readiness response. Inspect the entrypoint app '
                           'and route registration order, including an earlier mount at / and the mounted app. ')
            targets.append({'issueId': issue['issueId'], 'ruleId': rule,
                **{k: measured[k] for k in ('codeVersion', 'sourceArtifactId', 'reportArtifactId')},
                'bindings': context['bindings'], 'readBeforeEditing': [b['toolPath'] for b in context['bindings'].values()],
                'instruction': observation + _INSTRUCTION})
    if targets:
        value['activeRepairFocus'] = {**task_input['activeRepairFocus'],
            'brief': {**brief, 'startupRepairTargets': targets}}
    return value


def preservation_notice_for(checkpoint, contract, *, deadline_monotonic=None):
    context = _starting(checkpoint, contract)
    reader = getattr(checkpoint, 'review_source_files', None)
    if context is None or not callable(reader) or not (
            context['observedStartingSource']['metadataInitializationSites'] or context['observedStartingSource']['readyRouteSites']):
        return None
    watched = {b['toolPath'] for b in context['bindings'].values()}

    def notice(changed_paths):
        if not watched.intersection(changed_paths):
            return None
        try:
            ready = context['readyCheck']
            current = _facts(reader(deadline_monotonic=deadline_monotonic), context['bindings'],
                             ready[4:] if ready else None)
        except Exception:
            # Optional advice cannot hide a build failure or become an independent retry.
            # Checkpoint and actual build still validate files/budget/identity.
            return None
        if current is None:
            return None
        if context['observedStartingSource']['metadataInitializationSites'] and not current['metadataInitializationSites']:
            reason = 'STARTING_DATABASE_INITIALIZATION_NOT_OBSERVED'
        elif (context['observedStartingSource']['applicationLifecycleSites']
                and not current['applicationLifecycleSites'] and not current['moduleInitializationObserved']):
            reason = 'STARTING_LIFECYCLE_CONNECTION_NOT_OBSERVED'
        elif context['observedStartingSource']['readyRouteSites'] and not current['readyRouteSites']:
            reason = 'STARTING_READY_ROUTE_NOT_OBSERVED'
        elif current['rootMountBeforeReadyRoute']:
            reason = 'STARTING_READY_ROUTE_MAY_BE_SHADOWED'
        else:
            return None
        return {'startupPreservationNotice': {'policy': POLICY, 'reasonCode': reason, 'verdict': 'NOT_EVALUATED',
            'readBeforeEditing': list(context['bindings'][key]['toolPath'] for key in context['bindings']),
            'observedStartingSource': deepcopy(context['observedStartingSource']), 'observedCurrentSource': current,
            'instruction': _INSTRUCTION}}
    return notice
