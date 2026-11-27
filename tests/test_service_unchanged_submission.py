"""Service resubmission on real ASGI/Git/SQLite, fake model and Docker receipts.

No generated application code is executed and no paid API is called.
"""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace as N
import unittest
from uuid import uuid4

from a2a.types import Task
from google.protobuf.json_format import ParseDict
from unittest.mock import patch

from agents.core.identity import SinglePhase
from agents.llm.contracts import SourceEditStatus
from agents.llm.corrections import ExecutedTools, development_check
from agents.platform.budgets import RunBudgetRegistry
from agents.single.contracts import build_single_draft_contract
from agents.single.runtime import SingleAgentExecutor
from fixture_cleanup import adopt_cleanups, set_up_borrowed
from orchestrator.a2a.requests import build_send_message_request
from orchestrator.application.developer_output import DeveloperOutputValidationError, parse_developer_output
from orchestrator.domain import AgentRole, A2ATaskState, WorkflowStepStatus
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, SCN_001_ID, ScenarioDefinition, RequirementValidator, service_request_contract
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.sandbox.contracts import CLIResult
from test_llm_runtime import FakeProvider, text_response
import test_developer_agent as multi_fixture
import test_developer_fix_agent as fix_fixture
import test_llm_runtime as runtime_fixture
import test_single_agent_prompts as single_fixture
import test_developer_workspace as workspace_fixture


class ServiceUnchangedSubmissionTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, *, fixing=False):
        f = (fix_fixture.DeveloperFixTests if fixing else multi_fixture.DeveloperAgentTests)('runTest')
        f.service_request = '기존 API를 유지하고 다시 검사해줘.'
        set_up_borrowed(self, f)
        return f

    async def test_multi_no_write_ready_gets_real_snapshot_and_new_build_receipt(self):
        f = self.fixture()
        before = {p.name: p.read_bytes() for p in f.source.iterdir() if p.is_file()}
        provider = FakeProvider(f.draft_response(summary='코드는 변경하지 않았습니다. 현재 Source를 다시 검사해주세요.'))
        task, _ = await f.run_to(provider, 'TASK_STATE_COMPLETED')
        output = f.parse_completed(task)
        self.assertEqual(output.source.commit_hash, f.baseline_commit)
        self.assertEqual(output.change_report.changes, ())
        self.assertEqual(output.change_report.ready_review.edits, ())
        self.assertEqual(output.change_report.ready_review.validation_status, 'PENDING_QA_SECURITY')
        self.assertEqual(len(provider.requests), 1)
        self.assertEqual([name for name, _args in f.tool_calls()], ['run_build'])
        self.assertEqual(before, {p.name: p.read_bytes() for p in f.source.iterdir() if p.is_file()})
        # Empty edits cannot cite a different code baseline.
        bad = deepcopy(task)
        change = next(row for row in bad['artifacts'] if row['name'] == 'change-report.json')
        change['parts'][0]['data']['readyReview']['workingBaseCommit'] = 'f' * 40
        with self.assertRaises(DeveloperOutputValidationError):
            f.parse_completed(bad)
        missing_review = deepcopy(task)
        next(row for row in missing_review['artifacts'] if row['name'] == 'change-report.json')['parts'][0]['data'].pop('readyReview')
        with self.assertRaises(DeveloperOutputValidationError):
            f.parse_completed(missing_review)
        completed = f.step.model_copy(update={'status': WorkflowStepStatus.SUCCEEDED,
            'a2a_task_state': A2ATaskState.COMPLETED, 'a2a_task_id': task['id']})
        with self.assertRaises(DeveloperOutputValidationError) as caught:
            parse_developer_output(ParseDict(task, Task()), run=f.run.model_copy(update={'scenario_id': SCN_001_ID}),
                                   step=completed)
        self.assertIn('only a service', str(caught.exception.__cause__))

    async def test_multi_fix_no_write_gets_new_version_and_does_not_reuse_old_build(self):
        f = self.fixture(fixing=True)
        await f.prepare_fix()
        previous = f.previous
        self.assertFalse(previous.build_report.passed)
        f.docker.exit_code = 0
        f.docker.start_result = CLIResult(returncode=0, stdout=b'fresh synthetic build receipt\n', stderr=b'')
        provider = FakeProvider(f.draft_response(summary='추가 코드 변경 없이 같은 후보의 새 검증을 요청합니다.'))
        task, _ = await f.run_to(provider, 'TASK_STATE_COMPLETED')
        output = f.parse_completed(task)
        self.assertEqual(output.source.code_version, 2)
        self.assertEqual(output.source.commit_hash, previous.source.commit_hash)
        self.assertEqual(output.source.snapshot_sha256, previous.source.snapshot_sha256)
        self.assertNotEqual(output.source.artifact_id, previous.source.artifact_id)
        self.assertEqual(output.source.previous_artifact_id, previous.source.artifact_id)
        self.assertNotEqual(output.build_report.execution_manifest_id, previous.build_report.execution_manifest_id)
        self.assertTrue(output.build_report.passed)
        self.assertEqual(output.change_report.changes, ())
        self.assertEqual(output.change_report.ready_review.edits, ())
        self.assertEqual([name for name, _args in f.tool_calls()], ['run_build'])
        self.assertEqual(len(provider.requests), 1)
        f.observe_completed(task, output)
        run, _step, validators = f.repository.record_developer_candidate(f.run.run_id, f.step.workflow_step_id,
            source=output.source, change_report=output.change_report, build_report=output.build_report,
            validation_agents_configured=True,
            validation_requirement_ids={AgentRole.QA: f.scenario.requirement_ids_for(RequirementValidator.QA),
                                       AgentRole.SECURITY: f.scenario.requirement_ids_for(RequirementValidator.SECURITY)})
        self.assertEqual(run.status.value, 'REVALIDATING')
        self.assertEqual({step.agent_role for step in validators}, {AgentRole.QA, AgentRole.SECURITY})
        self.assertTrue(all(output.source.artifact_id in step.input_artifact_ids for step in validators))

    async def test_single_runtime_implementation_and_fix_accept_ready_without_a_write(self):
        f = single_fixture.SingleConversationTests('runTest')
        try:
            await f.asyncSetUp()
        finally:
            adopt_cleanups(self, f)
        runtime = runtime_fixture.LLMRuntimeTests('runTest')
        set_up_borrowed(self, runtime)
        contract = service_request_contract('기존 API를 다시 검사해줘.')
        scenario = ScenarioDefinition.from_contract(contract)
        repository = SQLiteWorkflowRepository(Path(f.temporary.name) / 'host.db')
        for phase, version in ((SinglePhase.IMPLEMENTING, 1), (SinglePhase.FIXING, 2)):
            with self.subTest(phase=phase):
                metadata = f.metadata.model_copy(update={'run_id': uuid4(), 'workflow_step_id': uuid4(),
                    'scenario_id': SERVICE_WEB_ID, 'requirement_ids': scenario.requirement_ids, 'code_version': version})
                task_input = {'serviceContract': contract}
                claimed = await f.store.claim_message(build_send_message_request(task_input, metadata), f.call_context)
                context = N(context_id=claimed.task.context_id, call_context=f.call_context)
                budget = runtime.budget(max_output_corrections=0)
                execution = N(metadata=metadata, scenario=scenario, budget=budget,
                    model=runtime.model, configuration=N(workspace_id=uuid4()))
                draft = build_single_draft_contract(phase, scenario=scenario)
                ready = {'kind': 'READY', 'questions': [], 'rejection': None, 'summary': '무변경 후보의 새 검증 요청'}
                provider = FakeProvider(text_response(json.dumps(ready, ensure_ascii=False)))
                executor = SingleAgentExecutor(repository=repository, budgets=RunBudgetRegistry(repository, limits=budget.limits),
                    provider=provider, services_factories={r: lambda _: None for r in
                        (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY)})
                executor.bind_task_store(f.store)
                decision, result, _ = await executor._model(context, execution, task_input, phase, draft,
                    source_progress_check=lambda: SourceEditStatus.UNCHANGED)
                self.assertEqual(decision.kind, 'READY')
                self.assertEqual(result.executed_tool_names, ())
                self.assertEqual(len(provider.requests), 1)


