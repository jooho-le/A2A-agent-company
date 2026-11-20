"""세 종료 결과가 모두 결과 저장(패키지)까지 가는지 확인 (Codex 의견 2026-11-18).

- FINISHED SUCCESS: 필요한 검증을 통과하고 결과 저장
- FINISHED FAIL: 제품 결함이 남았지만 최종 판정·결과 저장까지 완료(수정 3회 소진)
- FINISHED UNVERIFIED: 교정 소진(tests/test_qa_draft_recovery.py, tests/test_security_draft_exhaustion.py)

같은 결함 2회 반복도 05 문서 14절(석민 결정)에 따라 FINISHED FAIL + 패키지로 끝난다(이전: 사람 검토).
"""
import json
import unittest

from agents.platform.package_service import HostPackageService
from orchestrator.domain import FinalVerdict, WorkflowStatus
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_owned_agent_fix_pipeline as fix_fixture  # module import: its TestCase is not re-collected here


def _final_report(repository, run_id):
    with repository._connection() as c:
        row = c.execute('SELECT report FROM host_final_reports WHERE run_id=?', (str(run_id),)).fetchone()
    return None if row is None else json.loads(row[0])


class SuccessPackage(unittest.IsolatedAsyncioTestCase):
    async def assert_success_package(self, ver1):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        run = await helper.submit(f, f.providers())
        self.assertEqual((run.status, run.verdict), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = _final_report(f.repository, run.run_id)
        self.assertEqual(report['verdict'], 'SUCCESS')
        self.assertTrue({'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'} <= set(report['artifacts']))
        self.assertNotIn('unexecutedValidation', report)
        self.assertNotIn('validationStop', report)

    async def test_multi_ver1_success_saves_package(self):
        await self.assert_success_package(True)

    async def test_multi_ver2_success_saves_package(self):
        await self.assert_success_package(False)


class FixLimitPackages(fix_fixture.OwnedAgentFixPipelineTests):
    """Reuse the fix-loop harness; only add package enrollment and the package check."""

    def harness(self, qa_mode):
        harness = fix_fixture._FixHarness(self, qa_mode=qa_mode)
        packages = HostPackageService(harness.base.repository)
        prepare = harness.base.prepare_workspace

        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        harness.base.prepare_workspace = enrolled
        return harness, packages

    async def test_product_failure_after_three_fixes_finishes_fail_and_saves_package(self):
        harness, packages = self.harness('distinct-failures')
        run, artifacts, _steps, _calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.FAIL, 3, 4))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = _final_report(harness.base.repository, run.run_id)
        self.assertEqual((report['verdict'], report['codeVersion']), ('FAIL', 4))
        # The last candidate's reports are packaged; nothing is marked as not executed.
        self.assertEqual(report['artifacts']['QA_REPORT']['codeVersion'], 4)
        self.assertNotIn('unexecutedValidation', report)
        self.assertTrue(report['issues'])

    async def test_changed_generated_input_keeps_original_failure_unverified_in_archive(self):
        harness, packages = self.harness('initial-failure')
        original_providers = harness.providers
        def providers():
            from dataclasses import replace
            from orchestrator.domain import AgentRole
            from test_llm_runtime import tool_response
            values = original_providers()
            script = values[AgentRole.QA].script
            original_write = script[2]
            def revised_test(request):
                call, = original_write(request).tool_calls
                args = json.loads(call.arguments_json)
                args['content'] += '# revised fixture\n'
                return tool_response(replace(call, arguments_json=json.dumps(args)))
            script[2] = revised_test
            return values
        harness.providers = providers
        run, _, _, _ = await self.submit(harness)
        self.assertEqual((run.status, run.verdict), (WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        final = _final_report(harness.base.repository, run.run_id)
        issue, = final['issues']
        self.assertEqual(issue['revalidationResult'], 'UNVERIFIED')
        self.assertEqual([entry['issueId'] for entry in final['unresolvedGeneratedRechecks']], [issue['issueId']])

    async def test_regenerated_failure_exhausts_fix_limit_with_its_measured_package(self):
        harness, packages = self.harness('repeated-failure')
        run, _artifacts, _steps, _calls = await self.submit(harness)
        self.assertEqual((run.status, run.verdict, run.fix_attempt, run.code_version),
                         (WorkflowStatus.FINISHED, FinalVerdict.FAIL, 3, 4))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = _final_report(harness.base.repository, run.run_id)
        self.assertEqual((report['verdict'], report['codeVersion']), ('FAIL', 4))
        self.assertEqual(report['repairOutcome']['codeFixesUsed'], 3)
        self.assertEqual(report['artifacts']['QA_REPORT']['codeVersion'], 4)
        self.assertTrue(report['issues'])
        self.assertNotIn('unexecutedValidation', report)


# Only the methods defined here; the inherited fix-loop tests already run in their own module.
for _name in [n for n in dir(fix_fixture.OwnedAgentFixPipelineTests) if n.startswith('test_')]:
    if _name not in FixLimitPackages.__dict__:
        setattr(FixLimitPackages, _name, None)


if __name__ == '__main__':
    unittest.main()


class DeclaredUnexecutedTests(unittest.TestCase):
    """평가 packageComplete: Host가 선언하고 종료 사건이 있는 미생성 보고서만 누락에서 뺀다."""

    def export(self, declared, events):
        return {'packageFinalReport': {'unexecutedValidation': declared}}, {'events': events}

    def test_declared_draft_stop_with_host_event_is_not_missing(self):
        from evaluation.outcomes import _declared_unexecuted
        export, bundle = self.export({'QA_REPORT': {'status': 'UNVERIFIED', 'reason': 'QA_DRAFT_CORRECTIONS_EXHAUSTED'}},
                                     [{'eventType': 'QA_DRAFT_CORRECTIONS_EXHAUSTED', 'codeVersion': 2}])
        self.assertTrue(_declared_unexecuted(export, bundle, 'QA_REPORT', 2))
        self.assertFalse(_declared_unexecuted(export, bundle, 'SECURITY_REPORT', 2))
        self.assertFalse(_declared_unexecuted(export, bundle, 'QA_REPORT', 1))      # other version
        self.assertFalse(_declared_unexecuted(export, bundle, 'SOURCE', 2))         # never optional

    def test_declaration_without_host_event_or_with_foreign_reason_is_missing(self):
        from evaluation.outcomes import _declared_unexecuted
        export, bundle = self.export({'QA_REPORT': {'status': 'UNVERIFIED', 'reason': 'QA_DRAFT_CORRECTIONS_EXHAUSTED'}}, [])
        self.assertFalse(_declared_unexecuted(export, bundle, 'QA_REPORT', 2))
        export, bundle = self.export({'QA_REPORT': {'status': 'UNVERIFIED', 'reason': 'MODEL_SAID_SO'}},
                                     [{'eventType': 'MODEL_SAID_SO', 'codeVersion': 2}])
        self.assertFalse(_declared_unexecuted(export, bundle, 'QA_REPORT', 2))

    def test_build_failure_close_declares_not_run_reports(self):
        from evaluation.outcomes import _declared_unexecuted
        export, bundle = self.export({'SECURITY_REPORT': {'status': 'NOT_RUN', 'reason': 'BUILD_FAILED'}},
                                     [{'eventType': 'DEVELOPMENT_CORRECTIONS_EXHAUSTED', 'codeVersion': 1}])
        self.assertTrue(_declared_unexecuted(export, bundle, 'SECURITY_REPORT', 1))
