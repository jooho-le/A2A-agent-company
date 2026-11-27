"""Schedule measured previous generated cases before the validator changes its draft."""
from mcp_tools.tools import generated_recheck
from mcp_tools.tools.files import _run_file_operation
from orchestrator.domain.repair_progress import selected_checks
from orchestrator.domain.validation_feedback import measured_test_version, validation_feedback


def previous_scopes(services, execution, selectors):
    source = execution.source
    prior = getattr(execution, 'previous_report', None)
    parent = getattr(execution, 'previous_source', None)
    if not selectors or not prior or not parent or prior.execution_manifest != parent.execution_manifest():
        return ()
    binding = services.configuration.binding
    changes = [artifact for artifact in services._repository.list_project_artifacts(source.run_id)
               if artifact.artifact_type == 'CHANGE_REPORT' and artifact.code_version == source.code_version]
    if len(changes) != 1:
        return ()
    review = changes[0].ready_review
    if (not review or review.source_artifact_id != source.artifact_id or review.build_outcome != 'PASS'
            or review.reported_path_edits and not any(
                path.host_diff_status in ('INDIRECT_PENDING_TEST', 'NO_CHANGE_CLAIMED')
                for path in review.reported_path_edits)):
        return ()
    issues = {issue.issue_id: issue for issue in services._repository.list_issue_records(source.run_id)}
    feedback = validation_feedback([parent, prior])
    entries = prior.tests if prior.artifact_type == 'QA_REPORT' else prior.requirement_results
    proofs = {proof.evidence_ref: proof for entry in entries for proof in (entry.tool_evidence, *entry.execution_evidence)
              if proof and proof.tool_name == 'run_unit_tests' and proof.execution_manifest == prior.execution_manifest
              and proof.outcome.value == 'PASS'}
    result = {}
    for check in review.rechecks:
        issue = issues.get(check.issue_id)
        if (check.role != binding.role.value or not issue or issue.source_artifact_id != parent.artifact_id
                or check.prior_report_id != prior.artifact_id or issue.report_artifact_id != prior.artifact_id):
            continue
        for row in selected_checks(issue.model_dump(mode='json', by_alias=True), feedback):
            name = row['testId'].split(':', 2)
            if (len(name) != 3 or name[0] != 'run_unit_tests' or name[1] not in selectors
                    or row['testId'] != check.test_id or row['requirementId'] != str(check.requirement_id)
                    or not row.get('testVersion', '').startswith('inputs:')):
                continue
            matched = [proofs[ref] for ref in row['evidenceRefs'] if ref in proofs]
            if len(matched) != 1:
                continue
            proof, = matched
            call = services._tools.get(binding, proof.execution_id)
            if call.to_tool_evidence() != proof or call.workflow_step_id != prior.workflow_step_id:
                raise ValueError('GENERATED_RECHECK_EVIDENCE_INVALID')
            receipt = services._unit.get(binding.run_id, call.attempts[-1].execution_manifest_id)
            if (receipt.scope.kind != 'QA_TESTS' or receipt.scope.name != name[1] or receipt.role != binding.role
                    or receipt.execution_manifest != prior.execution_manifest or receipt.workflow_step_id != prior.workflow_step_id
                    or measured_test_version(receipt).get('testVersion') != row['testVersion']
                    or not any(case.test_id == name[2] for case in receipt.report.tests)):
                raise ValueError('GENERATED_RECHECK_EVIDENCE_INVALID')
            actual = next(case for case in receipt.report.tests if case.test_id == name[2])
            if generated_recheck.case_outcome(row, actual, execution.configuration.scenario_contract) != 'FAIL':
                continue  # A verdict from a different Host rule needs its own rule's evidence.
            result[(name[1], row['testVersion'])] = receipt
    return tuple(result.values())


async def run_previous_scopes(services, execution, tracked, selectors, measure):
    if execution.source.code_version <= 1 or not selectors:
        return
    priors = await _run_file_operation(previous_scopes, services, execution, selectors)
    for old in priors:
        execution.budget.check()
        step = execution.metadata.workflow_step_id
        admitted = await _run_file_operation(generated_recheck.claim, services._repository, step, execution.source, old)
        if admitted:
            # measure uses the usual tracked tool, global budget and receipt authentication.
            measured = await measure(old.scope.name)
            await _run_file_operation(generated_recheck.complete, services._repository, step, execution.source,
                                      old, measured[0] if measured else None)
