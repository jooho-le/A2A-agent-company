"""Public, read-only questions bound to the current Run/Step/Task."""
import asyncio
from hashlib import sha256
import json

from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from orchestrator.core.security import redact_text
from orchestrator.domain import A2ATaskState, WorkflowStatus, WorkflowStepStatus


def interaction_revision(run, steps):
    data = [str(run.run_id), run.status.value, run.updated_at.isoformat(), [
        [str(s.workflow_step_id), s.status.value, s.attempt, s.code_version,
         s.a2a_task_id, None if s.a2a_task_state is None else s.a2a_task_state.value]
        for s in sorted(steps, key=lambda s: str(s.workflow_step_id))]]
    return sha256(json.dumps(data, separators=(",", ":")).encode()).hexdigest()


def public_question(message):
    """Only structured questions, never history, tool output, or raw review data."""
    questions, code, policy = [], None, None
    for part in MessageToDict(message).get("parts", []):
        data = part.get("data")
        if not isinstance(data, dict):
            continue
        for question in data.get("questions", []) if isinstance(data.get("questions"), list) else []:
            if isinstance(question, str) and question.strip():
                value = redact_text(question).strip()[:2000]
                if value not in questions:
                    questions.append(value)
        if data.get("code") == "SEMANTIC_REVIEW_INPUT_REQUIRED":
            review = data.get("semanticReview", {})
            if isinstance(review, dict):
                code = review.get("reasonCode")
                policy = review.get("resumePolicy")
    semantic = code == "SEMANTIC_EVIDENCE_UNVERIFIED"
    reason = ("기획·작업 내용의 근거를 확인하지 못해 검토를 기다리고 있어요." if semantic else
              "직원이 확인할 내용을 남겼어요.")
    if semantic:
        # The generic model question must not imply that user input was missing.
        questions = ["확정된 요구사항 안에서 작업하도록 추가 설명을 남겨주세요. 근거 검증은 다시 진행됩니다."]
    return {"questions": questions[:8], "reason": reason,
            "replyMode": "NEW_RUN" if policy == "NEW_RUN_REQUIRED" else "ANSWER"}


async def read_interactions(repository, controls, run_id, *, expected_policy_sha256=None):
    run = repository.get_run(run_id)
    if run is None:
        return None
    steps = repository.list_steps(run_id)

    async def read(step):
        base = {"workflowStepId": str(step.workflow_step_id), "agentRole": step.agent_role.value,
                "attempt": step.attempt, "codeVersion": step.code_version,
                "questions": [], "replyMode": "UNAVAILABLE", "reason": "확인 내용을 불러오지 못했어요. 다시 확인해주세요."}
        if step.a2a_task_state == A2ATaskState.AUTH_REQUIRED:
            return {**base, "replyMode": "OPERATOR", "reason": "실행 환경의 인증 확인이 필요해요. 관리자에게 알려주세요."}
        if not step.a2a_task_id or step.a2a_task_state != A2ATaskState.INPUT_REQUIRED:
            return base
        try:
            url = controls.registry.require_base_url(step.agent_role)
            async with controls.client_factory(url) as client:
                task = await asyncio.wait_for(client.get_task(step.a2a_task_id), timeout=4)
            if (task.id != step.a2a_task_id or task.context_id != step.agent_context_id
                    or TaskState.Name(task.status.state) != A2ATaskState.INPUT_REQUIRED.value):
                return base
            public = public_question(task.status.message)
            if not public["questions"]:
                return {**base, "reason": "직원이 확인을 요청했지만 질문 내용을 확인할 수 없어요."}
            return {**base, **public}
        except Exception:
            return base

    paused = (WorkflowStatus.WAITING_INPUT, WorkflowStatus.HUMAN_REVIEW)
    # A Task may retain its historical question after an unattended Run ends.
    # It cannot authorize a user command or advertise an answerable question.
    waiting = [s for s in steps if run.status in paused
               and s.status == WorkflowStepStatus.WAITING_INPUT]
    requests = await asyncio.gather(*(read(s) for s in waiting))
    current = repository.get_run(run_id)
    latest = repository.list_steps(run_id)
    revision = interaction_revision(current, latest)
    if current.status not in paused or revision != interaction_revision(run, steps):
        requests = []  # A question from a previous attempt must never accept input.
    continuation_unavailable = None
    if expected_policy_sha256 is not None:
        try:
            with repository._connection() as db:
                has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='run_host_policies'").fetchone()
                pinned = None if has_table is None else db.execute(
                    'SELECT policy_sha256 FROM run_host_policies WHERE run_id=?', (str(run_id),)).fetchone()
            started = (current.status not in (WorkflowStatus.RECEIVED, WorkflowStatus.PLANNING)
                       or any(step.a2a_task_id for step in latest))
            if pinned is None and started:
                continuation_unavailable = '이 작업의 승인된 실행 규칙 기록이 없어 이어갈 수 없어요. 새 작업을 시작해주세요.'
            elif pinned is not None and pinned[0] != expected_policy_sha256:
                continuation_unavailable = '이전 실행 규칙으로 시작된 작업이라 현재 서버에서 이어갈 수 없어요. 새 작업을 시작해주세요.'
        except Exception:
            continuation_unavailable = '기존 작업의 실행 규칙을 확인하지 못했어요. 실행 환경 확인이 필요해요.'
    if continuation_unavailable:
        requests = [{**question, 'replyMode': 'NEW_RUN', 'reason': continuation_unavailable,
                     'questions': []} for question in requests]
    from orchestrator.infrastructure.run_feedback import read_feedback
    from orchestrator.application.work_failures import read_work_failures
    from orchestrator.domain.service_design import service_policy
    from orchestrator.infrastructure.service_continuations import public_continuation
    configuration = repository.get_run_configuration(run_id)
    feedback = read_feedback(repository, run_id)
    can_annotate = (continuation_unavailable is None and len(feedback) < 16
                    and configuration is not None and service_policy(configuration.scenario_contract)
                    and current.status not in (*paused, WorkflowStatus.FINISHED, WorkflowStatus.ABORTED))
    continuation = public_continuation(repository, current)
    if continuation_unavailable and continuation and continuation['available']:
        continuation_unavailable = '이전 실행 규칙의 작업은 그대로 재개할 수 없어요. 아래 이어서 수정·재시도로 코드와 요구사항을 이어받아주세요.'
        requests = [{**question, 'reason': continuation_unavailable} for question in requests]
    failures = await read_work_failures(repository, controls, current, latest)
    if continuation and continuation['available']:
        for failure in failures:
            if not failure['recovering']:
                failure['instruction'] = '이어서 수정·재시도를 선택하면 기존 코드·요구사항·실패 기록을 연결해 다시 진행해요. 기존 기록은 보존돼요.'
    return {"runId": str(run_id), "revision": revision, "status": current.status.value,
            'continuation': continuation,
            "canAnnotate": can_annotate, "feedback": feedback,
            "continuationUnavailable": continuation_unavailable,
            "failures": failures,
            "requestText": redact_text(current.request_text)[:20000], "requests": requests,
            "canCancel": current.status not in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED)}
