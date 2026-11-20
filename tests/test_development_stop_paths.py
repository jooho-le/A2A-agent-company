"""Real Host/A2A/Git/receipts, fake models and Docker; no paid calls."""
import json
import unittest
from dataclasses import replace
from unittest import mock
from uuid import UUID

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.platform.package_service import HostPackageService
from orchestrator.domain import AgentRole, CodeSnapshotArtifact, WorkflowStatus
from test_llm_runtime import tool_response
import test_completion_paths as completion_fixture
import test_single_agent_pipeline as single_fixture


def read_first(action, call_id='read-before-repair'):
    """A model's bounded read turn on the file the write targets (Codex review 2026-11-19 #2: after a READY
    without changes and without any read, the Host requires one read before the required change)."""
    def response(request):
        call = action(request).tool_calls[0]
        arguments = json.loads(call.arguments_json)
        return tool_response(replace(call, call_id=call_id, name='read_project_file', arguments_json=json.dumps(
            {'workspaceId': arguments['workspaceId'], 'path': arguments['path']})))
    return response


class DevelopmentStopPaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        helper = completion_fixture.CompletionPathTests('runTest')
        try:
            f = helper.fixture(ver1=ver1)
        finally:
            completion_fixture.adopt_cleanups(self, helper)
        packages = HostPackageService(f.repository)
        prepare = f.prepare_workspace
        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        f.prepare_workspace = enrolled
        from contextlib import asynccontextmanager
        original_running = f.running_platform
        @asynccontextmanager
        async def running(providers, **options):
            options.setdefault('tool_cap', 80)
            async with original_running(providers, **options) as values:
                yield values
        f.running_platform = running
        return helper, f, packages

    def assert_closed_failure(self, f, packages, run):
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'FAIL'), run)
        self.assertEqual((run.code_version, run.fix_attempt), (1, 1))
        sources = [a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, CodeSnapshotArtifact)]
        self.assertEqual([a.code_version for a in sources], [1])
        last_step = f.repository.list_steps(run.run_id)[-1]
        self.assertFalse(last_step.output_artifact_ids)
        events, _ = f.repository.list_events(run.run_id, limit=1000, offset=0)
        stops = [e for e in events if e.event_type == 'DEVELOPMENT_NO_PROGRESS_EXHAUSTED']
        self.assertEqual(len(stops), 1)
        self.assertEqual(stops[0].snapshot_sha256, sources[0].snapshot_sha256)
        self.assertEqual(stops[0].code_version, 1)
        self.assertTrue(f.repository.finish_development_no_progress(run.run_id, last_step.workflow_step_id, last_step.a2a_task_id))
        self.assertFalse(f.repository.finish_development_no_progress(run.run_id, last_step.workflow_step_id, 'wrong-task'))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as connection:
            report = json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                                  (str(run.run_id),)).fetchone()[0])
        self.assertEqual(report['codeVersion'], 1)
        self.assertEqual(report['verdict'], 'FAIL')
        self.assertFalse(report['developmentStop']['newCandidatePublished'])
        self.assertEqual(report['developmentStop']['retainedSnapshotSha256'], sources[0].snapshot_sha256)

    async def exercise_multi(self, ver1, *, ignored_write=False, forged=False):
        helper, f, packages = self.fixture(ver1)
        f.qa_failure = True
        providers = f.providers()
        dev = providers[AgentRole.DEVELOPER].script
        write, ready = dev
        # The last two responses represent Run #26's repeated READY, or a same-byte write.
        # Repeated READY never reads, so the Host's last requirement is the bounded read; a same-byte write
        # comes after that read, so the last requirement is the change itself.
        dev.extend([LLMRuntimeError(LLMErrorCode.NO_PROGRESS)] if forged else
                   [ready, read_first(write), completion_fixture.renamed(write, 'unchanged-fix')] if ignored_write
                   else [ready, ready])
        run = await helper.submit(f, providers)
        if forged:
            self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
            self.assertIsNone(run.verdict)
            self.assertFalse([e for e in helper.events(f, run) if e.event_type == 'DEVELOPMENT_NO_PROGRESS_EXHAUSTED'])
        else:
            self.assert_closed_failure(f, packages, run)
            last = providers[AgentRole.DEVELOPER].requests[-1]
            self.assertEqual(last.required_tool, 'write_source_file' if ignored_write else 'read_project_file')
            # No semantic review of the invalid READY is performed after the stop.
            self.assertNotIn('semanticReview', json.loads(last.input_items_json)[0]['content'])

    async def test_multi_ver1_repeated_ready_finishes_with_same_snapshot_fail_and_package(self):
        await self.exercise_multi(True)

    async def test_multi_ver2_unchanged_write_finishes_with_same_snapshot_fail_and_package(self):
        await self.exercise_multi(False, ignored_write=True)

    async def test_comment_only_repair_and_correction_keep_the_measured_candidate(self):
        helper, f, packages = self.fixture(ver1=True)
        f.qa_failure = True
        providers = f.providers()
        dev = providers[AgentRole.DEVELOPER].script
        write, ready = dev

        def comment_write(request):
            call = write(request).tool_calls[0]
            arguments = json.loads(call.arguments_json)
            marker = 'forced' if request.required_tool else 'ordinary'
            arguments['content'] += '\n# ' + marker + ' claimed repair\n'
            return tool_response(replace(call, call_id=marker, arguments_json=json.dumps(arguments)))

        dev.extend([comment_write, ready, read_first(write), comment_write])
        run = await helper.submit(f, providers)
        self.assert_closed_failure(f, packages, run)
        self.assertEqual(providers[AgentRole.DEVELOPER].requests[-1].required_tool, 'write_source_file')

    async def test_agent_failure_code_without_host_proof_cannot_force_terminal_verdict(self):
        await self.exercise_multi(False, forged=True)

    async def test_initial_no_change_aborts_with_reason_without_fabricating_snapshot(self):
        helper, f, _packages = self.fixture()
        providers = f.providers()
        ready = providers[AgentRole.DEVELOPER].script[-1]
        providers[AgentRole.DEVELOPER].script = [ready, ready]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.ABORTED, run)
        self.assertEqual(run.termination_reason, 'DEVELOPMENT_NO_PROGRESS_EXHAUSTED')
        self.assertIsNone(run.verdict)
        self.assertIsNone(run.code_version)
        self.assertEqual(sum(e.event_type == 'RUN_ABORTED' for e in helper.events(f, run)), 1)
        self.assertFalse([a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a, CodeSnapshotArtifact)])

    async def test_build_failure_then_no_edit_saves_package_with_validation_not_run(self):
        from contextlib import asynccontextmanager
        from orchestrator.sandbox.contracts import CLIResult
        helper, f, packages = self.fixture()
        original = f.peer_client
        @asynccontextmanager
        async def failed_build(configuration):
            async with original(configuration) as client:
                if configuration.binding.role is AgentRole.DEVELOPER:
                    docker = f.dockers[AgentRole.DEVELOPER][-1]
                    docker.exit_code = 1
                    docker.start_result = CLIResult(returncode=1, stdout=b'fixture build failure', stderr=b'')
                yield client
        f.peer_client = failed_build
        providers = f.providers()
        ready = providers[AgentRole.DEVELOPER].script[-1]
        providers[AgentRole.DEVELOPER].script.extend([ready, ready])
        run = await helper.submit(f, providers)
        self.assert_closed_failure(f, packages, run)
        with f.repository._connection() as connection:
            report = json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                                   (str(run.run_id),)).fetchone()[0])
        self.assertEqual(report['unexecutedValidation'], {
            kind: {'status': 'NOT_RUN', 'reason': 'BUILD_FAILED'} for kind in ('QA_REPORT', 'SECURITY_REPORT')})
        self.assertNotIn('QA_REPORT', report['artifacts'])
        self.assertNotIn('SECURITY_REPORT', report['artifacts'])

    async def test_single_same_rule_finishes_with_same_snapshot_fail_and_package(self):
        for name in ('agents.runtime.developer.start_precheck_for', 'agents.single.runtime.start_precheck_for'):
            patcher = mock.patch(name, lambda *_: None)
            patcher.start()
            self.addCleanup(patcher.stop)
        base = single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion_fixture.adopt_cleanups(self, base)
        base.measured_security = True
        f = base.f
        f.qa_failure = True
        packages = HostPackageService(f.repository)
        prepare = f.prepare_workspace
        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        f.prepare_workspace = enrolled
        provider = base.provider()
        ready = provider.script[2]
        provider.script.extend([ready, ready])
        async with base.running(cap=32, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assert_closed_failure(f, packages, run)

    async def test_forced_edit_reenters_build_and_validation_with_new_snapshot(self):
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        dev = providers[AgentRole.DEVELOPER].script
        write, ready = dev
        def repair(request):
            self.assertEqual(request.required_tool, 'write_source_file')
            call = write(request).tool_calls[0]
            arguments = json.loads(call.arguments_json)
            arguments['content'] = "def signup():\n    return 'repaired-inert-fixture'\n"
            f.qa_failure = False
            return tool_response(replace(call, call_id='actual-fix', arguments_json=json.dumps(arguments)))
        dev.extend([ready, read_first(write), repair, ready])
        for role in (AgentRole.QA, AgentRole.SECURITY):
            script = providers[role].script
            script.extend([completion_fixture.renamed(script[0], role.value.lower() + '-recheck'), script[1]])
        run = await helper.submit(f, providers)
        if run.status != WorkflowStatus.FINISHED:
            import sqlite3
            failures = []
            for role, settings in f.settings.items():
                with sqlite3.connect(settings.database_path) as connection:
                    for payload, in connection.execute("SELECT payload_json FROM agent_tasks"):
                        failures.append((role.value, json.loads(payload).get('status')))
            events, _ = f.repository.list_events(run.run_id, limit=1000, offset=0)
            self.fail(f'Run did not complete: {run}; task statuses: {failures}; events: {[e.event_type for e in events[-8:]]}')
        self.assertEqual(run.verdict.value, 'SUCCESS')
        self.assertEqual(run.code_version, 2)
        self.assertFalse([e for e in helper.events(f, run) if e.event_type == 'DEVELOPMENT_NO_PROGRESS_EXHAUSTED'])
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
