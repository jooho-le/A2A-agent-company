"""Negative requests execute without granting positive secret-output evidence."""
import json
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch
from uuid import uuid4

import pytest

from agents.runtime.generated_tests_precheck import inspect_generated_tests
from agents.runtime.security_test_scope import service_output_coverage
from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import parse_unit_report
from mcp_tools.tools.unit_report import UnitTestCase
from agents.runtime.security_services import SecurityRuntimeServices
from agents.runtime.test_fix import TestFixRequired
from orchestrator.domain.repair_progress import _generated_observations, _retains_generated_coverage
import asyncio


def execute_case(tmp_path, kind, *, leak_hash=False, leak_sample=False, success=False):
    class Client:
        calls = 0
        def request(self, method, path, **request):
            self.calls += 1
            submitted = request.get('json', {}).get('password')
            if leak_hash:
                body = json.dumps({'stored': '$'+'argon2id$'+'z'*24}).encode()
            elif leak_sample:
                body = json.dumps({'echo': submitted}).encode()
            else:
                body = b'{"detail":"invalid input"}'
            return N(status_code=201 if success else 400, content=body, headers=N(raw=[]))
    client = Client()
    samples = []
    class Case(unittest.TestCase):
        def test_request(self):
            from _host_test_data import new_password, check_http_output
            sample = {'none': lambda: None, 'literal_empty': lambda: '',
                      'factory_empty': lambda: new_password(length=0),
                      'factory_short': lambda: new_password(length=3),
                      'normal': lambda: new_password(length=12)}[kind]()
            samples.append(sample)
            payload = {} if sample is None else {'password':sample}
            response = check_http_output(client, 'POST', '/signup', sensitive=sample, json=payload)
            self.assertEqual(response.status_code, 201 if success else 400)
    with patch.object(unittest.TestLoader, 'discover', return_value=unittest.TestSuite([Case('test_request')])):
        report, code = unit_runner.run_tests(tmp_path, tmp_path, capture_output_checks=True, fixture_usage_repair=True)
    return parse_unit_report(json.dumps(report), code), code, client.calls, samples


@pytest.mark.parametrize('kind', ['none','literal_empty','factory_empty','factory_short'])
def test_negative_secret_input_executes_real_request_without_false_complete_observation(tmp_path, kind):
    report, code, calls, _ = execute_case(tmp_path, kind)
    assert calls == 1 and code == 0 and report.passed == 1 and report.skipped == 0
    mark='INCOMPLETE' if kind=='factory_short' else 'NO_SAMPLE'
    assert dict(report.tests[0].output_checks) == {'REQUEST_LOGS':mark,'OTHER_RESPONSE':mark}


def test_bad_input_acceptance_still_fails_the_test_instead_of_hiding_product_behavior(tmp_path):
    class Client:
        def request(self, method, path, **request):
            return N(status_code=201, content=b'{}', headers=N(raw=[]))
    class Case(unittest.TestCase):
        def test_invalid(self):
            from _host_test_data import check_http_output
            response = check_http_output(Client(),'POST','/signup',sensitive='',json={'password':''})
            self.assertEqual(response.status_code,400)
    with patch.object(unittest.TestLoader,'discover',return_value=unittest.TestSuite([Case('test_invalid')])):
        report,code=unit_runner.run_tests(tmp_path,tmp_path,capture_output_checks=True,fixture_usage_repair=True)
    assert code == 1 and report['failed'] == 1 and report['skipped'] == 0


@pytest.mark.parametrize('kind', ['none','literal_empty','factory_empty','factory_short','normal'])
def test_encoded_hash_exposure_remains_failure_even_on_negative_requests(tmp_path, kind):
    report, code, calls, _ = execute_case(tmp_path, kind, leak_hash=True)
    assert calls == 1 and code == 1 and report.failed == 1
    assert dict(report.tests[0].output_checks)['OTHER_RESPONSE'] == 'EXPOSED'


def test_normal_runtime_sample_still_proves_observation_and_detects_raw_exposure(tmp_path):
    report, code, _, samples = execute_case(tmp_path, 'normal')
    assert code == 0 and dict(report.tests[0].output_checks) == {'REQUEST_LOGS':'CHECKED','OTHER_RESPONSE':'CHECKED'}
    assert samples[0] not in json.dumps(report.tests[0].to_dict())
    other=tmp_path/'leak'; other.mkdir()
    report,code,_,_=execute_case(other,'normal',leak_sample=True)
    assert code == 1 and report.failed == 1
    assert dict(report.tests[0].output_checks)['OTHER_RESPONSE'] == 'EXPOSED'


