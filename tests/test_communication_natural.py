"""No paid calls: raw-input isolation, grounded checks and native A2A wiring."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

import httpx
from pydantic import SecretStr

from diagnostics.communication.natural import (
    ASSETS, CASE_IDS, NaturalRecord, NaturalRuntime, content_screen,
    load_case, output_contract, role_prompt, run, screen_case,
)


MODEL = "gpt-4.1-mini-2025-04-14"


def answer(request_text):
    return {"requestEcho": request_text, "summary": "합성 단위 검사",
            "requirements": [{"id": "R1", "text": "원문 검토", "sourceQuote": request_text}],
            "assumptions": [], "questions": ["미정인 내용을 알려주세요."], "checks": [],
            "roleWork": {role: "" for role in ("PLANNER", "DEVELOPER", "QA", "SECURITY")},
            "executionState": "NOT_EXECUTED"}


class NaturalContentTests(unittest.TestCase):
    def test_schema_contains_no_case_answers(self):
        schema=output_contract()
        schema.schema.require_openai_strict()
        for value in ['"const"','"enum"','20MB','12명','PDF','01-blog']:
            self.assertNotIn(value,schema.schema.schema_json)
            self.assertNotIn(value,role_prompt('PLANNER'))

    def test_invented_quote_and_execution_claim_are_not_repaired(self):
        request=load_case('02-documents')
        result=answer(request)
        result['requirements'][0]['sourceQuote']='사용자는 30일 자동 삭제를 확정했다'
        result['executionState']='ALL_TESTS_PASS'
        screen=content_screen({'answer':result},{'requestText':request})
        self.assertEqual(screen['status'],'FAIL')
        self.assertEqual({item['kind'] for item in screen['differences']},
                         {'UNSUPPORTED_EXECUTION_CLAIM','UNSUPPORTED_SOURCE_QUOTE'})
        self.assertEqual(result['executionState'],'ALL_TESTS_PASS')

    def test_case_coverage_cannot_establish_semantic_truth(self):
        request=load_case('02-documents')
        result=answer(request)
        # A literal quote alone cannot prove the generated interpretation.
        result['requirements'][0]['text']='모든 사용자가 모든 문서를 본다'
        row=NaturalRecord('PLANNER',1,0)
        row.payload={'requestText':request,'upstream':None}
        row.response_received={'answer':result}
        criteria=json.loads(ASSETS.joinpath('criteria.json').read_text())['cases']['02-documents']
        screen=screen_case({'rows':[row.view()]},criteria)[0]
        self.assertEqual(screen['status'],'SCREEN_PASS')
        self.assertEqual(screen['semanticCorrectness'],'REQUIRES_REVIEW')

    def test_short_exact_quote_is_not_reported_as_invented(self):
        request='15분 제한의 게임을 만들어줘.'
        result=answer(request)
        result['requirements'][0]['sourceQuote']='15분'
        self.assertEqual(content_screen({'answer':result},{'requestText':request})['status'],'PASS')

    def test_handoff_id_changes_are_flagged_for_review(self):
        request=load_case('01-blog')
        row=NaturalRecord('DEVELOPER',1,0)
        row.payload={'requestText':request,'upstream':{'answer':answer(request)}}
        changed=answer(request);changed['requirements'][0]['id']='R2'
        row.response_received={'answer':changed}
        criteria=json.loads(ASSETS.joinpath('criteria.json').read_text())['cases']['01-blog']
        screen=screen_case({'rows':[row.view()]},criteria)[0]
        self.assertEqual(screen['status'],'REVIEW')
        self.assertEqual(screen['lostUpstreamIds'],['R1'])


class NaturalWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifteen_bounded_calls_with_raw_text_no_hidden_rubric(self):
        seen=[]
        def respond(request):
            body=json.loads(request.content)
            received=json.loads(body['input'][0]['content'])['received']
            seen.append(received)
            self.assertEqual(set(received),{'probeId','caseId','requestText','upstream'})
            self.assertEqual(received['requestText'],load_case(received['caseId']))
            self.assertEqual(body['tools'],[])
            self.assertFalse(body['store'])
            return httpx.Response(200,json={'id':'resp_synthetic','status':'completed','model':MODEL,
                'output':[{'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                           'content':[{'type':'output_text','text':json.dumps(answer(received['requestText']),ensure_ascii=False),'annotations':[]}]}],
                'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}})
        runtime=NaturalRuntime(SecretStr('SYNTHETIC_TEST_ONLY'),MODEL,max_model_calls=15,
                               transport_factory=lambda:httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            report=await run(SimpleNamespace(model=MODEL,out=Path(directory)/'evidence'),runtime=runtime)
            saved=json.loads((Path(directory)/'evidence/run.json').read_text())
            self.assertEqual(len(saved['attempts']),15)
        self.assertEqual(len(seen),15)
        self.assertEqual(len(report['runs']),6)
        self.assertEqual(report['status'],'CAPTURED_REVIEW_REQUIRED')
        self.assertEqual(sum(r['transportPassed'] for r in report['runs']),15)
        for case_id in CASE_IDS:
            group=[item for item in seen if item['caseId']==case_id]
            self.assertEqual(len(group),5)
            self.assertEqual(sum(item['upstream'] is not None for item in group),3)
        self.assertNotIn('SYNTHETIC_TEST_ONLY',json.dumps(report))


if __name__=='__main__':
    unittest.main()
