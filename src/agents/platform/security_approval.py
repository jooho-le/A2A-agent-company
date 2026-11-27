"""Read-only approval of an explicitly supplied trusted Host assessor.

JSON never selects or imports Python. File bytes and the already loaded
function's code are different identity checks; neither attests its honesty,
mutable globals, transitive dependencies, or product security correctness.
No constructor executes the assessor or starts a provider, Tool or process.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import inspect
import json
import os
from pathlib import Path
import re
import stat
import sys
from types import CodeType, FunctionType, ModuleType
from urllib.parse import urlsplit

from agents.llm.content import sanitize_content
from agents.llm.contracts import parse_json


MAX_APPROVAL_BYTES = 262_144
MAX_CRITERIA_BYTES = 65_536
MAX_IMPLEMENTATION_FILE_BYTES = 1_048_576
MAX_IMPLEMENTATION_TOTAL_BYTES = 4_194_304
MAX_IMPLEMENTATION_FILES = 32
_FIELDS = frozenset({"schemaVersion", "verifierId", "verifierVersion", "criteriaRef",
    "scenarioContractSha256", "criteria", "implementationSha256", "implementationFiles"})
_FILE_FIELDS = frozenset({"name", "path", "sha256"})
_REFERENCE_SCHEMES = frozenset({"urn", "criteria", "security-criteria", "policy", "artifact", "approval"})


class SecurityApprovalError(ValueError):
    """Never retain submitted JSON, code, Host paths or an underlying error."""

    code = "SECURITY_APPROVAL_INVALID"

    def __init__(self):
        super().__init__(self.code)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _digest(value):
    return sha256(_json(value).encode("utf-8")).hexdigest()


def _hash(value):
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError


def _label(value, maximum=128):
    if (type(value) is not str or not value.strip() or len(value.encode("utf-8")) > maximum
            or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value)):
        raise ValueError


def _name(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", value) is None:
        raise ValueError


def _bounded_json(value, *, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > 16 or count[0] > 4096:
        raise ValueError
    if value is None or type(value) in (bool, int, float):
        return
    if type(value) is str:
        _label(value, 8192) if value else None
        return
    if type(value) is dict:
        for key, item in value.items():
            _label(key, 128)
            _bounded_json(item, depth=depth + 1, count=count)
        return
    if type(value) is list:
        for item in value:
            _bounded_json(item, depth=depth + 1, count=count)
        return
    raise ValueError


def _definition(value):
    if (type(value) is not dict or set(value) != _FIELDS
            or type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1):
        raise ValueError
    _name(value["verifierId"])
    if (type(value["verifierVersion"]) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}", value["verifierVersion"]) is None):
        raise ValueError
    reference = value["criteriaRef"]
    _label(reference, 512)
    parsed = urlsplit(reference)
    if (not reference.isascii() or parsed.scheme not in _REFERENCE_SCHEMES
            or not (parsed.netloc or parsed.path) or any(char.isspace() for char in reference)
            or parsed.username is not None or parsed.password is not None or "\\" in reference):
        raise ValueError
    for key in ("scenarioContractSha256", "implementationSha256"):
        _hash(value[key])
    criteria = value["criteria"]
    if type(criteria) is not dict or not criteria:
        raise ValueError
    _bounded_json(criteria)
    if len(_json(criteria).encode("utf-8")) > MAX_CRITERIA_BYTES:
        raise ValueError
    files = value["implementationFiles"]
    if type(files) is not list or not 1 <= len(files) <= MAX_IMPLEMENTATION_FILES:
        raise ValueError
    names = set()
    for item in files:
        if type(item) is not dict or set(item) != _FILE_FIELDS:
            raise ValueError
        _name(item["name"])
        _hash(item["sha256"])
        _label(item["path"], 4096)
        if (item["name"].casefold() in names or item["path"].startswith("~")
                or "$" in item["path"] or "\\" in item["path"] or "://" in item["path"]):
            raise ValueError
        names.add(item["name"].casefold())
    # Keep private implementation paths out of public metadata; criteria and
    # logical identifiers still must not contain recognized credential data.
    public = {key: item for key, item in value.items() if key != "implementationFiles"}
    public["implementationFiles"] = [{"name": item["name"], "sha256": item["sha256"]} for item in files]
    sanitize_content(public, reject_secrets=True)
    return value


def _parents(path):
    if type(path) not in (type(Path()), Path) or not path.is_absolute():
        raise ValueError
    for parent in reversed(path.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError


def _read_regular(path, maximum, *, allow_empty=False):
    descriptor = None
    try:
        _parents(path)
        before = path.lstat()
        minimum = 0 if allow_empty else 1
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or not minimum <= before.st_size <= maximum):
            raise ValueError
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        opened = os.fstat(descriptor)
        signature = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
        if signature(opened) != signature(before) or not stat.S_ISREG(opened.st_mode):
            raise ValueError
        chunks, size = [], 0
        while True:
            chunk = os.read(descriptor, min(65_536, maximum + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if size > maximum:
                raise ValueError
            chunks.append(chunk)
        _parents(path)
        if (size != opened.st_size or signature(os.fstat(descriptor)) != signature(opened)
                or signature(path.lstat()) != signature(opened)):
            raise ValueError
        canonical = path.resolve(strict=True)
        return canonical, b"".join(chunks), (opened.st_dev, opened.st_ino)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _function(assessor):
    if (type(assessor) is not FunctionType or type(assessor.__name__) is not str
            or assessor.__name__ == "<lambda>" or assessor.__qualname__ != assessor.__name__
            or assessor.__code__.co_name != assessor.__name__
            # co_qualname exists from Python 3.11; the declared minimum is 3.10,
            # where __qualname__ == __name__ above already pins the same identity.
            or getattr(assessor.__code__, "co_qualname", assessor.__qualname__) != assessor.__qualname__
            or assessor.__defaults__ is not None or assessor.__kwdefaults__ is not None
            or assessor.__closure__ is not None or assessor.__code__.co_freevars
            or assessor.__code__.co_argcount != 1 or assessor.__code__.co_kwonlyargcount != 0
            or assessor.__code__.co_flags & (inspect.CO_VARARGS | inspect.CO_VARKEYWORDS
                                          | inspect.CO_GENERATOR | inspect.CO_ASYNC_GENERATOR)):
        raise ValueError
    module = sys.modules.get(assessor.__module__)
    if type(module) is not ModuleType or assessor.__globals__ is not vars(module):
        raise ValueError
    if not hasattr(assessor.__code__, "co_qualname") and assessor.__code__.co_flags & inspect.CO_NESTED:
        # Python 3.10: without co_qualname a nested function could spoof
        # __qualname__; the compiler's nested-code flag still identifies it.
        raise ValueError
    path = Path(module.__file__).absolute()
    if path.suffix != ".py":
        raise ValueError
    _parents(path)
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise ValueError
    return path.resolve(strict=True)


def _code_value(value, *, depth=0, count=None):
    if count is None:
        count = [0]
    count[0] += 1
    if depth > 32 or count[0] > 8192:
        raise ValueError
    if type(value) is CodeType:
        return {"kind": "code", "argcount": value.co_argcount, "posonly": value.co_posonlyargcount,
            "kwonly": value.co_kwonlyargcount, "nlocals": value.co_nlocals, "stacksize": value.co_stacksize,
            "flags": value.co_flags, "bytecode": value.co_code.hex(),
            # Python 3.10 has no exception table/qualname; 3.11+ values are unchanged.
            "exceptiontable": getattr(value, "co_exceptiontable", b"").hex(), "names": list(value.co_names),
            "varnames": list(value.co_varnames), "freevars": list(value.co_freevars),
            "cellvars": list(value.co_cellvars), "name": value.co_name,
            "qualname": getattr(value, "co_qualname", value.co_name),
            "constants": [_code_value(item, depth=depth + 1, count=count) for item in value.co_consts]}
    if value is None or type(value) in (bool, int, str):
        return {"kind": type(value).__name__, "value": value}
    if type(value) is bytes:
        return {"kind": "bytes", "value": value.hex()}
    if type(value) is float:
        return {"kind": "float", "value": value.hex()}
    if type(value) is complex:
        return {"kind": "complex", "real": value.real.hex(), "imaginary": value.imag.hex()}
    if value is Ellipsis:
        return {"kind": "ellipsis"}
    if type(value) in (tuple, frozenset):
        items = [_code_value(item, depth=depth + 1, count=count) for item in value]
        return {"kind": type(value).__name__, "items": sorted(items, key=_json) if type(value) is frozenset else items}
    raise ValueError


def _implementation(assessor, files):
    module_path = _function(assessor)
    if type(files) is not tuple or not 1 <= len(files) <= MAX_IMPLEMENTATION_FILES:
        raise ValueError
    names, identities, paths, entries, total = set(), set(), [], [], 0
    for item in files:
        if type(item) is not tuple or len(item) != 3:
            raise ValueError
        name, path, expected = item
        _name(name)
        _hash(expected)
        if name.casefold() in names or not isinstance(path, Path) or path.suffix != ".py":
            raise ValueError
        canonical, content, identity = _read_regular(path, MAX_IMPLEMENTATION_FILE_BYTES, allow_empty=True)
        if identity in identities or canonical in paths or sha256(content).hexdigest() != expected:
            raise ValueError
        total += len(content)
        if total > MAX_IMPLEMENTATION_TOTAL_BYTES:
            raise ValueError
        names.add(name.casefold())
        identities.add(identity)
        paths.append(canonical)
        entries.append({"name": name, "sha256": expected})
    if module_path not in paths:
        raise ValueError
    code = _code_value(assessor.__code__)
    if len(_json(code).encode("utf-8")) > MAX_IMPLEMENTATION_FILE_BYTES:
        raise ValueError
    runtime = {"implementation": sys.implementation.name, "cacheTag": sys.implementation.cache_tag,
               "version": list(sys.version_info[:3])}
    return _digest({"schemaVersion": 1, "runtime": runtime, "function": code,
                    "files": sorted(entries, key=lambda item: item["name"])}), tuple(paths)


def calculate_implementation_sha256(assessor, files):
    """Operator helper; files is tuple[(logical_name, absolute_Path, SHA256)].

    Reads only declared regular .py files and the supplied function's code.
    Does not import, call the assessor, write approval, or declare it approved.
    """
    try:
        return _implementation(assessor, files)[0]
    except Exception:
        raise SecurityApprovalError() from None


def _public(data):
    return {"schemaVersion": 1, "verifierId": data["verifierId"], "verifierVersion": data["verifierVersion"],
        "criteriaRef": data["criteriaRef"], "scenarioContractSha256": data["scenarioContractSha256"],
        "criteriaSha256": _digest(data["criteria"]), "implementationSha256": data["implementationSha256"],
        "implementationFiles": sorted(({"name": item["name"], "sha256": item["sha256"]}
                                       for item in data["implementationFiles"]), key=lambda item: item["name"])}


@dataclass(frozen=True, init=False, repr=False)
class ApprovedSecurityAssessor:
    """An inert, sealed approval; no honesty or dependency attestation."""

    _definition_json: str = field(repr=False)
    _files: tuple = field(repr=False)
    _assessor: object = field(repr=False)
    _approval_path: Path = field(repr=False)

    def __init__(self):
        raise SecurityApprovalError()

    def __repr__(self):
        return "ApprovedSecurityAssessor()"

    def _definition(self):
        try:
            if type(self) is not ApprovedSecurityAssessor or type(self._definition_json) is not str:
                raise ValueError
            return _definition(parse_json(self._definition_json, max_bytes=MAX_APPROVAL_BYTES))
        except Exception:
            raise SecurityApprovalError() from None

    @property
    def descriptor(self):
        return _public(self._definition())

    @property
    def approval_sha256(self):
        return _digest(self.descriptor)

    @property
    def criteria_sha256(self):
        return self.descriptor["criteriaSha256"]

    @property
    def criteria(self):
        return self._definition()["criteria"]

    @property
    def scenario_contract_sha256(self):
        return self._definition()["scenarioContractSha256"]

    @property
    def assess(self):
        return self._assessor

    @property
    def implementation_paths(self):
        try:
            return tuple(item[1] for item in self._files)
        except Exception:
            raise SecurityApprovalError() from None

    @property
    def approval_path(self):
        return self._approval_path

    def verify_integrity(self):
        """Re-read declared bytes and loaded code before each explicit call."""
        try:
            data = self._definition()
            if (not isinstance(self._approval_path, Path) or not self._approval_path.is_absolute()
                    or type(self._files) is not tuple or len(self._files) != len(data["implementationFiles"])):
                raise ValueError
            approval_path, content, _identity = _read_regular(self._approval_path, MAX_APPROVAL_BYTES)
            current = _definition(parse_json(content.decode("utf-8"), max_bytes=MAX_APPROVAL_BYTES))
            if approval_path != self._approval_path or _json(current) != self._definition_json:
                raise ValueError
            expected = {item["name"]: item for item in data["implementationFiles"]}
            for name, path, digest in self._files:
                item = expected[name]
                original = Path(item["path"])
                original = original if original.is_absolute() else self._approval_path.parent / original
                _parents(original)
                if original.resolve(strict=True) != path or item["sha256"] != digest:
                    raise ValueError
            implementation, paths = _implementation(self._assessor, self._files)
            if implementation != data["implementationSha256"] or paths != self.implementation_paths:
                raise ValueError
        except Exception:
            raise SecurityApprovalError() from None


def load_approved_assessor(approval_file, assessor):
    """Load only data; assessor must already be explicitly supplied by Host."""
    try:
        approval_path, content, _identity = _read_regular(Path(approval_file).absolute(), MAX_APPROVAL_BYTES)
        data = _definition(parse_json(content.decode("utf-8"), max_bytes=MAX_APPROVAL_BYTES))
        files, total = [], 0
        for item in data["implementationFiles"]:
            path = Path(item["path"])
            path = path if path.is_absolute() else approval_path.parent / path
            canonical, _content, _identity = _read_regular(path, MAX_IMPLEMENTATION_FILE_BYTES, allow_empty=True)
            total += len(_content)
            if total > MAX_IMPLEMENTATION_TOTAL_BYTES:
                raise ValueError
            files.append((item["name"], canonical, item["sha256"]))
        result = object.__new__(ApprovedSecurityAssessor)
        object.__setattr__(result, "_definition_json", _json(data))
        object.__setattr__(result, "_files", tuple(files))
        object.__setattr__(result, "_assessor", assessor)
        object.__setattr__(result, "_approval_path", approval_path)
        result.verify_integrity()
        return result
    except Exception:
        raise SecurityApprovalError() from None
