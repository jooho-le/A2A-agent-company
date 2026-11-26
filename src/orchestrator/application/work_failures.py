"""Closed, actionable failure descriptions. No exception bodies or raw logs."""
import asyncio
import json

from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

_CAUSES = {
    'LLM_PROVIDER_ERROR': ('AI 요청에 실패했어요.', 'PROVIDER'),
    'LLM_AUTH_REQUIRED': ('AI 인증이나 이용 권한을 확인해야 해요.', 'OPERATOR'),
    'LLM_CONFIGURATION_ERROR': ('승인된 실행 설정을 확인해야 해요.', 'OPERATOR'),
    'LLM_TOOL_EXECUTION_FAILED': ('작업 도구 실행 중 문제가 생겼어요.', 'OPERATOR'),
    'LLM_BUDGET_EXHAUSTED': ('이 작업의 시간·호출 예산을 모두 사용했어요.', 'LIMIT'),
    'LLM_EXECUTION_TIMEOUT': ('작업의 제한 시간을 초과했어요.', 'OPERATOR'),
    'QA_TEST_UNVERIFIED': ('검사 결과를 확인할 수 없어 통과로 처리하지 않았어요.', 'OPERATOR'),
    'LLM_SOURCE_REPAIR_EXHAUSTED': ('필수 구현 문제를 허용된 수정 횟수 안에 해결하지 못했어요.', 'DEVELOPER'),
    'LLM_VALIDATION_DRAFT_EXHAUSTED': ('검사 준비 또는 결과서 작성에 문제가 생겼어요.', 'VALIDATOR'),
    'LLM_TOOL_POLICY_VIOLATION': ('허용되지 않은 도구 입력이 차단됐어요.', 'OPERATOR'),
    'DEVELOPER_CONTEXT_DENIED': ('개발 작업의 실행 정보가 승인 조건과 일치하지 않아요.', 'OPERATOR'),
    'DEVELOPER_INPUT_INVALID': ('개발 요청을 실행 규격에 맞게 읽지 못했어요.', 'OPERATOR'),
    'DEVELOPER_SERVICES_DENIED': ('개발 도구의 실행 설정을 준비하지 못했어요.', 'OPERATOR'),
}
_TOOL_MESSAGES = {
    'TEST_RUNNER_ERROR': '검사 실행기가 유효한 결과 보고서를 만들지 못했어요.',
    'PATH_TRAVERSAL': '도구가 요청한 참조 경로를 거절했어요. 없는 보고서나 잘못된 참조인지 확인해야 해요.',
    'FILE_NOT_FOUND': '요청한 파일이 현재 작업물에 없어요.',
    'REPORT_NOT_FOUND': '요청한 검사 보고서가 만들어지지 않았어요.',
    'TIMEOUT': '검사 도구의 제한 시간을 초과했어요.',
}
_TOOLS = {'read_project_file', 'write_test_file', 'run_unit_tests', 'run_browser_tests',
          'read_test_report', 'run_security_scan', 'read_security_report', 'run_build'}


