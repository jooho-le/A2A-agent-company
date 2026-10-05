from dataclasses import replace
from uuid import UUID

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.manifest import ExecutionManifest, aggregate_report


def manifest():
    return ExecutionManifest(
        repository_id="membership", code_version=1,
        project_artifact_id=str(UUID(int=1, version=4)),
        git_object_format="sha1", commit_hash="a" * 40, tree_hash="b" * 40,
        snapshot_sha256="c" * 64, container_image_digest="sha256:" + "d" * 64,
        dependency_lock_hash="sha256:" + "e" * 64,
    )


def report(observed, status=Status.PASS):
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
    summary = report(replace(manifest(), **change))
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["requiredPassRate"] == 0
    assert set(summary["manifestCheck"]["mismatchedFields"]) == set(change)
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISMATCH"


def test_missing_manifest_blocks_pass():
    summary = report(None)
    assert summary["validationVerdict"] == "UNVERIFIED"
    assert summary["manifestCheck"]["errorCode"] == "MANIFEST_MISSING"


def test_failure_from_old_source_is_not_attributed_to_new_source():
    summary = report(replace(manifest(), code_version=2), Status.FAIL)
    assert summary["confirmedFailureIds"] == []
    assert len(summary["unverifiedTestIds"]) == 1


@pytest.mark.parametrize("change", [
    {"code_version": True}, {"code_version": 0}, {"code_version": 5},
    {"project_artifact_id": "CODE-1"}, {"git_object_format": "md5"},
    {"commit_hash": "main"}, {"tree_hash": "abcdef"},
    {"snapshot_sha256": "abc"}, {"repository_id": " "},
    {"container_image_digest": None}, {"dependency_lock_hash": ""},
    {"container_image_digest": "latest"},
    {"container_image_digest": "a" * 64},
    {"dependency_lock_hash": "a" * 64},
    {"dependency_lock_hash": "sha256:" + "G" * 64},
])
def test_invalid_manifest_rejected(change):
    with pytest.raises(ValueError):
        replace(manifest(), **change)


def test_missing_trusted_expectation_rejected():
    with pytest.raises(ValueError):
        aggregate_report([], [], expected=None, observed=manifest())
