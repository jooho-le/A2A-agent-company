"""모델 행동으로 멈춘 Run을 마지막 측정 결과로 끝낸다(docs/live-api/05 22절 일반 종료 규칙).

2026-11-18 석민 결정·구현 요청("둘 다 구현해"). 주호 확인 필요(사람 검토의 의미가 바뀐다).

지금까지는 멈추는 경우를 하나씩 찾아 개별 종료 규칙을 더했다(형식 교정 소진, 같은 결함 반복 등).
실제 Run은 매번 다른 곳에서 멈췄다. 이 모듈은 남은 경우를 한 규칙으로 닫는다.

규칙
* 빌드 결과를 Host가 측정한 등록 후보(Snapshot)가 있고,
* 멈춘 이유가 모델 행동(형식·질문·의미 보류·참조/연결 오류·예산 소진)이거나
  판정 단계의 '증거 부족' 보류이면,
* 사람 검토 대신 FINISHED로 끝낸다. 그 후보에 도구 근거가 있는 측정 FAIL이 있으면 FAIL, 없으면 UNVERIFIED.
  판정 단계 보류에 측정 FAIL이 있고 코드 수정 기회가 남았으면 끝내지 않고 알려진 FAIL을 수정 단계로 보낸다.

절대 SUCCESS를 만들지 않는다. 판정은 Host가 등록한 측정 결과로만 정한다(Agent가 보낸 코드는 '지금 멈춘다'만 정한다).
제공자·인증·설정·도구 장애, 권한·Context 거부, 증거/Snapshot/Manifest 불일치, 예외, 사용자 취소,
시나리오 기준 없음은 해당하지 않으며 지금처럼 사람 검토로 간다. 첫 후보가 생기기 전(Planner·첫 구현)도 해당하지 않는다.
"""
from datetime import datetime, timezone

from orchestrator.domain import (AgentRole, BuildReportArtifact, CodeSnapshotArtifact, FinalVerdict, TraceEvent,
                                 WorkflowRun, WorkflowStatus, WorkflowStep)
from orchestrator.domain.state_machine import transition_run

POLICY = 'MODEL_STOP_PRESERVES_COMPLETED_VALIDATION_V2'
EVENT = 'AUTOMATIC_STOP_FINISHED'
ROUTED_TO_FIX = 'VERDICT_HOLD_ROUTED_TO_FIX'

# Agent가 FAILED·INPUT_REQUIRED Task에 실어 보내는 코드 중 '모델 행동'으로 닫힌 것.
# 거절은 넣지 않는다(Codex 재검토 2026-11-18 #4): 모델 거절(LLM_REFUSED)·DEVELOPER_REJECTED는 REJECTED
# 상태로 오고, 설명된 거절 소진(LLM_DEVELOPMENT_REJECTION_EXHAUSTED)에는 권한·정책 거절도 섞인다. 근거가
# 확인된 거절은 Host 증명 경로(DEVELOPMENT_REJECTION_EXHAUSTED)가 닫고, 나머지는 사람 검토로 남긴다.
MODEL_STOP_CODES = frozenset({
    # 모델 응답 형식·내용(교정 한도를 쓴 뒤)
    'LLM_RESPONSE_INVALID', 'LLM_SCHEMA_INVALID', 'LLM_RESPONSE_INCOMPLETE',
    'LLM_TOOL_POLICY_VIOLATION', 'LLM_DEVELOPMENT_NO_PROGRESS',
    'LLM_SECURITY_REFERENCE_INVALID', 'LLM_VALIDATION_DRAFT_EXHAUSTED',
    # 모든 구성에 같은 한도(공정한 자원 한계)
    'LLM_BUDGET_EXHAUSTED', 'SINGLE_AGENT_CONTEXT_LIMIT',
    # 모델의 결정: 질문·의미 검토 보류
    'DEVELOPER_INPUT_REQUIRED', 'QA_INPUT_REQUIRED', 'SECURITY_INPUT_REQUIRED',
    'SEMANTIC_REVIEW_INPUT_REQUIRED',
    # 모델이 쓴 보고서·시험의 연결·참조 오류(Host 검증기가 거부)
    'SECURITY_CODE_REFERENCE_INVALID', 'QA_REPORT_BINDING_INVALID', 'SECURITY_TEST_BINDING_INVALID',
    'QA_TEST_FIX_INVALID', 'SECURITY_TEST_FIX_INVALID',
    'DEVELOPER_OUTPUT_INVALID', 'QA_OUTPUT_INVALID', 'SECURITY_OUTPUT_INVALID',
})
# 의미 검토 자체가 검토 모델의 응답 형식·근거 인용 오류로 끝난 경우만. 검토 기준(BINDING)이 이번 작업과 다른 것은 제외.
_SEMANTIC_TECHNICAL = 'SEMANTIC_REVIEW_FAILED'
_SEMANTIC_MODEL_CATEGORIES = frozenset({'FORMAT', 'EVIDENCE'})

