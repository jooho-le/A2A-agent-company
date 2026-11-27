"""Model input de-duplication preserves every measured check and audit source."""
from copy import deepcopy
import json
import unittest
from types import SimpleNamespace

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_baseline import with_repair_baseline


class DeveloperInputProjectionTests(unittest.TestCase):
    def input(self):
        rows = [{'reportArtifactId': 'report', 'requirementId': 'req', 'testId': status,
                 'outcome': 'PASS' if status == 'PASS' else 'FAIL',
                 'observationStatus':'COMPLETE', 'reason': None, 'observations':
                 [{'caseId': 'case', 'itemId': status, 'actual': None if status in ('NOT_RUN','UNVERIFIED') else True,
                   'status': status, 'reason': 'OBSERVATION_UNAVAILABLE' if status == 'UNVERIFIED' else None}]}
                for status in ('PASS','FAIL','NOT_RUN','UNVERIFIED')]
        return {'fixRequest': {'attempt': 3, 'validationFeedback': rows,
            'issues': [{'category': 'SECURITY_REQUIREMENT_FAILURE', 'requirementIds': ['req'],
                'reportArtifactId': 'report', 'description': json.dumps({'code':'MEASURED_SECURITY_TESTS','tests':rows})}],
            'inputArtifacts': [{'artifactId': 'report', 'artifactType': 'QA_REPORT', 'artifactUri': 'artifact://original/report',
                'record': {'tests': [{'requirementId':'req','testId': r['testId'],
                    'details':json.dumps(r), 'normalizedLocation':'main.py:10'} for r in rows]}}]},
            'runConfiguration': {'scenarioContract': {'fixed':True}, 'frozenScenarioContractJson':'{"fixed":true}'}}

    def test_every_observation_and_reason_survives_once_and_original_is_immutable(self):
        source=self.input(); before=deepcopy(source); projected=compact_fix_input(source)
        key=lambda r:r['testId']
        # V2 (Codex review 2026-11-18 #4): failing/unverified/not-run checks keep every observation; the
        # fully passing check is listed by identifiers, with a digest of its observations, to keep passing.
        rows=sorted(projected['fixRequest']['validationFeedback'],key=key)
        original=sorted(before['fixRequest']['validationFeedback'],key=key)
        for row, old in zip(rows, original):
            if old['outcome']=='PASS':
                from agents.runtime.developer_input import _digest
                self.assertEqual(row, {'reportArtifactId':'report','requirementId':'req','testId':'PASS',
                    'outcome':'PASS','observationStatus':'COMPLETE','observationCount':1,
                    'observationsSha256':_digest(old['observations']),'keepPassing':True,
                    'hostDecision':'KEEP_PASSING','detailRecorded':True})
            else:
                # V3 (Codex review 2026-11-19 #1·#3): only the Host decision is added; every observation stays.
                self.assertEqual(row, {**old, 'hostDecision':'FIX_TARGET', 'detailRecorded':True})
        self.assertEqual(projected['developerInputProjection']['passingChecksSummarized'],1)
        self.assertEqual(source,before)
        artifact=projected['fixRequest']['inputArtifacts'][0]
        self.assertEqual(len(artifact['recordSha256']),64)
        for test in artifact['record']['tests']:
            self.assertNotIn('observations',json.loads(test['details']))
            self.assertEqual(test['normalizedLocation'],'main.py:10')
        self.assertEqual(projected['fixRequest']['validationFeedback'][-1]['outcome'],'PASS')
        self.assertEqual(projected['developerInputProjection']['checkCount'],4)
        self.assertNotIn('frozenScenarioContractJson',projected['runConfiguration'])

    def test_missing_feedback_keeps_original_report_and_unknown_diagnostics(self):
        source=self.input(); source['fixRequest']['validationFeedback'].pop()
        source['fixRequest']['issues'][0]['description']='This unique diagnostic is not duplicated.'
        projected=compact_fix_input(source)
        self.assertEqual(projected['fixRequest']['inputArtifacts'][0]['record']['tests'][-1],
                         source['fixRequest']['inputArtifacts'][0]['record']['tests'][-1])
        self.assertEqual(projected['fixRequest']['issues'],source['fixRequest']['issues'])

    def test_code_references_and_unique_diagnostics_remain_in_security_report_and_issue(self):
        source=self.input(); fix=source['fixRequest']
        detail={'code':'MEASURED_SECURITY_TESTS','tests':deepcopy(fix['validationFeedback']),
                'codeReferences':[{'path':'main.py','line':42}], 'diagnostic':'unique error cause'}
        fix['issues'][0]['description']=json.dumps(detail)
        fix['inputArtifacts'][0].update(artifactType='SECURITY_REPORT',record={'requirementResults':[
            {'requirementId':'req','details':json.dumps(detail), 'actualResult':'FAIL'}]})
        projected=compact_fix_input(source)['fixRequest']
        report=json.loads(projected['inputArtifacts'][0]['record']['requirementResults'][0]['details'])
        issue=json.loads(projected['issues'][0]['description'])
        for compact in (report,issue):
            self.assertEqual(compact['codeReferences'],detail['codeReferences'])
            self.assertEqual(compact['diagnostic'],'unique error cause')
            self.assertTrue(all('observations' not in t for t in compact['tests']))

    def test_same_id_with_different_observations_is_not_removed(self):
        source=self.input()
        source['fixRequest']['validationFeedback'][0]['observations'][0]['actual']=False
        result=compact_fix_input(source)
        self.assertEqual(result['fixRequest']['inputArtifacts'][0]['record']['tests'][0],
                         source['fixRequest']['inputArtifacts'][0]['record']['tests'][0])

    def test_unfamiliar_details_are_retained_without_crashing(self):
        for encoded in ('[]','null','{"tests": [null]}','invalid json'):
            with self.subTest(encoded=encoded):
                source=self.input()
                source['fixRequest']['inputArtifacts'][0]['record']['tests'][0]['details']=encoded
                result=compact_fix_input(source)
                self.assertEqual(result['fixRequest']['inputArtifacts'][0]['record']['tests'][0]['details'],encoded)

    def test_rejected_build_instruction_is_audit_reference_only(self):
        source=self.input(); source['fixRequest']['issues']=[{'issueId':'old','description':'Fix obsolete Argon2 TypeError'}]
        checkpoint=SimpleNamespace(repair_baseline={'sourceArtifactId':'restored','rejectedSourceArtifactId':'rejected',
            'fixRequest': self.input()['fixRequest'], 'validationFeedback':self.input()['fixRequest']['validationFeedback']})
        projected=with_repair_baseline(source,checkpoint)
        wire=json.dumps(projected)
        self.assertNotIn('TypeError',wire); self.assertNotIn('rejectedCandidateFixRequest',wire)
        self.assertEqual(projected['repairBaseline']['rejectedCandidateHistory']['issueIds'],['old'])
        self.assertNotIn('validationFeedback',projected['repairBaseline'])
        self.assertIn('TypeError',source['fixRequest']['issues'][0]['description'])

    def test_initial_input_is_unchanged(self):
        initial={'plan':['initial implementation']}
        self.assertIs(compact_fix_input(initial),initial)

    def test_restored_work_keeps_rejected_build_evidence_separate_from_active_targets(self):
        source = self.input()
        active = deepcopy(source['fixRequest'])
        failure = dict(issueId='obsolete', category='BUILD_CODE_FAILURE', sourceArtifactId='rejected',
                       reportArtifactId='build', codeVersion=2)
        diagnostic = dict(ruleId='SCN001_ATTRIBUTE_UNAVAILABLE', normalizedLocation='auth.py:79',
                          sourceExpression='router.exception_handler')
        record = dict(artifactId='build', sourceArtifactId='rejected', codeVersion=2, executionOutcome='FAIL',
                      failureKind='PRODUCT', diagnostics=[{**diagnostic, 'message':'OBSOLETE_INSTRUCTION'}])
        source['fixRequest'] = {'issues':[failure], 'inputArtifacts':[
            {'artifactId':'build', 'artifactType':'BUILD_REPORT', 'record':record}]}
        checkpoint = SimpleNamespace(repair_baseline={'sourceArtifactId':'restored', 'rejectedSourceArtifactId':'rejected',
                                                      'fixRequest':active})
        def brief():
            return with_repair_baseline(source,checkpoint)['activeRepairFocus']['brief']
        measured = brief()
        self.assertEqual(measured['lastRejectedBuild']['buildDiagnostics'], [diagnostic])
        self.assertNotIn('obsolete', [t['issueId'] for t in measured['targets']])
        self.assertNotIn('OBSOLETE_INSTRUCTION', json.dumps(measured))
        checkpoint.repair_baseline['rejectedSourceArtifactId'] = 'other'
        self.assertNotIn('lastRejectedBuild', brief())
        checkpoint.repair_baseline['rejectedSourceArtifactId'] = 'rejected'
        record['codeVersion'] = 1
        self.assertNotIn('lastRejectedBuild', brief())

    def test_receipt_pointers_restore_every_occurrence_across_roles(self):
        from agents.runtime.developer_input import _digest
        receipt = {'evidenceRef': 'shared-ref', 'executionId': 'execution', 'attempts': [{'status': 'OK'}]}
        different = {**receipt, 'attempts': [{'status': 'ERROR'}]}
        unknown = {'evidenceRef': 'partial'}
        source = {'fixRequest': {'inputArtifacts': [
            {'artifactId': 'qa', 'artifactType': 'QA_REPORT', 'record': {'tests': [
                {'toolEvidence': receipt, 'executionEvidence': [receipt, different]}]}},
            {'artifactId': 'security', 'artifactType': 'SECURITY_REPORT', 'record': {'requirementResults': [
                {'executionEvidence': [different, unknown, unknown], 'toolEvidence': receipt}]}}
        ]}}
        original = deepcopy(source)
        projected = compact_fix_input(source)
        self.assertEqual(source, original)
        artifacts = projected['fixRequest']['inputArtifacts']
        qa = artifacts[0]['record']['tests'][0]
        security = artifacts[1]['record']['requirementResults'][0]
        self.assertEqual(qa['executionEvidence'], [receipt, different])
        self.assertEqual(security['executionEvidence'][1:], [unknown, unknown])
        for link, expected in ((qa['toolEvidence'], receipt), (security['toolEvidence'], receipt),
                               (security['executionEvidence'][0], different)):
            target = projected
            for key in link['sameReceiptAs'].removeprefix('#/').split('/'):
                target = target[int(key)] if isinstance(target, list) else target[key]
            self.assertEqual(target, expected)
        for artifact, before in zip(artifacts, original['fixRequest']['inputArtifacts']):
            self.assertEqual(artifact['recordSha256'], _digest(before['record']))
            self.assertNotIn('checksLocation', artifact)
