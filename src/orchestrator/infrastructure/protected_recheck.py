"""Compare a previously admitted protected failure with the same Host check on a later candidate."""

import json

from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain.validation_feedback import case_feedback, measured_test_version


def current_protected_observation(connection, issue, source):
    previous = issue.protected_receipt
    if (previous is None or source.run_id != issue.run_id or source.code_version <= issue.code_version
            or not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='protected_scope_executions'"
            ).fetchone()):
        return None
    # The original admitted issue fixes the requirement-to-test mapping. Match both
    # role-entry admissions on their immutable policy, runner, selector and candidate.
    records = connection.execute("""
        SELECT current.* FROM protected_scope_claims old
        JOIN protected_scope_executions before USING(workflow_step_id,tool_name,selector)
        JOIN protected_scope_claims new ON new.run_id=old.run_id AND new.tool_name=old.tool_name
            AND new.selector=old.selector AND new.policy_sha256=old.policy_sha256
        JOIN protected_scope_executions after ON after.workflow_step_id=new.workflow_step_id
            AND after.tool_name=new.tool_name AND after.selector=new.selector
        JOIN unit_test_execution_records current ON current.execution_manifest_id=after.execution_manifest_id
            AND current.run_id=new.run_id AND current.source_artifact_id=new.source_artifact_id
            AND current.workflow_step_id=new.workflow_step_id
        WHERE old.run_id=? AND old.tool_name='run_unit_tests' AND old.workflow_step_id=?
            AND before.execution_manifest_id=? AND old.source_artifact_id=? AND new.source_artifact_id=?
        """, (str(issue.run_id), str(previous.workflow_step_id), str(previous.receipt_id),
              str(issue.source_artifact_id), str(source.artifact_id))).fetchall()
    if len(records) != 1:
        return None
    # Decode and verify the persisted bytes on the existing read transaction.
    receipt = UnitTestOutputStore._decode(connection, records[0])
    if (receipt.role != issue.reporter or receipt.scope.kind != 'PROTECTED'
            or receipt.execution_manifest != source.execution_manifest()
            or measured_test_version(receipt).get('testVersion') != previous.test_version):
        return None
    matching = [test for test in receipt.report.tests
                if f'run_unit_tests:{receipt.scope.name}:{test.test_id}' == previous.test_id]
    if len(matching) != 1 or matching[0].outcome not in ('PASS', 'FAIL'):
        return None
    case = matching[0]
    feedback = case_feedback(case)
    if feedback['observationStatus'] != 'COMPLETE':
        return None
    key = lambda item: (item['caseId'], item['itemId'], item['kind'])
    before_items = {key(item) for item in json.loads(previous.observations_json)}
    after_items = {key(item) for item in feedback['observations']}
    if before_items != after_items:
        return None  # A shorter passing case list cannot close an earlier measured failure.
    return {**feedback, 'role': issue.reporter.value, 'requirementId': str(issue.requirement_ids[0]),
            'testId': previous.test_id, 'testVersion': previous.test_version, 'outcome': case.outcome,
            'runId': str(source.run_id), 'codeVersion': source.code_version, 'sourceArtifactId': str(source.artifact_id),
            'executionManifest': source.execution_manifest().model_dump(mode='json', by_alias=True),
            'protectedReceiptId': str(receipt.execution_manifest_id), 'reportArtifactId': None}
