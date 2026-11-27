"""Durable A2A/Host schema diagnostics across roles; synthetic model and Docker."""
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch
from uuid import UUID

from a2a.server.context import ServerCallContext
from google.protobuf.json_format import MessageToDict

from agents.core.identity import SingleAgentIdentity
from agents.llm.contracts import json_text, ToolCall
from test_llm_runtime import tool_response
from agents.platform.telemetry_store import AgentTelemetryStore
from orchestrator.domain import AgentRole, WorkflowStatus
import test_owned_agent_pipeline as multi_fixture
import test_single_agent_pipeline as single_fixture


def malformed(original):
    def response(request):
        result = original(request) if callable(original) else original
        value = json.loads(result.output_text)
        value['private-property-do-not-log'] = 'private-payload-do-not-log'
        return replace(result, output_text=json_text(value))
    return response


class LLMSchemaPublicationTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, single=False):
        f = (single_fixture.SingleAgentPipelineTests if single else multi_fixture.OwnedAgentPipelineTests)('runTest')
        f.setUp()
        for fn, args, kwargs in f._cleanups: self.addCleanup(fn, *args, **kwargs)
        f._cleanups.clear()
        return f

    async def assert_failure(self, base, platform, client, role, *, single=False, recorded=True, policy=False):
        response = await asyncio.wait_for(client.post('/api/v1/runs', json=base.submission()), 25)
        self.assertEqual(response.status_code,201,response.text)
        run_id = UUID(response.json()['run']['runId'])
        # First Developer format exhaustion has no frozen candidate and closes
        # ABORTED (05 section 14). This legacy fixture has no measured Security
        # test proof, so validation roles still hold. Preserve diagnostics in both.
        expected = (WorkflowStatus.ABORTED if role is AgentRole.DEVELOPER and not policy
                    else WorkflowStatus.HUMAN_REVIEW)
        run = base.repository.get_run(run_id)
        self.assertIs(run.status, expected, role.value)
        if expected is WorkflowStatus.FINISHED:
            self.assertEqual(run.verdict.value, 'UNVERIFIED')
        step = next(s for s in base.repository.list_steps(run_id) if s.agent_role is role)
        actor=SingleAgentIdentity.SINGLE_AGENT if single else role
        store=platform.agent_apps[actor].state.task_store
        task=(await store.get(step.a2a_task_id,ServerCallContext(state={}))).task
        payload=MessageToDict(task.status.message.parts[0].data)
        self.assertEqual(payload['code'], 'LLM_TOOL_POLICY_VIOLATION' if policy else
                         'LLM_VALIDATION_DRAFT_EXHAUSTED' if role is AgentRole.QA else 'LLM_SCHEMA_INVALID')
        report=payload['llmValidation']
        self.assertEqual(report['stage'],'TOOL_POLICY' if policy else 'MODEL_OUTPUT')
        if policy:
            self.assertEqual(report['reasonCode'], 'TOOL_NOT_OFFERED')
        else:
            self.assertEqual(report['issues'][0]['reasonCode'],'UNEXPECTED_FIELD')
            self.assertEqual(report['issues'][0]['fieldPath'],'')
        self.assertIs(report['traceRecorded'],recorded)
        self.assertNotIn('private-',json.dumps(payload))
        self.assertFalse(task.artifacts)
        events,_=platform.telemetry_store.list_events(run_id,limit=500)
        event_type='LLM_POLICY_REJECTED' if policy else 'LLM_SCHEMA_REJECTED'
        diagnostics=[e for e in events if e['binding'].get('eventType')==event_type]
        self.assertEqual(len(diagnostics),int(recorded))
        if recorded:
            event=diagnostics[0]
            self.assertEqual(event['llmValidation']['sequence'],report['sequence'])
            self.assertEqual(event['binding']['workflowStepId'],str(step.workflow_step_id))
            self.assertEqual(event['binding']['actor'],actor.value)
            self.assertEqual(event['recordType'],'EVENT')
            self.assertNotIn('private-',json.dumps(event))
        # Details survive the Task's SQLite revision storage, independent of the
        # optional Host trace sink; no model response is needed to reconstruct it.
        revision=(await store.revisions(step.a2a_task_id))[-1]
        self.assertEqual(MessageToDict(revision.status.message.parts[0].data)['llmValidation'],report)
        usage=(await client.get(f'/api/v1/runs/{run_id}/usage')).json()
        self.assertEqual(usage['summary']['modelCalls'],len([e for e in events if e.get('recordType')=='USAGE' and e['kind']=='LLM']))
        return run_id

    async def test_all_multi_roles_publish_bound_value_free_diagnostics(self):
        for role in AgentRole:
            with self.subTest(role=role):
                f=self.fixture()
                providers=f.providers()
                providers[role].script[-1]=malformed(providers[role].script[-1])
                async with f.running_platform(providers=providers) as (client,_):
                    await self.assert_failure(f,f.platform,client,role)

    async def test_single_developer_uses_same_report_without_new_agent_identity(self):
        f=self.fixture(single=True)
        provider=f.provider()
        # Initial sequence: Planner draft, Developer write, Developer draft.
        provider.script[2]=malformed(provider.script[2])
        async with f.running(provider=provider) as (client,_):
            await self.assert_failure(f.f,f.platform,client,AgentRole.DEVELOPER,single=True)

    async def test_failed_host_trace_write_keeps_durable_a2a_failure(self):
        f=self.fixture()
        providers=f.providers()
        providers[AgentRole.PLANNER].script[0]=malformed(providers[AgentRole.PLANNER].script[0])
        with patch.object(AgentTelemetryStore,'append_llm_validation',side_effect=RuntimeError('private-storage-error')):
            async with f.running_platform(providers=providers) as (client,_):
                await self.assert_failure(f,f.platform,client,AgentRole.PLANNER,recorded=False)

    async def test_qa_policy_rejection_is_persisted_with_specific_reason(self):
        f=self.fixture()
        providers=f.providers()
        providers[AgentRole.QA].script[0]=tool_response(ToolCall(
            call_id='policy_test',name='run_build',arguments_json='{}'))
        async with f.running_platform(providers=providers) as (client,_):
            await self.assert_failure(f,f.platform,client,AgentRole.QA,policy=True)