# 판정 단계 보류 중 '증거가 모자라 확정 못 함'인 것만. 원문은 validation_output.decide_verdict와 dispatch의 문장이다.
# 문장이 바뀌면 여기와 맞지 않아 보류가 유지된다(안전한 쪽). tests/test_automatic_stop.py가 원문 존재를 확인한다.
VERDICT_HOLD_REASONS = {
    'Required results lack MCP Tool execution provenance': 'RESULT_PROVENANCE_MISSING',
    'Host protected validation remains unverified; generated-test recovery cannot settle it':
        'PROTECTED_VALIDATION_UNVERIFIED',
    'Required Tool validation is incomplete without proof that safe retries were exhausted': 'VALIDATION_INCOMPLETE',
    'Required PASS results lack successful MCP Tool execution provenance': 'PASS_PROVENANCE_MISSING',
    'Trace does not prove complete requirement-to-validation lineage': 'TRACE_LINEAGE_UNPROVEN',
    # 스캐너 발견을 Host 의미 증명이 확정·오탐으로 판단하지 못한 상태(SUSPECTED·UNVERIFIED·확정 MEDIUM).
    # 판단이 남은 것이지 통과가 아니므로 SUCCESS가 아니라 UNVERIFIED(측정 FAIL이 있으면 FAIL)로 남긴다.
    'Security findings require a policy decision': 'SECURITY_FINDING_JUDGMENT_PENDING',
}

_FINISHABLE = frozenset({WorkflowStatus.FIXING, WorkflowStatus.VALIDATING, WorkflowStatus.REVALIDATING,
                         WorkflowStatus.FIX_REQUIRED})
_ROLES = frozenset({AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY})
_TABLE = (
    'CREATE TABLE IF NOT EXISTS automatic_stop_records ('
    'run_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, stage TEXT NOT NULL, reason_code TEXT NOT NULL, '
    'workflow_step_id TEXT, a2a_task_id TEXT, code_version INTEGER NOT NULL, verdict TEXT NOT NULL, '
    'policy TEXT NOT NULL, created_at TEXT NOT NULL)',
    'CREATE TRIGGER IF NOT EXISTS automatic_stop_records_no_update BEFORE UPDATE ON automatic_stop_records '
    "BEGIN SELECT RAISE(ABORT, 'AUTOMATIC_STOP_RECORD_IMMUTABLE'); END",
    'CREATE TRIGGER IF NOT EXISTS automatic_stop_records_no_delete BEFORE DELETE ON automatic_stop_records '
    "BEGIN SELECT RAISE(ABORT, 'AUTOMATIC_STOP_RECORD_IMMUTABLE'); END",
)


def agent_stop_code(outcome):
    """Agent가 실패·질문으로 끝낸 Task의 코드가 모델 행동이면 그 코드, 아니면 None."""
    from google.protobuf.json_format import MessageToDict
    from orchestrator.application.a2a_tasks import A2ATaskRunResult, TaskRunDisposition
    if (not isinstance(outcome, A2ATaskRunResult) or outcome.task is None
            or outcome.step.agent_role not in _ROLES
            or outcome.disposition not in (TaskRunDisposition.AGENT_FAILED, TaskRunDisposition.WAITING_INPUT,
                                           TaskRunDisposition.HUMAN_REVIEW)):
        return None
    try:
        status = MessageToDict(outcome.task.status)
    except Exception:
        return None
    if status.get('state') not in ('TASK_STATE_FAILED', 'TASK_STATE_INPUT_REQUIRED'):
        return None
    parts = status.get('message', {}).get('parts', [])
    if len(parts) != 1 or type(parts[0].get('data')) is not dict:
        return None
    data = parts[0]['data']
    code = data.get('code')
    if code == _SEMANTIC_TECHNICAL:
        review = data.get('semanticReview')
        return code if type(review) is dict and review.get('category') in _SEMANTIC_MODEL_CATEGORIES else None
    return code if code in MODEL_STOP_CODES else None


