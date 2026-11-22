"""Restore the last Host-build-passing candidate after a product startup regression.

No success is inferred, no history is deleted, and the original experiment
baseline, budgets, code version and fix counter are unchanged.
"""
from orchestrator.domain.states import AgentRole
from orchestrator.domain.trace import TraceEvent
from orchestrator.infrastructure.sqlite_workflows import _insert_event
from copy import deepcopy
from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_input_failures import INVALID_INPUT
from agents.runtime.repair_progress import candidate_history

POLICY = 'LAST_BUILD_PASS_WITH_ACTIVE_REPAIR_AND_HISTORY_REFERENCES_V3'


def with_repair_baseline(task_input, checkpoint):
    """Project the restored candidate's authenticated work as the active request.

    The original A2A request remains immutable and is retained as history. Merely
    appending extra feedback leaves a contradictory active build-failure task.
    """
    history = getattr(checkpoint, "repair_history", None)
    records = getattr(checkpoint, "repair_issues", ())
    source_files = getattr(checkpoint, "repair_source_files", None)
    if checkpoint.repair_baseline is not None:
        baseline = deepcopy(checkpoint.repair_baseline)
        active = baseline.pop('fixRequest')
        baseline['rejectedCandidateFixRequest'] = deepcopy(task_input['fixRequest'])
        task_input = {**task_input, 'fixRequest': active, 'repairBaseline': baseline}
    projected = compact_fix_input(task_input, repair_history=history, issue_records=records, source_files=source_files)
    targets = projected.get('activeRepairFocus', {}).get('brief', {}).get('targets', ())
    # A discarded redaction may matter for an unrelated performance or build issue.
    # Only reject it when every selected issue has measured output paths.
    checkpoint.observed_output_repair = bool(targets) and all(
        target.get('checks') and all(set(check.get('observedFailurePaths', ())) - {INVALID_INPUT}
                                     for check in target['checks'])
        for target in targets)
    return projected


def restore_baseline(services, execution, checkpoint):
    previous = execution.previous_source
    if previous is None:
        return
    artifacts = services._repository.list_project_artifacts(previous.run_id)
    checkpoint.repair_issues = execution.fix_issues
    active = _restore_if_needed(services, execution, checkpoint, artifacts, previous) or previous
    checkpoint.repair_history = candidate_history(services, execution, artifacts, active)


def _restore_if_needed(services, execution, checkpoint, artifacts, previous):
    builds = [a for a in artifacts if a.artifact_type == 'BUILD_REPORT']
    failed = [a for a in builds if a.execution_manifest == previous.execution_manifest()]
    if not failed:
        return
    latest = max(failed, key=lambda a: a.artifact_version)
    if latest.execution_outcome.value != 'FAIL' or latest.failure_kind != 'PRODUCT':
        return
    passed = [a for a in builds if a.passed and a.code_version < previous.code_version]
    if not passed:
        return  # no measured good candidate: repair the current one
    build = max(passed, key=lambda a: (a.code_version, a.artifact_version))
    source = next(a for a in artifacts if a.artifact_type == 'SOURCE'
                  and a.artifact_id == build.execution_manifest.project_artifact_id)
    frozen = services._artifacts.bind(previous.run_id, role=AgentRole.DEVELOPER).read(source.artifact_id)
    snapshot = checkpoint._builder(checkpoint._deadline(execution.budget.deadline_monotonic)).build(
        source.commit_hash, services._lock_path)
    if (snapshot.archive != frozen.content or snapshot.snapshot_sha256 != source.snapshot_sha256
            or snapshot.tree_hash != source.tree_hash or source.repository_id != services._repository_id):
        raise ValueError('REPAIR_BASELINE_EVIDENCE_CHANGED')
    reports = [a for a in artifacts if a.artifact_type in ('QA_REPORT', 'SECURITY_REPORT')
               and a.execution_manifest == source.execution_manifest()]
    # Only latest revision per role, preserving every sibling observation.
    # sorted(): a set's string order varies per process (hash seed), which changed the replay input
    # hash for the same stored Run (Codex review 2026-11-18 #3).
    reports = [max((r for r in reports if r.artifact_type == kind),
                   key=lambda r: r.artifact_version) for kind in sorted({r.artifact_type for r in reports})]
    report_ids = {r.artifact_id for r in reports}
    issues = [i for i in services._repository.list_issue_records(previous.run_id)
              if i.source_artifact_id == source.artifact_id
              and (i.report_artifact_id in report_ids or i.protected_receipt is not None)]
    # Do not restore into an obsolete build-only request with no authenticated
    # remaining work. A good build alone does not prove the product is correct.
    if not issues:
        return
    if any(i.run_id != source.run_id or i.code_version != source.code_version
           or not set(i.requirement_ids).issubset(execution.metadata.requirement_ids) for i in issues):
        raise ValueError('REPAIR_BASELINE_ISSUE_MISMATCH')
    from orchestrator.application.dispatch import PlannerRunDispatcher
    run = services._repository.get_run(previous.run_id)
    if run.fix_attempt != execution.fix_attempt:
        raise ValueError('REPAIR_BASELINE_ATTEMPT_CHANGED')
    active_request = PlannerRunDispatcher.build_fix_payload(
        run, None, execution.plan, execution.scenario, issues, [source, build, *reports])['fixRequest']
    active_request['workingSourceArtifactId'] = str(source.artifact_id)
    active_request['gitParentSourceArtifactId'] = str(previous.artifact_id)
    checkpoint.restore_repair_baseline(snapshot, deadline_monotonic=execution.budget.deadline_monotonic)
    checkpoint.repair_issues = tuple(issues)
    checkpoint.repair_baseline = {
        'policy': POLICY, 'sourceArtifactId': str(source.artifact_id),
        'codeVersion': source.code_version, 'snapshotSha256': source.snapshot_sha256,
        'rejectedSourceArtifactId': str(previous.artifact_id),
        'validationFeedback': active_request['validationFeedback'],
        'fixRequest': active_request,
        'instruction': 'Working files were restored from the last Host Build PASS candidate. '
            'The top-level fixRequest now names the remaining measured issues on those working files. '
            'The rejectedCandidateFixRequest is history from a different candidate, not the current edit target. '
            'It is not a product PASS. Read the current files and their SHA before partial edits; repair '
            'the active fixRequest issues while preserving the passing siblings in validationFeedback. '
            'Restoration alone is not progress. All budgets and limits remain unchanged.'}
    with services._repository._transaction() as connection:
        _insert_event(connection, TraceEvent(run_id=source.run_id,
            event_type='DEVELOPMENT_BASELINE_RESTORED', actor='Orchestrator',
            workflow_step_id=execution.metadata.workflow_step_id,
            code_version=execution.metadata.code_version, attempt=execution.fix_attempt,
            input_artifact_ids=[previous.artifact_id, source.artifact_id, build.artifact_id,
                                *[r.artifact_id for r in reports]],
            snapshot_sha256=source.snapshot_sha256))
    return source
