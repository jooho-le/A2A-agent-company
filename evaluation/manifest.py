"""Local manifest binding draft; no Registry or artifact-byte verification.

The caller supplies the trusted expected manifest, resolved for this run and
attempt. Never derive it from the report being checked.
"""
from dataclasses import dataclass, fields
import re
from typing import Iterable, Optional

from .aggregation import CheckPlan, CheckResult, _uuid4, aggregate


MANIFEST_FIELDS = {
    "repositoryId": "repository_id", "codeVersion": "code_version",
    "projectArtifactId": "project_artifact_id", "gitObjectFormat": "git_object_format",
    "commitHash": "commit_hash", "treeHash": "tree_hash",
    "snapshotSha256": "snapshot_sha256", "containerImageDigest": "container_image_digest",
    "dependencyLockHash": "dependency_lock_hash",
}


@dataclass(frozen=True)
class ExecutionManifest:
    repository_id: str
    code_version: int
    project_artifact_id: str
    git_object_format: str
    commit_hash: str
    tree_hash: str
    snapshot_sha256: str
    container_image_digest: str
    dependency_lock_hash: str

    def to_payload(self) -> dict:
        """Serialize the common camelCase contract without inventing IDs."""
        return {external: getattr(self, internal) for external, internal in MANIFEST_FIELDS.items()}

    @classmethod
    def from_payload(cls, payload: dict) -> "ExecutionManifest":
        if not isinstance(payload, dict) or set(payload) != set(MANIFEST_FIELDS):
            raise ValueError("Manifest has missing or unknown fields")
        return cls(**{internal: payload[external] for external, internal in MANIFEST_FIELDS.items()})

    def __post_init__(self):
        for field in fields(self):
            if field.name == "code_version":
                continue
            value = getattr(self, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field.name} must be a nonempty string")
        if type(self.code_version) is not int or not 1 <= self.code_version <= 4:
            raise ValueError("code_version must be an integer from 1 to 4")
        _uuid4(self.project_artifact_id)
        if self.git_object_format not in ("sha1", "sha256"):
            raise ValueError("Unsupported git_object_format")
        width = 40 if self.git_object_format == "sha1" else 64
        for name in ("commit_hash", "tree_hash"):
            if not re.fullmatch(r"[0-9a-f]{%d}" % width, getattr(self, name)):
                raise ValueError(f"{name} must be a full Git object ID")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be a SHA-256 hex digest")
        # Matches dev 2b4fc60 execution_manifest.schema.json.
        for name in ("container_image_digest", "dependency_lock_hash"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", getattr(self, name)):
                raise ValueError(f"{name} must be a sha256-prefixed digest")


def aggregate_report(
    plan: Iterable[CheckPlan],
    results: Iterable[CheckResult],
    *,
    expected: ExecutionManifest,
    observed: Optional[ExecutionManifest],
) -> dict:
    """Aggregate only results bound to the expected code and environment.

    Missing/mismatched metadata makes this local report UNVERIFIED. Results
    from that report cannot prove either PASS or FAIL for the expected source.
    Retain the original report upstream for audit, without relabeling it.
    """
    if not isinstance(expected, ExecutionManifest):
        raise ValueError("A trusted expected ExecutionManifest is required")
    if observed is not None and not isinstance(observed, ExecutionManifest):
        raise ValueError("observed must be an ExecutionManifest or None")
    mismatches = ([] if observed is None else [
        field.name for field in fields(expected)
        if getattr(expected, field.name) != getattr(observed, field.name)
    ])
    error = ("MANIFEST_MISSING" if observed is None else
             "MANIFEST_MISMATCH" if mismatches else None)
    summary = aggregate(plan, () if error else results)
    if error:
        summary["validationVerdict"] = "UNVERIFIED"
    summary["manifestCheck"] = {
        "errorCode": error,
        "mismatchedFields": mismatches,
        "metadataMatches": error is None,
    }
    return summary
