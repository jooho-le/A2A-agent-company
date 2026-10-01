"""Validate the Developer's completed Task and its immutable output Artifacts."""

from collections.abc import Mapping
from typing import TypeVar
from uuid import UUID

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from pydantic import BaseModel, ConfigDict, ValidationError

from orchestrator.domain import (
    BuildReportArtifact,
    ChangeReportArtifact,
    CodeSnapshotArtifact,
    AgentRole,
    A2ATaskState,
    WorkflowRun,
    WorkflowStep,
    WorkflowStepStatus,
    code_version_for_fix_attempt,
)


class DeveloperOutputValidationError(ValueError):
    """Raised when a completed Developer Task violates the output contract."""


class _OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ValidatedDeveloperOutput(_OutputModel):
    source: CodeSnapshotArtifact
    change_report: ChangeReportArtifact
    build_report: BuildReportArtifact


_ArtifactModel = TypeVar(
    "_ArtifactModel", CodeSnapshotArtifact, ChangeReportArtifact, BuildReportArtifact
)
_ARTIFACT_METADATA_FIELDS = {
    "runId",
    "workflowStepId",
    "projectArtifactId",
    "artifactVersion",
}
_PAYLOAD_FIELDS_BY_MODEL = {
    CodeSnapshotArtifact: {
        "artifactId", "artifactType", "artifactVersion", "previousArtifactId",
        "runId", "workflowStepId", "a2aTaskId", "a2aArtifactId", "createdBy",
        "requirementIds", "codeVersion", "repositoryId", "commitHash",
        "gitObjectFormat", "treeHash", "snapshotSha256", "artifactUri",
        "containerImageDigest", "dependencyLockHash", "createdAt",
    },
    ChangeReportArtifact: {
        "artifactId", "artifactType", "artifactVersion", "previousArtifactId",
        "runId", "workflowStepId", "a2aTaskId", "a2aArtifactId", "createdBy",
        "requirementIds", "codeVersion", "summary", "fileChanges", "createdAt",
    },
    BuildReportArtifact: {
        "artifactId", "artifactType", "artifactVersion", "previousArtifactId",
        "runId", "workflowStepId", "a2aTaskId", "a2aArtifactId", "createdBy",
        "requirementIds", "codeVersion", "sourceArtifactId", "exitCode",
        "durationMs", "executionManifestId", "executionManifest", "stdoutRef",
        "stderrRef", "createdAt",
    },
}


def parse_developer_output(
    task: Task,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
) -> ValidatedDeveloperOutput:
    """Validate Task identity, three project JSON Artifacts, and cross-links."""
    try:
        if task.status is None or TaskState.Name(task.status.state) != "TASK_STATE_COMPLETED":
            raise ValueError("Developer Task is not completed")
        if not task.id.strip() or task.id != step.a2a_task_id:
            raise ValueError("Developer Task ID does not match its WorkflowStep")
        if (
            step.run_id != run.run_id
            or step.agent_role != AgentRole.DEVELOPER
            or step.status != WorkflowStepStatus.SUCCEEDED
            or step.a2a_task_state != A2ATaskState.COMPLETED
        ):
            raise ValueError("Developer WorkflowStep must be completed and belong to this Run")

        by_name: dict[str, list[object]] = {
            "source-snapshot.json": [],
            "change-report.json": [],
            "build-report.json": [],
        }
        for artifact in task.artifacts:
            if artifact.name in by_name:
                by_name[artifact.name].append(artifact)
        if any(len(artifacts) != 1 for artifacts in by_name.values()):
            raise ValueError(
                "Developer Task must return exactly one Source Snapshot, "
                "Change Report, and Build Report Artifact"
            )

        source = _parse_artifact(
            by_name["source-snapshot.json"][0],
            CodeSnapshotArtifact,
            run=run,
            step=step,
            task=task,
        )
        change_report = _parse_artifact(
            by_name["change-report.json"][0],
            ChangeReportArtifact,
            run=run,
            step=step,
            task=task,
        )
        build_report = _parse_artifact(
            by_name["build-report.json"][0],
            BuildReportArtifact,
            run=run,
            step=step,
            task=task,
        )

        expected_requirements = set(step.requirement_ids)
        expected_code_version = code_version_for_fix_attempt(run.fix_attempt)
        records = (source, change_report, build_report)
        if len({record.artifact_id for record in records}) != len(records):
            raise ValueError("Developer project Artifact IDs must be distinct")
        for record in records:
            if set(record.requirement_ids) != expected_requirements:
                raise ValueError("Developer Artifact Requirement IDs differ from its Step")
            if record.code_version != expected_code_version:
                raise ValueError("Developer Artifact codeVersion differs from the Run")

        if build_report.source_artifact_id != source.artifact_id:
            raise ValueError("Build Report references a different Source Artifact")
        if build_report.execution_manifest != source.execution_manifest():
            raise ValueError("Build Report Manifest differs from the Source Snapshot")
        return ValidatedDeveloperOutput(
            source=source,
            change_report=change_report,
            build_report=build_report,
        )
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise DeveloperOutputValidationError(
            "Developer output violates the project Artifact contract"
        ) from exc


