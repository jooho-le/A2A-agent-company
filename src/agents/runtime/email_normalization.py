"""Bounded, inert email input-flow observations; not an execution verdict."""
import ast
from hashlib import sha256

from agents.llm.recovery import safe_source_ref

POLICY = 'BOUND_CANONICAL_INPUT_AND_PARSER_ORDER_V1'
REASON = 'EMAIL_VALIDATED_BEFORE_CANONICAL_TRIM'
PARSER = 'email_validator.validate_email'
GUIDANCE = (
    'Read the named sourceRef and parser call before editing. The approved emailPolicy requires '
    'trimming the original email before format validation, then using the same canonical value for '
    'lookup, insert and uniqueness. Do not assume email_validator.validate_email trims surrounding '
    'whitespace. Normalize before calling it (including inside a helper), not after it returns. '
    'Preserve invalid-email rejection, response privacy, database uniqueness and unrelated behavior. '
    'Changing only DB exception handling or rewriting the same function does not change this input path. '
    'Source coordinates are static candidates; only revalidation proves a correction. Do not add dot/plus alias removal.')


def _key(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _key(node.value)
        return base + '.' + node.attr if base else None
    return None


def scan(trees, contract):
    """Return closed call coordinates and definite missing trim on known email flows.

    Unknown preprocessing, branching and model validators remain for measured
    verification. No module is imported from the supplied source trees.
    """
    from agents.runtime.ready_path_diff import _SourceView, _bound_names, _module
    if not contract.get('emailPolicy', {}).get('trimWhitespace'):
        return []
    external = contract.get('externalContract', {})
    app, api = external.get('app', {}), external.get('signupApi', {})
    entry = app.get('module'), app.get('attribute')
    if not all(isinstance(v, str) and v for v in entry):
        return []
    view = _SourceView(trees, (api.get('method'), api.get('path')), entry)
    if len(view.units) > 256:
        return []
    model_preprocessing = set()
    for path, tree in trees.items():
        module = _module(path)
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            def may_preprocess_email(call):
                if not isinstance(call, ast.Call):
                    return False
                method = (_key(call.func) or '').split('.')[-1]
                if method in ('model_validator', 'root_validator'):
                    return True
                if method in ('field_validator', 'validator'):
                    return not call.args or any(not isinstance(a, ast.Constant) or a.value in ('email', '*')
                                                for a in call.args)
                return method == 'ConfigDict' and any(k.arg is None or k.arg == 'str_strip_whitespace'
                    and not (isinstance(k.value, ast.Constant) and k.value.value is False) for k in call.keywords)
            if any(may_preprocess_email(n) for n in ast.walk(node)):
                model_preprocessing.add(module + '.' + node.name)
    inputs = {name: {} for name in view.units}

    def resolve(module, call, locals_):
        first = (_key(call.func) or '').partition('.')[0]
        if first in locals_:
            return ''
        history = view.import_history[module].get(first, ())
        if history and history[-1][1] is None:
            return module + '.' + (_key(call.func) or '')
        return view.resolve(module, call.func)

    for _round in range(8):
        changed, findings = False, []
        for name, (path, fn) in sorted(view.units.items()):
            module = _module(path)
            arguments = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
            locals_ = set(_bound_names([fn.args, *fn.body]))
            uncertain_models = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args)
                                if a.annotation and view.resolve(module, a.annotation) in model_preprocessing}
            states = dict(inputs[name])

            def value(expr, state):
                key = _key(expr)
                if key in state:
                    return state[key]
                if (isinstance(expr, ast.Attribute) and expr.attr == 'email'
                        and isinstance(expr.value, ast.Name) and expr.value.id in arguments
                        and expr.value.id not in uncertain_models):
                    return frozenset()
                if (isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
                        and expr.func.attr in ('strip', 'lower', 'casefold') and not expr.args and not expr.keywords):
                    previous = value(expr.func.value, state)
                    return previous | {expr.func.attr} if previous is not None else None
                return None

            def expression(expr, state, definite=True):
                nonlocal changed
                if expr is None or isinstance(expr, (ast.Lambda, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    return
                if isinstance(expr, ast.Call):
                    target = resolve(module, expr, locals_)
                    arg = expr.args[0] if expr.args else next((k.value for k in expr.keywords if k.arg == 'email'), None)
                    if target == PARSER and arg is not None:
                        transforms = value(arg, state) if definite else None
                        ref = 'source/' + path + '::' + fn.name
                        if safe_source_ref(ref) and len(findings) < 64:
                            findings.append({'sourceRef': ref, 'toolPath': 'source/' + path,
                                'line': expr.lineno, 'functionLine': fn.lineno, 'parser': PARSER,
                                'trimBeforeValidationObserved': None if transforms is None else 'strip' in transforms,
                                'activeSignupPath': ref in view.active_refs,
                                'reviewRevision': sha256(ast.dump(fn).encode()).hexdigest()})
                    if target in view.units:
                        callee = view.units[target][1]
                        params = [a.arg for a in (*callee.args.posonlyargs, *callee.args.args)]
                        bound = dict(zip(params, expr.args))
                        bound.update({k.arg: k.value for k in expr.keywords if k.arg in params})
                        for param, operand in bound.items():
                            observed = value(operand, state) if definite else None
                            if observed is None:
                                continue  # no established email provenance yet
                            if param not in inputs[target]:
                                inputs[target][param] = observed
                                changed = True
                            else:
                                old = inputs[target][param]
                                merged = old & observed if old is not None and observed is not None else None
                                if merged != old:
                                    inputs[target][param] = merged
                                    changed = True
                for child in ast.iter_child_nodes(expr):
                    expression(child, state, definite)

            def statements(body, state, definite=True):
                for node in body:
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        continue
                    if isinstance(node, (ast.Assign, ast.AnnAssign)):
                        expression(node.value, state, definite)
                        observed = value(node.value, state) if definite else None
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            key = _key(target)
                            if key:
                                state[key] = observed
                    elif isinstance(node, ast.Try):
                        # Calls in the try execute before its exception handlers.
                        statements(node.body, state, definite)
                        for handler in node.handlers:
                            statements(handler.body, dict(state), False)
                        statements(node.orelse, state, False)
                        statements(node.finalbody, state, False)
                    elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Match)):
                        expression(getattr(node, 'test', None), state, definite)
                        for field in ('body', 'orelse'):
                            statements(getattr(node, field, ()), dict(state), False)
                        # An unknown branch can transform or overwrite a local input.
                        for key in _bound_names([node]):
                            state[key] = None
                    else:
                        expression(node, state, definite)
            statements(fn.body, states)
        if not changed:
            break
    return findings


