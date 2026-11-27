"""Offline report keeps failed calibration, skipped work and real-call scope."""

from copy import deepcopy
import unittest

from diagnostics.communication.guarded_reporting import render, summary


def data():
    row = {'role': 'PLANNER', 'phase': 'COMPLETED', 'error': None, 'model': {'apiInputMatches': True},
        'handoff': {'eligible': True, 'reason': 'DIAGNOSTIC_CONTRACT_AND_MODEL_REVIEW_ADMITTED'},
        'checks': {'identity': 'PASS', 'request': {'endToEnd': {'status': 'PASS'}},
            'response': {'status': 'PASS'}, 'metadata': {'status': 'PASS'}, 'responseMetadata': {'status': 'PASS'}},
        'delivery': {'check': {'status': 'PASS'}}, 'timings': {'agentProcessingMs': 100, 'providerExchangeMs': 70},
        'semanticReview': {'model': {'timings': {'providerExchangeMs': 20}}}}
    call = {'httpStatus': 200, 'usage': {'input_tokens': 10, 'output_tokens': 2}}
    initial = {'modelId': 'fixed-model', 'temperature': 0, 'maxOutputTokens': 8192, 'reviewMaxOutputTokens': 2048,
        'caseRequests': {'04-office-page': '<script>untrusted()</script>'}, 'provenance': {'transportMode': 'CUSTOM_TRANSPORT'},
        'roleCalls': [call], 'reviewCalls': [call], 'calls': [call, call],
        'runs': [{'caseId': '04-office-page', 'architecture': 'MULTI_AGENT', 'elapsedMs': 110, 'rows': [row]}],
        'summary': {'completedRows': 1, 'transportMatchedRows': 1, 'inputRequiredRows': 0,
            'literalContractRejected': 0, 'handoffEligibleRows': 1, 'skippedRows': 0}}
    failed = {'calls': [call], 'provenance': {'transportMode': 'CUSTOM_TRANSPORT'},
        'results': [{'caseId': 'injected-privacy-expansion', 'expected': 'REJECTED',
            'review': {'status': 'SUPPORTED'}, 'matchedExpected': False}]}
    return initial, [failed], deepcopy(initial)


class GuardedReportingTests(unittest.TestCase):
    def test_skipped_work_is_not_a_failed_or_successful_transmission(self):
        first, controls, final = data()
        first['runs'][0]['rows'].append({'role': 'DEVELOPER', 'phase': 'SKIPPED', 'error': 'UPSTREAM_INPUT_REQUIRED',
            'handoff': {'eligible': False, 'reason': 'NOT_DISPATCHED'}})
        result = summary(first, controls, final)
        self.assertEqual(result['calls'], 5)
        self.assertEqual(result['completedRows'], 2)
        self.assertEqual(result['matchedRows'], 2)
        self.assertEqual(result['skippedRows'], 1)
        first['runs'][0]['rows'][0]['checks']['identity'] = 'UNVERIFIED'
        self.assertEqual(summary(first, controls, final)['matchedRows'], 1)

    def test_failed_control_and_unsafe_transport_label_are_visible_and_input_is_unchanged(self):
        first, controls, final = data()
        before = deepcopy((first, controls, final))
        html = render(first, controls, final, [('source', 'input.json', 'a'*64)])
        self.assertEqual(html.count('<section class="page">'), 8)
        self.assertIn('불일치', html)
        self.assertIn('잘못 허용/보류', html)
        self.assertIn('합성/미확인 Transport', html)
        self.assertIn('&lt;script&gt;untrusted()&lt;/script&gt;', html)
        self.assertNotIn('<script>', html)
        self.assertEqual((first, controls, final), before)

    def test_combined_count_over_forty_is_not_rendered_as_valid(self):
        first, controls, final = data()
        first['calls'] = [{} for _ in range(40)]
        with self.assertRaisesRegex(ValueError, 'REPORT_COMBINED_CALL_CAP_EXCEEDED'):
            summary(first, controls, final)


if __name__ == '__main__':
    unittest.main()
