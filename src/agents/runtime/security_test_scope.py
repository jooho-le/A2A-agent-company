"""Describe bounded runtime output checks and simple QA/Security tests in receipt-bound inputs.

Only a simple direct AppTestCase method with recognized operations is described.
Other arrangements (helpers, decorators, inheritance, custom assertions) are
unknown. Absence of a recognized limitation is never positive test evidence.
"""
import ast
from pathlib import PurePosixPath


EXPOSURE_CHANNELS = ('SUCCESS_RESPONSE', 'REJECTION_RESPONSE', 'STORAGE_ERROR_RESPONSE', 'REQUEST_LOGS', 'STORAGE_ERROR_REQUEST_LOGS')


def generated_output_coverage(cases):
    """Only runner observations from tests bound to this one requirement are eligible."""
    priority = {'PASS': 0, 'UNVERIFIED': 1, 'FAIL': 2}
    states = {}
    for case in cases:
        for channel, state in case.get('outputChecks', {}).items():
            if channel not in EXPOSURE_CHANNELS:
                continue
            verdict = ('FAIL' if state == 'EXPOSED' else 'PASS' if state == 'CHECKED'
                       and case['outcome'] == 'PASS' else 'UNVERIFIED')
            if channel not in states or priority[verdict] > priority[states[channel]]:
                states[channel] = verdict
    return {channel: states.get(channel, 'UNVERIFIED') for channel in EXPOSURE_CHANNELS}


def generated_requirement_coverage(key, cases, hash_policy=None):
    if key == 'REQ-006':
        coverage = generated_output_coverage(cases)
        return coverage, ['OUTPUT_NOT_OBSERVED_' + k for k, v in coverage.items() if v == 'UNVERIFIED']
    passed = [row.get('signupChecks', {}) for row in cases if row['outcome'] == 'PASS']
    if key == 'REQ-003':
        return None, ([] if any(facts.get('canonicalEmailUniqueKey') is True for facts in passed)
                      else ['DB_CONSTRAINT_NOT_OBSERVED'])
    if key == 'REQ-005':
        hashes = [facts['storedHashParameters'] for facts in passed if 'storedHashParameters' in facts]
        complete = bool(hashes and hash_policy and (not hash_policy.get('uniqueSalt') or
                        any('distinctSalts' in snapshot for snapshot in hashes)))
        return None, ([] if complete else ['HASH_PARAMETERS_NOT_OBSERVED'])
    return None, []


def protected_evidence_covers(key, cases):
    """Cross-check missing generated evidence against completed measurements in the protected Security scope."""
    measured = [row for row in cases if row['outcome'] == 'PASS' and row.get('observationStatus') == 'COMPLETE'
                and row.get('evidenceRefs')]
    for row in measured:
        by_case = {}
        for observation in row.get('observations', ()):
            if observation.get('status') == 'PASS':
                by_case.setdefault(observation['caseId'], set()).add(observation['itemId'])
        if key == 'REQ-003' and any({'canonical', 'unique'} <= fields for fields in by_case.values()):
            return True
        if key == 'REQ-005' and {'argon2id', 'distinct_salts'} <= by_case.get('storage', set()):
            accounts = sum({'m', 't', 'p', 'correct_password'} <= fields for fields in by_case.values())
            if accounts >= 2:
                return True
        if key == 'REQ-006':
            output = {'password_exposed', 'hash_exposed'}
            if (all(output | {'response_status'} <= by_case.get(case, set())
                    for case in ('success', 'invalid_email', 'db_error'))
                    and 'injected' in by_case.get('db_error', set())
                    and output <= by_case.get('application_logs', set())):
                return True
    return False


def measured_hash_complies(facts, policy):
    """A receipt holds actual values only. The frozen scenario supplies the accepted minimums."""
    return (facts['argon2id'] and facts['verified'] and policy['algorithm'] == 'argon2id'
            and (not policy.get('uniqueSalt') or facts.get('distinctSalts') is not False)
            and all(facts[k] >= policy[k] for k in ('memoryKiB', 'iterations', 'parallelism')))


