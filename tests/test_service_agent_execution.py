"""Service prompt admission and bounded corrections through the real engine.

Fake model and in-process A2A only; no cloud request, service listener or Docker.
"""
from dataclasses import replace
import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from agents.llm.contracts import LLMErrorCode, LLMRuntimeError
from agents.llm.corrections import development_check, checked_rejection
from agents.llm.engine import LLMEngine
from agents.llm.recovery import correction_route
from agents.roles.prompts import prepare_role_prompt
from agents.runtime.service_interface import service_interface_check
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import SERVICE_WEB_ID, ScenarioDefinition, service_request_contract
from orchestrator.domain.states import AgentRole
from fixture_cleanup import set_up_borrowed
import test_planner_agent as planner_fixture
import test_llm_runtime as runtime_fixture


class ServiceAgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_planner_executes_with_exact_service_request_and_emits_frozen_requirements(self):
        f = planner_fixture.PlannerAgentTests('runTest')
        set_up_borrowed(self, f)
        f.request_text = '할 일을 추가하고 완료 표시하는 웹사이트를 만들어줘.'
        contract = service_request_contract(f.request_text)
        f.scenario = ScenarioDefinition.from_contract(contract)
        f.metadata = f.metadata.model_copy(update={'scenario_id': SERVICE_WEB_ID, 'requirement_ids': f.scenario.requirement_ids})
        f.configuration = RunConfigurationArtifact(run_id=f.run_id, workspace_id=f.workspace_id,
            scenario_id=SERVICE_WEB_ID, configuration=f.configuration.configuration,
            frozen_scenario_contract_json=json.dumps(contract, ensure_ascii=False))
        draft = f.draft(tasks=[{'taskId': 'TASK-TODO', 'title': '할 일 화면과 기능 구현',
            'description': '요청된 추가·완료 기능과 연결된 화면을 구현한다.',
            'requirementIds': [str(i) for i in f.scenario.requirement_ids], 'dependsOn': []}])
        provider = runtime_fixture.FakeProvider(f.response(draft))
        task, _ = await f.run_to(provider, 'TASK_STATE_COMPLETED')
        payload = task['artifacts'][0]['parts'][0]['data']
        self.assertEqual(payload['requirements'], [{k: r[k] for k in
            ('requirementId', 'key', 'description', 'acceptanceCriteria')} for r in contract['requirements']])
        self.assertEqual(payload['implementationPlan'][0]['title'], '할 일 화면과 기능 구현')
        envelope = json.loads(json.loads(provider.requests[0].input_items_json)[0]['content'])
        self.assertEqual(envelope['taskInput']['request'], f.request_text)
        self.assertEqual(envelope['taskInput']['scenarioContract'], contract)
        self.assertEqual(len(provider.requests), 1)

    async def test_every_service_role_passes_exact_prompt_validation_and_custom_instructions_stay_blocked(self):
        f = runtime_fixture.LLMRuntimeTests('runTest')
        set_up_borrowed(self, f)
        metadata = f.metadata.model_copy(update={'scenario_id': SERVICE_WEB_ID})
        for role in AgentRole:
            prompt = prepare_role_prompt(role, task_input={'serviceContract': service_request_contract('할 일 앱')}, metadata=metadata)
            provider = runtime_fixture.FakeProvider(runtime_fixture.text_response())
            result = await LLMEngine(role=role, provider=provider).run(prompt=prompt, model=f.model,
                output=f.output, budget=f.budget(), workspace_id=f.workspace_id)
            self.assertEqual(result.data, {'answer': 'done'})
            with self.assertRaises(LLMRuntimeError) as caught:
                await LLMEngine(role=role, provider=provider).run(prompt=replace(prompt, system_prompt='arbitrary override'),
                    model=f.model, output=f.output, budget=f.budget(), workspace_id=f.workspace_id)
            self.assertEqual(caught.exception.code, LLMErrorCode.CONFIGURATION)
            self.assertEqual(len(provider.requests), 1)

    async def test_security_draft_stop_keeps_a_value_free_cause(self):
        f = runtime_fixture.LLMRuntimeTests('runTest')
        set_up_borrowed(self, f)
        provider = runtime_fixture.FakeProvider(runtime_fixture.text_response())
        routes = []
        with self.assertRaises(LLMRuntimeError) as caught:
            await LLMEngine(role=AgentRole.SECURITY, provider=provider).run(
                prompt=f.prepared(AgentRole.SECURITY), model=f.model, output=f.output,
                budget=f.budget(max_output_corrections=0), workspace_id=f.workspace_id,
                output_check=lambda data, tools: {'code': 'GENERATED_TESTS_INVALID', 'issues': [
                    {'fieldPath': '/testCases/0/testId', 'reasonCode': 'DECLARED_TEST_NOT_IN_FILE'}]},
                close_invalid_qa_draft=True, recovery_sink=routes.append)
        self.assertEqual(caught.exception.code, LLMErrorCode.VALIDATION_DRAFT)
        stops = [r.public() for r in routes if r.kind == 'DECISION']
        self.assertTrue(any(r.get('draftIssue') == 'DECLARED_TEST_NOT_IN_FILE' for r in stops))
        self.assertEqual(len(provider.requests), 1)

    async def test_single_service_keeps_one_identity_and_the_same_request_contract(self):
        import test_single_agent_prompts as single_fixture
        from fixture_cleanup import adopt_cleanups
        from agents.core.identity import SingleAgentIdentity, SinglePhase
        from agents.single.contracts import build_single_draft_contract
        from orchestrator.a2a.requests import build_send_message_request
        f = single_fixture.SingleConversationTests('runTest')
        try:
            await f.asyncSetUp()
        finally:
            adopt_cleanups(self, f)
        request = '할 일 추가·완료 웹사이트'
        contract = service_request_contract(request)
        f.scenario = ScenarioDefinition.from_contract(contract)
        f.metadata = f.metadata.model_copy(update={'scenario_id': SERVICE_WEB_ID, 'run_id': uuid4()})
        f.first = (await f.store.claim_message(build_send_message_request({'request': request}, f.metadata), f.call_context)).task
        draft_contract = build_single_draft_contract(SinglePhase.PLANNING, scenario=f.scenario)
        prompt = await f.prepare(task_input={'request': request, 'scenarioContract': contract}, contract=draft_contract)
        runtime = runtime_fixture.LLMRuntimeTests('runTest')
        set_up_borrowed(self, runtime)
        draft = {'kind': 'PLAN', 'implementationPlan': [{'taskId': 'TASK-TODO', 'title': '할 일 앱',
            'description': '요청한 UI와 동작 구현', 'requirementIds': [str(i) for i in f.scenario.requirement_ids],
            'dependsOn': []}], 'questions': [], 'unapprovedAssumptions': []}
        provider = runtime_fixture.FakeProvider(runtime_fixture.text_response(json.dumps(draft, ensure_ascii=False)))
        result = await LLMEngine(role=SingleAgentIdentity.SINGLE_AGENT, provider=provider,
            single_phase=SinglePhase.PLANNING).run(prompt=prompt, model=runtime.model, output=draft_contract.output,
            budget=runtime.budget(), workspace_id=runtime.workspace_id)
        self.assertEqual(result.data, draft)
        envelope = json.loads(prompt.input_json)
        self.assertEqual(envelope['trusted']['scenarioContract'], contract)
        self.assertEqual(envelope['taskInput']['request'], request)

    def test_api_only_completion_uses_existing_safe_source_repair_route(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'main.py').write_text('from fastapi import FastAPI\napp=FastAPI()\n@app.get("/")\ndef home():\n return {"status":"ok"}\n')
            check = development_check(lambda data: SimpleNamespace(kind=data['kind']),
                service_interface_check=service_interface_check(SimpleNamespace(source_root=root), service_request_contract('할 일 앱')))
            rejection = checked_rejection(check({'kind': 'READY'}, ('write_source_file',)))
            self.assertEqual(rejection['code'], 'READY_SERVICE_INTERFACE_REVIEW')
            self.assertEqual(rejection['issues'][0]['sourcePath'], 'main.py')
            route = correction_route(rejection['code'], sequence=2, operation_sequence=1, allowed=True, reason='ALLOWED')
            self.assertEqual((route.action, route.category), ('REGENERATE', 'MODEL_EXECUTION'))
