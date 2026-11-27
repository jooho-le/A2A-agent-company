import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_progress import compare_repair
from orchestrator.domain.repair_progress import final_rechecks
from orchestrator.domain.validation_feedback import measured_test_version, observation_json


def qa_issue(report='qa', test='case-A'):
    return {'category': 'QA_ASSERTION_FAILURE', 'issueId': 'issue-' + test, 'referenceId': test,
            'reportArtifactId': report, 'requirementIds': ['req'], 'codeVersion': 3, 'sourceArtifactId': 'source'}


def row(report, test, *, role='QA', outcome='FAIL'):
    return {'role': role, 'reportArtifactId': report, 'testId': test, 'requirementId': 'req', 'codeVersion': 3,
            'sourceArtifactId': 'source', 'outcome': outcome, 'observationStatus': 'COMPLETE', 'observations': [],
            'testVersion': 'inputs:' + 'a' * 64}


class RepairBindingTests(unittest.TestCase):
    def test_overlapping_issues_never_claim_excluded_checks_or_findings(self):
        issues = [qa_issue(), qa_issue(test='case-B'),
                  {**qa_issue('security'), 'category': 'SECURITY_REQUIREMENT_FAILURE', 'issueId': 'sec',
                   'referenceId': 'req', 'description': json.dumps({'code':'MEASURED_SECURITY_TESTS',
                   'tests':[{'testId':'case-A', 'outcome':'FAIL'}, {'testId':'passing', 'outcome':'PASS'}]})},
                  {**qa_issue('security'), 'issueId': 'finding', 'category':'SECURITY_FINDING_CONFIRMED'}]
        rows = [row('qa','case-A'), row('qa','case-B'), row('other','case-A'),
                row('qa','excluded-generated'), row('security','case-A',role='SECURITY'),
                row('security','passing',role='SECURITY',outcome='PASS')]
        source = {'fixRequest': {'issues':issues, 'validationFeedback':rows}}
        original = deepcopy(source)
        result = compact_fix_input(source)
        checks = [[(r['reportArtifactId'], r['testId']) for r in t['failingChecks']]
                  for t in result['activeRepairFocus']['targets']]
        self.assertEqual(checks, [[('qa','case-A')],[('qa','case-B')],[('security','case-A')],[]])
        self.assertEqual({r['testId'] for r in result['activeRepairFocus']['notSelectedFailures']},
                         {'case-A','excluded-generated'})
        self.assertEqual(source, original)

    def test_duplicate_wrong_source_or_wrong_version_cannot_become_targets(self):
        for rows in ([row('qa','case-A')]*2, [{**row('qa','case-A'), 'sourceArtifactId':'stale'}],
                     [{**row('qa','case-A'), 'codeVersion':2}]):
            with self.subTest(rows=rows):
                result=compact_fix_input({'fixRequest':{'issues':[qa_issue()], 'validationFeedback':rows}})
                self.assertEqual(result['activeRepairFocus']['targets'][0]['failingChecks'], [])


class RepairProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.saved = json.loads((Path(__file__).parent / 'fixtures/signup_repair_observations.json').read_text())

    def measured(self, version):
        saved = self.saved
        observations = deepcopy(saved['v2Observations'])
        for number in range(3, version+1):
            for change in saved[f'v{number}Changes']:
                target = next(o for o in observations if (o['caseId'],o['itemId']) == (change['caseId'],change['itemId']))
                target.update(change)
        observation_json(observations)
        return {**row('security', saved['testId'], role='SECURITY'), 'observations':observations,
                'testVersion':saved['testVersion'], 'codeVersion':version}

    def compare(self, before, after):
        history = {'previousRows':[before], 'beforeVersion':before['codeVersion'],'afterVersion':after['codeVersion'],
                   'beforeSnapshot':self.saved['snapshots'][str(before['codeVersion'])],
                   'afterSnapshot':self.saved['snapshots'][str(after['codeVersion'])],
                   'changedFiles':['main.py'],'changedFunctions':{}}
        return compare_repair(history, [after])['checks'][0]

    def test_archived_observations_and_connected_preconditions(self):
        v2, v3, v4 = [self.measured(v) for v in (2,3,4)]
        first, second = self.compare(v2,v3), self.compare(v3,v4)
        self.assertEqual((first['status'], first['counts']), ('PARTIAL_IMPROVEMENT', {'NOW_PASSING':1, 'UNCHANGED':30}))
        self.assertEqual((second['status'],second['counts']), ('NO_PROGRESS',{'UNCHANGED':31}))
        change, = [c for c in first['items'] if c['transition']=='NOW_PASSING']
        self.assertEqual((change['caseId'],change['itemId']),('malformed_missing_email','password_exposed'))
        issue={**qa_issue('security'),'category':'SECURITY_REQUIREMENT_FAILURE','referenceId':'req',
               'description':json.dumps({'code':'MEASURED_SECURITY_TESTS','tests':[{'testId':v3['testId'],'outcome':'FAIL'}]})}
        projected=compact_fix_input({'fixRequest':{'issues':[issue],'validationFeedback':[v3]}})
        obs=projected['activeRepairFocus']['targets'][0]['failingChecks'][0]['failingObservations']
        self.assertEqual(len(obs),10)
        self.assertEqual({(o['caseId'],o['itemId'],o['actual']) for o in obs if o['status']=='PASS'},
                         {('db_error','injected', True),('malformed_list','response_status',422),
                          ('malformed_misspelled','response_status',422)})
        self.assertTrue(all('expected' not in o for o in obs))
        self.assertEqual(sum(x['status']=='UNVERIFIED' and x['actual'] is None for x in obs),3)

    def test_unknown_changed_version_or_missing_observations_are_not_progress(self):
        old, new = self.measured(2), self.measured(3)
        for override in ({'testVersion':'inputs:'+'b'*64}, {'testVersion':None},
                         {'observations':new['observations'][:-1]}, {'observationStatus':'UNVERIFIED'}):
            with self.subTest(override=list(override)):
                self.assertEqual(self.compare(old,{**new,**override})['status'], 'NOT_COMPARABLE')
        different = deepcopy(old)
        item = next(o for o in different['observations'] if o['status']=='FAIL')
        item.update(status='UNVERIFIED', actual=None, reason='UNHANDLED_APP_EXCEPTION')
        result=self.compare(old,different)
        self.assertEqual(result['counts']['VERIFICATION_LOST'],1)
        self.assertNotIn(result['status'],('PARTIAL_IMPROVEMENT','RESOLVED','NO_PROGRESS'))

    def test_case_feedback_keeps_measured_conditions_unknowns_and_edit_effect_together(self):
        for before, after, status in [(2, 3, 'PARTIAL_IMPROVEMENT'), (3, 4, 'NO_PROGRESS')]:
            with self.subTest(after=after):
                old, new = self.measured(before), self.measured(after)
                old['reportArtifactId'] = 'prior'
                issue = {**qa_issue('security'), 'category': 'SECURITY_REQUIREMENT_FAILURE', 'codeVersion': after,
                         'description': json.dumps({'code': 'MEASURED_SECURITY_TESTS',
                                                   'tests': [{'testId': new['testId'], 'outcome': 'FAIL'}]})}
                history = {'previousRows': [old], 'beforeVersion': before, 'afterVersion': after,
                           'beforeSnapshot': self.saved['snapshots'][str(before)],
                           'afterSnapshot': self.saved['snapshots'][str(after)],
                           'changedFiles': ['main.py'], 'changedFunctions': {'main.py': ['handler']}}
                raw = {'fixRequest': {'issues': [issue], 'validationFeedback': [new]}}
                focus = compact_fix_input(raw, repair_history=history)['activeRepairFocus']
                self.assertEqual(sorted(focus)[0], 'brief')
                brief = focus['brief']
                target, = brief['targets']
                check, = target['checks']
                self.assertNotIn('hostIssueProgress', target)  # Offline observations cannot grant a fix.
                self.assertNotIn('repairRound', brief)
                self.assertEqual(brief['previousEdit']['changedFunctions'], {'main.py': ['handler']})
                cases = check['caseFeedback']
                self.assertEqual(cases['malformed_list'], {'handlerEntry': 'UNKNOWN', 'confirmedConditions': {'response_status': 422},
                                                           'failed': {'password_exposed': True}})
                self.assertEqual(cases['db_error']['confirmedConditions'], {'injected': True})
                self.assertEqual(cases['db_error']['unverified'],
                                 dict.fromkeys(('response_status','password_exposed','hash_exposed'),'UNHANDLED_APP_EXCEPTION'))
                self.assertEqual(check['previousEditEffect']['status'], status)
                if before == 2:
                    self.assertEqual(check['previousEditEffect']['nowPassing'],
                                     [{'caseId': 'malformed_missing_email', 'itemId': 'password_exposed'}])
                    self.assertNotIn('malformed_missing_email', cases)
                else:
                    self.assertEqual(check['previousEditEffect']['counts'], {'UNCHANGED': 31})
                new['testVersion'] = 'inputs:' + 'b' * 64
                other = compact_fix_input(raw, repair_history=history)['activeRepairFocus']
                self.assertEqual(other['brief']['targets'][0]['checks'][0]['previousEditEffect'],
                                 {'status': 'NOT_COMPARABLE', 'reason': 'TEST_VERSION_UNKNOWN_OR_CHANGED'})

    def test_not_run_and_zero_keep_their_meanings_in_qa_case_feedback(self):
        qa = row('qa', 'case-A')
        qa['observations'] = [
            {'caseId': 'setup', 'itemId': 'count', 'kind': 'precondition', 'actual': 0,
             'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH'},
            {'caseId': 'setup', 'itemId': 'response', 'kind': 'assertion', 'actual': None,
             'status': 'NOT_RUN', 'reason': 'NOT_REACHED'}]
        focus = compact_fix_input({'fixRequest': {'issues': [qa_issue()], 'validationFeedback': [qa]}})['activeRepairFocus']
        check, = focus['brief']['targets'][0]['checks']
        self.assertEqual(check['caseFeedback']['setup'], {'failed': {'count': 0}, 'notRun': {'response': 'NOT_REACHED'}})
        self.assertNotIn('previousEditEffect', check)

    def test_missing_final_security_report_cannot_imply_a_pass_or_a_failed_edit(self):
        recheck = SimpleNamespace(role='SECURITY', requirement_id='req', test_id='common-id',
                                  prior_report_id='older', issue_id='issue')
        source = SimpleNamespace(previous_artifact_id='parent')
        result, = final_rechecks(SimpleNamespace(rechecks=[recheck]), source, [], {'QA_REPORT': object()}, None)
        self.assertIsNone(result['currentReportId'])
        self.assertIsNone(result['currentOutcome'])
        self.assertEqual(result['editEffect'], {'status': 'NOT_COMPARABLE', 'reason': 'CURRENT_REPORT_MISSING'})

    def test_passing_one_item_with_other_observations_changing_is_mixed(self):
        old, new = self.measured(2), self.measured(3)
        item = next(o for o in new['observations'] if o['itemId'] == 'response_status' and o['status'] == 'PASS')
        item['actual'] = 401
        self.assertEqual(self.compare(old, new)['status'], 'CHANGED')

    def test_runner_input_versions_and_mixed_run_evidence(self):
        digest = 'b' * 64
        receipt = SimpleNamespace(scope=SimpleNamespace(protected_suite_ref=None), inputs={'inputsSha256':digest})
        self.assertEqual(measured_test_version(receipt), {'testVersion':'inputs:' + digest})
        self.assertEqual(measured_test_version(None), {})
        old, new = self.measured(2), self.measured(3)
        self.assertEqual(self.compare({**old, 'runId':'other'},new)['reason'], 'SOURCE_MISMATCH')
        self.assertEqual(self.compare(old,{**new,'executionManifest':{'snapshotSha256':'other'}})['reason'], 'SOURCE_MISMATCH')

    def test_only_observed_pass_can_resolve_an_unverified_item(self):
        old = self.measured(3)
        new = deepcopy(old)
        for item in new['observations']:
            if item['status'] != 'PASS':
                item.update(status='PASS', actual=500 if item['itemId']=='response_status' else False, reason=None)
        new['outcome'] = 'PASS'
        self.assertEqual(self.compare(old,new)['status'], 'RESOLVED')
        next(item for item in new['observations'] if item['caseId']=='db_error')['actual'] = None
        self.assertEqual(self.compare(old,new)['status'], 'NOT_COMPARABLE')

    def test_changed_actual_and_regressions_are_visible(self):
        old=self.measured(3)
        new=deepcopy(old)
        next(o for o in new['observations'] if o['itemId']=='response_status' and o['status']=='PASS')['actual']=401
        result=self.compare(old,new)
        self.assertEqual((result['status'],result['counts']['OBSERVATION_CHANGED']),('CHANGED',1))
        passed=next(o for o in new['observations'] if o['kind']=='assertion' and o['status']=='PASS')
        passed.update(status='FAIL',actual=True,reason='EXPECTATION_MISMATCH')
        result=self.compare(old,new)
        self.assertEqual((result['status'],result['counts']['NEW_FAILURE']),('REGRESSION',1))


