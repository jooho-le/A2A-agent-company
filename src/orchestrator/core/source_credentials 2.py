"""Reject recognizable credential literals without rewriting immutable Source.

This is an ingress guard, not a secret/taint-analysis engine. It deliberately
does not resolve variables, execute code, decrypt assets or read environment
values. All failures have a constant message; Source bytes never enter errors.
"""

import io
import re
import tarfile
import unicodedata
from urllib.parse import unquote, urlsplit

from orchestrator.core.security import contains_registered_secret, contains_uri_credentials


class SourceCredentialError(ValueError):
    def __init__(self):
        super().__init__("SOURCE_CREDENTIAL_DENIED")


_CREDENTIAL_NAME = (
    r"(?:[A-Za-z][A-Za-z0-9]*[_-])*(?:password(?:[_-]?hash)?|hashed[_-]?password|passwd|pwd|"
    r"(?:access|refresh|id|api)[_-]?token|token|api[_-]?key|(?:api|client)[_-]?secret|"
    r"secret(?:[_-]?key)?|(?:proxy[_-]?)?authorization|비밀번호|암호|토큰)"
)
_LITERAL = re.compile(
    rf"(?<![\w])[\"']?{_CREDENTIAL_NAME}[\"']?\s*"
    r"(?::\s*[A-Za-z_][A-Za-z0-9_.\[\], |]*\s*=|:|=)\s*"
    r"(?:[rubfRUBF]{0,2})?(?:\"(?:\\.|[^\"\\])+\"|'(?:\\.|[^'\\])+'|`[^`]+`)",
    re.IGNORECASE,
)
_BARE_CREDENTIAL = re.compile(
    r"\bBearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"\$argon2(?:id|i|d)\$|\$2[aby]\$\d{2}\$|"
    r"(?<![\w.-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![\w.-])|"
    r"(?<![\w-])sk-(?:(?:proj|svcacct)-)?[A-Za-z0-9_-]{16,}(?![\w-])|"
    r"(?<![\w])(?:AKIA|ASIA)[A-Z0-9]{16}(?![\w])|"
    r"(?<![\w])(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]{20,}(?![\w])|"
    r"(?<![\w])AIza[0-9A-Za-z_-]{35}(?![\w])|"
    r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----",
    re.IGNORECASE,
)
_ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{2})|u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8}))")
_JOINED_LITERAL = re.compile(r"([\"'])\s*(?:\+\s*)?[\"']")
_SOURCE_URI = re.compile(r"(?<![\w])(?:[A-Za-z][A-Za-z0-9+.-]*:)?//[^\s<>\"'\\]+")
_DYNAMIC = re.compile(r"\$?\{[^{}]*\}")
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_MAX_FILES = 1000


def _decoded_escape(match):
    try:
        return chr(int(next(value for value in match.groups() if value is not None), 16))
    except (ValueError, OverflowError):
        return match.group(0)


def _uri_credentials(source):
    """Distinguish literal URL credentials from dynamic auth expressions.

    Reference schemas reject credentials regardless of their values. Source
    additionally permits a runtime expression as an auth value: a dynamic
    query token or password is not a hardcoded secret. Static values in other
    components are still checked, even when the URL also has expressions.
    """
    for match in _SOURCE_URI.finditer(source):
        uri = match[0]
        if _DYNAMIC.search(uri) or contains_uri_credentials(uri):
            try:
                parts = urlsplit(uri)
            except ValueError:
                # Malformed literal URLs are not interpreted as executable
                # expressions; retain the conservative structural check.
                if contains_uri_credentials(uri):
                    return True
                continue
            userinfo, separator, hostname = parts.netloc.rpartition("@")
            username, colon, password = userinfo.partition(":")
            authority = parts.netloc
            if separator and _DYNAMIC.search(password if colon else username):
                authority = hostname
            def static_parameters(component):
                return "&".join(value for value in re.split(r"[&;]", component)
                                if value.partition("=")[2] and not _DYNAMIC.search(value.partition("=")[2]))
            uri = parts._replace(netloc=authority, query=static_parameters(parts.query),
                                 fragment=static_parameters(parts.fragment)).geturl()
            # The shared wire/reference parser excludes braces. Replace only
            # remaining non-auth expressions in this inspection view, so a
            # dynamic path/username cannot hide a separate static secret.
            uri = _DYNAMIC.sub("source-expression", uri)
        if contains_uri_credentials(uri):
            return True
    return False


