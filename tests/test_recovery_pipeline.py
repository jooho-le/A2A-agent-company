"""Offline full-pipeline fault scripts: Single, Multi Ver1 and Multi Ver2.

Real A2A/Host/Git/SQLite/receipts; synthetic model and Docker adapters. Ver1
adds Host-owned protected cases to both validators; Ver2 has model tests only.
This verifies routing/accounting, not real model accuracy or Docker execution.
"""
import json
from dataclasses import replace
import unittest
from uuid import UUID

from fixture_cleanup import set_up_borrowed

from agents.llm.contracts import LLMResponse, TokenUsage
from agents.platform.evaluation_export import write_evaluation_export
from agents.roles.qa_contract import QACaseBinding
from agents.runtime.qa_services import QARuntimeServices
from agents.runtime.security_services import SecurityRuntimeServices
from evaluation.evidence_metrics import process_metrics
from evaluation.host_process import derive_process_evidence
from evaluation.recovery_evidence import recovery_assessment
from mcp_tools.tools.unit_config import UnitTestScope
from orchestrator.domain import AgentRole, WorkflowStatus
from orchestrator.domain.scenario_registry import RequirementValidator
from orchestrator.observability import ObservationStore
from semantic_helpers import SemanticFakeProvider, supported_review
from test_llm_runtime import text_response, tool_response
import test_owned_agent_pipeline as multi_fixture
import test_single_agent_pipeline as single_fixture


def broken_path(action):
    def response(request):
        call = action(request).tool_calls[0]
        arguments = json.loads(call.arguments_json)
        arguments['path'] = 'signup.py'
        return tool_response(replace(call, call_id='rejected-path', arguments_json=json.dumps(arguments)))
    return response


def one_bad_review():
    seen = False
    def decide(request):
        nonlocal seen
        data = supported_review(request)
        if not seen:
            seen = True
            # Valid JSON/wire schema, but SUPPORTED without any evidence.
            # The semantic technical checker must regenerate before publication.
            next(iter(data['fieldReviews'].values()))['evidence'] = []
        return text_response(json.dumps(data))
    return decide


def injected_scripts(f):
    providers = f.providers()
    providers[AgentRole.PLANNER].script.insert(0, text_response(json.dumps({
        'kind': 'INPUT_REQUIRED', 'questions': ['Which signup path?'],
        'unapprovedAssumptions': [], 'implementationPlan': []})))
    dev = providers[AgentRole.DEVELOPER].script
    dev.insert(0, broken_path(dev[0]))
    dev.insert(2, text_response('{"kind":'))
    providers[AgentRole.SECURITY].script.insert(0, LLMResponse(status='incomplete', model_id='fake-model',
        usage=TokenUsage(input_tokens=10, output_tokens=4096, total_tokens=4106), output_text='{"kind":'))
    return providers


def enable_protected_cases(f):
    reference = 'https://tests.example.invalid/recovery/v1'
    f.configuration = f.configuration.model_copy(update={'protected_test_suite_ref': reference})
    original = f.services
    def services(role, execution):
        service = original(role, execution)
        if role not in (AgentRole.QA, AgentRole.SECURITY):
            return service
        qa = role is AgentRole.QA
        selector = 'protected-qa' if qa else 'protected-security'
        scope = UnitTestScope(name=selector, kind='PROTECTED', role=None if qa else 'SECURITY',
            protected_suite_ref=reference, protected_files={'tests/test_protected.py': '# inert protected fixture\n'})
        unit = service.configuration.unit_test_configuration
        config = replace(service.configuration, unit_test_configuration=replace(unit, scopes=(*unit.scopes, scope)))
        requirements = f.scenario.requirement_ids_for(RequirementValidator.QA if qa else RequirementValidator.SECURITY)
        prefix = 'test_signup.Signup' if qa else 'test_security.Security'
        cases = tuple(QACaseBinding(tool_name='run_unit_tests', selector=selector,
            test_id=f'{prefix}.test_req_{i}', requirement_id=requirement, title='Protected fixture',
            expected_result=next(r.acceptance_criteria[0] for r in f.scenario.requirements if r.requirement_id == requirement))
            for i, requirement in enumerate(requirements, 1))
        return (QARuntimeServices if qa else SecurityRuntimeServices)(f.repository, f.registry, f.artifacts,
            mcp_configuration=config, client_factory=f.peer_client, protected_cases=cases)
    f.services = services


class RecoveryPipelineTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, mode):
        single = mode == 'single'
        base = (single_fixture.SingleAgentPipelineTests('runTest') if single
                else multi_fixture.OwnedAgentPipelineTests('runTest'))
        set_up_borrowed(self, base)
        f = base.f if single else base
        prepare = f.prepare_workspace
        def observed_prepare(run_id):
            # Production Host pins this before source preparation; the low-level
            # composition fixture deliberately does not enroll observation policy.
            ObservationStore(f.repository).pin_policy(run_id)
            prepare(run_id)
        f.prepare_workspace = observed_prepare
        f.security_tests = True
        if single:
            base.measured_security = True
        if mode == 'ver1':
            enable_protected_cases(f)
        providers = injected_scripts(f)
        if single:
            provider = SemanticFakeProvider(*(action for role in AgentRole for action in providers[role].script),
                                            review_decider=one_bad_review())
            running = base.running(cap=32, provider=provider, corrections=2, reminders=1)
        else:
            providers[AgentRole.PLANNER].review_decider = one_bad_review()
            running = base.running_platform(providers, model_cap=32, corrections=2, reminders=1)
        async with running as (client, _providers):
            response = await client.post('/api/v1/runs', json=f.submission())
            self.assertEqual(response.status_code, 201, response.text)
            run_id = UUID(response.json()['run']['runId'])
            run = f.repository.get_run(run_id)
            self.assertEqual((run.status, run.verdict.value if run.verdict else None),
                             (WorkflowStatus.FINISHED, 'SUCCESS'), run)
            self.assertEqual((run.fix_attempt, run.code_version), (0, 1))
        stores = ({'SINGLE_AGENT': f.directory / 'single.sqlite3'} if single else
                  {role.value: settings.database_path for role, settings in f.settings.items()})
        files = write_evaluation_export(f.repository, run_id, f.directory / 'recovery-export', stores=stores)
        bundle, trace = (json.loads(files[name].read_text()) for name in ('bundle.json', 'trace.json'))
        recovery = recovery_assessment(bundle, trace)
        self.assertEqual(recovery['status'], 'PASS', recovery)
        self.assertEqual(recovery['regenerationCount'], 5, recovery)
        self.assertEqual({r['code'] for r in recovery['regenerations']}, {
            'QUESTIONS_NOT_ANSWERED', 'TOOL_ARGUMENTS_INVALID', 'MODEL_OUTPUT_INVALID',
            'OUTPUT_LIMIT_REACHED', 'SEMANTIC_REVIEW_FORMAT_INVALID'
        })
        evidence = derive_process_evidence({'bundle': bundle, 'trace': trace})
        metrics = process_metrics(str(run_id), evidence)
        self.assertEqual(metrics['retries']['channels']['LLM'], 5, evidence.get('llmRecovery'))
        self.assertEqual(metrics['retries']['channels']['A2A'], 0)
        # Regeneration never re-dispatches a role, refills budget or changes Source.
        self.assertEqual(len(f.repository.list_steps(run_id)), 4)
        sources = [r for r in f.repository.list_project_artifacts(run_id) if r.artifact_type == 'SOURCE']
        self.assertEqual(len(sources), 1)
        if mode == 'ver1':
            for role in (AgentRole.QA, AgentRole.SECURITY):
                scopes = [args['testScope'] for r, session in f.sessions if r is role
                          for name, args, _ in session.calls if name == 'run_unit_tests']
                self.assertIn('protected-qa' if role is AgentRole.QA else 'protected-security', scopes)
        # Every extra call has actual usage; the policy never manufactures zero usage.
        self.assertEqual(bundle['usage']['total'], 16)

    async def test_single_recovers_to_final_verdict(self):
        await self.exercise('single')

    async def test_multi_ver1_recovers_with_both_protected_validators(self):
        await self.exercise('ver1')

    async def test_multi_ver2_recovers_with_generated_tests(self):
        await self.exercise('ver2')

    async def test_repeated_semantic_format_error_stops_without_publishing_or_developing(self):
        f = multi_fixture.OwnedAgentPipelineTests('runTest')
        set_up_borrowed(self, f)
        providers = f.providers()
        def invalid(request):
            data = supported_review(request)
            next(iter(data['fieldReviews'].values()))['evidence'] = []
            return text_response(json.dumps(data))
        providers[AgentRole.PLANNER].review_decider = invalid
        async with f.running_platform(providers, corrections=3) as (client, _):
            response = await client.post('/api/v1/runs', json=f.submission())
            run_id = UUID(response.json()['run']['runId'])
        self.assertEqual(f.repository.get_run(run_id).status, WorkflowStatus.HUMAN_REVIEW)
        self.assertIsNone(f.repository.get_planning_artifact(run_id))
        self.assertFalse(providers[AgentRole.DEVELOPER].requests)
        reviews = [r for r in providers[AgentRole.PLANNER].requests if r.output.name == 'product_semantic_review_v5']
        self.assertEqual(len(reviews), 2)

    async def test_genuine_semantic_hold_persists_after_one_bounded_text_revision(self):
        from test_product_semantic_review import held_review
        for single in (False, True):
            base = (single_fixture.SingleAgentPipelineTests('runTest') if single
                    else multi_fixture.OwnedAgentPipelineTests('runTest'))
            set_up_borrowed(self, base)
            f = base.f if single else base
            if single:
                provider = base.provider()
                provider.review_decider = held_review
                running = base.running(provider=provider, corrections=3, reminders=1)
            else:
                providers = f.providers()
                provider = providers[AgentRole.PLANNER]
                provider.review_decider = held_review
                running = base.running_platform(providers, corrections=3, reminders=1)
            # The new policy permits one tool-less producer rewrite, followed by the same judge.
            # Supply that answer explicitly instead of exhausting the old one-answer fixture.
            # V4 (docs/live-api/05 25절): the rewrite returns only the held texts.
            from semantic_helpers import patch_reply
            provider.script.insert(1, patch_reply())
            async with running as (client, _):
                response = await client.post('/api/v1/runs', json=f.submission())
                run_id = UUID(response.json()['run']['runId'])
            self.assertEqual(f.repository.get_run(run_id).status, WorkflowStatus.WAITING_INPUT)
            self.assertIsNone(f.repository.get_planning_artifact(run_id))
            reviews = [r for r in provider.requests if r.output.name == 'product_semantic_review_v5']
            self.assertEqual(len(reviews), 2)
            rewrites = [r for r in provider.requests if r.output.name != 'product_semantic_review_v5'
                        and 'semanticReviewFeedback' in r.input_items_json]
            self.assertEqual(len(rewrites), 1)
            self.assertFalse(rewrites[0].tools)
