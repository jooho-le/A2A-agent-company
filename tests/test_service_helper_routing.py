"""Replay the real caller shape using only our fixed benign app, no LLM."""
import asyncio
import inspect
import json
import sys
from types import SimpleNamespace as N
from unittest.mock import patch
from uuid import uuid4

import pytest

from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report
from agents.runtime.test_fix import technical_reasons, TestFixRequired
from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.runtime.security_services import SecurityRuntimeServices
from mcp_tools.tools.unit_report import UnitTestCase
from orchestrator.application.work_failures import read_work_failures
from orchestrator.domain import AgentRole, FinalVerdict, WorkflowRun, WorkflowStep, WorkflowStatus, WorkflowStepStatus
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID
from test_service_validation_lifecycle import run_trusted_app


def test_actual_failed_keyword_client_shape_runs_and_records_both_channels(tmp_path):
    report, code, _ = run_trusted_app(tmp_path, mode='client_keyword')
    parsed = parse_unit_report(json.dumps(report), code)
    assert code == 0 and parsed.passed == 1 and parsed.failed == 0
    assert dict(parsed.tests[0].output_checks) == {'REQUEST_LOGS': 'CHECKED', 'OTHER_RESPONSE': 'CHECKED'}


@pytest.mark.parametrize('mode', ['wrong_client', 'missing_bound_path', 'missing_module_client', 'bad_request_keyword'])
def test_proven_helper_argument_errors_return_to_validator_not_developer(tmp_path, mode):
    report, code, _ = run_trusted_app(tmp_path, mode=mode)
    parsed = parse_unit_report(json.dumps(report), code)
    assert parsed.failed == 0 and parsed.passed == 0 and parsed.skipped == 1
    reasons = technical_reasons([], parsed.tests)
    assert reasons == {'NOT_RUN_FIXTURE_ARGUMENT_INVALID'}
    feedback = TestFixRequired(reasons, ['security-tests']).task_input(
        {'serviceContract': {'scenarioKey': 'SERVICE-WEB'}}, {'kind': 'READY'})['testRunFeedback']
    assert feedback['code'] == 'GENERATED_TESTS_NOT_RUN'
    assert 'same frozen Source' in feedback['instruction']
    assert 'Correct only your generated tests' in feedback['instruction']
    assert 'modify Source to hide a product defect' in feedback['instruction']


@pytest.mark.parametrize('mode', ['unrelated_typeerror', 'inner_request_typeerror'])
def test_unrelated_typeerror_after_real_http_check_remains_failure(tmp_path, mode):
    report, code, _ = run_trusted_app(tmp_path, mode=mode)
    assert code == 1 and report['failed'] == 1 and report['skipped'] == 0


def test_advertised_helper_contract_matches_actual_signature(tmp_path):
    contract = unit_runner.service_fixture_contract()['methods']['check_http_output']
    with unit_runner._service_app_test_api(tmp_path):
        helper = sys.modules[unit_runner.SERVICE_FIXTURE_MODULE].ServiceTestCase.check_http_output
        signature = inspect.signature(helper)
        required = [name for name, parameter in signature.parameters.items()
                    if parameter.kind is parameter.KEYWORD_ONLY and parameter.default is parameter.empty]
        optional = [name for name, parameter in signature.parameters.items()
                    if parameter.kind is parameter.KEYWORD_ONLY and parameter.default is not parameter.empty]
        assert required == contract['keywordOnly']
        assert optional == contract['optionalKeywordOnly']


def test_bound_client_keyword_is_consistent_between_precheck_and_runtime(tmp_path):
    (tmp_path/'test_security.py').write_text('''from _host_service_test import ServiceTestCase
from _host_test_data import new_password
class SecurityTests(ServiceTestCase):
 def test_exposure(self):
  sample = new_password()
  self.check_http_output(client=self.client, method='POST', path='/signup', sensitive=sample, json={'password':sample})
''')
    rows=[{'selector':'security-tests','testId':'test_security.SecurityTests.test_exposure'}]
    assert inspect_generated_tests(tmp_path, [('security-tests','test_*.py')], rows, 'testCases', service_fixture=True) == []


def test_terminal_no_progress_explains_exact_stop_instead_of_generic_staff_failure():
    run = WorkflowRun(scenario_id=SERVICE_WEB_ID, request_text='회원가입 API', status=WorkflowStatus.FINISHED,
        verdict=FinalVerdict.FAIL)
    step = WorkflowStep(run_id=run.run_id, agent_role=AgentRole.DEVELOPER,
        status=WorkflowStepStatus.FAILED, failure_code='LLM_DEVELOPMENT_NO_PROGRESS')
    result = asyncio.run(read_work_failures(None, None, run, [step]))
    assert len(result) == 1 and result[0]['code'] == 'LLM_DEVELOPMENT_NO_PROGRESS'
    assert '실제 코드 변경 없이' in result[0]['message']
    assert result[0]['recoveryOwner'] == 'DEVELOPER'


@pytest.mark.parametrize('repair_available', [True, False])
def test_real_security_result_assembly_preserves_technical_error_instead_of_product_fail(repair_available):
    """The actual Security assembly consumes the runner's closed not-run row."""
    requirement = uuid4()
    actual = UnitTestCase(test_id='test_security.SecurityTests.test_exposure', outcome='SKIP',
        details='NOT_RUN_FIXTURE_ARGUMENT_INVALID')
    receipt = N(report=N(tests=(actual,)), inputs={'inputsSha256':'a'*64}, report_ref='artifact://offline/security.json')
    evidence = N(execution_id=uuid4(), evidence_ref='artifact://offline/attempt.json')
    services = object.__new__(SecurityRuntimeServices)
    services._protected_cases = (); services._protected_selectors = (); services._test_selectors = ('security-tests',)
    decision = N(test_cases=(N(selector='security-tests', test_id=actual.test_id, requirement_id=requirement),))
    execution = N(metadata=N(requirement_ids=(requirement,)),
        configuration=N(scenario_contract={'scenarioKey':'SERVICE-WEB', 'requirements':[]}))
    design = N(model_dump=lambda **_: {'endpoints':[{'requestFields':['password']}]})
    with patch.object(services, '_measure_scopes', return_value=({'security-tests':(receipt,evidence)}, {}, {})), \
            patch('orchestrator.domain.service_design.design_for_execution', return_value=design):
        operation = services._run_tests(execution, decision, None, test_fix_available=repair_available)
        if repair_available:
            with pytest.raises(TestFixRequired) as caught:
                asyncio.run(operation)
            assert caught.value.reasons == ('NOT_RUN_FIXTURE_ARGUMENT_INVALID',)
            assert caught.value.selectors == ('security-tests',)
        else:
            result = asyncio.run(operation)
            assert result[requirement][0] == 'SECURITY_TESTS_NOT_RUN'
            assert result[requirement][2][0]['outcome'] == 'UNVERIFIED'
