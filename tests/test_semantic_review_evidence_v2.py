"""Offline regressions for publication-review evidence, not model accuracy.

The joined quotations reproduce the observed diagnostic failure without reading
local Run databases, calling a model, or executing generated product code.
"""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from a2a.types import TaskState
from google.protobuf.json_format import MessageToDict

from agents.llm.contracts import LLMResult, json_text
from agents.semantic_review import (
    ProductSemanticReviewer, SemanticReviewRequired, inspect_result, judge_result,
    publish_semantic_hold, review_output,
)
from agents.semantic_store import SemanticReviewStore
from orchestrator.domain.json_fingerprint import json_fingerprint
from orchestrator.domain.states import AgentRole


class SemanticReviewEvidenceV2Tests(unittest.TestCase):
    def setUp(self):
        self.result_id = '00000000-0000-4000-8000-000000000001'
        self.unique_id = '00000000-0000-4000-8000-000000000002'
        self.result_quote = '실제 가입 처리 결과와 사용자 응답이 일치해야 함'
        self.policy_quote = '응답 본문에 password와 password_hash 값을 넣지 않는다'
        self.result_criterion = '성공은 성공으로, 검증/중복 실패는 실패로 응답'
        self.unique_quote = '동일한 canonical email로 중복 계정을 만들 수 없어야 함'
        self.unique_criterion = '애플리케이션 사전검사와 별개로 DB UNIQUE 제약이 존재하며 동시 요청에서도 중복 생성 불가'
        self.fields = {'/summary': '회원가입 결과에 맞게 응답하고 비밀번호를 응답에 포함하지 않습니다.'}
        self.basis = {
            'requirements': [
                {'requirementId': self.result_id, 'description': self.result_quote,
                 'acceptanceCriteria': [self.result_criterion]},
                {'requirementId': self.unique_id, 'description': self.unique_quote,
                 'acceptanceCriteria': [self.unique_criterion]},
            ],
            'policies': {'securityPolicy': {'response': self.policy_quote},
                         'excludedFeatures': ['위치정보 수집 제외']},
        }
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)

    @staticmethod
    def _hashes(fields, basis):
        return {'candidate_sha': json_fingerprint(fields), 'basis_sha': json_fingerprint(basis)}

    def _row(self, evidence=None):
        return {
            'status': 'SUPPORTED',
            'evidence': evidence if evidence is not None else [
                {'sourceRef': self.result_id, 'quote': self.result_quote},
                {'sourceRef': '@policy', 'quote': self.policy_quote},
            ],
            'reasonCode': 'DIRECT_SUPPORT',
        }

    def _response(self, fields, hashes):
        return {'candidateSha256': hashes['candidate_sha'], 'basisSha256': hashes['basis_sha'],
                'fieldReviews': {pointer: self._row() for pointer in fields}}

    def _inspect(self, response=None):
        return inspect_result(self.response if response is None else response,
                              self.fields, self.basis, **self.hashes)

    def test_separate_requirement_and_policy_quotes_pass(self):
        decision = self._inspect()
        self.assertEqual((decision.status, decision.reason_code, decision.category),
                         ('SUPPORTED', 'BOUND_MODEL_REVIEW_SUPPORTED', 'NONE'))
        self.assertFalse(decision.issues)

    def test_separate_quotes_from_same_requirement_pass(self):
        self.response['fieldReviews']['/summary']['evidence'] = [
            {'sourceRef': self.result_id, 'quote': self.result_quote},
            {'sourceRef': self.result_id, 'quote': self.result_criterion},
        ]
        self.assertEqual(self._inspect().status, 'SUPPORTED')

    def test_quote_must_belong_to_its_own_source(self):
        self.response['fieldReviews']['/summary']['evidence'][0]['sourceRef'] = self.unique_id
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category), ('HELD', 'EVIDENCE'))
        self.assertEqual(decision.reason_code, 'SEMANTIC_EVIDENCE_INVALID')
        self.assertTrue(any(row['fieldPointer'] == '/summary' for row in decision.issues))

    def test_one_valid_quote_cannot_hide_one_invented_quote(self):
        self.response['fieldReviews']['/summary']['evidence'].append(
            {'sourceRef': '@policy', 'quote': '모든 사용자의 전화번호를 반드시 수집한다'})
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category), ('HELD', 'EVIDENCE'))
        self.assertEqual(decision.reason_code, 'SEMANTIC_EVIDENCE_INVALID')

    def test_policy_quote_cannot_be_attributed_to_requirement(self):
        self.response['fieldReviews']['/summary']['evidence'][1]['sourceRef'] = self.result_id
        self.assertEqual(self._inspect().category, 'EVIDENCE')

    def test_nonapproved_source_is_never_accepted(self):
        self.response['fieldReviews']['/summary']['evidence'][0]['sourceRef'] = 'unapproved'
        decision = self._inspect()
        self.assertEqual(decision.status, 'HELD')
        self.assertIn(decision.category, {'FORMAT', 'EVIDENCE'})

    def test_empty_evidence_and_blank_quote_cannot_support_a_field(self):
        for evidence in ([], [{'sourceRef': self.result_id, 'quote': ''}],
                         [{'sourceRef': self.result_id, 'quote': '  \n '}],):
            with self.subTest(evidence=evidence):
                response = copy.deepcopy(self.response)
                response['fieldReviews']['/summary']['evidence'] = evidence
                decision = self._inspect(response)
                self.assertEqual((decision.status, decision.category), ('HELD', 'EVIDENCE'))
                self.assertTrue(decision.issues)

    def test_joined_quote_does_not_pass_even_if_both_fragments_exist(self):
        self.response['fieldReviews']['/summary']['evidence'] = [
            {'sourceRef': self.result_id, 'quote': self.result_quote + '; ' + self.result_criterion},
        ]
        decision = self._inspect()
        self.assertEqual((decision.status, decision.reason_code),
                         ('HELD', 'SEMANTIC_EVIDENCE_INVALID'))

    def test_literal_semicolon_in_original_text_is_valid(self):
        self.basis['requirements'][0]['acceptanceCriteria'].append('가입 성공; 정상 응답')
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        self.response['fieldReviews']['/summary']['evidence'] = [
            {'sourceRef': self.result_id, 'quote': '가입 성공; 정상 응답'},
        ]
        self.assertEqual(self._inspect().status, 'SUPPORTED')

    def test_all_failed_fields_are_reported_without_supported_fields(self):
        self.fields = {f'/implementationPlan/{i}/description': '회원가입 API 구현' for i in range(4)}
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        bad = list(self.fields)[::2]
        for pointer in bad:
            self.response['fieldReviews'][pointer]['evidence'][0]['quote'] = '존재하지 않는 근거'
        decision = self._inspect()
        self.assertEqual(decision.status, 'HELD')
        self.assertEqual({issue['fieldPointer'] for issue in decision.issues}, set(bad))

    def test_missing_field_is_format_failure_and_identifies_field(self):
        self.fields['/title'] = '회원가입 API'
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        del self.response['fieldReviews']['/title']
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category, decision.reason_code),
                         ('HELD', 'FORMAT', 'SEMANTIC_RESPONSE_INVALID'))
        self.assertTrue(any(issue['fieldPointer'] == '/title' for issue in decision.issues))

    def test_malformed_field_does_not_hide_other_failed_fields(self):
        self.fields['/title'] = '회원가입 API'
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        del self.response['fieldReviews']['/title']['reasonCode']
        self.response['fieldReviews']['/summary']['evidence'][0]['quote'] = '존재하지 않는 근거'
        decision = self._inspect()
        self.assertEqual(decision.category, 'FORMAT')
        self.assertTrue({'/title', '/summary'} <= {issue['fieldPointer'] for issue in decision.issues})

    def test_old_single_quote_response_is_not_silently_reinterpreted(self):
        row = self.response['fieldReviews']['/summary']
        del row['evidence']
        row['sourceRefs'] = [self.result_id, '@policy']
        row['sourceQuote'] = self.result_quote + '; ' + self.policy_quote
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category), ('HELD', 'FORMAT'))

    def test_candidate_and_basis_mismatch_are_binding_failures(self):
        for key in ('candidateSha256', 'basisSha256'):
            with self.subTest(key=key):
                response = copy.deepcopy(self.response)
                response[key] = '0' * 64
                decision = self._inspect(response)
                self.assertEqual((decision.status, decision.category, decision.reason_code),
                                 ('HELD', 'BINDING', 'SEMANTIC_REVIEW_BINDING_INVALID'))

    def test_binding_failure_takes_priority_over_other_bad_evidence(self):
        self.response['basisSha256'] = '0' * 64
        self.response['fieldReviews']['/summary']['evidence'][0]['quote'] = '없는 근거'
        decision = self._inspect()
        self.assertEqual(decision.category, 'BINDING')

    def test_unsupported_meaning_remains_a_semantic_hold(self):
        row = self.response['fieldReviews']['/summary']
        row.update(status='UNSUPPORTED', reasonCode='UNAPPROVED_SCOPE')
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category, decision.reason_code),
                         ('HELD', 'MEANING', 'UNSUPPORTED_MEANING'))

    def test_true_unverified_with_no_evidence_remains_a_semantic_hold(self):
        row = self.response['fieldReviews']['/summary']
        row.update(status='UNVERIFIED', reasonCode='INSUFFICIENT_EVIDENCE', evidence=[])
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category, decision.reason_code),
                         ('HELD', 'MEANING', 'SEMANTIC_EVIDENCE_UNVERIFIED'))

    def test_meaning_issue_is_preserved_alongside_higher_priority_evidence_error(self):
        self.fields['/title'] = '회원가입 API'
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        self.response['fieldReviews']['/summary']['evidence'][0]['quote'] = '없는 근거'
        self.response['fieldReviews']['/title'].update(status='UNSUPPORTED', reasonCode='UNAPPROVED_SCOPE')
        decision = self._inspect()
        self.assertEqual(decision.category, 'EVIDENCE')
        categories = {issue['category'] for issue in decision.issues}
        self.assertTrue({'EVIDENCE', 'MEANING'} <= categories)

    def test_broadened_data_scope_is_still_blocked(self):
        self.fields['/summary'] = '개인정보를 수집하지 않는 회원가입 서비스를 구현합니다.'
        self.hashes = self._hashes(self.fields, self.basis)
        self.response = self._response(self.fields, self.hashes)
        decision = self._inspect()
        self.assertEqual((decision.status, decision.category, decision.reason_code),
                         ('HELD', 'MEANING', 'UNAPPROVED_DATA_SCOPE'))

    def test_nested_evidence_schema_stays_strict_and_bounded(self):
        output = review_output(self.fields, [self.result_id, self.unique_id, '@policy'], **self.hashes)
        self.assertEqual(output.name, 'product_semantic_review_v5')
        output.schema.require_openai_strict()
        output.schema.validate(self.response)
        for mutation in ('extraField', 'tooManyEvidence'):
            with self.subTest(mutation=mutation):
                response = copy.deepcopy(self.response)
                evidence = response['fieldReviews']['/summary']['evidence']
                if mutation == 'extraField':
                    evidence[0]['isApproved'] = True
                else:
                    evidence[:] = [copy.deepcopy(evidence[0]) for _ in range(17)]
                self.assertEqual(self._inspect(response).category, 'FORMAT')

    def test_inspection_does_not_modify_stored_inputs(self):
        self.response['fieldReviews']['/summary']['evidence'][0]['quote'] += '; ' + self.result_criterion
        original = copy.deepcopy((self.response, self.fields, self.basis))
        self._inspect()
        self.assertEqual((self.response, self.fields, self.basis), original)

    def test_tuple_api_reports_same_overall_decision(self):
        decision = self._inspect()
        self.assertEqual(judge_result(self.response, self.fields, self.basis, **self.hashes),
                         (decision.status, decision.reason_code))

    def test_actual_four_joined_quote_shapes_are_all_rejected_then_separate_evidence_passes(self):
        pairs = {
            '/implementationPlan/4/title': [
                {'sourceRef': self.unique_id, 'quote': self.unique_quote},
                {'sourceRef': self.result_id, 'quote': self.result_quote}],
            '/implementationPlan/4/description': [
                {'sourceRef': self.unique_id, 'quote': self.unique_criterion},
                {'sourceRef': self.result_id, 'quote': self.result_criterion}],
            '/implementationPlan/5/title': [
                {'sourceRef': self.result_id, 'quote': self.result_quote},
                {'sourceRef': '@policy', 'quote': self.policy_quote}],
            '/implementationPlan/5/description': [
                {'sourceRef': self.result_id, 'quote': self.result_criterion},
                {'sourceRef': '@policy', 'quote': self.policy_quote}],
        }
        fields = {f'/details/{i}/summary': '승인된 회원가입 기능 구현' for i in range(10)}
        fields.update({pointer: '사용자 저장 및 회원가입 API 구현' for pointer in pairs})
        hashes = self._hashes(fields, self.basis)
        response = self._response(fields, hashes)
        for pointer, evidence in pairs.items():
            response['fieldReviews'][pointer]['evidence'] = [{
                'sourceRef': evidence[0]['sourceRef'],
                'quote': '; '.join(item['quote'] for item in evidence),
            }]
        original = copy.deepcopy(response)
        decision = inspect_result(response, fields, self.basis, **hashes)
        self.assertEqual(len(response['fieldReviews']), 14)
        self.assertTrue(all(row['status'] == 'SUPPORTED' for row in response['fieldReviews'].values()))
        self.assertEqual((decision.status, decision.category), ('HELD', 'EVIDENCE'))
        self.assertEqual({issue['fieldPointer'] for issue in decision.issues}, set(pairs))
        self.assertEqual(response, original)
        separated = copy.deepcopy(response)
        for pointer, evidence in pairs.items():
            separated['fieldReviews'][pointer]['evidence'] = evidence
        repaired = inspect_result(separated, fields, self.basis, **hashes)
        self.assertEqual(repaired.status, 'SUPPORTED')
        self.assertFalse(repaired.issues)


