"""Codex 점검(2026-11-18) 3항목의 오프라인 확인.

① Security 생성 시험 ID도 QA처럼 Host 목록(enum)으로 고정한다.
② 교정 소진 종료를 QA만이 아니라 Security 단독·양쪽 소진까지 같은 증거 규칙으로 처리한다
   (FINISHED UNVERIFIED + 끝난 쪽 보고서 보존 + 결과 패키지).
③ Host 정책에 새 규칙 버전, 입력 증거에 실제 보낸 도구·응답 스키마 해시를 남긴다.

생성 코드는 실행하지 않는다. 시험 파일은 Host가 읽기만 하는 신뢰된 합성 텍스트다.
"""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from uuid import UUID, uuid4

from agents.platform.package_service import HostPackageService
from agents.roles.security_contract import build_security_output_contract
from agents.runtime.qa_catalog import SECURITY_ID_POLICY, refresh_qa_output
from orchestrator.domain import AgentRole, WorkflowStatus
from test_llm_runtime import text_response, tool_response
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_single_agent_pipeline as single_fixture


def _security_class(count):
    return 'import unittest\nclass Security(unittest.TestCase):\n' + ''.join(
        f'    def test_req_{i}(self): pass\n' for i in range(1, count + 1))


def real_security_tests(write, ready):
    """The fixture's Security write, but with a real (inert, never executed) unittest class."""
    def response(request):
        call = write(request).tool_calls[0]
        args = json.loads(call.arguments_json)
        args['content'] = _security_class(len(json.loads(ready(request).output_text)['testCases']))
        return tool_response(replace(call, arguments_json=json.dumps(args)))
    return response


def prefixed_ids(ready):
    """Run #30-style slip on Security: a ``tests.`` prefix the runner never reports."""
    def response(request):
        data = json.loads(ready(request).output_text)
        for row in data['testCases']:
            row['testId'] = 'tests.' + row['testId']
        return text_response(json.dumps(data, ensure_ascii=False))
    return response


class SecurityCatalogTests(unittest.TestCase):
    def test_security_test_ids_are_enum_bound_to_host_discovered_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'test_security.py').write_text(
                'import unittest\nraise RuntimeError("never execute")\n' + _security_class(2).split('\n', 1)[1])
            output = build_security_output_contract((uuid4(),), (), ('main.py',), ('security-unit',))
            narrowed, catalog = refresh_qa_output(output, root, [('security-unit', 'test_*.py')],
                snapshot_sha256='a' * 64, unit_only=True, field='testCases', policy=SECURITY_ID_POLICY)
        ids = narrowed.schema.to_dict()['properties']['testCases']['items']['properties']['testId']['enum']
        self.assertEqual(ids, ['test_security.Security.test_req_1', 'test_security.Security.test_req_2'])
        self.assertEqual(catalog['policy'], SECURITY_ID_POLICY)
        self.assertEqual(catalog['unresolvedSelectors'], [])
        # The original contract (used for validation) is untouched.
        self.assertNotIn('enum', output.schema.to_dict()['properties']['testCases']['items']['properties']['testId'])


class SecurityExhaustionPaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        return helper, f, packages

    def final_report(self, f, packages, run):
        self.assertEqual((run.status.value, getattr(run.verdict, 'value', None)), ('FINISHED', 'UNVERIFIED'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as c:
            return json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                        (str(run.run_id),)).fetchone()[0])

    def security_script(self, providers):
        write, ready = providers[AgentRole.SECURITY].script
        return real_security_tests(write, ready), ready

    async def test_security_wrong_ids_are_corrected_through_catalog_to_success(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, ready = self.security_script(providers)

        def checked(request):
            ids = request.output.schema.to_dict()['properties']['testCases']['items']['properties']['testId']['enum']
            result = ready(request)
            self.assertEqual(set(ids), {row['testId'] for row in json.loads(result.output_text)['testCases']})
            self.assertIn('hostTestIdCatalog', request.input_items_json)
            self.assertIn(SECURITY_ID_POLICY, request.input_items_json)
            return result
        providers[AgentRole.SECURITY].script = [write, prefixed_ids(ready), checked]
        run = await helper.submit(f, providers)
        diagnostic = {'steps': [(s.agent_role.value, s.status.value) for s in f.repository.list_steps(run.run_id)],
                      'lastEvents': [(e.actor, e.event_type) for e in helper.events(f, run)[-10:]]}
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), diagnostic)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        events = helper.events(f, run)
        self.assertFalse(any(e.event_type == 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED' for e in events))

    async def assert_security_exhaustion_keeps_qa_report(self, ver1):
        helper, f, packages = self.fixture(ver1)
        providers = f.providers()
        write, ready = self.security_script(providers)
        providers[AgentRole.SECURITY].script = [write, prefixed_ids(ready), prefixed_ids(ready)]
        run = await helper.submit(f, providers)
        report = self.final_report(f, packages, run)
        self.assertIn('QA_REPORT', report['artifacts'])
        self.assertNotIn('SECURITY_REPORT', report['artifacts'])
        self.assertEqual(report['validationStop']['reason'], 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED')
        self.assertFalse(report['validationStop']['securityReportProduced'])
        self.assertNotIn('qaReportProduced', report['validationStop'])
        self.assertNotIn('validationStops', report)
        self.assertEqual(report['unexecutedValidation'],
                         {'SECURITY_REPORT': {'status': 'UNVERIFIED', 'reason': 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'}})
        self.assertTrue(report['validationStop']['failureTraceEventIds'])
        events = helper.events(f, run)
        self.assertEqual(sum(e.event_type == 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED' for e in events), 1)
        self.assertEqual(sum(e.event_type == 'QA_REPORT_VALIDATED' for e in events), 1)
        self.assertFalse(any(e.workflow_state == WorkflowStatus.HUMAN_REVIEW for e in events))
        # No verdict was invented: the preserved QA report is the only validation report.
        reports = [a.artifact_type for a in f.repository.list_project_artifacts(run.run_id)
                   if a.artifact_type in ('QA_REPORT', 'SECURITY_REPORT')]
        self.assertEqual(reports, ['QA_REPORT'])

    async def test_multi_ver2_security_exhaustion_closes_with_qa_report_preserved(self):
        await self.assert_security_exhaustion_keeps_qa_report(False)

    async def test_multi_ver1_security_exhaustion_closes_with_qa_report_preserved(self):
        await self.assert_security_exhaustion_keeps_qa_report(True)

    async def test_security_bad_schema_exhaustion_also_closes(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, _ready = providers[AgentRole.SECURITY].script
        bad = text_response('{}')
        providers[AgentRole.SECURITY].script = [write, bad, bad]
        run = await helper.submit(f, providers)
        report = self.final_report(f, packages, run)
        self.assertEqual(report['unexecutedValidation']['SECURITY_REPORT']['reason'],
                         'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED')

    async def test_completed_qa_failure_survives_security_draft_exhaustion(self):
        from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        write, ready = self.security_script(providers)
        providers[AgentRole.SECURITY].script = [write, prefixed_ids(ready), prefixed_ids(ready)]
        # Observe the accepted fix input before intentionally ending at the next model decision.
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        self.assertEqual(run.fix_attempt, 1)
        issues = f.repository.list_issue_records(run.run_id)
        self.assertTrue(any(i.reporter is AgentRole.QA and i.evidence_refs for i in issues))
        self.assertIn('FIX_REQUESTED', [e.event_type for e in helper.events(f, run)])

    async def test_qa_and_security_both_exhausted_close_without_reports(self):
        helper, f, packages = self.fixture(True)
        providers = f.providers()
        bad = text_response('{}')
        providers[AgentRole.QA].script = [bad, bad]
        write, ready = self.security_script(providers)
        providers[AgentRole.SECURITY].script = [write, prefixed_ids(ready), prefixed_ids(ready)]
        run = await helper.submit(f, providers)
        report = self.final_report(f, packages, run)
        self.assertNotIn('QA_REPORT', report['artifacts'])
        self.assertNotIn('SECURITY_REPORT', report['artifacts'])
        self.assertEqual(set(report['unexecutedValidation']), {'QA_REPORT', 'SECURITY_REPORT'})
        self.assertEqual([s['reason'] for s in report['validationStops']],
                         ['QA_DRAFT_CORRECTIONS_EXHAUSTED', 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'])
        self.assertEqual(report['validationStop'], report['validationStops'][0])
        self.assertFalse(report['validationStops'][0]['qaReportProduced'])
        self.assertFalse(report['validationStops'][1]['securityReportProduced'])
        events = helper.events(f, run)
        for kind in ('QA_DRAFT_CORRECTIONS_EXHAUSTED', 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED'):
            self.assertEqual(sum(e.event_type == kind for e in events), 1)
        self.assertFalse(any(e.event_type in ('QA_REPORT_VALIDATED', 'SECURITY_REPORT_VALIDATED') for e in events))
        self.assertFalse(any(e.workflow_state == WorkflowStatus.HUMAN_REVIEW for e in events))

    async def test_qa_report_with_unresolved_result_keeps_human_review(self):
        """A preserved report must itself be complete; a non-technical UNVERIFIED keeps review."""
        from orchestrator.domain import ValidationOutcome
        from orchestrator.infrastructure import validation_stop
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, ready = self.security_script(providers)
        providers[AgentRole.SECURITY].script = [write, prefixed_ids(ready), prefixed_ids(ready)]
        original = validation_stop.finish_stopped

        def unresolved(repository, run_id, stopped, report=None):
            tests = list(report.tests)
            tests[0] = tests[0].model_copy(update={'outcome': ValidationOutcome.UNVERIFIED,
                                                   'actual_result': 'not established'})
            return original(repository, run_id, stopped, report.model_copy(update={'tests': tuple(tests)}))
        with mock.patch.object(validation_stop, 'finish_stopped', unresolved):
            run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'], 'NOT_COMPLETED')

    async def test_security_stop_without_host_proof_stays_review(self):
        from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
        helper, f, packages = self.fixture()
        providers = f.providers()
        providers[AgentRole.SECURITY].script = [LLMRuntimeError(LLMErrorCode.VALIDATION_DRAFT)]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertEqual(packages.complete(run.run_id)['status'], 'NOT_COMPLETED')

    async def test_single_security_exhaustion_keeps_qa_report_and_saves_package(self):
        for name in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(name, lambda *_: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion.adopt_cleanups(self, base)
        base.measured_security = True
        f = base.f
        packages = HostPackageService(f.repository)
        prepare = f.prepare_workspace

        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        f.prepare_workspace = enrolled
        provider = base.provider()
        write, ready = provider.script[-2:]
        provider.script[-2:] = [real_security_tests(write, ready), prefixed_ids(ready), prefixed_ids(ready)]
        async with base.running(cap=32, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        report = self.final_report(f, packages, run)
        self.assertIn('QA_REPORT', report['artifacts'])
        self.assertNotIn('SECURITY_REPORT', report['artifacts'])
        self.assertFalse(report['validationStop']['securityReportProduced'])


class RecordedPolicyTests(unittest.TestCase):
    def test_input_evidence_fingerprints_the_tools_and_schema_actually_sent(self):
        from agents.llm.input_evidence import ModelInputEvidence, output_schema_sha256, tools_sha256
        from agents.llm.tool_wire import pin_workspace_argument
        from agents.llm.contracts import JsonSchema, ToolDefinition
        tool = ToolDefinition(name='read_file', description='read', input_schema=JsonSchema.from_dict({
            'type': 'object', 'additionalProperties': False, 'required': ['workspaceId'],
            'properties': {'workspaceId': {'type': 'string'}}}),
            output_schema=JsonSchema.from_dict({'type': 'object'}))
        one, two = str(uuid4()), str(uuid4())
        self.assertNotEqual(tools_sha256((pin_workspace_argument(tool, one),)),
                            tools_sha256((pin_workspace_argument(tool, two),)))
        self.assertEqual(tools_sha256((pin_workspace_argument(tool, one),)),
                         tools_sha256((pin_workspace_argument(tool, one),)))
        output = build_security_output_contract((uuid4(),), (), ('main.py',), ('security-unit',))
        self.assertEqual(len(output_schema_sha256(output)), 64)
        # Older records without the new fields still validate.
        old = {'sequence': 1, 'metadataSha256': 'a' * 64, 'taskInputSha256': 'a' * 64,
               'initialInputSha256': 'a' * 64, 'systemSha256': 'a' * 64, 'inputItemsSha256': 'a' * 64}
        self.assertIsNone(ModelInputEvidence.model_validate(old).toolsSha256)

    def test_host_policy_names_every_new_rule_version(self):
        import inspect
        from agents.platform import host
        source = inspect.getsource(host)
        for key in ('securityTestIdPolicy', 'workspaceArgumentPolicy', 'providerUnknownUsagePolicy',
                    'toolErrorReturnPolicy', 'startPrecheckPolicy', 'instructionSha256', 'draftPrecheckPolicy',
                    'semanticRevisionPolicy', 'repeatedIssuePolicy', 'developmentStopPolicy',
                    'automaticStopPolicy'):
            self.assertIn(f'"{key}"', source)


if __name__ == '__main__':
    unittest.main()