def _ensure_table(connection):
    for statement in _TABLE:
        connection.execute(statement)


def _record(connection, run, *, stage, reason, step, task_id, source):
    from orchestrator.infrastructure.sqlite_workflows import _insert_event
    event = TraceEvent(run_id=run.run_id, workflow_step_id=None if step is None else step.workflow_step_id,
                       a2a_task_id=task_id, agent_context_id=None if step is None else step.agent_context_id,
                       event_type=EVENT, actor='Orchestrator', attempt=run.fix_attempt,
                       requirement_ids=[] if step is None else step.requirement_ids,
                       input_artifact_ids=[source.artifact_id], output_artifact_ids=[source.artifact_id],
                       code_version=run.code_version, snapshot_sha256=source.snapshot_sha256,
                       workflow_state=run.status)
    _insert_event(connection, event)
    connection.execute(
        'INSERT INTO automatic_stop_records(run_id,event_id,stage,reason_code,workflow_step_id,a2a_task_id,'
        'code_version,verdict,policy,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (str(run.run_id), str(event.event_id), stage, reason,
         None if step is None else str(step.workflow_step_id), task_id, run.code_version, run.verdict.value,
         POLICY, datetime.now(timezone.utc).isoformat()))


def _candidate(connection, run):
    """(Source, Build) of the last registered candidate when its build was measured by the Host."""
    from orchestrator.domain.tool_evidence import ToolExecutionOutcome
    from orchestrator.infrastructure.sqlite_workflows import _load_model
    rows = connection.execute("SELECT artifact_type,payload_json FROM project_artifacts WHERE run_id=? "
                              "AND artifact_type IN ('SOURCE','BUILD_REPORT')", (str(run.run_id),)).fetchall()
    sources = [a for a in (_load_model(CodeSnapshotArtifact, raw) for kind, raw in rows if kind == 'SOURCE')
               if a.code_version == run.code_version]
    builds = [a for a in (_load_model(BuildReportArtifact, raw) for kind, raw in rows if kind == 'BUILD_REPORT')
              if a.code_version == run.code_version]
    if len(sources) != 1 or not builds:
        return None
    source = sources[0]
    build = max(builds, key=lambda item: (item.artifact_version, item.created_at))
    measured = build.tool_evidence is not None and (
        build.passed or (build.execution_outcome == ToolExecutionOutcome.FAIL and build.failure_kind == 'PRODUCT'))
    if not measured or build.execution_manifest != source.execution_manifest():
        return None
    return source, build


def finish_agent_stop(repository, run_id, step_id, task_id, code):
    """A Developer/QA/Security Task stopped for a model reason: close with the last measured candidate."""
    from orchestrator.infrastructure.sqlite_workflows import (
        _current_measured_failure, _json_model, _load_model, _record_terminal_events)
    if code not in MODEL_STOP_CODES and code != _SEMANTIC_TECHNICAL:
        return False
    with repository._transaction() as connection:
        _ensure_table(connection)
        row = connection.execute('SELECT payload_json FROM workflow_runs WHERE run_id=?', (str(run_id),)).fetchone()
        step_row = connection.execute('SELECT payload_json FROM workflow_steps WHERE run_id=? AND workflow_step_id=?',
                                      (str(run_id), str(step_id))).fetchone()
        if row is None or step_row is None:
            return False
        run, step = _load_model(WorkflowRun, row[0]), _load_model(WorkflowStep, step_row[0])
        prior = connection.execute('SELECT workflow_step_id,a2a_task_id FROM automatic_stop_records WHERE run_id=?',
                                   (str(run_id),)).fetchone()
        if run.status in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED):
            # 같은 멈춤(같은 Step·Task)을 다시 관찰해도 한 번만 닫는다. 다른 Task는 그 증거가 아니다.
            return bool(prior and tuple(prior) == (str(step_id), task_id))
        if (run.status not in _FINISHABLE or run.code_version is None or prior is not None
                or step.agent_role not in _ROLES or step.a2a_task_id != task_id):
            return False
        expected = run.code_version + 1 if run.status == WorkflowStatus.FIXING else run.code_version
        if step.code_version != expected:
            return False
        candidate = _candidate(connection, run)
        if candidate is None:
            return False
        source, _build = candidate
        verdict = FinalVerdict.FAIL if _current_measured_failure(connection, run) else FinalVerdict.UNVERIFIED
        final = transition_run(run, WorkflowStatus.FINISHED, verdict=verdict)
        connection.execute('UPDATE workflow_runs SET status=?,updated_at=?,payload_json=? WHERE run_id=?',
                           (final.status.value, final.updated_at.isoformat(), _json_model(final), str(run_id)))
        _record(connection, final, stage=step.agent_role.value, reason=code, step=step, task_id=task_id,
                source=source)
        _record_terminal_events(connection, final)
        return True


