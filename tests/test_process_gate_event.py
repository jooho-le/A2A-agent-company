"""4번 과정 지표: Orchestrator가 Run을 HUMAN_REVIEW로 보낸 중앙 거부는 PROCESS_GATE_REJECTED 사건으로 남는다.

dev 시험(test_dispatch.py)의 가짜 Planner 클라이언트를 그대로 쓰며, 실제 SQLite 저장소에 기록된 사건을 확인한다.
평가 쪽 집계(host_process)가 같은 사건을 관문 거부로 세는지도 함께 본다.
"""
import tempfile
import unittest
from pathlib import Path

import httpx

from evaluation.evidence_metrics import process_metrics
from evaluation.host_process import derive_process_evidence
from orchestrator.a2a import A2AAgentRegistry
from orchestrator.application import PlannerRunDispatcher
from orchestrator.domain import AgentRole, SCN_001_ID, TraceEvent, WorkflowRun, WorkflowStatus, WorkflowStep
from orchestrator.infrastructure import SQLiteWorkflowRepository
from test_dispatch import FakePlannerClient


class ProcessGateEventTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.repository = SQLiteWorkflowRepository(Path(self.temp_dir.name) / 'gate.sqlite3')
        self.run = WorkflowRun(scenario_id=SCN_001_ID, request_text='회원가입 기능을 계획해줘.')
        step = WorkflowStep(run_id=self.run.run_id, agent_role=AgentRole.PLANNER)
        self.repository.create_run(self.run, (step,), (
            TraceEvent(run_id=self.run.run_id, event_type='RUN_STARTED', actor='Orchestrator', attempt=0,
                       workflow_state=self.run.status),
            TraceEvent(run_id=self.run.run_id, workflow_step_id=step.workflow_step_id,
                       event_type='WORKFLOW_STEP_CREATED', actor='Orchestrator', attempt=0,
                       workflow_state=self.run.status)))

    async def test_human_review_transition_records_a_process_gate_rejection(self):
        client = FakePlannerClient(httpx.ConnectError('connection interrupted'))
        dispatcher = PlannerRunDispatcher(self.repository, A2AAgentRegistry({
            AgentRole.PLANNER: 'http://planner.test', AgentRole.DEVELOPER: 'http://developer.test',
            AgentRole.QA: 'http://qa.test', AgentRole.SECURITY: 'http://security.test'}),
            client_factory=lambda url: client)
        await dispatcher.dispatch_planner(self.run.run_id)

        run = self.repository.get_run(self.run.run_id)
        events, total = self.repository.list_events(self.run.run_id, limit=100, offset=0)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        kinds = [event.event_type for event in events]
        self.assertIn('A2A_DISPATCH_REQUIRES_REVIEW', kinds)
        self.assertEqual(kinds.count('PROCESS_GATE_REJECTED'), 1)
        gate = next(event for event in events if event.event_type == 'PROCESS_GATE_REJECTED')
        reason = next(event for event in events if event.event_type == 'A2A_DISPATCH_REQUIRES_REVIEW')
        # 같은 전환에서 함께 기록되므로 같은 단계·시도·상태를 가리킨다.
        self.assertEqual((gate.workflow_step_id, gate.attempt, gate.workflow_state),
                         (reason.workflow_step_id, reason.attempt, WorkflowStatus.HUMAN_REVIEW))

        # 평가 쪽 집계가 이 사건을 Orchestrator 관문 거부 1건으로 센다.
        bundle_events = [event.to_trace_json() for event in events]
        export = {'bundle': {'run': {'runId': str(self.run.run_id), 'status': run.status.value, 'fixAttempt': 0,
                                     'verdict': None},
                             'events': bundle_events, 'eventsTotal': total, 'snapshotConsistent': True,
                             'configuration': {'configuration': {'architecture': 'MULTI_AGENT'}}},
                  'trace': {'telemetry': [], 'telemetryTotal': 0, 'artifacts': []}, 'handoff': {'steps': []}}
        evidence = derive_process_evidence(export)
        gates = [row for row in evidence['events'] if row['kind'] == 'GATE_REJECTION']
        self.assertEqual([(row['rejectionId'], row['stage']) for row in gates],
                         [(str(gate.event_id), 'ORCHESTRATOR_OUTPUT')])
        # 실행 전 관찰 정책이 없는 기록이라 관문 거부 '0건 아님'은 알지만 전체 건수를 확정하지는 않는다.
        self.assertNotIn('GATE_REJECTION', evidence['completeKinds'])
        self.assertIsNone(process_metrics(str(self.run.run_id), evidence)['gateRejections']['count'])
