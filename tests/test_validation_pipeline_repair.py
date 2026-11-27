"""Offline regressions for the observed service failures; no provider or Docker."""
import ast
import asyncio
import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
from uuid import uuid4

import pytest

from agents.llm.recovery import FailureRoute
from agents.llm.validation import role_report
from agents.roles.contract_explain import security_issues
from agents.roles.security_contract import build_security_output_contract, validate_security_decision
from agents.roles.security_draft import SecurityTestDraft, PENDING_TESTS
from agents.runtime.generated_tests_precheck import _starts_test_client
from agents.runtime.security_test_scope import service_output_coverage
from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report, UnitReportError
from orchestrator.application.work_failures import read_work_failures
from orchestrator.domain import AgentRole, WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.validation_feedback import case_feedback, _feedback
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.service_start_contract import service_validation_requests


def test_status_mismatch_survives_runner_receipt_and_fix_feedback_without_raw_error():
    class Fixture(unittest.TestCase):
        def test_status(self):
            response = N(status_code=422)
            self.assertEqual(response.status_code, 400)
    suite = unittest.TestSuite([Fixture('test_status')])
    result = unit_runner._Result(unit_runner._expected_cases(suite),
        roots=(('TEST', str(Path(__file__).parent.resolve())),))
    suite.run(result)
    report = parse_unit_report(json.dumps(result.report()), 1)
    feedback = _feedback(case_feedback(report.tests[0]))
    assert feedback['failureDiagnostic']['comparison'] == {'actualStatus': 422, 'expectedStatus': 400}
    assert feedback['failureDiagnostic']['origin'] == 'TEST'
    assert 'AssertionError:' not in json.dumps(feedback)
    assert parse_unit_report(json.dumps(report.to_dict()), 1) == report


@pytest.mark.parametrize('protected', [False, True])
def test_membership_status_mismatch_carries_actual_and_allowed_statuses_only_for_generated_tests(protected):
    class Fixture(unittest.TestCase):
        def test_status(self):
            response = N(status_code=200)
            self.assertIn(response.status_code, (400, 422))
    suite = unittest.TestSuite([Fixture('test_status')])
    result = unit_runner._Result(unit_runner._expected_cases(suite), capture_observations=protected,
        roots=(('TEST', str(Path(__file__).parent.resolve())),))
    suite.run(result)
    report = parse_unit_report(json.dumps(result.report()), 1)
    diagnostic = _feedback(case_feedback(report.tests[0]))['failureDiagnostic']
    if protected:
        assert 'comparison' not in diagnostic
    else:
        assert diagnostic['comparison'] == {'actualStatus':200, 'expectedStatuses':[400, 422]}
        from agents.runtime.developer_input import compact_fix_input
        from test_developer_input_projection import DeveloperInputProjectionTests
        task = DeveloperInputProjectionTests('runTest').input()
        next(r for r in task['fixRequest']['validationFeedback'] if r['testId'] == 'FAIL')['failureDiagnostic'] = diagnostic
        assert compact_fix_input(task)['activeRepairFocus']['brief']['unitFailureDiagnostics'][0]['failureDiagnostic'] == diagnostic


@pytest.mark.parametrize('expected', [[], [True], [400]*2, [99], [600], list(range(400, 417))])
def test_membership_diagnostic_schema_rejects_invented_or_unbounded_status_lists(expected):
    from orchestrator.domain.unit_failure_diagnostics import validate_failure_diagnostic
    with pytest.raises(ValueError):
        validate_failure_diagnostic({'kind':'ASSERTION', 'exceptionType':'AssertionError', 'origin':'TEST',
            'comparison':{'actualStatus':200, 'expectedStatuses':expected}})


@pytest.mark.parametrize('damage', ['unknown_field', 'raw_message', 'bool_status', 'pass_diagnostic'])
def test_failure_diagnostics_reject_untrusted_values(damage):
    facts = {'kind': 'ASSERTION', 'exceptionType': 'AssertionError', 'origin': 'TEST'}
    row = {'testId': 'Fixture.test_status', 'outcome': 'FAIL', 'failureDiagnostic': facts}
    if damage == 'unknown_field': facts['private'] = 'do-not-copy'
    if damage == 'raw_message': facts['exceptionType'] = 'do-not-copy'
    if damage == 'bool_status': facts['comparison'] = {'actualStatus': True, 'expectedStatus': 400}
    if damage == 'pass_diagnostic': row['outcome'] = 'PASS'
    passed = row['outcome'] == 'PASS'
    raw = {'format': 'unittest-v1', 'total': 1, 'passed': int(passed), 'failed': int(not passed), 'skipped': 0, 'tests': [row]}
    with pytest.raises(UnitReportError): parse_unit_report(json.dumps(raw), 0 if passed else 1)


