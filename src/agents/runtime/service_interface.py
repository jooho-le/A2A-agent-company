"""Bounded static detection of an API-only product submitted as a website.

This is a preflight, never UI execution or a PASS verdict. Unknown rendering
strategies are left to the configured browser tests on the frozen snapshot.
"""
import ast
from html.parser import HTMLParser
from pathlib import Path
import re

from agents.runtime.signup_quality_precheck import _modules


def inspect_service_interface(source_root, contract):
    if contract.get("scenarioKey") != "SERVICE-WEB" or source_root is None:
        return []
    root = Path(source_root)
    if not root.is_absolute() or root.is_symlink():
        return []
    modules = _modules(root, "main")
    # HTML templates may live outside Python. Never traverse symlinked trees.
    visited = 0
    def has_html(directory, depth=0):
        nonlocal visited
        if depth > 6:
            return True  # uninspected inventory is unknown, never proof of absence
        try:
            entries = directory.iterdir()
            for path in entries:
                visited += 1
                if visited > 512:
                    return True
                if path.is_symlink():
                    continue
                if path.is_file() and path.suffix.lower() in {".html", ".htm"}:
                    return True
                if path.is_dir() and not path.name.startswith('.') and has_html(path, depth+1):
                    return True
        except OSError:
            return True
        return False
    if has_html(root):
        return []
    for _path, tree in modules.values():
        if any(isinstance(n, ast.Constant) and isinstance(n.value, str) and
               any(tag in n.value.lower() for tag in ("<html", "<body", "<!doctype", "<form", "<main", "<div"))
               for n in ast.walk(tree)):
            return []
    for path, tree in modules.values():
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            root_route = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute) and
                d.func.attr == "get" and d.args and isinstance(d.args[0], ast.Constant) and
                d.args[0].value == "/" for d in fn.decorator_list)
            if root_route and fn.body and isinstance(fn.body[-1], ast.Return) and isinstance(fn.body[-1].value, ast.Dict):
                return [{"fieldPath": "/source", "reasonCode": "WEB_UI_NOT_IMPLEMENTED",
                         "sourcePath": path.relative_to(root).as_posix(), "lineNumber": fn.lineno}]
    return []


def service_interface_check(checkpoint, contract):
    return lambda: inspect_service_interface(getattr(checkpoint, "source_root", None), contract)


class _Selectors(HTMLParser):
    def __init__(self, path, rows):
        super().__init__()
        self.path, self.rows = path, rows

    def handle_starttag(self, tag, attrs):
        if not re.fullmatch(r'[a-z][a-z0-9-]{0,31}', tag):
            return
        for name, value in attrs:
            if (name in {'id', 'name'} and isinstance(value, str)
                    and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}', value) and len(self.rows) < 80):
                selector = '#' + value if name == 'id' else f'{tag}[name="{value}"]'
                row = {'path': self.path, 'selector': selector}
                if row not in self.rows:
                    self.rows.append(row)


def service_source_facts(files):
    """Bounded static facts from already authenticated Source bytes, not a DOM.

    Do not execute imports/JS, infer missing UI from absence, or turn this
    inventory into a test result. Dynamic rendering still needs browser tests.
    """
    paths, selectors, routes = [], [], []
    total = 0
    truncated = False
    for index, (path, raw) in enumerate(sorted(files.items())):
        if index >= 80:
            truncated = True
            break
        paths.append(path)
        if not path.endswith(('.py', '.html', '.htm')):
            continue
        total += len(raw)
        if len(raw) > 128_000 or total > 1_048_576:
            truncated = True
            continue
        try:
            text = raw.decode('utf-8')
            if path.endswith('.py'):
                tree = ast.parse(text)
                nodes = list(ast.walk(tree))
                if len(nodes) > 50_000:
                    truncated = True
                    continue
                for node in nodes:
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and '<' in node.value:
                        _Selectors(path, selectors).feed(node.value)
                    if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
                        for decorator in node.decorator_list:
                            if (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
                                    and decorator.func.attr in {'get', 'post', 'put', 'patch', 'delete'}
                                    and decorator.args and isinstance(decorator.args[0], ast.Constant)):
                                value = decorator.args[0].value
                                if (isinstance(value, str) and re.fullmatch(r'/[A-Za-z0-9_/{.}-]{0,199}', value)
                                        and len(routes) < 64):
                                    routes.append({'path': path, 'method': decorator.func.attr.upper(), 'route': value})
            else:
                _Selectors(path, selectors).feed(text)
        except (UnicodeError, SyntaxError, ValueError, RecursionError):
            truncated = True
    return {'files': paths, 'staticRoutes': routes, 'staticSelectors': selectors,
            'truncated': truncated, 'isLiveDOM': False}
