import json
import unittest
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import (
    AgentRole,
    BuildReportArtifact,
    ChangeReportArtifact,
    CodeSnapshotArtifact,
    GitObjectFormat,
)


class DeveloperArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = uuid4()
        self.step_id = uuid4()
        self.requirement_ids = (uuid4(),)
        self.task_id = "developer-task-opaque"
        self.source_a2a_id = "developer-source-a2a"
        self.source = CodeSnapshotArtifact(
            artifact_version=1,
            run_id=self.run_id,
            workflow_step_id=self.step_id,
            a2a_task_id=self.task_id,
            a2a_artifact_id=self.source_a2a_id,
            requirement_ids=self.requirement_ids,
            code_version=1,
            repository_id="a2a-agent-company",
            commit_hash="a" * 40,
            git_object_format=GitObjectFormat.SHA1,
            tree_hash="b" * 40,
            snapshot_sha256="c" * 64,
            artifact_uri=f"registry://source/{uuid4()}/v1",
            container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash="sha256:" + "e" * 64,
        )

    def make_change(self, **overrides) -> ChangeReportArtifact:
        values = {
            "artifactId": uuid4(),
            "artifactVersion": 1,
            "runId": self.run_id,
            "workflowStepId": self.step_id,
            "a2aTaskId": self.task_id,
            "a2aArtifactId": "developer-change-a2a",
            "requirementIds": self.requirement_ids,
            "codeVersion": 1,
            "summary": "구현 변경 내용을 요약한다.",
            "fileChanges": [{"path": "src/signup.py", "action": "ADDED"}],
        }
        values.update(overrides)
        return ChangeReportArtifact.model_validate(values)

    def make_build(self, **overrides) -> BuildReportArtifact:
        values = {
            "artifactId": uuid4(),
            "artifactVersion": 1,
            "runId": self.run_id,
            "workflowStepId": self.step_id,
            "a2aTaskId": self.task_id,
            "a2aArtifactId": "developer-build-a2a",
            "requirementIds": self.requirement_ids,
            "codeVersion": 1,
            "sourceArtifactId": self.source.artifact_id,
            "exitCode": 0,
            "durationMs": 100,
            "executionManifestId": uuid4(),
            "executionManifest": self.source.execution_manifest().model_dump(
                mode="json", by_alias=True
            ),
            "stdoutRef": None,
            "stderrRef": None,
        }
        values.update(overrides)
        return BuildReportArtifact.model_validate(values)

    def test_change_report_is_immutable_and_paths_are_normalized_relative_paths(self) -> None:
        change = self.make_change()
        self.assertEqual(change.created_by, AgentRole.DEVELOPER)
        with self.assertRaises(ValidationError):
            change.summary = "mutated"

        unsafe_paths = (
            "../secret",
            "/etc/passwd",
            "src/../secret",
            "C:/secret",
            "src\\secret",
        )
        for unsafe_path in unsafe_paths:
            with self.subTest(path=unsafe_path), self.assertRaises(ValidationError):
                self.make_change(fileChanges=[{"path": unsafe_path, "action": "MODIFIED"}])

    def test_build_report_records_integer_tool_result_and_snapshot_identity(self) -> None:
        report = self.make_build(exitCode=0.0, durationMs=100.0)
        self.assertTrue(report.passed)
        self.assertEqual(report.exit_code, 0)

        failed_build = self.make_build(exitCode=2)
        self.assertFalse(failed_build.passed)

        with self.assertRaises(ValidationError):
            self.make_build(exitCode=0.5)
        with self.assertRaises(ValidationError):
            self.make_build(durationMs=-1)

    def test_build_report_requires_the_source_snapshot_identity(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_build(sourceArtifactId=uuid4())

        # The report model can check its source ID/code version. The parser compares
        # every Manifest field with the Source Artifact; SQLite checks predecessor existence.
        manifest = self.source.execution_manifest()
        self.assertEqual(self.make_build().execution_manifest, manifest)

    def test_lineage_requires_a_previous_registry_artifact_after_version_one(self) -> None:
        with self.assertRaises(ValidationError):
            self.make_change(artifactVersion=2)
        next_report = self.make_change(
            artifactVersion=2,
            previousArtifactId=uuid4(),
            artifactId=uuid4(),
        )
        self.assertEqual(next_report.artifact_version, 2)

    def test_shared_developer_artifact_schemas_are_valid_json_and_strict_objects(self) -> None:
        schema_root = Path(__file__).resolve().parents[1] / "schemas" / "project"
        for schema_name in (
            "developer_artifact_metadata.schema.json",
            "developer_source_snapshot.schema.json",
            "developer_change_report.schema.json",
            "developer_build_report.schema.json",
        ):
            with self.subTest(schema=schema_name):
                schema = json.loads((schema_root / schema_name).read_text(encoding="utf-8"))
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertTrue(schema["required"])


if __name__ == "__main__":
    unittest.main()
