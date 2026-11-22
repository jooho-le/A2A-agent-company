"""Recognize edits confined to discarded text in a straight-line Python handler."""
import ast
from io import BytesIO
import re
import tokenize


def _name(node, value):
    return isinstance(node, ast.Name) and node.id == value


def _safe_bindings(tree):
    """Require the usual imports and defer shadowed functions, module writes and dynamic bindings."""
    imports = set()
    names = {'re', 'str', 'JSONResponse'}
    members = {n.value for n in ast.walk(tree) if isinstance(n, ast.Attribute) and _name(n.value, 're')}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                binding = alias.asname or alias.name.split('.')[0]
                if binding == '*' or (alias.name == 're' and alias.asname is not None):
                    return False
                if binding not in names:
                    continue
                if node in tree.body and alias.asname is None and (
                    isinstance(node, ast.Import) and alias.name == 're'
                    or isinstance(node, ast.ImportFrom) and not node.level and alias.name == 'JSONResponse'
                    and node.module in ('fastapi.responses', 'starlette.responses')
                ):
                    imports.add(binding)
                else:
                    return False
        elif isinstance(node, ast.Name):
            if node.id == 're' and node not in members:
                return False
            if node.id in names and not isinstance(node.ctx, ast.Load):
                return False
            if node.id in {'exec', 'eval', 'setattr', 'delattr', 'globals', 'locals', 'vars'}:
                return False
        elif isinstance(node, ast.Attribute) and _name(node.value, 're'):
            if not isinstance(node.ctx, ast.Load) or node.attr not in {'sub', 'IGNORECASE'}:
                return False
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.ExceptHandler)) and node.name in names:
            return False
        elif isinstance(node, ast.arg) and node.arg in names:
            return False
    return {'re', 'JSONResponse'} <= imports


def _discarded_operands(function):
    # Scope intentionally narrow: no nested code, branches, global writes, calls
    # with callbacks, or reads of the rewritten local while constructing the response.
    if len(function.body) != 3:
        return ()
    initial, assign, ret = function.body
    if not (isinstance(initial, ast.Assign) and isinstance(assign, ast.Assign)
            and len(initial.targets) == len(assign.targets) == 1 and isinstance(assign.targets[0], ast.Name)
            and _name(initial.targets[0], assign.targets[0].id) and isinstance(initial.value, ast.Call)
            and _name(initial.value.func, 'str') and not initial.value.keywords and len(initial.value.args) == 1
            and isinstance(initial.value.args[0], ast.Name) and isinstance(ret, ast.Return)
            and isinstance(ret.value, ast.Call) and _name(ret.value.func, 'JSONResponse')):
        return ()
    for expr in (*ret.value.args, *(kw.value for kw in ret.value.keywords)):
        ast.literal_eval(expr)  # Unsupported return expressions defer to the normal build and rechecks.
    call = assign.value
    if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and _name(call.func.value, 're') and call.func.attr == 'sub' and len(call.args) == 3
            and _name(call.args[2], assign.targets[0].id)):
        return ()
    flags = 0
    if call.keywords:
        kw, *others = call.keywords
        if others or kw.arg != 'flags' or not (isinstance(kw.value, ast.Attribute)
                and _name(kw.value.value, 're') and kw.value.attr == 'IGNORECASE'):
            return ()
        flags = re.IGNORECASE
    pattern, replacement = call.args[:2]
    for node in (pattern, replacement):
        if not (isinstance(node, ast.Constant) and type(node.value) is str and len(node.value) <= 256
                and node.lineno == node.end_lineno):
            return ()
    # Invalid patterns/templates can change exception behavior. Parse the bounded
    # pattern but never match it against data. Check the replacement against an
    # always-failing host pattern with the same number of capture groups.
    parsed = re.compile(pattern.value, flags)
    if parsed.groupindex or parsed.groups > 16:
        return ()
    re.compile('(?!)' + '()' * parsed.groups).sub(replacement.value, '')
    return pattern, replacement


def without_discarded_operands(source):
    """Mask only the two literal operands; callers still compare all remaining tokens."""
    if len(source) > 128_000 or b're.sub' not in source:
        return source
    try:
        encoding, _ = tokenize.detect_encoding(BytesIO(source).readline)
        if encoding != 'utf-8':
            return source
        tree = ast.parse(source.decode('utf-8'))  # AST columns are UTF-8 byte offsets.
        if not _safe_bindings(tree):
            return source
        spans = []
        for function in tree.body:
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spans.extend((n.lineno - 1, n.col_offset, n.end_col_offset) for n in _discarded_operands(function))
        lines = source.splitlines(keepends=True)
        for line, start, end in sorted(spans, reverse=True):
            lines[line] = lines[line][:start] + b"''" + lines[line][end:]
        return b''.join(lines)
    except (SyntaxError, ValueError, IndexError, RecursionError, OverflowError, re.error):
        return source
