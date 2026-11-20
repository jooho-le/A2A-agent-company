"""Reconstruct same-candidate measurable failures from the Host journal.

The model supplies neither source identity nor evidence. The engine separately
measures net source changes. Terminal routing repeats this journal check in its
transaction, rather than trusting the assessment embedded in a task response.
"""
import json

from agents.llm.development_rejection import BoundFailure
from orchestrator.domain.developer_artifacts import BuildReportArtifact
from orchestrator.domain.issues import IssueRecord
from orchestrator.domain.models import WorkflowStep
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.validation_artifacts import QAReportArtifact, SecurityReportArtifact


def bound_failure(connection, step):
    models = {'SOURCE': CodeSnapshotArtifact, 'BUILD_REPORT': BuildReportArtifact,
              'QA_REPORT': QAReportArtifact, 'SECURITY_REPORT': SecurityReportArtifact}
    artifacts = [models[kind].model_validate_json(data) for kind, data in connection.execute(
        'SELECT artifact_type,payload_json FROM project_artifacts WHERE run_id=?', (str(step.run_id),))
        if kind in models]
    prior = [a for a in artifacts if isinstance(a, CodeSnapshotArtifact)
             and a.artifact_id in step.input_artifact_ids and a.code_version + 1 == step.code_version]
    if len(prior) != 1:
        return None
    source = prior[0]
    restored = connection.execute("SELECT payload_json FROM trace_events WHERE run_id=? AND json_extract(payload_json,'$.workflowStepId')=? "
        "AND event_type='DEVELOPMENT_BASELINE_RESTORED' ORDER BY rowid DESC LIMIT 1",
        (str(step.run_id), str(step.workflow_step_id))).fetchone()
    if restored:
        event = json.loads(restored[0])
        if (event.get('actor') != 'Orchestrator' or event.get('codeVersion') != step.code_version
                or str(source.artifact_id) not in event.get('inputArtifactIds', [])):
            return None
        candidates = [a for a in artifacts if isinstance(a, CodeSnapshotArtifact)
            and str(a.artifact_id) in event['inputArtifactIds'] and a.snapshot_sha256 == event.get('snapshotSha256')
            and a.code_version < source.code_version]
        if len(candidates) != 1:
            return None
        source = candidates[0]
        allowed_reports = set(event['inputArtifactIds'])
    else:
        allowed_reports = set(map(str, step.input_artifact_ids))
    reports = {a.artifact_id: a for a in artifacts if not isinstance(a, CodeSnapshotArtifact)
        and str(a.artifact_id) in allowed_reports and a.execution_manifest == source.execution_manifest()}
    issues = []
    for row in connection.execute('SELECT payload_json FROM issue_records WHERE run_id=?', (str(step.run_id),)):
        issue = IssueRecord.model_validate_json(row[0])
        if (issue.source_artifact_id != source.artifact_id or issue.code_version != source.code_version
                or not set(issue.requirement_ids).issubset(step.requirement_ids)):
            continue
        report = reports.get(issue.report_artifact_id)
        if isinstance(report, BuildReportArtifact):
            failed = report.execution_outcome.value == 'FAIL' and report.failure_kind == 'PRODUCT' and report.tool_evidence is not None
        elif isinstance(report, (QAReportArtifact, SecurityReportArtifact)):
            results = report.tests if isinstance(report, QAReportArtifact) else report.requirement_results
            failed = any(r.outcome.value == 'FAIL' and r.tool_evidence is not None
                         and r.requirement_id in issue.requirement_ids for r in results)
        else:
            failed = False
        if failed:
            issues.append(issue)
    if not issues:
        return None
    return BoundFailure(sourceArtifactId=source.artifact_id, snapshotSha256=source.snapshot_sha256,
        issueIds=tuple(sorted((i.issue_id for i in issues), key=str)),
        requirementIds=tuple(sorted({r for i in issues for r in i.requirement_ids}, key=str)))


def failure_reader(services, execution):
    def read():
        with services._repository._connection() as connection:
            row = connection.execute('SELECT payload_json FROM workflow_steps WHERE workflow_step_id=? AND run_id=?',
                (str(execution.metadata.workflow_step_id), str(execution.metadata.run_id))).fetchone()
            return None if row is None else bound_failure(connection, WorkflowStep.model_validate_json(row[0]))
    return read
