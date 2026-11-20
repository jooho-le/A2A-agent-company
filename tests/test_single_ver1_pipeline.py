"""Single Ver1 offline completion (2026-11-19 final pipeline check, no paid call).

Single Ver1 (one agent, in-loop protected tests) had neither a real Run nor an offline end-to-end test.
The other three conditions did (Multi Ver1, Multi Ver2, Single with generated tests only). These tests use the
real Host/A2A/Git/SQLite/receipts with synthetic model and Docker adapters, like tests/test_completion_paths.py:

* the Host-approved protected QA and Security scopes run inside Single's QA/Security phases, and the Run
  finishes SUCCESS with its result package;
* the Ver1 rule is the same as Multi Ver1: a generated QA scope that cannot run in either round while the
  protected tests pass ends FINISHED SUCCESS, the not-run cases are left out of fix requests as a recorded
  conflict (VALIDATION_CONFLICT_POLICY_EXCLUDED), and nothing is sent to the fix phase.

This verifies routing and accounting, not model accuracy or a real Docker run.
"""
import unittest
from unittest import mock
from uuid import UUID

from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.observability import ObservationStore
import test_completion_paths as completion
from test_recovery_pipeline import enable_protected_cases
import test_single_agent_pipeline as single_fixture

PROTECTED_SELECTORS = frozenset({"run_unit_tests:protected-qa", "run_unit_tests:protected-security"})


class SingleVer1PipelineTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        # The inert fixture seed has no main.py; the static start precheck would rightly reject it.
        for target in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(target, lambda *args, **kwargs: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion.adopt_cleanups(self, base)
        f = base.f
        packages = completion.enroll_packages(f)
        prepare = f.prepare_workspace

        def observed_prepare(run_id):
            ObservationStore(f.repository).pin_policy(run_id)
            prepare(run_id)
        f.prepare_workspace = observed_prepare
        f.security_tests = True
        # measured_security would rebuild the Security service without its protected scope; Ver1 keeps the
        # protected-case services built by enable_protected_cases, exactly as the Host does for both architectures.
        base.measured_security = False
        enable_protected_cases(f)
        original = single_fixture.create_single_platform

        def with_selectors(**values):
            # The production Host passes host._protected_selectors(tools) to the Single factory too.
            return original(**values, protected_selectors=PROTECTED_SELECTORS)
        patcher = mock.patch.object(single_fixture, 'create_single_platform', with_selectors)
        patcher.start()
        self.addCleanup(patcher.stop)
        return base, f, packages

    async def submit(self, base, provider):
        async with base.running(cap=40, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=base.f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            return base.f.repository.get_run(UUID(response.json()['run']['runId']))

    def qa_report(self, f, run):
        from orchestrator.domain import QAReportArtifact
        return [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, QAReportArtifact)][-1]

    def protected_runs(self, f, role):
        return [args.get('testScope') for session_role, session in f.sessions if session_role is role
                for name, args, _ in session.calls if name == 'run_unit_tests']

    async def test_single_ver1_runs_protected_scopes_and_saves_package(self):
        base, f, packages = self.fixture()
        run = await self.submit(base, base.provider())
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
        self.assertIn('protected-qa', self.protected_runs(f, AgentRole.QA))
        self.assertIn('protected-security', self.protected_runs(f, AgentRole.SECURITY))
        report = self.qa_report(f, run)
        self.assertTrue([case for case in report.tests if ':protected-qa:' in case.test_id], report.tests)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_single_ver1_generated_failure_with_passing_protected_tests_finishes_success(self):
        base, f, packages = self.fixture()
        helper = completion.CompletionPathTests('runTest')
        rounds = helper.fail_generated_qa_runs(f, failing_rounds={1, 2})
        roles = f.providers()
        qa = roles[AgentRole.QA].script
        qa.extend([completion.renamed(qa[0], 'qa-write-fix'), qa[1]])
        provider = single_fixture.FakeProvider(*(action for role in AgentRole for action in roles[role].script))
        run = await self.submit(base, provider)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
        self.assertEqual(len(rounds), 2)  # first run + one in-step test fix
        report = self.qa_report(f, run)
        generated = [case for case in report.tests if ':qa-unit:' in case.test_id]
        self.assertTrue(generated)
        self.assertTrue(all(case.actual_result == 'NOT_RUN_SCOPE_FAILED' for case in generated))
        events, _ = f.repository.list_events(run.run_id, limit=1000, offset=0)
        conflict = [event for event in events if event.event_type == 'VALIDATION_CONFLICT_POLICY_EXCLUDED']
        self.assertEqual(len(conflict), 1)
        self.assertFalse(f.repository.list_issue_records(run.run_id))  # nothing sent to the fix phase
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
