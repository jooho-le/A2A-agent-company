"""Revalidation after the prior validation attempt stopped before publishing its role report."""
import json
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from pydantic import ValidationError

from orchestrator.domain import AgentRole
from orchestrator.domain.developer_artifacts import ReadyRecheck
from orchestrator.domain.repair_progress import final_rechecks, protected_issue_progress
from orchestrator.infrastructure.protected_recheck import current_protected_observation


class Snapshot:
    artifact_type = 'SOURCE'

    def __init__(self, run, version, previous=None):
        self.run_id, self.code_version = run, version
        self.artifact_id, self.commit_hash, self.snapshot_sha256 = uuid4(), str(version) * 40, str(version) * 64
        self.previous_artifact_id = previous

    def execution_manifest(self):
        return self

    def model_dump(self, **_options):
        return {'snapshotSha256': self.snapshot_sha256, 'projectArtifactId': str(self.artifact_id)}


class IndependentRecheckTests(unittest.TestCase):
    def setUp(self):
        run = uuid4()
        self.old = Snapshot(run, 1)
        self.new = Snapshot(run, 2, self.old.artifact_id)
        self.req, self.step, self.new_step = uuid4(), uuid4(), uuid4()
        self.receipt, self.new_receipt = uuid4(), uuid4()
        self.test_name = 'test_saved.signup'
        self.selector = 'protected-security'
        version = 'practice:' + 'a' * 64
        self.row = {'caseId': 'spaces', 'itemId': 'http_status', 'kind': 'assertion',
                    'status': 'FAIL', 'actual': 400, 'reason': 'EXPECTATION_MISMATCH'}
        self.issue = SimpleNamespace(issue_id=uuid4(), run_id=run, source_artifact_id=self.old.artifact_id,
            code_version=1, reporter=AgentRole.SECURITY, requirement_ids=[self.req], evidence_refs=[],
            protected_receipt=SimpleNamespace(workflow_step_id=self.step, receipt_id=self.receipt,
                test_id=f'run_unit_tests:{self.selector}:{self.test_name}', test_version=version,
                observations_json=json.dumps([self.row])))
        self.fields = dict(issue_id=self.issue.issue_id, role='SECURITY', test_id=self.issue.protected_receipt.test_id,
                           requirement_id=self.req, unresolved_cases=['spaces'])
        self.check = ReadyRecheck(**self.fields, prior_protected_receipt_id=self.receipt)
        self.review = SimpleNamespace(working_base_commit=self.old.commit_hash, source_artifact_id=self.new.artifact_id,
                                      rechecks=[self.check])
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE protected_scope_claims(workflow_step_id,selector,source_artifact_id,run_id,tool_name,policy_sha256);
            CREATE TABLE protected_scope_executions(workflow_step_id,selector,execution_manifest_id,tool_name);
            CREATE TABLE unit_test_execution_records(execution_manifest_id,run_id,source_artifact_id,workflow_step_id);
        """)
        for source, step, receipt in ((self.old, self.step, self.receipt), (self.new, self.new_step, self.new_receipt)):
            self.db.execute("INSERT INTO protected_scope_claims VALUES(?,?,?,?,?,?)",
                (str(step), self.selector, str(source.artifact_id), str(run), 'run_unit_tests', 'frozen'))
            self.db.execute("INSERT INTO protected_scope_executions VALUES(?,?,?,?)",
                (str(step), self.selector, str(receipt), 'run_unit_tests'))
            self.db.execute("INSERT INTO unit_test_execution_records VALUES(?,?,?,?)",
                (str(receipt), str(run), str(source.artifact_id), str(step)))
        self.case = SimpleNamespace(test_id=self.test_name, outcome='PASS',
            observations_json=json.dumps([{**self.row, 'status': 'PASS', 'actual': 201, 'reason': None}]))
        self.execution = SimpleNamespace(role=AgentRole.SECURITY, execution_manifest=self.new,
            report=SimpleNamespace(tests=[self.case]), execution_manifest_id=self.new_receipt,
            scope=SimpleNamespace(kind='PROTECTED', name=self.selector,
                                  protected_suite_ref='artifact://frozen/practice-' + 'a' * 64))
        decoder = patch('mcp_tools.tools.unit_store.UnitTestOutputStore._decode', return_value=self.execution)
        self.decode = decoder.start()
        self.addCleanup(decoder.stop)

    def test_prior_report_or_receipt_but_never_both(self):
        self.assertNotIn('priorReportId', self.check.model_dump(by_alias=True))
        legacy = ReadyRecheck(**self.fields, prior_report_id=self.receipt)
        self.assertNotIn('priorProtectedReceiptId', legacy.model_dump(by_alias=True))
        for fields in ({}, {'prior_report_id': self.receipt, 'prior_protected_receipt_id': self.receipt}):
            with self.assertRaises(ValidationError):
                ReadyRecheck(**self.fields, **fields)

    def test_exact_receipts_resolve_old_failure_without_either_project_report(self):
        result = current_protected_observation(self.db, self.issue, self.new)
        self.assertEqual(result['outcome'], 'PASS')
        measured, = final_rechecks(self.review, self.new, [self.old, self.new], {}, None,
            issues=[self.issue], protected_rows={self.issue.issue_id: result})
        self.assertNotIn('priorReportId', measured)
        self.assertEqual(measured['priorProtectedReceiptId'], str(self.receipt))
        self.assertEqual(measured['currentProtectedReceiptId'], str(self.new_receipt))
        self.assertIsNone(measured['currentReportId'])
        self.assertEqual(measured['editEffect']['status'], 'RESOLVED')

    def test_missing_prior_case_is_unverified_even_when_the_new_suite_reports_pass(self):
        self.issue.protected_receipt.observations_json = json.dumps([self.row, {**self.row, 'caseId': 'missing'}])
        self.assertIsNone(current_protected_observation(self.db, self.issue, self.new))
        measured, = final_rechecks(self.review, self.new, [self.old, self.new], {}, None,
                                   issues=[self.issue], protected_rows={})
        self.assertEqual(measured['caseRechecks'], [{'caseId': 'spaces', 'outcome': 'UNVERIFIED'}])

    def test_passing_different_item_with_same_case_id_does_not_clear_previous_failed_item(self):
        self.case.observations_json = json.dumps([{**self.row, 'itemId': 'different_assertion',
                                                'status': 'PASS', 'actual': 201, 'reason': None}])
        self.assertIsNone(current_protected_observation(self.db, self.issue, self.new))
        measured, = final_rechecks(self.review, self.new, [self.old, self.new], {}, None,
                                   issues=[self.issue], protected_rows={})
        self.assertEqual(measured['caseRechecks'], [{'caseId': 'spaces', 'outcome': 'UNVERIFIED'}])

    def test_other_passing_case_and_changed_policy_cannot_clear_issue(self):
        self.case.test_id = 'test_saved.different_case'
        self.assertIsNone(current_protected_observation(self.db, self.issue, self.new))
        self.case.test_id = self.test_name
        self.db.execute('UPDATE protected_scope_claims SET policy_sha256=? WHERE workflow_step_id=?',
                        ('different', str(self.new_step)))
        self.decode.reset_mock()
        self.assertIsNone(current_protected_observation(self.db, self.issue, self.new))
        self.decode.assert_not_called()

    def test_independent_failures_keep_the_same_repeat_policy_for_the_exact_case(self):
        from copy import deepcopy
        new_issue = deepcopy(self.issue)
        new_issue.code_version = self.new.code_version
        new_issue.source_artifact_id = self.new.artifact_id
        new_issue.protected_receipt.receipt_id = self.new_receipt
        self.assertEqual(protected_issue_progress(self.issue, new_issue, self.old, self.new), 'NO_PROGRESS')
        old = [self.row, {**self.row, 'caseId': 'other_case'}]
        new = [self.row, {**old[1], 'actual': 201, 'reason': None, 'status': 'PASS'}]
        self.issue.protected_receipt.observations_json = json.dumps(old)
        new_issue.protected_receipt.observations_json = json.dumps(new)
        self.assertEqual(protected_issue_progress(self.issue, new_issue, self.old, self.new), 'PARTIAL_IMPROVEMENT')
        new_issue.protected_receipt.test_id += '_different'
        self.assertEqual(protected_issue_progress(self.issue, new_issue, self.old, self.new), 'NOT_COMPARABLE')

    def test_identical_failure_is_measured_again(self):
        self.case.outcome = 'FAIL'
        self.case.observations_json = json.dumps([self.row])
        current = current_protected_observation(self.db, self.issue, self.new)
        result, = final_rechecks(self.review, self.new, [self.old, self.new], {}, None,
            issues=[self.issue], protected_rows={self.issue.issue_id: current})
        self.assertEqual((result['currentOutcome'], result['editEffect']['status']), ('FAIL', 'NO_PROGRESS'))
