"""No paid calls: env isolation, real-provider wiring, bounds, and real A2A."""

import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from agents.llm.contracts import LLMRuntimeError
from diagnostics.communication.contracts import CheckOptions, ProbeRecord, grounding
from diagnostics.communication.lab import running_lab
from diagnostics.communication.live import LiveRuntime, answer_contract, root_api_key
from diagnostics.communication.workloads import load_request, expected_specification


KEY = "SYNTHETIC_LIVE_TEST_KEY"
MODEL = "gpt-4.1-mini-2025-04-14"


def payload():
    doc=load_request("signup")
    return {"profile":"signup","workRequest":doc,"sample":{"value":100,"label":"실제 경로 검사",
            "requirementIds":[r['id'] for r in doc['requirements']]},"upstream":None}


def provider_response(body, role="DEVELOPER", mutate=None):
    data=json.loads(body['input'][0]['content'])['received']
    if 'SINGLE_AGENT' in body['instructions'].split('역할')[0]:role='SINGLE_AGENT'
    else:
        for name in ['PLANNER','DEVELOPER','QA','SECURITY']:
            if name in body['instructions'].split('역할')[0]:role=name
    spec=expected_specification(data['profile'],role)
    del spec['requestFingerprint']
    answer={'claims':{'value':data['sample']['value'],'label':data['sample']['label'],
                     'requirementCount':len(data['sample']['requirementIds']),'specification':spec},
            'freeText':'합성 요청 계약을 추출했습니다. 실제 도구는 실행하지 않았습니다.'}
    if mutate:mutate(answer)
    return {'id':'resp_synthetic','status':'completed','model':MODEL,
            'output':[{'id':'msg_synthetic','type':'message','role':'assistant','status':'completed',
                       'content':[{'type':'output_text','text':json.dumps(answer,ensure_ascii=False),'annotations':[]}]}],
            'usage':{'input_tokens':100,'output_tokens':50,'total_tokens':150}}


def transport_factory(seen, *, status=200, code=None, mutate=None):
    def respond(request):
        body=json.loads(request.content);seen.append(body)
        if status!=200:
            return httpx.Response(status,json={'error':{'code':code,'message':KEY}})
        return httpx.Response(200,json=provider_response(body,mutate=mutate),headers={'x-request-id':'req_synthetic'})
    return lambda:httpx.MockTransport(respond)


class LiveConfigurationTests(unittest.TestCase):
    def test_only_root_env_and_no_interpolation(self):
        with TemporaryDirectory() as tmp,patch.dict(os.environ,{},clear=True):
            root=Path(tmp);(root/'docs').mkdir()
            (root/'.env').write_text('OPENAI_API_KEY=synthetic-${DO_NOT_EXPAND}\n')
            (root/'docs'/'.env').write_text('OPENAI_API_KEY=wrong-nested-key\n')
            self.assertEqual(root_api_key(root).get_secret_value(),'synthetic-${DO_NOT_EXPAND}')
            self.assertNotIn('OPENAI_API_KEY',os.environ)
            (root/'.env').unlink()
            with self.assertRaisesRegex(ValueError,'OPENAI_API_KEY_MISSING'):
                root_api_key(root)

    def test_environment_wins_and_key_is_not_in_runtime_repr(self):
        with TemporaryDirectory() as tmp,patch.dict(os.environ,{'OPENAI_API_KEY':KEY}):
            runtime=LiveRuntime(root_api_key(Path(tmp)),MODEL)
            self.assertEqual(runtime.api_key.get_secret_value(),KEY)
            self.assertNotIn(KEY,repr(runtime))

    def test_live_admission_rejects_faults_and_excess_cost(self):
        runtime=LiveRuntime(SecretStr(KEY),MODEL,max_model_calls=5)
        for field,value in [('scenario','permission_swap'),('processing_ms',1),
                            ('padding_kib',1),('sender_delay_ms',1)]:
            with self.assertRaises(ValueError):
                CheckOptions(rounds=1,**{field:value})
        with self.assertRaises(ValueError):
            runtime.admit(CheckOptions(rounds=2))
        runtime.admit(CheckOptions(rounds=1))

    def test_schema_contains_types_but_no_answer_values_or_gold_access(self):
        data=payload()
        with patch('diagnostics.communication.workloads._asset',side_effect=AssertionError('gold forbidden')):
            output=answer_contract(data,'DEVELOPER')
        text=output.schema.schema_json
        for item in ['"const"','"enum"','REQ-SU-01','20971520','requestFingerprint','upstreamHash']:
            self.assertNotIn(item,text)
        output.schema.require_openai_strict()


class LiveProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_provider_receives_source_not_reference_and_records_usage(self):
        seen=[];runtime=LiveRuntime(SecretStr(KEY),MODEL,transport_factory=transport_factory(seen))
        row=ProbeRecord('DEVELOPER',1,0)
        answer=await runtime.complete(payload(),row)
        self.assertEqual(answer['claims']['value'],100)
        self.assertEqual(len(seen),1)
        self.assertFalse(seen[0]['store']);self.assertEqual(seen[0]['tools'],[])
        self.assertNotIn(KEY,json.dumps(seen))
        self.assertEqual(row.model_observation['usage']['total_tokens'],150)
        self.assertTrue(row.model_observation['apiInputMatches'])

    async def test_auth_and_quota_errors_stop_without_retry_and_hide_body(self):
        for status,code in [(401,'invalid_api_key'),(429,'insufficient_quota')]:
            seen=[];runtime=LiveRuntime(SecretStr(KEY),MODEL,transport_factory=transport_factory(seen,status=status,code=code))
            row=ProbeRecord('DEVELOPER',1,0)
            with self.assertRaises(LLMRuntimeError) as error:await runtime.complete(payload(),row)
            self.assertNotIn(KEY,str(error.exception));self.assertNotIn(KEY,json.dumps(runtime.calls))
            self.assertEqual(runtime.fatal_error,code)
            with self.assertRaises(LLMRuntimeError):await runtime.complete(payload(),row)
            self.assertEqual(len(seen),1)

    async def test_model_wrong_value_is_preserved_not_repaired(self):
        seen=[]
        runtime=LiveRuntime(SecretStr(KEY),MODEL,transport_factory=transport_factory(seen,mutate=lambda a:a['claims'].__setitem__('value',101)))
        from diagnostics.communication.live import OpenAIProbeExecutor
        data=payload();row=ProbeRecord('DEVELOPER',1,0)
        result=await OpenAIProbeExecutor({},runtime).make_result(data,row)
        self.assertEqual(result['claims']['value'],101)
        self.assertEqual(grounding(result,data,'DEVELOPER')['status'],'FAIL')

    async def test_real_a2a_calls_four_roles_and_single_with_shared_call_limit(self):
        seen=[];runtime=LiveRuntime(SecretStr(KEY),MODEL,transport_factory=transport_factory(seen))
        async with running_lab(model_runtime=runtime) as lab:
            for architecture,expected in [('MULTI_AGENT',4),('SINGLE_AGENT',1)]:
                report=await lab.execute(CheckOptions(rounds=1,architecture=architecture),lambda event:None)
                self.assertEqual(report['summary']['verifiedRequests'],expected)
                self.assertTrue(report['llmUsed']);self.assertEqual(report['mode'],'REAL_HTTP_A2A_OPENAI')
                self.assertTrue(all(r['model']['status']=='COMPLETED' for r in report['rows']))
                performance = report['performance']
                self.assertEqual(performance['diagnosticElapsedMs'],report['elapsedMs'])
                self.assertEqual(performance['timeline']['status'],'MEASURED')
                self.assertEqual(len({r['timing']['clockId'] for r in report['rows']}),1)
                self.assertEqual(sum(group['completedSamples'] for group in performance['roles'].values()),expected)
                for row in report['rows']:
                    self.assertEqual(row['timing']['ordering'],'PASS')
                    self.assertEqual(row['timing']['phases']['providerExchangeMs']['status'],'MEASURED')
                    self.assertIsNotNone(row['timings']['admissionAndSchedulingMs'])
                    self.assertIsNotNone(row['timings']['resultObservationMs'])
            with self.assertRaises(ValueError):
                await lab.execute(CheckOptions(rounds=1,architecture='SINGLE_AGENT'),lambda event:None)
        self.assertEqual(len(seen),5)


if __name__=='__main__':
    unittest.main()
