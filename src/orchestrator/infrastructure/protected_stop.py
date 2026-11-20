"""Recover product observations when Host policy rejected a test-file write before execution."""
import json

from mcp_tools.execution_policy import arguments_sha256
from mcp_tools.execution_store import ToolExecutionStore, _digest
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.unit_store import UnitTestOutputStore
from orchestrator.domain import AgentRole, IssueRecord, make_issue_fingerprint
from orchestrator.domain.issues import ProtectedReceipt
from orchestrator.domain.validation_feedback import case_feedback, measured_test_version


def blocked_test_write(repository, run, step, source):
    """A generic peer TOOL_POLICY code cannot attest what operation was stopped."""
    expected = {'runId': str(run.run_id), 'workflowStepId': str(step.workflow_step_id),
        'a2aTaskId': step.a2a_task_id, 'agentContextId': step.agent_context_id, 'attempt': step.attempt,
        'codeVersion': source.code_version, 'snapshotSha256': source.snapshot_sha256,
        'inputArtifactIds': [str(source.artifact_id)]}
    with repository._transaction() as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='agent_runtime_events'").fetchone():
            return False
        records = connection.execute("SELECT t.event_type, r.payload_json FROM agent_runtime_events r JOIN trace_events t "
            "ON t.event_id=r.trace_event_id AND t.run_id=r.run_id WHERE r.run_id=? AND r.workflow_step_id=? "
            "ORDER BY r.sequence", (str(run.run_id), str(step.workflow_step_id))).fetchall()
    rejected, decisions, latest_call = set(), [], 0
    for event_type, raw in records:
        event = json.loads(raw)
        binding, detail = event['binding'], event['detail']
        if (any(binding.get(key) != value for key, value in expected.items())
                or binding.get('eventType', event_type) != event_type
                or binding.get('actor', binding.get('role')) not in (step.agent_role.value, 'SINGLE_AGENT')
                or set(binding.get('requirementIds', ())) != {str(req) for req in step.requirement_ids}):
            continue
        if event_type in ('LLM_MODEL_CALLED', 'LLM_CALL_FINISHED'):
            latest_call = max(latest_call, detail.get('sequence', 0))
        if event_type == 'LLM_POLICY_REJECTED' and detail.get('purpose') == 'LLM_TOOL_POLICY_VALIDATION':
            rejection = detail.get('llmValidation', {})
            if (rejection.get('stage'), rejection.get('boundary'), rejection.get('fieldPath'),
                rejection.get('reasonCode'), rejection.get('sourceRule'), rejection.get('toolName')) == (
                    'TOOL_POLICY', 'TOOL_ARGUMENTS', '/content', 'SOURCE_CREDENTIAL_DENIED',
                    'CREDENTIAL_ASSIGNMENT', 'write_test_file'):
                rejected.add(rejection.get('sequence'))
        if event_type == 'LLM_RECOVERY_DECIDED' and detail.get('purpose') == 'LLM_RECOVERY':
            decisions.append(detail['recovery'])
    if len(decisions) < 2:
        return False
    prior, last = decisions[-2:]
    return (last.get('policy') == prior.get('policy') == 'BOUNDED_PRE_EXECUTION_REGENERATION_V1'
            and last.get('kind') == prior.get('kind') == 'DECISION'
            and last.get('operationSequence') == prior.get('operationSequence')
            and (prior.get('action'), prior.get('code'), prior.get('reason')) ==
                ('REGENERATE', 'SOURCE_CREDENTIAL_DENIED', 'ALLOWED')
            and (last.get('action'), last.get('category'), last.get('code'), last.get('reason')) ==
                ('STOP', 'POLICY_BLOCK', 'LLM_TOOL_POLICY_VIOLATION', 'NOT_SAFE_TO_RETRY')
            and last.get('sequence') == latest_call and latest_call in rejected
            and prior.get('sequence') in rejected and prior['sequence'] < latest_call)


