"""Shared secret redaction at API, persistence, A2A, and logging boundaries.

This masks recognizable secret fields and patterns, not arbitrary prose. It
does not rewrite opaque protocol IDs or ordinary Git/Snapshot SHA-256 hashes.
"""

import re
from collections.abc import Mapping
from threading import RLock
import unicodedata
from urllib.parse import unquote_plus


REDACTED = "[REDACTED]"
_KNOWN_SECRETS: tuple[str, ...] = ()
_SECRET_GUARD = RLock()


def register_secret_values(*values: str | None) -> None:
    """Register trusted Host credentials for unlabelled-text masking in memory.

    Never reads environment files or persists this registry. Short values are
    masked by secret field/header rules, not global substring replacement: a
    one-character token must not corrupt every ordinary identifier or message.
    Opaque protocol references still bypass structured-value redaction.
    """
    global _KNOWN_SECRETS
    if any(value is not None and type(value) is not str for value in values):
        raise ValueError("INVALID_SECRET_REGISTRATION")
    with _SECRET_GUARD:
        _KNOWN_SECRETS = tuple(sorted(
            set(_KNOWN_SECRETS).union(value for value in values if value and len(value) >= 8),
            key=len, reverse=True,
        ))


def contains_registered_secret(value: str | bytes) -> bool:
    """Check the in-memory credential registry without reading or exposing it."""
    with _SECRET_GUARD:
        secrets = _KNOWN_SECRETS
    if isinstance(value, bytes):
        return any(secret.encode(encoding) in value for secret in secrets
                   for encoding in ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"))
    return any(secret in value for secret in secrets)


_SECRET_KEYS = frozenset(
    {
        "password", "passwd", "pwd", "passwordhash", "hashedpassword",
        "token", "accesstoken", "refreshtoken", "idtoken", "apitoken",
        "apikey", "apisecret", "secret", "secretkey", "clientsecret",
        "authorization", "proxyauthorization", "비밀번호", "암호", "토큰",
    }
)
_SECRET_SUFFIXES = (
    "password", "passwordhash", "hashedpassword", "accesstoken", "refreshtoken",
    "apikey", "clientsecret", "secretkey",
)
_OPAQUE_REFERENCE_KEYS = frozenset(
    {"a2ataskid", "a2aartifactid", "agentcontextid", "contextid", "taskid", "messageid"}
)
_LABEL = (
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*(?:password(?:[ _-]?hash)?|hashed[ _-]?password|passwd|pwd|"
    r"(?:access|refresh|id|api)[ _-]?token|token|api[ _-]?key|"
    r"(?:api|client)[ _-]?secret|secret(?:[ _-]?key)?|"
    r"(?:proxy[ _-]?)?authorization|비밀번호|암호|토큰)"
)
_ASSIGNMENT = re.compile(
    rf"(?<![\w])(?P<label>[\"']?{_LABEL}[\"']?[ \t]*[:=][ \t]*)"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|"
    r"(?:Bearer|Basic|Digest|Token)[ \t]+[^\s,;\"'}]+|[^\s,;}&]+)",
    re.IGNORECASE,
)
_BEARER = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE)
_ARGON2 = re.compile(
    r"\$argon2(?:id|i|d)\$(?:v=\d+\$)?m=\d+,t=\d+,p=\d+"
    r"\$[A-Za-z0-9+/]+=*\$[A-Za-z0-9+/]+=*"
)
_BCRYPT = re.compile(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}")
_JWT = re.compile(r"(?<![\w.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\w.-])")
_URI = re.compile(r"(?<![\w])(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s<>\"'{}\\]+")
_URI_START = re.compile(r"(?<![\w/])(?=(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)")
_ENCODED_COLON = r"%(?:25){0,2}3a"
_ENCODED_SLASH = r"%(?:25){0,2}2f"
_ENCODED_BYTE = r"%(?:25){0,2}[0-9a-f]{2}"
_ENCODED_SCHEME = rf"(?:[A-Za-z]|{_ENCODED_BYTE})(?:[A-Za-z0-9+.-]|{_ENCODED_BYTE})*"
_ENCODED_URI = re.compile(
    rf"(?<![\w])(?:{_ENCODED_SCHEME}(?:{_ENCODED_COLON}(?://|{_ENCODED_SLASH}{_ENCODED_SLASH})|:{_ENCODED_SLASH}{_ENCODED_SLASH})"
    rf"|{_ENCODED_SLASH}{_ENCODED_SLASH})[^\s<>\"'{{}}\\]+",
    re.IGNORECASE,
)
_URI_COMPONENTS = re.compile(
    r"(?P<prefix>(?:[A-Za-z][A-Za-z0-9+.-]*:)?//)(?P<authority>[^/?#]*)(?P<suffix>.*)",
)
_URI_PARAMETER = re.compile(
    r"(?P<separator>[?#&;])(?P<key>[^=?#&;]+)=(?P<value>[^?#&;]*)",
)


def _decoded_reference(value: str) -> str:
    # Bound decoding work; cover encoded keys and conventional nested encoding.
    for _ in range(3):
        decoded = unquote_plus(value)
        if decoded == value:
            break
        value = decoded
    return unicodedata.normalize("NFKC", value)


def _is_secret_key(value: str) -> bool:
    normalized = re.sub(r"[\s_-]", "", _decoded_reference(value)).casefold()
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES)


