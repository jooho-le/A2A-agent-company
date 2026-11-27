"""Real Host/A2A/Git/evidence, synthetic provider/Docker: no paid calls."""
import json
import unittest
from unittest import mock
from uuid import UUID

from agents.llm.contracts import ToolCall
from agents.platform.package_service import HostPackageService
from orchestrator.domain import AgentRole, WorkflowStatus, SecurityReportArtifact
from semantic_helpers import review_input, supported_review
from test_llm_runtime import text_response, tool_response
import test_development_stop_paths as completion
import test_single_agent_pipeline as single_fixture


def task_input(request):
    return json.loads(json.loads(request.input_items_json)[0]['content'])['taskInput']


def read_source(request):
    data=task_input(request)
    return tool_response(ToolCall(call_id='security-source-read',name='read_project_file',
        arguments_json=json.dumps({'workspaceId':data['workspaceId'],'path':'source/signup.py'})))


def anchored_ready(original):
    def ready(request):
        data=task_input(request)
        catalog=data['sourceReferenceCatalog']
        anchor=next(k for k,v in catalog['anchors'].items() if v['path']=='signup.py')
        draft=json.loads(original(request).output_text)
        for row in draft['requirementReviews']:
            row['references']=[{'anchorId':anchor}]
        return text_response(json.dumps(draft,ensure_ascii=False))
    return ready


def review_with_real_hold_for_free_claims(request):
    """Reproduce #27's hold if a mixed rationale is still sent to meaning review."""
    data=review_input(request)
    result=supported_review(request)
    for pointer in data['fields']:
        if pointer.startswith('/requirementReviews/'):
            result['fieldReviews'][pointer].update(status='UNVERIFIED',reasonCode='INSUFFICIENT_EVIDENCE')
    return text_response(json.dumps(result,ensure_ascii=False))


