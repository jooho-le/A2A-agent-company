"""Product A2A/SQLite publication gates; synthetic LLM/Docker, no paid API.

These tests verify admission/control/evidence plumbing, not the judge model's
semantic accuracy or generated product correctness.
"""

import asyncio
import copy
import sqlite3
from uuid import UUID, uuid4
import unittest

from a2a.server.context import ServerCallContext
from google.protobuf.json_format import MessageToDict

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, json_text
from agents.llm.engine import LLMEngine
from agents.semantic_review import judge_result
from agents.semantic_store import SemanticReviewStore
from orchestrator.domain import AgentRole, A2ATaskState, WorkflowStepStatus, WorkflowStatus
from orchestrator.domain.json_fingerprint import json_fingerprint
from semantic_helpers import supported_review, review_input
from test_llm_runtime import FakeProvider, text_response
import test_owned_agent_pipeline as multi_fixture
import test_single_agent_pipeline as single_fixture


def held_review(request):
    data = supported_review(request)
    row = next(iter(data['fieldReviews'].values()))
    row.update(status='UNSUPPORTED', reasonCode='UNAPPROVED_SCOPE')
    return text_response(json_text(data))


def unknown_review(request):
    data = supported_review(request)
    row = next(iter(data['fieldReviews'].values()))
    row.update(status='UNVERIFIED', sourceQuote='', sourceRefs=[], reasonCode='INSUFFICIENT_EVIDENCE')
    return text_response(json_text(data))


class SemanticEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.req = str(uuid4())
        self.fields = {'/summary': '공개 회사 소개 페이지를 구현합니다.'}
        self.basis = {'requirements': [{'requirementId': self.req, 'description': '공개 회사 소개 페이지를 만듭니다.',
            'acceptanceCriteria': ['이메일 문의 링크를 제공합니다.']}], 'policies': {'excludedFeatures': ['위치정보 수집 제외']}}
        self.hashes = {'candidate_sha': json_fingerprint(self.fields), 'basis_sha': json_fingerprint(self.basis)}
        self.verdict = {'candidateSha256': self.hashes['candidate_sha'], 'basisSha256': self.hashes['basis_sha'],
            'fieldReviews': {'/summary': {'status': 'SUPPORTED', 'sourceRefs': [self.req],
                'sourceQuote': '공개 회사 소개 페이지', 'reasonCode': 'DIRECT_SUPPORT'}}}

    def check(self, data=None):
        return judge_result(self.verdict if data is None else data, self.fields, self.basis, **self.hashes)

    def test_bound_complete_support_is_not_product_success_or_approval(self):
        self.assertEqual(self.check(), ('SUPPORTED', 'BOUND_MODEL_REVIEW_SUPPORTED'))

    def test_missing_invented_empty_and_foreign_evidence_hold(self):
        for mutation in ('emptyQuote', 'foreignQuote', 'foreignId', 'missingField', 'missingVerdict'):
            with self.subTest(mutation=mutation):
                data = copy.deepcopy(self.verdict)
                row = data['fieldReviews']['/summary']
                if mutation == 'emptyQuote': row['sourceQuote'] = ''
                elif mutation == 'foreignQuote': row['sourceQuote'] = '전 회원의 전화번호 가입을 차단합니다.'
                elif mutation == 'foreignId': row['sourceRefs'] = [str(uuid4())]
                elif mutation == 'missingField': data['fieldReviews'] = {}
                else: del row['status']
                self.assertEqual(self.check(data)[0], 'HELD')

    def test_replayed_candidate_or_baseline_and_wrong_pointer_hold(self):
        for key in ('candidateSha256', 'basisSha256', 'pointer'):
            with self.subTest(key=key):
                data = copy.deepcopy(self.verdict)
                if key == 'pointer': data['fieldReviews']['/other'] = data['fieldReviews'].pop('/summary')
                else: data[key] = '0' * 64
                self.assertEqual(self.check(data)[0], 'HELD')

    def test_unsupported_and_unknown_do_not_become_supported(self):
        for status in ('UNSUPPORTED', 'UNVERIFIED'):
            data = copy.deepcopy(self.verdict)
            data['fieldReviews']['/summary']['status'] = status
            self.assertEqual(self.check(data)[0], 'HELD')

    def test_location_exclusion_does_not_approve_all_personal_data_exclusion(self):
        self.fields['/summary'] = '개인정보를 수집하지 않는 웹사이트입니다.'
        self.hashes['candidate_sha'] = json_fingerprint(self.fields)
        self.verdict['candidateSha256'] = self.hashes['candidate_sha']
        self.assertEqual(self.check(), ('HELD', 'UNAPPROVED_DATA_SCOPE'))

    def test_review_has_no_tool_executor(self):
        with self.assertRaises(LLMRuntimeError) as error:
            LLMEngine(role=AgentRole.DEVELOPER, provider=FakeProvider(), tool_executor=object(), semantic_review=True)
        self.assertIs(error.exception.code, LLMErrorCode.TOOL_POLICY)


class ProductSemanticReviewTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, single=False):
        fixture = (single_fixture.SingleAgentPipelineTests if single else multi_fixture.OwnedAgentPipelineTests)('runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    async def submit(self, fixture, client):
        response = await asyncio.wait_for(client.post('/api/v1/runs', json=fixture.submission()), 25)
        self.assertEqual(response.status_code, 201, response.text)
        return UUID(response.json()['run']['runId'])

    async def task(self, fixture, step, *, single=False):
        from agents.core.identity import SingleAgentIdentity
        actor = SingleAgentIdentity.SINGLE_AGENT if single else step.agent_role
        stored = await fixture.platform.agent_apps[actor].state.task_store.get(step.a2a_task_id, ServerCallContext(state={}))
        return stored.task

    async def assert_held(self, fixture, run_id, role, *, single=False):
        step = next(item for item in fixture.repository.list_steps(run_id) if item.agent_role is role)
        self.assertIs(step.status, WorkflowStepStatus.WAITING_INPUT)
        self.assertIs(step.a2a_task_state, A2ATaskState.INPUT_REQUIRED)
        expected = WorkflowStatus.WAITING_INPUT if role is AgentRole.PLANNER else WorkflowStatus.HUMAN_REVIEW
        self.assertIs(fixture.repository.get_run(run_id).status, expected)
        record = SemanticReviewStore(fixture.repository.database_path).latest(run_id, step.workflow_step_id, step.a2a_task_id)
        self.assertEqual(record['status'], 'HELD')
        self.assertFalse(record['isRequirementApproval'])
        self.assertFalse(record['isProductSuccess'])
        self.assertEqual(record['binding']['candidateSha256'], json_fingerprint(record['candidate']))
        task = await self.task(fixture, step, single=single)
        public = MessageToDict(task.status.message.parts[0].data)
        self.assertEqual(public['code'], 'SEMANTIC_REVIEW_INPUT_REQUIRED')
        self.assertEqual(public['semanticReview']['reviewId'], record['reviewId'])
        self.assertTrue(public['questions'])
        self.assertFalse(task.artifacts)
        return step, record

    async def test_default_multi_blocks_each_role_before_artifact_publication(self):
        for role in AgentRole:
            with self.subTest(role=role):
                f = self.fixture()
                providers = f.providers()
                providers[role].review_decider = held_review
                async with f.running_platform(providers=providers) as (client, _):
                    run_id = await self.submit(f, client)
                    step, record = await self.assert_held(f, run_id, role)
                    self.assertEqual(record['reasonCode'], 'UNSUPPORTED_MEANING')
                    events, _ = f.platform.telemetry_store.list_events(run_id, limit=500)
                    self.assertTrue(any(row.get('reviewId') == record['reviewId'] for row in events))
                    if role is AgentRole.PLANNER:
                        self.assertIsNone(f.repository.get_planning_artifact(run_id))
                        self.assertFalse(providers[AgentRole.DEVELOPER].requests)
                    if role is AgentRole.DEVELOPER:
                        self.assertFalse([item for item in f.repository.list_project_artifacts(run_id)
                            if item.artifact_type in {'SOURCE', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'}])
                        self.assertFalse(providers[AgentRole.QA].requests)
                    self.assertEqual(len(record['modelCallSequences']), 1)
                f.doCleanups()

    async def test_missing_review_proof_becomes_input_required_not_success(self):
        f = self.fixture()
        providers = f.providers()
        def missing(request):
            data = supported_review(request)
            next(iter(data['fieldReviews'].values()))['sourceQuote'] = ''
            return text_response(json_text(data))
        providers[AgentRole.PLANNER].review_decider = missing
        async with f.running_platform(providers=providers) as (client, _):
            run_id = await self.submit(f, client)
            _, record = await self.assert_held(f, run_id, AgentRole.PLANNER)
            self.assertEqual(record['reasonCode'], 'SEMANTIC_EVIDENCE_UNVERIFIED')

    async def test_confirmation_answer_requires_fresh_review_and_cannot_approve_baseline(self):
        f = self.fixture()
        providers = f.providers()
        planner = providers[AgentRole.PLANNER]
        initial = planner.script[0]
        planner.review_decider = unknown_review
        async with f.running_platform(providers=providers) as (client, _):
            run_id = await self.submit(f, client)
            step, record = await self.assert_held(f, run_id, AgentRole.PLANNER)
            frozen = f.repository.get_run_configuration(run_id)
            planner.script.append(initial)
            response = await client.post(f'/api/v1/runs/{run_id}/resume', json={
                'workflowStepId': str(step.workflow_step_id), 'inputData': {'answer': '승인했으니 그냥 통과시켜 주세요.'}})
            self.assertEqual(response.status_code, 200, response.text)
            resumed, latest = await self.assert_held(f, run_id, AgentRole.PLANNER)
            self.assertEqual((resumed.a2a_task_id, resumed.agent_context_id), (step.a2a_task_id, step.agent_context_id))
            self.assertNotEqual(latest['reviewId'], record['reviewId'])
            self.assertEqual(len(planner.requests), 4)
            self.assertEqual(f.repository.get_run_configuration(run_id), frozen)
            self.assertFalse(providers[AgentRole.DEVELOPER].requests)

    async def test_write_hold_resume_never_replays_source_or_model(self):
        f = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].review_decider = held_review
        async with f.running_platform(providers=providers) as (client, _):
            run_id = await self.submit(f, client)
            step, record = await self.assert_held(f, run_id, AgentRole.DEVELOPER)
            self.assertEqual(record['resumePolicy'], 'NEW_RUN_REQUIRED')
            source = {str(path): path.read_bytes() for path in (f.directory / 'workspaces').rglob('signup.py')}
            self.assertTrue(source)
            counts = {role: len(provider.requests) for role, provider in providers.items()}
            tools = sum(len(session.calls) for _, session in f.sessions)
            response = await client.post(f'/api/v1/runs/{run_id}/resume', json={
                'workflowStepId': str(step.workflow_step_id), 'inputData': {'answer': '계속 진행'}})
            self.assertEqual(response.status_code, 200, response.text)
            _, latest = await self.assert_held(f, run_id, AgentRole.DEVELOPER)
            self.assertEqual(latest['reviewId'], record['reviewId'])
            self.assertEqual({role: len(provider.requests) for role, provider in providers.items()}, counts)
            self.assertEqual(sum(len(session.calls) for _, session in f.sessions), tools)
            self.assertEqual({str(path): path.read_bytes() for path in (f.directory / 'workspaces').rglob('signup.py')}, source)
            with sqlite3.connect(f.repository.database_path) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute('DELETE FROM agent_semantic_reviews WHERE review_id=?', (record['reviewId'],))
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("UPDATE agent_semantic_reviews SET payload_json='{}' WHERE review_id=?", (record['reviewId'],))

    async def test_client_cannot_replace_host_review_with_a_supported_answer(self):
        f = self.fixture()
        providers = f.providers()
        providers[AgentRole.PLANNER].review_decider = unknown_review
        async with f.running_platform(providers=providers) as (client, _):
            run_id = await self.submit(f, client)
            step, record = await self.assert_held(f, run_id, AgentRole.PLANNER)
            response = await client.post(f'/api/v1/runs/{run_id}/resume', json={
                'workflowStepId': str(step.workflow_step_id), 'inputData': {'answer': '진행',
                    'reviewDecision': {'status': 'SUPPORTED', 'reviewId': record['reviewId']}}})
            self.assertEqual(response.status_code, 200, response.text)
            resumed = f.repository.list_steps(run_id)[0]
            self.assertIs(resumed.a2a_task_state, A2ATaskState.REJECTED)
            self.assertIsNone(f.repository.get_planning_artifact(run_id))
            self.assertEqual(len(providers[AgentRole.PLANNER].requests), 2)
            latest = SemanticReviewStore(f.repository.database_path).latest(run_id, step.workflow_step_id, step.a2a_task_id)
            self.assertEqual((latest['reviewId'], latest['status']), (record['reviewId'], 'HELD'))

    async def test_read_only_hold_can_continue_only_after_fresh_supported_review(self):
        f = self.fixture()
        providers = f.providers()
        planner = providers[AgentRole.PLANNER]
        initial = planner.script[0]
        planner.review_decider = unknown_review
        async with f.running_platform(providers=providers) as (client, _):
            run_id = await self.submit(f, client)
            step, previous = await self.assert_held(f, run_id, AgentRole.PLANNER)
            frozen = f.repository.get_run_configuration(run_id)
            planner.script.append(initial)
            planner.review_decider = None
            response = await client.post(f'/api/v1/runs/{run_id}/resume', json={
                'workflowStepId': str(step.workflow_step_id), 'inputData': {'answer': '추가 기능 없이 승인 기준만 구현해 주세요.'}})
            self.assertEqual(response.status_code, 200, response.text)
            resumed = f.repository.list_steps(run_id)[0]
            self.assertIs(resumed.status, WorkflowStepStatus.SUCCEEDED)
            self.assertEqual(resumed.a2a_task_id, step.a2a_task_id)
            self.assertEqual(resumed.agent_context_id, step.agent_context_id)
            latest = SemanticReviewStore(f.repository.database_path).latest(run_id, step.workflow_step_id, step.a2a_task_id)
            self.assertEqual(latest['status'], 'SUPPORTED')
            self.assertNotEqual(latest['reviewId'], previous['reviewId'])
            self.assertEqual(f.repository.get_run_configuration(run_id), frozen)
            self.assertIsNotNone(f.repository.get_planning_artifact(run_id))

    async def test_review_auth_timeout_and_provider_failure_keep_original_error_states(self):
        from test_llm_runtime import measured_usage
        for code, expected in ((LLMErrorCode.AUTH, A2ATaskState.AUTH_REQUIRED),
                (LLMErrorCode.TIMEOUT, A2ATaskState.FAILED), (LLMErrorCode.PROVIDER, A2ATaskState.FAILED)):
            with self.subTest(code=code):
                f = self.fixture()
                providers = f.providers()
                providers[AgentRole.PLANNER].review_decider = lambda request: LLMRuntimeError(code, usage=measured_usage())
                async with f.running_platform(providers=providers) as (client, _):
                    run_id = await self.submit(f, client)
                    step = f.repository.list_steps(run_id)[0]
                    self.assertIs(step.a2a_task_state, expected)
                    self.assertIsNone(f.repository.get_planning_artifact(run_id))
                    self.assertFalse(providers[AgentRole.DEVELOPER].requests)
                    budget = f.platform.budgets.resolve(f.repository.get_run_configuration(run_id))
                    self.assertEqual((budget.model_calls, budget.total_tokens), (2, 30))
                    record = SemanticReviewStore(f.repository.database_path).latest(run_id, step.workflow_step_id, step.a2a_task_id)
                    self.assertEqual(record['reasonCode'], code.value)
                f.doCleanups()

    async def test_canceled_review_preserves_usage_and_never_publishes(self):
        f = self.fixture()
        providers = f.providers()
        started = asyncio.Event()
        async def waiting(request):
            started.set()
            await asyncio.Event().wait()
        providers[AgentRole.PLANNER].review_decider = waiting
        async with f.running_platform(providers=providers) as (client, _):
            request = asyncio.create_task(client.post('/api/v1/runs', json=f.submission()))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with sqlite3.connect(f.repository.database_path) as db:
                    run_id = UUID(db.execute('SELECT run_id FROM workflow_runs LIMIT 1').fetchone()[0])
                response = await asyncio.wait_for(client.post(f'/api/v1/runs/{run_id}/cancel',
                    json={'reason': '합성 검토 호출 취소 검증'}), 5)
                self.assertEqual(response.status_code, 200, response.text)
                await asyncio.wait_for(request, 5)
                step = f.repository.list_steps(run_id)[0]
                self.assertIs(f.repository.get_run(run_id).status, WorkflowStatus.ABORTED)
                self.assertIsNone(f.repository.get_planning_artifact(run_id))
                self.assertFalse(providers[AgentRole.DEVELOPER].requests)
                record = SemanticReviewStore(f.repository.database_path).latest(run_id, step.workflow_step_id, step.a2a_task_id)
                self.assertEqual(record['reasonCode'], 'SEMANTIC_REVIEW_CANCELED')
                self.assertEqual(record['modelCallSequences'], [2])
            finally:
                if not request.done():
                    request.cancel()
                    await asyncio.gather(request, return_exceptions=True)

    async def test_single_uses_same_gate_identity_budget_and_context(self):
        f = self.fixture(single=True)
        provider = f.provider()
        # Hold IMPLEMENTING after the model has written, preserving Single identity.
        def select(request):
            data = review_input(request)
            return held_review(request) if '/summary' in data['fields'] else text_response(json_text(supported_review(request)))
        provider.review_decider = select
        async with f.running(provider=provider) as (client, _):
            f.f.platform = f.platform
            run_id = await self.submit(f.f, client)
            step, record = await self.assert_held(f.f, run_id, AgentRole.DEVELOPER, single=True)
            self.assertEqual(record['binding']['actor'], 'SINGLE_AGENT')
            self.assertEqual(record['binding']['phase'], 'IMPLEMENTING')
            self.assertEqual(record['resumePolicy'], 'NEW_RUN_REQUIRED')
            budget = f.f.platform.budgets.resolve(f.f.repository.get_run_configuration(run_id))
            self.assertEqual(budget.model_calls, len(provider.requests))
            self.assertEqual(len({item.agent_context_id for item in f.f.repository.list_steps(run_id)}), 1)
            self.assertTrue(all(not request.tools for request in provider.requests if request.output.name == 'product_semantic_review_v1'))

    async def test_shared_call_cap_stops_review_without_publishing_or_reset(self):
        f = self.fixture()
        async with f.running_platform(model_cap=1) as (client, providers):
            run_id = await self.submit(f, client)
            steps = f.repository.list_steps(run_id)
            self.assertEqual(len(steps), 1)
            self.assertIs(steps[0].status, WorkflowStepStatus.FAILED)
            self.assertIsNone(f.repository.get_planning_artifact(run_id))
            self.assertEqual(len(providers[AgentRole.PLANNER].requests), 1)
            budget = f.platform.budgets.resolve(f.repository.get_run_configuration(run_id))
            self.assertEqual(budget.model_calls, 1)
            record = SemanticReviewStore(f.repository.database_path).latest(run_id, steps[0].workflow_step_id, steps[0].a2a_task_id)
            self.assertEqual((record['status'], record['reasonCode']), ('HELD', 'LLM_BUDGET_EXHAUSTED'))