def _redact_plain_uri(match: re.Match[str]) -> str:
    """Mask credentials without normalizing safe URL bytes or opaque IDs."""
    uri = match[0]
    components = _URI_COMPONENTS.fullmatch(uri)
    if components is None:
        return uri
    authority = components["authority"]
    if "@" in authority:
        authority = REDACTED + "@" + authority.rsplit("@", 1)[1]

    def parameter(item: re.Match[str]) -> str:
        if _is_secret_key(item["key"]) or contains_uri_credentials(item["value"]):
            return item["separator"] + item["key"] + "=" + REDACTED
        return item[0]

    suffix = _URI_PARAMETER.sub(parameter, components["suffix"])
    return components["prefix"] + authority + suffix


def _redact_uri(match: re.Match[str]) -> str:
    result = _redact_plain_uri(match)
    # If an encoded separator/authority hides credentials not handled by the
    # normal components, mask this URI instead of decoding/reformatting it.
    decoded = _decoded_reference(result)
    if decoded != result and _URI.sub(_redact_plain_uri, decoded) != decoded:
        return REDACTED
    return result


def contains_uri_credentials(value: str) -> bool:
    """Detect URI userinfo and recognizable secret query/fragment parameters."""
    for candidate in (value, _decoded_reference(value)):
        # Overlapping starts also see a URI inside a query/fragment value; a
        # greedy outer URL match must not hide nested redirect credentials.
        for start in _URI_START.finditer(candidate):
            match = _URI.match(candidate, start.start())
            if match is None:
                continue
            parts = _URI_COMPONENTS.fullmatch(match[0])
            if parts is not None and (
                "@" in parts["authority"]
                or any(_is_secret_key(item["key"]) for item in _URI_PARAMETER.finditer(parts["suffix"]))
            ):
                return True
    return False


def contains_credential_reference(value: str) -> bool:
    """Reject credential-bearing immutable references, including masked URLs.

    Detection uses recognizable URI userinfo, secret parameter names and the
    shared text/registered-secret rules. It is not a universal secret detector.
    """
    if contains_uri_credentials(value):
        return True
    for candidate in (value, _decoded_reference(value)):
        if redact_text(candidate) != candidate:
            return True
    return False


def redact_text(value: str) -> str:
    """Mask recognizable secrets and credential-bearing URI components."""
    def replace_assignment(match: re.Match[str]) -> str:
        original = match["value"]
        quote = original[0] if original.startswith(('"', "'")) else ""
        content = original[1:-1] if quote else original
        label = re.split(r"[:=]", match["label"], maxsplit=1)[0].strip("\"' \t")
        normalized = re.sub(r"[\s_-]", "", label).casefold()
        replacement = REDACTED
        if normalized in {"authorization", "proxyauthorization"}:
            scheme, separator, _ = content.partition(" ")
            if separator and scheme.casefold() in {"bearer", "basic", "digest", "token"}:
                replacement = f"{scheme} {REDACTED}"
        return match["label"] + quote + replacement + quote

    for secret in _KNOWN_SECRETS:
        value = value.replace(secret, REDACTED)
    value = _URI.sub(_redact_uri, value)
    value = _ENCODED_URI.sub(
        lambda match: REDACTED if contains_uri_credentials(match[0]) else match[0], value,
    )
    value = _ASSIGNMENT.sub(replace_assignment, value)
    value = _BEARER.sub("Bearer " + REDACTED, value)
    for pattern in (_ARGON2, _BCRYPT, _JWT):
        value = pattern.sub(REDACTED, value)
    return value


def redact_data(value: object) -> object:
    """Return a recursively redacted copy; preserve JSON containers and safe values."""
    if isinstance(value, Mapping):
        result: dict[object, object] = {}
        for key, item in value.items():
            normalized = re.sub(r"[\s_-]", "", str(key)).casefold()
            if normalized in _OPAQUE_REFERENCE_KEYS:
                # The Agent owns these opaque references; changing their bytes
                # would break Task/artifact routing rather than mask a secret.
                result[key] = item
            elif _is_secret_key(str(key)):
                if normalized in {"authorization", "proxyauthorization"} and isinstance(item, str):
                    scheme, separator, _ = item.partition(" ")
                    result[key] = (
                        f"{scheme} {REDACTED}"
                        if separator and scheme.casefold() in {"bearer", "basic", "digest", "token"}
                        else REDACTED
                    )
                else:
                    result[key] = REDACTED
            else:
                result[key] = redact_data(item)
        return result
    if isinstance(value, list):
        return [redact_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_data(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