def context(site):
    return {k: site[k] for k in ('sourceRef', 'parser', 'trimBeforeValidationObserved')}


def repair_targets(issues):
    result = []
    for row in issues:
        data = row.get('emailNormalizationContext')
        if row.get('reasonCode') != REASON or not valid_context(data):
            continue
        ref = data['sourceRef']
        if ref.partition('::')[0] != 'source/' + row.get('repairSourcePath', ''):
            continue
        target = {'toolPath': ref.partition('::')[0], 'sourceRef': ref,
            'lineNumber': row.get('lineNumber'), 'parser': PARSER,
            'requiredOrder': ['TRIM_WHITESPACE', 'CASE_NORMALIZATION', 'FORMAT_VALIDATION', 'LOOKUP_AND_INSERT'],
            'instruction': GUIDANCE}
        if target not in result:
            result.append(target)
    return result[:16]


def valid_context(value):
    return (isinstance(value, dict) and safe_source_ref(value.get('sourceRef'))
            and value.get('parser') == PARSER and value.get('trimBeforeValidationObserved') is False)


def measured_rejections(fix, configuration, contract):
    """Selected, current, authorized observations only; no hidden final cases."""
    from agents.runtime.repair_input_failures import _bound_row
    if contract.get('scenarioKey') != 'SCN-001' or not contract.get('emailPolicy', {}).get('trimWhitespace'):
        return None
    requirements = {r.get('requirementId') for r in contract.get('requirements', ()) if r.get('key') == 'REQ-003'}
    sources = [a['record'] for a in fix.get('inputArtifacts', ()) if a.get('artifactType') == 'SOURCE'
               and isinstance(a.get('record'), dict) and a.get('artifactId') == a['record'].get('artifactId')]
    if len(sources) != 1:
        return None
    source = sources[0]
    if type(source.get('codeVersion')) not in (int, float) or source['codeVersion'] < 1:
        return None
    rejected = []
    for row in fix.get('validationFeedback', ()):
        if (row.get('requirementId') not in requirements or row.get('role') not in ('QA', 'SECURITY')
                or not _bound_row(row, source, configuration.get('protectedTestSuiteRef'))):
            continue
        observations = row.get('observations') or ()
        canonical = {o.get('caseId') for o in observations if o.get('itemId') == 'canonical'
                     and o.get('status') in ('FAIL', 'NOT_RUN', 'UNVERIFIED')}
        for obs in observations:
            actual = obs.get('actual')
            if (obs.get('caseId') in canonical and obs.get('itemId') == 'http_status'
                    and obs.get('kind') == 'assertion' and obs.get('status') == 'FAIL'
                    and type(actual) in (int, float) and 400 <= actual < 500 and int(actual) == actual):
                rejected.append({**{k: row[k] for k in ('role', 'testId', 'requirementId', 'reportArtifactId')},
                                 'caseId': obs['caseId'], 'httpStatus': int(actual)})
    if not rejected:
        return None
    return {'sourceArtifactId': source['artifactId'], 'codeVersion': source['codeVersion'],
        'snapshotSha256': source['snapshotSha256'], 'failedCaseCount': len(rejected), 'failedCases': rejected[:16],
        'instruction': ('These selected checks recorded a rejected canonical-account creation, not a duplicate '
                        'that should simply be refused. Existing QA duplicate-rejection PASS does not establish '
                        'successful canonical creation. Candidate source sites below are static, not exception frames. '
                        + GUIDANCE)}


def attach_source_sites(group, files, contract):
    """Read only already-authorized immutable baseline bytes; never import Source."""
    from agents.runtime.ready_path_diff import _bounded_tree
    trees, remaining = {}, 1_048_576
    for path, raw in sorted(files.items()):
        if (len(trees) >= 64 or not isinstance(raw, bytes) or len(raw) > min(128_000, remaining)
                or not safe_source_ref('source/' + path + '::@module')):
            continue
        try:
            tree = ast.parse(raw)
        except (SyntaxError, ValueError, UnicodeError, RecursionError):
            continue
        if _bounded_tree(tree):
            trees[path] = tree
            remaining -= len(raw)
    sites = []
    for row in scan(trees, contract):
        if not row['activeSignupPath']:
            continue
        site = {k: row[k] for k in ('sourceRef', 'toolPath', 'line', 'parser', 'trimBeforeValidationObserved')}
        site['sourceSha256'] = sha256(files[row['toolPath'].removeprefix('source/')]).hexdigest()
        site['evidenceKind'] = 'STATIC_CANDIDATE_ON_SELECTED_SNAPSHOT'
        sites.append(site)
    group['sourceSites'] = sites[:8]
