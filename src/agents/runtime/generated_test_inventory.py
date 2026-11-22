"""Bounded, inert unittest discovery shared by draft checks and the ID catalog.

Non-package folders (including browser JSON) are not Python discovery scopes.
Package initializers with executable wiring remain unresolved: the Host never
imports generated code to predict load_tests, re-exports, or dynamic IDs.
"""
import ast
from dataclasses import dataclass
import fnmatch
from itertools import islice
from pathlib import Path, PurePosixPath

MAX_FILES = 64
MAX_FILE_BYTES = 1_048_576
MAX_ENTRIES = 2048
MAX_DEPTH = 32


@dataclass(frozen=True)
class UnitInventory:
    files: tuple[tuple[Path, str], ...]
    complete: bool


def scope_root(tests_root, source_directory='tests'):
    """Resolve an approved tests-relative scope, never a model-selected Host path."""
    relative = PurePosixPath(source_directory)
    if (relative.is_absolute() or not relative.parts or relative.parts[0] != 'tests'
            or any(p in ('.', '..') for p in relative.parts) or '\\' in source_directory):
        raise ValueError('GENERATED_TEST_SCOPE_INVALID')
    current = Path(tests_root)
    if current.is_symlink():
        raise ValueError('GENERATED_TEST_SCOPE_INVALID')
    for part in relative.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError('GENERATED_TEST_SCOPE_INVALID')
    return current


def unit_inventory(root, pattern):
    """Mirror plain unittest discovery, without importing files or following links."""
    root = Path(root)
    files, count, complete = [], 0, True
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        return UnitInventory((), False)

    def visit(directory, prefix=(), depth=0):
        nonlocal count, complete
        if depth > MAX_DEPTH:
            complete = False
            return
        try:
            entries = list(islice(directory.iterdir(), MAX_ENTRIES + 1))
            count += len(entries)
            if count > MAX_ENTRIES:
                complete = False
                return
            for entry in sorted(entries):
                if entry.name == '__pycache__':
                    continue
                if entry.is_symlink():
                    # The execution input capture also rejects links. Do not
                    # advertise a complete catalog over an unsafe input tree.
                    complete = False
                    continue
                if entry.is_dir():
                    initializer = entry / '__init__.py'
                    if not initializer.exists():
                        continue  # browser/assets and namespace folders are not unittest packages
                    if not entry.name.isidentifier() or initializer.is_symlink():
                        complete = False
                        continue
                    with initializer.open('rb') as stream:
                        raw = stream.read(MAX_FILE_BYTES + 1)
                    if len(raw) > MAX_FILE_BYTES:
                        complete = False
                        continue
                    tree = ast.parse(raw)
                    if any(not (isinstance(n, ast.Pass) or isinstance(n, ast.Expr)
                               and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str))
                           for n in tree.body):
                        complete = False
                        continue
                    visit(entry, (*prefix, entry.name), depth + 1)
                elif entry.is_file() and entry.suffix == '.py' and fnmatch.fnmatchcase(entry.name, pattern):
                    if not entry.stem.isidentifier() or len(files) >= MAX_FILES:
                        complete = False
                        continue
                    files.append((entry, '.'.join((*prefix, entry.stem))))
        except (OSError, SyntaxError, UnicodeError, ValueError, RecursionError):
            complete = False

    visit(root)
    return UnitInventory(tuple(files), complete)