def _assertion_limit(method):
    if method.decorator_list or any(not isinstance(stmt, (ast.Expr, ast.Assign)) for stmt in method.body):
        return None
    observed = set()
    for node in ast.walk(method):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == 'new_password':
            continue
        if not isinstance(func, ast.Attribute):
            return None
        if func.attr == 'post':
            if not (isinstance(func.value, ast.Attribute) and func.value.attr == 'client'
                    and isinstance(func.value.value, ast.Name) and func.value.value.id == 'self'):
                return None
            continue
        if func.attr in {'first', 'query', 'filter_by', 'startswith'}:
            continue
        if (not isinstance(func.value, ast.Name) or func.value.id != 'self' or len(node.args) != 1
                or node.keywords):
            return None
        arg, = node.args
        if func.attr == 'assertIsNotNone' and isinstance(arg, ast.Name):
            observed.add('PRESENT')
        elif (func.attr == 'assertTrue' and isinstance(arg, ast.Compare)
                and isinstance(arg.left, ast.BinOp) and isinstance(arg.left.op, ast.FloorDiv)
                and isinstance(arg.left.right, ast.Constant) and arg.left.right.value == 100
                and isinstance(arg.left.left, ast.Attribute) and arg.left.left.attr == 'status_code'
                and len(arg.ops) == 1 and isinstance(arg.ops[0], ast.Eq)
                and isinstance(arg.comparators[0], ast.Constant)):
            observed.add('HTTP_STATUS')
        elif (func.attr == 'assertTrue' and isinstance(arg, ast.Call) and isinstance(arg.func, ast.Attribute)
                and arg.func.attr == 'startswith' and isinstance(arg.func.value, ast.Attribute)
                and arg.func.value.attr == 'password_hash' and len(arg.args) == 1
                and isinstance(arg.args[0], ast.Constant) and arg.args[0].value == '$argon2id$'):
            observed.add('HASH_PREFIX')
        else:
            return None
    if observed == {'HTTP_STATUS'}:
        return 'HTTP_STATUS_ONLY'
    if 'HASH_PREFIX' in observed and observed <= {'HASH_PREFIX', 'PRESENT'}:
        return 'HASH_PREFIX_ONLY'
    return None


def _sequential_signup_only(method):
    """Recognize straight-line requests in ordinary fixture tests; custom helpers stay unknown."""
    if method.decorator_list or any(not isinstance(stmt, (ast.Expr, ast.Assign)) for stmt in method.body):
        return None
    if any(isinstance(stmt, ast.Assign) and any(not isinstance(t, ast.Name) or t.id == 'self' for t in stmt.targets)
           for stmt in method.body):
        return None
    posts = 0
    for node in ast.walk(method):
        if isinstance(node, (ast.Lambda, ast.IfExp, ast.comprehension, ast.NamedExpr)):
            return None
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in ('len', 'new_password'):
            continue
        if not isinstance(func, ast.Attribute):
            return None
        if (func.attr == 'post' and isinstance(func.value, ast.Attribute) and func.value.attr == 'client'
                and isinstance(func.value.value, ast.Name) and func.value.value.id == 'self'
                and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == '/api/auth/signup'):
            posts += 1
        elif func.attr in {'assertEqual', 'assertTrue', 'assertFalse', 'assertIn', 'assertIsNotNone', 'signup_email_state'}:
            if not isinstance(func.value, ast.Name) or func.value.id != 'self':
                return None
        elif func.attr not in {'query', 'filter_by', 'first', 'all', 'count', 'strip', 'lower', 'upper'}:
            return None
    return 'SEQUENTIAL_SIGNUPS_ONLY' if posts >= 2 else None


def simple_assertion_limits(files, directory, *, qa=False):
    limits = {}
    for name, contents in files.items():
        path = PurePosixPath(name)
        if path.parent != PurePosixPath(directory) or path.suffix != '.py' or len(contents) > 128_000:
            continue
        try:
            tree = ast.parse(contents)
        except (ValueError, SyntaxError, RecursionError):
            continue
        # Ambiguous rebinding or dynamic unittest discovery cannot prove a limitation.
        if any(not isinstance(node, (ast.Import, ast.ImportFrom, ast.ClassDef)) for node in tree.body):
            continue
        classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
        if qa:
            imports = [(node, name) for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
                       for name in node.names]
            bases = [(node, name) for node, name in imports if (name.asname or name.name) == 'AppTestCase']
            if (len(bases) != 1 or not isinstance(bases[0][0], ast.ImportFrom) or bases[0][0].level != 0
                    or bases[0][0].module != '_host_app_test' or bases[0][1].name != 'AppTestCase'
                    or any(name.name == '*' for _, name in imports) or any(c.name == 'AppTestCase' for c in classes)):
                continue
        if len({node.name for node in classes}) != len(classes):
            continue
        for cls in classes:
            if (any(not isinstance(method, ast.FunctionDef) or not method.name.startswith('test')
                    for method in cls.body) or len({n.name for n in cls.body}) != len(cls.body)
                    or cls.decorator_list or len(cls.bases) != 1
                    or not isinstance(cls.bases[0], ast.Name) or cls.bases[0].id != 'AppTestCase'):
                continue
            for method in cls.body:
                if isinstance(method, ast.FunctionDef) and method.name.startswith('test'):
                    limit = _sequential_signup_only(method) if qa else _assertion_limit(method)
                    if limit:
                        limits[f'{path.stem}.{cls.name}.{method.name}'] = limit
    return limits
