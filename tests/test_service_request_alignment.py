"""Product request alignment on real admission/contracts, without network or Docker.

These regressions do not claim generated product quality. They prevent the
observed todo→signup substitution, invented UI facts and lost failure causes.
"""
import asyncio
from hashlib import sha256
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from a2a.types import Task
from google.protobuf.json_format import ParseDict

from agents.roles.prompts import prepare_role_prompt
from agents.runtime.planner_context import _scenario, PlannerContextError
from agents.runtime.requirement_scope import build_requirement_scope, signup_observation_plan
from agents.runtime.service_interface import inspect_service_interface, service_source_facts
from agents.runtime.test_guidance import unit_test_targets
from agents.single.prompts import build_single_system_prompt
from orchestrator.a2a.requests import A2AWorkflowMetadata
from orchestrator.api.schemas.runs import WorkflowStepResponse
from orchestrator.application.a2a_tasks import _apply_task
from orchestrator.application.control_input import validate_control_input
from orchestrator.core.config import Settings
from orchestrator.domain.models import AgentContext, WorkflowStep
from orchestrator.domain.planning_artifacts import RequirementArtifact
from orchestrator.domain.requirement_authority import freeze_requirement_authority
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import (
    SCN_001_ID, SERVICE_WEB_ID, RequirementValidator, ScenarioDefinition,
    get_scenario, service_request_contract,
)
from orchestrator.domain.states import AgentRole
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.main import create_app


def configuration(text, *, contract=None):
    return RunConfigurationArtifact(run_id=uuid4(), workspace_id=uuid4(), scenario_id=SERVICE_WEB_ID,
        frozen_scenario_contract_json=json.dumps(contract or service_request_contract(text), ensure_ascii=False))


