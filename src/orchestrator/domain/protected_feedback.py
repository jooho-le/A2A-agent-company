"""Developer feedback backed by an independent Host receipt from a stopped validator."""

import json

from orchestrator.domain.validation_feedback import LABELS


def protected_feedback_row(issue, source):
    """Build one row from a Host-authenticated issue and its original frozen Source."""
    measurement = issue.protected_receipt
    if (source is None or issue.run_id != source.run_id or issue.code_version != source.code_version
            or issue.source_artifact_id != source.artifact_id):
        raise ValueError('PROTECTED_FEEDBACK_SOURCE_MISMATCH')
    receipt_id = str(measurement.receipt_id)
    observations = [dict(row, label=LABELS.get(row['itemId'], row['itemId']))
                    for row in json.loads(measurement.observations_json)]
    return {
        'role': issue.reporter.value, 'requirementId': str(issue.requirement_ids[0]),
        'runId': str(source.run_id), 'sourceArtifactId': str(source.artifact_id),
        'codeVersion': source.code_version, 'executionManifest': source.execution_manifest().model_dump(
            mode='json', by_alias=True), 'reportArtifactId': None,
        'evidenceOrigin': 'HOST_PROTECTED_RECEIPT', 'protectedReceiptId': receipt_id,
        'testId': measurement.test_id, 'testVersion': measurement.test_version, 'outcome': 'FAIL',
        'evidenceRefs': list(issue.evidence_refs), 'observationStatus': 'COMPLETE',
        'observations': observations, 'reason': None}


def append_protected_feedback(fix, issues, artifacts):
    """Keep a private unit-report receipt distinct from a QA/Security project report."""
    source = next((artifact for artifact in artifacts if artifact.artifact_type == 'SOURCE'), None)
    for issue, target in zip(issues, fix['issues'], strict=True):
        measurement = issue.protected_receipt
        if measurement is None:
            continue
        row = protected_feedback_row(issue, source)
        target.update({'evidenceOrigin': row['evidenceOrigin'], 'protectedReceiptId': row['protectedReceiptId']})
        fix['validationFeedback'].append(row)
