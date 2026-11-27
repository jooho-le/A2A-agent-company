"""Validate completed QA/Security Tasks and derive product-level outcomes."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import UUID

from a2a.types import Task, TaskState
from google.protobuf.json_format import MessageToDict
from google.protobuf.struct_pb2 import Struct
from pydantic import ValidationError
from orchestrator.application.output_envelope import validate_output_envelope
from orchestrator.domain import test_not_run
from orchestrator.domain.recovery_causes import execution_receipts, product_precondition_blocked
from orchestrator.domain.validation_artifacts import SecurityRequirementResult, validation_evidence_outcome_matches

from orchestrator.domain import (
    AgentRole,
    A2ATaskState,
    BuildReportArtifact,
    CodeSnapshotArtifact,
    FinalVerdict,
    FindingDisposition,
    MAX_CODE_FIX_ATTEMPTS,
    QAReportArtifact,
    QATestResult,
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
_OPTIONAL_REPORT_FIELDS = {"artifactUri"}


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
        validate_output_envelope(task, step.agent_role, run_id=run.run_id, workflow_step_id=step.workflow_step_id)
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
        if not expected_fields.issubset(payload) or set(payload) - expected_fields - _OPTIONAL_REPORT_FIELDS:
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

        result_items = report.tests if isinstance(report, QAReportArtifact) else report.requirement_results
        allowed_tools = ({"run_unit_tests", "run_browser_tests"} if isinstance(report, QAReportArtifact)
                         else {"run_security_scan", "run_unit_tests"})
        for item in result_items:
            evidence = item.tool_evidence
            if evidence is not None and (
                evidence.execution_manifest != report.execution_manifest
                or evidence.tool_name not in allowed_tools
                or not validation_evidence_outcome_matches(item)
            ):
                raise ValueError("validation Tool evidence must match the Agent role, result and Snapshot")

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


@dataclass(frozen=True)
class ProtectedSupersession:
    """Generated-test results that passing protected tests settle (Ver1 only).

    Rule decided by 석민 on 2026-11-17 for in-loop protected suites (Ver1):
    when every Host protected test bound to a Requirement PASSes, a FAIL or
    UNVERIFIED from a model-written generated test on that same Requirement is
    not a product failure and is not sent to the Developer. Live Runs
    3754980f/42f131fe showed generated tests failing correct code (an app
    opened without its lifespan, a case-sensitive DB lookup) while the
    protected tests passed, so the bounded fix loop chased test defects.

    Nothing is rewritten: reports keep every measured outcome, and the IDs here
    only name what the verdict and the fix request leave out. A protected FAIL
    or UNVERIFIED, a Requirement without protected tests, a Security result
    decided by semantic proof, and every finding keep the existing policy.
    Without protected selectors (Ver2, product defaults) nothing changes.
    """

    qa_test_ids: frozenset[str] = frozenset()
    security_requirement_ids: frozenset[UUID] = frozenset()

    def __bool__(self) -> bool:
        return bool(self.qa_test_ids or self.security_requirement_ids)


def _selector(test_id: str) -> str | None:
    """``tool:scope`` of a Host-built ``tool:scope:test`` identifier."""
    parts = test_id.split(":", 2)
    return ":".join(parts[:2]) if len(parts) == 3 else None


def _measured_security_tests(result) -> tuple[tuple[str, str], ...] | None:
    """``(testId, outcome)`` rows of a Host-measured Security test result, else None."""
    try:
        data = json.loads(result.details or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("code") not in (
            "MEASURED_SECURITY_TESTS", "SECURITY_TESTS_UNVERIFIED", "SECURITY_TESTS_NOT_RUN"):
        return None
    rows = data.get("tests")
    if not isinstance(rows, list) or not rows:
        return None
    measured = []
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("testId"), str)
                or row.get("outcome") not in ("PASS", "FAIL", "UNVERIFIED")):
            return None
        measured.append((row["testId"], row["outcome"]))
    return tuple(measured)


def technical_not_run(item) -> bool:
    """An UNVERIFIED result whose test could not run for a recorded technical reason.

    docs/live-api/05 sections 3·6: it carries the Host's attempt evidence and a
    closed not-run reason. The in-step test fix has already been spent when a
    report still holds one, so it ends as technical UNVERIFIED, not HUMAN_REVIEW.
    """
    if item.outcome != ValidationOutcome.UNVERIFIED or item.tool_evidence is None:
        return False
    if isinstance(item, QATestResult):
        return item.actual_result in test_not_run.TECHNICAL
    try:
        data = json.loads(item.details or "")
    except (TypeError, ValueError):
        return False
    return isinstance(data, dict) and data.get("code") == test_not_run.SECURITY_TESTS_NOT_RUN


def generated_coverage_gap(item):
    if isinstance(item, QATestResult):
        if item.outcome != ValidationOutcome.UNVERIFIED or item.actual_result != 'CONCURRENT_SIGNUPS_NOT_OBSERVED':
            return False
        try:
            detail = json.loads(item.details or '')
        except ValueError:
            return False
        return (isinstance(detail, dict) and detail.get('code') == item.actual_result
                and detail.get('runnerOutcome') == 'PASS' and detail.get('sourceAssertionScope') in ('SEQUENTIAL_SIGNUPS_ONLY', 'CONCURRENT_SIGNUPS_NOT_OBSERVED')
                and isinstance(detail.get('evidenceRefs'), list)
                and any(e.tool_name in {'run_unit_tests', 'run_browser_tests'} and item.test_id.startswith(e.tool_name + ':')
                        and e.outcome.value == 'PASS' and e.evidence_ref in detail['evidenceRefs']
                        for e in item.execution_evidence))
    if not isinstance(item, SecurityRequirementResult) or item.actual_result != 'OUTPUT_COVERAGE_INCOMPLETE':
        return False
    rows = _measured_security_tests(item)
    if item.outcome != ValidationOutcome.UNVERIFIED or not rows:
        return False
    data = json.loads(item.details)
    allowed = {'DB_CONSTRAINT_NOT_OBSERVED', 'HASH_PARAMETERS_NOT_OBSERVED',
               'OUTPUT_NOT_OBSERVED_SUCCESS_RESPONSE', 'OUTPUT_NOT_OBSERVED_REJECTION_RESPONSE',
               'OUTPUT_NOT_OBSERVED_STORAGE_ERROR_RESPONSE', 'OUTPUT_NOT_OBSERVED_REQUEST_LOGS',
               'OUTPUT_NOT_OBSERVED_STORAGE_ERROR_REQUEST_LOGS'}
    gaps = data.get('generatedEvidenceGaps')
    receipts = {e.evidence_ref for e in item.execution_evidence if e.tool_name == 'run_unit_tests'
                and e.outcome.value == 'PASS'}
    return (data.get('code') == 'SECURITY_TESTS_UNVERIFIED' and isinstance(gaps, list) and bool(gaps)
            and all(isinstance(gap, str) and gap in allowed for gap in gaps)
            and all(row['outcome'] == 'PASS' or (row['outcome'] == 'UNVERIFIED' and
                    row.get('unobservedReason') == 'APP_RESPONSE_NOT_CAPTURED' and isinstance(row.get('outputChecks'), dict)
                    and any(row['outputChecks'].get(k) == 'INCOMPLETE' for k in ('STORAGE_ERROR_RESPONSE', 'OTHER_RESPONSE')))
                    for row in data['tests'])
            and all(isinstance(row.get('evidenceRefs'), list) and
                    any(isinstance(ref, str) and ref in receipts for ref in row['evidenceRefs'])
                    for row in data['tests']))



def _proved_concurrent_signup(test):
    """Only the protected QA receipt's completed concurrency assertions can fill this specific gap."""
    try:
        detail = json.loads(test.details or '{}')
        rows = detail.get('observations', [])
        if (detail.get('observationStatus') != 'COMPLETE'
                or test.tool_evidence.evidence_ref not in detail.get('evidenceRefs', [])):
            return False
        by_case = {}
        for row in rows:
            if row.get('kind') == 'assertion' and row.get('status') == 'PASS':
                by_case.setdefault(row.get('caseId'), set()).add(row.get('itemId'))
        return any({'account_count', 'success_count', 'response_statuses'} <= names for names in by_case.values())
    except (AttributeError, TypeError, ValueError):
        return False