def validate_source_text(source: str) -> None:
    """Preserve expressions such as password=request.password byte-for-byte.

    Inspect compatibility characters and common literal escapes/concatenation
    as additional views only. Never return a sanitized or normalized Source.
    """
    if type(source) is not str:
        raise SourceCredentialError()
    normalized = source if source.isascii() else unicodedata.normalize("NFKC", source)
    escaped = _ESCAPE.sub(_decoded_escape, normalized) if "\\" in normalized else normalized
    views = dict.fromkeys((source, normalized, escaped))
    # A literal URL/key remains a credential when its enclosing URI is percent
    # encoded. Keep decoding bounded and do not reinterpret Source '+' as a
    # URL-space operator; every accepted byte remains unchanged.
    candidate = escaped
    for _ in range(3):
        if "%" not in candidate:
            break
        decoded = unquote(candidate)
        if decoded == candidate:
            break
        decoded = decoded if decoded.isascii() else unicodedata.normalize("NFKC", decoded)
        decoded = _ESCAPE.sub(_decoded_escape, decoded) if "\\" in decoded else decoded
        views[decoded] = None
        candidate = decoded
    for value in views:
        # A split recognizable API key must not evade a literal-only guard.
        joined = _JOINED_LITERAL.sub("", value) if '"' in value or "'" in value else value
        if any(contains_registered_secret(view) or _LITERAL.search(view) or _BARE_CREDENTIAL.search(view)
               or _uri_credentials(view)
               for view in dict.fromkeys((value, joined))):
            raise SourceCredentialError()


def validate_source_bytes(content: bytes) -> None:
    """Inspect text regardless of filename, while allowing ordinary binary assets.

    Latin-1 is a lossless view for embedded ASCII credentials. UTF-8 and both
    endian UTF-16/32 views catch non-ASCII names and BOM-less text disguised as
    binary. No successful decoding is required for a real binary asset.
    """
    if type(content) is not bytes:
        raise SourceCredentialError()
    if contains_registered_secret(content):
        raise SourceCredentialError()
    views = {content.decode("latin-1")}
    # Binary framing bytes must not masquerade as an identifier boundary and
    # hide an adjacent ASCII credential (e.g. 0xff + api_key="...").
    if not content.isascii():
        views.add(content.decode("ascii", errors="replace"))
    encodings = ("utf-8-sig",)
    # A nonempty credential assignment/ASCII key encoded in UTF-16/32 must
    # contain NUL; non-ASCII bytes also retain the Unicode-only registry case.
    # Plain ASCII without NUL has only one text interpretation relevant to
    # these rules, so avoid four full impossible wide-text scans.
    if b"\0" in content or not content.isascii():
        encodings += ("utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
    for encoding in encodings:
        try:
            value = content.decode(encoding, errors="strict")
        except UnicodeError:
            continue
        views.add(value)
    # An embedded text segment may not align with the surrounding binary asset
    # or tar header. Removing NUL only in an inspection view catches ASCII
    # UTF-16/32 literals without modifying the accepted bytes.
    if b"\0" in content:
        views.add(content.replace(b"\0", b"").decode("ascii", errors="replace"))
    for value in views:
        validate_source_text(value)


def validate_source_archive_credentials(content: bytes) -> None:
    """Credential-check byte attachments; archive shape remains its own policy.

    The immutable BLOB store historically accepts opaque bytes. For actual
    uncompressed Source tar attachments, bounded member inspection handles
    encoding boundaries exactly; malformed/non-tar bytes are still scanned
    and the Sandbox's canonical-archive validator rejects their structure.
    """
    validate_source_bytes(content)
    try:
        archive = tarfile.open(fileobj=io.BytesIO(content), mode="r:")
    except tarfile.TarError:
        return
    with archive:
        total = 0
        count = 0
        try:
            for member in archive:
                if not member.isfile():
                    continue
                count += 1
                if (member.size < 0 or member.size > _MAX_FILE_BYTES or count > _MAX_FILES):
                    raise SourceCredentialError()
                total += member.size
                if total > _MAX_TOTAL_BYTES:
                    raise SourceCredentialError()
                stream = archive.extractfile(member)
                if stream is None:
                    raise SourceCredentialError()
                value = stream.read(_MAX_FILE_BYTES + 1)
                if len(value) != member.size:
                    raise SourceCredentialError()
                validate_source_bytes(value)
        except (tarfile.TarError, OSError, OverflowError):
            raise SourceCredentialError() from None
