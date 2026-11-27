import hashlib
import json
import re
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import (
    AgentRole,
    CodeSnapshotArtifact,
    ExecutionManifest,
    GitObjectFormat,
    SnapshotHandoff,
    SnapshotIntegrityError,
    SnapshotMismatchError,
    SnapshotReadGrant,
    assert_same_execution_snapshot,
    code_version_for_fix_attempt,
    verify_snapshot_archive,
)


class SnapshotHandoffTests(unittest.TestCase):
    archive = b"immutable, normalized source archive"

    def make_snapshot(self, **updates: object) -> CodeSnapshotArtifact:
        values: dict[str, object] = {
            "artifact_version": 1,
            "run_id": uuid4(),
            "workflow_step_id": uuid4(),
            "a2a_task_id": "developer-task-opaque-id",
            "a2a_artifact_id": "developer-artifact-opaque-id",
            "requirement_ids": (uuid4(), uuid4()),
            "code_version": 1,
            "repository_id": "a2a-agent-company",
            "commit_hash": "a" * 40,
            "git_object_format": GitObjectFormat.SHA1,
            "tree_hash": "b" * 40,
            "snapshot_sha256": hashlib.sha256(self.archive).hexdigest(),
            "artifact_uri": f"registry://source/{uuid4()}/versions/1",
            "container_image_digest": "sha256:" + "c" * 64,
            "dependency_lock_hash": "sha256:" + "d" * 64,
        }
        values.update(updates)
        return CodeSnapshotArtifact(**values)

    def test_source_artifact_is_immutable_and_keeps_project_and_a2a_ids_separate(self) -> None:
        snapshot = self.make_snapshot()
        self.assertNotEqual(str(snapshot.artifact_id), snapshot.a2a_artifact_id)
        self.assertEqual(snapshot.artifact_type, "SOURCE")
        with self.assertRaises(ValidationError):
            snapshot.code_version = 2

    def test_snapshot_requires_full_git_hashes_and_artifact_registry_uri(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_snapshot(commit_hash="abc")
        with self.assertRaises(ValidationError):
            self.make_snapshot(code_version=5)
        with self.assertRaises(ValidationError):
            self.make_snapshot(artifact_uri="/tmp/source.tar")
        with self.assertRaises(ValidationError):
            self.make_snapshot(artifact_uri="file:///tmp/source.tar")

        sha256_git_snapshot = self.make_snapshot(
            commit_hash="a" * 64,
            git_object_format=GitObjectFormat.SHA256,
            tree_hash="b" * 64,
        )
        self.assertEqual(
            sha256_git_snapshot.execution_manifest().git_object_format,
            GitObjectFormat.SHA256,
        )

    def test_artifact_lineage_and_developer_ownership_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_snapshot(artifact_version=2)
        with self.assertRaises(ValidationError):
            self.make_snapshot(created_by=AgentRole.QA)

        previous_id = uuid4()
        next_snapshot = self.make_snapshot(
            artifact_version=2,
            previous_artifact_id=previous_id,
            code_version=2,
        )
        self.assertEqual(next_snapshot.previous_artifact_id, previous_id)

    def test_handoff_gives_qa_and_security_read_only_access_to_same_artifact(self) -> None:
        snapshot = self.make_snapshot()
        handoff = SnapshotHandoff.from_snapshot(snapshot)

        self.assertEqual(handoff.project_artifact_id, snapshot.artifact_id)
        self.assertEqual(handoff.artifact_uri, snapshot.artifact_uri)
        self.assertEqual(
            {grant.recipient for grant in handoff.grants},
            {AgentRole.QA, AgentRole.SECURITY},
        )
        self.assertTrue(all(grant.access == "READ_ONLY" for grant in handoff.grants))
        payload = handoff.execution_manifest.model_dump(mode="json", by_alias=True)
        self.assertEqual(
            payload["projectArtifactId"],
            str(snapshot.artifact_id),
        )

        with self.assertRaises(ValidationError):
            SnapshotReadGrant(
                project_artifact_id=snapshot.artifact_id,
                recipient=AgentRole.QA,
                access="READ_WRITE",
            )

    def test_handoff_rejects_missing_or_wrong_recipient_grants(self) -> None:
        snapshot = self.make_snapshot()
        manifest = snapshot.execution_manifest()
        with self.assertRaises(ValidationError):
            SnapshotHandoff(
                run_id=snapshot.run_id,
                project_artifact_id=snapshot.artifact_id,
                artifact_uri=snapshot.artifact_uri,
                execution_manifest=manifest,
                grants=(),
            )

    def test_build_qa_and_security_must_use_identical_snapshot_and_environment(self) -> None:
        snapshot = self.make_snapshot()
        manifest = snapshot.execution_manifest()
        self.assertEqual(
            assert_same_execution_snapshot(manifest, manifest, manifest), manifest
        )

        manifest_values = manifest.model_dump()
        manifest_values["container_image_digest"] = "sha256:" + "e" * 64
        other_environment = ExecutionManifest(**manifest_values)
        with self.assertRaises(SnapshotMismatchError):
            assert_same_execution_snapshot(manifest, manifest, other_environment)

    def test_registry_archive_hash_is_verified_before_use(self) -> None:
        snapshot = self.make_snapshot()
        verify_snapshot_archive(snapshot, self.archive)
        with self.assertRaises(SnapshotIntegrityError):
            verify_snapshot_archive(snapshot, self.archive + b" tampered")

    def test_code_version_is_initial_candidate_plus_fix_attempt(self) -> None:
        self.assertEqual(
            [code_version_for_fix_attempt(attempt) for attempt in range(4)],
            [1, 2, 3, 4],
        )
        with self.assertRaises(ValueError):
            code_version_for_fix_attempt(4)

    def test_execution_manifest_schema_has_exact_camel_case_contract(self) -> None:
        root = Path(__file__).resolve().parents[1]
        schema_path = (
            root / "schemas" / "project" / "snapshot_execution_manifest.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        expected_fields = {
            "repositoryId",
            "codeVersion",
            "projectArtifactId",
            "commitHash",
            "gitObjectFormat",
            "treeHash",
            "snapshotSha256",
            "containerImageDigest",
            "dependencyLockHash",
        }
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), expected_fields)

        doc = (root / "docs" / "05-code-handoff.md").read_text(encoding="utf-8")
        manifest_match = re.search(r"```json\s*(.*?)\s*```", doc, re.DOTALL)
        self.assertIsNotNone(manifest_match)
        manifest_payload = json.loads(manifest_match.group(1))
        manifest = ExecutionManifest.model_validate(manifest_payload)
        self.assertEqual(set(manifest_payload), expected_fields)
        self.assertEqual(manifest.code_version, 2)


if __name__ == "__main__":
    unittest.main()
