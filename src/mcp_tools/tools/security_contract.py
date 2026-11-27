"""Standalone bounded scanner policy contract, shipped read-only to Container.

Only standard-library imports are used. This module performs no filesystem,
network, scanner discovery, installation or Source execution.
"""

import json
import math
import re
import unicodedata
from urllib.parse import urlsplit


MAX_JSON_BYTES = 1024 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 65536
HOST_FIELDS = frozenset({
    "scanner", "profile_name", "scanner_version", "rule_ids", "profile_ref", "ignore_nosec", "scan_scope",
})
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_VERSION = re.compile(r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
_RULE = re.compile(r"B[0-9]{3}\Z")
_SECRET_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".codex", ".agents", ".gnupg", ".kube", ".npmrc", ".pypirc", ".netrc",
    "credentials", "credentials.json", "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "docker.sock", ".workspace.json",
})


class SecurityContractError(ValueError):
    code = "SCANNER_ERROR"

    def __init__(self):
        super().__init__(self.code)


def _string(value, maximum, *, empty=False):
    if type(value) is not str or (not empty and not value):
        raise SecurityContractError()
    try:
        if len(value.encode("utf-8")) > maximum:
            raise SecurityContractError()
    except UnicodeError:
        raise SecurityContractError() from None
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise SecurityContractError()
    return value


def _name(value):
    value = _string(value, 64)
    if _NAME.fullmatch(value) is None:
        raise SecurityContractError()
    return value


def _version(value):
    value = _string(value, 32)
    if _VERSION.fullmatch(value) is None:
        raise SecurityContractError()
    return value


def _rule(value):
    value = _string(value, 4)
    # B001 is Bandit's umbrella suppression identifier, not a concrete
    # selectable detector. Unsupported installed IDs are rejected by Runner.
    if _RULE.fullmatch(value) is None or value == "B001":
        raise SecurityContractError()
    return value


def _source_path(value):
    """Source-relative lexical path; never resolve/open a Host path or URI."""
    value = _string(value, 4096)

    def check(path):
        _string(path, 4096)
        if not path.strip() or path.startswith("/") or "\\" in path or ":" in path:
            raise SecurityContractError()
        parts = path.split("/")
        if len(parts) > 128 or any(part in {"", ".", ".."} for part in parts):
            raise SecurityContractError()
        for part in parts:
            name = part.casefold()
            if (name in _SECRET_NAMES or name.startswith((".env", ".mcp-write-"))
                    or name.endswith((".pem", ".key", ".p12", ".pfx"))):
                raise SecurityContractError()
        return parts

    parts = check(value)
    # Compatibility forms may not disguise traversal, separators or Secret
    # names. Normalize only for validation; never rename Source/report paths.
    normalized_parts = check(unicodedata.normalize("NFKC", value))
    if len(parts) != len(normalized_parts):
        raise SecurityContractError()
    return value


def _profile_reference(value):
    value = _string(value, 4096)
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"artifact", "https"} or not parsed.netloc or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or not parsed.path.startswith("/")
            or "\\" in value or "%" in value or any(char.isspace() for char in value)
        ):
            raise SecurityContractError()
        _source_path(parsed.path[1:])
        return value
    except (ValueError, TypeError, UnicodeError):
        raise SecurityContractError() from None


def _bounds(value):
    remaining = MAX_JSON_NODES

    def visit(item, depth):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise SecurityContractError()
        if item is None or type(item) in (str, int, bool):
            return
        if type(item) is float and math.isfinite(item):
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                visit(child, depth + 1)
            return
        raise SecurityContractError()

    visit(value, 0)


def canonical_json(value, *, max_bytes=MAX_JSON_BYTES):
    try:
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_JSON_BYTES:
            raise SecurityContractError()
        _bounds(value)
        text = json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        if len(text.encode("utf-8")) > max_bytes:
            raise SecurityContractError()
        return text
    except (SecurityContractError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise SecurityContractError() from None


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SecurityContractError()
        result[key] = value
    return result


def _nonfinite(_value):
    raise SecurityContractError()


def parse_json(text, *, max_bytes=MAX_JSON_BYTES):
    try:
        if (type(max_bytes) is not int or not 1 <= max_bytes <= MAX_JSON_BYTES
                or type(text) is not str or len(text.encode("utf-8")) > max_bytes):
            raise SecurityContractError()
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_nonfinite)
        _bounds(value)
        canonical_json(value, max_bytes=max_bytes)
        return value
    except (SecurityContractError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError):
        raise SecurityContractError() from None


def validate_security_host_payload(data):
    """Closed immutable scanner selection; no scope/exclude/nosec/baseline knobs."""
    try:
        if (
            type(data) is not dict or set(data) != HOST_FIELDS or data["scanner"] != "bandit"
            or data["ignore_nosec"] is not True or data["scan_scope"] != "ALL_PYTHON"
            or type(data["rule_ids"]) is not list or not 1 <= len(data["rule_ids"]) <= 128
        ):
            raise SecurityContractError()
        name, version, reference = _name(data["profile_name"]), _version(data["scanner_version"]), _profile_reference(data["profile_ref"])
        rules = [_rule(value) for value in data["rule_ids"]]
        if len(set(rules)) != len(rules):
            raise SecurityContractError()
        return {
            "scanner": "bandit", "profile_name": name, "scanner_version": version,
            "rule_ids": sorted(rules), "profile_ref": reference, "ignore_nosec": True, "scan_scope": "ALL_PYTHON",
        }
    except (SecurityContractError, TypeError, ValueError, KeyError, UnicodeError, OverflowError, RecursionError):
        raise SecurityContractError() from None
