"""Development-loop observations only; fake providers/transport, no paid runs."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

from mcp_tools.tools import unit_runner
from mcp_tools.tools.unit_report import UnitReportError, parse_unit_report
from orchestrator.domain.validation_feedback import case_feedback, observation_json


def rows():
    return [
        {'caseId': 'signup', 'itemId': 'http_status', 'kind': 'assertion', 'actual': 201, 'status': 'PASS', 'reason': None},
        {'caseId': 'signup', 'itemId': 'user_delta', 'kind': 'assertion', 'actual': 0, 'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH'},
        {'caseId': 'account_1', 'itemId': 'm', 'kind': 'assertion', 'actual': None, 'status': 'NOT_RUN', 'reason': 'HASH_FORMAT_REQUIRED'},
        {'caseId': 'account_2', 'itemId': 'correct_password', 'kind': 'assertion', 'actual': None, 'status': 'UNVERIFIED', 'reason': 'OBSERVATION_UNAVAILABLE'},
    ]


def document(observations=None):
    case = {'testId': 'test_signup.Case.test_signup', 'outcome': 'FAIL'}
    if observations is not None:
        case['observations'] = observations
    return json.dumps({'format': 'unittest-v1', 'total': 1, 'passed': 0, 'failed': 1, 'skipped': 0, 'tests': [case]})


def test_observations_roundtrip_preserves_all_statuses_and_has_no_expectations():
    report = parse_unit_report(document(rows()), 1)
    assert report.to_dict()['tests'][0]['observations'] == rows()
    assert parse_unit_report(json.dumps(report.to_dict()), 1) == report
    feedback = case_feedback(report.tests[0])
    assert feedback['observationStatus'] == 'COMPLETE'
    assert [r['status'] for r in feedback['observations']] == ['PASS', 'FAIL', 'NOT_RUN', 'UNVERIFIED']
    assert feedback['observations'][1]['actual'] == 0
    assert 'expected' not in json.dumps(feedback)
    leaked_copy = report.to_dict()
    leaked_copy['tests'][0]['observations'][0]['actual'] = 999
    assert report.to_dict()['tests'][0]['observations'][0]['actual'] == 201


@pytest.mark.parametrize('damage', ['expected', 'secret', 'duplicate', 'unknown_status', 'unknown_reason', 'missing_value', 'bool_list', 'large', 'identifier', 'wrong_dependency_status'])
def test_invalid_or_secret_observations_cannot_enter_receipts(damage):
    data = rows()
    if damage == 'expected': data[0]['expected'] = 200
    if damage == 'secret': data[0]['actual'] = 'private-password-or-hash'
    if damage == 'duplicate': data.append(deepcopy(data[0]))
    if damage == 'unknown_status': data[0]['status'] = 'UNKNOWN'
    if damage == 'unknown_reason': data[1]['reason'] = 'PRIVATE_ERROR_TEXT'
    if damage == 'missing_value': data[0]['actual'] = None
    if damage == 'bool_list': data[0]['actual'] = [True]
    if damage == 'large': data[0]['actual'] = 10 ** 13
    if damage == 'identifier': data[0]['caseId'] = 'email@example.com'
    if damage == 'wrong_dependency_status': data[2]['status'] = 'UNVERIFIED'
    with pytest.raises(UnitReportError): parse_unit_report(document(data), 1)


def test_missing_and_invalid_receipts_are_unverified_and_parent_is_preserved():
    report = parse_unit_report(document(), 1)
    assert case_feedback(report.tests[0]) == {'observationStatus': 'UNVERIFIED', 'observations': [], 'reason': 'DETAIL_RECORD_MISSING'}
    invalid = replace(report.tests[0], observation_error='DETAIL_RECORD_INVALID')
    assert case_feedback(invalid)['reason'] == 'DETAIL_RECORD_INVALID'
    conflict = replace(report.tests[0], outcome='PASS', observations_json=observation_json(rows()))
    assert case_feedback(conflict)['reason'] == 'PARENT_DETAIL_MISMATCH'
    assert conflict.outcome == 'PASS'
    skipped = replace(conflict, outcome='SKIP')
    assert case_feedback(skipped)['reason'] == 'PARENT_DETAIL_MISMATCH'


def test_runner_collects_same_execution_only_when_explicitly_enabled():
    observations = rows() + [dict(caseId='signup', itemId='response_status', kind='precondition',
                                  actual=422, status='PASS', reason=None, handlerEntry='NOT_ENTERED')]
    class Fixture(unittest.TestCase):
        def test_signup(self):
            self.detail_recorder = SimpleNamespace(rows=[{**r, 'expected': {'operator': 'eq', 'value': 'DO_NOT_FORWARD'}} for r in observations])
            with self.subTest(item='user_delta'): self.fail('user_delta')
    for enabled in (False, True):
        with TemporaryDirectory() as directory:
            suite = unittest.TestSuite([Fixture('test_signup')])
            with patch.object(unit_runner.unittest.TestLoader, 'discover', return_value=suite):
                report, code = unit_runner.run_tests(Path(directory), Path(directory), capture_observations=enabled)
        assert code == 1 and report['failed'] == 1
        assert ('observations' in report['tests'][0]) == enabled
        if enabled:
            assert report['tests'][0]['observations'] == observations
            case = parse_unit_report(json.dumps(report), code).tests[0]
            assert case_feedback(case)['observations'][-1]['handlerEntry'] == 'NOT_ENTERED'
        assert 'DO_NOT_FORWARD' not in json.dumps(report)


def test_runner_drops_unsafe_actuals_before_output():
    class Fixture(unittest.TestCase):
        def test_signup(self):
            value = rows(); value[0]['actual'] = 'DO_NOT_FORWARD_SECRET'
            self.detail_recorder = SimpleNamespace(rows=value)
    with TemporaryDirectory() as directory:
        with patch.object(unit_runner.unittest.TestLoader, 'discover', return_value=unittest.TestSuite([Fixture('test_signup')])):
            report, code = unit_runner.run_tests(Path(directory), Path(directory), capture_observations=True)
    assert code == 0
    assert report['tests'][0]['observationError'] == 'DETAIL_RECORD_INVALID'
    assert 'DO_NOT_FORWARD_SECRET' not in json.dumps(report)


def test_protected_receipt_persists_observations_and_checks_integrity():
    import test_mcp_unit_store as fixtures
    from mcp_tools.tools.unit_store import UnitTestStoreError
    f = fixtures.UnitTestOutputStoreTests('test_immutable_receipt_roundtrip_and_closed_tool_output')
    f.setUp()
    try:
        f.qa_context(kind='PROTECTED')
        text = document(rows())
        f.report = parse_unit_report(text, 1)
        f.result = replace(f.result, stdout=text, exit_code=1)
        record = f.publish()
        restored = f.store.get(f.run.run_id, record.execution_manifest_id)
        assert restored.report.to_dict()['tests'][0]['observations'] == rows()
        assert 'expected' not in restored.stdout
        f.corrupt(record, report=b'{}')
        with pytest.raises(UnitTestStoreError): f.store.get(f.run.run_id, record.execution_manifest_id)
    finally:
        f.doCleanups()


def test_generated_test_cannot_claim_protected_observations():
    import test_mcp_unit_store as fixtures
    from mcp_tools.tools.unit_store import UnitTestStoreError
    f = fixtures.UnitTestOutputStoreTests('test_immutable_receipt_roundtrip_and_closed_tool_output')
    f.setUp()
    try:
        text = document(rows()); f.report = parse_unit_report(text, 1)
        f.result = replace(f.result, stdout=text, exit_code=1)
        with pytest.raises(UnitTestStoreError): f.publish()
    finally:
        f.doCleanups()


class LoopFeedbackServicesTests(unittest.IsolatedAsyncioTestCase):
    async def check_role(self, role):
        import test_qa_agent as qa_fixture
        import test_security_agent as security_fixture
        from agents.roles.qa_contract import QACaseBinding
        from agents.runtime.qa_services import QARuntimeServices
        from agents.runtime.security_services import SecurityRuntimeServices
        from mcp_tools.runtime import MCPDispatcher
        from mcp_tools.tools.unit import UnitTestTools
        from mcp_tools.tools.unit_config import UnitTestConfiguration, UnitTestScope
        from mcp_tools.tools.unit_store import UnitTestOutputStore
        from orchestrator.application.dispatch import PlannerRunDispatcher
        from orchestrator.domain.run_configuration import RunConfiguration
        from orchestrator.domain.states import AgentRole
        from orchestrator.sandbox.contracts import CLIResult, SandboxLimits
        from test_llm_runtime import FakeProvider, text_response
        reference = 'artifact://protected-tests/loop-feedback-fixture'
        if role is AgentRole.QA:
            f = qa_fixture.QAAgentTests('test_initial_qa_real_sdk_mcp_receipts_report_same_snapshot')
        else:
            f = security_fixture.SecurityAgentTests('test_zero_warnings_completed_report_is_not_security_pass')
        target = 'test_developer_agent.RunConfiguration' if role is AgentRole.QA else 'test_security_agent.RunConfiguration'
        with patch(target, side_effect=lambda **kw: RunConfiguration(protected_test_suite_ref=reference, **kw)):
            await f.asyncSetUp()
        for fn, args, kw in f._cleanups:
            self.addCleanup(fn, *args, **kw)
        f._cleanups.clear()
        ids = getattr(f, 'case_ids', [f'test_security.Case.test_req_{i}' for i in range(len(f.metadata.requirement_ids))])
        requirements = f.metadata.requirement_ids
        generated_name = 'qa-unit' if role is AgentRole.QA else 'security-unit'
        role_kw = {} if role is AgentRole.QA else {'role': 'SECURITY'}
        scopes = (
            UnitTestScope(name=generated_name, kind='QA_TESTS', **role_kw),
            UnitTestScope(name='protected-loop', kind='PROTECTED', source_directory='tests/protected',
                protected_files={'tests/protected/test_feedback.py': '# Host-selected inert fixture\n'},
                protected_suite_ref=reference, **role_kw),
        )
        unit = UnitTestConfiguration(scopes=scopes, limits=SandboxLimits(timeout_seconds=2, control_timeout_seconds=.5))
        f.mcp_configuration = replace(f.mcp_configuration, unit_test_configuration=unit)
        protected = tuple(QACaseBinding(tool_name='run_unit_tests', selector='protected-loop', test_id=identity,
            requirement_id=requirement, title='보호 검사', expected_result='DO_NOT_FORWARD_EXPECTATION')
            for identity, requirement in zip(ids, requirements))
        tools = UnitTestTools(f.artifacts, f.sandbox, UnitTestOutputStore(f.repository), configuration=unit, max_call_seconds=5)
        if role is AgentRole.QA:
            f.rebuild_dispatcher()
            service_class = QARuntimeServices
            provider = FakeProvider(f.write_response(), f.draft_response())
        else:
            from mcp_tools.tools.files import FileTools
            from mcp_tools.tools.security import SecurityScanTools
            files = FileTools(f.artifacts, frozen_source=f.mcp_configuration.frozen_source)
            scans = SecurityScanTools(f.artifacts, f.sandbox, f.scan_store, configuration=f.scan_configuration, max_call_seconds=5)
            f.dispatcher = MCPDispatcher(f.binding, f.registry, handlers={**files.handlers(role), **scans.handlers(role), **tools.handlers(role)}, max_call_seconds=5)
            service_class = SecurityRuntimeServices
            cases = [{'selector': generated_name, 'testId': identity, 'requirementId': str(requirement),
                      'title': '생성 검사', 'expectedResult': next(r.acceptance_criteria[0] for r in f.fixture.scenario.requirements if r.requirement_id == requirement)}
                     for identity, requirement in zip(ids, requirements)]
            def ready(request):
                data = f.draft(request); data['testCases'] = cases
                # docs/live-api/10: with generated tests the pre-run rationale is the fixed pending marker.
                for row in data['requirementReviews']:
                    row['rationale'] = 'HOST_TESTS_PENDING'
                return text_response(json.dumps(data))
            # A real bounded test write, not a Source read, prepares QA_TESTS.
            from agents.llm.contracts import ToolCall
            from test_llm_runtime import tool_response
            provider = FakeProvider(tool_response(ToolCall(call_id='security-write', name='write_test_file', arguments_json=json.dumps({
                'workspaceId': str(f.run.workspace_id), 'path': 'outputs/security/tests/test_feedback.py', 'content': '# inert fixture\n'}))), ready)
        f.services_factory = lambda execution: service_class(f.repository, f.registry, f.artifacts,
            mcp_configuration=f.mcp_configuration, client_factory=f.peer_client, protected_cases=protected)
        from signup_receipt_fixture import generated_signup_evidence
        scan_result = f.docker.start_result
        observed = rows()
        if role == AgentRole.SECURITY:
            observed[0].update(kind='precondition', itemId='response_status', actual=422, handlerEntry='NOT_ENTERED')
        def select_result(container):
            command = container['Config']['Cmd']
            if any('_security_runner.py' in item for item in command):
                f.docker.start_result = scan_result; f.docker.exit_code = 0
                return
            is_protected = 'tests/protected' in command
            cases = [{'testId': identity, 'outcome': 'FAIL' if is_protected and i == 0 else 'PASS',
                      **({'observations': observed if i == 0 else [rows()[0]]} if is_protected
                         else generated_signup_evidence(role.value, i + 1))}
                     for i, identity in enumerate(ids)]
            value = {'format': 'unittest-v1', 'total': len(cases), 'passed': len(cases)-int(is_protected),
                     'failed': int(is_protected), 'skipped': 0, 'tests': cases}
            f.docker.exit_code = int(is_protected)
            f.docker.start_result = CLIResult(returncode=int(is_protected), stdout=json.dumps(value).encode(), stderr=b'')
        f.docker.start_hook = select_result
        wire, _ = await f.run_to(provider, 'TASK_STATE_COMPLETED')
        report = f.parse_completed(wire)
        payload = PlannerRunDispatcher.build_fix_payload(f.run, f.step, f.fixture.plan, f.fixture.scenario, [], [f.source, report])
        feedback = [r for r in payload['fixRequest']['validationFeedback'] if 'protected-loop' in r['testId']]
        self.assertEqual(len(feedback), len(requirements))
        self.assertEqual([r['status'] for r in feedback[0]['observations']], ['PASS', 'FAIL', 'NOT_RUN', 'UNVERIFIED'])
        self.assertEqual(feedback[0]['observations'][1]['actual'], 0)
        if role == AgentRole.SECURITY:
            self.assertEqual(feedback[0]['observations'][0]['handlerEntry'], 'NOT_ENTERED')
        self.assertEqual(feedback[0]['codeVersion'], f.source.code_version)
        self.assertEqual(feedback[0]['sourceArtifactId'], str(f.source.artifact_id))
        self.assertTrue(feedback[0]['evidenceRefs'])
        self.assertNotIn('DO_NOT_FORWARD_EXPECTATION', json.dumps(payload['fixRequest']))
        self.assertNotIn('expectedResult', json.dumps(payload['fixRequest']))
        self.assertTrue(all(row['observations'][0]['status'] == 'PASS' for row in feedback[1:]))
        # Original immutable records retain their audit definitions.
        if role is AgentRole.QA:
            self.assertTrue(any(t.expected_result == 'DO_NOT_FORWARD_EXPECTATION' for t in report.tests))
        from orchestrator.domain.validation_feedback import validation_feedback
        with self.assertRaisesRegex(ValueError, 'CHECK_FEEDBACK_SOURCE_MISMATCH'):
            validation_feedback([f.source, report.model_copy(update={'code_version': 2})])
        return payload

    async def test_qa_receipt_through_real_service_to_developer_fix_feedback(self):
        from orchestrator.domain.states import AgentRole
        await self.check_role(AgentRole.QA)

    async def test_security_receipt_through_real_service_to_developer_fix_feedback(self):
        from orchestrator.domain.states import AgentRole
        await self.check_role(AgentRole.SECURITY)


@pytest.mark.parametrize('variant,scope,check_item,expected_status,expected_value', [
    ('seven_char_password', 'in-loop-qa', 'user_delta', 'FAIL', 1),
    ('weak_argon2_memory', 'in-loop-security', 't', 'PASS', 2),
    ('plaintext_storage', 'in-loop-security', 'm', 'NOT_RUN', None),
    ('app_missing', 'in-loop-qa', 'ready', 'UNVERIFIED', None),
])
def test_current_in_loop_protected_tests_export_safe_observations(variant, scope, check_item, expected_status, expected_value):
    from tests.evaluation import test_protected_signup as fixture
    code = fixture._RUN.replace('sys.argv[4])', 'sys.argv[4], capture_observations=True)')
    assert code != fixture._RUN
    with patch.object(fixture, '_RUN', code):
        output = fixture.run_protected(variant, scope=scope)
    report = parse_unit_report(json.dumps(output['report']), output['exit'])
    cases = [c for c in report.tests if ('004' if variant == 'seven_char_password' else '002') in c.test_id] if variant != 'app_missing' else report.tests
    assert cases
    observations = [row for case in cases for row in json.loads(case.observations_json)]
    matched = [row for row in observations if row['itemId'] == check_item and row['status'] == expected_status and row['actual'] == expected_value]
    assert matched
    assert all(set(row) == {'caseId', 'itemId', 'kind', 'actual', 'status', 'reason'} for row in observations)
    if variant == 'weak_argon2_memory':
        assert any(row['itemId'] == 'm' and row['status'] == 'FAIL' for row in observations)
        assert all(row['status'] != 'NOT_RUN' for row in observations)
    if variant == 'plaintext_storage':
        assert all(row['reason'] == 'HASH_FORMAT_REQUIRED' for row in observations if row['status'] == 'NOT_RUN')
