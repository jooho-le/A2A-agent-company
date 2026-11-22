"""Offline continuation of a published Developer diagnostic using measured practice feedback."""
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from diagnostics import developer_replay as replay
from diagnostics.focused_feedback import read_measurement
from orchestrator.application.dispatch import make_issue_fingerprint
from orchestrator.core.security import redact_data
from orchestrator.domain.developer_artifacts import BuildReportArtifact, ChangeReportArtifact
from orchestrator.domain.issues import IssueRecord
from orchestrator.domain.validation_artifacts import QAReportArtifact, QATestResult, SecurityReportArtifact, SecurityRequirementResult
from orchestrator.infrastructure.sqlite_workflows import _insert_artifact, _insert_step, _upsert_step


def _import_report(con, source, row):
    role = row['role']
    req = UUID(row['requirementId'])
    previous = con.execute('SELECT artifact_id,artifact_version FROM project_artifacts WHERE run_id=? '
                           'AND artifact_type=? ORDER BY artifact_version DESC LIMIT 1',
                           (str(source.run_id), role + '_REPORT')).fetchone()
    common = dict(artifact_id=uuid4(), workflow_step_id=uuid4(), run_id=source.run_id,
                  a2a_task_id='offline-practice-import-' + str(uuid4()), a2a_artifact_id=str(uuid4()),
                  artifact_version=previous[1] + 1 if previous else 1, previous_artifact_id=previous[0] if previous else None,
                  requirement_ids=[req], code_version=source.code_version, execution_manifest=source.execution_manifest())
    # Full expectation-free rows stay in the report, including PASS rows needed for history comparisons.
    feedback = {k: row[k] for k in ('testId', 'outcome', 'reason', 'testVersion', 'observationStatus', 'observations')}
    if role == 'QA':
        category = 'QA_ASSERTION_FAILURE'
        reference = row['testId']
        detail = replay._json({'code': 'MEASURED_UNIT_TEST', 'origin': 'OFFLINE_PRACTICE_IMPORT', **feedback})
        report = QAReportArtifact(**common, tests=[QATestResult(test_id=reference, title=reference, requirement_id=req,
                                                                outcome='FAIL', details=detail)])
    else:
        category = 'SECURITY_REQUIREMENT_FAILURE'
        reference = str(req)
        detail = replay._json({'code': 'MEASURED_SECURITY_TESTS', 'origin': 'OFFLINE_PRACTICE_IMPORT', 'tests': [feedback]})
        report = SecurityReportArtifact(**common, requirement_results=[SecurityRequirementResult(
            requirement_id=req, outcome='FAIL', actual_result='FAIL', details=detail)])
    _insert_step(con, replay.WorkflowStep(run_id=source.run_id, workflow_step_id=report.workflow_step_id,
        agent_role=role, status='SUCCEEDED', code_version=source.code_version, requirement_ids=[req],
        input_artifact_ids=[source.artifact_id], output_artifact_ids=[report.artifact_id],
        a2a_artifact_ids=[report.a2a_artifact_id], a2a_task_id=report.a2a_task_id,
        a2a_task_state='TASK_STATE_COMPLETED', agent_context_id='offline-practice-import'))
    _insert_artifact(con, report, replay.WorkflowStatus.REVALIDATING)
    return IssueRecord(run_id=source.run_id, code_version=source.code_version, source_artifact_id=source.artifact_id,
        report_artifact_id=report.artifact_id, requirement_ids=[req], reporter=role, category=category,
        reference_id=reference, fingerprint=make_issue_fingerprint(str(req), reference, category, reference),
        severity='HIGH' if role == 'SECURITY' else 'FUNCTIONAL', title=row['testId'], description=detail,
        actual_result='FAIL', normalized_location=reference, cause_status='CONFIRMED', consecutive_repeat_count=0)


def published_candidate(parent, prior, frozen):
    status = replay._read_json(parent / 'attempt/result.json')
    if (prior.get('purpose') != replay.PURPOSE or prior.get('measurementEligible') is not False
            or prior['approvalSha256'] != sha256(replay._json({k:v for k,v in prior.items() if k != 'approvalSha256'}).encode()).hexdigest()
            or status.get('replayId') != prior['replayId'] or status.get('taskStatus', {}).get('state') != 'TASK_STATE_COMPLETED'
            or prior['codeVersion'] != frozen.code_version or str(frozen.workflow_step_id) != prior['sourceStepId']):
        raise replay.ReplayError('FOCUSED_COMPLETED_REPAIR_REQUIRED')
    attempt = parent / 'attempt'
    types = {'SOURCE': replay.CodeSnapshotArtifact, 'CHANGE_REPORT': ChangeReportArtifact, 'BUILD_REPORT': BuildReportArtifact}
    artifacts = {}
    for artifact in replay._read_json(attempt / 'artifacts.json')['artifacts']:
        data = artifact['parts'][0]['data']
        item = types[data['artifactType']].model_validate(data)
        if (item.a2a_artifact_id != artifact['artifactId'] or item.a2a_task_id != prior['sourceTaskId']
                or item.workflow_step_id != frozen.workflow_step_id or item.run_id != frozen.run_id
                or item.code_version != frozen.code_version or item.artifact_type in artifacts):
            raise replay.ReplayError('FOCUSED_CANDIDATE_MISMATCH')
        artifacts[item.artifact_type] = item
    if set(artifacts) != set(types):
        raise replay.ReplayError('FOCUSED_CANDIDATE_MISMATCH')
    source, change, build = (artifacts[k] for k in types)
    if (source != frozen.model_copy(update={'a2a_task_id': source.a2a_task_id, 'a2a_artifact_id': source.a2a_artifact_id})
            or not build.passed or build.execution_manifest != source.execution_manifest()
            or build.source_artifact_id != source.artifact_id
            or (change.ready_review is not None and (change.ready_review.source_artifact_id != source.artifact_id
                                                     or change.ready_review.build_report_id != build.artifact_id))):
        raise replay.ReplayError('FOCUSED_CANDIDATE_MISMATCH')
    return source, change, build


