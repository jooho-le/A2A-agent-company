# "어떤 코드를 어떤 환경에서 검사했는가"를 적은 신분증(Execution Manifest)을 확인하는 파일이다.
# 결과에 붙어 온 신분증이 "우리가 채점하려던 신분증"과 똑같을 때만 그 결과를 집계한다.
#
# 왜 필요한가?
#   서로 다른 버전의 코드로 시험을 본 결과가 한데 섞이면, 결국 어느 코드도 제대로 검증됐다고 말할 수 없게 된다.
#   그래서 신분증이 다르면 결과를 아예 쓰지 않는다.
#
# 이 파일은 신분증의 "글자 모양"(길이·접두사 등)만 확인한다.
# 실제 파일이나 컨테이너의 해시를 직접 계산해서 맞는지 확인하지는 않는다.
#
# [처음 읽는 분을 위한 용어]
# - Manifest(명세서): "어떤 코드를, 어떤 환경에서 검사했는가"를 적은 신분증. 코드와 환경 정보를 9개 값으로 묶는다.
#   채점 결과가 "우리가 채점하려던 바로 그 코드"의 것인지 확인하는 데 쓴다.
# - 해시(hash): 파일 내용을 글자 수가 일정한 지문(예: 64자리 16진수)으로 줄인 값. 내용이 한 글자만 달라도 지문이 달라진다.
# - 커밋(commit): Git에서 저장해 둔 코드의 한 시점. tree는 그 시점의 파일 구성이다.
# - 컨테이너 이미지 digest: 검사를 돌린 실행 환경(Docker 이미지)의 지문.
from dataclasses import dataclass, fields  # fields(): 데이터 상자에 어떤 필드가 있는지 알려 주는 도구
import re  # 정규식(글자 모양 규칙)으로 형식을 검사하는 도구
from typing import Iterable, Optional

from .aggregation import CheckPlan, CheckResult, _uuid4, aggregate  # 같은 폴더의 aggregation.py에서 가져온다


# 이름 짝짓기 표: 외부와 주고받는 JSON에서는 repositoryId처럼 쓰고(camelCase), 파이썬 안에서는 repository_id처럼 쓴다(snake_case).
# 팀 공통 스키마(약속된 JSON 모양)의 9개 필드와 똑같아야 한다.
MANIFEST_FIELDS = {
    "repositoryId": "repository_id", "codeVersion": "code_version",
    "projectArtifactId": "project_artifact_id", "gitObjectFormat": "git_object_format",
    "commitHash": "commit_hash", "treeHash": "tree_hash",
    "snapshotSha256": "snapshot_sha256", "containerImageDigest": "container_image_digest",
    "dependencyLockHash": "dependency_lock_hash",
}


