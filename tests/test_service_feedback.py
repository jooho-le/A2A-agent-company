"""Real authenticated Host routes, SQLite receipts and worker input; no AI/I/O."""
import asyncio
import json
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
from pydantic import SecretStr

from orchestrator.api.dependencies import get_workflow_controls
from orchestrator.application.run_interactions import interaction_revision
from orchestrator.core.config import Settings
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.run_configuration import RunConfiguration, RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, service_request_contract
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.run_feedback import feedback_input, read_feedback
from orchestrator.main import create_app
from agents.platform.host_policy_store import HostPolicyStore


class ServiceFeedbackTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(temporary.name)/'host.db')
        self.run = WorkflowRun(scenario_id=SERVICE_WEB_ID, request_text='회원가입 API 만들어', status=WorkflowStatus.IMPLEMENTING)
        self.step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.DEVELOPER,
                                 status=WorkflowStepStatus.RUNNING, code_version=1)
        self.configuration = RunConfigurationArtifact(run_id=self.run.run_id, workspace_id=self.run.workspace_id,
            scenario_id=SERVICE_WEB_ID, configuration=RunConfiguration(),
            frozen_scenario_contract_json=json.dumps(service_request_contract(self.run.request_text),ensure_ascii=False))
        self.repository.create_run(self.run, (self.step,), (), run_configuration=self.configuration)
        self.controls = SimpleNamespace(resume=AsyncMock(), cancel=AsyncMock())
        self.app = create_app(self.repository,settings=Settings(_env_file=None,database_path=str(self.repository.database_path)))
        self.app.state.host_api_auth_required = True
        self.app.state.host_api_token = SecretStr('test-feedback-service-token-'+'a'*40)
        self.app.dependency_overrides[get_workflow_controls] = lambda: self.controls
        self.path = f'/api/v1/runs/{self.run.run_id}'

    def request(self, method, suffix, body=None, key=None, auth=True):
        async def send():
            headers = {'Authorization': 'Bearer '+self.app.state.host_api_token.get_secret_value()} if auth else {}
            if key: headers['Idempotency-Key'] = str(key)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test') as client:
                return await client.request(method,self.path+suffix,json=body,headers=headers)
        return asyncio.run(send())

    def body(self):
        return {'action':'ANNOTATE','revision':interaction_revision(self.run,[self.step]),
                'clarification':'요청한 API는 JSON으로 사용하려고 해요.'}

    def test_feedback_is_persisted_once_and_receipted_without_starting_work(self):
        key = uuid4(); body = self.body()
        self.assertEqual(self.request('POST','/commands',body,key,auth=False).status_code,401)
        self.assertEqual(self.request('POST','/commands',body,key).status_code,202)
        self.assertEqual(self.request('POST','/commands',body,key).status_code,200)
        receipt = self.request('GET',f'/commands/{key}').json()
        self.assertEqual((receipt['action'],receipt['status']),('ANNOTATE','SUCCEEDED'))
        interaction = self.request('GET','/interaction').json()
        self.assertTrue(interaction['canAnnotate'])
        self.assertEqual(len(interaction['feedback']),1)
        self.assertEqual(interaction['feedback'][0]['status'],'QUEUED')
        self.assertEqual(self.request('POST','/commands',{**body,'clarification':'다른 내용'},key).status_code,409)
        self.controls.resume.assert_not_awaited(); self.controls.cancel.assert_not_awaited()
        self.assertEqual(self.repository.get_run(self.run.run_id),self.run)

    def test_worker_receives_notes_without_mutating_frozen_criteria_or_budget(self):
        self.request('POST','/commands',self.body(),uuid4())
        services = SimpleNamespace(_repository=self.repository,configuration=SimpleNamespace(binding=SimpleNamespace(role=AgentRole.DEVELOPER)))
        execution = SimpleNamespace(configuration=self.configuration,metadata=SimpleNamespace(
            run_id=self.run.run_id,workflow_step_id=self.step.workflow_step_id))
        for _ in range(2):
            inputs = feedback_input(services,execution)
            self.assertEqual(inputs['userFeedback']['notes'][0]['text'],self.body()['clarification'])
        self.assertEqual(read_feedback(self.repository,self.run.run_id)[0]['status'],'INCLUDED')
        self.assertEqual(self.repository.get_run_configuration(self.run.run_id),self.configuration)
        with self.repository._connection() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM run_user_feedback_deliveries').fetchone()[0],1)

    def test_paused_or_finished_work_does_not_silently_queue_undeliverable_input(self):
        paused = self.run.model_copy(update={'status':WorkflowStatus.HUMAN_REVIEW,'resume_state':WorkflowStatus.IMPLEMENTING})
        self.repository.save_run_update(paused,())
        self.assertEqual(self.request('POST','/commands',self.body(),uuid4()).status_code,409)
        self.assertFalse(self.request('GET','/interaction').json()['canAnnotate'])
        self.assertEqual(read_feedback(self.repository,self.run.run_id),[])

    def test_feedback_cannot_replace_contract_and_is_bounded(self):
        body = self.body()
        self.assertEqual(self.request('POST','/commands',{**body,'serviceDesign':{}},uuid4()).status_code,422)
        self.assertEqual(self.request('POST','/commands',{**body,'clarification':'x'*4001},uuid4()).status_code,422)
        self.assertEqual(self.request('POST','/commands',{**body,'clarification':'   '},uuid4()).status_code,422)

    def test_old_policy_cannot_accept_new_feedback(self):
        with self.repository._transaction() as db:
            HostPolicyStore._schema(db)
            db.execute('INSERT INTO run_host_policies VALUES(?,?)',(str(self.run.run_id),'a'*64))
        self.app.state.host_policy_sha256 = 'b'*64
        data = self.request('GET','/interaction').json()
        self.assertFalse(data['canAnnotate'])
        self.assertIn('이전 실행 규칙',data['continuationUnavailable'])
        self.assertEqual(self.request('POST','/commands',self.body(),uuid4()).status_code,409)
        self.assertEqual(read_feedback(self.repository,self.run.run_id),[])

    def test_feedback_limit_immutability_and_undelivered_end_are_explicit(self):
        for _ in range(16):
            self.assertEqual(self.request('POST','/commands',self.body(),uuid4()).status_code,202)
        self.assertEqual(self.request('POST','/commands',self.body(),uuid4()).status_code,409)
        self.assertFalse(self.request('GET','/interaction').json()['canAnnotate'])
        with self.assertRaises(sqlite3.IntegrityError), self.repository._transaction() as db:
            db.execute('UPDATE run_user_feedback SET text=?',('changed',))
        ended = self.run.model_copy(update={'status':WorkflowStatus.ABORTED,'termination_reason':'테스트에서 중단'})
        self.repository.save_run_update(ended,())
        self.assertTrue(all(row['status']=='NOT_APPLIED' for row in read_feedback(self.repository,self.run.run_id)))

    def test_feedback_is_not_delivered_to_another_run_or_role(self):
        self.request('POST','/commands',self.body(),uuid4())
        services = SimpleNamespace(_repository=self.repository,configuration=SimpleNamespace(binding=SimpleNamespace(role=AgentRole.QA)))
        execution = SimpleNamespace(configuration=self.configuration,metadata=SimpleNamespace(
            run_id=self.run.run_id,workflow_step_id=self.step.workflow_step_id))
        with self.assertRaisesRegex(ValueError,'FEEDBACK_STEP_INVALID'):
            feedback_input(services,execution)
        self.assertEqual(read_feedback(self.repository,self.run.run_id)[0]['status'],'QUEUED')
        execution.metadata.run_id = uuid4()
        self.assertEqual(feedback_input(services,execution),{})
