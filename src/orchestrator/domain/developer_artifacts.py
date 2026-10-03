"""Immutable Developer change and Build Report Artifact records."""

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Literal

from pydantic import Field, UUID4, field_validator, model_validator

from orchestrator.domain.snapshot_handoff import ExecutionManifest, ImmutableDomainModel, _validate_artifact_uri
from orchestrator.domain.states import AgentRole
from orchestrator.domain.tool_evidence import ToolExecutionEvidence, ToolExecutionOutcome


class ChangeReportFile(ImmutableDomainModel):
    path: str = Field(min_length=1)
    action: Literal["ADDED", "MODIFIED", "DELETED"]

    @field_validator("path")
    @classmethod
    def path_must_be_safe_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            "\\" in value
            or (len(value) >= 2 and value[0].isalpha() and value[1] == ":")
            or path.is_absolute()
            or value in {"", "."}
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or path.as_posix() != value
        ):
            raise ValueError("changed file path must be a normalized relative path")
        return value


class ChangeReportArtifact(ImmutableDomainModel):
    artifact_id: UUID4 = Field(alias="artifactId")
    artifact_type: Literal["CHANGE_REPORT"] = Field(
        default="CHANGE_REPORT", alias="artifactType"
    )
    artifact_version: int = Field(ge=1, alias="artifactVersion")
    previous_artifact_id: UUID4 | None = Field(default=None, alias="previousArtifactId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str = Field(min_length=1, alias="a2aTaskId")
    a2a_artifact_id: str = Field(min_length=1, alias="a2aArtifactId")
    created_by: AgentRole = Field(default=AgentRole.DEVELOPER, alias="createdBy")
    requirement_ids: tuple[UUID4, ...] = Field(min_length=1, alias="requirementIds")
    code_version: int = Field(ge=1, le=4, alias="codeVersion")
    summary: str = Field(min_length=1)
    artifact_uri: str = Field(default="", alias="artifactUri")
    changes: tuple[ChangeReportFile, ...] = Field(min_length=1, alias="fileChanges")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), alias="createdAt"
    )

    @field_validator("a2a_task_id", "a2a_artifact_id", "summary")
    @classmethod
    def text_references_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Change Report reference and summary fields must not be blank")
        return value

    @field_validator("requirement_ids")
    @classmethod
    def requirement_ids_must_be_unique(
        cls, values: tuple[UUID4, ...]
    ) -> tuple[UUID4, ...]:
        if len(values) != len(set(values)):
            raise ValueError("requirement_ids must not contain duplicates")
        return values

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_report(self) -> "ChangeReportArtifact":
        if not self.artifact_uri:
            object.__setattr__(self, "artifact_uri", f"artifact://{self.artifact_id}/change-report.json")
        _validate_artifact_uri(self.artifact_uri)
        _validate_artifact_lineage(
            self.artifact_id, self.artifact_version, self.previous_artifact_id
        )
        if self.created_by != AgentRole.DEVELOPER:
            raise ValueError("a Change Report must be created by the Developer Agent")
        paths = [change.path for change in self.changes]
        if len(paths) != len(set(paths)):
            raise ValueError("Change Report paths must be unique")
        return self


class BuildDiagnostic(ImmutableDomainModel):
    rule_id: str = Field(alias="ruleId", min_length=1)
    normalized_location: str = Field(alias="normalizedLocation", min_length=1)
    message: str = Field(min_length=1)

    @field_validator("rule_id", "normalized_location", "message")
    @classmethod
    def nonblank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Build diagnostics must not be blank")
        return value