@pytest.mark.parametrize(('source', 'valid'), [
    ('client = TestClient(app)\nwith open("notes") as f: pass', False),
    ('client = TestClient(app)\nwith lock: pass', False),
    ('with TestClient(app) as client: pass', True),
    ('self.client = TestClient(app)\nwith self.client: pass', True),
    ('client = TestClient(app)\nstack.enter_context(client)', True),
    ('self.client = self.enterContext(TestClient(app))', True),
    ('client = TestClient(app)\nclient.__enter__()', True),
    ('client = TestClient(app)\nother.__enter__()', False),
])
def test_only_actual_client_context_entry_satisfies_lifespan(source, valid):
    assert _starts_test_client(ast.parse(source)) is valid


def test_host_pending_inventory_never_supplies_a_security_pass_or_test():
    req = uuid4()
    wire = SecurityTestDraft({'main.py': b'pass\n'}, 'a'*64, pending_requirement_ids=[req])
    wire.output(build_security_output_contract((req,), (), ('main.py',), ('security-tests',)))
    candidate = {'kind': 'READY', 'questions': [], 'requirementReviews': [], 'findingReviews': [],
        'testCases': [{'selector': 'security-tests', 'testId': 'test_app.Security.test_input',
            'requirementId': str(req), 'title': 'Input boundary', 'expectedResult': 'Approved criterion'}]}
    decoded = wire.decode(candidate)
    decision = validate_security_decision(decoded, (req,), (), ('main.py',), ('security-tests',))
    assert decision.requirement_reviews[0].proposed_outcome == 'UNVERIFIED'
    assert decision.requirement_reviews[0].rationale == PENDING_TESTS
    assert decoded['requirementReviews'][0]['references'] == []
    assert candidate['requirementReviews'] == []
    assert decoded['testCases'] == candidate['testCases']
    bad = {**candidate, 'requirementReviews': [{'requirementId': str(req), 'proposedOutcome': 'PASS',
        'rationale': 'guessed', 'references': []}]}
    issues = wire.explain(bad, lambda data: security_issues(data, [req], [], ['main.py'], ['security-tests']))
    assert {row['reasonCode'] for row in issues} == {'PENDING_STATE_REQUIRED'}


def test_role_diagnostic_roundtrips_through_durable_recovery_record():
    output = build_security_output_contract((uuid4(),), (), ('main.py',), ('security-tests',))
    report = role_report(output.schema,
        [{'fieldPath': '/testCases/0/testId', 'reasonCode': 'CASE_DUPLICATE', 'private': 'never-copy'}], sequence=3)
    route = FailureRoute(sequence=3, operationSequence=1, code='OUTPUT_CONTRACT_INVALID',
        category='MODEL_FORMAT', action='REGENERATE', reason='ALLOWED', llmValidation=report)
    stored = FailureRoute.model_validate(route.public())
    assert stored.validation_report.issues[0].reason_code == 'CASE_DUPLICATE'
    assert 'never-copy' not in json.dumps(stored.public())


@pytest.mark.parametrize('status', [WorkflowStatus.FIXING, WorkflowStatus.REVALIDATING, WorkflowStatus.WAITING_INPUT])
def test_history_failure_does_not_tell_owner_to_abandon_active_repair(status):
    run = WorkflowRun(scenario_id=uuid4(), request_text='회원가입 API', status=status,
        code_version=2, fix_attempt=1, resume_state=WorkflowStatus.FIXING if status is WorkflowStatus.WAITING_INPUT else None)
    step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.SECURITY, code_version=1,
        status=WorkflowStepStatus.FAILED, failure_code='LLM_VALIDATION_DRAFT_EXHAUSTED')
    failure = asyncio.run(read_work_failures(None, None, run, [step]))[0]
    assert failure['nextAction'] == 'WAIT_FOR_REVALIDATION'
    assert failure['recovering'] and not failure['blocking']
    assert '새 작업으로' not in failure['instruction']