@pytest.mark.parametrize('value', ['None', "''", 'sample'])
def test_precheck_allows_no_secret_mode_without_accepting_literal_credentials(tmp_path,value):
    source='from _host_service_test import ServiceTestCase\nclass SecurityTests(ServiceTestCase):\n def test_invalid(self):\n  self.check_http_output(method="POST",path="/signup",sensitive='+value+',json={})\n'
    (tmp_path/'test_security.py').write_text(source)
    cases=[{'selector':'security-tests','testId':'test_security.SecurityTests.test_invalid'}]
    assert inspect_generated_tests(tmp_path,[('security-tests','test_*.py')],cases,'testCases',service_fixture=True)==[]
    (tmp_path/'test_security.py').write_text(source.replace('sensitive='+value,'sensitive="unregistered-value"'))
    assert any(row['reasonCode']=='TEST_OUTPUT_ARGUMENT_INVALID' for row in inspect_generated_tests(
        tmp_path,[('security-tests','test_*.py')],cases,'testCases',service_fixture=True))


def test_incomplete_negative_observation_does_not_satisfy_security_coverage():
    design={'endpoints':[{'requestFields':['email','password']}]}
    negative={'testId':'invalid','outcome':'PASS','outputChecks':{'REQUEST_LOGS':'NO_SAMPLE','OTHER_RESPONSE':'NO_SAMPLE'}}
    assert set(service_output_coverage(design,[negative])[1]) == {'OUTPUT_NOT_OBSERVED_REQUEST_LOGS','OUTPUT_NOT_OBSERVED_OTHER_RESPONSE'}
    positive={'testId':'valid','outcome':'PASS','outputChecks':{'REQUEST_LOGS':'CHECKED','OTHER_RESPONSE':'CHECKED'}}
    assert service_output_coverage(design,[negative,positive])[1] == []
    truncated={'testId':'truncated','outcome':'PASS','outputChecks':{'REQUEST_LOGS':'INCOMPLETE','OTHER_RESPONSE':'INCOMPLETE'}}
    assert set(service_output_coverage(design,[negative,positive,truncated])[1]) == {'OUTPUT_NOT_OBSERVED_REQUEST_LOGS','OUTPUT_NOT_OBSERVED_OTHER_RESPONSE'}


def test_no_sample_is_neither_repair_progress_nor_a_replacement_for_exposure_evidence():
    marks={'REQUEST_LOGS':'NO_SAMPLE','OTHER_RESPONSE':'NO_SAMPLE'}
    assert _generated_observations({'outcome':'PASS','testVersion':'inputs:'+'a'*64,'outputChecks':marks}) is None
    assert not _retains_generated_coverage({'outputChecks':{'OTHER_RESPONSE':'EXPOSED'}}, {'outputChecks':marks})


def test_security_assembly_routes_real_empty_input_acceptance_to_product_fix():
    requirement=uuid4(); services=object.__new__(SecurityRuntimeServices)
    services._protected_cases=();services._protected_selectors=();services._test_selectors=('security-tests',)
    actual=UnitTestCase(test_id='test_security.SecurityTests.test_invalid',outcome='FAIL',
        output_checks=(('REQUEST_LOGS','NO_SAMPLE'),('OTHER_RESPONSE','NO_SAMPLE')))
    receipt=N(report=N(tests=(actual,)),inputs={'inputsSha256':'a'*64},report_ref='artifact://offline/security.json')
    evidence=N(execution_id=uuid4(),evidence_ref='artifact://offline/attempt.json')
    decision=N(test_cases=(N(selector='security-tests',test_id=actual.test_id,requirement_id=requirement),))
    execution=N(metadata=N(requirement_ids=(requirement,)),configuration=N(scenario_contract={'scenarioKey':'SERVICE-WEB','requirements':[]}))
    design=N(model_dump=lambda **_: {'endpoints':[{'requestFields':['password']}]})
    with patch.object(services,'_measure_scopes',return_value=({'security-tests':(receipt,evidence)}, {}, {})), \
            patch('orchestrator.domain.service_design.design_for_execution',return_value=design):
        result=asyncio.run(services._run_tests(execution,decision,None,test_fix_available=True))
    assert result[requirement][0] == 'FAIL'
    assert result[requirement][2][0]['outcome'] == 'FAIL'
