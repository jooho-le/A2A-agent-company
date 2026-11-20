"""Run #29/#30 regressions: actual ID catalog and evidence-bound technical completion."""
import json
from pathlib import Path
import tempfile
import unittest
from uuid import uuid4, UUID
from unittest import mock
from dataclasses import replace

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.roles.qa_contract import build_qa_output_contract, validate_qa_decision, QAContractError
from agents.roles.contract_explain import qa_issues
from agents.runtime.qa_catalog import refresh_qa_output
from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.platform.package_service import HostPackageService
from orchestrator.domain import AgentRole, WorkflowStatus
from test_llm_runtime import text_response, tool_response
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_single_agent_pipeline as single_fixture


class QACatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.ids = (uuid4(),)
        self.selectors = {'run_unit_tests': ('qa-unit',)}
        self.output = build_qa_output_contract(self.ids, self.selectors)

    def refresh(self):
        return refresh_qa_output(self.output, self.root, [('qa-unit', 'test_*.py')],
                                 snapshot_sha256='a'*64, unit_only=True)

    def test_live_ids_are_host_discovered_and_not_guessed_or_imported(self):
        # Importing this file on Host would raise; analysis must remain inert.
        file = self.root/'test_signup.py'
        file.write_text('import unittest\nraise RuntimeError("never execute")\nclass SignupTests(unittest.TestCase):\n    def test_valid_signup(self): pass\n')
        output, catalog = self.refresh()
        ids = output.schema.to_dict()['properties']['cases']['items']['properties']['testId']['enum']
        self.assertEqual(ids, ['test_signup.SignupTests.test_valid_signup'])
        self.assertNotIn('tests.test_signup.SignupTests.test_valid_signup', ids)
        self.assertNotIn('test_signup.test_valid_signup', ids)
        self.assertEqual(catalog['unresolvedSelectors'], [])
        file.write_text(file.read_text().replace('test_valid_signup', 'test_changed'))
        updated, catalog2 = self.refresh()
        self.assertNotEqual(catalog['filesSha256'], catalog2['filesSha256'])
        self.assertEqual(updated.schema.to_dict()['properties']['cases']['items']['properties']['testId']['enum'],
                         ['test_signup.SignupTests.test_changed'])

    def test_unsupported_dynamic_layout_never_fabricates_ids(self):
        (self.root/'test_dynamic.py').write_text('def load_tests(*args): pass\n')
        output, catalog = self.refresh()
        self.assertEqual(catalog['cases'], [])
        self.assertEqual(catalog['unresolvedSelectors'], ['qa-unit'])
        self.assertNotIn('enum',output.schema.to_dict()['properties']['cases']['items']['properties']['testId'])

    def test_precheck_detects_both_unbound_and_nonexistent_declarations(self):
        (self.root/'test_signup.py').write_text('import unittest\nclass Signup(unittest.TestCase):\n    def test_ok(self): pass\n')
        rows = [{'selector':'qa-unit', 'testId':'tests.test_signup.Signup.test_ok'}]
        issues = inspect_generated_tests(self.root, [('qa-unit','test_*.py')], rows, 'cases')
        self.assertEqual({r['reasonCode'] for r in issues}, {'TEST_IN_FILE_NOT_DECLARED','DECLARED_TEST_NOT_IN_FILE'})

    def test_rejected_with_questions_has_precise_diagnostic_and_is_still_rejected(self):
        draft = {'kind':'REJECTED','cases':[],'questions':['need clarification']}
        with self.assertRaises(QAContractError): validate_qa_decision(draft,self.ids,self.selectors)
        self.assertEqual(qa_issues(draft,self.ids,self.selectors),
            [{'fieldPath':'/questions','reasonCode':'QUESTIONS_NOT_ALLOWED_WHEN_REJECTED'}])
        draft['questions']=[]
        self.assertEqual(validate_qa_decision(draft,self.ids,self.selectors).kind,'REJECTED')
        self.assertEqual(qa_issues(draft,self.ids,self.selectors),[])


class QAExhaustionPaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner=stop_fixture.DevelopmentStopPaths('runTest')
        try: helper,f,packages=owner.fixture(ver1)
        finally: completion.adopt_cleanups(self,owner)
        return helper,f,packages

    def assert_package(self, f, packages, run):
        self.assertEqual((run.status.value,getattr(run.verdict,'value',None)),('FINISHED','UNVERIFIED'),run)
        self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')
        self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')
        with f.repository._connection() as c:
            report=json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',(str(run.run_id),)).fetchone()[0])
        self.assertFalse(report['validationStop']['qaReportProduced'])
        self.assertNotIn('QA_REPORT',report['artifacts'])
        self.assertIn('SECURITY_REPORT',report['artifacts'])
        self.assertEqual(report['unexecutedValidation']['QA_REPORT']['reason'],'QA_DRAFT_CORRECTIONS_EXHAUSTED')
        self.assertEqual(report['validationStop']['retainedCodeVersion'],run.code_version)
        return report

    async def test_multi_ver1_malformed_rejected_exhaustion_closes_without_fake_pass(self):
        helper,f,packages=self.fixture(True)
        providers=f.providers()
        bad=text_response(json.dumps({'kind':'REJECTED','cases':[],'questions':['unresolved']}))
        providers[AgentRole.QA].script=[bad,bad]
        run=await helper.submit(f,providers)
        self.assert_package(f,packages,run)
        events=helper.events(f,run)
        self.assertEqual(sum(e.event_type=='QA_DRAFT_CORRECTIONS_EXHAUSTED' for e in events),1)
        self.assertFalse(any(e.workflow_state==WorkflowStatus.HUMAN_REVIEW for e in events))

    async def test_multi_ver2_bad_schema_exhaustion_closes_without_final_qa_report(self):
        helper,f,packages=self.fixture()
        providers=f.providers()
        bad=text_response('{}')
        providers[AgentRole.QA].script=[bad,bad]
        run=await helper.submit(f,providers)
        self.assert_package(f,packages,run)

    async def test_real_id_prefix_mismatch_exhaustion_preserves_unverified_package(self):
        helper,f,packages=self.fixture()
        providers=f.providers()
        write,ready=providers[AgentRole.QA].script
        def write_real(request):
            call=write(request).tool_calls[0]
            args=json.loads(call.arguments_json)
            count=len(json.loads(ready(request).output_text)['cases'])
            args['content']='import unittest\nclass Signup(unittest.TestCase):\n'+''.join(
                f'    def test_req_{i}(self): pass\n' for i in range(1,count+1))
            return tool_response(replace(call,arguments_json=json.dumps(args)))
        def wrong_ids(request):
            data=json.loads(ready(request).output_text)
            for row in data['cases']: row['testId']='tests.'+row['testId']
            return text_response(json.dumps(data))
        providers[AgentRole.QA].script=[write_real,wrong_ids,wrong_ids]
        run=await helper.submit(f,providers)
        self.assert_package(f,packages,run)

    async def test_unresolved_security_judgment_keeps_human_review(self):
        from orchestrator.infrastructure import validation_stop
        from orchestrator.domain import ValidationOutcome
        helper,f,packages=self.fixture()
        providers=f.providers()
        bad=text_response('{}');providers[AgentRole.QA].script=[bad,bad]
        original=validation_stop.finish_stopped
        def unresolved(repository,run_id,stopped,report=None,**options):
            rows=list(report.requirement_results)
            rows[0]=rows[0].model_copy(update={'outcome':ValidationOutcome.UNVERIFIED,'details':'not established'})
            return original(repository,run_id,stopped,report.model_copy(update={'requirement_results':rows}),**options)
        with mock.patch.object(validation_stop,'finish_stopped',unresolved):
            run=await helper.submit(f,providers)
        self.assertEqual(run.status,WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'],'NOT_COMPLETED')

    async def test_valid_rejected_is_not_reclassified_as_technical_exhaustion(self):
        helper,f,packages=self.fixture()
        providers=f.providers()
        providers[AgentRole.QA].script=[text_response(json.dumps({'kind':'REJECTED','cases':[],'questions':[]}))]
        run=await helper.submit(f,providers)
        self.assertEqual(run.status,WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'],'NOT_COMPLETED')

    async def test_security_manifest_mismatch_cannot_use_qa_exhaustion_exit(self):
        from orchestrator.infrastructure import validation_stop
        helper,f,packages=self.fixture()
        providers=f.providers()
        bad=text_response('{}')
        providers[AgentRole.QA].script=[bad,bad]
        original=validation_stop.finish_stopped
        def wrong_snapshot(repository,run_id,stopped,report=None,**options):
            manifest=report.execution_manifest.model_copy(update={'snapshot_sha256':'b'*64})
            return original(repository,run_id,stopped,report.model_copy(update={'execution_manifest':manifest}),**options)
        with mock.patch.object(validation_stop,'finish_stopped',wrong_snapshot):
            run=await helper.submit(f,providers)
        self.assertEqual(run.status,WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'],'NOT_COMPLETED')

    async def test_forged_agent_exhaustion_code_without_host_proof_stays_review(self):
        helper,f,packages=self.fixture()
        providers=f.providers()
        providers[AgentRole.QA].script=[LLMRuntimeError(LLMErrorCode.VALIDATION_DRAFT)]
        run=await helper.submit(f,providers)
        self.assertEqual(run.status,WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'],'NOT_COMPLETED')

    async def test_catalog_refresh_after_write_guides_exact_bindings_to_success(self):
        helper,f,packages=self.fixture()
        providers=f.providers()
        write,ready=providers[AgentRole.QA].script
        def write_real(request):
            call=write(request).tool_calls[0]
            args=json.loads(call.arguments_json)
            count=len(json.loads(ready(request).output_text)['cases'])
            args['content']='import unittest\nclass Signup(unittest.TestCase):\n'+''.join(
                f'    def test_req_{i}(self): pass\n' for i in range(1,count+1))
            return tool_response(replace(call,arguments_json=json.dumps(args)))
        def choose(request):
            schema=request.output.schema.to_dict()
            ids=schema['properties']['cases']['items']['properties']['testId']['enum']
            result=ready(request)
            self.assertEqual(set(ids),{r['testId'] for r in json.loads(result.output_text)['cases']})
            self.assertIn('hostTestIdCatalog',request.input_items_json)
            return result
        providers[AgentRole.QA].script=[write_real,choose]
        run=await helper.submit(f,providers)
        self.assertEqual(run.status,WorkflowStatus.FINISHED,run)
        self.assertEqual(run.verdict.value,'SUCCESS')
        self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')

    async def test_single_qa_exhaustion_still_runs_security_and_saves_package(self):
        for name in ('agents.runtime.developer.start_precheck_for','agents.single.runtime.start_precheck_for'):
            patcher=mock.patch(name,lambda *_:None);patcher.start();self.addCleanup(patcher.stop)
        base=single_fixture.SingleAgentPipelineTests('runTest')
        try: base.setUp()
        finally: completion.adopt_cleanups(self,base)
        base.measured_security=True
        f=base.f
        packages=HostPackageService(f.repository)
        prepare=f.prepare_workspace
        def enrolled(run_id): packages.enroll(run_id);prepare(run_id)
        f.prepare_workspace=enrolled
        provider=base.provider()
        bad=text_response(json.dumps({'kind':'REJECTED','cases':[],'questions':['unresolved']}))
        provider.script[3:5]=[bad,bad]
        async with base.running(cap=32,tool_cap=40,provider=provider,corrections=2,reminders=1) as (client,_):
            response=await client.post('/api/v1/runs',json=f.submission())
            self.assertEqual(response.status_code,201,response.text)
            run=f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assert_package(f,packages,run)