@dataclass(frozen=True)  # frozen: 만든 뒤에는 값을 바꿀 수 없다(신분증을 몰래 고치지 못하게)
class ExecutionManifest:
    # 코드 정보(커밋·트리·스냅샷 해시, 버전 1~4)와 환경 정보(컨테이너·의존성 해시)를 한데 묶은, 바꿀 수 없는 값이다.
    #
    # 9개 필드가 하나도 빠짐없이 같아야 "같은 신분증"이다.
    # 비교 기준이 되는 값(expected)은 결과에 딸려 온 값이 아니라, 믿을 수 있는 실행 설정에서 받아야 한다.
    repository_id: str            # 저장소(코드 보관함)를 구분하는 이름
    code_version: int             # 코드 버전. 처음 만든 코드가 1이고 수정할 때마다 1씩 늘어 최대 4
    project_artifact_id: str      # 이 소스 산출물의 고유 번호(UUIDv4)
    git_object_format: str        # Git 해시 방식: "sha1"(40자리) 또는 "sha256"(64자리)
    commit_hash: str              # 검사한 커밋의 전체 해시
    tree_hash: str                # 그 커밋의 파일 구성을 나타내는 해시
    snapshot_sha256: str          # 고정(동결)한 코드 묶음 전체의 SHA-256 해시
    container_image_digest: str   # 실행 환경 이미지의 해시("sha256:" + 64자리)
    dependency_lock_hash: str     # 의존성(설치한 라이브러리 버전 목록) 잠금 파일의 해시

    def to_payload(self) -> dict:
        # 파이썬 객체를 외부와 주고받는 JSON용 사전(dict)으로 바꾼다. 이름은 camelCase(예: repositoryId)가 된다.
        # getattr(self, "repository_id")는 self.repository_id와 같은 뜻이다(이름을 글자로 줄 수 있을 뿐).
        return {external: getattr(self, internal) for external, internal in MANIFEST_FIELDS.items()}

    @classmethod  # 객체를 만들기 전에 부르는 함수. ExecutionManifest.from_payload(...) 처럼 쓴다.
    def from_payload(cls, payload: dict) -> "ExecutionManifest":
        # JSON 사전(dict)에서 신분증 객체를 만든다. 필요한 9개 필드가 빠졌거나 모르는 필드가 섞여 있으면 거부한다.
        # set(payload)는 사전의 키 목록이다. 기대하는 9개 키와 정확히 같아야 한다.
        if not isinstance(payload, dict) or set(payload) != set(MANIFEST_FIELDS):
            raise ValueError("Manifest has missing or unknown fields")
        # cls(**{...})는 사전의 키-값을 이름 붙은 인자로 풀어서 생성자에 넘긴다.
        return cls(**{internal: payload[external] for external, internal in MANIFEST_FIELDS.items()})

    def __post_init__(self):
        # 만들어진 직후의 형식 검사: 빈 글자가 없는지, 버전이 1~4인지, UUID가 맞는지, 해시 길이와 접두사가 맞는지 본다.
        # __post_init__은 데이터 상자가 만들어진 직후 파이썬이 자동으로 부르는 특별한 함수다.
        for field in fields(self):
            if field.name == "code_version":
                continue  # 버전은 숫자라서 아래에서 따로 검사한다.
            value = getattr(self, field.name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field.name} must be a nonempty string")
        # bool은 int의 하위 타입이라 type(...) is int로 걸러낸다.
        # (파이썬에서는 True가 숫자 1로도 취급되므로 isinstance로는 True를 걸러낼 수 없다.)
        if type(self.code_version) is not int or not 1 <= self.code_version <= 4:
            raise ValueError("code_version must be an integer from 1 to 4")
        _uuid4(self.project_artifact_id)
        if self.git_object_format not in ("sha1", "sha256"):
            raise ValueError("Unsupported git_object_format")
        # 줄여 쓴 해시나 브랜치 이름(main 등)이 아니라 "전체 해시"여야 한다(sha1은 40자리, sha256은 64자리).
        # 브랜치 이름은 가리키는 코드가 계속 바뀔 수 있어서 "특정 시점의 코드"를 가리키는 증거가 될 수 없다.
        width = 40 if self.git_object_format == "sha1" else 64
        for name in ("commit_hash", "tree_hash"):
            # 정규식 [0-9a-f]{40} 은 "0~9, a~f 글자가 정확히 40개"라는 뜻이다. fullmatch는 문자열 전체가 그래야 통과한다.
            if not re.fullmatch(r"[0-9a-f]{%d}" % width, getattr(self, name)):
                raise ValueError(f"{name} must be a full Git object ID")
        if not re.fullmatch(r"[0-9a-f]{64}", self.snapshot_sha256):
            raise ValueError("snapshot_sha256 must be a SHA-256 hex digest")
        # 컨테이너·의존성 해시는 "sha256:"로 시작하고 그 뒤에 64자리 소문자 16진수가 온다.
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
    # 결과에 붙어 온 신분증(observed)이 기준 신분증(expected)과 같을 때만 결과를 집계한다.
    #
    # 신분증이 없거나 다르면 결과를 하나도 쓰지 않는다. PASS든 FAIL이든 인정하지 않고 전체 판정을 UNVERIFIED(판정 불가)로 둔다.
    # 그리고 manifestCheck 항목에 오류 코드(MANIFEST_MISSING = 없음 / MANIFEST_MISMATCH = 다름)와
    # 값이 다른 필드 이름 목록을 남긴다.
    #
    # expected = "채점하려던 코드·환경"(믿을 수 있는 실행 설정에서 받은 기준값)
    # observed = "결과가 실제로 나온 코드·환경"(결과에 붙어 온 값)
    # 두 값이 같아야 "우리가 의도한 코드의 결과"라고 믿을 수 있다.
    if not isinstance(expected, ExecutionManifest):
        raise ValueError("A trusted expected ExecutionManifest is required")
    if observed is not None and not isinstance(observed, ExecutionManifest):
        raise ValueError("observed must be an ExecutionManifest or None")
    # 값이 다른 필드의 이름 목록을 만든다. observed가 없으면 비교할 수 없으므로 빈 목록이다.
    mismatches = ([] if observed is None else [
        field.name for field in fields(expected)
        if getattr(expected, field.name) != getattr(observed, field.name)
    ])
    # 오류 코드: observed가 없으면 MISSING, 있는데 다른 필드가 있으면 MISMATCH, 문제 없으면 None.
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
    # manifestCheck는 "신분증 확인 결과"이다. metadataMatches가 True일 때만 결과를 믿을 수 있다.
    summary["manifestCheck"] = {
        "errorCode": error,
        "mismatchedFields": mismatches,
        "metadataMatches": error is None,
    }
    return summary
