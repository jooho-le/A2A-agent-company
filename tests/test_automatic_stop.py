"""docs/live-api/05 22절 일반 종료 규칙(2026-11-18 석민 결정, 주호 확인 필요) 오프라인 확인.

모델 행동으로 멈춘 Run은 사람 검토 대신 마지막으로 Host가 측정한 후보로 FINISHED(FAIL/UNVERIFIED) + 결과 패키지.
SUCCESS는 만들지 않는다. 제공자·인증 장애처럼 모델 행동이 아닌 멈춤은 지금처럼 사람 검토다.
실제 A2A/Host/Git/SQLite + 가짜 모델·Docker(생성 코드를 실행하지 않음, 유료 호출 없음).
"""
import inspect
import json
import re
import unittest
from pathlib import Path
from unittest import mock

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from orchestrator.domain import AgentRole, CodeSnapshotArtifact, FinalVerdict, WorkflowStatus
from orchestrator.infrastructure import automatic_stop
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_owned_agent_pipeline as multi_fixture
import test_single_agent_pipeline as single_fixture
import test_semantic_revision_and_dev_stop as revision  # module import: pytest must not re-collect its classes

SRC = Path(__file__).resolve().parents[1] / 'src'


class AutomaticStopPaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False, *, enabled=True):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        if enabled:
            # The production Host turns the rule on (host.py); this low-level fixture builds the platform directly.
            original = multi_fixture.create_platform

            def with_automatic_stop(**values):
                return original(**{**values, 'automatic_stop': True})
            patcher = mock.patch.object(multi_fixture, 'create_platform', with_automatic_stop)
            patcher.start()
            self.addCleanup(patcher.stop)
        return helper, f, packages

    def final_report(self, f, run):
        with f.repository._connection() as connection:
            return json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                                 (str(run.run_id),)).fetchone()[0])

    def assert_finished_by_rule(self, helper, f, packages, run, *, verdict, stage, code):
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, verdict), run)
        events = helper.events(f, run)
        stops = [e for e in events if e.event_type == automatic_stop.EVENT]
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].code_version, run.code_version)
        self.assertFalse(any(e.workflow_state == WorkflowStatus.HUMAN_REVIEW for e in events))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = self.final_report(f, run)
        self.assertEqual(report['verdict'], verdict)
        self.assertEqual((report['automaticStop']['stage'], report['automaticStop']['reasonCode']), (stage, code))
        self.assertEqual(report['automaticStop']['policy'], automatic_stop.POLICY)
        self.assertFalse(report['automaticStop']['newCandidatePublished'])
        return report

    async def test_fix_round_model_error_finishes_fail_with_last_candidate_and_package(self):
        """수정 라운드의 Developer가 형식 오류로 멈춤(Host 회복 기록 없음): 마지막 측정 후보 v1의 FAIL로 끝난다."""
        helper, f, packages = self.fixture(True)
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        self.assert_finished_by_rule(helper, f, packages, run, verdict='FAIL', stage='DEVELOPER',
                                     code='LLM_TOOL_POLICY_VIOLATION')
        self.assertEqual((run.code_version, run.fix_attempt), (1, 1))
        sources = [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, CodeSnapshotArtifact)]
        self.assertEqual([a.code_version for a in sources], [1])  # 멈춘 v2 작업은 Snapshot으로 꾸미지 않는다
        # 같은 멈춤을 다시 관찰해도 한 번만 닫는다. 다른 Task ID는 거부한다.
        step = f.repository.list_steps(run.run_id)[-1]
        self.assertTrue(automatic_stop.finish_agent_stop(f.repository, run.run_id, step.workflow_step_id,
                                                         step.a2a_task_id, 'LLM_TOOL_POLICY_VIOLATION'))
        self.assertFalse(automatic_stop.finish_agent_stop(f.repository, run.run_id, step.workflow_step_id,
                                                          'other-task', 'LLM_TOOL_POLICY_VIOLATION'))

    async def test_budget_exhaustion_in_fix_round_finishes_with_measured_fail(self):
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.BUDGET))
        run = await helper.submit(f, providers)
        self.assert_finished_by_rule(helper, f, packages, run, verdict='FAIL', stage='DEVELOPER',
                                     code='LLM_BUDGET_EXHAUSTED')

    async def test_provider_failure_is_not_a_model_stop_and_keeps_review(self):
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.AUTH))
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertFalse([e for e in helper.events(f, run) if e.event_type == automatic_stop.EVENT])
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_refusals_and_unproven_rejections_stay_human_review(self):
        """Codex review 2026-11-18 #4: a model refusal (REJECTED state) and an explained rejection without
        Host proof (may be a permission/policy refusal) are never closed by this rule."""
        import test_development_rejection as rejection_fixture  # module import: no re-collection
        for case in ('MODEL_REFUSAL', 'POLICY_BLOCKED', 'UNKNOWN'):
            with self.subTest(case=case):
                helper, f, packages = self.fixture()
                f.qa_failure = True
                providers = f.providers()
                providers[AgentRole.DEVELOPER].script.append(
                    LLMRuntimeError(LLMErrorCode.REFUSAL) if case == 'MODEL_REFUSAL'
                    else rejection_fixture.rejected(case, f.scenario.requirement_ids))
                run = await helper.submit(f, providers)
                self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
                self.assertFalse([e for e in helper.events(f, run) if e.event_type == automatic_stop.EVENT])

    async def test_rule_is_off_unless_the_host_enables_it(self):
        helper, f, packages = self.fixture(enabled=False)
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertFalse([e for e in helper.events(f, run) if e.event_type == automatic_stop.EVENT])

    async def test_validator_model_stop_keeps_completed_sibling_and_declares_only_missing_report(self):
        """A stopped Security validator cannot discard a completed QA report."""
        helper, f, packages = self.fixture()
        providers = f.providers()
        providers[AgentRole.SECURITY].script = [LLMRuntimeError(LLMErrorCode.TOOL_POLICY)]
        run = await helper.submit(f, providers)
        report = self.assert_finished_by_rule(helper, f, packages, run, verdict='UNVERIFIED', stage='SECURITY',
                                              code='LLM_TOOL_POLICY_VIOLATION')
        self.assertEqual(report['unexecutedValidation'], {
            'SECURITY_REPORT': {'status': 'UNVERIFIED', 'reason': 'SECURITY_MODEL_VALIDATION_STOP'}})
        self.assertNotIn('SECURITY_REPORT', report['artifacts'])
        self.assertIn('QA_REPORT', report['artifacts'])

    async def test_completed_qa_failure_enters_fix_before_model_stop_gets_packaged(self):
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.SECURITY].script = [LLMRuntimeError(LLMErrorCode.TOOL_POLICY)]
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        report = self.assert_finished_by_rule(helper, f, packages, run, verdict='FAIL', stage='DEVELOPER',
                                              code='LLM_TOOL_POLICY_VIOLATION')
        self.assertEqual(run.fix_attempt, 1)
        self.assertTrue(any(row['outcome'] == 'FAIL' for row in report['artifacts']['QA_REPORT']['tests']))
        self.assertEqual(set(report['unexecutedValidation']), {'SECURITY_REPORT'})
        self.assertTrue(report['issues'])

    async def test_qa_model_stop_keeps_completed_security_report(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        providers[AgentRole.QA].script = [LLMRuntimeError(LLMErrorCode.TOOL_POLICY)]
        run = await helper.submit(f, providers)
        report = self.assert_finished_by_rule(helper, f, packages, run, verdict='UNVERIFIED', stage='QA',
                                              code='LLM_TOOL_POLICY_VIOLATION')
        self.assertIn('SECURITY_REPORT', report['artifacts'])
        self.assertEqual(report['validationStop']['stopCode'], 'LLM_TOOL_POLICY_VIOLATION')
        self.assertEqual(set(report['unexecutedValidation']), {'QA_REPORT'})

    async def test_repair_after_missing_security_report_revalidates_both_roles_and_completes(self):
        from dataclasses import replace
        from test_llm_runtime import tool_response
        helper, f, packages = self.fixture()
        providers = f.providers()
        f.qa_failure = True
        initial_write, ready = providers[AgentRole.DEVELOPER].script
        def repair(request):
            f.qa_failure = False
            call, = initial_write(request).tool_calls
            args = json.loads(call.arguments_json)
            args['content'] = "def signup():\n    return 'repaired-inert-candidate'\n"
            return tool_response(replace(call, call_id='distinct-source-repair', arguments_json=json.dumps(args)))
        providers[AgentRole.DEVELOPER].script.extend((repair, ready))
        qa_write, qa_ready = providers[AgentRole.QA].script
        providers[AgentRole.QA].script.extend((qa_write, qa_ready))
        providers[AgentRole.SECURITY].script.insert(0, LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict, run.code_version), (WorkflowStatus.FINISHED, FinalVerdict.SUCCESS, 2))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = self.final_report(f, run)
        self.assertEqual(report['artifacts']['QA_REPORT']['artifactVersion'], 2)
        self.assertEqual(report['artifacts']['SECURITY_REPORT']['artifactVersion'], 1)
        self.assertNotIn('unexecutedValidation', report)

    async def test_two_stopped_validators_pack_each_actual_stop_code(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        for role in (AgentRole.QA, AgentRole.SECURITY):
            providers[role].script = [LLMRuntimeError(LLMErrorCode.TOOL_POLICY)]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value), (WorkflowStatus.FINISHED, 'UNVERIFIED'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = self.final_report(f, run)
        self.assertEqual(set(report['unexecutedValidation']), {'QA_REPORT', 'SECURITY_REPORT'})
        self.assertEqual({(entry['reason'], entry['stopCode']) for entry in report['validationStops']}, {
            ('QA_MODEL_VALIDATION_STOP', 'LLM_TOOL_POLICY_VIOLATION'),
            ('SECURITY_MODEL_VALIDATION_STOP', 'LLM_TOOL_POLICY_VIOLATION')})

    def test_only_the_specific_single_context_limit_is_allowed(self):
        self.assertIn('SINGLE_AGENT_CONTEXT_LIMIT', automatic_stop.MODEL_STOP_CODES)
        self.assertNotIn('SINGLE_AGENT_EXECUTION_FAILED', automatic_stop.MODEL_STOP_CODES)



    def hold_first_verdict(self, reason):
        """The first real verdict is replaced by an evidence-shortage hold (routing check only)."""
        from orchestrator.application import dispatch as dispatch_module
        from orchestrator.application.validation_output import VerdictDecision
        original, calls = dispatch_module.decide_verdict, []

        def held(*args, **kwargs):
            calls.append(original(*args, **kwargs))
            if len(calls) == 1:
                return VerdictDecision(WorkflowStatus.HUMAN_REVIEW, FinalVerdict.HUMAN_REVIEW, reason)
            return calls[-1]
        patcher = mock.patch.object(dispatch_module, 'decide_verdict', held)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls

    async def test_verdict_hold_with_measured_failure_goes_to_fix_then_closes(self):
        """판정 보류 + 측정 FAIL + 수정 기회 남음 → 사람 검토 대신 수정 단계. 수정이 멈추면 측정 FAIL로 끝난다."""
        helper, f, packages = self.fixture()
        f.qa_failure = True
        self.hold_first_verdict('Required results lack MCP Tool execution provenance')
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        run = await helper.submit(f, providers)
        events = helper.events(f, run)
        routed = [e for e in events if e.event_type == automatic_stop.ROUTED_TO_FIX]
        self.assertEqual(len(routed), 1)
        self.assertEqual(routed[0].code_version, 1)
        self.assertEqual(run.fix_attempt, 1)  # 수정 단계가 실제로 시작됐다
        self.assert_finished_by_rule(helper, f, packages, run, verdict='FAIL', stage='DEVELOPER',
                                     code='LLM_TOOL_POLICY_VIOLATION')

    async def test_verdict_hold_without_measured_failure_finishes_unverified(self):
        helper, f, packages = self.fixture()
        self.hold_first_verdict('Required PASS results lack successful MCP Tool execution provenance')
        run = await helper.submit(f, f.providers())
        report = self.assert_finished_by_rule(helper, f, packages, run, verdict='UNVERIFIED', stage='VERDICT',
                                              code='PASS_PROVENANCE_MISSING')
        self.assertIn('QA_REPORT', report['artifacts'])  # 판정 단계 보류는 등록된 두 보고서를 그대로 담는다
        self.assertIn('SECURITY_REPORT', report['artifacts'])
        self.assertNotIn('unexecutedValidation', report)

    async def test_single_uses_the_same_rule(self):
        """Single도 같은 Dispatcher 규칙: 수정 단계에서 모델 행동으로 멈추면 마지막 측정 후보의 FAIL과 패키지."""
        await self.assert_single_measured_fix_stop('LLM_TOOL_POLICY_VIOLATION')

    async def test_oversized_single_fix_context_packages_the_last_measured_failure(self):
        from agents.single.prompts import MAX_SINGLE_PROMPT_BYTES, prepare_single_prompt

        async def oversized_fix(*args, **kwargs):
            if kwargs['draft_contract'].phase.value == 'FIXING':
                kwargs['task_input'] = {**kwargs['task_input'], 'request': 'x' * MAX_SINGLE_PROMPT_BYTES}
            return await prepare_single_prompt(*args, **kwargs)

        with mock.patch('agents.single.runtime.prepare_single_prompt', oversized_fix):
            await self.assert_single_measured_fix_stop('SINGLE_AGENT_CONTEXT_LIMIT')

    async def assert_single_measured_fix_stop(self, expected_code):
        for name in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(name, lambda *_: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        original = single_fixture.create_single_platform

        def with_automatic_stop(**values):
            return original(**{**values, 'automatic_stop': True})
        patcher = mock.patch.object(single_fixture, 'create_single_platform', with_automatic_stop)
        patcher.start()
        self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion.adopt_cleanups(self, base)
        base.measured_security = True
        f = base.f
        f.qa_failure = True
        from agents.platform.package_service import HostPackageService
        packages = HostPackageService(f.repository)
        prepare = f.prepare_workspace

        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        f.prepare_workspace = enrolled
        provider = base.provider()
        provider.script.append(LLMRuntimeError(LLMErrorCode.TOOL_POLICY))
        async with base.running(cap=32, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            from uuid import UUID
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status, run.verdict.value if run.verdict else None), (WorkflowStatus.FINISHED, 'FAIL'), run)
        events, _ = f.repository.list_events(run.run_id, limit=1000, offset=0)
        self.assertEqual(sum(e.event_type == automatic_stop.EVENT for e in events), 1)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        report = self.final_report(f, run)
        self.assertEqual((report['automaticStop']['stage'], report['automaticStop']['reasonCode']), ('DEVELOPER', expected_code))
        self.assertTrue(any(test['outcome'] == 'FAIL' for test in report['artifacts']['QA_REPORT']['tests']))


class AutomaticStopSemanticHold(revision.SemanticRevisionPaths):
    """실제 Run #27 유형: 의미 검토가 다시 보류되면 지금은 사람 검토였다. 규칙을 켜면 측정 결과로 끝난다."""

    def fixture(self, ver1=False):
        helper, f, packages = super().fixture(ver1)
        original = multi_fixture.create_platform

        def with_automatic_stop(**values):
            return original(**{**values, 'automatic_stop': True})
        patcher = mock.patch.object(multi_fixture, 'create_platform', with_automatic_stop)
        patcher.start()
        self.addCleanup(patcher.stop)
        return helper, f, packages

    async def test_repeated_meaning_hold_finishes_unverified_with_package(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        from test_security_plan_paths import anchored_ready
        from semantic_helpers import patch_reply
        provider = self.security(providers, lambda ready: [patch_reply()])
        provider.review_decider = revision.held_first_then(99)
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'UNVERIFIED'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        stops = [e for e in helper.events(f, run) if e.event_type == automatic_stop.EVENT]
        self.assertEqual(len(stops), 1)

    # The inherited V2 rewrite tests assert today's (rule-off) behavior; they run in their own module.
    test_security_meaning_hold_is_rewritten_once_and_run_succeeds = None
    test_security_rewrite_cannot_write_test_files = None
    test_security_held_again_after_revision_still_stops = None
    test_qa_meaning_hold_is_rewritten_once_and_run_succeeds = None


class AutomaticStopRules(unittest.TestCase):
    def test_verdict_hold_reasons_still_exist_in_the_verdict_code(self):
        """원문이 바뀌면 규칙이 조용히 꺼지므로(보류 유지), 대상 문장이 실제 판정 코드에 있는지 확인한다."""
        text = (SRC / 'orchestrator/application/validation_output.py').read_text() + \
            (SRC / 'orchestrator/application/dispatch.py').read_text()
        for reason in automatic_stop.VERDICT_HOLD_REASONS:
            self.assertIn(json.dumps(reason), text.replace("'", '"'))

    def test_integrity_and_configuration_holds_are_not_converted(self):
        for reason in ('QA and Security reports refer to different execution manifests',
                       'Scenario Registry is unavailable; Planner requirements are not a protected baseline',
                       'Build PASS lacks successful MCP Tool provenance'):
            self.assertIsNone(automatic_stop.verdict_fallback(reason, measured_failure=True, fix_attempt=3))

    def test_converted_hold_never_becomes_success(self):
        for reason in automatic_stop.VERDICT_HOLD_REASONS:
            for fix_attempt in (0, 3):
                status, verdict, _code = automatic_stop.verdict_fallback(reason, measured_failure=False,
                                                                         fix_attempt=fix_attempt)
                self.assertEqual((status, verdict), (WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED))
            status, verdict, _code = automatic_stop.verdict_fallback(reason, measured_failure=True, fix_attempt=3)
            self.assertEqual((status, verdict), (WorkflowStatus.FINISHED, FinalVerdict.FAIL))

    def test_measured_failure_with_fix_attempts_left_goes_to_the_fix_cycle(self):
        """보류가 있어도 측정된 FAIL이 있고 수정 기회가 남았으면 끝내지 않고 알려진 FAIL을 수정으로 보낸다."""
        for reason in automatic_stop.VERDICT_HOLD_REASONS:
            for fix_attempt in (0, 1, 2):
                status, verdict, _code = automatic_stop.verdict_fallback(reason, measured_failure=True,
                                                                         fix_attempt=fix_attempt)
                self.assertEqual((status, verdict), (WorkflowStatus.FIX_REQUIRED, None))

    def test_security_finding_judgment_is_unverified_not_success(self):
        status, verdict, code = automatic_stop.verdict_fallback(
            'Security findings require a policy decision', measured_failure=False, fix_attempt=0)
        self.assertEqual((status, verdict, code),
                         (WorkflowStatus.FINISHED, FinalVerdict.UNVERIFIED, 'SECURITY_FINDING_JUDGMENT_PENDING'))

    def test_semantic_review_failures_close_only_for_model_categories(self):
        self.assertEqual(automatic_stop._SEMANTIC_MODEL_CATEGORIES, frozenset({'FORMAT', 'EVIDENCE'}))

    def test_infrastructure_and_permission_codes_are_outside_the_closed_set(self):
        for code in ('LLM_REFUSED', 'DEVELOPER_REJECTED', 'LLM_DEVELOPMENT_REJECTION_EXHAUSTED','LLM_PROVIDER_ERROR', 'LLM_AUTH_REQUIRED', 'LLM_CONFIGURATION_ERROR', 'LLM_EXECUTION_TIMEOUT',
                     'LLM_TOOL_EXECUTION_FAILED', 'DEVELOPER_EXECUTION_FAILED', 'QA_CONTEXT_DENIED',
                     'SECURITY_SERVICES_DENIED', 'SECURITY_PROOF_INVALID', 'QA_TEST_EVIDENCE_INVALID',
                     'SECURITY_SCAN_UNVERIFIED', 'DEVELOPER_INPUT_INVALID'):
            self.assertNotIn(code, automatic_stop.MODEL_STOP_CODES)

    def test_only_the_production_host_enables_the_rule_and_records_its_version(self):
        from agents.platform import composition, host
        from agents.single import platform as single_platform
        self.assertIn('automatic_stop=True', inspect.getsource(host))
        self.assertIn('"automaticStopPolicy": AUTOMATIC_STOP_POLICY', inspect.getsource(host))
        for factory in (composition.create_platform, single_platform.create_single_platform):
            self.assertIs(inspect.signature(factory).parameters['automatic_stop'].default, False)
        self.assertRegex(automatic_stop.POLICY, re.compile(r'_V\d+$'))


if __name__ == '__main__':
    unittest.main()
