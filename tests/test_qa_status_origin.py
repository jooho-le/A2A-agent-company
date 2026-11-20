"""The practice runner and independent detail recorder agree on the origin of each 5xx."""
import importlib
import json
import sys
import threading
import unittest
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient

from evaluation import check_details
from evaluation.protected_tests.tests.signup import signup_link as link
from mcp_tools.tools import unit_runner
from orchestrator.domain.validation_feedback import observation_json


def test_response_and_escaped_exception_have_the_same_score_but_distinct_origins(tmp_path):
    with patch.dict(sys.modules, {'signup_link': link}):
        qa = importlib.import_module('evaluation.protected_tests.tests.signup.signup_qa_checks')
    plan = link.details.plan('QA-SIGNUP-002', {'invalid':['one', 'two']})
    case_ids = ('invalid_email_1', 'invalid_email_2')
    app = FastAPI()

    @app.post(link.SIGNUP_PATH)
    def signup(payload: dict):
        if payload['email'] == 'first':
            return Response(status_code=500)
        raise ValueError('private-error')

    class Fixture(link.SignupCheck):
        def test_status(self):
            self.detail_recorder = link.details.Recorder('QA-SIGNUP-002', {'invalid':['one', 'two']})
            service = link.Service()
            self.addCleanup(service.close)
            service._database_guard = threading.Lock()
            service._database_events = {True: 0, False: 0}
            service.client = TestClient(app)
            self.addCleanup(service.client.close)
            for case, address in zip(case_ids, ('first', 'second')):
                status = service.measured_signup(address, 'example')
                self.assertFalse(qa.observe_status(self, case, status, required=False))
            self.fail('two failed status checks')

    case = Fixture('test_status')
    suite = unittest.TestSuite([case])
    with patch.object(unit_runner.unittest.TestLoader, 'discover', return_value=suite):
        report, code = unit_runner.run_tests(tmp_path, tmp_path, capture_observations=True)
    assert code == 1 and 'two failed status checks' in report['tests'][0]['details']
    detailed = check_details._validated(case.detail_recorder.rows, plan)
    wire = json.loads(observation_json(report['tests'][0]['observations']))
    for rows in (detailed, wire):
        failed = [r for r in rows if r['itemId'] == 'http_status' and r['status'] == 'FAIL']
        assert [(r['caseId'], r['actual'], r['statusOrigin']) for r in failed] == [
            (case_ids[0], 500, 'HTTP_RESPONSE'), (case_ids[1], 500, 'ESCAPED_EXCEPTION')]
        assert 'private-error' not in json.dumps(rows)


@pytest.mark.parametrize('overrides', [{'statusOrigin': 'arbitrary'}, {'statusOrigin': 'UNKNOWN'},
    {'actual': 501, 'statusOrigin': 'ESCAPED_EXCEPTION'}, {'itemId':'user_delta'},
    {'status':'UNVERIFIED', 'actual':None, 'reason':'UNHANDLED_APP_EXCEPTION'}])
def test_invalid_origin_metadata_is_rejected(overrides):
    row = {'caseId':'invalid_email_1', 'itemId':'http_status', 'kind':'assertion', 'status':'FAIL',
           'actual':500, 'reason':'EXPECTATION_MISMATCH', 'statusOrigin':'HTTP_RESPONSE'}
    with pytest.raises(ValueError):
        observation_json([row | overrides])
