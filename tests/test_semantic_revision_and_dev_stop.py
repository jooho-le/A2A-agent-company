"""docs/live-api/05 14절 오프라인 확인(2026-11-18, 석민 승인).

① 의미 검토 보류(#27 유형): 만든 역할이 보류된 문장만 1회 다시 쓰고 같은 검토를 다시 받는다.
   다시 보류되거나, 기술(형식·근거·연결) 보류이거나, 시험 연결이 바뀌면 지금처럼 멈춘다.
② Developer 형식 교정 소진: 수정 라운드면 마지막 후보·측정 결과로 FINISHED(FAIL), 첫 구현이면
   ABORTED + 사유. Host 기록 없이 Agent 코드만 있으면 지금처럼 사람 검토.
생성 코드는 실행하지 않는다(합성 응답·가짜 Docker).
"""
import json
import unittest

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.runtime.semantic_revision import (POLICY, apply_patch, held_pointers, only_held_texts_changed,
                                              revision_input)
from agents.semantic_review import SemanticReviewRequired
from orchestrator.domain import AgentRole, CodeSnapshotArtifact, WorkflowStatus
from semantic_helpers import patch_reply, review_input, supported_review
from test_llm_runtime import text_response
import test_development_stop_paths as stop_fixture
import test_completion_paths as completion
from test_security_plan_paths import anchored_ready, read_source


def _hold(record_validation):
    return SemanticReviewRequired({'reviewId': 'r', 'reasonCode': 'UNSUPPORTED_MEANING', 'validation': record_validation})


class RevisionRuleTests(unittest.TestCase):
    def test_only_meaning_holds_with_issues_are_revisable(self):
        candidate = {'kind': 'READY', 'summary': 'An unsupported claim'}
        meaning = {'category': 'MEANING', 'issues': [{'fieldPointer': '/summary', 'reasonCode': 'MODEL_UNSUPPORTED',
                                                      'category': 'MEANING', 'modelStatus': 'UNSUPPORTED'}]}
        self.assertEqual(held_pointers(_hold(meaning), candidate), frozenset({'/summary'}))
        for category in ('FORMAT', 'EVIDENCE', 'BINDING', 'NONE'):
            self.assertIsNone(held_pointers(_hold({**meaning, 'category': category}), candidate))
        self.assertIsNone(held_pointers(_hold({'category': 'MEANING', 'issues': []}), candidate))
        self.assertIsNone(held_pointers(SemanticReviewRequired({'reviewId': 'legacy'}), candidate))
        self.assertIsNone(held_pointers(_hold(meaning), {'kind': 'READY'}))

    def test_feedback_carries_pointers_and_codes_but_no_reviewer_text(self):
        held = _hold({'category': 'MEANING', 'issues': [{'fieldPointer': '/x', 'reasonCode': 'MODEL_UNSUPPORTED',
            'category': 'MEANING', 'modelStatus': 'UNSUPPORTED', 'reviewerNote': 'free text'}]})
        data = revision_input({'a': 1}, {'kind': 'READY'}, held, frozenset({'/x'}))
        feedback = data['semanticReviewFeedback']
        self.assertEqual(data['a'], 1)
        self.assertEqual(feedback['issues'], [{'fieldPointer': '/x', 'reasonCode': 'MODEL_UNSUPPORTED',
                                               'category': 'MEANING'}])
        self.assertEqual(feedback['previousAnswer'], {'kind': 'READY'})
        self.assertEqual(feedback['editablePointers'], ['/x'])
        self.assertNotIn('free text', json.dumps(data))

    def test_feedback_carries_the_review_basis_only_when_given(self):
        held = _hold({'category': 'MEANING', 'issues': [{'fieldPointer': '/x', 'reasonCode': 'MODEL_UNSUPPORTED',
            'category': 'MEANING', 'modelStatus': 'UNSUPPORTED'}]})
        basis = {'requirements': [{'requirementId': 'r', 'description': 'd', 'acceptanceCriteria': ['c']}],
                 'policies': {'securityPolicy': {'x': 1}}}
        with_basis = revision_input({}, {'kind': 'READY'}, held, frozenset({'/x'}), basis)
        self.assertEqual(with_basis['semanticReviewFeedback']['reviewBasis'], basis)
        self.assertNotIn('reviewBasis', revision_input({}, {'kind': 'READY'}, held, frozenset({'/x'}))
                         ['semanticReviewFeedback'])

    def test_host_applies_only_an_exact_patch_of_the_held_texts(self):
        """V4: the model returns texts only; IDs and test bindings cannot change by construction, and a
        patch that misses, repeats or exceeds the held pointers, or is blank, keeps the hold."""
        data = {'kind': 'READY', 'cases': [{'testId': 't.C.test_x', 'title': 'Old', 'expectedResult': 'E'}]}
        held = frozenset({'/cases/0/title'})
        good = {'replacements': [{'fieldPointer': '/cases/0/title', 'text': 'New'}]}
        self.assertEqual(apply_patch(data, good, held)['cases'][0], {'testId': 't.C.test_x', 'title': 'New',
                                                                     'expectedResult': 'E'})
        self.assertEqual(data['cases'][0]['title'], 'Old')  # the first candidate is not mutated
        for bad in ({'replacements': []},
                    {'replacements': [{'fieldPointer': '/cases/0/testId', 'text': 'renamed'}]},
                    {'replacements': [good['replacements'][0], good['replacements'][0]]},
                    {'replacements': [{'fieldPointer': '/cases/0/title', 'text': '  '}]},
                    {'replacements': [{'fieldPointer': '/cases/0/title', 'text': 'x', 'extra': 1}]},
                    {'other': []}):
            self.assertIsNone(apply_patch(data, bad, held), bad)

    def test_unheld_fields_and_test_structure_cannot_change(self):
        original = {'kind': 'READY', 'summary': 'Keep this claim',
                    'cases': [{'testId': 'test_x.Check.test_signup', 'title': 'Unsupported claim',
                               'expectedResult': 'Original requirement'}]}
        pointers = frozenset({'/cases/0/title'})
        revised = json.loads(json.dumps(original))
        revised['cases'][0]['title'] = 'A narrower supported claim'
        self.assertTrue(only_held_texts_changed(original, revised, pointers))
        for field, value in [('testId', 'test_x.Check.test_other'), ('expectedResult', 'Weaker requirement')]:
            changed = json.loads(json.dumps(revised))
            changed['cases'][0][field] = value
            self.assertFalse(only_held_texts_changed(original, changed, pointers))
        for changed in ({**revised, 'summary': 'Changed'}, {**revised, 'cases': []},
                        {**revised, 'extra': True}):
            self.assertFalse(only_held_texts_changed(original, changed, pointers))


