"""One immutable directory policy shared by public metadata and runtime access."""

from dataclasses import dataclass
from enum import Enum
from pathlib import PureWindowsPath
from types import MappingProxyType
from uuid import UUID, RFC_4122

from orchestrator.domain.states import AgentRole


class WorkspaceErrorCode(str, Enum):
    NOT_FOUND = "WORKSPACE_NOT_FOUND"
    IDENTITY = "WORKSPACE_IDENTITY_MISMATCH"
    ROOT = "WORKSPACE_ROOT_MISMATCH"
    NOT_PROVISIONED = "WORKSPACE_NOT_PROVISIONED"
    CONFLICT = "WORKSPACE_PROVISION_CONFLICT"
    PATH_DENIED = "PATH_DENIED"
    PERMISSION_DENIED = "WORKSPACE_PERMISSION_DENIED"
    FILE_NOT_FOUND = "FILE_NOT_FOUND"
    IO = "WORKSPACE_IO_ERROR"
    PLATFORM = "WORKSPACE_PLATFORM_UNSUPPORTED"


class WorkspaceAccessError(RuntimeError):
    """No input path, Host root, secret, or underlying exception in the message."""
    def __init__(self, code: WorkspaceErrorCode):
        self.code = WorkspaceErrorCode(code)
        super().__init__(self.code.value)


class WorkspaceAccess(str, Enum):
    READ = "READ"
    WRITE = "WRITE"


@dataclass(frozen=True)
class RolePermissions:
    read: tuple[str, ...]
    write: tuple[str, ...]


_PRODUCT_READ = ("planning/", "source/", "snapshots/", "outputs/qa/", "outputs/security/")
WORKSPACE_PERMISSIONS = MappingProxyType({
    AgentRole.PLANNER: RolePermissions(read=("planning/",), write=("planning/",)),
    AgentRole.DEVELOPER: RolePermissions(read=_PRODUCT_READ, write=("source/",)),
    AgentRole.QA: RolePermissions(read=_PRODUCT_READ, write=("outputs/qa/",)),
    AgentRole.SECURITY: RolePermissions(read=_PRODUCT_READ, write=("outputs/security/",)),
})
WORKSPACE_LAYOUT = ("planning", "source", "snapshots", "outputs/qa", "outputs/security")
LAYOUT_VERSION = 1
OWNER_MARKER = ".workspace.json"

_SECRET_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".codex", ".agents", ".gnupg", ".kube",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "secrets", "secrets.json", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "docker.sock", OWNER_MARKER,
})


def workspace_uuid(value: UUID | str) -> UUID:
    try:
        if not isinstance(value, (str, UUID)):
            raise ValueError("UUID required")
        parsed = value if isinstance(value, UUID) else UUID(value)
        if parsed.version != 4 or parsed.variant != RFC_4122:
            raise ValueError("UUID4 required")
        return parsed
    except (ValueError, TypeError, AttributeError):
        raise WorkspaceAccessError(WorkspaceErrorCode.IDENTITY) from None


def relative_parts(path: str) -> tuple[str, ...]:
    """Strict POSIX relative syntax; reject before normalization or any I/O."""
    if (
        not isinstance(path, str) or not path.strip() or path.startswith("/")
        or "\\" in path or PureWindowsPath(path).drive
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
        or len(path.encode("utf-8", errors="surrogatepass")) > 4096
    ):
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    parts = tuple(path.split("/"))
    if any(part in ("", ".", "..") or ":" in part for part in parts):
        raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    for part in parts:
        name = part.casefold()
        if name in _SECRET_NAMES or name.startswith(".env") or name.endswith((".pem", ".key", ".p12", ".pfx")):
            raise WorkspaceAccessError(WorkspaceErrorCode.PATH_DENIED)
    return parts


def authorize_path(role: AgentRole, path: str, access: WorkspaceAccess) -> tuple[str, ...]:
    parts = relative_parts(path)
    if not isinstance(role, AgentRole) or not isinstance(access, WorkspaceAccess):
        raise WorkspaceAccessError(WorkspaceErrorCode.PERMISSION_DENIED)
    permissions = WORKSPACE_PERMISSIONS[role]
    grants = permissions.read if access is WorkspaceAccess.READ else permissions.write
    if not any(parts[:len(prefix)] == prefix for prefix in (tuple(grant.rstrip("/").split("/")) for grant in grants)):
        raise WorkspaceAccessError(WorkspaceErrorCode.PERMISSION_DENIED)
    return parts


def public_permissions() -> dict[str, dict[str, object]]:
    result = {}
    for role, grant in WORKSPACE_PERMISSIONS.items():
        result[role.value] = {"read": list(grant.read), "write": list(grant.write)}
        if role is not AgentRole.PLANNER:
            result[role.value]["snapshot"] = "READ_ONLY"
    return result
