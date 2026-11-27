"""Offline completion paths (docs/live-api/05-completion-rules.md, 석민 확정 2026-11-18).

Real A2A/Host/Git/SQLite/receipts with synthetic model and Docker adapters, like
tests/test_recovery_pipeline.py. Every Run here must reach a final verdict:

* Ver1, the generated QA scope cannot run in either round while the protected
  tests pass: one in-step test fix is used, the not-run cases stay with their
  attempt evidence, and the conflict policy leaves them out of fix requests as an
  unresolved conflict (Trace VALIDATION_CONFLICT_POLICY_EXCLUDED) -> FINISHED SUCCESS;
* Ver2 (no protected tests), the same failure in both rounds -> FINISHED
  UNVERIFIED as a technical not-run, never HUMAN_REVIEW and never a fix request;
* Ver2, the fix round repairs the tests -> FINISHED SUCCESS after two test runs.

This verifies routing and accounting, not model accuracy or a real Docker run.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import unittest
from unittest import mock
from uuid import UUID

from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.observability import ObservationStore
from test_llm_runtime import tool_response
import test_owned_agent_pipeline as multi_fixture
from fixture_cleanup import adopt_cleanups
from test_recovery_pipeline import enable_protected_cases

RUNNER_ERROR = b'{"error":"TEST_RUNNER_ERROR"}'


def enroll_packages(f):
    from agents.platform.package_service import HostPackageService
    packages = HostPackageService(f.repository)
    prepare = f.prepare_workspace

    def enrolled(run_id):
        packages.enroll(run_id)
        prepare(run_id)
    f.prepare_workspace = enrolled
    return packages


def renamed(action, call_id):
    """The same tool call under a fresh ID, as a real model would issue in a new round."""
    def response(request):
        call = action(request).tool_calls[0]
        return tool_response(replace(call, call_id=call_id))
    return response


class CompletionPathTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, ver1):
        # The inert fixture seed has no main.py; the static start precheck would rightly reject it.
        for target in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(target, lambda *args, **kwargs: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        f = multi_fixture.OwnedAgentPipelineTests('runTest')
        try:
            f.setUp()
        finally:
            adopt_cleanups(self, f)
        prepare = f.prepare_workspace

        def observed_prepare(run_id):
            ObservationStore(f.repository).pin_policy(run_id)
            prepare(run_id)
        f.prepare_workspace = observed_prepare
        f.security_tests = True
        if ver1:
            enable_protected_cases(f)
            # The production Host derives these from its tool configuration (host._protected_selectors);
            # this low-level fixture builds the platform directly, so pass the same names.
            original = multi_fixture.create_platform

            def with_selectors(**values):
                return original(**values, protected_selectors=frozenset(
                    {"run_unit_tests:protected-qa", "run_unit_tests:protected-security"}))
            patcher = mock.patch.object(multi_fixture, "create_platform", with_selectors)
            patcher.start()
            self.addCleanup(patcher.stop)
        return f

    def fail_generated_qa_runs(self, f, failing_rounds, role=AgentRole.QA, *, setup_errors=False):
        """Generated test scope runs of ``role`` fail in the given 1-based rounds; protected runs always pass."""
        original, rounds = f.peer_client, []

        @asynccontextmanager
        async def peer_client(configuration):
            async with original(configuration) as client:
                if configuration.binding.role is role and role in f.dockers:
                    docker = f.dockers[role][-1]
                    passing = (docker.exit_code, docker.start_result)

                    def hook(container):
                        command = container["Config"]["Cmd"]
                        kind = command[command.index("--kind") + 1] if "--kind" in command else None
                        if kind == "QA_TESTS":
                            rounds.append(1)
                        if kind == "QA_TESTS" and len(rounds) in failing_rounds:
                            if setup_errors:
                                report = json.loads(passing[1].stdout)
                                cases = report['tests']
                                rows = []
                                for case in cases:
                                    rows.extend([
                                        {'testId': case['testId'], 'outcome': 'SKIP',
                                         'details': 'NOT_RUN_SETUP_FAILED'},
                                        {'testId': 'setUp (' + case['testId'] + ')', 'outcome': 'FAIL',
                                         'details': '[phase=SETUP] RuntimeError: fixture startup [origin=TEST]'},
                                    ])
                                report.update(total=len(rows), passed=0, failed=len(cases), skipped=len(cases), tests=rows)
                                docker.exit_code = 1
                                docker.start_result = multi_fixture.CLIResult(
                                    returncode=1, stdout=json.dumps(report).encode(), stderr=b'')
                            else:
                                docker.exit_code = 2
                                docker.start_result = multi_fixture.CLIResult(returncode=2, stdout=RUNNER_ERROR, stderr=b"")
                        else:
                            docker.exit_code, docker.start_result = passing
                    docker.start_hook = hook
                yield client
        f.peer_client = peer_client
        return rounds

    def providers_with_fix_round(self, f):
        providers = f.providers()
        qa = providers[AgentRole.QA].script
        qa.extend([renamed(qa[0], "qa-write-fix"), qa[1]])
        return providers

    async def submit(self, f, providers):
        async with f.running_platform(providers, model_cap=32, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()['run']['runId'])
            return f.repository.get_run(run_id)

    def events(self, f, run):
        events, _total = f.repository.list_events(run.run_id, limit=1000, offset=0)
        return events

    def qa_report(self, f, run):
        from orchestrator.domain import QAReportArtifact
        return [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, QAReportArtifact)][-1]

    def feedback_requests(self, providers):
        found = []
        for request in providers[AgentRole.QA].requests:
            text = getattr(request, "input_items_json", "") or ""
            if "testRunFeedback" in text:
                found.append(request)
        return found

    async def test_unobserved_generated_security_pass_is_repaired_once_then_saved_as_unverified(self):
        f = self.fixture(ver1=False)
        packages = enroll_packages(f)
        f.security_output_checks = {}
        providers = f.providers()
        script = providers[AgentRole.SECURITY].script
        script.extend([renamed(script[0], 'coverage-fix-write'), script[1]])
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value, run.fix_attempt), (WorkflowStatus.FINISHED, 'UNVERIFIED', 0))
        from orchestrator.domain.validation_artifacts import SecurityReportArtifact
        report, = [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, SecurityReportArtifact)]
        item, = [r for r in report.requirement_results if r.actual_result == 'OUTPUT_COVERAGE_INCOMPLETE']
        details = json.loads(item.details)
        self.assertEqual(set(details['generatedOutputCoverage'].values()), {'UNVERIFIED'})
        self.assertTrue(all(r['outcome'] == 'PASS' for r in details['tests']))
        self.assertTrue(item.execution_evidence)
        feedback = [r for r in providers[AgentRole.SECURITY].requests if 'testRunFeedback' in r.input_items_json]
        self.assertEqual(len(feedback), 2)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def qa_concurrency_run(self, repaired, *, ver1=False):
        f = self.fixture(ver1=ver1)
        packages = enroll_packages(f)
        providers = f.providers()
        script = providers[AgentRole.QA].script
        write, ready = script
        requirements = f.scenario.requirement_ids_for(multi_fixture.RequirementValidator.QA)
        peer_client = f.peer_client
        attempts = []
        @asynccontextmanager
        async def scoped_receipts(configuration):
            async with peer_client(configuration) as client:
                if configuration.binding.role is AgentRole.QA:
                    docker = f.dockers[AgentRole.QA][-1]
                    complete = docker.start_result
                    def receipt(container):
                        if '--qa-output-checks' in container['Config']['Cmd']:
                            attempts.append(1)
                            data = json.loads(complete.stdout)
                            if len(attempts) == 1 or not repaired:
                                for row in data['tests']:
                                    row.pop('signupChecks', None)
                            docker.start_result = replace(complete, stdout=json.dumps(data).encode())
                    docker.start_hook = receipt
                yield client
        f.peer_client = scoped_receipts
        identity = next(item.requirement_id for item in f.scenario.requirements if item.key == 'REQ-003')
        def write_cases(request, *, concurrent):
            content = 'from _host_app_test import AppTestCase\nfrom _host_test_data import new_password\nclass Signup(AppTestCase):\n'
            for index, requirement in enumerate(requirements, 1):
                content += f'    def test_req_{index}(self):\n'
                if requirement != identity:
                    content += '        self.assertTrue(True)\n'
                    continue
                content += "        data = {'email': 'trial@example.org', 'password': new_password()}\n"
                if concurrent:
                    content += ("        statuses = self.concurrent_signup_statuses(data, data)\n"
                                "        self.assertEqual(sorted(code // 100 for code in statuses), [2, 4])\n")
                else:
                    content += ("        first = self.client.post('/api/auth/signup', json=data)\n"
                                "        second = self.client.post('/api/auth/signup', json=data)\n"
                                "        self.assertEqual(first.status_code // 100, 2)\n"
                                "        self.assertEqual(second.status_code // 100, 4)\n")
                content += "        self.assertEqual(self.signup_email_state(data['email'])['exactEmailRows'], 1)\n"
            original = write(request).tool_calls[0]
            arguments = {**json.loads(original.arguments_json), 'content': content}
            return tool_response(replace(original, arguments_json=json.dumps(arguments)))
        script[:] = [lambda request: write_cases(request, concurrent=False), ready,
                     renamed(lambda request: write_cases(request, concurrent=repaired), 'rewrite-sequential'), ready]
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.fix_attempt, run.code_version), (WorkflowStatus.FINISHED, 0, 1), run)
        self.assertEqual(len(self.feedback_requests(providers)), 2)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        item, = [test for test in self.qa_report(f, run).tests if test.requirement_id == identity
                 and test.test_id.startswith("run_unit_tests:qa-unit:")]
        return run.verdict.value, item

    async def test_ver1_also_retries_generated_concurrency_with_passing_protected_smoke_tests(self):
        verdict, missing = await self.qa_concurrency_run(repaired=False, ver1=True)
        self.assertEqual((verdict, missing.actual_result), ('UNVERIFIED', 'CONCURRENT_SIGNUPS_NOT_OBSERVED'))

    async def test_sequential_only_signup_gets_one_qa_fix_then_saved_as_unverified(self):
        verdict, missing = await self.qa_concurrency_run(repaired=False)
        self.assertEqual(verdict, 'UNVERIFIED')
        self.assertEqual(missing.actual_result, 'CONCURRENT_SIGNUPS_NOT_OBSERVED')
        self.assertEqual(json.loads(missing.details)['runnerOutcome'], 'PASS')
        self.assertTrue(missing.execution_evidence)

    async def test_qa_concurrency_fix_reuses_the_same_snapshot_and_saves_the_result(self):
        verdict, item = await self.qa_concurrency_run(repaired=True)
        self.assertEqual((verdict, item.outcome.value), ('SUCCESS', 'PASS'))

    async def test_coverage_can_be_repaired_on_the_same_snapshot(self):
        f = self.fixture(ver1=False)
        packages = enroll_packages(f)
        original = f.peer_client
        rounds = []

        @asynccontextmanager
        async def clients(configuration):
            async with original(configuration) as client:
                if configuration.binding.role is AgentRole.SECURITY:
                    docker = f.dockers[AgentRole.SECURITY][-1]
                    passing = docker.start_result
                    def on_start(container):
                        if "--security-output-checks" not in container["Config"]["Cmd"]:
                            return
                        rounds.append(1)
                        if len(rounds) == 1:
                            payload = json.loads(passing.stdout)
                            for row in payload['tests']:
                                row.pop('outputChecks', None)
                            docker.start_result = replace(passing, stdout=json.dumps(payload).encode())
                        else:
                            docker.start_result = passing
                    docker.start_hook = on_start
                yield client
        f.peer_client = clients
        providers = f.providers()
        script = providers[AgentRole.SECURITY].script
        script.extend([renamed(script[0], 'coverage-recovered-write'), script[1]])
        run = await self.submit(f, providers)
        self.assertEqual((len(rounds), run.code_version, run.fix_attempt, run.verdict.value), (2, 1, 0, 'SUCCESS'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_ver1_not_run_generated_tests_with_passing_protected_tests_finish_success(self):
        f = self.fixture(ver1=True)
        rounds = self.fail_generated_qa_runs(f, failing_rounds={1, 2})
        providers = self.providers_with_fix_round(f)
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
        self.assertEqual(len(rounds), 2)                         # first run + one in-step test fix
        # The fix round's two model calls (write, READY) carry the feedback; the first round's do not.
        self.assertEqual(len(self.feedback_requests(providers)), 2)
        self.assertNotIn("testRunFeedback", providers[AgentRole.QA].requests[0].input_items_json)
        report = self.qa_report(f, run)
        generated = [case for case in report.tests if ":qa-unit:" in case.test_id]
        self.assertTrue(generated)
        self.assertTrue(all(case.actual_result == "NOT_RUN_SCOPE_FAILED" and case.tool_evidence is not None
                            for case in generated))
        conflict = [event for event in self.events(f, run) if event.event_type == "VALIDATION_CONFLICT_POLICY_EXCLUDED"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(set(conflict[0].requirement_ids), {case.requirement_id for case in generated})
        self.assertFalse(f.repository.list_issue_records(run.run_id))  # nothing sent to the Developer
        # The post-Run audit must count real report cases, not just Requirement IDs.
        from evaluation.conflicts import conflict_analysis
        events = [event.to_trace_json() for event in self.events(f, run)]
        analysis = conflict_analysis(
            {'run': {'runId': str(run.run_id)}, 'events': events,
             'eventsTotal': len(events), 'snapshotConsistent': True},
            {'runId': str(run.run_id), 'artifacts': [artifact.model_dump(mode='json', by_alias=True)
                for artifact in f.repository.list_project_artifacts(run.run_id)]})
        self.assertEqual((analysis['status'], analysis['excluded'], analysis['undetermined']),
                         ('MEASURED', len(generated), len(generated)), analysis)
        self.assertEqual({row['testId'] for row in analysis['conflicts']}, {case.test_id for case in generated})

    async def test_ver2_not_run_generated_tests_finish_technical_unverified(self):
        f = self.fixture(ver1=False)
        rounds = self.fail_generated_qa_runs(f, failing_rounds={1, 2})
        providers = self.providers_with_fix_round(f)
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'UNVERIFIED'), run)
        self.assertEqual(run.fix_attempt, 0)
        self.assertEqual(len(rounds), 2)
        self.assertFalse(f.repository.list_issue_records(run.run_id))
        self.assertFalse([event for event in self.events(f, run)
                          if event.event_type == "VALIDATION_CONFLICT_POLICY_EXCLUDED"])

    async def test_workspace_id_is_fixed_on_the_wire_for_every_role(self):
        """#31 (2026-11-18): the model never types the Host-owned workspace UUID; the wire offers one value."""
        f = self.fixture(ver1=False)
        providers = f.providers()
        run = await self.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.FINISHED)
        checked = 0
        for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
            for request in providers[role].requests:
                for tool in request.tools or ():
                    field = tool.input_schema.to_dict()['properties'].get('workspaceId')
                    if field is not None:
                        self.assertEqual(field['enum'], [str(run.workspace_id)], (role, tool.name))
                        checked += 1
        self.assertGreater(checked, 0)

    async def test_ver2_repaired_tests_finish_success_after_two_runs(self):
        f = self.fixture(ver1=False)
        rounds = self.fail_generated_qa_runs(f, failing_rounds={1})
        providers = self.providers_with_fix_round(f)
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(len(rounds), 2)
        report = self.qa_report(f, run)
        self.assertTrue(all(case.outcome.value == "PASS" for case in report.tests))


    async def test_ver2_security_not_run_finishes_technical_unverified(self):
        import json
        f = self.fixture(ver1=False)
        rounds = self.fail_generated_qa_runs(f, failing_rounds={1, 2}, role=AgentRole.SECURITY)
        providers = f.providers()
        security = providers[AgentRole.SECURITY].script
        security.extend([renamed(security[0], "security-write-fix"), security[1]])
        run = await self.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'UNVERIFIED'), run)
        self.assertEqual(len(rounds), 2)
        from orchestrator.domain import SecurityReportArtifact
        report = [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, SecurityReportArtifact)][-1]
        for result in report.requirement_results:
            self.assertEqual((result.outcome.value, json.loads(result.details)["code"]), ("UNVERIFIED", "SECURITY_TESTS_NOT_RUN"))
            self.assertIsNotNone(result.tool_evidence)
        self.assertFalse(f.repository.list_issue_records(run.run_id))

    async def test_per_case_setup_failure_repairs_or_finishes_without_developer_blame(self):
        for role, ver1, failures, verdict in (
                (AgentRole.QA, False, {1}, 'SUCCESS'),
                (AgentRole.QA, False, {1, 2}, 'UNVERIFIED'),
                (AgentRole.SECURITY, False, {1, 2}, 'UNVERIFIED'),
                (AgentRole.QA, True, {1, 2}, 'SUCCESS')):
            with self.subTest(role=role, ver1=ver1, failures=failures):
                f = self.fixture(ver1=ver1)
                packages = enroll_packages(f)
                rounds = self.fail_generated_qa_runs(f, failures, role, setup_errors=True)
                providers = f.providers()
                script = providers[role].script
                script.extend([renamed(script[0], 'setup-fix-write'), script[1]])
                run = await self.submit(f, providers)
                self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                                 (WorkflowStatus.FINISHED, verdict),
                                 (role, ver1, failures, run, [e.to_trace_json() for e in self.events(f, run)[-5:]]))
                self.assertEqual((len(rounds), run.fix_attempt, run.code_version), (2, 0, 1))
                self.assertFalse(f.repository.list_issue_records(run.run_id))
                feedback = [r.input_items_json for r in providers[role].requests
                            if 'testRunFeedback' in r.input_items_json]
                self.assertEqual(len(feedback), 2)
                self.assertTrue(all('NOT_RUN_SETUP_FAILED' in value for value in feedback))
                self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_single_not_run_generated_tests_finish_technical_unverified(self):
        await self.single_not_run_generated_tests(setup_errors=False)

    async def test_single_per_case_setup_failure_finishes_technical_unverified(self):
        await self.single_not_run_generated_tests(setup_errors=True)

    async def single_not_run_generated_tests(self, *, setup_errors):
        import test_single_agent_pipeline as single_fixture
        for target in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(target, lambda *args, **kwargs: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            adopt_cleanups(self, base)
        f = base.f
        packages = enroll_packages(f)
        prepare = f.prepare_workspace

        def observed_prepare(run_id):
            ObservationStore(f.repository).pin_policy(run_id)
            prepare(run_id)
        f.prepare_workspace = observed_prepare
        f.security_tests = True
        base.measured_security = True
        rounds = self.fail_generated_qa_runs(f, failing_rounds={1, 2}, setup_errors=setup_errors)
        providers = f.providers()
        qa = providers[AgentRole.QA].script
        qa.extend([renamed(qa[0], "qa-write-fix"), qa[1]])
        provider = single_fixture.FakeProvider(*(action for role in AgentRole for action in providers[role].script))
        async with base.running(cap=32, provider=provider, corrections=2, reminders=1) as (client, _providers):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'UNVERIFIED'), run)
        self.assertEqual(len(rounds), 2)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')


if __name__ == "__main__":
    unittest.main()
