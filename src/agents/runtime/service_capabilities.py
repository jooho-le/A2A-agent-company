"""Approved lock inventory and static checks. Never import generated source."""
import ast
from hashlib import sha256
from pathlib import Path
import re
import sys

_ALIASES = {"argon2-cffi": "argon2", "python-multipart": "multipart", "email-validator": "email_validator",
            "python-dotenv": "dotenv", "pyyaml": "yaml", "pillow": "PIL", "scikit-learn": "sklearn",
            "beautifulsoup4": "bs4", "argon2-cffi-bindings": "_argon2_cffi_bindings"}


def runtime_capabilities(lock_file, expected_hash):
    path = Path(lock_file)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_048_576:
        raise ValueError("SERVICE_RUNTIME_LOCK_INVALID")
    raw = path.read_bytes()
    if "sha256:" + sha256(raw).hexdigest() != expected_hash:
        raise ValueError("SERVICE_RUNTIME_LOCK_CHANGED")
    packages = {}
    for line in raw.decode("utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)==([A-Za-z0-9_.+!-]+)(?:\s|$)", line)
        if match:
            name = re.sub(r"[-_.]+", "-", match[1]).lower()
            packages[name] = match[2]
    if not packages:
        raise ValueError("SERVICE_RUNTIME_LOCK_EMPTY")
    modules = sorted({_ALIASES.get(name, name.replace("-", "_")) for name in packages})
    return {"policy": "APPROVED_LOCK_CAPABILITIES_V1", "dependencyLockHash": expected_hash,
            "packages": packages, "importModules": modules, "networkInstalls": False,
            "instruction": "Use only the standard library and packages in this immutable lock. A missing package is not installed by editing requirements.lock. For plain HTML use HTMLResponse; for JSON APIs use JSON input models. FastAPI Form/File require python-multipart; Jinja2Templates requires jinja2."}


def inspect_dependencies(root, capabilities):
    if not root or not isinstance(capabilities, dict):
        return []
    root = Path(root)
    if not root.is_absolute() or root.is_symlink():
        return []
    allowed = set(capabilities["importModules"]) | set(sys.stdlib_module_names) | {"__future__"}
    issues = []
    for index, path in enumerate(sorted(root.rglob("*.py"))):
        if index >= 400:
            break
        relative = path.relative_to(root)
        if (any(p.startswith('.') or p in ('node_modules', '__pycache__') for p in relative.parts)
                or path.is_symlink() or any(root.joinpath(*relative.parts[:i]).is_symlink()
                                           for i in range(1, len(relative.parts)))
                or path.stat().st_size > 1_048_576):
            continue
        try:
            tree = ast.parse(path.read_bytes())
        except (SyntaxError, ValueError):
            continue
        dependency_calls = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                for alias in node.names:
                    if node.module in ('fastapi', 'fastapi.param_functions') and alias.name in ('Form', 'File'):
                        dependency_calls[alias.asname or alias.name] = 'multipart'
                    elif node.module in ('fastapi.templating', 'starlette.templating') and alias.name == 'Jinja2Templates':
                        dependency_calls[alias.asname or alias.name] = 'jinja2'
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split('.')[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split('.')[0]]
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                dependency = dependency_calls.get(node.func.id)
                if dependency:
                    names = [dependency]
            for name in names:
                local = (root / (name + '.py')).is_file() or (root / name).is_dir()
                if name not in allowed and not local:
                    item = {"fieldPath": "/source/imports/" + name,
                            "reasonCode": "SERVICE_DEPENDENCY_UNAVAILABLE",
                            "sourcePath": relative.as_posix(), "lineNumber": node.lineno}
                    if item not in issues:
                        issues.append(item)
    return issues[:24]