def protected_supersession(
    qa_report: QAReportArtifact | None,
    security_report: SecurityReportArtifact | None,
    protected_selectors: frozenset[str] = frozenset(),
) -> ProtectedSupersession:
    """Which generated results the passing protected tests settle; see ProtectedSupersession."""
    if not protected_selectors:
        return ProtectedSupersession()
    qa_tests = qa_report.tests if qa_report is not None else ()
    security_results = security_report.requirement_results if security_report is not None else ()
    protected: dict[UUID, list[bool]] = {}
    concurrency_proven = set()
    for test in qa_tests:
        if _selector(test.test_id) in protected_selectors:
            protected.setdefault(test.requirement_id, []).append(
                test.outcome == ValidationOutcome.PASS and test.tool_evidence is not None)
            if test.outcome == ValidationOutcome.PASS and _proved_concurrent_signup(test):
                concurrency_proven.add(test.requirement_id)
    security_tests = {}
    for result in security_results:
        rows = _measured_security_tests(result)
        security_tests[result.requirement_id] = rows
        for test_id, outcome in rows or ():
            if _selector(test_id) in protected_selectors:
                protected.setdefault(result.requirement_id, []).append(outcome == "PASS")
    settled = {requirement for requirement, passed in protected.items() if passed and all(passed)}
    qa_ids = frozenset(
        test.test_id for test in qa_tests
        if test.requirement_id in settled and test.outcome != ValidationOutcome.PASS
        and (test.actual_result != 'CONCURRENT_SIGNUPS_NOT_OBSERVED' or test.requirement_id in concurrency_proven)
        and _selector(test.test_id) not in protected_selectors)
    security_ids = frozenset(
        result.requirement_id for result in security_results
        if result.requirement_id in settled and result.outcome != ValidationOutcome.PASS
        and security_tests.get(result.requirement_id) and result.actual_result != 'OUTPUT_COVERAGE_INCOMPLETE'
        and all(_selector(test_id) not in protected_selectors
                for test_id, outcome in security_tests[result.requirement_id] if outcome != "PASS"))
    return ProtectedSupersession(qa_ids, security_ids)


