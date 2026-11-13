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


def answer(request_text, role="PLANNER"):
    return {"requestEcho": request_text, "summary": "합성 단위 검사",
            "requirements": [{"id": "R1", "text": "원문 검토", "sourceQuote": request_text}],
            "assumptions": [], "questions": ["미정인 내용을 알려주세요."], "checks": [],
            "roleWork": {name: "현재 역할의 검토 내용" if role in (name, "SINGLE_AGENT") else ""
                         for name in ("PLANNER", "DEVELOPER", "QA", "SECURITY")},
            "executionState": "NOT_EXECUTED"}


class NaturalContentTests(unittest.TestCase):
    def test_schema_contains_no_case_answers(self):
        schema=output_contract("PLANNER")
        schema.schema.require_openai_strict()
        for value in ['"const"','20MB','12명','PDF','01-blog']:
            self.assertNotIn(value,schema.schema.schema_json)
            self.assertNotIn(value,role_prompt('PLANNER'))

    def test_invented_quote_and_execution_claim_are_not_repaired(self):
        request=load_case('02-documents')
        result=answer(request)
        result['requirements'][0]['sourceQuote']='사용자는 30일 자동 삭제를 확정했다'
        result['executionState']='ALL_TESTS_PASS'
        screen=content_screen({'answer':result},{'requestText':request},'PLANNER')
        self.assertEqual(screen['status'],'FAIL')
        self.assertEqual({item['kind'] for item in screen['differences']},
                         {'OUTPUT_SCHEMA_INVALID','UNSUPPORTED_EXECUTION_CLAIM','UNSUPPORTED_SOURCE_QUOTE'})
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
        self.assertEqual(content_screen({'answer':result},{'requestText':request},'PLANNER')['status'],'PASS')

    def test_handoff_id_changes_are_flagged_for_review(self):
        request=load_case('01-blog')
        row=NaturalRecord('DEVELOPER',1,0)
        row.payload={'requestText':request,'upstream':{'answer':answer(request)}}
        changed=answer(request, 'DEVELOPER');changed['requirements'][0]['id']='R2'
        row.response_received={'answer':changed}
        criteria=json.loads(ASSETS.joinpath('criteria.json').read_text())['cases']['01-blog']
        screen=screen_case({'rows':[row.view()]},criteria)[0]
        self.assertEqual(screen['status'],'REVIEW')
        self.assertEqual(screen['lostUpstreamIds'],['R1'])

    def test_each_role_has_a_closed_schema_and_single_keeps_all_four_views(self):
        for role in ('PLANNER','DEVELOPER','QA','SECURITY','SINGLE_AGENT'):
            with self.subTest(role=role):
                contract=output_contract(role)
                contract.schema.require_openai_strict()
                result=answer('회원가입 기능을 만들어줘.',role)
                contract.schema.validate(result)
                self.assertEqual(content_screen({'answer':result},{'requestText':result['requestEcho']},role)['status'],'PASS')
                self.assertEqual(contract.name,'natural_language_handoff_v2_'+role.lower())

    def test_foreign_role_fields_are_rejected_without_clearing_the_answer(self):
        from agents.llm.contracts import LLMRuntimeError
        result=answer('여러 명이 소통할 블로그를 만들어줘.','DEVELOPER')
        result['roleWork']['QA']='QA 검사도 완료했다고 가정한다.'
        with self.assertRaises(LLMRuntimeError):
            output_contract('DEVELOPER').schema.validate(result)
        screen=content_screen({'answer':result},{'requestText':result['requestEcho']},'DEVELOPER')
        self.assertEqual(screen['status'],'FAIL')
        self.assertIn({'kind':'FOREIGN_ROLE_WORK','field':'QA'},screen['differences'])
        self.assertEqual(result['roleWork']['QA'],'QA 검사도 완료했다고 가정한다.')

    def test_blank_active_work_and_missing_single_views_are_not_admitted(self):
        for role,field in (('PLANNER','PLANNER'),('SINGLE_AGENT','SECURITY')):
            result=answer('추리게임을 만들어줘.',role)
            result['roleWork'][field]='   '
            output_contract(role).schema.validate(result)
            screen=content_screen({'answer':result},{'requestText':result['requestEcho']},role)
            self.assertIn({'kind':'MISSING_ROLE_WORK','field':field},screen['differences'])

    def test_unknown_roles_and_malformed_model_rows_fail_safely(self):
        for role in ('ORCHESTRATOR','planner',None,True):
            with self.assertRaises(ValueError):
                output_contract(role)
            with self.assertRaises(ValueError):
                role_prompt(role)
        for changes in ({'requirements':True},{'requirements':[{'id':[]} ]},
                        {'checks':[None]},{'roleWork':[]},{'extra':'not permitted'}):
            result={**answer('문서 목록을 만들어줘.'),**changes}
            screen=content_screen({'answer':result},{'requestText':result['requestEcho']},'PLANNER')
            self.assertEqual(screen['status'],'FAIL')


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
            role=body['text']['format']['name'].removeprefix('natural_language_handoff_v2_').upper()
            return httpx.Response(200,json={'id':'resp_synthetic','status':'completed','model':MODEL,
                'output':[{'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                           'content':[{'type':'output_text','text':json.dumps(answer(received['requestText'],role),ensure_ascii=False),'annotations':[]}]}],
                'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}})
        runtime=NaturalRuntime(SecretStr('SYNTHETIC_TEST_ONLY'),MODEL,max_model_calls=15,
                               transport_factory=lambda:httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            report=await run(SimpleNamespace(model=MODEL,out=Path(directory)/'evidence'),runtime=runtime)
            saved=json.loads((Path(directory)/'evidence/run.json').read_text())
            self.assertEqual(len(saved['attempts']),15)
            self.assertEqual(saved['version'],'natural-language-3cases-v2')
            self.assertEqual(set(saved['schemaSha256ByRole']),{'PLANNER','DEVELOPER','QA','SECURITY','SINGLE_AGENT'})
            self.assertEqual(len(set(saved['schemaSha256ByRole'].values())),5)
        self.assertEqual(len(seen),15)
        self.assertEqual(len(report['runs']),6)
        self.assertEqual(report['status'],'CAPTURED_REVIEW_REQUIRED')
        self.assertEqual(sum(r['transportPassed'] for r in report['runs']),15)
        self.assertEqual(saved['provenance']['transportMode'],'CUSTOM_TRANSPORT')
        self.assertEqual(saved['provenance']['sourceStability'],'STABLE')
        self.assertEqual(len(saved['provenance']['sourceManifestSha256']),64)
        self.assertEqual(saved['provenance']['comparison'],'DESCRIPTIVE_NOT_CAUSAL')
        self.assertTrue(all(group['performance']['timeline']['status']=='MEASURED' for group in report['runs']))
        self.assertEqual(sum(item['completedSamples'] for group in report['runs']
                             for item in group['performance']['roles'].values()),15)
        self.assertTrue(all(row['timing']['ordering']=='PASS' for group in saved['runs'] for row in group['rows']))
        for case_id in CASE_IDS:
            group=[item for item in seen if item['caseId']==case_id]
            self.assertEqual(len(group),5)
            self.assertEqual(sum(item['upstream'] is not None for item in group),3)
        self.assertNotIn('SYNTHETIC_TEST_ONLY',json.dumps(report))

    async def test_transport_success_with_blank_role_work_is_preserved_but_not_forwarded(self):
        seen=[]
        def respond(request):
            body=json.loads(request.content)
            received=json.loads(body['input'][0]['content'])['received']
            role=body['text']['format']['name'].removeprefix('natural_language_handoff_v2_').upper()
            seen.append(role)
            result=answer(received['requestText'],role)
            if role=='PLANNER':
                result['roleWork']['PLANNER']='   '
            return httpx.Response(200,json={'id':'resp_synthetic','status':'completed','model':MODEL,
                'output':[{'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':json.dumps(result,ensure_ascii=False),'annotations':[]}]}],
                'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}})
        runtime=NaturalRuntime(SecretStr('SYNTHETIC_TEST_ONLY'),MODEL,max_model_calls=15,
            transport_factory=lambda:httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            report=await run(SimpleNamespace(model=MODEL,out=Path(directory)/'evidence'),runtime=runtime)
        self.assertEqual(seen,['PLANNER','SINGLE_AGENT']*3)
        self.assertEqual(len(runtime.calls),6)
        self.assertEqual(report['status'],'FAIL_OR_INCOMPLETE')
        for run_result in report['runs']:
            if run_result['architecture']!='MULTI_AGENT':
                continue
            planner,*others=run_result['rows']
            self.assertEqual(planner['phase'],'COMPLETED')
            self.assertEqual(planner['model']['httpStatus'],200)
            self.assertEqual(planner['observations']['responseReceived']['answer']['roleWork']['PLANNER'],'   ')
            self.assertFalse(planner['handoff']['eligible'])
            self.assertEqual(planner['handoff']['reason'],'CONTENT_CONTRACT_REJECTED')
            self.assertTrue(all(row['phase']=='SKIPPED' and row['model'] is None for row in others))
            self.assertTrue(all(row['error']=='UPSTREAM_CONTRACT_REJECTED' for row in others))

    async def test_foreign_role_response_fails_schema_without_repair_or_retry(self):
        seen=[]
        def respond(request):
            body=json.loads(request.content)
            received=json.loads(body['input'][0]['content'])['received']
            role=body['text']['format']['name'].removeprefix('natural_language_handoff_v2_').upper()
            seen.append(role)
            result=answer(received['requestText'],role)
            if role=='PLANNER':
                result['roleWork']['DEVELOPER']='다른 역할도 작성했다.'
            return httpx.Response(200,json={'id':'resp_synthetic','status':'completed','model':MODEL,
                'output':[{'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                    'content':[{'type':'output_text','text':json.dumps(result,ensure_ascii=False),'annotations':[]}]}],
                'usage':{'input_tokens':10,'output_tokens':10,'total_tokens':20}})
        runtime=NaturalRuntime(SecretStr('SYNTHETIC_TEST_ONLY'),MODEL,max_model_calls=15,
            transport_factory=lambda:httpx.MockTransport(respond))
        with TemporaryDirectory() as directory:
            report=await run(SimpleNamespace(model=MODEL,out=Path(directory)/'evidence'),runtime=runtime)
        self.assertEqual(seen,['PLANNER','SINGLE_AGENT']*3)
        self.assertEqual(len(runtime.calls),6)
        self.assertEqual(report['status'],'FAIL_OR_INCOMPLETE')
        for result in report['runs']:
            if result['architecture']=='MULTI_AGENT':
                planner,*others=result['rows']
                self.assertEqual(planner['phase'],'ERROR')
                self.assertEqual(planner['model']['httpStatus'],200)
                self.assertEqual(planner['model']['errorCode'],'LLM_SCHEMA_INVALID')
                self.assertFalse(planner['handoff']['eligible'])
                self.assertEqual(planner['handoff']['reason'],'MODEL_OUTPUT_CONTRACT_REJECTED')
                self.assertTrue(all(row['phase']=='SKIPPED' for row in others))


if __name__=='__main__':
    unittest.main()