def protected_issues(repository, run, step, source, cases):
    """Join the pre-model completion, approved case catalog and actual private execution output."""
    from agents.runtime.protected_scopes import completed_protected_scopes
    from agents.runtime.evaluation_policy_store import EvaluationPolicyStore
    config = repository.get_run_configuration(run.run_id)
    suite_ref = config.configuration.protected_test_suite_ref
    if not suite_ref:
        return ()
    policies = {p.role: p for p in EvaluationPolicyStore(repository).list_for_run(run.run_id)}
    binding = MCPBinding(role=step.agent_role, agent_role=step.agent_role,
                         run_id=run.run_id, workspace_id=run.workspace_id)
    issues = []
    completed = completed_protected_scopes(repository, run_id=run.run_id,
        workflow_step_id=step.workflow_step_id, source_artifact_id=source.artifact_id)
    for entry in completed:
        if entry.tool_name != 'run_unit_tests':
            continue  # Only typed sub-observations can prove an independent product assertion.
        policy = policies.get(step.agent_role)
        if not policy or policy.policy_sha256 != entry.policy_sha256 or policy.workspace_id != run.workspace_id:
            raise ValueError('PROTECTED_RECEIPT_POLICY_MISMATCH')
        record = ToolExecutionStore(repository).get(binding, entry.logical_call_id)
        receipt = UnitTestOutputStore(repository).get(run.run_id, entry.execution_manifest_id)
        evidence = record.to_tool_evidence()
        arguments = {'workspaceId': str(run.workspace_id), 'snapshotId': str(source.artifact_id), 'testScope': entry.selector}
        if (record.tool_name != entry.tool_name or record.role != step.agent_role
                or record.workflow_step_id != step.workflow_step_id or record.source_artifact_id != source.artifact_id
                or record.execution_manifest != source.execution_manifest() or evidence.outcome.value != 'PASS'
                or record.input_sha256 != arguments_sha256(entry.tool_name, arguments)
                or record.attempts[-1].output_sha256 != _digest(receipt.tool_output())
                or record.attempts[-1].execution_manifest_id != receipt.execution_manifest_id
                or record.attempts[-1].execution_id != receipt.execution_id or receipt.role != step.agent_role
                or receipt.workflow_step_id != step.workflow_step_id or receipt.workspace_id != run.workspace_id
                or receipt.source_artifact_id != source.artifact_id or receipt.execution_manifest != source.execution_manifest()
                or receipt.scope.kind != 'PROTECTED' or receipt.scope.name != entry.selector
                or receipt.scope.protected_suite_ref != suite_ref):
            raise ValueError('PROTECTED_RECEIPT_IDENTITY_MISMATCH')
        approved_cases = [case for case in cases if (case.tool_name, case.selector) == (entry.tool_name, entry.selector)]
        approved = {case.test_id: case for case in approved_cases}
        measured = {case.test_id for case in receipt.report.tests}
        if (not approved or set(approved) != measured or len(approved) != len(approved_cases)
                or len(measured) != len(receipt.report.tests)
                or any(case.requirement_id not in step.requirement_ids for case in approved_cases)):
            raise ValueError('PROTECTED_RECEIPT_CASE_MISMATCH')
        for actual in receipt.report.tests:
            if actual.outcome != 'FAIL':
                continue
            feedback = case_feedback(actual)
            if (feedback['observationStatus'] != 'COMPLETE' or not any(
                    row['kind'] == 'assertion' and row['status'] == 'FAIL' for row in feedback['observations'])):
                continue
            case = approved[actual.test_id]
            test_id = ':'.join((entry.tool_name, entry.selector, case.test_id))
            version = measured_test_version(receipt).get('testVersion', '')
            if not version:
                raise ValueError('PROTECTED_RECEIPT_VERSION_MISSING')
            role = step.agent_role
            category = 'QA_ASSERTION_FAILURE' if role is AgentRole.QA else 'SECURITY_REQUIREMENT_FAILURE'
            requirement = str(case.requirement_id)
            issue = IssueRecord(run_id=run.run_id, code_version=source.code_version, source_artifact_id=source.artifact_id,
                fingerprint=make_issue_fingerprint(requirement, test_id, category, test_id),
                reporter=role, requirement_ids=[case.requirement_id], category=category,
                reference_id=test_id if role is AgentRole.QA else requirement,
                title=case.title, description=json.dumps({'code': 'MEASURED_SECURITY_TESTS' if role is AgentRole.SECURITY
                    else 'MEASURED_UNIT_TEST', 'tests': [{'testId': test_id, 'outcome': 'FAIL'}]}),
                severity='FUNCTIONAL' if role is AgentRole.QA else 'SECURITY', actual_result='MEASURED_FAIL',
                normalized_location=case.test_id, cause_status='CONFIRMED', consecutive_repeat_count=0,
                evidence_refs=(evidence.evidence_ref, receipt.report_ref), protected_receipt=ProtectedReceipt(
                    receipt_id=receipt.execution_manifest_id, workflow_step_id=step.workflow_step_id,
                    test_id=test_id, test_version=version, observations_json=actual.observations_json))
            issues.append(issue)
    return tuple(issues)
