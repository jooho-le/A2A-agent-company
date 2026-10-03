import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from orchestrator.application.validation_output import decide_verdict
from orchestrator.domain import (
    AgentRole,
    ExecutionManifest,
    FinalVerdict,
    FindingDisposition,
    QAReportArtifact,
    QATestResult,
    SecurityFinding,
    SecurityReportArtifact,
    SecurityRequirementResult,
    SecuritySeverity,
    ValidationOutcome,
    WorkflowStatus,
)
from orchestrator.infrastructure import SQLiteWorkflowRepository
from test_dispatch import tool_evidence


class ValidationArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = uuid4()
        self.requirement_id = uuid4()
        self.qa_step_id = uuid4()
        self.security_step_id = uuid4()
        self.manifest = ExecutionManifest(
            repository_id="a2a-agent-company",
            code_version=1,
            project_artifact_id=uuid4(),
            commit_hash="a" * 40,
            git_object_format="sha1",
            tree_hash="b" * 40,
            snapshot_sha256="c" * 64,
            container_image_digest="sha256:" + "d" * 64,
            dependency_lock_hash="sha256:" + "e" * 64,
        )

    def make_qa(self, outcome: ValidationOutcome = ValidationOutcome.PASS):
        return QAReportArtifact(
            artifact_id=uuid4(),
            artifact_version=1,
            run_id=self.run_id,
            workflow_step_id=self.qa_step_id,
            a2a_task_id="qa-task",
            a2a_artifact_id="qa-artifact",
            requirement_ids=(self.requirement_id,),
            code_version=1,
            execution_manifest=self.manifest,
            tests=(
                QATestResult(
                    test_id="QA-001",
                    requirement_id=self.requirement_id,
                    outcome=outcome,
                    title="Required behavior",
                    tool_evidence=tool_evidence("run_unit_tests", self.manifest.model_dump(mode="json", by_alias=True), outcome="UNVERIFIED" if outcome == ValidationOutcome.UNVERIFIED else "PASS"),
                ),
            ),
        )

    def make_security(
        self,
        outcome: ValidationOutcome = ValidationOutcome.PASS,
        findings: tuple[SecurityFinding, ...] = (),
    ):
        return SecurityReportArtifact(
            artifact_id=uuid4(),
            artifact_version=1,
            run_id=self.run_id,
            workflow_step_id=self.security_step_id,
            a2a_task_id="security-task",
            a2a_artifact_id="security-artifact",
            requirement_ids=(self.requirement_id,),
            code_version=1,
            execution_manifest=self.manifest,
            requirement_results=(
                SecurityRequirementResult(
                    requirement_id=self.requirement_id,
                    outcome=outcome,
                    tool_evidence=tool_evidence("run_security_scan", self.manifest.model_dump(mode="json", by_alias=True), outcome="UNVERIFIED" if outcome == ValidationOutcome.UNVERIFIED else "PASS"),
                ),
            ),
            findings=findings,
        )

    def test_reports_must_be_immutable_and_use_the_expected_agent(self) -> None:
        report = self.make_qa()
        self.assertEqual(report.created_by, AgentRole.QA)
        with self.assertRaises(ValueError):
            report.artifact_version = 2

    def test_success_requires_an_authoritative_requirement_baseline(self) -> None:
        decision = decide_verdict(
            self.make_qa(),
            self.make_security(),
            fix_attempt=0,
            requirements_authoritative=True,
        )
        self.assertEqual(decision.target_status, WorkflowStatus.FINISHED)
        self.assertEqual(decision.verdict, FinalVerdict.SUCCESS)

        provisional = decide_verdict(
            self.make_qa(), self.make_security(), fix_attempt=0
        )
        self.assertEqual(provisional.target_status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(provisional.verdict, FinalVerdict.HUMAN_REVIEW)

    def test_failed_checks_request_fixes_then_fail_after_limit(self) -> None:
        qa = self.make_qa(ValidationOutcome.FAIL)
        security = self.make_security()
        retryable = decide_verdict(qa, security, fix_attempt=2)
        exhausted = decide_verdict(qa, security, fix_attempt=3)

        self.assertEqual(retryable.target_status, WorkflowStatus.FIX_REQUIRED)
        self.assertIsNone(retryable.verdict)
        self.assertEqual(exhausted.target_status, WorkflowStatus.FINISHED)
        self.assertEqual(exhausted.verdict, FinalVerdict.FAIL)

    def test_blocking_and_policy_ambiguous_findings_have_distinct_dispositions(self) -> None:
        high = SecurityFinding(
            finding_id="SEC-001",
            severity=SecuritySeverity.HIGH,
            disposition=FindingDisposition.CONFIRMED,
            title="Confirmed blocker",
            description="Reproducible security issue.",
        )
        medium = SecurityFinding(
            finding_id="SEC-002",
            severity=SecuritySeverity.MEDIUM,
            disposition=FindingDisposition.CONFIRMED,
            title="Policy decision",
            description="Severity policy is not yet decided.",
        )

        blocked = decide_verdict(
            self.make_qa(), self.make_security(findings=(high,)), fix_attempt=0
        )
        review = decide_verdict(
            self.make_qa(), self.make_security(findings=(medium,)), fix_attempt=0
        )
        self.assertEqual(blocked.target_status, WorkflowStatus.FIX_REQUIRED)
        self.assertEqual(review.target_status, WorkflowStatus.HUMAN_REVIEW)

    def test_report_json_schemas_are_valid_and_strict(self) -> None:
        schema_dir = Path(__file__).resolve().parents[1] / "schemas" / "project"
        qa_schema = json.loads((schema_dir / "qa_report.schema.json").read_text())
        security_schema = json.loads(
            (schema_dir / "security_report.schema.json").read_text()
        )
        manifest_schema = json.loads(
            (schema_dir / "execution_manifest.schema.json").read_text()
        )
        self.assertFalse(qa_schema["additionalProperties"])
        self.assertFalse(security_schema["additionalProperties"])
        self.assertIn("executionManifest", qa_schema["properties"])
        self.assertFalse(manifest_schema["additionalProperties"])

    def test_low_and_info_findings_are_recorded_without_blocking_success(self) -> None:
        for severity in (SecuritySeverity.LOW, SecuritySeverity.INFO):
            finding = SecurityFinding(finding_id="nonblocking", severity=severity, disposition=FindingDisposition.CONFIRMED, title="Recorded finding", description="No explicit Requirement violation.")
            decision = decide_verdict(self.make_qa(), self.make_security(findings=(finding,)), fix_attempt=0, requirements_authoritative=True)
            self.assertEqual(decision.verdict, FinalVerdict.SUCCESS)

    def test_missing_tool_provenance_cannot_be_pass_or_product_failure(self) -> None:
        for outcome in (ValidationOutcome.PASS, ValidationOutcome.FAIL):
            qa = self.make_qa(outcome)
            test_data = qa.tests[0].model_dump(mode="python")
            test_data["tool_evidence"] = None
            qa = QAReportArtifact.model_validate({**qa.model_dump(mode="python"), "tests": [test_data]})
            decision = decide_verdict(qa, self.make_security(), fix_attempt=3, requirements_authoritative=True)
            self.assertEqual(decision.verdict, FinalVerdict.HUMAN_REVIEW)

    def test_retry_ledger_rejects_skipped_attempts_and_product_failure_retries(self) -> None:
        from orchestrator.domain.tool_evidence import ToolExecutionEvidence
        manifest = self.manifest.model_dump(mode="json", by_alias=True)
        value = tool_evidence("run_unit_tests", manifest, outcome="UNVERIFIED", retries=2)
        self.assertTrue(ToolExecutionEvidence.model_validate(value).retries_exhausted)
        value["attempts"][0]["outcome"] = "FAIL"
        with self.assertRaises(ValueError):
            ToolExecutionEvidence.model_validate(value)
        value = tool_evidence("run_unit_tests", manifest, outcome="UNVERIFIED", retries=2)
        value["attempts"].pop(0)
        with self.assertRaises(ValueError):
            ToolExecutionEvidence.model_validate(value)

    def test_artifact_type_migration_preserves_rows_and_append_only_triggers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "workflow.sqlite3"
            run_id = str(uuid4())
            artifact_id = str(uuid4())
            with sqlite3.connect(database_path) as connection:
                connection.execute(
                    "CREATE TABLE workflow_runs(run_id TEXT PRIMARY KEY)"
                )
                connection.execute("INSERT INTO workflow_runs VALUES (?)", (run_id,))
                connection.execute(
                    "CREATE TABLE project_artifacts ("
                    "artifact_id TEXT PRIMARY KEY, "
                    "run_id TEXT NOT NULL REFERENCES workflow_runs(run_id), "
                    "artifact_type TEXT NOT NULL CHECK (artifact_type IN "
                    "('SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT')), "
                    "artifact_version INTEGER NOT NULL, payload_json TEXT NOT NULL, "
                    "UNIQUE(run_id, artifact_type, artifact_version))"
                )
                connection.execute(
                    "INSERT INTO project_artifacts VALUES (?, ?, 'SOURCE', 1, '{}')",
                    (artifact_id, run_id),
                )

            SQLiteWorkflowRepository(database_path)

            with sqlite3.connect(database_path) as connection:
                row = connection.execute(
                    "SELECT artifact_id, artifact_type, payload_json "
                    "FROM project_artifacts WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                self.assertEqual(row, (artifact_id, "SOURCE", "{}"))
                connection.execute(
                    "INSERT INTO project_artifacts VALUES (?, ?, 'QA_REPORT', 1, '{}')",
                    (str(uuid4()), run_id),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE project_artifacts SET artifact_version = 2 "
                        "WHERE artifact_id = ?",
                        (artifact_id,),
                    )


if __name__ == "__main__":
    unittest.main()