async def prepare_focused(parent):
    parent = Path(parent).resolve(strict=True)
    prior = replay._read_json(parent / 'manifest.json')
    frozen, _, measured = read_measurement(parent)
    source, change, build = published_candidate(parent, prior, frozen)
    if frozen.code_version >= 4 or measured['outcome'] != 'FAIL':
        raise replay.ReplayError('FOCUSED_COMPLETED_REPAIR_REQUIRED')
    attempt = parent / 'attempt'
    policy = replay._read_json(parent / 'policy.json')
    if sha256(replay._json(policy).encode()).hexdigest() != prior['policySha256']:
        raise replay.ReplayError('FOCUSED_POLICY_CHANGED')
    root = Path(tempfile.mkdtemp(prefix='capstone-developer-replay-')).resolve()
    repo = replay._copy_inputs(attempt / 'diagnostic.sqlite3', root / 'diagnostic.sqlite3', root / 'workspaces',
                               run_id=str(source.run_id), step_id=str(source.workflow_step_id), role=replay.AgentRole.DEVELOPER)
    run = repo.get_run(source.run_id)
    step = next(s for s in repo.list_steps(source.run_id) if s.workflow_step_id == source.workflow_step_id)
    now = datetime.now(timezone.utc)
    with repo._transaction() as con:
        done = replay.WorkflowStep.model_validate({**step.model_dump(), 'status':'SUCCEEDED', 'updated_at':now,
            'a2a_task_state':'TASK_STATE_COMPLETED', 'a2a_task_id':source.a2a_task_id, 'agent_context_id':prior['sourceContextId'],
            'output_artifact_ids':[a.artifact_id for a in (source, change, build)],
            'a2a_artifact_ids':[a.a2a_artifact_id for a in (source, change, build)]})
        _upsert_step(con, done)
        for artifact in (source, change, build):
            _insert_artifact(con, artifact, replay.WorkflowStatus.REVALIDATING)
        issue = _import_report(con, source, measured)
        ready = replay.WorkflowRun.model_validate({**run.model_dump(), 'code_version':source.code_version,
                                                   'status':'FIX_REQUIRED', 'updated_at': now})
        con.execute('UPDATE workflow_runs SET status=?, updated_at=?, payload_json=? WHERE run_id=?',
                    (ready.status.value, now.isoformat(), ready.model_dump_json(), str(source.run_id)))
    run, next_step, *_ = repo.start_fix_cycle(source.run_id, [issue], developer_configured=True)
    if not next_step:
        raise replay.ReplayError('FOCUSED_FIX_LIMIT_REACHED')
    task = str(uuid4())
    old_request = replay._read_json(parent / 'request.json')
    request = {'configuration': old_request['configuration'], 'metadata': {**old_request['metadata'],
        'projectArtifactIds':[str(v) for v in next_step.input_artifact_ids], 'workflowStepId':str(next_step.workflow_step_id),
        'codeVersion':next_step.code_version, 'attempt':0}, 'message':{'role':'ROLE_USER','contextId':prior['sourceContextId'],
        'messageId':str(uuid4()),'parts':[{'mediaType':'application/json','data':{'workspaceId':str(run.workspace_id)}}]}}
    budget = replay.ExecutionBudget(runtime_budget_ms=prior['runtimeBudgetMs'], limits=replay.LLMLimits.model_validate(prior['llmLimits']))
    execution = replay.SQLiteDeveloperContextLoader(repo, lambda _: budget)(replay._context(request, task, prior['sourceContextId']))
    request['message']['parts'][0]['data'] = redact_data(execution.initial_payload)
    replay._write(root/'request.json', request)
    replay._write(root/'policy.json', policy)
    pack = replay._git(attempt/'workspaces'/str(run.workspace_id)/'source', 'pack-objects', '--stdout', '--revs',
                       data=(source.commit_hash+'\n').encode())
    (root/'objects.pack').write_bytes(pack)
    manifest = {k:prior[k] for k in ('schemaVersion','purpose','status','maxExecutions','sourceRunId','sourceContextId',
        'workspaceId','runtimeBudgetMs','llmLimits','sourceEvidenceDirectory','measurementEligible','overallVerdict','originalCodeHashes')}
    manifest.update(replayId=str(uuid4()), sourceStepId=str(next_step.workflow_step_id), sourceTaskId=task,
                    codeVersion=next_step.code_version, baseCodeVersion=source.code_version, codeHashes=replay._code_hashes(),
                    policyMode='FOCUSED_PRACTICE_FEEDBACK_DIAGNOSTIC', parentReplayId=prior['replayId'],
                    parentSnapshotSha256=source.snapshot_sha256, feedbackOrigin={'kind':'OFFLINE_PRACTICE_IMPORT',
                    'originalFileSha256':sha256((parent/'focused-result.json').read_bytes()).hexdigest()})
    with replay._readonly(attempt / 'diagnostic.sqlite3') as con:
        archive = con.execute('SELECT content FROM artifact_contents WHERE artifact_id=?', (str(source.artifact_id),)).fetchone()[0]
    return await replay._seal_preparation(root, manifest, expected_archive=archive)
