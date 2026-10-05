"""검사 대상 코드·환경의 식별 정보(Execution Manifest)를 검사하고, 기준과 같을 때만 결과를 집계한다.

다른 코드 버전의 결과가 섞이면 어떤 코드도 전부 검증된 것이 아니므로 이를 막는 용도다.
형식만 검사하며 실제 파일·컨테이너 해시를 계산하지는 않는다.
"""
from dataclasses import dataclass, fields
import re
from typing import Iterable, Optional

from .aggregation import CheckPlan, CheckResult, _uuid4, aggregate


# 공통 JSON 필드명(camelCase) → 내부 속성명(snake_case). 공통 스키마의 9개 필드와 같아야 한다.
MANIFEST_FIELDS = {
    "repositoryId": "repository_id", "codeVersion": "code_version",
    "projectArtifactId": "project_artifact_id", "gitObjectFormat": "git_object_format",
    "commitHash": "commit_hash", "treeHash": "tree_hash",
    "snapshotSha256": "snapshot_sha256", "containerImageDigest": "container_image_digest",
    "dependencyLockHash": "dependency_lock_hash",
}


@dataclass(frozen=True)
class ExecutionManifest:
    """코드(커밋·트리·스냅샷 해시, 버전 1~4)와 환경(컨테이너·의존성 해시)을 묶은 불변 값.

    모든 필드가 같아야 같은 Manifest다. 기준값은 결과가 아니라 신뢰된 실행 설정에서 받아야 한다.
    """
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
        """공통 JSON 형식(camelCase) dict로 변환한다."""
        return {external: getattr(self, internal) for external, internal in MANIFEST_FIELDS.items()}

    @classmethod
    def from_payload(cls, payload: dict) -> "ExecutionManifest":
        """공통 JSON 형식 dict에서 만든다. 빠진 필드나 모르는 필드가 있으면 거부한다."""
        if not isinstance(payload, dict) or set(payload) != set(MANIFEST_FIELDS):
            raise ValueError("Manifest has missing or unknown fields")
        return cls(**{internal: payload[external] for external, internal in MANIFEST_FIELDS.items()})

    def __post_init__(self):
        """생성 직후 형식 검사: 빈 문자열, 버전 범위, UUID, 해시 길이·접두사."""
        for field in fields(self):
            if field.name == "code_version":
                continue
            value = getattr(self, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field.name} must be a nonempty string")
        # bool은 int의 하위 타입이라 type(...) is int로 걸러낸다.
        if type(self.code_version) is not int or not 1 <= self.code_version <= 4:
            raise ValueError("code_version must be an integer from 1 to 4")
        _uuid4(self.project_artifact_id)
        if self.git_object_format not in ("sha1", "sha256"):
            raise ValueError("Unsupported git_object_format")
        # 줄임 해시나 브랜치 이름이 아닌 전체 해시여야 한다(sha1 40자리, sha256 64자리).
        width = 40 if self.git_object_format == "sha1" else 64
        for name in ("commit_hash", "tree_hash"):
            if not re.fullmatch(r"[0-9a-f]{%d}" % width, getattr(self, name)):
                raise ValueError(f"{name} must be a full Git object ID")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be a SHA-256 hex digest")
        # dev 2b4fc60 스키마와 같은 "sha256:" + 64자리 형식.
        for name in ("container_image_digest", "dependency_lock_hash"):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", getattr(self, name)):
                raise ValueError(f"{name} must be a sha256-prefixed digest")


def aggregate_report(
    plan: Iterable[CheckPlan],
    results: Iterable[CheckResult],
    *,
    expected: ExecutionManifest,
    observed: Optional[ExecutionManifest],
    sensitive_values: Iterable[str] = (),
) -> dict:
    """observed가 expected와 같을 때만 결과를 집계한다.

    없거나 다르면 결과를 하나도 쓰지 않고(PASS도 FAIL도 인정 안 함) UNVERIFIED로 두며,
    manifestCheck에 오류 코드(MANIFEST_MISSING / MANIFEST_MISMATCH)와 다른 필드 목록을 남긴다.
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
    # 불일치면 빈 결과로 집계해 모든 검사가 NOT_RUN으로 드러나게 한다.
    summary = aggregate(plan, () if error else results, sensitive_values=sensitive_values)
    if error:
        summary["validationVerdict"] = "UNVERIFIED"
        # 측정하지 못한 통과율을 0%(전부 실패)로 읽지 않도록 None으로 둔다.
        summary["requiredPassRate"] = None
        # 검사별 원인도 RESULT_MISSING이 아니라 실제 원인(Manifest 오류)으로 남긴다.
        for row in summary["results"]:
            row["errorCode"] = error
    summary["manifestCheck"] = {
        "errorCode": error,
        "mismatchedFields": mismatches,
        "metadataMatches": error is None,
    }
    return summary