class BuildReportArtifact(ImmutableDomainModel):
    artifact_id: UUID4 = Field(alias="artifactId")
    artifact_type: Literal["BUILD_REPORT"] = Field(
        default="BUILD_REPORT", alias="artifactType"
    )
    artifact_version: int = Field(ge=1, alias="artifactVersion")
    previous_artifact_id: UUID4 | None = Field(default=None, alias="previousArtifactId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str = Field(min_length=1, alias="a2aTaskId")
    a2a_artifact_id: str = Field(min_length=1, alias="a2aArtifactId")
    created_by: AgentRole = Field(default=AgentRole.DEVELOPER, alias="createdBy")
    requirement_ids: tuple[UUID4, ...] = Field(min_length=1, alias="requirementIds")
    code_version: int = Field(ge=1, le=4, alias="codeVersion")
    source_artifact_id: UUID4 = Field(alias="sourceArtifactId")
    exit_code: int = Field(strict=True, alias="exitCode")
    duration_ms: int = Field(ge=0, strict=True, alias="durationMs")
    execution_manifest_id: UUID4 = Field(alias="executionManifestId")
    execution_manifest: ExecutionManifest = Field(alias="executionManifest")
    artifact_uri: str = Field(default="", alias="artifactUri")
    execution_outcome: ToolExecutionOutcome | None = Field(default=None, alias="executionOutcome")
    failure_kind: Literal["PRODUCT", "INFRASTRUCTURE"] | None = Field(default=None, alias="failureKind")
    tool_evidence: ToolExecutionEvidence | None = Field(default=None, alias="toolEvidence")
    diagnostics: tuple[BuildDiagnostic, ...] = ()
    stdout_ref: str | None = Field(default=None, min_length=1, alias="stdoutRef")
    stderr_ref: str | None = Field(default=None, min_length=1, alias="stderrRef")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), alias="createdAt"
    )

    @field_validator("a2a_task_id", "a2a_artifact_id", "stdout_ref", "stderr_ref")
    @classmethod
    def references_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("Build Report references must not be blank")
        return value

    @field_validator("requirement_ids")
    @classmethod
    def requirement_ids_must_be_unique(
        cls, values: tuple[UUID4, ...]
    ) -> tuple[UUID4, ...]:
        if len(values) != len(set(values)):
            raise ValueError("requirement_ids must not contain duplicates")
        return values

    @field_validator("exit_code", "duration_ms", mode="before")
    @classmethod
    def numeric_fields_must_be_integers(cls, value: object) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("Build Tool numeric fields must be integers")
        if not float(value).is_integer():
            raise ValueError("Build Tool numeric fields must be integers")
        return int(value)

    @field_validator("created_at")
    @classmethod
    def created_at_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_report(self) -> "BuildReportArtifact":
        if not self.artifact_uri:
            object.__setattr__(self, "artifact_uri", f"artifact://{self.artifact_id}/build-report.json")
        _validate_artifact_uri(self.artifact_uri)
        if self.execution_outcome is None:
            object.__setattr__(self, "execution_outcome", ToolExecutionOutcome.PASS if self.exit_code == 0 else ToolExecutionOutcome.FAIL)
        if self.execution_outcome == ToolExecutionOutcome.PASS and self.exit_code != 0:
            raise ValueError("Build PASS requires exitCode=0")
        if self.execution_outcome == ToolExecutionOutcome.FAIL and self.exit_code == 0:
            raise ValueError("Build FAIL requires a nonzero product Build exitCode")
        if self.execution_outcome == ToolExecutionOutcome.UNVERIFIED and self.failure_kind != "INFRASTRUCTURE":
            raise ValueError("unverified Build requires an infrastructure failure classification")
        if self.failure_kind == "INFRASTRUCTURE" and self.execution_outcome != ToolExecutionOutcome.UNVERIFIED:
            raise ValueError("infrastructure failures cannot be reported as product Build failures")
        if self.tool_evidence is not None:
            if self.tool_evidence.tool_name != "run_build" or self.tool_evidence.execution_manifest != self.execution_manifest:
                raise ValueError("Build evidence must prove run_build on the reported Snapshot")
            expected_tool_outcome = ToolExecutionOutcome.UNVERIFIED if self.execution_outcome == ToolExecutionOutcome.UNVERIFIED else ToolExecutionOutcome.PASS
            if self.tool_evidence.outcome != expected_tool_outcome:
                raise ValueError("Build Tool evidence does not match its execution classification")
        _validate_artifact_lineage(
            self.artifact_id, self.artifact_version, self.previous_artifact_id
        )
        if self.created_by != AgentRole.DEVELOPER:
            raise ValueError("a Build Report must be created by the Developer Agent")
        if self.code_version != self.execution_manifest.code_version:
            raise ValueError("Build Report and Execution Manifest codeVersion differ")
        if self.source_artifact_id != self.execution_manifest.project_artifact_id:
            raise ValueError("Build Report Manifest references a different Source Artifact")
        return self

    @property
    def passed(self) -> bool:
        return self.execution_outcome == ToolExecutionOutcome.PASS and self.exit_code == 0


def _validate_artifact_lineage(
    artifact_id: UUID4,
    artifact_version: int,
    previous_artifact_id: UUID4 | None,
) -> None:
    if artifact_version == 1 and previous_artifact_id is not None:
        raise ValueError("the first Artifact version must not have a predecessor")
    if artifact_version > 1 and previous_artifact_id is None:
        raise ValueError("later Artifact versions must reference the previous Artifact")
    if previous_artifact_id == artifact_id:
        raise ValueError("an Artifact cannot be its own previous version")
