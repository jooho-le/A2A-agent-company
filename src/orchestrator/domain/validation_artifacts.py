"""Immutable QA and Security result Artifact records."""

from datetime import datetime, timezone
from enum import Enum
import json
from typing import Literal

from pydantic import Field, UUID4, field_validator, model_validator

from orchestrator.domain.contract_validation import JSONInteger
from orchestrator.domain.snapshot_handoff import ExecutionManifest, ImmutableDomainModel, _validate_artifact_uri
from orchestrator.domain.states import AgentRole
from orchestrator.domain.tool_evidence import ToolExecutionEvidence
from orchestrator.domain import test_not_run


class ValidationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIED = "UNVERIFIED"


class SecuritySeverity(str, Enum):
    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INFO = "INFO"


class FindingDisposition(str, Enum):
    CONFIRMED = "CONFIRMED"
    SUSPECTED = "SUSPECTED"
    FALSE_POSITIVE = "FALSE_POSITIVE"
    UNVERIFIED = "UNVERIFIED"


class QATestResult(ImmutableDomainModel):
    execution_evidence: tuple[ToolExecutionEvidence, ...] = Field(default=(), alias="executionEvidence", max_length=64)
    test_id: str = Field(alias="testId", min_length=1)
    requirement_id: UUID4 = Field(alias="requirementId")
    outcome: ValidationOutcome
    title: str = Field(min_length=1)
    details: str | None = None
    expected_result: str | None = Field(default=None, alias="expectedResult")
    actual_result: str | None = Field(default=None, alias="actualResult")
    normalized_location: str | None = Field(default=None, alias="normalizedLocation")
    tool_evidence: ToolExecutionEvidence | None = Field(default=None, alias="toolEvidence")

    @field_validator("test_id", "title", "details")
    @classmethod
    def text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("QA test text fields must not be blank")
        return value


class SecurityRequirementResult(ImmutableDomainModel):
    execution_evidence: tuple[ToolExecutionEvidence, ...] = Field(default=(), alias="executionEvidence", max_length=64)
    requirement_id: UUID4 = Field(alias="requirementId")
    outcome: ValidationOutcome
    details: str | None = None
    expected_result: str | None = Field(default=None, alias="expectedResult")
    actual_result: str | None = Field(default=None, alias="actualResult")
    normalized_location: str | None = Field(default=None, alias="normalizedLocation")
    tool_evidence: ToolExecutionEvidence | None = Field(default=None, alias="toolEvidence")

    @field_validator("details")
    @classmethod
    def details_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("security result details must not be blank")
        return value.strip() if value is not None else None


def validation_evidence_outcome_matches(item) -> bool:
    """A successful runner may authentically report an unexecuted test body.

    Do not rewrite the receipt's PASS (the tool completed) into UNVERIFIED.
    Admit only closed, structured technical not-run rows; arbitrary skips or
    semantic uncertainty still cannot use a successful tool as their proof.
    """
    evidence = item.tool_evidence
    if evidence is None:
        return True
    expected = ValidationOutcome.UNVERIFIED if item.outcome == ValidationOutcome.UNVERIFIED else ValidationOutcome.PASS
    if evidence.outcome == expected:
        return True
    if (item.outcome != ValidationOutcome.UNVERIFIED or evidence.outcome != ValidationOutcome.PASS
            or evidence.tool_name != 'run_unit_tests'):
        return False
    try:
        data = json.loads(item.details or '')
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    runner_reasons = test_not_run.TECHNICAL & test_not_run.RUNNER_REASONS
    if isinstance(item, QATestResult):
        return item.actual_result in runner_reasons and data.get('code') == item.actual_result
    if not isinstance(item, SecurityRequirementResult) or data.get('code') != test_not_run.SECURITY_TESTS_NOT_RUN:
        return False
    rows = data.get('tests')
    if not isinstance(rows, list) or not rows or not all(isinstance(row, dict) for row in rows):
        return False
    nonpassing = [row for row in rows if row.get('outcome') != 'PASS']
    return bool(nonpassing) and all(
        row.get('outcome') == 'UNVERIFIED' and isinstance(row.get('notRunReason'), str)
        and row['notRunReason'] in test_not_run.TECHNICAL
        for row in nonpassing) and any(
            row.get('notRunReason') in runner_reasons
            and isinstance(row.get('evidenceRefs'), list) and evidence.evidence_ref in row['evidenceRefs']
            for row in nonpassing)