class SecurityPlanPaths(unittest.IsolatedAsyncioTestCase):
    async def multi(self, ver1, *, read=True, held_title=False, fix_tests=False, recover_read=False,
                    held_after_fix=False):
        owner=completion.DevelopmentStopPaths('runTest')
        try:
            helper,f,packages=owner.fixture(ver1)
        finally:
            completion.completion_fixture.adopt_cleanups(self,owner)
        providers=f.providers();provider=providers[AgentRole.SECURITY]
        write,ready=provider.script
        provider.script=[write,*([read_source] if read else []),anchored_ready(ready)]
        if recover_read:
            provider.script=[write,anchored_ready(ready),read_source,anchored_ready(ready)]
        if fix_tests:
            import test_completion_paths as test_fix
            helper.fail_generated_qa_runs(f, {1}, role=AgentRole.SECURITY)
            provider.script.extend([test_fix.renamed(write, 'security-test-repair'), anchored_ready(ready)])
        provider.review_decider=review_with_real_hold_for_free_claims
        if held_title:
            from semantic_helpers import patch_reply
            provider.script.append(patch_reply())  # the one rewrite (held texts only) after the meaning hold
        if held_title or held_after_fix:
            review_count=0
            def held(request):
                nonlocal review_count
                review_count+=1
                result=supported_review(request)
                if held_title or review_count>1:
                    result['fieldReviews']['/testCases/0/title'].update(status='UNSUPPORTED',reasonCode='UNAPPROVED_SCOPE')
                return text_response(json.dumps(result,ensure_ascii=False))
            provider.review_decider=held
        if not read:
            provider.script.append(anchored_ready(ready))  # repeated unread evidence must stop, not be reviewed
        run=await helper.submit(f,providers)
        if read and not held_title and not held_after_fix:
            self.assertEqual((run.status,run.verdict.value if run.verdict else None),(WorkflowStatus.FINISHED,'SUCCESS'),run)
            self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')
            reports=[a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a,SecurityReportArtifact)]
            self.assertEqual(len(reports),1)
            self.assertTrue(all(r.tool_evidence is not None for r in reports[0].requirement_results))
            reviews=[r for r in provider.requests if r.output.name=='product_semantic_review_v5']
            self.assertEqual(len(reviews),2 if fix_tests else 1)
            fields=review_input(reviews[0])['fields']
            self.assertFalse(any(p.startswith('/requirementReviews/') for p in fields))
            self.assertTrue(any(p.endswith('/expectedResult') for p in fields))
            with f.repository._connection() as db:
                records=[json.loads(r[0]) for r in db.execute('SELECT payload_json FROM agent_semantic_reviews')]
            record=next(r for r in records if r['binding']['stepRole']=='SECURITY')
            self.assertEqual(len(record['binding']['deterministicFields']),3)
            self.assertEqual(record['candidate']['requirementReviews'][0]['rationale'],'HOST_TESTS_PENDING')
        elif not read:
            # docs/live-api/05 13절(2026-11-18): a code reference that stays invalid is a Security
            # draft exhaustion like QA's; it ends FINISHED UNVERIFIED with the QA report kept and is
            # still never sent to semantic review (was HUMAN_REVIEW).
            self.assertEqual((run.status,run.verdict.value if run.verdict else None),(WorkflowStatus.FINISHED,'UNVERIFIED'),run)
            self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')
            self.assertFalse([a for a in f.repository.list_project_artifacts(run.run_id) if isinstance(a,SecurityReportArtifact)])
            reviews=[r for r in provider.requests if r.output.name=='product_semantic_review_v5']
            self.assertEqual(len(reviews),0)
        else:
            self.assertEqual(run.status,WorkflowStatus.HUMAN_REVIEW,run)
            self.assertNotEqual(packages.complete(run.run_id)['status'],'COMPLETED')
            reviews=[r for r in provider.requests if r.output.name=='product_semantic_review_v5']
            # docs/live-api/05 14절: a meaning hold gets one rewrite and a second review.
            self.assertEqual(len(reviews),2 if held_after_fix else 2 if held_title else 0)

    async def test_multi_ver1_read_anchors_and_pending_plan_reach_final_package(self):
        await self.multi(True)

    async def test_multi_ver2_same_rules_reach_final_package(self):
        await self.multi(False)

    async def test_unread_anchor_can_be_repaired_by_real_read_within_same_step(self):
        await self.multi(False,recover_read=True)

    async def test_test_repair_reviews_final_draft_again_and_preserves_measured_verdict(self):
        await self.multi(False,fix_tests=True)

    async def test_unread_anchor_repetition_never_enters_semantic_review(self):
        await self.multi(False,read=False)

    async def test_actual_unapproved_test_intent_still_holds(self):
        await self.multi(False,held_title=True)

    async def test_held_repaired_draft_cannot_reuse_the_previous_semantic_approval(self):
        await self.multi(False,fix_tests=True,held_after_fix=True)

    async def test_single_security_uses_identical_wire_and_measured_reports(self):
        for name in ('agents.runtime.developer.start_precheck_for','agents.single.runtime.start_precheck_for'):
            p=mock.patch(name,lambda *_:None);p.start();self.addCleanup(p.stop)
        base=single_fixture.SingleAgentPipelineTests('runTest')
        try:
            base.setUp()
        finally:
            completion.completion_fixture.adopt_cleanups(self,base)
        base.measured_security=True
        f=base.f;packages=HostPackageService(f.repository);prepare=f.prepare_workspace
        def enrolled(run_id):packages.enroll(run_id);prepare(run_id)
        f.prepare_workspace=enrolled
        provider=base.provider()
        ready=provider.script[-1];provider.script[-1:]=[read_source,anchored_ready(ready)]
        provider.review_decider=review_with_real_hold_for_free_claims
        async with base.running(provider=provider,cap=32,tool_cap=40,corrections=2,reminders=1) as (client,_):
            response=await client.post('/api/v1/runs',json=f.submission())
            self.assertEqual(response.status_code,201,response.text)
            run=f.repository.get_run(UUID(response.json()['run']['runId']))
        self.assertEqual((run.status,run.verdict.value if run.verdict else None),(WorkflowStatus.FINISHED,'SUCCESS'),run)
        self.assertEqual(packages.complete(run.run_id)['status'],'COMPLETED')
