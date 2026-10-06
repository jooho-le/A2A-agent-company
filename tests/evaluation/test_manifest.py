"""manifest.py 테스트: 다른 코드·환경의 결과가 섞이지 않고, 잘못된 형식은 생성 시 거부되는지."""
from dataclasses import replace
from uuid import UUID

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.manifest import ExecutionManifest, aggregate_report


def manifest():
    """테스트 기준 Manifest(가짜 값)."""
    return ExecutionManifest(
        repository_id="membership", code_version=1,
        project_artifact_id=str(UUID(int=1, version=4)),
        git_object_format="sha1", commit_hash="a" * 40, tree_hash="b" * 40,
        snapshot_sha256="c" * 64, container_image_digest="sha256:" + "d" * 64,
        dependency_lock_hash="sha256:" + "e" * 64,
    )


def report(observed, status=Status.PASS):
    """검사 1개를 기준 Manifest와 observed로 집계한다."""
    test_id = str(UUID(int=2, version=4))
    return aggregate_report(
        [CheckPlan(test_id)],
        [CheckResult(test_id, status, (str(UUID(int=3, version=4)),))],
        expected=manifest(), observed=observed,
    )


@pytest.mark.parametrize("status,verdict", [
    (Status.PASS, "PASS"), (Status.FAIL, "FAIL"), (Status.ERROR, "UNVERIFIED"),
])
def test_matching_manifest_preserves_check_verdict(status, verdict):
    """Manifest가 같으면 검사 결과의 의미가 그대로 유지된다."""
    summary = report(manifest(), status)
    assert summary["validationVerdict"] == verdict
    assert summary["manifestCheck"]["metadataMatches"] is True
    assert "finalVerdict" not in summary


@pytest.mark.parametrize("change", [
    {"repository_id": "another"}, {"code_version": 2},
    {"project_artifact_id": str(UUID(int=4, version=4))},
    {"commit_hash": "f" * 40}, {"tree_hash": "f" * 40},
    {"snapshot_sha256": "f" * 64},
    {"container_image_digest": "sha256:" + "f" * 64},
    {"dependency_lock_hash": "sha256:" + "f" * 64},
    {"git_object_format": "sha256", "commit_hash": "a" * 64, "tree_hash": "b" * 64},
])
def test_each_identity_difference_blocks_pass(change):
    """9개 필드 중 하나라도 다르면 PASS를 쓰지 못하고, 어떤 필드가 다른지 기록된다."""
    summary = report(replace(manifest(), **change))
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["requiredPassRate"] is None  # 측정 못 한 것을 0%로 읽지 않는다
    assert {row["errorCode"] for row in summary["results"]} == {"MANIFEST_MISMATCH"}
    assert set(summary["manifestCheck"]["mismatchedFields"]) == set(change)
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISMATCH"


def test_missing_manifest_blocks_pass():
    """결과가 어떤 코드에서 나왔는지 모르면(observed=None) 통과로 인정하지 않는다."""
    summary = report(None)
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISSING"


def test_failure_from_old_source_is_not_attributed_to_new_source():
    """옛 코드(CODE-2가 아닌 다른 버전)의 FAIL을 현재 코드의 확인된 실패로 옮겨 적지 않는다."""
    summary = report(replace(manifest(), code_version=2), Status.FAIL)
    assert summary["confirmedFailureIds"] == []
    assert len(summary["unverifiedTestIds"]) == 1


@pytest.mark.parametrize("change", [
    {"code_version": True},                       # bool은 int로 취급하지 않음
    {"code_version": 0}, {"code_version": 5},     # 허용 범위 1~4 밖
    {"project_artifact_id": "CODE-1"},            # UUID가 아닌 표시 이름
    {"git_object_format": "md5"},                 # 지원하지 않는 해시 방식
    {"commit_hash": "main"},                      # 해시 대신 브랜치 이름
    {"tree_hash": "abcdef"},                      # 줄임 해시
    {"snapshot_sha256": "abc"},                   # 길이 부족
    {"repository_id": " "},                       # 공백뿐인 문자열
    {"container_image_digest": None}, {"dependency_lock_hash": ""},  # 값 없음
    {"container_image_digest": "latest"},         # 태그 이름(내용이 바뀔 수 있음)
    {"container_image_digest": "a" * 64},         # "sha256:" 접두사 없음
    {"dependency_lock_hash": "a" * 64},           # "sha256:" 접두사 없음
    {"dependency_lock_hash": "sha256:" + "G" * 64},  # 16진수가 아닌 문자
])
def test_invalid_manifest_rejected(change):
    """형식이 잘못된 Manifest는 객체를 만드는 순간 ValueError로 거부된다."""
    with pytest.raises(ValueError):
        replace(manifest(), **change)


def test_missing_trusted_expectation_rejected():
    """비교 기준(expected)이 없으면 집계 자체를 거부한다."""
    with pytest.raises(ValueError):
        aggregate_report([], [], expected=None, observed=manifest())