def decide_verdict(
    qa_report: QAReportArtifact,
    security_report: SecurityReportArtifact,
    *,
    fix_attempt: int,
    requirements_authoritative: bool = False,
    protected_selectors: frozenset[str] = frozenset(),
) -> VerdictDecision:
    """Apply deterministic product verdict policy to same-snapshot reports.

    ``protected_selectors`` names the Host's in-loop protected ``tool:scope``
    pairs (Ver1); see ``ProtectedSupersession`` for what they settle.
    """
    if qa_report.execution_manifest != security_report.execution_manifest:
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "QA and Security reports refer to different execution manifests",
        )

    superseded = protected_supersession(qa_report, security_report, protected_selectors)
    qa_tests = tuple(test for test in qa_report.tests if test.test_id not in superseded.qa_test_ids)
    security_results = tuple(result for result in security_report.requirement_results
                             if result.requirement_id not in superseded.security_requirement_ids)
    result_items = (*qa_tests, *security_results)
    if any(not execution_receipts(item) for item in result_items):
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "Required results lack MCP Tool execution provenance",
        )

    confirmed_blocker = any(
        finding.disposition == FindingDisposition.CONFIRMED
        and finding.severity in (SecuritySeverity.CRITICAL, SecuritySeverity.HIGH)
        for finding in security_report.findings
    )
    policy_ambiguous_finding = any(
        finding.disposition in (FindingDisposition.SUSPECTED, FindingDisposition.UNVERIFIED)
        or (
            finding.disposition == FindingDisposition.CONFIRMED
            and finding.severity == SecuritySeverity.MEDIUM
        )
        for finding in security_report.findings
    )
    has_failure = (
        any(test.outcome == ValidationOutcome.FAIL for test in qa_tests)
        or any(
            item.outcome == ValidationOutcome.FAIL
            for item in security_results
        )
        or confirmed_blocker
    )
    has_unverified = any(item.outcome == ValidationOutcome.UNVERIFIED for item in result_items)

    def generated_technical(item):
        protected = (_selector(item.test_id) in protected_selectors if isinstance(item, QATestResult)
            else any(outcome == 'UNVERIFIED' and _selector(test_id) in protected_selectors
                     for test_id, outcome in (_measured_security_tests(item) or ())))
        return not protected and technical_not_run(item)

    recoverable_blocked = (has_failure and not policy_ambiguous_finding
        and all(generated_technical(item) or product_precondition_blocked(item, protected_selectors)
                for item in result_items if item.outcome == ValidationOutcome.UNVERIFIED
                or any(outcome == 'UNVERIFIED' for _test_id, outcome in (_measured_security_tests(item) or ()))))

    if not recoverable_blocked and (any(test.outcome == ValidationOutcome.UNVERIFIED and _selector(test.test_id) in protected_selectors
            for test in qa_tests)
            or any(outcome == 'UNVERIFIED' and _selector(test_id) in protected_selectors
                   for item in security_results for test_id, outcome in (_measured_security_tests(item) or ()))):
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "Host protected validation remains unverified; generated-test recovery cannot settle it",
        )

    if policy_ambiguous_finding:
        return VerdictDecision(WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
                               "Security findings require a policy decision")
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
    if has_unverified:
        unverified_items = [
            item for item in result_items
            if item.outcome == ValidationOutcome.UNVERIFIED
        ]
        if all(item.tool_evidence is not None and item.tool_evidence.retries_exhausted for item in unverified_items):
            return VerdictDecision(
                WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED,
                "Required Tool validation remains unverified after the initial call and two safe retries",
            )
        if any(generated_coverage_gap(item) for item in unverified_items) and all(
            generated_coverage_gap(item) or technical_not_run(item)
            or (item.tool_evidence is not None and item.tool_evidence.retries_exhausted) for item in unverified_items
        ):
            return VerdictDecision(WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED,
                                   "Generated tests lack required measurement coverage after the allowed correction")
        if all(technical_not_run(item) or (item.tool_evidence is not None and item.tool_evidence.retries_exhausted)
               for item in unverified_items):
            # A recorded technical not-run after recovery is a technical
            # UNVERIFIED end, counted apart from a judgment hold (HUMAN_REVIEW).
            return VerdictDecision(
                WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED,
                "Required tests could not run for a recorded technical reason after the allowed recovery",
            )
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "Required Tool validation is incomplete without proof that safe retries were exhausted",
        )
    if any(item.tool_evidence is None or item.tool_evidence.outcome != ValidationOutcome.PASS for item in result_items):
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW,
            "Required PASS results lack successful MCP Tool execution provenance",
        )
    if not requirements_authoritative:
        return VerdictDecision(
            WorkflowStatus.HUMAN_REVIEW,
            FinalVerdict.HUMAN_REVIEW,
            "Scenario Registry is unavailable; Planner requirements are not a protected baseline",
        )
    return VerdictDecision(
        WorkflowStatus.FINISHED, FinalVerdict.SUCCESS,
        "Build, QA, Security requirements, and blocking-finding checks passed"
        + ("; generated-test results on Requirements whose protected tests all passed were left out of fix "
           "requests by the Ver1 policy as unresolved conflicts, not confirmed test defects" if superseded else ""),
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