def verdict_fallback(reason, *, measured_failure, fix_attempt):
    """An evidence-shortage/judgment hold becomes (target status, verdict, reason code), or None to keep it.

    A measured FAIL with code-fix attempts left goes to the normal fix cycle with the known failures
    (the unsettled items stay unsettled and are re-measured on the next Snapshot). Otherwise the Run
    ends: FAIL with a measured failure, UNVERIFIED without. Never SUCCESS.
    """
    from orchestrator.domain.constants import MAX_CODE_FIX_ATTEMPTS
    code = VERDICT_HOLD_REASONS.get(reason)
    if code is None:
        return None
    if measured_failure and fix_attempt < MAX_CODE_FIX_ATTEMPTS:
        return WorkflowStatus.FIX_REQUIRED, None, code
    return WorkflowStatus.FINISHED, (FinalVerdict.FAIL if measured_failure else FinalVerdict.UNVERIFIED), code


def record_hold_routed_to_fix(repository, run_id, reason_code):
    """Keep why a verdict hold was sent to the fix cycle (Trace only; the Run is not terminal)."""
    from orchestrator.infrastructure.sqlite_workflows import _insert_event, _load_model
    if reason_code not in VERDICT_HOLD_REASONS.values():
        return False
    with repository._transaction() as connection:
        row = connection.execute('SELECT payload_json FROM workflow_runs WHERE run_id=?', (str(run_id),)).fetchone()
        if row is None:
            return False
        run = _load_model(WorkflowRun, row[0])
        if run.status != WorkflowStatus.FIX_REQUIRED or run.code_version is None:
            return False
        _insert_event(connection, TraceEvent(run_id=run.run_id, event_type=ROUTED_TO_FIX, actor='Orchestrator',
                                             attempt=run.fix_attempt, code_version=run.code_version,
                                             workflow_state=run.status))
        return True


def record_verdict_stop(repository, run_id, reason_code):
    """After record_validation_results finished the Run from a converted hold, keep why (append-only)."""
    from orchestrator.infrastructure.sqlite_workflows import _load_model
    if reason_code not in VERDICT_HOLD_REASONS.values():
        return False
    with repository._transaction() as connection:
        _ensure_table(connection)
        row = connection.execute('SELECT payload_json FROM workflow_runs WHERE run_id=?', (str(run_id),)).fetchone()
        if row is None:
            return False
        run = _load_model(WorkflowRun, row[0])
        if connection.execute('SELECT 1 FROM automatic_stop_records WHERE run_id=?', (str(run_id),)).fetchone():
            return True
        if (run.status != WorkflowStatus.FINISHED or run.verdict not in (FinalVerdict.FAIL, FinalVerdict.UNVERIFIED)
                or run.code_version is None):
            return False
        candidate = _candidate(connection, run)
        if candidate is None:
            return False
        _record(connection, run, stage='VERDICT', reason=reason_code, step=None, task_id=None, source=candidate[0])
        return True


def stop_record(connection, run_id):
    """The automatic stop of a Run as a dict, or None (the table may not exist in older stores)."""
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='automatic_stop_records'"
                              ).fetchone():
        return None
    row = connection.execute('SELECT event_id,stage,reason_code,workflow_step_id,a2a_task_id,code_version,verdict,'
                             'policy FROM automatic_stop_records WHERE run_id=?', (str(run_id),)).fetchone()
    if row is None:
        return None
    keys = ('eventId', 'stage', 'reasonCode', 'workflowStepId', 'a2aTaskId', 'retainedCodeVersion', 'verdict',
            'policy')
    return dict(zip(keys, row))
