"""Control labels never enter the semantic model input; no paid calls."""

from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import httpx
from pydantic import SecretStr

from diagnostics.communication.calibrate import cases, run
from diagnostics.communication.semantic_review import SemanticRuntime
from test_communication_guarded import MODEL, answer, critique


def main_evidence(call_count=19):
    text = '회사 소개 화면 하나만 만들고 개인정보는 수집하지 마.'
    return {'modelId': MODEL, 'temperature': 0, 'calls': [{} for _ in range(call_count)],
        'caseRequests': {'04-office-page': text, '03-mystery': '위치 정보 수집은 필요 없어.', '01-blog': '동아리 사람들이 소통할 블로그를 만들어줘.'},
        'runs': [{'caseId': '04-office-page', 'architecture': 'MULTI_AGENT', 'rows': [{'role': 'PLANNER',
            'observations': {'responseReceived': {'answer': answer(text)}}}]}]}


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_controls_are_structurally_valid_and_do_not_modify_recorded_evidence(self):
        main = main_evidence()
        original = deepcopy(main)
        controls = cases(main)
        self.assertEqual(main, original)
        self.assertEqual([v['expected'] for v in controls], ['SUPPORTED', 'REJECTED', 'REJECTED'])
        self.assertTrue(all(v['kind'].startswith('HOST_INJECTED') for v in controls[1:]))

    async def test_labels_are_not_sent_and_wrong_model_judgments_are_not_repaired(self):
        seen = []
        def respond(request):
            body = json.loads(request.content)
            received = json.loads(body['input'][0]['content'])['received']
            self.assertEqual(set(received), {'requestText', 'candidate'})
            seen.append(received)
            return httpx.Response(200, json={'id': 'resp_test', 'status': 'completed', 'model': MODEL,
                'output': [{'id': 'msg_test', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                    'content': [{'type': 'output_text', 'text': json.dumps(critique(candidate=received['candidate'])), 'annotations': []}]}],
                'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2}})
        runtime = SemanticRuntime(SecretStr('SYNTHETIC_TEST_ONLY'), MODEL, max_model_calls=3,
            transport_factory=lambda: httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            source, out = Path(directory)/'main.json', Path(directory)/'calibration.json'
            source.write_text(json.dumps(main_evidence()))
            result = await run(source, out, runtime=runtime)
        self.assertEqual(len(seen), 3)
        self.assertEqual(result['combinedCallCount'], 22)
        self.assertEqual([v['matchedExpected'] for v in result['results']], [True, False, False])

    async def test_shared_forty_call_cap_is_checked_before_network_or_output(self):
        with TemporaryDirectory() as directory:
            source, out = Path(directory)/'main.json', Path(directory)/'calibration.json'
            source.write_text(json.dumps(main_evidence(38)))
            with self.assertRaisesRegex(ValueError, 'COMBINED_DIAGNOSTIC_CALL_CAP_EXCEEDED'):
                await run(source, out)
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
