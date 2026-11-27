"""Real A2A/Git/SQLite preparation and isolated executor, synthetic provider/Docker."""
from contextlib import asynccontextmanager
from hashlib import sha256
import json
from pathlib import Path
import shutil
import sqlite3
import unittest
from unittest.mock import Mock, patch

from agents.llm.budget import ExecutionBudget, LLMLimits
from agents.platform.evaluation_export import write_evaluation_export
from diagnostics import developer_replay as replay
from mcp_tools.client import BoundMCPClient
from mcp_tools.runtime import MCPDispatcher
from mcp_tools.tools.build import BuildTools
from mcp_tools.tools.build_config import encode_build_configuration
from mcp_tools.tools.build_store import BuildOutputStore
from mcp_tools.tools.files import FileTools
from orchestrator.artifacts.service import ArtifactStore
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.runtime import SandboxRuntime
from orchestrator.workspaces.registry import WorkspaceRegistry
from test_mcp_execution_runtime import DispatcherSession
from test_sandbox_runtime import FakeDocker
from test_stage_replay import ClosingProvider
from test_development_rejection import rejected
import test_development_stop_paths as stop_fixture
import test_completion_paths as completion
import test_owned_agent_fix_pipeline as fix_fixture


def export_inputs(f, run):
    with sqlite3.connect(f.repository.database_path) as src, sqlite3.connect(f.directory/'orchestrator.sqlite3') as dst:
        src.backup(dst)
    write_evaluation_export(f.repository, run.run_id, f.directory/'export',
        stores={role: settings.database_path for role,settings in f.settings.items()})
    replay._write(f.directory/'source-version.json', replay._code_hashes())


@asynccontextmanager
async def build_client(configuration):
    assert configuration.binding.role is AgentRole.DEVELOPER
    repository = SQLiteWorkflowRepository(configuration.database_path)
    registry = WorkspaceRegistry(repository, configuration.workspace_root)
    artifacts = ArtifactStore(repository, registry)
    files = FileTools(artifacts)
    sandbox = SandboxRuntime(repository, registry, artifacts, docker=FakeDocker())
    build = BuildTools(artifacts, sandbox, BuildOutputStore(repository),
        configuration=configuration.build_configuration, max_call_seconds=configuration.max_call_seconds)
    dispatcher = MCPDispatcher(configuration.binding, registry,
        handlers={**files.handlers(AgentRole.DEVELOPER), **build.handlers(AgentRole.DEVELOPER)}, max_call_seconds=5)
    session = DispatcherSession(dispatcher)
    yield BoundMCPClient(configuration=configuration, _client=type('Peer',(),{'session':session})())