class UnchangedCheckpointTests(unittest.TestCase):
    def test_unchanged_snapshot_reuses_parent_without_an_empty_commit(self):
        f = workspace_fixture.DeveloperWorkspaceTests('runTest')
        set_up_borrowed(self, f)
        checkpoint = f.make_checkpoint(allow_unchanged_source=True, runtime_repair=True)
        checkpoint.prepare()
        self.assertIs(checkpoint.source_edit_status(), SourceEditStatus.UNCHANGED)
        with patch.object(checkpoint, '_commit', side_effect=AssertionError('empty commit must not be written')):
            result = checkpoint.checkpoint()
        self.assertEqual(result.commit_hash, f.baseline)
        self.assertEqual(result.changes, ())
        self.assertEqual(result.edits, ())

    def test_unchanged_allowance_still_rejects_a_modified_dependency_lock(self):
        f = workspace_fixture.DeveloperWorkspaceTests('runTest')
        set_up_borrowed(self, f)
        checkpoint = f.make_checkpoint(allow_unchanged_source=True)
        checkpoint.prepare()
        (f.source / 'requirements.lock').write_bytes(b'changed-fixture==2\n')
        with patch.object(checkpoint, '_commit', side_effect=AssertionError('changed lock must not be frozen')):
            f.assert_code('LOCK_MISMATCH', checkpoint.checkpoint)

    def test_no_change_allowance_keeps_prerequisite_checks_and_experiment_default(self):
        ready = {'kind': 'READY'}
        tools = ExecutedTools((), source_changed=False)
        validate = lambda data: N(kind=data['kind'])
        self.assertEqual(development_check(validate)(ready, tools)['code'], 'READY_WITHOUT_CHANGES')
        self.assertIsNone(development_check(validate, allow_unchanged_source=True)(ready, tools))
        invalid = development_check(validate, allow_unchanged_source=True,
            service_interface_check=lambda: [{'fieldPath': '/source', 'reasonCode': 'SERVICE_ROOT_HTML_REQUIRED'}])
        self.assertEqual(invalid(ready, tools)['code'], 'READY_SERVICE_INTERFACE_REVIEW')
