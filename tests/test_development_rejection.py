"""Explained rejections: actual A2A/SQLite/packages, synthetic model and Docker."""
import json
import sqlite3
from pathlib import Path
import unittest
from dataclasses import replace
from unittest import mock
from uuid import UUID, uuid4

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.development_rejection import RejectionReason, assess_rejection
from agents.roles.developer_contract import DeveloperContractError, validate_developer_decision
from agents.platform.package_service import HostPackageService
from orchestrator.domain import AgentRole, CodeSnapshotArtifact, WorkflowStatus
from test_llm_runtime import text_response, tool_response
import test_completion_paths as completion
import test_development_stop_paths as stop_fixture
import test_single_agent_pipeline as single_fixture


def reason(code='IMPLEMENTATION_BLOCKED', ids=()):
    return {'reasonCode': code, 'explanation': 'The assigned implementation cannot be completed.',
            'requirementIds': list(map(str, ids))}


def rejected(code='IMPLEMENTATION_BLOCKED', ids=()):
    return text_response(json.dumps({'kind': 'REJECTED', 'summary': '', 'questions': [],
                                    'rejection': reason(code, ids)}))


class RejectionContractTests(unittest.TestCase):
    def test_reason_is_required_only_for_rejected_and_not_an_execution_claim(self):
        for kind, summary, rejection in [('REJECTED', '', None), ('READY', 'Work', reason())]:
            with self.assertRaises(DeveloperContractError):
                validate_developer_decision({'kind': kind, 'summary': summary, 'questions': [], 'rejection': rejection})
        data = json.loads(rejected().output_text)
        self.assertEqual(validate_developer_decision(data).rejection.reason_code, 'IMPLEMENTATION_BLOCKED')
        data.pop('rejection')
        with self.assertRaises(DeveloperContractError):
            validate_developer_decision(data)

    def test_reason_is_bounded_closed_and_contains_only_canonical_ids(self):
        for changes in ({'explanation': ''}, {'explanation': 'x' * 1025}, {'explanation': 'line\nbreak'},
                        {'reasonCode': 'PASS'}, {'requirementIds': ['fake']}, {'verdict': 'SUCCESS'},
                        {'explanation': 'OPENAI_API_KEY=sk-' + 'a' * 40}):
            with self.assertRaises(ValueError):
                RejectionReason.model_validate({**reason(), **changes})

    def test_host_facts_do_not_promote_generic_scope_or_policy_claims(self):
        assigned = uuid4()
        for code in ('OUT_OF_SCOPE', 'REQUIREMENTS_CONFLICT', 'POLICY_BLOCKED', 'UNKNOWN'):
            self.assertEqual(assess_rejection(reason(code, [assigned]), assigned_ids=[assigned],
                                             source_changed=True).classification, 'HUMAN_REVIEW')
        self.assertEqual(assess_rejection(reason('REQUIREMENT_NOT_ASSIGNED', [assigned]), assigned_ids=[assigned],
                                         source_changed=False).basis, 'REQUIREMENTS_ARE_ASSIGNED')
        self.assertEqual(assess_rejection(reason('BUILD_TOOL_UNAVAILABLE'), assigned_ids=[assigned],
                                         source_changed=False).basis, 'HOST_RUNS_BUILD')
        for code in ('BUILD_TOOL_UNAVAILABLE', 'REQUIREMENT_NOT_ASSIGNED', 'IMPLEMENTATION_BLOCKED'):
            self.assertEqual(assess_rejection(reason(code, [uuid4()]), assigned_ids=[assigned],
                                             source_changed=True).classification, 'HUMAN_REVIEW')


class RejectionPipelineTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            result = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        return result

    def records(self, f, run):
        with f.repository._connection() as c:
            return [json.loads(v) for (v,) in c.execute('SELECT payload_json FROM agent_runtime_events WHERE run_id=?',
                                                      (str(run.run_id),))]

    def rejections(self, f, run):
        return [d['detail']['recovery'] for d in self.records(f, run)
                if d.get('detail', {}).get('recovery', {}).get('developmentRejection')]

    def assert_package(self, f, packages, run):
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'FAIL'), run)
        self.assertEqual(run.code_version, 1)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as c:
            report = json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                          (str(run.run_id),)).fetchone()[0])
        self.assertEqual(report['developmentStop']['reason'], 'DEVELOPMENT_REJECTION_EXHAUSTED')
        self.assertFalse(report['developmentStop']['newCandidatePublished'])
        self.assertEqual(report['developmentRejections'][-1]['action'], 'STOP')
        self.assertEqual(len([a for a in f.repository.list_project_artifacts(run.run_id)
                             if isinstance(a, CodeSnapshotArtifact)]), 1)
        return report

    async def exhausted(self, ver1, *, edit=False):
        helper, f, packages = self.fixture(ver1)
        f.qa_failure = True
        providers = f.providers()
        dev = providers[AgentRole.DEVELOPER].script
        write, ready = dev
        if edit:
            def edit_then_decline(request):
                call = write(request).tool_calls[0]
                args = json.loads(call.arguments_json)
                args['content'] = "def signup():\n    return 'unpublished-work'\n"
                return tool_response(replace(call, call_id='edit-before-rejection', arguments_json=json.dumps(args)))
            dev.append(edit_then_decline)
        dev.extend([rejected(ids=f.scenario.requirement_ids), rejected(ids=f.scenario.requirement_ids)])
        run = await helper.submit(f, providers)
        report = self.assert_package(f, packages, run)
        routes = self.rejections(f, run)
        self.assertEqual([r['action'] for r in routes], ['REGENERATE', 'STOP'])
        self.assertEqual(routes[-1]['reason'], 'REPEATED')
        self.assertEqual(report['developmentRejections'][-1]['developmentRejection']['sourceChanged'], edit)
        if edit:
            record = f.registry.get_record(run.workspace_id, run_id=run.run_id)
            self.assertIn('unpublished-work', (Path(record.root_path) / 'source/signup.py').read_text())
        self.assertEqual(report['developmentRejections'][-1]['developmentRejection']['classification'],
                         'MODEL_DECLARED_TECHNICAL_STOP')
        with sqlite3.connect(f.settings[AgentRole.DEVELOPER].database_path) as c:
            tasks = [json.loads(v) for v, in c.execute('SELECT payload_json FROM agent_tasks')]
        final = next(t for t in tasks if t['status']['state'] == 'TASK_STATE_FAILED')
        payload = final['status']['message']['parts'][0]['data']
        self.assertEqual(payload['code'], 'LLM_DEVELOPMENT_REJECTION_EXHAUSTED')
        self.assertEqual(payload['developmentRejection']['proposal']['reasonCode'], 'IMPLEMENTATION_BLOCKED')

    async def test_multi_ver1_rejection_after_edit_preserves_last_candidate_and_reason(self):
        await self.exhausted(True, edit=True)

    async def test_multi_ver2_exhaustion_stores_result_package(self):
        await self.exhausted(False)

    async def test_confirmed_build_responsibility_misunderstanding_recovers_to_success(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.insert(1, rejected('BUILD_TOOL_UNAVAILABLE'))
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        route, = self.rejections(f, run)
        self.assertEqual(route['developmentRejection']['basis'], 'HOST_RUNS_BUILD')
        self.assertEqual(route['action'], 'REGENERATE')
        self.assertTrue(route['developmentRejection']['sourceChanged'])
        # Three work responses plus the unchanged, separate semantic review.
        self.assertEqual(len([d for d in self.records(f, run)
            if d.get('binding', {}).get('actor') == 'DEVELOPER'
            and d.get('binding', {}).get('eventType') == 'LLM_MODEL_CALLED']), 4)

    async def test_unchanged_measured_failure_rejection_closes_with_proof_and_package(self):
        helper, f, packages = self.fixture(True)
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.extend([
            rejected('NO_CHANGE_REQUIRED', f.scenario.requirement_ids),
            rejected('NO_CHANGE_REQUIRED', f.scenario.requirement_ids)])
        run = await helper.submit(f, providers)
        from agents.runtime.development_failure import bound_failure
        with f.repository._connection() as connection:
            proof = bound_failure(connection, f.repository.list_steps(run.run_id)[-1])
        self.assertIsNotNone(proof)
        self.assertEqual(run.status.value, 'FINISHED', self.records(f, run)[-5:])
        report = self.assert_package(f, packages, run)
        evidence = report['developmentRejections'][-1]['developmentRejection']
        self.assertEqual(evidence['basis'], 'UNCHANGED_MEASURED_FAILURE')
        self.assertTrue(evidence['failureEvidence']['issueIds'])

    async def test_no_change_claim_without_measured_candidate_stays_review(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script = [rejected('NO_CHANGE_REQUIRED', f.scenario.requirement_ids)]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)

    async def test_wrong_snapshot_in_host_assessment_cannot_authorize_terminal_stop(self):
        from agents.runtime.development_failure import failure_reader
        helper, f, _ = self.fixture(True)
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.extend([rejected('NO_CHANGE_REQUIRED', f.scenario.requirement_ids)] * 2)
        def corrupt(services, execution):
            read = failure_reader(services, execution)
            def wrong():
                evidence = read()
                return evidence.model_copy(update={'source_artifact_id': uuid4()}) if evidence else None
            return wrong
        with mock.patch('agents.runtime.development_failure.failure_reader', side_effect=corrupt):
            run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertIsNone(run.verdict)

    async def test_policy_and_unconfirmed_rejections_remain_review_with_explanation(self):
        for code in ('POLICY_BLOCKED', 'OUT_OF_SCOPE', 'REQUIREMENTS_CONFLICT', 'UNKNOWN'):
            with self.subTest(code=code):
                helper, f, packages = self.fixture()
                providers = f.providers()
                providers[AgentRole.DEVELOPER].script = [rejected(code, f.scenario.requirement_ids)]
                run = await helper.submit(f, providers)
                self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
                route, = self.rejections(f, run)
                self.assertEqual(route['action'], 'RETURN_TO_VALIDATOR')
                self.assertEqual(route['developmentRejection']['classification'], 'HUMAN_REVIEW')
                with sqlite3.connect(f.settings[AgentRole.DEVELOPER].database_path) as c:
                    task = json.loads(c.execute('SELECT payload_json FROM agent_tasks').fetchone()[0])
                data = task['status']['message']['parts'][0]['data']
                self.assertEqual(data['code'], 'DEVELOPER_REJECTED')
                self.assertEqual(data['developmentRejection']['proposal']['reasonCode'], code)

    async def test_forged_failure_code_without_host_routing_proof_does_not_finish(self):
        helper, f, _ = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.DEVELOPMENT_BLOCKED))
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        self.assertIsNone(run.verdict)

    async def test_real_policy_hold_after_a_correction_is_not_relabelled_as_exhaustion(self):
        helper, f, _ = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.extend([
            rejected(ids=f.scenario.requirement_ids), rejected('POLICY_BLOCKED', f.scenario.requirement_ids)])
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertEqual([r['action'] for r in self.rejections(f, run)], ['REGENERATE', 'RETURN_TO_VALIDATOR'])
        self.assertFalse([e for e in helper.events(f, run) if e.event_type == 'DEVELOPMENT_REJECTION_EXHAUSTED'])

    async def test_explained_policy_rejection_after_forced_write_keeps_review(self):
        helper, f, _ = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        write, ready = providers[AgentRole.DEVELOPER].script
        # Codex review 2026-11-19 #2: READY before any read gets one bounded read turn, then the forced write.
        from test_development_stop_paths import read_first
        providers[AgentRole.DEVELOPER].script.extend([ready, read_first(write), rejected('POLICY_BLOCKED')])
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertEqual(providers[AgentRole.DEVELOPER].requests[-1].required_tool, 'write_source_file')
        route, = self.rejections(f, run)
        self.assertEqual(route['developmentRejection']['proposal']['reasonCode'], 'POLICY_BLOCKED')
        self.assertFalse([e for e in helper.events(f, run) if e.event_type in (
            'DEVELOPMENT_NO_PROGRESS_EXHAUSTED', 'DEVELOPMENT_REJECTION_EXHAUSTED')])

    async def test_real_question_after_forced_write_keeps_input_boundary(self):
        helper, f, _ = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        ready = providers[AgentRole.DEVELOPER].script[-1]
        question = text_response(json.dumps({'kind': 'INPUT_REQUIRED', 'summary': '',
            'questions': ['The approved policies conflict; which should apply?'], 'rejection': None}))
        providers[AgentRole.DEVELOPER].script.extend([ready, question, question])
        run = await helper.submit(f, providers)
        # The existing FIXING dispatcher sends a valid input-required Task to
        # HUMAN_REVIEW; preserve that boundary rather than inventing a new state.
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertIsNone(run.verdict)
        self.assertIsNone(providers[AgentRole.DEVELOPER].requests[-1].required_tool)
        with sqlite3.connect(f.settings[AgentRole.DEVELOPER].database_path) as c:
            tasks = [json.loads(v) for v, in c.execute('SELECT payload_json FROM agent_tasks')]
        waiting = [t for t in tasks if t['status']['state'] == 'TASK_STATE_INPUT_REQUIRED']
        self.assertEqual(len(waiting), 1)
        data = waiting[0]['status']['message']['parts'][0]['data']
        self.assertEqual(data['code'], 'DEVELOPER_INPUT_REQUIRED')
        self.assertEqual(data['questions'], ['The approved policies conflict; which should apply?'])
        self.assertFalse([e for e in helper.events(f, run) if e.event_type in (
            'DEVELOPMENT_NO_PROGRESS_EXHAUSTED', 'DEVELOPMENT_REJECTION_EXHAUSTED')])

    async def test_provider_refusal_is_not_a_model_implementation_stop(self):
        helper, f, _ = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.extend([
            rejected(ids=f.scenario.requirement_ids), text_response(refused=True)])
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertEqual([r['action'] for r in self.rejections(f, run)], ['REGENERATE'])
        self.assertFalse([e for e in helper.events(f, run) if e.event_type == 'DEVELOPMENT_REJECTION_EXHAUSTED'])

    async def test_disabled_correction_does_not_buy_an_extra_call(self):
        helper, f, _ = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script = [rejected(ids=f.scenario.requirement_ids)]
        async with f.running_platform(providers, model_cap=32, corrections=0, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual(run.status, WorkflowStatus.ABORTED, run)
        route, = self.rejections(f, run)
        self.assertEqual((route['action'], route['reason']), ('STOP', 'DISABLED'))

    async def test_changing_rejection_code_does_not_buy_a_second_correction(self):
        helper, f, _ = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script = [
            rejected(ids=f.scenario.requirement_ids), rejected('BUILD_TOOL_UNAVAILABLE')]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.ABORTED, run)
        first, last = self.rejections(f, run)
        self.assertEqual(first['action'], 'REGENERATE')
        self.assertEqual((last['action'], last['reason']), ('STOP', 'REPEATED'))
        self.assertEqual(last['developmentRejection']['classification'], 'HOST_CONTRADICTED')

    async def test_first_implementation_stop_aborts_without_inventing_snapshot(self):
        helper, f, _ = self.fixture()
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script = [rejected(ids=f.scenario.requirement_ids)] * 2
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.ABORTED, run)
        self.assertEqual(run.termination_reason, 'DEVELOPMENT_REJECTION_EXHAUSTED')
        self.assertIsNone(run.code_version)
        self.assertIsNone(run.verdict)
        self.assertEqual(len(self.rejections(f, run)), 2)

    async def single_stop(self, code):
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
        f.qa_failure = True
        packages = HostPackageService(f.repository)
        prepare = f.prepare_workspace
        def enrolled(run_id):
            packages.enroll(run_id)
            prepare(run_id)
        f.prepare_workspace = enrolled
        provider = base.provider()
        provider.script.extend([rejected(code, ids=f.scenario.requirement_ids)] * 2)
        async with base.running(cap=32, tool_cap=40, provider=provider, corrections=2, reminders=1) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run = f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assert_package(f, packages, run)

    async def test_single_development_uses_same_rejection_stop_and_package(self):
        await self.single_stop('IMPLEMENTATION_BLOCKED')

    async def test_single_no_change_claim_uses_same_measured_failure_and_package(self):
        await self.single_stop('NO_CHANGE_REQUIRED')