@pytest.fixture
def host(tmp_path):
    repository = SQLiteWorkflowRepository(tmp_path / 'host.sqlite3')
    app = create_app(repository, settings=Settings(_env_file=None, database_path=str(repository.database_path)))
    def request(method, path, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                return await client.request(method, path, **kwargs)
        return asyncio.run(send())
    return repository, request


@pytest.mark.parametrize('text', [
    '할 일 관리웹사이트\n만들어줘.',
    '여러 명이 글과 댓글을 작성하는 블로그를 만들어줘. 로그인은 필요 없어.',
    '예약 날짜를 선택하고 취소할 수 있는 화면을 만들어줘.',
])
def test_admission_freezes_the_user_application_and_scope_reaches_every_role(host, text):
    repository, request = host
    response = request('POST', '/api/v1/runs', json={'scenarioId': str(SERVICE_WEB_ID), 'requestText': text})
    assert response.status_code == 201
    run_id = UUID(response.json()['run']['runId'])
    saved = repository.get_run_configuration(run_id)
    contract = saved.scenario_contract
    scenario = _scenario(saved, request_text=text)
    assert contract['externalContract']['userRequest'] == text
    assert contract['externalContract']['requestIdentity'] == sha256(text.encode()).hexdigest()
    assert contract['requirements'][0]['acceptanceCriteria'] == ['요청사항 구현: ' + text]
    assert 'signupApi' not in contract['externalContract']
    assert 'userModel' not in contract['externalContract']
    assert not contract['excludedFeatures']
    assert all(UUID(r['requirementId']).version == 4 for r in contract['requirements'])
    assert signup_observation_plan(contract) == {}
    assert ScenarioDefinition.from_contract(contract).planner_contract() == contract
    authority = freeze_requirement_authority(saved)
    artifact_id, planner_step = uuid4(), uuid4()
    from service_test_helpers import service_design
    payload = {'schemaVersion': 1, 'serviceDesign': service_design('WEB'), 'requirements': [{k: r[k] for k in
        ('requirementId', 'key', 'description', 'acceptanceCriteria')} for r in contract['requirements']],
        'implementationPlan': [{'taskId': 'TASK-WEB', 'title': '요청 웹사이트 구현',
            'description': '원문 기능과 실제 화면 구현', 'requirementIds': [str(i) for i in scenario.requirement_ids], 'dependsOn': []}]}
    artifact = RequirementArtifact(artifact_id=artifact_id, run_id=run_id, workflow_step_id=planner_step,
        a2a_task_id='planner', a2a_artifact_id='plan', requirement_ids=scenario.requirement_ids,
        artifact_uri=f'artifact://{artifact_id}/requirements.json', payload=payload)
    for role, validator in ((AgentRole.DEVELOPER, None), (AgentRole.QA, RequirementValidator.QA),
                            (AgentRole.SECURITY, RequirementValidator.SECURITY)):
        ids = scenario.requirement_ids if validator is None else scenario.requirement_ids_for(validator)
        metadata = A2AWorkflowMetadata(run_id=run_id, workflow_step_id=uuid4(), scenario_id=SERVICE_WEB_ID,
                                       attempt=0, requirement_ids=ids)
        scope = build_requirement_scope(saved, artifact, metadata, role)
        assert scope.authority == authority
        assert tuple(r.requirement_id for r in scope.requirements) == ids
        assert all(r.acceptance_criteria == next(x.acceptance_criteria for x in authority.requirements
                                                if x.requirement_id == r.requirement_id) for r in scope.requirements)


def test_distinct_runs_never_reuse_mutable_requirement_identity():
    first, second = (service_request_contract('할 일 목록을 만들어줘.') for _ in range(2))
    assert first['externalContract']['requestIdentity'] == second['externalContract']['requestIdentity']
    assert not {r['requirementId'] for r in first['requirements']} & {r['requirementId'] for r in second['requirements']}


def test_long_unicode_scope_survives_context_bounds_without_losing_request():
    text = ('할 일 📋 추가·완료·삭제.\n' * 700).strip()
    saved = configuration(text)
    _scenario(saved, request_text=text)
    assert saved.scenario_contract['externalContract']['userRequest'] == text
    assert all(len(c.encode()) <= 4096 for r in saved.scenario_contract['requirements'] for c in r['acceptanceCriteria'])


@pytest.mark.parametrize('mutation', ['request', 'digest', 'unbound-template'])
def test_a_different_request_or_unbound_template_is_not_a_valid_service_context(mutation):
    text = '할 일 목록을 만들어줘.'
    contract = service_request_contract(text)
    if mutation == 'request':
        contract['externalContract']['userRequest'] = '회원가입을 만들어줘.'
    elif mutation == 'digest':
        contract['externalContract']['requestIdentity'] = '0' * 64
    else:
        contract = get_scenario(SERVICE_WEB_ID).planner_contract()
    with pytest.raises(PlannerContextError):
        _scenario(configuration(text, contract=contract), request_text=text)


@pytest.mark.parametrize('text', ['x' * 20001, '할 일\x00목록'])
def test_invalid_service_request_is_rejected_before_any_run_is_saved(host, text):
    repository, request = host
    response = request('POST', '/api/v1/runs', json={'scenarioId': str(SERVICE_WEB_ID), 'requestText': text})
    assert response.status_code == 422
    with repository._connection() as connection:
        assert connection.execute('SELECT count(*) FROM workflow_runs').fetchone()[0] == 0


def test_registered_signup_experiment_retains_its_original_frozen_contract(host):
    repository, request = host
    expected = get_scenario(SCN_001_ID).planner_contract()
    response = request('POST', '/api/v1/runs', json={'scenarioId': str(SCN_001_ID), 'requestText': '회원가입 기능을 만들어줘.'})
    assert response.status_code == 201
    saved = repository.get_run_configuration(UUID(response.json()['run']['runId']))
    assert saved.scenario_contract == expected
    assert len(expected['requirements']) == 8
    assert expected['externalContract']['signupApi']['path'] == '/api/auth/signup'


def test_service_cannot_mislabel_a_protected_experiment_configuration(host):
    _, request = host
    response = request('POST', '/api/v1/runs', json={'scenarioId': str(SERVICE_WEB_ID), 'requestText': '목록 웹앱',
        'configuration': {'protectedTestSuiteRef': 'artifact://protected/suite'}})
    assert response.status_code == 422
    assert response.json()['detail'] == 'SERVICE_REQUEST_REQUIRES_NON_EXPERIMENT_CONFIGURATION'


@pytest.mark.parametrize('role', list(AgentRole))
def test_all_role_prompts_use_request_stack_instead_of_signup_fixture(role):
    # Orchestrator is not an LLM role in this enum.
    text = '할 일 앱을 만들어줘.'
    metadata = A2AWorkflowMetadata(run_id=uuid4(), workflow_step_id=uuid4(), scenario_id=SERVICE_WEB_ID, attempt=0)
    prompt = prepare_role_prompt(role, task_input={'serviceContract': service_request_contract(text),
        'runtimeInstructions': 'old signup-only fixture instructions'}, metadata=metadata)
    envelope = json.loads(prompt.input_json)
    assert envelope['taskInput']['serviceContract']['externalContract']['userRequest'] == text
    assert 'old signup-only' not in prompt.input_json
    assert 'SCN-001-only' in envelope['taskInput']['runtimeInstructions']
    assert '실제 Source' in prompt.system_prompt
    assert text not in prompt.system_prompt
    assert 'SCN-001 전용' in build_single_system_prompt(service_request=True)


def test_general_test_targets_preserve_mandatory_paths_without_requiring_user_model():
    unit = SimpleNamespace(scopes=(SimpleNamespace(name='generated', pattern='test_*.py', source_directory='tests', kind='QA_TESTS'),))
    for role in ('qa', 'security'):
        target = unit_test_targets(unit, role, scenario_key='SERVICE-WEB')[0]
        assert target['required'] is True
        assert target['directory'] == f'outputs/{role}/tests'
        assert 'appFixture' not in target and 'signupEmailEvidence' not in target
        assert 'TestClient' in target['fixtureGuidance']


def test_service_security_retains_anchor_and_pending_review_protocol():
    from agents.runtime.service_scope import service_runtime_instructions
    instructions = service_runtime_instructions({'sourceReferenceCatalog': {'anchors': {}}})
    assert 'anchorId' in instructions
    assert 'HOST_TESTS_PENDING' in instructions
    assert 'UNVERIFIED' in instructions
    assert 'measuredSecurity.profiles[].reportRef' in instructions


def test_static_interface_preflight_flags_api_only_source_but_never_claims_ui_success(tmp_path):
    contract = service_request_contract('할 일 앱')
    main = tmp_path / 'main.py'
    main.write_text('from fastapi import FastAPI\napp=FastAPI()\n@app.get("/")\ndef home():\n return {"status":"ok"}\n')
    issues = inspect_service_interface(tmp_path, contract)
    assert issues == [{'fieldPath': '/source', 'reasonCode': 'WEB_UI_NOT_IMPLEMENTED', 'sourcePath': 'main.py', 'lineNumber': 4}]
    assert inspect_service_interface(tmp_path, get_scenario(SCN_001_ID).planner_contract()) == []
    (tmp_path / 'index.html').write_text('<main id="tasks"></main>')
    assert inspect_service_interface(tmp_path, contract) == []  # deferred to actual browser execution


def test_static_inventory_uses_actual_source_selectors_without_values_or_execution():
    facts = service_source_facts({'main.py': b'from missing import app\n@app.get("/")\ndef home():\n return \'<input name="todo" value="private-value">\'\n',
        'static/index.html': b'<button id="add-task">Add</button>', 'tasks.js': b'throw Error("never run")'})
    assert {'path': 'main.py', 'selector': 'input[name="todo"]'} in facts['staticSelectors']
    assert {'path': 'static/index.html', 'selector': '#add-task'} in facts['staticSelectors']
    assert facts['isLiveDOM'] is False and not facts['truncated']
    assert 'email' not in json.dumps(facts) and 'private-value' not in json.dumps(facts)


@pytest.mark.parametrize('code,expected', [('LLM_VALIDATION_DRAFT_EXHAUSTED', 'LLM_VALIDATION_DRAFT_EXHAUSTED'),
                                         ('private-provider-value', 'AGENT_EXECUTION_FAILED')])
def test_transport_failure_has_a_safe_reason_in_the_step_api(code, expected):
    run_id = uuid4()
    step = WorkflowStep(run_id=run_id, agent_role=AgentRole.QA)
    metadata = A2AWorkflowMetadata(run_id=run_id, workflow_step_id=step.workflow_step_id, scenario_id=SCN_001_ID, attempt=0)
    context = AgentContext(run_id=run_id, agent_id='qa')
    task = ParseDict({'id': 'qa-task', 'contextId': 'qa-context', 'status': {'state': 'TASK_STATE_FAILED',
        'message': {'messageId': 'failure-message', 'role': 'ROLE_AGENT', 'parts': [{'data': {'code': code}}]}}}, Task())
    updated, _, _ = _apply_task(step, context, task, previous_state=None, expected_metadata=metadata)
    assert updated.failure_code == expected
    public = WorkflowStepResponse.from_step(updated).model_dump(by_alias=True)
    assert public['failureCode'] == expected
    assert 'private-provider-value' not in json.dumps(public, default=str)


@pytest.mark.parametrize('field', ['serviceContract', 'sourceInterface'])
def test_clarifications_cannot_replace_host_facts(field):
    with pytest.raises(ValueError):
        validate_control_input({field: {'anything': 'replacement'}})
