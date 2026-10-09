"""Shared secret redaction at API, persistence, A2A, and logging boundaries.

This masks recognizable secret fields and patterns, not arbitrary prose. It
does not rewrite opaque protocol IDs or ordinary Git/Snapshot SHA-256 hashes.
"""

import re
from collections.abc import Mapping
from threading import RLock


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


def redact_text(value: str) -> str:
    """Mask labeled credentials, encoded password hashes, Bearer tokens, and JWTs."""
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
            elif normalized in _SECRET_KEYS or normalized.endswith(_SECRET_SUFFIXES):
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
