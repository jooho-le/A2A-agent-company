"""Validate completed QA/Security Tasks and derive product-level outcomes."""

from collections.abc import Mapping
from dataclasses import dataclass

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from pydantic import ValidationError

from orchestrator.domain import (
    AgentRole,
    A2ATaskState,
    BuildReportArtifact,
    CodeSnapshotArtifact,
    FinalVerdict,
    FindingDisposition,
    MAX_CODE_FIX_ATTEMPTS,
    QAReportArtifact,
    SecurityReportArtifact,
    SecuritySeverity,
    ValidationOutcome,
    WorkflowRun,
    WorkflowStatus,
    WorkflowStep,
    WorkflowStepStatus,
)


class ValidationOutputError(ValueError):
    """Raised when a QA/Security completed Task violates its report contract."""


_ARTIFACT_METADATA_FIELDS = {
    "runId",
    "workflowStepId",
    "projectArtifactId",
    "artifactVersion",
}
_QA_REPORT_FIELDS = {
    "artifactId", "artifactType", "artifactVersion", "previousArtifactId",
    "runId", "workflowStepId", "a2aTaskId", "a2aArtifactId", "createdBy",
    "requirementIds", "codeVersion", "executionManifest", "createdAt", "tests",
}
_SECURITY_REPORT_FIELDS = {
    "artifactId", "artifactType", "artifactVersion", "previousArtifactId",
    "runId", "workflowStepId", "a2aTaskId", "a2aArtifactId", "createdBy",
    "requirementIds", "codeVersion", "executionManifest", "createdAt",
    "requirementResults", "findings",
}


@dataclass(frozen=True)
class VerdictDecision:
    """Outcome of applying the agreed validation policy to verified evidence."""

    target_status: WorkflowStatus
    verdict: FinalVerdict | None
    reason: str


