"""Bounded, inert warning for a simple FastAPI default-validation exposure path.

This is a pre-READY correction aid, not a security verdict. Alternative request
handling, middleware, registered handlers and dynamic wiring are left to the
existing measured Security checks. No generated code is imported or executed.
"""
import ast
from pathlib import Path

POLICY = "OUTPUT_SAFETY_PRECHECK_V1"
MAX_BYTES = 128_000
MAX_NODES = 12_000
REASON = "DEFAULT_VALIDATION_MAY_ECHO_CREDENTIALS"


def _imports(tree, module, exported):
    return {a.asname or a.name for n in tree.body if isinstance(n, ast.ImportFrom)
            and n.module == module and not n.level for a in n.names if a.name == exported}


def _name(node):
    return node.id if isinstance(node, ast.Name) else None


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return _name(node)


def inspect_output_safety(source_root, contract):
    """Warn only for the direct, plain-str request-model path observed in SCN-001."""
    if (not isinstance(contract, dict) or contract.get("scenarioKey") != "SCN-001"
            or not any(r.get("key") == "REQ-006" for r in contract.get("requirements", ()))):
        return []
    interface = contract.get("externalContract") or {}
    app_contract = interface.get("app") or {}
    api = interface.get("signupApi") or {}
    module, attribute = app_contract.get("module"), app_contract.get("attribute")
    if (app_contract.get("framework") != "FastAPI" or not isinstance(module, str)
            or not all(part.isidentifier() for part in module.split(".")) or not isinstance(attribute, str)
            or api.get("method") != "POST" or "password" not in api.get("bodyFields", ())):
        return []
    root = Path(source_root)
    if not root.is_absolute() or root.is_symlink():
        return []
    current = root
    for part in module.split(".")[:-1]:
        current = current / part
        if current.is_symlink():
            return []
    path = current / (module.split(".")[-1] + ".py")
    if path.is_symlink() or not path.is_file():
        return []
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return []
        tree = ast.parse(raw)
        nodes = list(ast.walk(tree))
    except (OSError, SyntaxError, ValueError, RecursionError):
        return []
    if len(nodes) > MAX_NODES:
        return []
    factories, bases = _imports(tree, "fastapi", "FastAPI"), _imports(tree, "pydantic", "BaseModel")
    if not factories or not bases:
        return []
    if any(isinstance(n, (ast.Assign, ast.AnnAssign))
           and any(_name(t) in factories | bases for t in
                   (n.targets if isinstance(n, ast.Assign) else [n.target])) for n in tree.body):
        return []
    assignments = [n for n in tree.body if isinstance(n, ast.Assign)
                   and any(_name(target) == attribute for target in n.targets)]
    if len(assignments) != 1:
        return []
    constructor = assignments[0].value
    if (not isinstance(constructor, ast.Call) or _name(constructor.func) not in factories
            or any(k.arg in (None, "middleware", "exception_handlers") for k in constructor.keywords)):
        return []
    # Unknown/custom handlers may already meet the requirement. Do not force a
    # particular implementation or treat this heuristic as proof of their absence.
    for node in nodes:
        if isinstance(node, ast.Call):
            if _name(node.func) in {"exec", "eval", "setattr", "getattr", "globals", "__import__"}:
                return []
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                    "exception_handler", "add_exception_handler", "middleware", "add_middleware"}:
                return []
            if (isinstance(node.func, ast.Attribute) and _root_name(node.func) == attribute
                    and node.func.attr not in {"get", "post", "put", "patch", "delete", "head", "options", "route", "api_route"}):
                return []
            if any(_name(arg) == attribute for arg in node.args):
                return []
        if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, (ast.Attribute, ast.Subscript))
                   and any(_name(n) == attribute for n in ast.walk(t)) for t in targets):
                return []
    models = set()
    for node in tree.body:
        if (isinstance(node, ast.ClassDef) and len(node.bases) == 1 and _name(node.bases[0]) in bases
                and not any(isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) for child in node.body)
                and any(isinstance(field, ast.AnnAssign) and _name(field.target) == "password"
                        and _name(field.annotation) == "str" for field in node.body)):
            models.add(node.name)
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        route = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                    and _name(d.func.value) == attribute and d.func.attr == "post" and d.args
                    and isinstance(d.args[0], ast.Constant) and d.args[0].value == api.get("path")
                    for d in node.decorator_list)
        required = node.args.posonlyargs + node.args.args
        required = required[:len(required) - len(node.args.defaults)] if node.args.defaults else required
        if route and any(_name(arg.annotation) in models for arg in required):
            return [{"fieldPath": "/source", "reasonCode": REASON}]
    return []


def output_safety_precheck_for(checkpoint, contract):
    def inspect():
        root = getattr(checkpoint, "source_root", None)
        return inspect_output_safety(root, contract) if root is not None else []
    return inspect
