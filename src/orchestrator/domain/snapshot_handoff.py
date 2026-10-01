"""Immutable source-snapshot metadata and read-only validation handoff."""

from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from re import fullmatch
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, UUID4, field_validator, model_validator

from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS
from orchestrator.domain.states import AgentRole


class GitObjectFormat(str, Enum):
    SHA1 = "sha1"
    SHA256 = "sha256"


class SnapshotIntegrityError(ValueError):
    """Raised when archived snapshot bytes do not match their registered hash."""


class SnapshotMismatchError(ValueError):
    """Raised when Build/QA/Security results refer to different executions."""


class ImmutableDomainModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )


class ExecutionManifest(ImmutableDomainModel):
    """Exact snapshot and environment identity attached to Build/QA/Security."""

    repository_id: str = Field(alias="repositoryId", min_length=1)
    code_version: int = Field(
        alias="codeVersion", ge=1, le=MAX_CODE_FIX_ATTEMPTS + 1
    )
    project_artifact_id: UUID4 = Field(alias="projectArtifactId")
    commit_hash: str = Field(alias="commitHash", min_length=40, max_length=64)
    git_object_format: GitObjectFormat = Field(alias="gitObjectFormat")
    tree_hash: str = Field(alias="treeHash", min_length=40, max_length=64)
    snapshot_sha256: str = Field(alias="snapshotSha256")
    container_image_digest: str = Field(alias="containerImageDigest")
    dependency_lock_hash: str = Field(alias="dependencyLockHash")

    @field_validator("repository_id")
    @classmethod
    def repository_id_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("repository_id must not be blank")
        return value

    @model_validator(mode="after")
    def validate_hash_formats(self) -> "ExecutionManifest":
        git_hash_size = 40 if self.git_object_format == GitObjectFormat.SHA1 else 64
        for field_name in ("commit_hash", "tree_hash"):
            value = getattr(self, field_name)
            if len(value) != git_hash_size or fullmatch(r"[0-9a-f]+", value) is None:
                raise ValueError(
                    f"{field_name} must be a full lowercase {self.git_object_format.value} object ID"
                )
        _require_sha256(self.snapshot_sha256, "snapshot_sha256")
        _require_digest(self.container_image_digest, "container_image_digest")
        _require_digest(self.dependency_lock_hash, "dependency_lock_hash")
        return self


class CodeSnapshotArtifact(ImmutableDomainModel):
    """Project Artifact Registry record for one immutable Developer snapshot."""

    artifact_id: UUID4 = Field(default_factory=uuid4, alias="artifactId")
    artifact_type: Literal["SOURCE"] = Field(default="SOURCE", alias="artifactType")
    artifact_version: int = Field(ge=1, alias="artifactVersion")
    previous_artifact_id: UUID4 | None = Field(default=None, alias="previousArtifactId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str | None = Field(default=None, min_length=1, alias="a2aTaskId")
    a2a_artifact_id: str | None = Field(default=None, min_length=1, alias="a2aArtifactId")
    created_by: AgentRole = Field(default=AgentRole.DEVELOPER, alias="createdBy")
    requirement_ids: tuple[UUID4, ...] = Field(min_length=1, alias="requirementIds")
    code_version: int = Field(
        ge=1, le=MAX_CODE_FIX_ATTEMPTS + 1, alias="codeVersion"
    )
    repository_id: str = Field(min_length=1, alias="repositoryId")
    commit_hash: str = Field(min_length=40, max_length=64, alias="commitHash")
    git_object_format: GitObjectFormat = Field(alias="gitObjectFormat")
    tree_hash: str = Field(min_length=40, max_length=64, alias="treeHash")
    snapshot_sha256: str = Field(alias="snapshotSha256")
    artifact_uri: str = Field(min_length=1, alias="artifactUri")
    container_image_digest: str = Field(alias="containerImageDigest")
    dependency_lock_hash: str = Field(alias="dependencyLockHash")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), alias="createdAt"
    )

    @field_validator("repository_id")
    @classmethod
    def repository_id_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("repository_id must not be blank")
        return value

    @field_validator("a2a_task_id", "a2a_artifact_id")
    @classmethod
    def a2a_reference_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("A2A references must not be blank")
        return value

    @field_validator("requirement_ids")
    @classmethod
    def requirement_ids_must_be_unique(
        cls, values: tuple[UUID4, ...]
    ) -> tuple[UUID4, ...]:
        if len(values) != len(set(values)):
            raise ValueError("requirement_ids must not contain duplicates")
        return values

    @field_validator("artifact_uri")
    @classmethod
    def artifact_uri_must_not_be_a_local_path(cls, value: str) -> str:
        return _validate_artifact_uri(value)

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_artifact(self) -> "CodeSnapshotArtifact":
        if self.created_by != AgentRole.DEVELOPER:
            raise ValueError("a source snapshot must be created by the Developer Agent")
        if (self.a2a_task_id is None) != (self.a2a_artifact_id is None):
            raise ValueError("a2a_task_id and a2a_artifact_id must be supplied together")
        if self.previous_artifact_id == self.artifact_id:
            raise ValueError("an Artifact cannot be its own previous version")
        if self.artifact_version == 1 and self.previous_artifact_id is not None:
            raise ValueError("the first Artifact version must not have a predecessor")
        if self.artifact_version > 1 and self.previous_artifact_id is None:
            raise ValueError("later Artifact versions must reference the previous Artifact")

        git_hash_size = 40 if self.git_object_format == GitObjectFormat.SHA1 else 64
        for field_name in ("commit_hash", "tree_hash"):
            value = getattr(self, field_name)
            if len(value) != git_hash_size or fullmatch(r"[0-9a-f]+", value) is None:
                raise ValueError(
                    f"{field_name} must be a full lowercase {self.git_object_format.value} object ID"
                )
        _require_sha256(self.snapshot_sha256, "snapshot_sha256")
        _require_digest(self.container_image_digest, "container_image_digest")
        _require_digest(self.dependency_lock_hash, "dependency_lock_hash")
        return self

    def execution_manifest(self) -> ExecutionManifest:
        """Return the camelCase contract attached identically to each result."""
        return ExecutionManifest(
            repository_id=self.repository_id,
            code_version=self.code_version,
            project_artifact_id=self.artifact_id,
            commit_hash=self.commit_hash,
            git_object_format=self.git_object_format,
            tree_hash=self.tree_hash,
            snapshot_sha256=self.snapshot_sha256,
            container_image_digest=self.container_image_digest,
            dependency_lock_hash=self.dependency_lock_hash,
        )