def test_fix_projection_keeps_unit_failure_facts_in_the_active_repair_brief():
    from agents.runtime.developer_input import compact_fix_input
    from test_developer_input_projection import DeveloperInputProjectionTests
    source = DeveloperInputProjectionTests('runTest').input()
    facts = {'kind': 'ASSERTION', 'origin': 'TEST', 'exceptionType': 'AssertionError',
             'comparison': {'actualStatus': 422, 'expectedStatus': 400}}
    next(row for row in source['fixRequest']['validationFeedback'] if row['testId'] == 'FAIL')['failureDiagnostic'] = facts
    projected = compact_fix_input(source)
    brief = projected['activeRepairFocus']['brief']
    assert brief['unitFailureDiagnostics'] == [{'testId': 'FAIL', 'failureDiagnostic': facts}]
    assert 'before entering' in brief['unitFailureInstruction']
    assert 'activeRepairFocus' not in source


def test_protected_suite_never_exports_assertion_expectations_as_diagnostics():
    class Fixture(unittest.TestCase):
        def test_status(self):
            response = N(status_code=422)
            self.assertEqual(response.status_code, 400)
    suite = unittest.TestSuite([Fixture('test_status')])
    result = unit_runner._Result(unit_runner._expected_cases(suite), capture_observations=True,
        roots=(('TEST', str(Path(__file__).parent.resolve())),))
    suite.run(result)
    case = parse_unit_report(json.dumps(result.report()), 1).tests[0]
    assert 'comparison' not in case_feedback(case)['failureDiagnostic']


def test_provider_failure_is_available_with_agent_offline_and_keeps_work(tmp_path):
    repository = SQLiteWorkflowRepository(tmp_path/'workflow.db')
    run = WorkflowRun(scenario_id=uuid4(), request_text='API', status=WorkflowStatus.HUMAN_REVIEW,
        resume_state=WorkflowStatus.IMPLEMENTING)
    step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER,
        status=WorkflowStepStatus.FAILED, failure_code='LLM_PROVIDER_ERROR', a2a_task_id='offline-task')
    with repository._transaction() as db:
        db.execute('CREATE TABLE agent_runtime_events(sequence INTEGER PRIMARY KEY, run_id TEXT, workflow_step_id TEXT, payload_json TEXT)')
        for index, detail in enumerate([
            {'failureDiagnostic': {'httpStatus': 500, 'private': 'never-copy'}},
            {'recovery': {'code': 'PROVIDER_TRANSIENT_RETRY'}},
            {'failureDiagnostic': {'httpStatus': 500}},
        ]):
            db.execute('INSERT INTO agent_runtime_events VALUES(?,?,?,?)', (index, str(run.run_id), str(step.workflow_step_id), json.dumps({'detail': detail})))
    failure = asyncio.run(read_work_failures(repository, None, run, [step]))[0]
    assert failure['httpStatus'] == 500 and failure['providerRetriesUsed'] == 1
    assert failure['workPreserved'] and failure['productFailure'] is False
    assert failure['nextAction'] == 'CHECK_PROVIDER'
    assert 'never-copy' not in json.dumps(failure)


def test_empty_body_probe_uses_only_explicit_required_json_contract():
    media = {'schema': {'type': 'object', 'required': ['email', 'password']}}
    operation = {'requestBody': {'content': {'application/json': media}}}
    schema = {'paths': {'/signup': {'post': operation}}}
    design = {'endpoints': [{'method': 'POST', 'path': '/signup', 'bodyFormat': 'JSON', 'rejectionStatuses': [400, 409]}]}
    assert service_validation_requests(schema, design) == [('POST', '/signup', (400, 409))]
    design['endpoints'][0]['rejectionStatuses'].append(422)
    assert service_validation_requests(schema, design) == []


