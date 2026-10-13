"""manifest.py를 시험하는 파일이다.

확인하는 것:
  1) 결과에 붙어 온 코드·환경 신분증(Manifest)이 기준과 다르면 그 결과를 쓰지 않는가?
  2) 모양이 잘못된 신분증은 만드는 순간 거부되는가?
"""
from dataclasses import replace  # 데이터 상자의 값 일부만 바꾼 복사본을 만드는 도구(원본은 그대로)
from uuid import UUID

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.manifest import ExecutionManifest, aggregate_report


def manifest():
    """시험에서 "기준"으로 쓰는 가짜 신분증(Manifest)을 만든다. 해시 자리에는 같은 글자를 반복한 가짜 값을 넣었다."""
    return ExecutionManifest(
        repository_id="membership", code_version=1,
        project_artifact_id=str(UUID(int=1, version=4)),
        git_object_format="sha1", commit_hash="a" * 40, tree_hash="b" * 40,
        snapshot_sha256="c" * 64, container_image_digest="sha256:" + "d" * 64,
        dependency_lock_hash="sha256:" + "e" * 64,
    )


def report(observed, status=Status.PASS):
    """검사 1개의 결과를 "기준 신분증"과 "결과에 붙어 온 신분증(observed)"으로 비교하며 집계한다."""
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
    """신분증이 기준과 같으면 검사 결과의 의미가 그대로 유지된다(PASS→PASS, FAIL→FAIL, ERROR→UNVERIFIED)."""
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
    """신분증의 9개 필드 중 하나라도 다르면 PASS를 인정하지 않고, 어떤 필드가 다른지 기록한다."""
    summary = report(replace(manifest(), **change))
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["requiredPassRate"] is None  # 측정하지 못한 것을 0%(전부 실패)로 읽으면 안 된다
    assert {row["errorCode"] for row in summary["results"]} == {"MANIFEST_MISMATCH"}
    assert set(summary["manifestCheck"]["mismatchedFields"]) == set(change)
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISMATCH"


def test_missing_manifest_blocks_pass():
    """결과가 어떤 코드에서 나왔는지 모르면(observed=None) 통과로 인정하지 않는다."""
    summary = report(None)
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISSING"


def test_failure_from_old_source_is_not_attributed_to_new_source():
    """다른 버전(예: 옛 코드)에서 나온 FAIL을 현재 코드의 "확인된 실패"로 옮겨 적지 않는다."""
    summary = report(replace(manifest(), code_version=2), Status.FAIL)
    assert summary["confirmedFailureIds"] == []
    assert len(summary["unverifiedTestIds"]) == 1


@pytest.mark.parametrize("change", [
    {"code_version": True},                       # True는 숫자 1처럼 동작하지만 버전으로 인정하지 않는다
    {"code_version": 0}, {"code_version": 5},     # 허용 범위(1~4)를 벗어남
    {"project_artifact_id": "CODE-1"},            # UUID가 아니라 사람이 읽는 이름
    {"git_object_format": "md5"},                 # 지원하지 않는 해시 방식
    {"commit_hash": "main"},                      # 해시 대신 브랜치 이름
    {"tree_hash": "abcdef"},                      # 줄여 쓴 해시
    {"snapshot_sha256": "abc"},                   # 길이가 모자람
    {"repository_id": " "},                       # 공백뿐인 글자
    {"container_image_digest": None}, {"dependency_lock_hash": ""},  # 값이 없음
    {"container_image_digest": "latest"},         # 태그 이름(가리키는 내용이 바뀔 수 있다)
    {"container_image_digest": "a" * 64},         # "sha256:" 접두사가 없음
    {"dependency_lock_hash": "a" * 64},           # "sha256:" 접두사가 없음
    {"dependency_lock_hash": "sha256:" + "G" * 64},  # 16진수가 아닌 글자(G)
])
def test_invalid_manifest_rejected(change):
    """모양이 잘못된 신분증은 객체를 만드는 순간 ValueError로 거부된다."""
    with pytest.raises(ValueError):
        replace(manifest(), **change)


def test_missing_trusted_expectation_rejected():
    """비교 기준(expected)이 없으면 집계 자체를 거부한다. 기준 없이는 "같은지" 확인할 수 없기 때문이다."""
    with pytest.raises(ValueError):
        aggregate_report([], [], expected=None, observed=manifest())
