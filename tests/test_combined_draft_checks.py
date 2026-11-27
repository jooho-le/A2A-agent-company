"""Multiple pre-READY faults must not hide missing Security read evidence."""
from types import SimpleNamespace
import json
from dataclasses import replace
import unittest
from unittest import mock
from uuid import UUID

from agents.llm.corrections import case_expectation_check, checked_rejection, feedback


def issue(reason, path='/testCases/0/testId'):
    return {'fieldPath': path, 'reasonCode': reason}


READ = issue('REFERENCE_FILE_NOT_READ', '/requirementReviews/0/references/0/anchorId')
LIFESPAN = issue('TEST_CLIENT_NOT_STARTED')
MISSING = issue('GENERATED_TEST_FILE_MISSING')


class CombinedDraftCheckTests(unittest.TestCase):
    def check(self, generated, references, expectations=None):
        return case_expectation_check(lambda data: SimpleNamespace(kind=data['kind']), expectations,
            extra_checks=(('GENERATED_TESTS_INVALID', generated), ('CODE_REFERENCE_INVALID', references)))

    def test_unread_reference_is_not_hidden_by_a_lifespan_error(self):
        result = self.check(lambda _: [LIFESPAN], lambda _: [READ])({'kind': 'READY'})
        self.assertEqual(result['code'], 'CODE_REFERENCE_INVALID')
        self.assertEqual(result['issues'], [READ, LIFESPAN])
        instructions = feedback(**{'kind': result['code'], 'issues': result['issues'], 'remaining': 1})
        text = instructions['hostCorrection']['instruction']
        self.assertIn('read_project_file', text)
        self.assertIn('TEST_CLIENT_NOT_STARTED', text)

    def test_missing_file_keeps_forced_write_and_also_reports_unread_reference(self):
        result = self.check(lambda _: [MISSING], lambda _: [READ])({'kind': 'READY'})
        self.assertEqual(result['code'], 'GENERATED_TESTS_INVALID')
        self.assertEqual(result['issues'], [MISSING, READ])
        self.assertIn('read_project_file', feedback(result['code'], issues=result['issues'],
                                                  remaining=1)['hostCorrection']['instruction'])

    def test_fixed_reference_leaves_independent_test_issue_visible(self):
        result = self.check(lambda _: [LIFESPAN], lambda _: [])({'kind': 'READY'})
        self.assertEqual(result, {'code': 'GENERATED_TESTS_INVALID', 'issues': [LIFESPAN]})

    def test_expectation_error_does_not_hide_read_evidence_failure(self):
        expectation = issue('CASE_EXPECTATION_NOT_APPROVED', '/testCases/0/expectedResult')
        result = self.check(lambda _: [], lambda _: [READ], lambda _: [expectation])({'kind': 'READY'})
        self.assertEqual(result['code'], 'CODE_REFERENCE_INVALID')
        self.assertEqual(result['issues'], [READ, expectation])

    def test_failing_host_checker_is_not_silently_treated_as_valid(self):
        def broken(_):
            raise RuntimeError('trusted checker failed')
        for generated, references, expectations in ((broken, lambda _: [], None),
                                                    (lambda _: [], broken, None),
                                                    (lambda _: [], lambda _: [], broken)):
            with self.assertRaises(RuntimeError):
                self.check(generated, references, expectations)({'kind': 'READY'})

    def test_primary_read_issue_survives_public_issue_limit(self):
        generated = [issue('TEST_CLIENT_NOT_STARTED', f'/testCases/{i}/testId') for i in range(30)]
        result = checked_rejection(self.check(lambda _: generated, lambda _: [READ])({'kind': 'READY'}))
        self.assertEqual(result['code'], 'CODE_REFERENCE_INVALID')
        self.assertEqual(result['issues'][0], READ)
        self.assertLessEqual(len(result['issues']), 16)

    def test_non_ready_does_not_run_draft_inspectors(self):
        def unexpected(_):
            raise AssertionError('must not inspect a question or rejection as a READY draft')
        check = self.check(unexpected, unexpected)
        self.assertIsNone(check({'kind': 'REJECTED'}))
        self.assertEqual(check({'kind': 'INPUT_REQUIRED'})['code'], 'QUESTIONS_NOT_ANSWERED')


