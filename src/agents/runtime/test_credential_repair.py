"""Value-free hints for a rejected Python test write, never a secret exemption.

Parse only bounded ASTs. Never execute, rewrite or approve the source. The
existing credential guard must check every replacement before any tool runs.
"""
import ast
from itertools import islice

from agents.runtime.generated_test_inventory import MAX_FILE_BYTES
from mcp_tools.tools.unit_runner import MAX_TEST_PASSWORD_LENGTH, TEST_DATA_MODULE
from orchestrator.core.source_credentials import credential_assignment_kind

POLICY = "VALUE_FREE_TEST_CREDENTIAL_REPAIR_V1"
MAX_NODES = 50_000
MAX_SITES = 16


def _names(target):
    pending, visited = [target], 0
    while pending and visited < 128:
        node = pending.pop()
        visited += 1
        if isinstance(node, ast.Name):
            yield node.id
        elif isinstance(node, ast.Attribute):
            yield node.attr
        elif isinstance(node, (ast.Tuple, ast.List)):
            pending.extend(node.elts)


def _values(node):
    if isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
        targets = node.targets if isinstance(node, ast.Assign) else (node.target,)
        for target in targets:
            for name in _names(target):
                yield name, node.value, target, 'ASSIGNMENT'
    elif isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                yield key.value, value, key, 'DICTIONARY_VALUE'
    elif isinstance(node, ast.keyword) and node.arg is not None:
        yield node.arg, node.value, node, 'KEYWORD_ARGUMENT'
    elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
        args = node.args
        positional = [*args.posonlyargs, *args.args]
        for argument, value in zip(positional[-len(args.defaults):], args.defaults):
            yield argument.arg, value, argument, 'DEFAULT_ARGUMENT'
        for argument, value in zip(args.kwonlyargs, args.kw_defaults):
            if value is not None:
                yield argument.arg, value, argument, 'DEFAULT_ARGUMENT'


def credential_repair_plan(arguments):
    """Describe recognized sites without retaining names or submitted values.

    Length/kind hints help preserve a boundary test's intent, but are not
    acceptance rules or evidence that a different string is semantically equal.
    Unknown expressions stay under the original guard and need model review.
    """
    result = {'policy': POLICY, 'coverage': 'DIRECT_CREDENTIAL_STRING_SITES_ONLY',
              'sites': [], 'truncated': False}
    path, content = arguments.get('path'), arguments.get('content')
    if not isinstance(path, str) or not path.endswith('.py') or not isinstance(content, str):
        return result
    try:
        if len(content.encode('utf-8')) > MAX_FILE_BYTES:
            return {**result, 'truncated': True}
        tree = ast.parse(content)
        nodes = list(islice(ast.walk(tree), MAX_NODES + 1))
    except (SyntaxError, ValueError, UnicodeError, RecursionError):
        return {**result, 'truncated': True}
    result['truncated'] = len(nodes) > MAX_NODES
    for node in nodes[:MAX_NODES]:
        for name, value, location, site_kind in _values(node):
            credential_kind = credential_assignment_kind(name)
            static = isinstance(value, ast.Constant) and isinstance(value.value, str)
            composed = isinstance(value, (ast.JoinedStr, ast.BinOp))
            if credential_kind is None or not (static or composed):
                continue
            row = {'sourceLine': location.lineno, 'siteKind': site_kind,
                   'credentialKind': credential_kind,
                   'valueCategory': 'STATIC_STRING' if static else 'COMPOSED_STRING',
                   'instructionCode': {
                       'PASSWORD': 'USE_RUNTIME_PASSWORD_FACTORY',
                       'HASH': 'READ_HASH_FROM_APPLICATION',
                       'TOKEN': 'USE_APPLICATION_RESPONSE_OR_RUNTIME_SYNTHETIC_DATA',
                       'SECRET': 'REMOVE_SUPPLIED_CREDENTIAL',
                   }[credential_kind]}
            if static and credential_kind == 'PASSWORD':
                length = len(value.value)
                if length <= MAX_TEST_PASSWORD_LENGTH:
                    kind = ('spaces' if length and set(value.value) == {' '} else
                            'digits' if length and value.value.isascii() and value.value.isdecimal() else
                            'unicode' if not value.value.isascii() else 'ascii')
                    row['factory'] = {'module': TEST_DATA_MODULE, 'function': 'new_password',
                                      'arguments': {'length': length, 'kind': kind}}
            if row in result['sites']:
                continue
            if len(result['sites']) >= MAX_SITES:
                result['truncated'] = True
                return result
            result['sites'].append(row)
    result['sites'].sort(key=lambda row: (row['sourceLine'], row['siteKind'], row['credentialKind']))
    return result