def held_first_then(supported_after=1, pointer_prefix='/testCases/0/title'):
    """Reviewer holds the first ``supported_after`` reviews of the field, then supports it."""
    count = 0

    def decide(request):
        nonlocal count
        count += 1
        result = supported_review(request)
        if count <= supported_after:
            for pointer in review_input(request)['fields']:
                if pointer.startswith(pointer_prefix):
                    result['fieldReviews'][pointer].update(status='UNSUPPORTED', reasonCode='UNAPPROVED_SCOPE')
        return text_response(json.dumps(result, ensure_ascii=False))
    return decide


class SemanticRevisionPaths(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        return helper, f, packages

    def security(self, providers, extra):
        provider = providers[AgentRole.SECURITY]
        write, ready = provider.script
        provider.script = [write, read_source, anchored_ready(ready), *extra(ready)]
        return provider

    @staticmethod
    def reviews(provider):
        return [r for r in provider.requests if r.output.name == 'product_semantic_review_v5']

    async def test_security_meaning_hold_is_rewritten_once_and_run_succeeds(self):
        helper, f, packages = self.fixture(True)
        providers = f.providers()
        provider = self.security(providers, lambda ready: [patch_reply()])
        provider.review_decider = held_first_then(1)
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.reviews(provider)), 2)
        revision = [r for r in provider.requests if 'semanticReviewFeedback' in r.input_items_json]
        self.assertTrue(revision)
        feedback = json.loads(json.loads(revision[0].input_items_json)[0]['content'])['taskInput']['semanticReviewFeedback']
        self.assertEqual(feedback['issues'][0]['fieldPointer'], '/testCases/0/title')
        self.assertEqual(feedback['policy'], POLICY)
        self.assertTrue(all(not r.tools for r in revision))
        # V3 (Codex review 2026-11-18 #3): the rewrite sees the review's own basis, scoped to this step.
        basis = feedback['reviewBasis']
        self.assertEqual(set(basis), {'requirements', 'policies'})
        security_step = next(s for s in f.repository.list_steps(run.run_id) if s.agent_role == AgentRole.SECURITY)
        self.assertEqual({r['requirementId'] for r in basis['requirements']},
                         {str(i) for i in security_step.requirement_ids})
        self.assertNotIn('source', json.dumps(basis).lower().split('"policies"')[0])

    async def test_security_rewrite_cannot_write_test_files(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        original_write = providers[AgentRole.SECURITY].script[0]
        provider = self.security(providers, lambda ready: [original_write, original_write])
        provider.review_decider = held_first_then(1)
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertEqual(len(self.reviews(provider)), 1)
        revisions = [r for r in provider.requests if 'semanticReviewFeedback' in r.input_items_json]
        self.assertTrue(revisions)
        self.assertTrue(all(not r.tools for r in revisions))
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')

    async def test_security_held_again_after_revision_still_stops(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        provider = self.security(providers, lambda ready: [patch_reply()])
        provider.review_decider = held_first_then(99)
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.reviews(provider)), 2)

    async def test_qa_meaning_hold_is_rewritten_once_and_run_succeeds(self):
        helper, f, packages = self.fixture()
        providers = f.providers()
        qa = providers[AgentRole.QA]
        write, ready = qa.script
        qa.script = [write, ready, patch_reply()]
        qa.review_decider = held_first_then(1, '/cases/0/title')
        run = await helper.submit(f, providers)
        self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                         (WorkflowStatus.FINISHED, 'SUCCESS'), run)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        self.assertEqual(len(self.reviews(qa)), 2)


