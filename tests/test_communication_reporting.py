"""Offline provenance/report checks; no model calls or private evidence required."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from diagnostics.communication import provenance
from diagnostics.communication.reporting import compare_baseline, render_html, summarize_evidence, validate_review


def evidence():
    request = '휴대폰에서도 편한 블로그를 만들어줘.'
    return {'caseRequests': {'01-blog': request}, 'provenance': {'transportMode': 'CUSTOM_TRANSPORT'},
        'runs': [{'caseId': '01-blog', 'architecture': 'MULTI_AGENT', 'rows': [{
            'role': 'PLANNER', 'phase': 'COMPLETED',
            'checks': {'request': {'endToEnd': {'status': 'PASS'}}},
            'observations': {'responseReceived': {'answer': {
                'summary': '추가 확인 필요', 'roleWork': {'PLANNER': '계획', 'DEVELOPER': ''},
                'executionState': 'NOT_EXECUTED'}}}, 'timings': {'agentProcessingMs': 100}}]}]}


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'runtime.py').write_text('source = 1\n')
        (self.root / 'uv.lock').write_text('version = 1\n')
        (self.root / '.env').write_text('SECRET=NEVER_EXPORT_TEST_ONLY')
        selected = patch.object(provenance, 'EXECUTION_PATHS', ('runtime.py', 'uv.lock'))
        selected.start()
        self.addCleanup(selected.stop)
        git = patch.object(provenance, '_git', side_effect=lambda root, args: 'a' * 40 if args[0] == 'rev-parse' else ' M private-setting')
        git.start()
        self.addCleanup(git.stop)

    def capture(self, factory=None):
        return provenance.capture_provenance(SimpleNamespace(transport_factory=factory), root=self.root)

    def test_only_selected_hashes_are_exported_and_custom_transport_is_labeled(self):
        result = self.capture()
        self.assertEqual(set(result['sources']), {'runtime.py', 'uv.lock'})
        self.assertEqual(result['transportMode'], 'DEFAULT_HTTPS_TRANSPORT')
        self.assertTrue(result['worktreeDirty'])
        encoded = json.dumps(result, sort_keys=True)
        for forbidden in ('NEVER_EXPORT_TEST_ONLY', '.env', 'private-setting', 'source = 1'):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(result['sourceManifestSha256'], hashlib.sha256(
            json.dumps(result['sources'], sort_keys=True, separators=(',', ':')).encode()).hexdigest())
        self.assertEqual(self.capture(lambda: None)['transportMode'], 'CUSTOM_TRANSPORT')
        self.assertEqual(result['providerCacheState'], 'UNVERIFIED')
        self.assertEqual(result['comparison'], 'DESCRIPTIVE_NOT_CAUSAL')

    def test_changed_source_is_not_reported_as_stable(self):
        result = self.capture()
        provenance.finish_provenance(result, root=self.root)
        self.assertEqual(result['sourceStability'], 'STABLE')
        (self.root / 'runtime.py').write_text('source = 2\n')
        provenance.finish_provenance(result, root=self.root)
        self.assertEqual(result['sourceStability'], 'CHANGED')
        self.assertEqual(result['changedSourceFiles'], ['runtime.py'])

    def test_unavailable_source_is_unverified_and_error_does_not_expose_paths(self):
        result = self.capture()
        (self.root / 'runtime.py').unlink()
        provenance.finish_provenance(result, root=self.root)
        self.assertEqual(result['sourceStability'], 'UNVERIFIED')
        with self.assertRaisesRegex(ValueError, '^DIAGNOSTIC_SOURCE_MANIFEST_UNAVAILABLE$') as caught:
            self.capture()
        self.assertIsNone(caught.exception.__context__)

    def test_symlink_is_not_fingerprinted_as_an_execution_file(self):
        (self.root / 'runtime.py').unlink()
        (self.root / 'runtime.py').symlink_to(self.root / '.env')
        with self.assertRaises(ValueError):
            self.capture()


class ReportingTests(unittest.TestCase):
    def test_missing_observations_are_never_counted_as_matching(self):
        result = summarize_evidence(evidence())
        self.assertEqual(result['boundaries']['endToEnd'], {'passed': 1, 'failed': 0, 'unverified': 0})
        for name in ('receipt', 'modelInput', 'response', 'metadata', 'sourceGrounding'):
            self.assertEqual(result['boundaries'][name], {'passed': 0, 'failed': 0, 'unverified': 1})
        self.assertEqual(result['calls'], 0)
        self.assertEqual(result['notExecutedClaims'], 1)

    def test_skipped_rows_remain_distinct_from_failed_delivery(self):
        data = evidence()
        data['runs'][0]['rows'].append({'role': 'DEVELOPER', 'phase': 'SKIPPED'})
        result = summarize_evidence(data)
        self.assertEqual(result['skippedRows'], 1)
        self.assertEqual(result['rows'], 2)
        self.assertEqual(result['parsedAnswers'], 1)
        self.assertEqual(result['boundaries']['endToEnd']['failed'], 0)
        self.assertEqual(result['boundaries']['endToEnd']['unverified'], 1)

    def test_failed_samples_and_invalid_times_do_not_enter_completed_latency(self):
        data = evidence()
        rows = data['runs'][0]['rows']
        for phase, value in [('ERROR', 2), ('COMPLETED', -1), ('COMPLETED', float('nan')), ('COMPLETED', float('inf'))]:
            rows.append({'role': 'PLANNER', 'phase': phase, 'timings': {'agentProcessingMs': value}})
        result = summarize_evidence(data)['roles']['PLANNER']
        self.assertEqual(result['agentProcessingMs'], {'samples': 1, 'min': 100, 'median': 100, 'max': 100})
        self.assertEqual(result['providerExchangeMs']['samples'], 0)
        self.assertIsNone(result['providerExchangeMs']['median'])

    def test_cross_role_fields_and_execution_claims_are_preserved(self):
        data = evidence()
        answer = data['runs'][0]['rows'][0]['observations']['responseReceived']['answer']
        answer['roleWork']['DEVELOPER'] = '작성하지 않은 개발 결과'
        answer['executionState'] = 'ALL_TESTS_PASS'
        result = summarize_evidence(data)
        self.assertEqual(result['foreignRoleFields'], 1)
        self.assertEqual(result['notExecutedClaims'], 0)
        self.assertEqual(answer['executionState'], 'ALL_TESTS_PASS')

    def test_unknown_comparison_is_not_same_and_even_equal_inputs_do_not_prove_speedup(self):
        empty = compare_baseline({}, {})
        self.assertIsNone(empty['sameModel'])
        self.assertIsNone(empty['samePrompts'])
        before = {'modelId': 'fixed', 'temperature': 0, 'rolePrompts': {'PLANNER': 'before'}, 'schemaSha256': 'old'}
        after = {**before, 'rolePrompts': {'PLANNER': 'after'}, 'schemaSha256ByRole': {'PLANNER': 'new'}}
        comparison = compare_baseline(after, before)
        self.assertTrue(comparison['sameModel'])
        self.assertTrue(comparison['sameTemperature'])
        self.assertFalse(comparison['samePrompts'])
        self.assertFalse(comparison['sameSchemas'])
        self.assertFalse(compare_baseline(before, before)['causalSpeedupAllowed'])

    def test_synthetic_report_escapes_content_and_does_not_claim_actual_api_execution(self):
        data = evidence()
        data['caseRequests']['01-blog'] = '<script>notTrusted()</script>'
        original = deepcopy(data)
        html = render_html(data, raw_path='offline.json', raw_sha256='a' * 64, review={})
        self.assertIn('&lt;script&gt;notTrusted()&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('실제 OpenAI API 실행 결과', html)
        self.assertIn('실제 API 결과로 확정하지 않음', html)
        self.assertEqual(html.count('<section class="page">'), 6)
        self.assertEqual(data, original)

    def test_review_excerpt_must_exist_in_the_exact_case_architecture_and_role(self):
        data = evidence()
        item = {'caseId': '01-blog', 'architecture': 'MULTI_AGENT', 'role': 'PLANNER',
            'sourceQuote': '휴대폰에서도', 'answerQuote': '추가 확인 필요'}
        validate_review(data, {'caseReviews': [item]})
        for changes in ({'sourceQuote': '원문에 없는 문장'}, {'answerQuote': '없는 응답'},
                        {'role': 'SECURITY'}, {'architecture': 'SINGLE_AGENT'}, {'caseId': 'unknown'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_review(data, {'caseReviews': [{**item, **changes}]})


if __name__ == '__main__':
    unittest.main()
