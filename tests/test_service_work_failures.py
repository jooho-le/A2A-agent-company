"""Closed recovery facts and matched Task context, never raw failures or LLM I/O."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict
import pytest

from orchestrator.application.work_failures import read_work_failures
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStep, WorkflowStepStatus, WorkflowStatus
from orchestrator.infrastructure import SQLiteWorkflowRepository


def remote_task(context, status):
    task = ParseDict({'id':'current-task','contextId':context,'status':{'state':'TASK_STATE_FAILED',
        'message':{'parts':[{'data':{'code':'LLM_PROVIDER_ERROR','llmFailure':{
            'httpStatus':status,'privateMessage':'private-provider-body'}}}]}}},Task())
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self,*args): pass
        async def get_task(self,_task_id): return task
    return SimpleNamespace(registry=SimpleNamespace(require_base_url=lambda role:'http://test'),
                           client_factory=lambda url:Client())


@pytest.mark.parametrize('status',[500,429,401])
def test_provider_failure_uses_only_matched_task_and_closed_http_status(status):
    run = WorkflowRun(scenario_id=uuid4(),request_text='API 요청',
        status=WorkflowStatus.HUMAN_REVIEW,resume_state=WorkflowStatus.VALIDATING)
    step = WorkflowStep(run_id=run.run_id,agent_role=AgentRole.QA,status=WorkflowStepStatus.FAILED,
        a2a_task_id='current-task',agent_context_id='current-context',failure_code='LLM_PROVIDER_ERROR')
    data = asyncio.run(read_work_failures(None,remote_task('current-context',status),run,[step]))[0]
    assert data['httpStatus'] == status
    assert str(status) in data['message']
    assert 'private-provider-body' not in json.dumps(data)
    mismatch = asyncio.run(read_work_failures(None,remote_task('another-context',status),run,[step]))[0]
    assert 'httpStatus' not in mismatch


def test_tool_failure_uses_the_current_step_and_allowlisted_metadata():
    with TemporaryDirectory() as directory:
        repository = SQLiteWorkflowRepository(Path(directory)/'workflow.db')
        run = WorkflowRun(scenario_id=uuid4(),request_text='API 요청',
            status=WorkflowStatus.HUMAN_REVIEW,resume_state=WorkflowStatus.VALIDATING)
        step = WorkflowStep(run_id=run.run_id,agent_role=AgentRole.QA,status=WorkflowStepStatus.FAILED,
                            failure_code='LLM_TOOL_EXECUTION_FAILED')
        with repository._transaction() as db:
            db.execute('CREATE TABLE agent_runtime_events (sequence INTEGER PRIMARY KEY,run_id TEXT,workflow_step_id TEXT,kind TEXT,payload_json TEXT)')
            db.execute('INSERT INTO agent_runtime_events VALUES(1,?,?,?,?)',
                (str(run.run_id),str(step.workflow_step_id),'MCP',json.dumps({'detail':{
                    'toolName':'read_test_report','errorKind':'PATH_TRAVERSAL','rawBody':'private-tool-body'}})))
            db.execute('INSERT INTO agent_runtime_events VALUES(2,?,?,?,?)',
                (str(run.run_id),str(uuid4()),'MCP',json.dumps({'detail':{'toolName':'run_build','errorKind':'TIMEOUT'}})))
        data = asyncio.run(read_work_failures(repository,None,run,[step]))[0]
        assert data['toolName']=='read_test_report' and data['toolError']=='PATH_TRAVERSAL'
        assert 'private-tool-body' not in json.dumps(data)
        later = step.model_copy(update={'workflow_step_id':uuid4(),'status':WorkflowStepStatus.RUNNING,
            'created_at':datetime.now(timezone.utc)+timedelta(seconds=1)})
        assert asyncio.run(read_work_failures(repository,None,run,[step,later])) == []