class CombinedSecurityPipelineTests(unittest.IsolatedAsyncioTestCase):
    """Actual role/A2A/SQLite/package path, synthetic provider and Docker only."""

    def fixture(self):
        import test_development_stop_paths as fixture
        from test_completion_paths import adopt_cleanups
        owner = fixture.DevelopmentStopPaths('runTest')
        try:
            result = owner.fixture(False)
        finally:
            adopt_cleanups(self, owner)
        return result

    def actions(self, write, ready):
        from test_security_draft_exhaustion import real_security_tests
        from test_security_plan_paths import anchored_ready
        from test_llm_runtime import tool_response
        good = real_security_tests(write, ready)

        def bad(request):
            call = good(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            args['content'] = args['content'].replace('class Security(unittest.TestCase):',
                'class Security(unittest.TestCase):\n    def setUp(self):\n        self.client = TestClient(None)')
            return tool_response(replace(call, call_id='bad-setup-test', arguments_json=json.dumps(args)))
        return bad, good, anchored_ready(ready)

    async def recovered(self, missing_first):
        from orchestrator.domain import AgentRole, WorkflowStatus
        from test_security_plan_paths import read_source
        helper, f, packages = self.fixture()
        providers = f.providers()
        bad, good, ready = self.actions(*providers[AgentRole.SECURITY].script)
        def unreferenced(request):
            # Before any read, a strict provider can offer no anchorId (Codex review 2026-11-18 #5),
            # so the first file-less READY carries no references.
            from test_llm_runtime import text_response
            data = json.loads(ready(request).output_text)
            for field in ('requirementReviews', 'findingReviews'):
                for row in data.get(field) or []:
                    row['references'] = []
            return text_response(json.dumps(data, ensure_ascii=False))
        providers[AgentRole.SECURITY].script = ([unreferenced, bad, read_source, ready, good, ready] if missing_first
                                              else [bad, ready, read_source, ready, good, ready])
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        requests = providers[AgentRole.SECURITY].requests
        # Codex review 2026-11-18 #5: the answer schema offers only anchors of files read in this step,
        # so an unread citation is refused at the output schema before any publication (a strict
        # provider cannot even produce it); the run recovers after reading the file.
        if not missing_first:
            self.assertIn('VALUE_NOT_ALLOWED', requests[2].input_items_json)
        self.assertTrue(any(('GENERATED_TEST_FILE_MISSING' if missing_first else 'TEST_CLIENT_NOT_STARTED')
                            in request.input_items_json for request in requests[2:]))
        if missing_first:
            self.assertEqual(requests[1].required_tool, 'write_test_file')

    async def test_lifespan_and_unread_reference_recover_to_final_package(self):
        await self.recovered(False)

    async def test_missing_file_forced_write_also_preserves_read_feedback(self):
        await self.recovered(True)

    async def test_ignored_reference_with_test_defect_stops_before_semantic_publication(self):
        from orchestrator.domain import AgentRole, WorkflowStatus
        helper, f, packages = self.fixture()
        providers = f.providers()
        bad, _good, ready = self.actions(*providers[AgentRole.SECURITY].script)
        provider = providers[AgentRole.SECURITY]
        provider.script = [bad, ready, ready]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'UNVERIFIED'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertFalse([r for r in provider.requests if r.output.name == 'product_semantic_review_v5'])
        self.assertFalse([a for a in f.repository.list_project_artifacts(run.run_id)
                          if a.artifact_type == 'SECURITY_REPORT'])
        self.assertTrue(any(e.event_type == 'SECURITY_DRAFT_CORRECTIONS_EXHAUSTED' for e in helper.events(f, run)))

    async def test_single_uses_the_same_combined_checks_and_read_receipts(self):
        from test_completion_paths import adopt_cleanups, enroll_packages
        from test_security_plan_paths import read_source
        from orchestrator.domain import WorkflowStatus
        import test_single_agent_pipeline as fixture
        for name in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(name, lambda *_: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            adopt_cleanups(self, base)
        base.measured_security = True
        f = base.f
        packages = enroll_packages(f)
        provider = base.provider()
        bad, good, ready = self.actions(*provider.script[-2:])
        provider.script[-2:] = [bad, ready, read_source, ready, good, ready]
        async with base.running(provider=provider, cap=32, tool_cap=40, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
