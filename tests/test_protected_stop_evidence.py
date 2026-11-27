"""Only the approved pre-write correction and an observed product assertion can authorize repair."""
from contextlib import nullcontext
import json
import sqlite3
import unittest
from types import SimpleNamespace
from uuid import uuid4

from orchestrator.domain import AgentRole
from orchestrator.domain.issues import ProtectedReceipt
from orchestrator.infrastructure.protected_stop import blocked_test_write


class ProtectedStopProofTests(unittest.TestCase):
    def setUp(self):
        self.c = sqlite3.connect(':memory:')
        self.addCleanup(self.c.close)
        self.c.execute("CREATE TABLE trace_events(event_id text,event_type text,run_id text)")
        self.c.execute("CREATE TABLE agent_runtime_events(sequence integer,trace_event_id text,run_id text,"
                       "workflow_step_id text,payload_json text)")
        self.run = SimpleNamespace(run_id=uuid4())
        self.step = SimpleNamespace(workflow_step_id=uuid4(), agent_context_id='context', a2a_task_id='task',
            agent_role=AgentRole.SECURITY, attempt=0, requirement_ids=[uuid4()])
        self.source = SimpleNamespace(artifact_id=uuid4(), code_version=2, snapshot_sha256='1'*64)
        self.repository = SimpleNamespace(_transaction=lambda: nullcontext(self.c))
        self.call = {'kind':'DECISION', 'policy':'BOUNDED_PRE_EXECUTION_REGENERATION_V1','operationSequence':2}
        self.binding = {'runId':str(self.run.run_id),'workflowStepId':str(self.step.workflow_step_id),
            'a2aTaskId':'task','agentContextId':'context','attempt':0,'codeVersion':2,'snapshotSha256':'1'*64,
            'inputArtifactIds':[str(self.source.artifact_id)],'requirementIds':[str(self.step.requirement_ids[0])]}

    def event(self, event, detail, metered=False):
        idx = self.c.execute('select count(*) from agent_runtime_events').fetchone()[0]+1
        rid = str(self.run.run_id)
        binding = {'role':'SINGLE_AGENT'} if metered else {'actor':'SINGLE_AGENT','eventType':event}
        self.c.execute('insert into trace_events values(?,?,?)', (str(idx),event,rid))
        self.c.execute('insert into agent_runtime_events values(?,?,?,?,?)',
            (idx,str(idx),rid,str(self.step.workflow_step_id),json.dumps({'binding':{**self.binding,**binding},'detail':detail})))

    def attempts(self, rule='CREDENTIAL_ASSIGNMENT', tool='write_test_file'):
        for seq in (2,3):
            self.event('LLM_MODEL_CALLED', {'sequence':seq}, metered=True)
            self.event('LLM_POLICY_REJECTED', {'purpose':'LLM_TOOL_POLICY_VALIDATION','llmValidation':{
                'sequence':seq,'stage':'TOOL_POLICY','boundary':'TOOL_ARGUMENTS','fieldPath':'/content',
                'reasonCode':'SOURCE_CREDENTIAL_DENIED','sourceRule':rule,'toolName':tool}})
            recovery = ({'action':'REGENERATE','code':'SOURCE_CREDENTIAL_DENIED','reason':'ALLOWED'} if seq==2 else
                {'action':'STOP','category':'POLICY_BLOCK','code':'LLM_TOOL_POLICY_VIOLATION','reason':'NOT_SAFE_TO_RETRY'})
            self.event('LLM_RECOVERY_DECIDED', {'purpose':'LLM_RECOVERY','recovery':{**self.call,**recovery,'sequence':seq}})

    def matches(self):
        return blocked_test_write(self.repository,self.run,self.step,self.source)

    def test_rejected_assignment(self):
        self.attempts()
        self.assertTrue(self.matches())
        self.source.code_version=3
        self.assertFalse(self.matches())

    def test_registered_secret_is_not_recoverable(self):
        self.attempts(rule='REGISTERED_SECRET')
        self.assertFalse(self.matches())

    def test_other_tool_or_stale_proof_is_not_recoverable(self):
        self.attempts(tool='write_source_file')
        self.assertFalse(self.matches())
        self.attempts()
        self.event('LLM_MODEL_CALLED',{'sequence':4},metered=True)
        self.assertFalse(self.matches())

    def test_setup_only_failure_is_not_a_product_failure(self):
        args = dict(receipt_id=uuid4(),workflow_step_id=uuid4(),test_id='protected:test',test_version='practice:'+'b'*64)
        row = {'caseId':'pad','itemId':'http_status','actual':400,'status':'FAIL','reason':'EXPECTATION_MISMATCH'}
        with self.assertRaises(ValueError):
            ProtectedReceipt(**args, observations_json=json.dumps([{**row,'kind':'precondition'}]))
        good = ProtectedReceipt(**args, observations_json=json.dumps([{**row,'kind':'assertion'}]))
        self.assertEqual(json.loads(good.observations_json)[0]['actual'],400)