class SnapshotReadGrant(ImmutableDomainModel):
    project_artifact_id: UUID4
    recipient: AgentRole
    access: Literal["READ_ONLY"] = "READ_ONLY"

    @model_validator(mode="after")
    def validate_read_grant(self) -> "SnapshotReadGrant":
        if self.recipient not in (AgentRole.QA, AgentRole.SECURITY):
            raise ValueError("frozen source access is only granted to QA or Security")
        return self


class SnapshotHandoff(ImmutableDomainModel):
    run_id: UUID4
    project_artifact_id: UUID4
    artifact_uri: str
    execution_manifest: ExecutionManifest
    grants: tuple[SnapshotReadGrant, ...]

    @field_validator("artifact_uri")
    @classmethod
    def artifact_uri_must_be_a_registry_reference(cls, value: str) -> str:
        return _validate_artifact_uri(value)

    @model_validator(mode="after")
    def validate_recipients(self) -> "SnapshotHandoff":
        recipients = [grant.recipient for grant in self.grants]
        if len(recipients) != 2 or set(recipients) != {
            AgentRole.QA,
            AgentRole.SECURITY,
        }:
            raise ValueError("a handoff must grant read-only access to QA and Security")
        if any(grant.project_artifact_id != self.project_artifact_id for grant in self.grants):
            raise ValueError("all read grants must reference the handed-off Artifact")
        if self.execution_manifest.project_artifact_id != self.project_artifact_id:
            raise ValueError("execution manifest must identify the handed-off Artifact")
        return self

    @classmethod
    def from_snapshot(cls, snapshot: CodeSnapshotArtifact) -> "SnapshotHandoff":
        return cls(
            run_id=snapshot.run_id,
            project_artifact_id=snapshot.artifact_id,
            artifact_uri=snapshot.artifact_uri,
            execution_manifest=snapshot.execution_manifest(),
            grants=(
                SnapshotReadGrant(
                    project_artifact_id=snapshot.artifact_id,
                    recipient=AgentRole.QA,
                ),
                SnapshotReadGrant(
                    project_artifact_id=snapshot.artifact_id,
                    recipient=AgentRole.SECURITY,
                ),
            ),
        )


def code_version_for_fix_attempt(fix_attempt: int) -> int:
    """Map initial candidate/fix cycles to Code Versions 1 through 4."""
    if (
        isinstance(fix_attempt, bool)
        or not isinstance(fix_attempt, int)
        or not 0 <= fix_attempt <= MAX_CODE_FIX_ATTEMPTS
    ):
        raise ValueError(f"fix_attempt must be between 0 and {MAX_CODE_FIX_ATTEMPTS}")
    return fix_attempt + 1


def assert_same_execution_snapshot(
    build: ExecutionManifest,
    qa: ExecutionManifest,
    security: ExecutionManifest,
) -> ExecutionManifest:
    """Require the complete immutable snapshot and environment to be identical."""
    if build != qa or build != security:
        raise SnapshotMismatchError(
            "Build, QA, and Security must reference the same snapshot and environment"
        )
    return build


def verify_snapshot_archive(snapshot: CodeSnapshotArtifact, archive: bytes) -> None:
    """Verify bytes fetched from Artifact Registry against the immutable SHA-256."""
    actual_hash = sha256(archive).hexdigest()
    if actual_hash != snapshot.snapshot_sha256:
        raise SnapshotIntegrityError(
            "fetched source archive does not match the registered snapshot_sha256"
        )


def _require_sha256(value: str, field_name: str) -> None:
    if fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must be a lowercase 64-character SHA-256 hex digest")


def _require_digest(value: str, field_name: str) -> None:
    if fullmatch(r"sha256:[0-9a-f]{64}", value) is None:
        raise ValueError(f"{field_name} must use the sha256:<64-hex> digest format")


def _validate_artifact_uri(value: str) -> str:
    parsed = urlsplit(value)
    if (
        not parsed.scheme
        or parsed.scheme.lower() == "file"
        or len(parsed.scheme) == 1
        or not (parsed.netloc or parsed.path)
        or "\\" in value
        or any(character.isspace() for character in value)
        or (parsed.scheme.lower() in {"http", "https", "s3", "gs"} and not parsed.netloc)
    ):
        raise ValueError("artifact_uri must be a non-local Artifact Registry URI")
    return value