class PublishedRepairTests(unittest.IsolatedAsyncioTestCase):
    async def test_rewind_keeps_original_feedback_and_excludes_subsequent_results(self):
        harness = fix_fixture._FixHarness(self)
        owner = fix_fixture.OwnedAgentFixPipelineTests()
        run, artifacts, steps, _ = await owner.submit(harness)
        f = harness.base
        export_inputs(f, run)
        step = next(s for s in steps if s.agent_role is AgentRole.DEVELOPER and s.code_version == 2)
        later_source = next(a for a in artifacts if a.artifact_type == 'SOURCE' and a.code_version == 2)
        base = next(a for a in artifacts if a.artifact_type == 'SOURCE' and a.code_version == 1)
        evidence_hash = sha256((f.directory/'orchestrator.sqlite3').read_bytes()).hexdigest()
        services = f.services(AgentRole.DEVELOPER, next(e for r,e in f.service_contexts if r is AgentRole.DEVELOPER))
        args = dict(policy={'build': encode_build_configuration(services.configuration.build_configuration),
                    'maxCallSeconds': 5, 'baselineCommitHash': f.baseline_commit,
                    'repositoryId': base.repository_id, 'lockPath': 'requirements.lock'},
                    limits=LLMLimits(max_model_calls=10, max_tool_calls=10, max_total_tokens=10000),
                    runtime_budget_ms=30000, step_id=str(step.workflow_step_id))
        with self.assertRaisesRegex(replay.ReplayError, 'EXPECTS_COMPLETED'):
            await replay.prepare_replay(f.directory, **args, base_code_version=2)
        with self.assertRaisesRegex(replay.ReplayError, 'EXPECTS_UNPUBLISHED'):
            await replay.prepare_replay(f.directory, **args)
        root = await replay.prepare_replay(f.directory, **args, base_code_version=1)
        self.addCleanup(shutil.rmtree, root)
        manifest = replay.verify_prepared(root)
        self.assertEqual(manifest['baseCodeVersion'], 1)
        self.assertFalse(manifest['measurementEligible'])
        fix = replay._read_json(root/'projected-input.json')['fixRequest']
        self.assertEqual([i['issueId'] for i in fix['issues']], [i['issueId'] for i in harness.fix_inputs[0]['fixRequest']['issues']])
        self.assertEqual(fix['issues'][0]['sourceArtifactId'], str(base.artifact_id))
        with sqlite3.connect(root/'diagnostic.sqlite3') as db:
            for table in ('project_artifacts', 'artifact_contents', 'snapshot_read_grants'):
                self.assertEqual(db.execute(f'SELECT count(*) FROM {table} WHERE artifact_id=?',
                    (str(later_source.artifact_id),)).fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM issue_events WHERE event_type='REVALIDATION_FINISHED'").fetchone()[0], 0)
        source = root/'workspaces'/str(run.workspace_id)/'source'
        with self.assertRaisesRegex(replay.ReplayError, 'REPLAY_GIT_FAILED'):
            replay._git(source, 'cat-file', '-e', later_source.commit_hash)
        provider = ClosingProvider(*f.providers()[AgentRole.DEVELOPER].script)
        result = await replay.execute_replay(root, approval_id=manifest['approvalSha256'],
            provider_factory=lambda: provider, client_factory=build_client)
        self.assertEqual(result['taskStatus']['state'], 'TASK_STATE_COMPLETED', result)
        self.assertEqual(len(replay._read_json(root/'attempt/artifacts.json')['artifacts']), 3)
        self.assertEqual(sha256((f.directory/'orchestrator.sqlite3').read_bytes()).hexdigest(), evidence_hash)


class DeveloperReplayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try: helper, f, _packages = owner.fixture()
        finally: completion.adopt_cleanups(self, owner)
        self.f = f
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script = [rejected('UNKNOWN', f.scenario.requirement_ids)]
        self.original_run = await helper.submit(f, providers)
        self.step = f.repository.list_steps(self.original_run.run_id)[-1]
        self.evidence = f.directory
        # A consistent readonly-export copy; no mutation of the real fixture DB.
        export_inputs(f, self.original_run)
        self.original_hash = sha256((self.evidence/'orchestrator.sqlite3').read_bytes()).hexdigest()
        execution = next(e for role,e in f.service_contexts if role is AgentRole.DEVELOPER)
        services = f.services(AgentRole.DEVELOPER, execution)
        self.policy = {'build': encode_build_configuration(services.configuration.build_configuration),
            'maxCallSeconds': 5, 'baselineCommitHash': f.baseline_commit,
            'repositoryId': 'owned-pipeline-fixture', 'lockPath': 'requirements.lock'}
        self.limits = LLMLimits(max_model_calls=10, max_tool_calls=10, max_total_tokens=10000, max_output_tokens=4096)

    async def prepare(self):
        root = await replay.prepare_replay(self.evidence, policy=self.policy, limits=self.limits,
            runtime_budget_ms=30000, step_id=str(self.step.workflow_step_id))
        self.addCleanup(shutil.rmtree, root)
        return root

    async def test_prepare_and_approval_guard_are_inert(self):
        root = await self.prepare()
        manifest = replay.verify_prepared(root)
        self.assertEqual(manifest['policyMode'], 'EXPLICIT_CHANGED_IMPLEMENTATION_DIAGNOSTIC_FORK')
        factory = Mock(side_effect=AssertionError('must remain inert'))
        with self.assertRaisesRegex(replay.ReplayError, 'EXPLICIT_APPROVAL'):
            await replay.execute_replay(root, approval_id=None, provider_factory=factory)
        factory.assert_not_called()
        self.assertFalse((root/'execution-claimed.json').exists())
        self.assertEqual(sha256((self.evidence/'orchestrator.sqlite3').read_bytes()).hexdigest(), self.original_hash)

    async def test_tampered_git_pack_and_plan_are_rejected_before_provider(self):
        root = await self.prepare()
        with (root/'objects.pack').open('ab') as stream: stream.write(b'changed')
        with self.assertRaisesRegex(replay.ReplayError, 'PREPARED_INPUT_CHANGED'):
            replay.verify_prepared(root)
        manifest = replay._read_json(root/'manifest.json'); manifest['runtimeBudgetMs'] += 1
        (root/'manifest.json').write_text(json.dumps(manifest))
        with self.assertRaisesRegex(replay.ReplayError, 'APPROVAL_PLAN_CHANGED'):
            replay.verify_prepared(root)

    async def test_feedback_compares_the_exact_redacted_first_request(self):
        original = replay.with_repair_baseline
        feedback = {'short_password': {'status': 'FAIL'}, 'valid_signup': {'status': 'FAIL'}}

        def project(task_input, checkpoint):
            return {**original(task_input, checkpoint), 'caseFeedback': feedback}

        with patch.object(replay, 'with_repair_baseline', project), patch(
                'agents.runtime.repair_baseline.with_repair_baseline', project):
            for change_after_approval in (False, True):
                with self.subTest(changed=change_after_approval):
                    root = await self.prepare()
                    manifest = replay.verify_prepared(root)
                    sealed = replay._read_json(root / 'projected-input.json')
                    self.assertEqual(sealed['caseFeedback'], replay.redact_data(feedback))
                    raw = {**sealed, 'caseFeedback': feedback}
                    self.assertEqual(manifest['sourceProjectionSha256'], sha256(replay._json(raw).encode()).hexdigest())
                    self.assertNotEqual(manifest['projectedInputSha256'], manifest['sourceProjectionSha256'])
                    if change_after_approval:
                        feedback['valid_signup']['status'] = 'PASS'
                    provider = ClosingProvider(*self.f.providers()[AgentRole.DEVELOPER].script)
                    result = await replay.execute_replay(root, approval_id=manifest['approvalSha256'],
                        provider_factory=lambda: provider, client_factory=build_client)
                    if change_after_approval:
                        self.assertEqual(result['error'], 'REPLAY_INPUT_CHANGED')
                        self.assertEqual(provider.requests, [])
                    else:
                        self.assertEqual(result['taskStatus']['state'], 'TASK_STATE_COMPLETED')
                        first = json.loads(json.loads(provider.requests[0].input_items_json)[0]['content'])
                        self.assertEqual(first['taskInput'], sealed)

    async def test_fake_execution_only_runs_developer_and_build_once(self):
        root = await self.prepare(); manifest = replay.verify_prepared(root)
        provider = ClosingProvider(*self.f.providers()[AgentRole.DEVELOPER].script)
        result = await replay.execute_replay(root, approval_id=manifest['approvalSha256'],
            provider_factory=lambda: provider, client_factory=build_client)
        self.assertIsNone(result['error'], result)
        self.assertEqual(result['taskStatus']['state'], 'TASK_STATE_COMPLETED', result)
        self.assertTrue(provider.closed)
        self.assertFalse(result['measurementEligible']); self.assertIsNone(result['overallVerdict'])
        artifacts = replay._read_json(root/'attempt/artifacts.json')['artifacts']
        self.assertEqual(len(artifacts), 3)
        self.assertEqual(self.f.repository.get_run(self.original_run.run_id), self.original_run)
        self.assertEqual(sha256((self.evidence/'orchestrator.sqlite3').read_bytes()).hexdigest(), self.original_hash)
        with self.assertRaisesRegex(replay.ReplayError, 'ALREADY_ATTEMPTED'):
            await replay.execute_replay(root, approval_id=manifest['approvalSha256'], provider_factory=lambda: provider)

    async def test_setup_failure_is_saved_without_constructing_provider(self):
        root = await self.prepare(); manifest = replay.verify_prepared(root)
        factory = Mock(side_effect=AssertionError('must not create provider'))
        with patch.object(replay, '_assemble', side_effect=replay.ReplayError('REPLAY_GIT_FAILED')):
            with self.assertRaises(replay.ReplayError):
                await replay.execute_replay(root, approval_id=manifest['approvalSha256'], provider_factory=factory)
        factory.assert_not_called()
        self.assertEqual(replay._read_json(root/'attempt/result.json')['error'], 'REPLAY_SETUP_FAILED')

    async def test_input_budget_refusal_saves_failed_task_and_no_billable_call(self):
        root = await self.prepare(); manifest = replay.verify_prepared(root)
        provider = ClosingProvider()
        reserve = ExecutionBudget.reserve_model_call

        def oversized_input(budget, **kwargs):
            return reserve(budget, input_tokens=20000)

        with patch.object(ExecutionBudget, 'reserve_model_call', oversized_input):
            result = await replay.execute_replay(root, approval_id=manifest['approvalSha256'],
                provider_factory=lambda: provider, client_factory=build_client)
        self.assertEqual(result['taskStatus']['state'], 'TASK_STATE_FAILED', result)
        self.assertEqual((result['modelCalls'],result['toolCalls'],result['totalTokens']),(0,0,0))
        self.assertEqual(provider.requests,[])
        self.assertTrue(provider.closed)
        self.assertEqual(replay._read_json(root/'attempt/result.json'),result)
        self.assertEqual(replay._read_json(root/'attempt/artifacts.json')['artifacts'],[])
        self.assertEqual(self.f.repository.get_run(self.original_run.run_id), self.original_run)
