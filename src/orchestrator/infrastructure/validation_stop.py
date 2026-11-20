"""Retain independently measured validation when a sibling stops on the same Snapshot.

Draft exhaustion needs an authenticated Host recovery record. Other model stops
must match the closed automatic-stop policy. Persist the completed report, its
issues and decision atomically; permission and integrity failures stay on review.
"""
import json

from orchestrator.domain import (AgentRole, A2ATaskState, BuildReportArtifact, CodeSnapshotArtifact,
    FinalVerdict, QAReportArtifact, SecurityReportArtifact, WorkflowRun, WorkflowStatus, WorkflowStep,
    WorkflowStepStatus)
from orchestrator.domain.state_machine import transition_run
from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS

EVENT = 'QA_DRAFT_CORRECTIONS_EXHAUSTED'
EVENTS = {AgentRole.QA: EVENT, AgentRole.SECURITY: 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'}
_REPORT_ROLE = {QAReportArtifact: AgentRole.QA, SecurityReportArtifact: AgentRole.SECURITY}
_REPORT_EVENT = {AgentRole.QA: 'QA_REPORT_VALIDATED', AgentRole.SECURITY: 'SECURITY_REPORT_VALIDATED'}
_REPORT_TYPE = {AgentRole.QA: 'QA_REPORT', AgentRole.SECURITY: 'SECURITY_REPORT'}
_STOP_REASONS = ('DISABLED', 'REPEATED', 'LIMIT_REACHED')


def finish(repository, run_id, step_id, task_id, report):
    """QA draft exhausted, Security completed (the original path)."""
    if not isinstance(report, SecurityReportArtifact):
        return False
    return finish_stopped(repository, run_id, ((step_id, task_id),), report)


def finish_stopped(repository, run_id, stopped, report=None, *, generic=None, issues=(), policy_excluded_requirement_ids=(), protected_cases=()):
    """Close the Run when every stopped validator is proven and the other one (if any) completed."""
    from orchestrator.infrastructure import automatic_stop
    from orchestrator.application.validation_output import generated_coverage_gap, technical_not_run
    from orchestrator.infrastructure.sqlite_workflows import (
        _load_model, _json_model, _insert_event, _insert_artifact, _upsert_step, _record_terminal_events, _store_detected_issues, _record_revalidation_results)
    from orchestrator.domain import TraceEvent
    generic = {} if generic is None else generic
    stopped = tuple(stopped)
    if any(k not in {sid for sid, _ in stopped} or v not in automatic_stop.MODEL_STOP_CODES | {automatic_stop._SEMANTIC_TECHNICAL}
           or v == 'LLM_VALIDATION_DRAFT_EXHAUSTED' for k, v in generic.items()):
        return False
    if not 1 <= len(stopped) <= 2:
        return False
    report_role = _REPORT_ROLE.get(type(report)) if report is not None else None
    if report is not None and report_role is None:
        return False
    from orchestrator.infrastructure.protected_stop import blocked_test_write, protected_issues
    safe_step, independent = None, ()
    if protected_cases and len(stopped) == 1 and set(generic.values()) == {'LLM_TOOL_POLICY_VIOLATION'}:
        step_id, task_id = stopped[0]
        current = repository.get_run(run_id)
        if current and current.status in (WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING):
            step = next((s for s in repository.list_steps(run_id) if s.workflow_step_id == step_id), None)
            source = next((a for a in repository.list_project_artifacts(run_id) if isinstance(a, CodeSnapshotArtifact)
                           and a.code_version == current.code_version), None)
            if (step and step.a2a_task_id == task_id and step.agent_role in EVENTS and source
                    and blocked_test_write(repository, current, step, source)):
                safe_step = step
                independent = protected_issues(repository, current, step, source, protected_cases)
    with repository._transaction() as c:
        r = c.execute('SELECT payload_json FROM workflow_runs WHERE run_id=?', (str(run_id),)).fetchone()
        if not r:
            return False
        run = _load_model(WorkflowRun, r[0])
        steps = []
        for step_id, task_id in stopped:
            q = c.execute('SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?',
                          (str(run_id), str(step_id))).fetchone()
            if not q:
                return False
            step = _load_model(WorkflowStep, q[0])
            stopped_state = (step.status, step.a2a_task_state)
            failed = stopped_state == (WorkflowStepStatus.FAILED, A2ATaskState.FAILED)
            waiting = stopped_state == (WorkflowStepStatus.WAITING_INPUT, A2ATaskState.INPUT_REQUIRED)
            if (step.agent_role not in EVENTS or step.a2a_task_id != task_id
                    or not (failed or (step_id in generic and waiting))):
                return False
            steps.append(step)
        roles = {step.agent_role for step in steps}
        if len(roles) != len(steps):
            return False
        if report is None and roles != {AgentRole.QA, AgentRole.SECURITY} and not independent:
            return False
        if report is not None and (report_role in roles or roles | {report_role} != {AgentRole.QA, AgentRole.SECURITY}):
            return False
        event_for = {step.workflow_step_id: (step.agent_role.value + '_MODEL_VALIDATION_STOP'
                     if step.workflow_step_id in generic else EVENTS[step.agent_role]) for step in steps}
        if run.status == WorkflowStatus.FINISHED:
            done = []
            for step in steps:
                done.append(c.execute("SELECT 1 FROM trace_events WHERE run_id=? AND event_type=? "
                    "AND json_extract(payload_json,'$.workflowStepId')=?",
                    (str(run_id), event_for[step.workflow_step_id], str(step.workflow_step_id))).fetchone() is not None)
            return all(done)
        if run.status not in (WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING):
            return False
        if len(generic) < len(stopped) and not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_runtime_events'").fetchone():
            return False
        bindings = {}
        for step in steps:
            if step.workflow_step_id in generic:
                continue
            proofs = c.execute("SELECT r.payload_json FROM agent_runtime_events r JOIN trace_events t "
                "ON t.event_id=r.trace_event_id AND t.run_id=r.run_id WHERE r.run_id=? AND r.workflow_step_id=? "
                "AND t.event_type='LLM_RECOVERY_DECIDED'", (str(run_id), str(step.workflow_step_id))).fetchall()
            found = []
            for row in proofs:
                payload = json.loads(row[0])
                b, d = payload.get('binding', {}), payload.get('detail', {})
                route = d.get('recovery', {})
                if (b.get('a2aTaskId') == step.a2a_task_id and b.get('agentContextId') == step.agent_context_id
                        and b.get('actor') in (step.agent_role.value, 'SINGLE_AGENT')
                        and b.get('attempt') == step.attempt and b.get('codeVersion') == step.code_version
                        and d.get('purpose') == 'LLM_RECOVERY' and route.get('kind') == 'DECISION'
                        and route.get('code') == 'LLM_VALIDATION_DRAFT_EXHAUSTED' and route.get('action') == 'STOP'
                        and route.get('reason') in _STOP_REASONS):
                    found.append(b)
            if not found:
                return False
            bindings[step.workflow_step_id] = found
        rows = c.execute('SELECT artifact_type,payload_json FROM project_artifacts WHERE run_id=?', (str(run_id),))
        sources, builds = [], []
        for kind, raw in rows:
            if kind == 'SOURCE':
                sources.append(_load_model(CodeSnapshotArtifact, raw))
            elif kind == 'BUILD_REPORT':
                builds.append(_load_model(BuildReportArtifact, raw))
        source = next((a for a in sources if a.code_version == run.code_version), None)
        build = next((a for a in builds if a.code_version == run.code_version), None)
        if (source is None or build is None or not build.passed or build.tool_evidence is None
                or build.execution_manifest != source.execution_manifest()):
            return False
        for step in steps:
            if step.code_version != source.code_version or step.input_artifact_ids != [source.artifact_id]:
                return False
            if step.workflow_step_id not in generic and not any(b.get('snapshotSha256') == source.snapshot_sha256
                       and b.get('inputArtifactIds') == [str(source.artifact_id)]
                       and set(b.get('requirementIds', [])) == {str(i) for i in step.requirement_ids}
                       for b in bindings[step.workflow_step_id]):
                return False
        other = None
        if report is not None:
            if (report.run_id != run_id or report.code_version != source.code_version
                    or report.execution_manifest != source.execution_manifest()):
                return False
            row = c.execute('SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?',
                            (str(run_id), str(report.workflow_step_id))).fetchone()
            if row is None:
                return False
            other = _load_model(WorkflowStep, row[0])
            if (other.agent_role != report_role or other.status != WorkflowStepStatus.SUCCEEDED
                    or other.a2a_task_state != A2ATaskState.COMPLETED or other.a2a_task_id != report.a2a_task_id
                    or other.code_version != source.code_version or other.input_artifact_ids != [source.artifact_id]
                    or report.a2a_artifact_id not in other.a2a_artifact_ids
                    or set(other.requirement_ids) != set(report.requirement_ids)):
                return False
            items = report.tests if report_role is AgentRole.QA else report.requirement_results
            if any((i.tool_evidence is None and not generated_coverage_gap(i))
                   or (i.outcome.value in ('PASS', 'FAIL') and i.tool_evidence is not None
                       and i.tool_evidence.outcome.value != 'PASS') for i in items):
                return False
            if any(i.outcome.value == 'UNVERIFIED' and not (technical_not_run(i) or generated_coverage_gap(i)) for i in items):
                return False
            if report_role is AgentRole.SECURITY and any(
                    f.disposition.value in ('SUSPECTED', 'UNVERIFIED') or
                    f.disposition.value == 'CONFIRMED' and f.severity.value == 'MEDIUM' for f in report.findings):
                return False
            prior = c.execute("SELECT artifact_id,artifact_version FROM project_artifacts WHERE run_id=? "
                "AND artifact_type=? ORDER BY artifact_version DESC LIMIT 1",
                (str(run_id), _REPORT_TYPE[report_role])).fetchone()
            if (prior is None and (report.artifact_version != 1 or report.previous_artifact_id is not None)
                    or prior is not None and (report.artifact_version != prior[1] + 1
                                              or str(report.previous_artifact_id) != prior[0])):
                return False
        # Caller-supplied issues still require a completed A2A report. The only other
        # origin admitted here is the Host's independent, bound measurement above.
        if any(report is None or i.report_artifact_id != report.artifact_id or not i.evidence_refs for i in issues):
            return False
        if any(i.protected_receipt is not None for i in issues):
            return False
        if independent and (steps != [safe_step] or any(i.run_id != run_id or i.source_artifact_id != source.artifact_id
                or i.code_version != source.code_version for i in independent)):
            return False
        issues = (*issues, *independent)
        retry = bool(issues and run.fix_attempt < MAX_CODE_FIX_ATTEMPTS and not
                     {'LLM_BUDGET_EXHAUSTED', 'SINGLE_AGENT_CONTEXT_LIMIT'}.intersection(generic.values()))
        if retry:
            final = transition_run(run, WorkflowStatus.FIX_REQUIRED)
        else:
            final = transition_run(run, WorkflowStatus.FINISHED,
                                   verdict=FinalVerdict.FAIL if issues else FinalVerdict.UNVERIFIED)
        if report is not None:
            _insert_artifact(c, report, run.status)
            _upsert_step(c, other.model_copy(update={'output_artifact_ids': [report.artifact_id]}))
            _insert_event(c, TraceEvent(run_id=run_id, workflow_step_id=other.workflow_step_id,
                a2a_task_id=other.a2a_task_id, event_type=_REPORT_EVENT[report_role], actor='Orchestrator',
                attempt=other.attempt, requirement_ids=other.requirement_ids,
                input_artifact_ids=other.input_artifact_ids, output_artifact_ids=[report.artifact_id],
                a2a_task_state=other.a2a_task_state, code_version=run.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
            _insert_event(c, TraceEvent(run_id=run_id, workflow_step_id=other.workflow_step_id,
                event_type='VALIDATION_FINISHED', actor=other.agent_role.value, attempt=run.fix_attempt,
                requirement_ids=other.requirement_ids, input_artifact_ids=other.input_artifact_ids,
                output_artifact_ids=[report.artifact_id], code_version=run.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
        _store_detected_issues(c, run, issues, independent_issues={i.issue_id for i in independent})
        _record_revalidation_results(c, run,
            report if report_role is AgentRole.QA else None,
            report if report_role is AgentRole.SECURITY else None)
        _insert_event(c, TraceEvent(run_id=run_id, event_type='VALIDATION_DECISION_' + final.status.value,
            actor='Orchestrator', attempt=run.fix_attempt,
            requirement_ids=list(dict.fromkeys(req for step in steps + ([other] if other else [])
                                               for req in step.requirement_ids)),
            output_artifact_ids=[report.artifact_id] if report else [],
            code_version=run.code_version, snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
        if policy_excluded_requirement_ids:
            _insert_event(c, TraceEvent(run_id=run_id, event_type='VALIDATION_CONFLICT_POLICY_EXCLUDED',
                actor='Orchestrator', attempt=run.fix_attempt, requirement_ids=list(policy_excluded_requirement_ids),
                output_artifact_ids=[report.artifact_id], code_version=run.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
        c.execute('UPDATE workflow_runs SET status=?,updated_at=?,payload_json=? WHERE run_id=?',
                  (final.status.value, final.updated_at.isoformat(), _json_model(final), str(run_id)))
        retained = [source.artifact_id, build.artifact_id] + ([report.artifact_id] if report is not None else [])
        for step in sorted(steps, key=lambda item: item.agent_role is AgentRole.SECURITY):
            _insert_event(c, TraceEvent(run_id=run_id, workflow_step_id=step.workflow_step_id,
                a2a_task_id=step.a2a_task_id, agent_context_id=step.agent_context_id,
                event_type=event_for[step.workflow_step_id], actor='Orchestrator', attempt=step.attempt,
                requirement_ids=step.requirement_ids, input_artifact_ids=step.input_artifact_ids,
                output_artifact_ids=retained, code_version=run.code_version,
                snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
            if step.workflow_step_id in generic:
                _insert_event(c, TraceEvent(run_id=run_id, workflow_step_id=step.workflow_step_id,
                    event_type='VALIDATOR_STOP_' + generic[step.workflow_step_id], actor='Orchestrator',
                    a2a_task_id=step.a2a_task_id, attempt=step.attempt, code_version=run.code_version,
                    snapshot_sha256=source.snapshot_sha256, workflow_state=final.status))
        if generic and not retry:
            automatic_stop._ensure_table(c)
            step = next(step for step in steps if step.workflow_step_id in generic)
            automatic_stop._record(c, final, stage=step.agent_role.value, reason=generic[step.workflow_step_id],
                                   step=step, task_id=step.a2a_task_id, source=source)
        _record_terminal_events(c, final)
        return True