class DevelopmentFormatStop(unittest.IsolatedAsyncioTestCase):
    def fixture(self, ver1=False):
        owner = stop_fixture.DevelopmentStopPaths('runTest')
        try:
            helper, f, packages = owner.fixture(ver1)
        finally:
            completion.adopt_cleanups(self, owner)
        return helper, f, packages

    async def test_fix_round_format_exhaustion_finishes_fail_with_last_candidate_and_package(self):
        helper, f, packages = self.fixture(True)
        f.qa_failure = True
        providers = f.providers()
        bad = text_response('{}')
        providers[AgentRole.DEVELOPER].script.extend([bad, bad])
        run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'FAIL'), run)
        self.assertEqual((run.code_version, run.fix_attempt), (1, 1))
        events = helper.events(f, run)
        stops = [e for e in events if e.event_type == 'DEVELOPMENT_CORRECTIONS_EXHAUSTED']
        self.assertEqual(len(stops), 1)
        self.assertFalse([e for e in events if e.event_type == 'DEVELOPMENT_NO_PROGRESS_EXHAUSTED'])
        self.assertFalse(any(e.workflow_state == WorkflowStatus.HUMAN_REVIEW for e in events))
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as c:
            report = json.loads(c.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                          (str(run.run_id),)).fetchone()[0])
        self.assertEqual(report['developmentStop']['reason'], 'DEVELOPMENT_CORRECTIONS_EXHAUSTED')
        self.assertEqual(report['verdict'], 'FAIL')

    async def test_unresolved_normal_claim_finishes_on_the_last_measured_candidate(self):
        from dataclasses import replace
        from hashlib import sha256
        from unittest.mock import patch
        from agents.runtime.normal_request_repair import NORMAL_REQUEST
        from agents.runtime.repair_baseline import with_repair_baseline
        from test_llm_runtime import tool_response

        helper, f, packages = self.fixture(True)
        f.qa_failure = True
        providers = f.providers()
        dev = providers[AgentRole.DEVELOPER].script
        initial_write = dev[0]

        def unrelated(request):
            call = initial_write(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            args['content'] += '\ndef other(): return 1\n'
            return tool_response(replace(call, call_id='unrelated-edit', arguments_json=json.dumps(args)))

        def projected(request, checkpoint):
            data = with_repair_baseline(request, checkpoint)
            if 'fixRequest' in data:
                source = checkpoint.repair_source_files()['signup.py']
                data['activeRepairFocus']['brief'].update(readyReportPaths=[NORMAL_REQUEST],
                    priorityRepairPaths=[NORMAL_REQUEST], blockingPrerequisite={
                        'httpStatuses': [500], 'exceptionTypes': ['builtins.TypeError'], 'blockedCheckCount': 0,
                        'sourceSites': [{'toolPath': 'source/signup.py', 'line': 2, 'sourceRef': 'source/signup.py::signup',
                                         'sourceSha256': sha256(source).hexdigest()}]})
            return data

        false_ready = text_response(json.dumps({'kind': 'READY', 'summary': 'Candidate updated.',
            'questions': [], 'rejection': None, 'repairPaths': {NORMAL_REQUEST: {'sourceRef': 'source/signup.py::other',
            'edit': 'Repaired the blocker.', 'recheck': 'Run the normal request.'}}}))
        dev.extend([unrelated, false_ready, false_ready])
        with patch('agents.runtime.repair_baseline.with_repair_baseline', projected):
            run = await helper.submit(f, providers)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'FAIL'))
        self.assertEqual(run.code_version, 1)
        self.assertFalse(f.repository.list_steps(run.run_id)[-1].output_artifact_ids)
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with f.repository._connection() as connection:
            report = json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                                    (str(run.run_id),)).fetchone()[0])
        self.assertEqual(report['developmentStop']['reason'], 'DEVELOPMENT_CORRECTIONS_EXHAUSTED')
        stop = report['developmentStop']
        self.assertFalse(stop['newCandidatePublished'])
        self.assertEqual(stop['correctionCode'], 'READY_RUNTIME_REPAIR_REQUIRED')
        self.assertEqual(stop['unresolvedPaths'], [NORMAL_REQUEST])
        self.assertTrue(any(str(event.event_id) == stop['recoveryEventId'] and event.event_type == 'LLM_RECOVERY_DECIDED'
                            for event in helper.events(f, run)))

    async def test_initial_format_exhaustion_aborts_with_reason(self):
        helper, f, _packages = self.fixture()
        providers = f.providers()
        bad = text_response('{}')
        providers[AgentRole.DEVELOPER].script = [bad, bad]
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.ABORTED, run)
        self.assertEqual(run.termination_reason, 'DEVELOPMENT_CORRECTIONS_EXHAUSTED')
        self.assertIsNone(run.code_version)
        self.assertFalse([a for a in f.repository.list_project_artifacts(run.run_id)
                          if isinstance(a, CodeSnapshotArtifact)])

    async def test_repeated_library_type_error_preserves_measured_snapshot_and_report(self):
        from dataclasses import replace
        from unittest.mock import patch
        from agents.runtime.developer_workspace import GitDeveloperCheckpoint
        from agents.runtime.ready_library_types import ready_library_type_mismatches
        from test_llm_runtime import tool_response

        helper, fixture, packages = self.fixture(ver1=True)
        fixture.qa_failure = True
        providers = fixture.providers()
        script = providers[AgentRole.DEVELOPER].script
        write, ready = script

        def wrong_type(request):
            call = write(request).tool_calls[0]
            args = json.loads(call.arguments_json)
            args['content'] += '\nfrom argon2 import PasswordHasher\nhasher = PasswordHasher(type="text")\n'
            return tool_response(replace(call, call_id='repair-with-invalid-type', arguments_json=json.dumps(args)))

        def check_using_locked_image(_checkpoint, inventory):
            # The Host fixture has a synthetic lock. Supply the registered image/lock at the
            # classifier boundary and exercise the normal correction, dispatch and archive paths.
            environment = {
                'containerImageDigest': 'sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a',
                'dependencyLockHash': 'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0',
            }
            return ready_library_type_mismatches({p: content for p, (content, _mode) in inventory.items()}, environment)

        script.extend([wrong_type, ready, ready])
        with patch.object(GitDeveloperCheckpoint, '_check_library_types', check_using_locked_image):
            run = await helper.submit(fixture, providers)
        self.assertEqual((run.status.value, run.verdict.value if run.verdict else None), ('FINISHED', 'FAIL'), run)
        sources = [a for a in fixture.repository.list_project_artifacts(run.run_id) if isinstance(a, CodeSnapshotArtifact)]
        self.assertEqual([source.code_version for source in sources], [1])
        self.assertEqual(packages.complete(run.run_id)['status'], 'COMPLETED')
        with fixture.repository._connection() as connection:
            report = json.loads(connection.execute('SELECT report FROM host_final_reports WHERE run_id=?',
                                                    (str(run.run_id),)).fetchone()[0])
        stop = report['developmentStop']
        self.assertEqual(stop['correctionCode'], 'READY_LIBRARY_TYPE_MISMATCH')
        self.assertEqual(stop['retainedSnapshotSha256'], sources[0].snapshot_sha256)
        self.assertFalse(stop['newCandidatePublished'])
        self.assertTrue(any(str(e.event_id) == stop['recoveryEventId'] and e.event_type == 'LLM_RECOVERY_DECIDED'
                            for e in helper.events(fixture, run)))

    async def test_agent_code_without_host_record_stays_review(self):
        helper, f, packages = self.fixture()
        f.qa_failure = True
        providers = f.providers()
        providers[AgentRole.DEVELOPER].script.append(LLMRuntimeError(LLMErrorCode.SCHEMA))
        run = await helper.submit(f, providers)
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW, run)
        self.assertNotEqual(packages.complete(run.run_id)['status'], 'COMPLETED')


if __name__ == '__main__':
    unittest.main()