def test_generated_channel_progress_needs_same_test_inputs_and_measured_channels():
    from orchestrator.domain.repair_progress import compare_check
    version = 'inputs:' + 'a' * 64
    history = dict(beforeVersion=2, afterVersion=3, beforeSnapshot='before', afterSnapshot='after')
    marks = {'REJECTION_RESPONSE': 'EXPOSED', 'REQUEST_LOGS': 'EXPOSED'}
    old = dict(codeVersion=2, testVersion=version, outcome='FAIL', outputChecks=marks)
    new = dict(old, codeVersion=3)
    assert compare_check(old, new, history)['status'] == 'NO_PROGRESS'
    new['outputChecks'] = {**marks, 'REJECTION_RESPONSE': 'CHECKED'}
    assert compare_check(old, new, history)['status'] == 'PARTIAL_IMPROVEMENT'
    assert compare_check(old, dict(new, testVersion='inputs:' + 'b'*64), history)['status'] == 'NOT_COMPARABLE'
    assert compare_check(old, dict(new, outputChecks={'REQUEST_LOGS': 'CHECKED'}), history)['status'] == 'NOT_COMPARABLE'
    no_exposure = {'REQUEST_LOGS': 'CHECKED'}
    assert compare_check(dict(old, outputChecks=no_exposure), dict(new, outputChecks=no_exposure), history)['status'] == 'NOT_COMPARABLE'
    incomplete = {'REQUEST_LOGS': 'INCOMPLETE'}
    assert compare_check(dict(old, outputChecks=incomplete), dict(new, outputChecks=incomplete), history)['status'] == 'NOT_COMPARABLE'