async def read_work_failures(repository, controls, run, steps):
    active = run.status.value in {'RECEIVED', 'PLANNING', 'IMPLEMENTING', 'SNAPSHOT_READY',
        'VALIDATING', 'FIX_REQUIRED', 'FIXING', 'REVALIDATING'}
    waiting_during_work = run.status.value == 'WAITING_INPUT' and getattr(run.resume_state, 'value', None) in {
        'PLANNING', 'IMPLEMENTING', 'FIXING', 'VALIDATING', 'REVALIDATING'}
    active = active or waiting_during_work
    async def read(step):
        code = step.failure_code or 'AGENT_EXECUTION_FAILED'
        message, owner = _CAUSES.get(code, ('직원 실행에 실패했어요. 실행 기록 확인이 필요해요.', 'OPERATOR'))
        # Refine old generic records from the exact terminal Task, without
        # rewriting Step history or trusting arbitrary peer-provided messages.
        if code == 'AGENT_EXECUTION_FAILED' and step.agent_role.value == 'DEVELOPER' and step.a2a_task_id:
            try:
                url = controls.registry.require_base_url(step.agent_role)
                async with controls.client_factory(url) as client:
                    task = await asyncio.wait_for(client.get_task(step.a2a_task_id), 4)
                if (task.id == step.a2a_task_id and task.context_id == step.agent_context_id
                        and task.status.state in (TaskState.TASK_STATE_FAILED, TaskState.TASK_STATE_REJECTED)):
                    parts = MessageToDict(task.status.message).get('parts', [])
                    detail_code = parts[0].get('data', {}).get('code') if len(parts) == 1 else None
                    if detail_code in {'DEVELOPER_CONTEXT_DENIED', 'DEVELOPER_INPUT_INVALID', 'DEVELOPER_SERVICES_DENIED'}:
                        code = detail_code
                        message, owner = _CAUSES[code]
            except Exception:
                pass
        result = {'workflowStepId': str(step.workflow_step_id), 'agentRole': step.agent_role.value,
                  'code': code, 'message': message, 'recoveryOwner': owner,
                  'blocking': not active, 'recovering': active,
                  'nextAction': 'REVIEW_OR_NEW_RUN',
                  'instruction': '이 단계는 이미 실패해서 답변만으로 재개할 수 없어요. 만든 결과와 원인을 확인한 뒤 새 작업으로 시작해주세요.'}
        if code in ('LLM_PROVIDER_ERROR', 'LLM_AUTH_REQUIRED') and repository is not None:
            # Prefer durable facts: the Agent can be offline after a failure.
            try:
                with repository._connection() as db:
                    stored = db.execute('''SELECT payload_json FROM agent_runtime_events
                        WHERE run_id=? AND workflow_step_id=? ORDER BY sequence DESC LIMIT 64''',
                        (str(run.run_id), str(step.workflow_step_id))).fetchall()
                retries = 0
                for record in stored:
                    detail = json.loads(record[0]).get('detail', {})
                    recovery = detail.get('recovery', {})
                    retries += recovery.get('code') == 'PROVIDER_TRANSIENT_RETRY'
                    diagnostic = detail.get('failureDiagnostic') or recovery.get('failureDiagnostic', {})
                    status = diagnostic.get('httpStatus') if isinstance(diagnostic, dict) else None
                    if 'httpStatus' not in result and type(status) is int and 400 <= status <= 599:
                        result['httpStatus'] = status
                result['providerRetriesUsed'] = retries
            except Exception:
                pass
        if code in ('LLM_PROVIDER_ERROR', 'LLM_AUTH_REQUIRED') and step.a2a_task_id and 'httpStatus' not in result:
            try:
                url = controls.registry.require_base_url(step.agent_role)
                async with controls.client_factory(url) as client:
                    task = await asyncio.wait_for(client.get_task(step.a2a_task_id), 4)
                if task.id == step.a2a_task_id and task.context_id == step.agent_context_id and task.status.state == TaskState.TASK_STATE_FAILED:
                    parts = MessageToDict(task.status.message).get('parts', [])
                    data = parts[0].get('data', {}) if len(parts) == 1 else {}
                    failure = data.get('llmFailure', {}) if data.get('code') == code else {}
                    status = failure.get('httpStatus')
                    if type(status) in (int, float) and int(status) == status and 400 <= status <= 599:
                        result['httpStatus'] = int(status)
                        if status >= 500:
                            result['message'] = f'AI 제공자가 서버 오류({int(status)})를 반환했어요.'
                        elif status == 429:
                            result['message'] = 'AI 제공자가 요청 한도 또는 사용량 문제로 요청을 거절했어요(429).'
                        elif status in (401, 403):
                            result['message'] = f'AI 인증 또는 접근 권한 확인이 필요해요({int(status)}).'
            except Exception:
                pass
        status = result.get('httpStatus')
        if code == 'LLM_PROVIDER_ERROR':
            result.update(nextAction='CHECK_PROVIDER', productFailure=False, workPreserved=True,
                instruction='AI 제공자 오류로 중단됐어요. 이전 작업물과 기록은 보존됐어요. 제공자 연결·사용량을 확인해주세요. 이미 실패한 요청을 답변으로 재개하거나 자동 반복하지 않아요.')
            if isinstance(status, int) and status >= 500:
                result['message'] = f'AI 제공자가 서버 오류({status})를 반환했어요.'
        if code == 'LLM_VALIDATION_DRAFT_EXHAUSTED' and repository is not None:
            try:
                with repository._connection() as db:
                    stored = db.execute('''SELECT payload_json FROM agent_runtime_events
                        WHERE run_id=? AND workflow_step_id=? ORDER BY sequence DESC LIMIT 32''',
                        (str(run.run_id), str(step.workflow_step_id))).fetchall()
                from agents.llm.validation import LLMValidationReport
                for record in stored:
                    detail = json.loads(record[0]).get('detail', {})
                    validation = detail.get('llmValidation') or detail.get('recovery', {}).get('llmValidation')
                    if validation:
                        validation = {k: v for k, v in validation.items() if k != 'schemaVersion'}
                        validation['issues'] = [{k: v for k, v in row.items() if k != 'reason'}
                                                for row in validation.get('issues', [])]
                        result['validationIssues'] = LLMValidationReport.model_validate(
                            validation).public()['issues']
                        break
            except Exception:
                pass
        if code == 'LLM_TOOL_EXECUTION_FAILED':
            try:
                with repository._connection() as db:
                    rows = db.execute('''SELECT payload_json FROM agent_runtime_events WHERE run_id=?
                        AND workflow_step_id=? AND kind='MCP' ORDER BY sequence DESC LIMIT 16''',
                        (str(run.run_id), str(step.workflow_step_id))).fetchall()
                for row in rows:
                    detail = json.loads(row[0]).get('detail', {})
                    tool, error = detail.get('toolName'), detail.get('errorKind')
                    if tool in _TOOLS and error in _TOOL_MESSAGES:
                        result.update(toolName=tool, toolError=error, message=_TOOL_MESSAGES[error])
                        break
            except Exception:
                pass
        if active:
            result.update(nextAction='WAIT_FOR_REVALIDATION',
                instruction=('현재 작업의 다른 직원이 확인을 기다려요. 확인 요청에 답변하고 이 작업에서 진행 상황을 확인해주세요.'
                    if waiting_during_work else '앞선 단계의 실패를 바탕으로 자동 수정·재검사를 진행 중이에요. 현재 작업에서 진행 상황을 확인해주세요.'))
        return result

    latest = {}
    for step in sorted(steps, key=lambda row: (row.created_at, str(row.workflow_step_id))):
        latest[step.agent_role] = step
    return await asyncio.gather(*(read(step) for step in latest.values() if step.status.value == 'FAILED'))