class SecurityFinding(ImmutableDomainModel):
    finding_id: str = Field(alias="findingId", min_length=1)
    severity: SecuritySeverity
    disposition: FindingDisposition
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    requirement_id: UUID4 | None = Field(default=None, alias="requirementId")
    evidence_ref: str | None = Field(default=None, alias="evidenceRef", min_length=1)
    rule_id: str | None = Field(default=None, alias="ruleId", min_length=1)
    normalized_location: str | None = Field(default=None, alias="normalizedLocation", min_length=1)

    @field_validator("finding_id", "title", "description", "evidence_ref")
    @classmethod
    def text_must_not_be_blank(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("security finding text fields must not be blank")
        return value


class _ValidationArtifact(ImmutableDomainModel):
    artifact_id: UUID4 = Field(alias="artifactId")
    artifact_version: JSONInteger = Field(ge=1, alias="artifactVersion")
    previous_artifact_id: UUID4 | None = Field(default=None, alias="previousArtifactId")
    run_id: UUID4 = Field(alias="runId")
    workflow_step_id: UUID4 = Field(alias="workflowStepId")
    a2a_task_id: str = Field(min_length=1, alias="a2aTaskId")
    a2a_artifact_id: str = Field(min_length=1, alias="a2aArtifactId")
    requirement_ids: tuple[UUID4, ...] = Field(min_length=1, alias="requirementIds")
    code_version: JSONInteger = Field(ge=1, le=4, alias="codeVersion")
    execution_manifest: ExecutionManifest = Field(alias="executionManifest")
    artifact_uri: str = Field(default="", alias="artifactUri")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc), alias="createdAt"
    )

    @field_validator("a2a_task_id", "a2a_artifact_id")
    @classmethod
    def references_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A2A references must not be blank")
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
    def validate_common_identity(self):
        if not self.artifact_uri:
            object.__setattr__(self, "artifact_uri", f"artifact://{self.artifact_id}/report.json")
        _validate_artifact_uri(self.artifact_uri)
        if self.artifact_version == 1 and self.previous_artifact_id is not None:
            raise ValueError("the first Artifact version must not have a predecessor")
        if self.artifact_version > 1 and self.previous_artifact_id is None:
            raise ValueError("later Artifact versions must reference the previous Artifact")
        if self.previous_artifact_id == self.artifact_id:
            raise ValueError("an Artifact cannot be its own previous version")
        if self.code_version != self.execution_manifest.code_version:
            raise ValueError("report codeVersion and Execution Manifest differ")
        return self