def parse_validation_output(
    task: Task,
    *,
    run: WorkflowRun,
    step: WorkflowStep,
    source: CodeSnapshotArtifact,
) -> QAReportArtifact | SecurityReportArtifact:
    """Validate Task, project metadata, manifest, and requirement coverage."""
    try:
        if task.status is None or TaskState.Name(task.status.state) != "TASK_STATE_COMPLETED":
            raise ValueError("Validation Task is not completed")
        if not task.id.strip() or task.id != step.a2a_task_id:
            raise ValueError("Validation Task ID does not match its WorkflowStep")
        if (
            step.run_id != run.run_id
            or step.agent_role not in (AgentRole.QA, AgentRole.SECURITY)
            or step.status != WorkflowStepStatus.SUCCEEDED
            or step.a2a_task_state != A2ATaskState.COMPLETED
        ):
            raise ValueError("Validation WorkflowStep must be completed and belong to this Run")
        if step.code_version != source.code_version:
            raise ValueError("Validation Step codeVersion differs from the Source Snapshot")

        name = "qa-report.json" if step.agent_role == AgentRole.QA else "security-report.json"
        model_type = QAReportArtifact if step.agent_role == AgentRole.QA else SecurityReportArtifact
        matches = [artifact for artifact in task.artifacts if artifact.name == name]
        if len(matches) != 1:
            raise ValueError(f"Validation Task must return exactly one {name}")
        artifact = matches[0]
        if not artifact.artifact_id.strip():
            raise ValueError("Validation A2A Artifact must have an artifactId")
        if len(artifact.parts) != 1:
            raise ValueError(f"{name} must have one JSON data Part")
        part = artifact.parts[0]
        if part.WhichOneof("content") != "data" or part.media_type != "application/json":
            raise ValueError(f"{name} must use one application/json data Part")

        metadata = _struct_mapping(artifact.metadata)
        if set(metadata) != _ARTIFACT_METADATA_FIELDS:
            raise ValueError(f"{name} metadata has unexpected or missing fields")
        if metadata.get("runId") != str(run.run_id):
            raise ValueError(f"{name} runId does not match the current Run")
        if metadata.get("workflowStepId") != str(step.workflow_step_id):
            raise ValueError(f"{name} workflowStepId does not match the validation Step")
        project_artifact_id = metadata.get("projectArtifactId")
        if not isinstance(project_artifact_id, str):
            raise ValueError(f"{name} metadata must contain projectArtifactId")
        artifact_version = _positive_integer(metadata.get("artifactVersion"))

        payload = MessageToDict(part.data)
        expected_fields = (
            _QA_REPORT_FIELDS
            if model_type is QAReportArtifact
            else _SECURITY_REPORT_FIELDS
        )
        if set(payload) != expected_fields:
            raise ValueError(f"{name} payload has unexpected or missing fields")
        if payload.get("a2aTaskId") != task.id:
            raise ValueError(f"{name} must reference the completed A2A Task")
        if payload.get("a2aArtifactId") != artifact.artifact_id:
            raise ValueError(f"{name} must reference its actual A2A artifactId")
        if payload.get("artifactId") != project_artifact_id:
            raise ValueError(f"{name} projectArtifactId metadata mismatch")
        if payload.get("artifactVersion") != artifact_version:
            raise ValueError(f"{name} artifactVersion metadata mismatch")

        report = model_type.model_validate(payload)
        if report.run_id != run.run_id or report.workflow_step_id != step.workflow_step_id:
            raise ValueError(f"{name} payload Run/Step identity mismatch")
        if report.a2a_task_id != task.id or report.a2a_artifact_id != artifact.artifact_id:
            raise ValueError(f"{name} payload A2A reference mismatch")
        if set(report.requirement_ids) != set(step.requirement_ids):
            raise ValueError(f"{name} Requirement IDs differ from its WorkflowStep")
        if report.code_version != source.code_version:
            raise ValueError(f"{name} codeVersion differs from the Source Snapshot")
        if report.execution_manifest != source.execution_manifest():
            raise ValueError(f"{name} Execution Manifest differs from the Source Snapshot")

        required_ids = set(step.requirement_ids)
        if isinstance(report, QAReportArtifact):
            covered_ids = {test.requirement_id for test in report.tests}
            if covered_ids != required_ids:
                raise ValueError("QA Report must include at least one test per Requirement")
        else:
            reported_ids = {item.requirement_id for item in report.requirement_results}
            if reported_ids != required_ids:
                raise ValueError("Security Report must include every Step Requirement")
        return report
    except (ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise ValidationOutputError(
            "QA/Security output violates the project validation Artifact contract"
        ) from exc


def decide_verdict(
    qa_report: QAReportArtifact,
    security_report: SecurityReportArtifact,
    *,
    fix_attempt: int,
    requirements_authoritative: bool = False,
) -> VerdictDecision:
    """Apply deterministic product verdict policy to same-snapshot reports."""
    if qa_report.execution_manifest != security_report.execution_manifest:
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "QA and Security reports refer to different execution manifests",
        )

    confirmed_blocker = any(
        finding.disposition == FindingDisposition.CONFIRMED
        and finding.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
        for finding in security_report.findings
    )
    policy_ambiguous_finding = any(
        finding.disposition == FindingDisposition.SUSPECTED
        or (
            finding.disposition == FindingDisposition.CONFIRMED
            and finding.severity in (SecuritySeverity.MEDIUM, SecuritySeverity.LOW)
        )
        for finding in security_report.findings
    )
    has_failure = (
        any(test.outcome == ValidationOutcome.FAIL for test in qa_report.tests)
        or any(
            item.outcome == ValidationOutcome.FAIL
            for item in security_report.requirement_results
        )
        or confirmed_blocker
    )
    has_unverified = qa_report.has_unverified or security_report.has_unverified

    if has_failure:
        if fix_attempt >= MAX_CODE_FIX_ATTEMPTS:
            return VerdictDecision(
                WorkflowStatus.FINISHED, FinalVerdict.FAIL,
                "A verifiable required check failed after all code-fix attempts",
            )
        return VerdictDecision(
            WorkflowStatus.FIX_REQUIRED, None,
            "A verifiable required check failed and requires a code fix",
        )
    if has_unverified or policy_ambiguous_finding:
        # Tool retry counts and automatic retry dispatch are not present yet;
        # do not turn an incomplete/ambiguous result into a product verdict.
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "A required check is unverified or a Security finding needs policy review",
        )
    if not requirements_authoritative:
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW,
            FinalVerdict.HUMAN_REVIEW,
            "Scenario Registry is unavailable; Planner requirements are not a protected baseline",
        )
    return VerdictDecision(
        WorkflowStatus.FINISHED, FinalVerdict.SUCCESS,
        "Build, QA, Security requirements, and blocking-finding checks passed",
    )


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