class SemanticReviewPublicationV2Tests(unittest.IsolatedAsyncioTestCase):
    def record(self, *, category='EVIDENCE', fresh=False, legacy=False):
        record = {
            'schemaVersion': 1 if legacy else 2,
            'reviewId': '00000000-0000-4000-8000-000000000010',
            'status': 'HELD',
            'reasonCode': 'SEMANTIC_EVIDENCE_UNVERIFIED' if legacy else 'SEMANTIC_EVIDENCE_INVALID',
            'resumePolicy': 'NEW_RUN_REQUIRED' if fresh else 'REGENERATE_AND_REVIEW',
            'binding': {'candidateSha256': 'a' * 64, 'basisSha256': 'b' * 64},
            'candidate': {'summary': 'PRIVATE-CANDIDATE-DO-NOT-PUBLISH'},
            'result': {'fieldReviews': {
                '/valid/summary': {'status': 'SUPPORTED', 'sourceRefs': ['private-source'],
                    'sourceQuote': 'PRIVATE-VALID-QUOTE', 'reasonCode': 'DIRECT_SUPPORT'},
                '/failed/summary': {'status': 'SUPPORTED', 'sourceRefs': ['private-source'],
                    'sourceQuote': 'PRIVATE-FAILED-QUOTE', 'reasonCode': 'DIRECT_SUPPORT'},
            }},
        }
        if not legacy:
            record['validation'] = {'category': category, 'issues': [{
                'fieldPointer': '/failed/summary', 'modelStatus': 'SUPPORTED',
                'category': category, 'reasonCode': 'QUOTE_NOT_IN_SOURCE', 'evidenceIndex': 0,
            }]}
        return record

    async def publish(self, record):
        updater = SimpleNamespace(update_status=AsyncMock(),
            new_agent_message=Mock(side_effect=lambda **kwargs: SimpleNamespace(**kwargs)))
        metadata = {'runId': 'publication-only-test'}
        await publish_semantic_hold(updater, metadata, SemanticReviewRequired(record))
        updater.update_status.assert_awaited_once()
        updater.new_agent_message.assert_called_once()
        call = updater.update_status.await_args
        self.assertEqual(call.kwargs['metadata'], metadata)
        payload = MessageToDict(call.kwargs['message'].parts[0].data)
        return call.args[0], payload

    async def test_legacy_record_retains_input_required_without_rejudging_or_rewriting(self):
        for fresh in (False, True):
            with self.subTest(fresh=fresh):
                record = self.record(legacy=True, fresh=fresh)
                original = copy.deepcopy(record)
                state, payload = await self.publish(record)
                self.assertIs(state, TaskState.TASK_STATE_INPUT_REQUIRED)
                self.assertEqual(payload['code'], 'SEMANTIC_REVIEW_INPUT_REQUIRED')
                self.assertTrue(payload['questions'])
                public = payload['semanticReview']
                self.assertEqual(public['category'], 'LEGACY')
                self.assertEqual(public['reasonCode'], record['reasonCode'])
                self.assertEqual(public['resumePolicy'], record['resumePolicy'])
                self.assertIsNone(public['issueCount'])
                self.assertFalse(public['issuesAvailable'])
                self.assertFalse(public['issuesTruncated'])
                self.assertEqual(public['issues'], [])
                self.assertEqual(record, original)

    async def test_public_v2_contains_only_host_failed_issues_without_quotes_or_candidate(self):
        record = self.record()
        original = copy.deepcopy(record)
        state, payload = await self.publish(record)
        self.assertIs(state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(payload['code'], 'SEMANTIC_REVIEW_FAILED')
        self.assertEqual(payload['questions'], [])
        public = payload['semanticReview']
        self.assertEqual(public['schemaVersion'], 2)
        self.assertEqual(public['issueCount'], 1)
        self.assertTrue(public['issuesAvailable'])
        self.assertFalse(public['issuesTruncated'])
        self.assertFalse(public['isRequirementApproval'])
        self.assertEqual([issue['fieldPointer'] for issue in public['issues']], ['/failed/summary'])
        # SUPPORTED is retained as the model's decision; Host evidence failed.
        self.assertEqual(public['issues'][0]['modelStatus'], 'SUPPORTED')
        self.assertEqual(public['issues'][0]['category'], 'EVIDENCE')
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn('PRIVATE-', encoded)
        self.assertNotIn('/valid/summary', encoded)
        self.assertNotIn('sourceQuote', encoded)
        self.assertEqual(record, original)

    async def test_issue_overflow_is_explicit_and_private_record_remains_complete(self):
        record = self.record()
        record['validation']['issues'] = [{
            'fieldPointer': f'/implementationPlan/{i}/description', 'modelStatus': 'SUPPORTED',
            'category': 'EVIDENCE', 'reasonCode': 'QUOTE_NOT_IN_SOURCE', 'evidenceIndex': 0,
        } for i in range(70)]
        original = copy.deepcopy(record)
        _, payload = await self.publish(record)
        public = payload['semanticReview']
        self.assertEqual(public['issueCount'], 70)
        self.assertTrue(public['issuesTruncated'])
        self.assertEqual(len(public['issues']), 64)
        self.assertEqual([issue['fieldPointer'] for issue in public['issues']],
                         [issue['fieldPointer'] for issue in original['validation']['issues'][:64]])
        self.assertEqual(record, original)

    async def test_exact_issue_limit_is_not_marked_truncated(self):
        record = self.record()
        record['validation']['issues'] = [{
            'fieldPointer': f'/implementationPlan/{i}/description', 'modelStatus': 'SUPPORTED',
            'category': 'EVIDENCE', 'reasonCode': 'QUOTE_NOT_IN_SOURCE',
        } for i in range(64)]
        _, payload = await self.publish(record)
        self.assertEqual(payload['semanticReview']['issueCount'], 64)
        self.assertFalse(payload['semanticReview']['issuesTruncated'])
        self.assertEqual(len(payload['semanticReview']['issues']), 64)

    async def test_technical_categories_publish_korean_explanation_without_input_question(self):
        for category, reason in (
            ('FORMAT', 'SEMANTIC_RESPONSE_INVALID'),
            ('EVIDENCE', 'SEMANTIC_EVIDENCE_INVALID'),
            ('BINDING', 'SEMANTIC_REVIEW_BINDING_INVALID'),
        ):
            with self.subTest(category=category):
                record = self.record(category=category)
                record['reasonCode'] = reason
                state, payload = await self.publish(record)
                self.assertIs(state, TaskState.TASK_STATE_FAILED)
                self.assertEqual(payload['questions'], [])
                self.assertIn('중단', payload['message'])
                self.assertRegex(payload['message'], '[가-힣]')
                self.assertRegex(payload['semanticReview']['issues'][0]['message'], '[가-힣]')
                self.assertEqual(payload['semanticReview']['category'], category)
                # The public payload round-trips as UTF-8, including human text.
                serialized = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.assertEqual(json.loads(serialized.decode('utf-8')), payload)

    async def test_post_write_technical_failure_preserves_new_run_requirement(self):
        record = self.record(fresh=True)
        state, payload = await self.publish(record)
        self.assertIs(state, TaskState.TASK_STATE_FAILED)
        self.assertEqual(payload['semanticReview']['resumePolicy'], 'NEW_RUN_REQUIRED')
        self.assertIn('새 작업', payload['message'])
        self.assertEqual(payload['questions'], [])

    async def test_true_meaning_hold_still_asks_for_input_and_preserves_failed_field(self):
        record = self.record(category='MEANING')
        record['reasonCode'] = 'SEMANTIC_EVIDENCE_UNVERIFIED'
        record['validation']['issues'][0].update(modelStatus='UNVERIFIED', reasonCode='MODEL_UNVERIFIED')
        state, payload = await self.publish(record)
        self.assertIs(state, TaskState.TASK_STATE_INPUT_REQUIRED)
        self.assertEqual(payload['code'], 'SEMANTIC_REVIEW_INPUT_REQUIRED')
        self.assertTrue(payload['questions'])
        self.assertEqual(payload['semanticReview']['category'], 'MEANING')
        self.assertEqual(payload['semanticReview']['issues'][0]['fieldPointer'], '/failed/summary')
        self.assertEqual(payload['semanticReview']['issues'][0]['modelStatus'], 'UNVERIFIED')


class SemanticReviewLocalBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        directory = tempfile.TemporaryDirectory(prefix='semantic-review-boundary-')
        self.addCleanup(directory.cleanup)
        store = SemanticReviewStore(Path(directory.name) / 'reviews.sqlite3')
        provider = SimpleNamespace(name='openai', complete=AsyncMock(
            side_effect=AssertionError('This boundary must never call a model')))
        metadata = SimpleNamespace(
            run_id='00000000-0000-4000-8000-000000000021',
            workflow_step_id='00000000-0000-4000-8000-000000000022',
            requirement_ids=['00000000-0000-4000-8000-000000000023'],
            project_artifact_ids=[],
        )
        metadata.to_a2a_json = Mock(return_value={
            'runId': metadata.run_id, 'workflowStepId': metadata.workflow_step_id,
        })
        # The draft's existing usage is owned by the Run budget, even when the
        # subsequent local publication check does not make a review call.
        budget = SimpleNamespace(check=Mock(), reserve_model_call=Mock(), model_calls=1, total_tokens=37)
        execution = SimpleNamespace(metadata=metadata, budget=budget)
        context = SimpleNamespace(task_id='local-review-task', context_id='local-review-context')
        basis = {'requirements': [{'requirementId': metadata.requirement_ids[0],
            'description': '회원가입 API를 구현합니다.', 'acceptanceCriteria': []}], 'policies': {}}
        return SimpleNamespace(store=store, provider=provider, execution=execution,
            context=context, basis=basis, reviewer=ProductSemanticReviewer(provider, store))

    async def test_excess_draft_fields_stop_locally_and_persist_technical_failure_without_review_usage(self):
        f = self.fixture()
        candidate = {'implementationPlan': [{'description': f'회원가입 구현 항목 {i}'} for i in range(257)]}
        result = LLMResult(data_json=json_text(candidate), records=(), tool_calls=0)
        usage_sink, started_sink = Mock(), Mock()
        original = copy.deepcopy(candidate)
        with patch('agents.semantic_review.approved_basis', return_value=f.basis):
            with self.assertRaises(SemanticReviewRequired) as held:
                await f.reviewer.check(context=f.context, execution=f.execution, candidate=candidate,
                    role=AgentRole.PLANNER, result=result, usage_sink=usage_sink, call_started_sink=started_sink)
        record = held.exception.record
        self.assertEqual((record['status'], record['reasonCode']), ('HELD', 'SEMANTIC_RESPONSE_INVALID'))
        self.assertEqual(record['validation']['category'], 'FORMAT')
        self.assertEqual([issue['reasonCode'] for issue in record['validation']['issues']], ['FIELD_LIMIT_EXCEEDED'])
        self.assertEqual(record['candidate'], original)
        self.assertEqual(record['binding']['candidateSha256'], json_fingerprint(original))
        self.assertEqual(record['binding']['basisSha256'], json_fingerprint(f.basis))
        self.assertEqual(record['modelCallSequences'], [])
        self.assertIsNone(record['result'])
        self.assertFalse(record['isRequirementApproval'])
        self.assertFalse(record['isProductSuccess'])
        self.assertEqual(f.store.latest(f.execution.metadata.run_id, f.execution.metadata.workflow_step_id,
                                       f.context.task_id), record)
        f.provider.complete.assert_not_awaited()
        f.execution.budget.reserve_model_call.assert_not_called()
        f.execution.budget.check.assert_called_once()
        usage_sink.assert_not_called()
        started_sink.assert_not_called()
        self.assertEqual((f.execution.budget.model_calls, f.execution.budget.total_tokens), (1, 37))
        self.assertEqual(candidate, original)

    async def test_no_free_text_claims_need_no_review_call_and_are_not_product_approval(self):
        f = self.fixture()
        candidate = {'status': 'READY', 'count': 3}
        result = LLMResult(data_json=json_text(candidate), records=(), tool_calls=0)
        usage_sink, started_sink = Mock(), Mock()
        with patch('agents.semantic_review.approved_basis', return_value=f.basis):
            record = await f.reviewer.check(context=f.context, execution=f.execution, candidate=candidate,
                role=AgentRole.PLANNER, result=result, usage_sink=usage_sink, call_started_sink=started_sink)
        self.assertEqual((record['status'], record['reasonCode']), ('SUPPORTED', 'NO_FREE_TEXT_CLAIMS'))
        self.assertEqual(record['validation'], {'category': 'NONE', 'issues': []})
        self.assertEqual(record['modelCallSequences'], [])
        self.assertFalse(record['isRequirementApproval'])
        self.assertFalse(record['isProductSuccess'])
        self.assertEqual(f.store.latest(f.execution.metadata.run_id, f.execution.metadata.workflow_step_id,
                                       f.context.task_id), record)
        f.provider.complete.assert_not_awaited()
        f.execution.budget.reserve_model_call.assert_not_called()
        f.execution.budget.check.assert_called_once()
        usage_sink.assert_not_called()
        started_sink.assert_not_called()
        self.assertEqual((f.execution.budget.model_calls, f.execution.budget.total_tokens), (1, 37))


if __name__ == '__main__':
    unittest.main()