def _parse_artifact(
    artifact: object,
    model_type: type[_ArtifactModel],
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    task: Task,
) -> _ArtifactModel:
    if not artifact.artifact_id.strip():
        raise ValueError("Developer A2A Artifact must have an A2A artifactId")
    if len(artifact.parts) != 1:
        raise ValueError(f"{artifact.name} must have one JSON data Part")
    part = artifact.parts[0]
    if part.WhichOneof("content") != "data" or part.media_type != "application/json":
        raise ValueError(f"{artifact.name} must use one application/json data Part")

    metadata = _struct_mapping(artifact.metadata)
    if set(metadata) != _ARTIFACT_METADATA_FIELDS:
        raise ValueError(f"{artifact.name} metadata has unexpected or missing fields")
    if metadata.get("runId") != str(run.run_id):
        raise ValueError(f"{artifact.name} runId does not match the current Run")
    if metadata.get("workflowStepId") != str(step.workflow_step_id):
        raise ValueError(f"{artifact.name} workflowStepId does not match the Developer Step")
    project_artifact_id = metadata.get("projectArtifactId")
    if not isinstance(project_artifact_id, str):
        raise ValueError(f"{artifact.name} metadata must contain projectArtifactId")
    artifact_version = _positive_integer(metadata.get("artifactVersion"))

    payload = MessageToDict(part.data)
    if set(payload) != _PAYLOAD_FIELDS_BY_MODEL[model_type]:
        raise ValueError(f"{artifact.name} payload has unexpected or missing fields")
    if payload.get("a2aTaskId") != task.id:
        raise ValueError(f"{artifact.name} must reference the completed A2A Task")
    if payload.get("a2aArtifactId") != artifact.artifact_id:
        raise ValueError(f"{artifact.name} must reference its actual A2A artifactId")
    if payload.get("artifactId") != project_artifact_id:
        raise ValueError(f"{artifact.name} projectArtifactId metadata mismatch")
    if payload.get("artifactVersion") != artifact_version:
        raise ValueError(f"{artifact.name} artifactVersion metadata mismatch")

    record = model_type.model_validate(payload)
    if record.run_id != run.run_id or record.workflow_step_id != step.workflow_step_id:
        raise ValueError(f"{artifact.name} payload Run/Step identity mismatch")
    if str(record.artifact_id) != project_artifact_id:
        raise ValueError(f"{artifact.name} project Artifact ID mismatch")
    return record


def _positive_integer(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not float(value).is_integer()
        or value < 1
    ):
        raise ValueError("artifactVersion metadata must be a positive integer")
    return int(value)


def _struct_mapping(value: Struct) -> Mapping[str, object]:
    return MessageToDict(value)
