"""Real localhost A2A + controlled API transports; no paid model requests."""

from copy import deepcopy
import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from diagnostics.communication import guarded
from diagnostics.communication.guarded_contracts import content_screen, output_contract, role_prompt
from diagnostics.communication.semantic_review import bind_review, review_contract, review_matches, review_fields


MODEL = 'gpt-4.1-mini-2025-04-14'


def answer(text, role='PLANNER'):
    return {'requestEcho': text, 'summary': '원문 조건을 검토합니다.',
        'requirements': [{'id': 'R1', 'text': text, 'sourceQuote': text, 'sourceKind': 'EXPLICIT'}],
        'assumptions': [], 'assumptionStatus': 'UNAPPROVED', 'questions': [], 'decision': 'READY',
        'checks': [{'name': '요청 조건 확인', 'requirementId': 'R1', 'expected': text, 'sourceQuote': text}],
        'roleWork': {key: '원문 범위만 검토합니다.' if role in (key, 'SINGLE_AGENT') else ''
            for key in ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY')}, 'executionState': 'NOT_EXECUTED'}


def screen(value, role='PLANNER', upstream=None):
    return content_screen({'answer': value}, {'requestText': value['requestEcho'], 'upstream': upstream}, role)


def critique(judgment='SUPPORTED', findings=None, *, candidate=None, request_text=None):
    candidate = candidate or answer('위치 정보는 수집하지 마.')
    request_text = request_text or candidate['requestEcho']
    findings = findings or []
    return {'judgment': judgment, 'findings': findings,
        'fieldReviews': {pointer: {'status': 'UNSUPPORTED' if judgment == 'UNSUPPORTED'
            and any(item['fieldPath'] == pointer for item in findings) else
            'UNVERIFIED' if judgment == 'UNVERIFIED' else 'SUPPORTED',
            'statementQuote': text, 'requestQuote': request_text, 'reason': '합성 검토 근거'}
            for pointer, text in review_fields(candidate).items()}}


def finding(text, *, field='/summary', quote='원문 조건을 검토합니다.'):
    return {'fieldPath': field, 'statementQuote': quote, 'requestQuote': text,
        'category': 'SCOPE_EXPANSION', 'reason': '합성 오류 주입용 짧은 근거'}


class GuardedContractTests(unittest.TestCase):
    def test_all_five_closed_contracts_and_no_case_answers_in_prompts(self):
        for role in ('PLANNER', 'DEVELOPER', 'QA', 'SECURITY', 'SINGLE_AGENT'):
            contract = output_contract(role)
            contract.schema.require_openai_strict()
            value = answer('회사 화면을 만들어줘.', role)
            contract.schema.validate(value)
            self.assertEqual(screen(value, role)['status'], 'PASS')
            self.assertEqual(contract.name, 'natural_language_handoff_v3_' + role.lower())
            self.assertNotIn('20MB', role_prompt(role))
        review_contract().schema.require_openai_strict()

    def test_real_quote_cannot_legitimize_a_paraphrased_requirement(self):
        value = answer('처음에는 동아리 사람들이 사용할 거야.')
        value['requirements'][0]['text'] = '동아리 회원만 사용할 수 있다.'
        self.assertIn({'kind': 'REQUIREMENT_NOT_LITERAL', 'index': 0}, screen(value)['differences'])

    def test_real_quote_cannot_legitimize_an_invented_expectation(self):
        value = answer('여러 명이 소통할 블로그를 만들어줘.')
        value['checks'][0]['expected'] = '댓글과 동시 작성을 지원해야 한다.'
        original = deepcopy(value)
        result = screen(value)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('UNBOUND_OR_CHANGED_EXPECTATION', [v['kind'] for v in result['differences']])
        self.assertEqual(value, original)

    def test_unknown_reference_and_missing_coverage_are_rejected(self):
        for mutate in (lambda v: v['checks'][0].update(requirementId='UNKNOWN'), lambda v: v.update(checks=[])):
            value = answer('화면을 만들어줘.')
            mutate(value)
            self.assertEqual(screen(value)['status'], 'FAIL')

    def test_proposals_questions_and_undecided_items_cannot_claim_ready(self):
        for mutate in (lambda v: v.update(assumptions=['별도 기능 제안']),
                       lambda v: v.update(questions=['접근 권한을 제한할까요?']),
                       lambda v: (v['requirements'][0].update(sourceKind='UNDECIDED'), v.update(checks=[]))):
            value = answer('보관 기간은 아직 정하지 않았어.')
            mutate(value)
            self.assertIn({'kind': 'UNAPPROVED_READY_DECISION'}, screen(value)['differences'])

    def test_undecided_item_must_not_receive_a_fixed_test_expectation(self):
        value = answer('보관 기간은 아직 정하지 않았어.')
        value['requirements'][0]['sourceKind'] = 'UNDECIDED'
        value.update(decision='INPUT_REQUIRED', questions=['보관 기간을 알려주세요.'])
        self.assertEqual(screen(value)['status'], 'FAIL')
        value['checks'] = []
        self.assertEqual(screen(value)['status'], 'PASS')

    def test_input_required_needs_a_real_question(self):
        value = answer('화면을 만들어줘.')
        value['decision'] = 'INPUT_REQUIRED'
        self.assertIn({'kind': 'INPUT_REQUIRED_WITHOUT_QUESTION'}, screen(value)['differences'])

    def test_upstream_ids_order_text_and_kind_are_frozen(self):
        previous = answer('회사 화면을 만들어줘.')
        value = answer(previous['requestEcho'], 'DEVELOPER')
        self.assertEqual(screen(value, 'DEVELOPER', {'answer': previous})['status'], 'PASS')
        value['requirements'][0]['id'] = 'R2'
        value['checks'][0]['requirementId'] = 'R2'
        self.assertIn({'kind': 'UPSTREAM_REQUIREMENTS_CHANGED'}, screen(value, 'DEVELOPER', {'answer': previous})['differences'])

    def test_foreign_role_schema_and_unsupported_execution_remain_closed(self):
        for changes in ({'executionState': 'PASS'}, {'assumptionStatus': 'APPROVED'}, {'extra': True}):
            value = {**answer('화면을 만들어줘.'), **changes}
            self.assertEqual(screen(value)['status'], 'FAIL')
        value = answer('화면을 만들어줘.')
        value['roleWork']['SECURITY'] = '다른 역할도 완료'
        self.assertEqual(screen(value)['status'], 'FAIL')


class SemanticBindingTests(unittest.TestCase):
    def setUp(self):
        self.text = '위치 정보는 수집하지 마.'
        self.candidate = answer(self.text)
        self.context = dict(candidate=self.candidate, request_text=self.text, role='PLANNER', probe_id='probe-1')

    def test_supported_receipt_is_bound_to_exact_material_not_product_approval(self):
        value = bind_review(critique(), **self.context)
        self.assertTrue(review_matches(value, **self.context))
        self.assertFalse(value['isProductApproval'])
        for changes in ({'request_text': self.text + ' 바뀜'}, {'role': 'SECURITY'}, {'probe_id': 'probe-2'},
                        {'candidate': {**self.candidate, 'summary': '바뀜'}}):
            self.assertFalse(review_matches(value, **{**self.context, **changes}))

    def test_scope_expansion_witness_is_bound_to_actual_free_text(self):
        self.candidate['roleWork']['PLANNER'] = '개인정보는 전혀 수집하지 않습니다.'
        result = critique('UNSUPPORTED', [finding(self.text, field='/roleWork/PLANNER', quote='개인정보는 전혀 수집하지 않습니다.')], candidate=self.candidate)
        value = bind_review(result, **self.context)
        self.assertEqual(value['status'], 'REJECTED')
        self.assertEqual(value['result'], result)

    def test_missing_uncertain_or_contradictory_review_is_not_passed(self):
        for raw in (None, {}, critique('UNVERIFIED'), critique('UNSUPPORTED'),
                    critique('SUPPORTED', [finding(self.text)])):
            self.assertEqual(bind_review(raw, **self.context)['status'], 'UNVERIFIED')

    def test_invented_witness_wrong_path_or_blank_quote_is_unverified(self):
        for changes in ({'statementQuote': '응답에 없는 내용'}, {'requestQuote': '수집을 허용'},
                        {'fieldPath': '/missing'}, {'fieldPath': '/requirements'}, {'statementQuote': ' '}):
            item = {**finding(self.text), **changes}
            self.assertEqual(bind_review(critique('UNSUPPORTED', [item]), **self.context)['status'], 'UNVERIFIED')

    def test_altering_result_after_binding_cannot_reuse_supported_status(self):
        value = bind_review(critique(), **self.context)
        value['result'] = critique('UNSUPPORTED')
        self.assertFalse(review_matches(value, **self.context))

    def test_supported_without_complete_field_evidence_is_unverified(self):
        raw = critique()
        for mutate in (lambda value: value['fieldReviews'].pop('/summary'),
                       lambda value: value['fieldReviews']['/summary'].update(statementQuote='일부만'),
                       lambda value: value['fieldReviews']['/summary'].update(requestQuote='없는 원문'),
                       lambda value: value['fieldReviews']['/summary'].update(status='UNSUPPORTED')):
            value = deepcopy(raw)
            mutate(value)
            self.assertEqual(bind_review(value, **self.context)['status'], 'UNVERIFIED')
        self.assertEqual(bind_review({'judgment': 'SUPPORTED', 'findings': []}, **self.context)['status'], 'UNVERIFIED')

    def test_model_supported_broad_privacy_claim_is_held_without_explicit_request(self):
        self.candidate['roleWork']['PLANNER'] = '개인정보는 전혀 수집하지 않으므로 식별 정보도 저장하지 않는다.'
        raw = critique(candidate=self.candidate)
        value = bind_review(raw, **self.context)
        self.assertEqual(value['status'], 'UNVERIFIED')
        self.assertEqual(value['reason'], 'UNAPPROVED_DATA_SCOPE_REQUIRES_REVIEW')
        self.assertEqual(value['result']['judgment'], 'SUPPORTED')
        self.assertFalse(review_matches(value, **self.context))

    def test_explicit_user_information_exclusion_does_not_bypass_model_review(self):
        text = '사용자 정보 수집 기능은 넣지 마.'
        candidate = answer(text)
        candidate['roleWork']['PLANNER'] = '개인정보는 수집하지 않는다.'
        context = {**self.context, 'candidate': candidate, 'request_text': text}
        self.assertEqual(bind_review(critique(candidate=candidate), **context)['status'], 'SUPPORTED')
        self.assertEqual(bind_review(None, **context)['status'], 'UNVERIFIED')


class GuardedWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_case_budget_includes_all_prior_calls_before_network(self):
        with TemporaryDirectory() as directory:
            prior, out = Path(directory)/'prior.json', Path(directory)/'evidence'
            prior.write_text(json.dumps({'calls': [{} for _ in range(31)]}))
            args = SimpleNamespace(model=MODEL, out=out, case_ids=['04-office-page'], prior_runs=[prior])
            with self.assertRaisesRegex(ValueError, 'COMBINED_DIAGNOSTIC_CALL_CAP_EXCEEDED'):
                await guarded.run(args)
            self.assertFalse(out.exists())

    async def test_unknown_or_duplicate_case_selection_is_rejected(self):
        for selected in (['unknown'], ['04-office-page', '04-office-page']):
            with TemporaryDirectory() as directory:
                with self.assertRaisesRegex(ValueError, 'INVALID_GUARDED_CASE_SELECTION'):
                    await guarded.run(SimpleNamespace(model=MODEL, out=Path(directory)/'evidence', case_ids=selected))

    async def test_reusing_runtime_budget_is_rejected_before_creating_evidence(self):
        runtime = guarded.GuardedRuntime(SecretStr('SYNTHETIC_TEST_ONLY'), MODEL, max_model_calls=20)
        runtime.calls.append({'status': 'PREVIOUS_RUN'})
        with TemporaryDirectory() as directory:
            out = Path(directory)/'evidence'
            with self.assertRaisesRegex(ValueError, 'GUARDED_RUNTIME_ALREADY_USED'):
                await guarded.run(SimpleNamespace(model=MODEL, out=out), runtime=runtime)
            self.assertFalse(out.exists())

    async def test_canceled_review_stays_unverified_and_preserves_cancel(self):
        runtime = guarded.GuardedRuntime(SecretStr('SYNTHETIC_TEST_ONLY'), MODEL, max_model_calls=20,
            transport_factory=lambda: httpx.MockTransport(lambda request: httpx.Response(500)))
        runtime.evidence, runtime.checkpoint = {'semanticReviews': []}, lambda: None
        row = guarded.GuardedRecord('PLANNER', 1, 0)
        value = answer('회사 화면을 만들어줘.')
        payload = {'requestText': value['requestEcho'], 'upstream': None}
        with patch.object(runtime.semantic_runtime, 'complete', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await runtime.review(payload, row, value)
        self.assertEqual(row.semantic_review['status'], 'UNVERIFIED')
        self.assertEqual(row.semantic_review['errorCode'], 'REVIEW_CANCELED')
        self.assertIsNone(row.semantic_review['model'])

    async def test_no_review_or_missing_api_input_proof_cannot_enable_handoff(self):
        row = guarded.GuardedRecord('PLANNER', 1, 0)
        value = answer('회사 화면을 만들어줘.')
        row.payload = {'requestText': value['requestEcho']}
        row.response_received = {'answer': value}
        receipt = bind_review(critique(candidate=value), candidate=value, request_text=value['requestEcho'], role=row.role, probe_id=row.probe_id)
        base = {'handoff': {'eligible': True, 'reason': 'CONTRACT_ADMITTED'}}
        for review in (None, receipt, {**receipt, 'model': {'status': 'COMPLETED', 'httpStatus': 200, 'apiInputMatches': False}}):
            row.semantic_review = review
            with patch.object(guarded.NaturalRecord, 'view', return_value=deepcopy(base)):
                result = row.view()
            self.assertFalse(result['handoff']['eligible'])
            self.assertEqual(result['handoff']['reason'], 'SEMANTIC_REVIEW_UNVERIFIED')

    async def test_reusing_output_directory_does_not_make_api_calls(self):
        seen = []
        runtime = guarded.GuardedRuntime(SecretStr('SYNTHETIC_TEST_ONLY'), MODEL, max_model_calls=20,
            transport_factory=lambda: httpx.MockTransport(lambda request: seen.append(request)))
        with TemporaryDirectory() as directory:
            with self.assertRaises(FileExistsError):
                await guarded.run(SimpleNamespace(model=MODEL, out=Path(directory)), runtime=runtime)
        self.assertEqual(seen, [])

    async def execute(self, mutate=None, review=None, *, review_http_error=None):
        seen = []
        def respond(request):
            body = json.loads(request.content)
            payload = json.loads(body['input'][0]['content'])['received']
            contract = body['text']['format']['name']
            self.assertEqual(body['tools'], [])
            self.assertFalse(body['store'])
            seen.append((contract, deepcopy(payload)))
            if contract == 'natural_semantic_review_v3':
                self.assertEqual(set(payload), {'requestText', 'candidate'})
                if review_http_error:
                    return httpx.Response(review_http_error, json={'error': {'code': 'invalid_api_key'}})
                result = review(payload) if review else critique(candidate=payload['candidate'])
            else:
                self.assertEqual(set(payload), {'probeId', 'caseId', 'requestText', 'upstream'})
                role = contract.removeprefix('natural_language_handoff_v3_').upper()
                self.assertEqual(payload['requestText'], guarded.load_case(payload['caseId']))
                result = answer(payload['requestText'], role)
                if mutate:
                    mutate(result, role)
            return httpx.Response(200, headers={'x-request-id': 'synthetic_' + str(len(seen))}, json={
                'id': 'resp_synthetic', 'status': 'completed', 'model': MODEL,
                'output': [{'id': 'msg_synthetic', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': json.dumps(result, ensure_ascii=False), 'annotations': []}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 10, 'total_tokens': 20}})
        runtime = guarded.GuardedRuntime(SecretStr('SYNTHETIC_TEST_ONLY'), MODEL, max_model_calls=20,
            transport_factory=lambda: httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            report = await guarded.run(SimpleNamespace(model=MODEL, out=Path(directory)/'evidence'), runtime=runtime)
            saved = json.loads((Path(directory)/'evidence/run.json').read_text())
            self.assertEqual(report, saved)
        self.assertNotIn('SYNTHETIC_TEST_ONLY', json.dumps(report))
        return report, seen

    async def test_twenty_answers_and_twenty_reviews_are_separate_bounded_calls(self):
        report, seen = await self.execute()
        self.assertEqual(len(seen), 40)
        self.assertEqual(report['summary']['completedRows'], 20)
        self.assertEqual(report['summary']['handoffEligibleRows'], 20)
        self.assertEqual(report['summary']['roleCalls'], 20)
        self.assertEqual(report['summary']['reviewCalls'], 20)
        self.assertEqual(report['status'], 'CAPTURED_REVIEW_REQUIRED')
        self.assertEqual(report['provenance']['sourceStability'], 'STABLE')
        self.assertEqual(report['provenance']['caseOrder'], list(guarded.CASE_IDS))
        for group in report['runs']:
            for row in group['rows']:
                self.assertFalse(row['semanticReview']['isProductApproval'])
                self.assertEqual(row['semanticReview']['model']['apiInputMatches'], True)
                self.assertEqual(row['handoff']['authorization'], 'DIAGNOSTIC_ONLY_NOT_PRODUCT_APPROVAL')

    async def test_input_required_blocks_developer_without_automatic_approval(self):
        def mutate(value, role):
            if role == 'PLANNER':
                value.update(decision='INPUT_REQUIRED', assumptions=['기능 제안은 미승인'], questions=['기능 범위를 알려주세요.'])
        report, _ = await self.execute(mutate=mutate)
        self.assertEqual(report['summary']['inputRequiredRows'], 4)
        self.assertEqual(report['summary']['skippedRows'], 12)
        self.assertEqual(report['summary']['roleCalls'], 8)
        self.assertEqual(report['summary']['reviewCalls'], 8)
        self.assertEqual(report['status'], 'CAPTURED_WITH_POLICY_BLOCKS')
        for group in report['runs']:
            if group['architecture'] == 'MULTI_AGENT':
                self.assertTrue(all(row['error'] == 'UPSTREAM_INPUT_REQUIRED' for row in group['rows'][1:]))

    async def test_semantic_rejection_preserves_original_and_skips_downstream(self):
        def mutate(value, role):
            if role == 'PLANNER':
                value['summary'] = '개인정보는 수집하지 않는다고 단정한다.'
        def review(payload):
            if payload['candidate']['summary'].startswith('개인정보'):
                return critique('UNSUPPORTED', [finding(payload['requestText'], quote=payload['candidate']['summary'])], candidate=payload['candidate'])
            return critique(candidate=payload['candidate'])
        report, _ = await self.execute(mutate=mutate, review=review)
        self.assertEqual(report['summary']['modelReviewRejected'], 4)
        self.assertEqual(report['summary']['transportMatchedRows'], 8)
        self.assertEqual(report['summary']['skippedRows'], 12)
        for group in report['runs']:
            if group['architecture'] == 'MULTI_AGENT':
                row = group['rows'][0]
                self.assertEqual(row['observations']['responseReceived']['answer']['summary'], '개인정보는 수집하지 않는다고 단정한다.')
                self.assertEqual(row['handoff']['reason'], 'SEMANTIC_REVIEW_REJECTED')

    async def test_uncertain_review_does_not_enable_handoff(self):
        report, _ = await self.execute(review=lambda payload: critique('UNVERIFIED', candidate=payload['candidate']))
        self.assertEqual(report['summary']['modelReviewUnverified'], 8)
        self.assertEqual(report['summary']['handoffEligibleRows'], 0)
        self.assertEqual(report['summary']['skippedRows'], 12)

    async def test_deterministic_failure_does_not_pay_for_a_semantic_review(self):
        def mutate(value, role):
            if role == 'PLANNER':
                value['checks'][0]['expected'] = '원문에 없는 필수 기능'
        report, _ = await self.execute(mutate=mutate)
        self.assertEqual(report['summary']['literalContractRejected'], 4)
        self.assertEqual(report['summary']['roleCalls'], 8)
        self.assertEqual(report['summary']['reviewCalls'], 4)
        self.assertEqual(len(report['calls']), 12)

    async def test_fatal_review_auth_error_stops_later_calls(self):
        report, seen = await self.execute(review_http_error=401)
        self.assertEqual(len(seen), 2)
        self.assertEqual(len(report['notStarted']), 7)
        self.assertEqual(report['status'], 'FAIL_OR_INCOMPLETE')
        self.assertEqual(report['fatalError'], 'invalid_api_key')

    async def test_candidate_changed_after_review_cannot_reuse_the_receipt(self):
        original = guarded.GuardedExecutor
        class TamperingExecutor(original):
            async def make_result(self, payload, row):
                result = await super().make_result(payload, row)
                result['answer']['summary'] = '검토 이후 변경한 문장'
                return result
        with patch.object(guarded, 'GuardedExecutor', TamperingExecutor):
            report, _ = await self.execute()
        self.assertEqual(report['summary']['handoffEligibleRows'], 0)
        self.assertEqual(report['summary']['transportMatchedRows'], 8)
        self.assertEqual(report['summary']['skippedRows'], 12)


if __name__ == '__main__':
    unittest.main()