@pytest.mark.parametrize('leak', ['none', 'logs', 'body', 'escaped_body'])
def test_generic_helper_checks_real_logs_and_response_without_exposing_values(tmp_path, leak):
    sink = io.StringIO()
    logger = logging.getLogger('aio.offline.output.check')
    handler = logging.StreamHandler(sink)
    old_level, old_propagate = logger.level, logger.propagate
    logger.addHandler(handler); logger.setLevel(logging.INFO); logger.propagate = False
    class Client:
        def request(self, _method, _path, **kwargs):
            value = kwargs['json']['password']
            if leak == 'logs': logger.info(value)
            body = json.dumps({'value': value}, ensure_ascii=leak == 'escaped_body').encode() if leak in ('body', 'escaped_body') else b'{"ok":true}'
            return N(content=body, headers=N(raw=[]), status_code=201)
    class Fixture(unittest.TestCase):
        def test_output(self):
            from _host_test_data import new_password, check_http_output
            sample = new_password(kind='unicode' if leak == 'escaped_body' else 'ascii')
            response = check_http_output(Client(), 'POST', '/signup', sensitive=sample, json={'password': sample})
            self.assertEqual(response.status_code, 201)
    try:
        with patch.object(unit_runner.unittest.TestLoader, 'discover', return_value=unittest.TestSuite([Fixture('test_output')])):
            report, code = unit_runner.run_tests(tmp_path, tmp_path, capture_output_checks=True)
        parsed = parse_unit_report(json.dumps(report), code)
        marks = dict(parsed.tests[0].output_checks)
        assert marks['REQUEST_LOGS'] == ('EXPOSED' if leak == 'logs' else 'CHECKED')
        assert marks['OTHER_RESPONSE'] == ('EXPOSED' if leak in ('body', 'escaped_body') else 'CHECKED')
        assert code == (0 if leak == 'none' else 1)
        assert parsed.tests[0].details is None or parsed.tests[0].details.startswith('AssertionError: REQUEST_OUTPUT_EXPOSURE')
    finally:
        logger.removeHandler(handler); handler.close(); sink.close()
        logger.setLevel(old_level); logger.propagate = old_propagate


def test_security_test_title_does_not_count_as_log_or_storage_evidence():
    design = {'endpoints': [{'requestFields': ['email', 'password']}]}
    cases = [{'testId': 'test_no_log_leak', 'outcome': 'PASS'}]
    coverage, gaps = service_output_coverage(design, cases)
    assert set(gaps) == {'OUTPUT_NOT_OBSERVED_REQUEST_LOGS', 'OUTPUT_NOT_OBSERVED_OTHER_RESPONSE'}
    cases[0]['outputChecks'] = {'REQUEST_LOGS': 'CHECKED', 'OTHER_RESPONSE': 'CHECKED'}
    assert service_output_coverage(design, cases)[1] == []
    assert 'storedHash' not in coverage
    assert service_output_coverage({'endpoints': [{'requestFields': ['title']}]}, cases) == (None, [])


@pytest.mark.parametrize('rewrite', [True, False])
@pytest.mark.parametrize('reason', ['TEST_CLIENT_NOT_STARTED', 'TEST_SERVICE_FIXTURE_REQUIRED', 'TEST_OUTPUT_ARGUMENT_INVALID'])
def test_invalid_lifespan_requires_a_real_test_write_before_ready(rewrite, reason):
    from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
    from agents.llm.engine import LLMEngine
    import test_llm_runtime as fixture
    owner = fixture.LLMRuntimeTests('runTest'); owner.setUp()
    fixed, routes = [], []
    ready = fixture.text_response('{"answer":"draft"}')
    write = fixture.tool_response(owner.call(name='write_test_file'))
    provider = fixture.FakeProvider(ready, write if rewrite else ready, ready)
    def executed(*_args):
        fixed.append(True)
        return {'answer': 'written'}
    engine = LLMEngine(role=AgentRole.QA, provider=provider,
        tools=(owner.tool('write_test_file'),), tool_executor=fixture.FakeToolExecutor(executed))
    budget = owner.budget(max_model_calls=6, max_tool_calls=3, max_output_corrections=2)
    async def run():
        return await engine.run(prompt=owner.prepared(AgentRole.QA), model=owner.model,
            output=owner.output, budget=budget, workspace_id=owner.workspace_id,
            close_invalid_qa_draft=True, recovery_sink=lambda route: routes.append(route.public()),
            output_check=lambda *_: None if fixed else {'code': 'GENERATED_TESTS_INVALID',
                'issues': [{'fieldPath': '/cases', 'reasonCode': reason}]})
    if rewrite:
        assert asyncio.run(run()).data == {'answer': 'draft'}
        assert fixed
    else:
        with pytest.raises(LLMRuntimeError) as caught: asyncio.run(run())
        assert caught.value.code is LLMErrorCode.VALIDATION_DRAFT
        assert caught.value.public_validation()['issues'][0]['reasonCode'] == reason
        assert not fixed
    assert provider.requests[1].required_tool == 'write_test_file'
    assert any(row.get('llmValidation', {}).get('issues') for row in routes)
