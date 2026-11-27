"""Internal content contracts: no Source bytes or Host paths in repr/errors."""

from dataclasses import dataclass, field
from enum import Enum

from orchestrator.domain.snapshot_handoff import ImmutableDomainModel


MAX_CONTENT_BYTES = 20 * 1024 * 1024


class ArtifactErrorCode(str, Enum):
    NOT_FOUND = "ARTIFACT_CONTENT_NOT_FOUND"
    DENIED = "ARTIFACT_ACCESS_DENIED"
    CONFLICT = "ARTIFACT_CONTENT_CONFLICT"
    INTEGRITY = "ARTIFACT_INTEGRITY_ERROR"
    INVALID = "ARTIFACT_INPUT_INVALID"
    CONFIGURATION = "ARTIFACT_ENVIRONMENT_NOT_CONFIGURED"
    GIT_INVALID = "SNAPSHOT_GIT_INVALID"
    TOO_LARGE = "ARTIFACT_TOO_LARGE"
    TIMEOUT = "SNAPSHOT_TIMEOUT"
    PATH_DENIED = "PATH_DENIED"
    IO = "ARTIFACT_STORAGE_ERROR"


class ArtifactAccessError(RuntimeError):
    def __init__(self, code: ArtifactErrorCode):
        self.code = ArtifactErrorCode(code)
        super().__init__(self.code.value)


@dataclass(frozen=True, kw_only=True)
class StoredContent:
    metadata: ImmutableDomainModel = field(repr=False)
    content: bytes = field(repr=False)
    media_type: str
    content_sha256: str
    size_bytes: int

    @property
    def artifact_id(self):
        return self.metadata.artifact_id

    @property
    def run_id(self):
        return self.metadata.run_id
