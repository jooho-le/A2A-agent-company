"""Archive-bound reproduction metadata, never raw code in the prose wire path."""
from dataclasses import replace
from hashlib import sha256
import json
import unittest

import pytest

import test_mcp_unit_store as fixtures
from mcp_tools.tools.unit_report import parse_unit_report
from mcp_tools.tools.unit_inputs import UnitTestInputsError
from orchestrator.a2a.requests import A2AWorkflowMetadata, build_send_message_request
from orchestrator.core.security import redact_data
from orchestrator.domain.validation_feedback import measured_test_version
from orchestrator.infrastructure.repair_reproduction import _file_reference, generated_reproduction


SOURCE = b'''from _host_service_test import ServiceTestCase
from _host_test_data import new_password
class TestSignup(ServiceTestCase):
    def test_email(self):
        password = new_password()
        response = self.check_http_output(method="POST", path="/signup", sensitive=password,
            json={"email": "fixture@example.invalid", "password": ""})
        self.assertIn(response.status_code, (400, 422))
'''


class RepairReproductionTests(unittest.TestCase):
    def fixture(self, kind='QA_TESTS'):
        f = fixtures.UnitTestOutputStoreTests('runTest')
        self.addCleanup(f.doCleanups)
        f.setUp()
        f.qa_context(kind=kind)
        if kind == 'QA_TESTS':
            f.inputs = f.inputs_for({'tests/test_signup.py': SOURCE})
        raw = f.report_json(outcome='FAIL')
        f.report = parse_unit_report(raw, 1)
        f.result = replace(f.result, exit_code=1, stdout=raw)
        record = f.publish()
        feedback = {'role':'QA','runId':str(f.run.run_id),'sourceArtifactId':str(f.source.artifact_id),
            'testId':'run_unit_tests:'+record.scope.name+':tests.TestSignup.test_email', 'outcome':'FAIL',
            **measured_test_version(record)}
        return f, record, feedback

    def test_normal_password_code_crosses_the_real_a2a_boundary_as_closed_metadata(self):
        f, record, feedback = self.fixture()
        assert redact_data({'content':SOURCE.decode()}) != {'content':SOURCE.decode()}  # observed old bug
        result = generated_reproduction(f.repository, f.source, [feedback])
        assert len(result) == 1
        assert result[0]['sourceDelivery'] == 'HOST_PRIVATE_ARCHIVE'
        assert redact_data(result) == result
        file, = result[0]['files']
        assert 'content' not in file and file['sha256'] == sha256(SOURCE).hexdigest()
        assert file['staticHintsAreExecutionEvidence'] is False
        hint, = file['staticCaseHints'][0]['possibleHttpCalls']
        assert (hint['method'], hint['path'], hint['encoding']) == ('POST','/signup','JSON')
        assert {'fieldName':'password','shape':'EMPTY_STRING'} in hint['fieldShapes']
        assert 'fixture@example.invalid' not in json.dumps(result)
        assert 'new_password()' not in json.dumps(result)
        metadata = A2AWorkflowMetadata(run_id=f.run.run_id, workflow_step_id=record.workflow_step_id,
            scenario_id=f.run.scenario_id, attempt=0, code_version=1)
        build_send_message_request({'fixRequest':{'reproduction':result}}, metadata)
        assert generated_reproduction(f.repository,f.source,[{**feedback,'runId':str(record.workflow_step_id)}]) == []
        # The original bytes remain in the private archive for exact Host replay.
        from mcp_tools.tools.generated_recheck import _files
        with f.repository._connection() as db:
            assert _files(db,record)['tests/test_signup.py'] == SOURCE

    def test_protected_test_sources_never_enter_the_metadata_projection(self):
        f, _, feedback = self.fixture('PROTECTED')
        assert generated_reproduction(f.repository,f.source,[feedback]) == []

    def test_credential_literals_still_fail_at_the_original_source_boundary(self):
        with pytest.raises(UnitTestInputsError, match='SECRET_DENIED'):
            fixtures.UnitTestOutputStoreTests.inputs_for({'tests/test_private.py':
                b'api_key = "fixture-credential-literal-denied"\n'})


@pytest.mark.parametrize('expression', [
    "self.client.post('/signup', json={'password': None})",
    "self.client.request('POST', '/signup', json={'password': None})",
    "self.check_http_output('POST', '/signup', sensitive=None, json={'password': None})",
    "check_http_output(self.client, method='POST', path='/signup', sensitive=None, json={'password': None})",
])
def test_static_request_shapes_never_copy_input_values_or_claim_execution(expression):
    content = ('class C:\n def test_blank(self):\n  response = '+expression+'\n').encode()
    file = _file_reference('tests/test_case.py', content, {'test_case.C.test_blank'})
    assert file['staticHintsAreExecutionEvidence'] is False
    call, = file['staticCaseHints'][0]['possibleHttpCalls']
    assert call['fieldShapes'] == [{'fieldName':'password','shape':'NULL'}]
    assert redact_data(file) == file
