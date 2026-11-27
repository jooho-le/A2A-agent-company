"""Run #33: file-less READY must require a real write or close with Host evidence.

Real role executors, A2A, SQLite, workspace writes and packaging; model/Docker
adapters are synthetic. No cloud API or generated code executes on the Host.
"""
import json
import unittest
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID
from unittest import mock

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError, ToolCall
from agents.platform.package_service import HostPackageService
from orchestrator.domain import AgentRole, WorkflowStatus
from test_llm_runtime import text_response, tool_response
import test_completion_paths as completion
import test_qa_draft_recovery as qa_fixture
import test_single_agent_pipeline as single_fixture


class MissingTestFilePaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner = qa_fixture.QAExhaustionPaths('runTest')
        try:
            result = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        return result

    def assert_missing_stop(self, f, packages, run):
        report = qa_fixture.QAExhaustionPaths.assert_package(self, f, packages, run)
        with f.repository._connection() as c:
            rows = [json.loads(row[0]) for row in c.execute(
                "SELECT payload_json FROM agent_runtime_events WHERE run_id=?", (str(run.run_id),))]
        stopped = [row for row in rows if row.get('binding', {}).get('actor') in ('QA', 'SINGLE_AGENT')
                   and row.get('detail', {}).get('recovery', {}).get('draftIssue') == 'GENERATED_TEST_FILE_MISSING'
                   and row['detail']['recovery'].get('action') == 'STOP']
        self.assertEqual(len(stopped), 1)
        self.assertEqual(stopped[0]['detail']['recovery']['code'], 'LLM_VALIDATION_DRAFT_EXHAUSTED')
        self.assertIn(stopped[0]['binding']['eventId'], report['validationStop']['failureTraceEventIds'])
        return report

    async def exercise_repeated_ready(self, ver1):
        helper, f, packages = self.fixture(ver1)
        providers = f.providers()
        ready = providers[AgentRole.QA].script[1]
        providers[AgentRole.QA].script = [ready, ready]
        run = await helper.submit(f, providers)
        self.assert_missing_stop(f, packages, run)
        calls = [r for r in providers[AgentRole.QA].requests if r.output.name != 'product_semantic_review_v5']
        self.assertEqual([r.required_tool for r in calls], [None, 'write_test_file'])
        self.assertFalse([r for r in providers[AgentRole.QA].requests if r.output.name == 'product_semantic_review_v5'])

    async def test_multi_ver1_repeated_ready_saves_unverified_package(self):
        await self.exercise_repeated_ready(True)

    async def test_multi_ver2_repeated_ready_saves_unverified_package(self):
        await self.exercise_repeated_ready(False)

    async def test_disabled_corrections_preserve_missing_file_evidence_and_package(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        ready = providers[AgentRole.QA].script[1]
        providers[AgentRole.QA].script = [ready]
        original = f.running_platform

        @asynccontextmanager
        async def without_corrections(providers, **options):
            options['corrections'] = 0
            async with original(providers, **options) as value:
                yield value
        f.running_platform = without_corrections
        run = await helper.submit(f, providers)
        self.assert_missing_stop(f, packages, run)
        calls = [r for r in providers[AgentRole.QA].requests if r.output.name != 'product_semantic_review_v5']
        self.assertEqual(len(calls), 1)

    async def test_missing_file_recovers_by_writing_and_finishes_success(self):
        helper, f, packages = self.fixture(True)
        providers = f.providers()
        write, ready = providers[AgentRole.QA].script

        def actual_write(request):
            self.assertEqual(request.required_tool, 'write_test_file')
            call = write(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            count = len(json.loads(ready(request).output_text)['cases'])
            args['content'] = 'import unittest\nclass Signup(unittest.TestCase):\n' + ''.join(
                f'    def test_req_{i}(self): pass\n' for i in range(1, count + 1))
            return tool_response(replace(call, arguments_json=json.dumps(args)))

        providers[AgentRole.QA].script = [ready, actual_write, ready]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'SUCCESS'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        calls = [r for r in providers[AgentRole.QA].requests if r.output.name != 'product_semantic_review_v5']
        self.assertEqual([r.required_tool for r in calls], [None, 'write_test_file', None])

    async def test_wrong_filename_does_not_satisfy_file_presence(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, ready = providers[AgentRole.QA].script

        def wrong_file(request):
            call = write(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            args['path'] = 'outputs/qa/tests/helper.py'
            return tool_response(replace(call, arguments_json=json.dumps(args)))

        providers[AgentRole.QA].script = [ready, wrong_file, ready]
        run = await helper.submit(f, providers)
        self.assert_missing_stop(f, packages, run)

    async def test_read_instead_of_required_write_stops_before_execution(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, ready = providers[AgentRole.QA].script

        def read_only(request):
            args = json.loads(write(request).tool_calls[0].arguments_json)
            return tool_response(ToolCall(call_id='ignored-write', name='read_project_file',
                arguments_json=json.dumps({'workspaceId': args['workspaceId'], 'path': 'source/signup.py'})))

        providers[AgentRole.QA].script = [ready, read_only]
        run = await helper.submit(f, providers)
        self.assert_missing_stop(f, packages, run)
        events = helper.events(f, run)
        self.assertFalse([e for e in events if e.actor == 'QA' and e.event_type == 'MCP_TOOL_CALLED'])

    async def test_permission_violation_cannot_use_missing_file_exit(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        write, ready = providers[AgentRole.QA].script

        def forbidden(request):
            call = write(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            args['path'] = 'outputs/security/tests/test_signup.py'
            return tool_response(replace(call, arguments_json=json.dumps(args)))

        providers[AgentRole.QA].script = [ready, forbidden, forbidden]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_valid_rejection_after_missing_file_is_not_converted(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        ready = providers[AgentRole.QA].script[1]
        providers[AgentRole.QA].script = [ready, text_response(json.dumps(
            {'kind': 'REJECTED', 'cases': [], 'questions': []}))]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_provider_failure_keeps_its_original_stop(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        ready = providers[AgentRole.QA].script[1]
        providers[AgentRole.QA].script = [ready, LLMRuntimeError(LLMErrorCode.PROVIDER)]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_security_uses_same_file_recovery_and_preserves_qa_report(self):
        helper, f, packages = self.fixture(True)
        providers = f.providers()
        ready = providers[AgentRole.SECURITY].script[1]
        providers[AgentRole.SECURITY].script = [ready, ready]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'UNVERIFIED'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as c:
            report = json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                         (str(run.run_id),)).fetchone()[0])
        self.assertIn('QA_REPORT', report['artifacts'])
        self.assertIn('SECURITY_REPORT', report['unexecutedValidation'])

    async def test_both_missing_validation_files_save_both_reasons(self):
        helper, f, packages = self.fixture(True)
        providers = f.providers()
        for role in (AgentRole.QA, AgentRole.SECURITY):
            ready = providers[role].script[1]
            providers[role].script = [ready, ready]
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value), ('FINISHED', 'UNVERIFIED'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as c:
            report = json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                         (str(run.run_id),)).fetchone()[0])
        self.assertEqual(set(report['unexecutedValidation']), {'QA_REPORT', 'SECURITY_REPORT'})
        self.assertEqual(len(report['validationStops']), 2)

    async def test_single_missing_qa_still_completes_security_and_package(self):
        for target in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(target, lambda *_: None)
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
        ready = provider.script[4]
        provider.script[3:5] = [ready, ready]
        async with base.running(cap=32, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assert_missing_stop(f, packages, run)