class QAReportArtifact(_ValidationArtifact):
    artifact_type: Literal["QA_REPORT"] = Field(default="QA_REPORT", alias="artifactType")
    created_by: Literal[AgentRole.QA] = Field(default=AgentRole.QA, alias="createdBy")
    tests: tuple[QATestResult, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_tests(self) -> "QAReportArtifact":
        test_ids = [test.test_id for test in self.tests]
        if len(test_ids) != len(set(test_ids)):
            raise ValueError("QA test IDs must be unique within a report")
        if not set(self.requirement_ids).issuperset(
            {test.requirement_id for test in self.tests}
        ):
            raise ValueError("QA tests cannot reference unknown requirements")
        for test in self.tests:
            validate_execution_evidence(test, self.execution_manifest, {"run_unit_tests", "run_browser_tests"})
            if test.tool_evidence is not None and (
                test.tool_evidence.execution_manifest != self.execution_manifest
                or test.tool_evidence.tool_name not in {"run_unit_tests", "run_browser_tests"}
                or not validation_evidence_outcome_matches(test)
            ):
                raise ValueError("QA Tool evidence must match the role, execution and Snapshot")
        return self

    @property
    def passed(self) -> bool:
        return all(test.outcome == ValidationOutcome.PASS for test in self.tests)

    @property
    def has_unverified(self) -> bool:
        return any(test.outcome == ValidationOutcome.UNVERIFIED for test in self.tests)


class SecurityReportArtifact(_ValidationArtifact):
    artifact_type: Literal["SECURITY_REPORT"] = Field(
        default="SECURITY_REPORT", alias="artifactType"
    )
    created_by: Literal[AgentRole.SECURITY] = Field(
        default=AgentRole.SECURITY, alias="createdBy"
    )
    requirement_results: tuple[SecurityRequirementResult, ...] = Field(
        min_length=1, alias="requirementResults"
    )
    findings: tuple[SecurityFinding, ...] = ()

    @model_validator(mode="after")
    def validate_security_results(self) -> "SecurityReportArtifact":
        result_ids = [result.requirement_id for result in self.requirement_results]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("Security requirement results must be unique")
        if not set(result_ids).issubset(set(self.requirement_ids)):
            raise ValueError("Security results cannot reference unknown requirements")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("Security finding IDs must be unique")
        if any(
            finding.requirement_id is not None
            and finding.requirement_id not in self.requirement_ids
            for finding in self.findings
        ):
            raise ValueError("Security findings cannot reference unknown requirements")
        for result in self.requirement_results:
            validate_execution_evidence(result, self.execution_manifest, {"run_unit_tests", "run_security_scan"})
            if result.tool_evidence is not None and (
                result.tool_evidence.execution_manifest != self.execution_manifest
                # Scanner evidence, or measured Security unit-test receipts when
                # the Host runs Security on the same test path as QA.
                or result.tool_evidence.tool_name not in {"run_security_scan", "run_unit_tests"}
                or not validation_evidence_outcome_matches(result)
            ):
                raise ValueError("Security Tool evidence must match the role, execution and Snapshot")
        return self

    @property
    def passed(self) -> bool:
        return all(
            result.outcome == ValidationOutcome.PASS
            for result in self.requirement_results
        )

    @property
    def has_unverified(self) -> bool:
        return any(
            result.outcome == ValidationOutcome.UNVERIFIED
            for result in self.requirement_results
        )


def validate_execution_evidence(item, manifest, allowed_tools):
    """Attempt provenance is independent of whether a product check completed."""
    seen = set()
    for evidence in item.execution_evidence:
        if (evidence.execution_manifest != manifest or evidence.tool_name not in allowed_tools
                or evidence.execution_id in seen):
            raise ValueError('execution evidence must be unique and bound to this Snapshot')
        seen.add(evidence.execution_id)
    if item.execution_evidence and item.tool_evidence is not None and item.tool_evidence not in item.execution_evidence:
        raise ValueError('verdict evidence must belong to execution evidence')
    if item.execution_evidence:
        try:
            data = json.loads(item.details or '{}')
        except (ValueError, TypeError):
            # Legacy/browser reports carry a stable text code, without per-case refs.
            return
        if not isinstance(data, dict):
            raise ValueError('execution details must be an object')
        rows = data.get('tests', [data])
        refs = {e.evidence_ref for e in item.execution_evidence}
        if (not isinstance(rows, list) or any(not isinstance(row, dict)
                or not isinstance(row.get('evidenceRefs', []), list)
                or any(not isinstance(ref, str) or ref not in refs for ref in row.get('evidenceRefs', []))
                for row in rows)):
            raise ValueError('test evidence reference is not bound to an execution receipt')
