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


async def read_interactions(repository, controls, run_id):
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

    waiting = [s for s in steps if s.status == WorkflowStepStatus.WAITING_INPUT]
    requests = await asyncio.gather(*(read(s) for s in waiting))
    current = repository.get_run(run_id)
    latest = repository.list_steps(run_id)
    revision = interaction_revision(current, latest)
    if revision != interaction_revision(run, steps):
        requests = []  # A question from a previous attempt must never accept input.
    return {"runId": str(run_id), "revision": revision, "status": current.status.value,
            "requestText": redact_text(current.request_text)[:20000], "requests": requests,
            "canCancel": current.status not in (WorkflowStatus.FINISHED, WorkflowStatus.ABORTED)}
