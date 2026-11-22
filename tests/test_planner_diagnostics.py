"""Failure positions and closed reasons; no provider, secrets or raw exception text."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agents.core.identity import SinglePhase
from agents.roles.planner_contract import PlannerContractError, validate_planner_decision
from agents.roles.planner_diagnostics import PlannerValidationIssue, PlannerValidationReport
from agents.single.contracts import SingleContractError, build_single_draft_contract
from orchestrator.application.control_input import validate_control_input
from test_planner_contract import SCENARIO, decision, task


class PlannerDiagnosticsTests(unittest.TestCase):
    def report(self, data, **kwargs):
        before = deepcopy(data)
        with self.assertRaises(PlannerContractError) as caught:
            validate_planner_decision(data, kwargs.get('scenario', SCENARIO))
        self.assertEqual(data, before)
        error = caught.exception
        self.assertEqual(error.args, ('PLANNER_OUTPUT_INVALID',))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        return error.validation.public()

    def test_missing_requirement_has_exact_approved_key_not_submitted_prose(self):
        data = decision(implementationPlan=[task(requirementIds=[str(x) for x in SCENARIO.requirement_ids[:-1]],
            description='private-model-prose-do-not-log')])
        report = self.report(data)
        self.assertEqual(report['stage'], 'PLAN_MODEL')
        self.assertEqual(report['issueCount'], 1)
        self.assertEqual(report['issues'][0]['reasonCode'], 'REQUIREMENT_NOT_COVERED')
        self.assertEqual(report['issues'][0]['requirementKey'], 'REQ-008')
        self.assertEqual(report['issues'][0]['fieldPath'], '/implementationPlan')
        self.assertNotIn('private-model-prose', json.dumps(report))
        self.assertEqual(len(report['candidateSha256']), 64)
        self.assertEqual(report, self.report(data))

    def test_independent_missing_duplicate_and_dependency_issues_are_all_retained(self):
        first = str(SCENARIO.requirement_ids[0])
        report = self.report(decision(implementationPlan=[task(requirementIds=[first, first],
            dependsOn=['TASK-001', 'TASK-UNKNOWN', 'TASK-UNKNOWN']), task(taskId='TASK-001', requirementIds=[first])]))
        codes = {item['reasonCode'] for item in report['issues']}
        self.assertTrue({'REQUIREMENT_NOT_COVERED', 'REQUIREMENT_DUPLICATE', 'TASK_ID_DUPLICATE',
                         'DEPENDENCY_SELF', 'DEPENDENCY_UNKNOWN', 'DEPENDENCY_DUPLICATE'} <= codes)
        self.assertEqual({x['requirementKey'] for x in report['issues'] if 'requirementKey' in x},
                         {f'REQ-{i:03d}' for i in range(2,9)})
        self.assertNotIn('TASK-UNKNOWN', json.dumps(report))

    def test_cycle_marks_only_members_and_not_a_task_depending_on_the_cycle(self):
        report = self.report(decision(implementationPlan=[task(dependsOn=['TASK-002']),
            task(taskId='TASK-002', dependsOn=['TASK-001']), task(taskId='TASK-003', dependsOn=['TASK-002'])]))
        self.assertEqual({x['fieldPath'] for x in report['issues'] if x['reasonCode']=='DEPENDENCY_CYCLE'},
                         {'/implementationPlan/0/dependsOn', '/implementationPlan/1/dependsOn'})

    def test_decision_conflicts_include_questions_and_assumptions_together(self):
        report = self.report(decision(questions=['private-question'], unapprovedAssumptions=['private-assumption'] * 2))
        self.assertEqual({x['reasonCode'] for x in report['issues']},
                         {'QUESTIONS_FORBIDDEN','ASSUMPTIONS_FORBIDDEN','ASSUMPTION_DUPLICATE'})
        self.assertNotIn('private-', json.dumps(report))

    def test_schema_paths_do_not_echo_unknown_properties_or_secret_values(self):
        data = decision()
        data['api_key=fixture-private-secret'] = 'another-private-value'
        data['implementationPlan'][0]['taskId'] = 'private-invalid-task'
        del data['implementationPlan'][0]['title']
        report = self.report(data)
        self.assertEqual(report['stage'], 'MODEL_SCHEMA')
        self.assertTrue({'', '/implementationPlan/0/taskId', '/implementationPlan/0/title'} <=
                        {x['fieldPath'] for x in report['issues']})
        self.assertNotIn('private', json.dumps(report))

    def test_text_failure_has_a_field_position_without_credential_copy(self):
        report = self.report(decision(implementationPlan=[task(title='api_key=fixture-private-secret')]))
        self.assertEqual(report['stage'], 'TEXT')
        self.assertIn('/implementationPlan/0/title', [x['fieldPath'] for x in report['issues']])
        self.assertNotIn('fixture-private', json.dumps(report))

    def test_host_schema_loading_failure_is_not_reported_as_model_coverage_error(self):
        with patch('agents.roles.planner_contract._canonical_schema', side_effect=OSError('private-host-path')):
            report = self.report(decision())
        self.assertEqual(report['stage'], 'ARTIFACT_SCHEMA_LOAD')
        self.assertEqual(report['issues'][0]['reasonCode'], 'HOST_SCHEMA_UNAVAILABLE')
        self.assertNotIn('private-host-path', json.dumps(report))

    def test_final_schema_rejection_keeps_safe_artifact_field_path(self):
        with patch('agents.roles.planner_contract._canonical_schema', return_value={
            'type':'object', 'properties':{'implementationPlan':{'maxItems':0}}}):
            report = self.report(decision())
        self.assertEqual(report['stage'], 'ARTIFACT_SCHEMA')
        self.assertEqual(report['issues'][0]['fieldPath'], '/implementationPlan')
        self.assertEqual(report['issues'][0]['reasonCode'], 'ARTIFACT_SCHEMA_INVALID')

    def test_large_failure_list_has_exact_count_and_explicit_truncation(self):
        report = self.report(decision(implementationPlan=[task() for _ in range(128)]))
        self.assertEqual(report['issueCount'], 128)
        self.assertEqual(len(report['issues']), 64)
        self.assertTrue(report['issuesTruncated'])

    def test_single_preserves_same_closed_report_without_exception_context(self):
        data = decision(implementationPlan=[task(requirementIds=[str(SCENARIO.requirement_ids[0])])])
        contract = build_single_draft_contract(SinglePhase.PLANNING, scenario=SCENARIO)
        with self.assertRaises(SingleContractError) as caught:
            contract.validate(data)
        self.assertEqual(caught.exception.planner_validation.public(), self.report(data))
        self.assertIsNone(caught.exception.__context__)

    def test_report_rejects_unbounded_unknown_or_untrusted_values(self):
        for changes in ({'fieldPath':'/private-secret'}, {'reasonCode':'private-exception'},
                        {'requirementKey':'api_key=private-secret'}, {'message':'private-prose'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                PlannerValidationIssue.model_validate({'fieldPath':'','reasonCode':'JSON_INVALID',**changes})
        issue = PlannerValidationIssue(fieldPath='', reasonCode='JSON_INVALID')
        with self.assertRaises(ValueError):
            PlannerValidationReport(stage='JSON', issues=(issue,), issueCount=2)
        with self.assertRaises(ValueError):
            validate_control_input({'plannerValidation':{'issueCount':0}})
