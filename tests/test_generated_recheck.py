"""A previous Ver2 generated test remains measurable when its validator rewrites the suite."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from uuid import UUID

from agents.runtime.qa_services import QARuntimeServices
from mcp_tools.tools import generated_recheck
from orchestrator.domain import AgentRole, FinalVerdict
from orchestrator.domain.repair_progress import final_rechecks, generated_issue_recheck
from orchestrator.domain.validation_feedback import validation_feedback
from test_owned_agent_fix_pipeline import _FixHarness


class GeneratedRecheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_recheck_uses_archived_input_on_candidate_and_ignores_new_test_version(self):
        harness = _FixHarness(self, qa_mode='repeated-failure')
        harness.failure_index = lambda version: 0 if version == 1 else None
        normal_entry = QARuntimeServices.run_protected
        normal_claim = generated_recheck.claim

        def check_claim(repo, step_id, source, previous):
            admitted = normal_claim(repo, step_id, source, previous)
            if admitted:
                with self.assertRaisesRegex(ValueError, 'GENERATED_RECHECK_INTERRUPTED'):
                    normal_claim(repo, step_id, source, previous)
            return admitted

        async def changed_workspace(service, execution, tracked):
            if execution.source.code_version == 2:
                binding = service.configuration.binding
                workspace = service._workspaces.bind(binding.workspace_id, run_id=binding.run_id, role=AgentRole.QA)
                (Path(workspace.record.root_path) / 'outputs/qa/tests/test_signup.py').write_text('# different scratch\n')
            await normal_entry(service, execution, tracked)

        with patch.object(QARuntimeServices, 'run_protected', changed_workspace), \
                patch.object(generated_recheck, 'claim', check_claim):
            async with harness.running() as (client, _):
                response = await client.post('/api/v1/runs', json=harness.base.submission())
        self.assertEqual(response.status_code, 201)
        run_id = UUID(response.json()['run']['runId'])
        repo = harness.base.repository
        self.assertEqual(repo.get_run(run_id).verdict, FinalVerdict.SUCCESS)
        artifacts = repo.list_project_artifacts(run_id)
        chosen = {a.artifact_type:a for a in artifacts if getattr(a,'code_version',None) == 2}
        source, review = chosen['SOURCE'], chosen['CHANGE_REPORT'].ready_review
        self.assertFalse(review.reported_path_edits)  # A generated Ver2 case with no known static repair path.
        issues = repo.list_issue_records(run_id)
        issue, = [i for i in issues if i.code_version == 1 and i.reporter is AgentRole.QA]
        prior = next(a for a in artifacts if a.artifact_id == issue.report_artifact_id)
        old = next(r for r in validation_feedback([prior]) if r['testId'] == issue.reference_id)
        from orchestrator.infrastructure.repair_reproduction import generated_reproduction
        old_source = next(a for a in artifacts if a.artifact_id == issue.source_artifact_id)
        reproduction = generated_reproduction(repo, old_source, validation_feedback([old_source, prior]))
        self.assertEqual(len(reproduction), 1)
        self.assertTrue(reproduction[0]['readOnly'])
        self.assertEqual(reproduction[0]['testIds'], [issue.reference_id])
        self.assertTrue(all(f['path'] != '_unit_runner.py' for f in reproduction[0]['files']))
        self.assertNotIn('different scratch', json.dumps(reproduction))
        self.assertEqual(generated_reproduction(repo, old_source,
            [{**f, 'runId': str(UUID(int=1))} for f in validation_feedback([old_source, prior])]), [])
        current = chosen['QA_REPORT']
        new = next(r for r in validation_feedback([current]) if r['testId'] == issue.reference_id)
        self.assertNotEqual(old['testVersion'],new['testVersion'])
        self.assertEqual(generated_issue_recheck(issue,prior,current),'UNVERIFIED')
        with repo._connection() as c:
            receipt = generated_recheck.measured_row(c,issue,source,old)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM generated_recheck_results').fetchone()[0],1)
            self.assertIsNotNone(receipt)
            self.assertEqual(receipt['sourceArtifactId'],str(source.artifact_id))
            self.assertEqual(receipt['testVersion'],old['testVersion'])
            self.assertEqual(receipt['outcome'],'PASS')
            later = source.model_copy(update={'code_version':3, 'previous_artifact_id':source.artifact_id})
            self.assertIsNone(generated_recheck.measured_row(c,issue,later,old))
            self.assertIsNone(generated_recheck.measured_row(c,issue,source,{**old,'testId':old['testId']+'.other'}))
            self.assertIsNone(generated_recheck.measured_row(c,issue,source,{**old,'testVersion':new['testVersion']}))
            results=final_rechecks(review,source,artifacts,chosen,repo.get_run_configuration(run_id),issues=issues,
                                   generated_lookup=lambda issue,src,row:generated_recheck.measured_row(c,issue,src,row))
            result, = [r for r in results if r['issueId'] == str(issue.issue_id)]
            self.assertEqual(result['currentGeneratedReceiptId'],receipt['recheckReceiptId'])
            self.assertIsNone(result['currentReportId'])
            self.assertEqual(result['caseRechecks'],[{'caseId':issue.reference_id,'outcome':'PASS'}])
        self.assertEqual(issue.revalidation_result,'PASS')


    async def test_failed_archived_case_prevents_success_despite_new_draft_passing(self):
        harness = _FixHarness(self, qa_mode='initial-failure')
        normal_entry = QARuntimeServices.run_protected

        async def failed_archive(service, execution, tracked):
            if execution.source.code_version != 2:
                return await normal_entry(service, execution, tracked)
            docker = harness.base.dockers[AgentRole.QA][-1]
            original = docker.start_result
            payload = json.loads(original.stdout)
            payload.update(passed=payload['passed'] - 1, failed=1)
            payload['tests'][0]['outcome'] = 'FAIL'
            docker.start_result = type(original)(returncode=1, stdout=json.dumps(payload).encode(), stderr=b'')
            old_exit, docker.exit_code = docker.exit_code, 1
            try:
                await normal_entry(service, execution, tracked)
            finally:
                docker.exit_code, docker.start_result = old_exit, original

        with patch.object(QARuntimeServices, 'run_protected', failed_archive):
            async with harness.running() as (client, _):
                response = await client.post('/api/v1/runs', json=harness.base.submission())
        self.assertEqual(response.status_code, 201)
        repo = harness.base.repository
        run_id = UUID(response.json()['run']['runId'])
        artifacts = repo.list_project_artifacts(run_id)
        candidate = next(a for a in artifacts if a.artifact_type == 'SOURCE' and a.code_version == 2)
        current = next(a for a in artifacts if a.artifact_type == 'QA_REPORT' and a.code_version == 2)
        issue = next(i for i in repo.list_issue_records(run_id) if i.code_version == 1 and i.reporter is AgentRole.QA)
        original = next(a for a in artifacts if a.artifact_id == issue.report_artifact_id)
        old = next(r for r in validation_feedback([original]) if r['testId'] == issue.reference_id)
        new = next(r for r in validation_feedback([current]) if r['testId'] == issue.reference_id)
        self.assertEqual(new['outcome'], 'PASS')
        with repo._connection() as connection:
            lookup = lambda row: generated_recheck.measured_row(connection, issue, candidate, row)
            self.assertEqual(lookup(old)['outcome'], 'FAIL')
            self.assertEqual(generated_issue_recheck(issue,original,current,replayed=lookup), 'FAIL')
        self.assertNotEqual(repo.get_run(run_id).verdict, FinalVerdict.SUCCESS)


class GeneratedVerdictTests(unittest.TestCase):
    def test_raw_pass_cannot_erase_host_failure_or_lost_observation(self):
        from types import SimpleNamespace as Case
        def actual(facts=None, marks=()):
            return Case(outcome='PASS', signup_checks_json=json.dumps(facts) if facts else None, output_checks=marks)
        previous={'role':'QA','requirementId':'r','signupChecks':{'concurrentCanonicalSignup':False}}
        self.assertEqual(generated_recheck.case_outcome(previous,actual({'concurrentCanonicalSignup':False}),{}),'FAIL')
        self.assertEqual(generated_recheck.case_outcome(previous,actual({'concurrentCanonicalSignup':True}),{}),'PASS')
        self.assertEqual(generated_recheck.case_outcome(previous,actual(),{}),'UNVERIFIED')
        protected = {'role':'SECURITY','requirementId':'r','outputChecks':{'SUCCESS_RESPONSE':'EXPOSED'}}
        for mark,expected in [('EXPOSED','FAIL'),('INCOMPLETE','UNVERIFIED'),('CHECKED','PASS')]:
            self.assertEqual(generated_recheck.case_outcome(protected,actual(marks=(('SUCCESS_RESPONSE',mark),)),{}),expected)
        self.assertEqual(generated_recheck.case_outcome(protected,actual(),{}),'UNVERIFIED')

    def test_same_named_unittest_with_a_missing_failed_assertion_cannot_pass(self):
        from types import SimpleNamespace
        failed = {'caseId':'signup','itemId':'first','kind':'assertion','status':'FAIL',
                  'actual':400,'reason':'EXPECTATION_MISMATCH'}
        previous = {'role':'QA', 'requirementId':'r', 'observationStatus':'COMPLETE', 'observations':[failed]}
        passed = {**failed, 'status':'PASS', 'actual':201, 'reason':None}
        case = SimpleNamespace(outcome='PASS', signup_checks_json=None, output_checks=(),
                               observations_json=json.dumps([{**passed,'itemId':'second'}]))
        self.assertEqual(generated_recheck.case_outcome(previous, case, {}), 'UNVERIFIED')
        case.observations_json=json.dumps([passed])
        self.assertEqual(generated_recheck.case_outcome(previous, case, {}), 'PASS')

    def test_hash_measurements_use_frozen_policy_and_require_previous_salt_coverage(self):
        from types import SimpleNamespace
        contract={'scenarioKey':'SCN-001','requirements':[{'requirementId':'r','key':'REQ-005'}],
                  'securityPolicy':{'passwordHashPolicy':{'algorithm':'argon2id','memoryKiB':19456,
                                                           'iterations':2,'parallelism':1,'uniqueSalt':True}}}
        valid={'argon2id':True,'verified':True,'memoryKiB':19456,'iterations':2,'parallelism':1,'distinctSalts':True}
        old={'role':'SECURITY','requirementId':'r','signupChecks':{'storedHashParameters':{**valid,'argon2id':False}}}
        for measured,expected in [({**valid,'iterations':1},'FAIL'),(valid,'PASS'),
                                  ({k:v for k,v in valid.items() if k!='distinctSalts'},'UNVERIFIED'),({},'UNVERIFIED')]:
            case=SimpleNamespace(outcome='PASS', signup_checks_json=json.dumps({'storedHashParameters':measured}), output_checks=())
            self.assertEqual(generated_recheck.case_outcome(old,case,contract),expected)
