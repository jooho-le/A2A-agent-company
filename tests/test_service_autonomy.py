"""Offline service contracts and startup recipe; never run Source or Docker."""
import ast
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.roles.planner_contract import build_planner_output_contract, validate_planner_decision, PlannerContractError
from agents.runtime.service_capabilities import runtime_capabilities, inspect_dependencies
from agents.runtime.service_interface import service_interface_check
from agents.runtime.service_scope import service_input
from agents.runtime.test_fix import TestFixRequired, available_report_tools
from agents.platform.composition import PlatformConfigurationError
from mcp_tools.runtime import MCPBinding
from mcp_tools.tools.build_config import encode_build_configuration
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain import AgentRole, SCENARIO_REGISTRY, SCN_001_ID
from orchestrator.domain.scenario_registry import service_request_contract, ScenarioDefinition
from orchestrator.domain.service_design import ServiceDesign, validate_service_plan
from orchestrator.sandbox.service_start_contract import service_build_argv, service_build_diagnostics, service_interface_failure
from service_test_helpers import service_design
import unittest
import test_host_services as host_fixtures


def test_prepared_natural_requests_bind_to_service_policy_not_experiment():
    fixture = json.loads((Path(__file__).resolve().parents[1] / 'configs/service-validation-cases.json').read_text())
    assert [case['expectedDelivery'] for case in fixture['cases']] == ['API', 'WEB', 'WEB']
    for case in fixture['cases']:
        contract = service_request_contract(case['request'])
        assert contract['externalContract']['servicePolicy'] == 'SERVICE_AUTONOMY_V2'
        assert contract['externalContract']['userRequest'] == case['request']
        assert contract['scenarioKey'] == 'SERVICE-WEB'
        assert case['checks']


def test_shared_implementation_decisions_are_also_semantically_reviewed():
    from agents.semantic_review import draft_fields
    draft = {'summary': '원래 요청 구현', 'serviceDesign': service_design()}
    fields = draft_fields(draft)
    assert fields['/serviceDesign/delivery'] == 'API'
    assert fields['/serviceDesign/endpoints/1/bodyFormat'] == 'JSON'
    assert fields['/serviceDesign/endpoints/1/requestFields/0'] == 'title'
    assert fields['/serviceDesign/dataModel']
    assert not any(pointer.endswith('/successStatuses/0') for pointer in fields)
    assert draft_fields({'summary': '기존 실험 응답'}) == {'/summary': '기존 실험 응답'}


def decision(contract, delivery='API'):
    return {'kind': 'PLAN', 'implementationPlan': [{'taskId': 'TASK-MAKE', 'title': '요청 구현',
        'description': '원문 범위와 공유 인터페이스를 구현하고 검사한다.',
        'requirementIds': [r['requirementId'] for r in contract['requirements']], 'dependsOn': []}],
        'questions': [], 'unapprovedAssumptions': [], 'serviceDesign': service_design(delivery)}


@pytest.mark.parametrize('user_text,delivery,path,fields', [('회원가입 API 만들어', 'API', '/signup', ['email','password']),
    ('할 일 관리 사이트 만들어', 'WEB', '/items', ['title']), ('글과 댓글을 작성하는 블로그 만들어', 'WEB', '/posts', ['title','body'])])
def test_short_requests_preserve_user_scope_and_share_one_design(user_text, delivery, path, fields):
    contract = service_request_contract(user_text)
    scenario = ScenarioDefinition.from_contract(contract)
    draft = decision(contract, delivery)
    draft['serviceDesign'] = service_design(delivery,path,fields)
    build_planner_output_contract(scenario).schema.require_openai_strict()
    plan = validate_planner_decision(draft, scenario).plan
    assert contract['externalContract']['userRequest'] == user_text
    assert plan.service_design.delivery == delivery
    original = deepcopy(contract)
    for role in (AgentRole.DEVELOPER, AgentRole.QA, AgentRole.SECURITY):
        execution = SimpleNamespace(configuration=SimpleNamespace(scenario_contract=contract),
                                    requirement_artifact=SimpleNamespace(payload=plan.model_dump(mode='json', by_alias=True)))
        bound = service_input(execution)
        assert bound['serviceDesign'] == draft['serviceDesign']
        assert bound['serviceContract'] == original
    assert contract == original


def test_service_design_is_required_but_experiment_format_stays_unchanged():
    contract = service_request_contract('회원가입 API 만들어')
    scenario = ScenarioDefinition.from_contract(contract)
    draft = decision(contract)
    draft.pop('serviceDesign')
    with pytest.raises(PlannerContractError): validate_planner_decision(draft, scenario)
    fixed = SCENARIO_REGISTRY[SCN_001_ID]
    schema = build_planner_output_contract(fixed).schema.to_dict()
    assert 'serviceDesign' not in schema['properties']
    plan = validate_planner_decision({k: v for k, v in decision(fixed.planner_contract()).items()
                                      if k != 'serviceDesign'}, fixed).plan
    assert 'serviceDesign' not in plan.model_dump(by_alias=True)
    plan.service_design = ServiceDesign.model_validate(service_design())
    with pytest.raises(ValueError): validate_service_plan(fixed.planner_contract(), plan)


def test_api_only_ready_source_is_not_forced_to_add_a_page(tmp_path):
    (tmp_path/'main.py').write_text('def ready():\n    return {"ready": True}\n')
    checkpoint = SimpleNamespace(source_root=tmp_path)
    contract = service_request_contract('API만 만들어')
    assert service_interface_check(checkpoint, contract, service_design())() == []


def test_capability_guard_catches_the_recorded_missing_dependencies(tmp_path):
    lock = tmp_path/'requirements.lock'
    lock.write_text('fastapi==0.1\nargon2-cffi==25.1.0\nsqlalchemy==2.0.0\n')
    digest = 'sha256:'+sha256(lock.read_bytes()).hexdigest()
    caps = runtime_capabilities(lock, digest)
    imports = 'from fastapi import FastAPI, Form\nfrom fastapi.templating import Jinja2Templates\nfrom argon2 import PasswordHasher\n'
    (tmp_path/'main.py').write_text(imports)
    assert inspect_dependencies(tmp_path, caps) == []  # Lazy imports alone do not need the parsers.
    (tmp_path/'main.py').write_text(imports + 'templates = Jinja2Templates(directory="templates")\ndef signup(email=Form(...)): pass\n')
    errors = inspect_dependencies(tmp_path, caps)
    assert {e['fieldPath'].rsplit('/', 1)[-1] for e in errors} == {'multipart', 'jinja2'}
    assert all(e['sourcePath'] == 'main.py' for e in errors)
    assert caps['packages']['argon2-cffi'] == '25.1.0'
    lock.write_text('fastapi==0.1\njinja2==3.0\n')
    with pytest.raises(ValueError, match='CHANGED'): runtime_capabilities(lock, digest)


def openapi(encoding='application/json', fields=('title',)):
    return {'paths': {'/': {'get': {}}, '/items': {'post': {'requestBody': {'content': {
        encoding: {'schema': {'$ref': '#/components/schemas/Input'}}}}}}},
        'components': {'schemas': {'Input': {'properties': {name: {'type': 'string'} for name in fields}}}}}


def test_startup_gate_rejects_form_json_mismatch_missing_fields_routes_and_html():
    design = service_design()
    assert service_interface_failure(openapi(), design, 200, 'application/json') is None
    assert service_interface_failure(openapi('application/x-www-form-urlencoded'), design, 200, 'application/json') == 'SERVICE_REQUEST_ENCODING_MISMATCH'
    assert service_interface_failure(openapi(fields=()), design, 200, 'application/json') == 'SERVICE_REQUEST_FIELDS_MISMATCH'
    assert service_interface_failure({'paths': {}}, design, 200, 'application/json') == 'SERVICE_ENDPOINT_MISSING'
    assert service_interface_failure(openapi(), design, 500, 'application/json') == 'SERVICE_READY_FAILED'
    assert service_interface_failure(openapi(), service_design('WEB'), 200, 'application/json') == 'SERVICE_HTML_REQUIRED'


def test_probe_is_host_bound_data_and_only_closed_diagnostics_are_returned():
    design = service_design()
    argv = service_build_argv(design)
    ast.parse(argv[-2])
    assert json.loads(argv[-1]) == design
    assert 'TestClient' in argv[-2] and 'service_interface_failure' in argv[-2]
    assert 'DATABASE_URL' in argv[-2] and '/work/service-start.sqlite3' in argv[-2]
    assert '\n' not in argv[-2] and len(argv[-2].encode()) < 8192
    marker = 'SERVICE_START:'+json.dumps({'code': 'SERVICE_REQUEST_ENCODING_MISMATCH', 'path': 'main.py', 'line': 1, 'missing': None})
    diagnostics = service_build_diagnostics(argv=argv, exit_code=1, stdout=marker,
        design=design, source_files={'main.py': b'# actual source\n'})
    assert diagnostics[0].rule_id == 'SERVICE_REQUEST_ENCODING_MISMATCH'
    assert service_build_diagnostics(argv=(*argv[:-1], '{}'), exit_code=1, stdout=marker,
        design=design, source_files={'main.py': b'# actual source\n'}) == ()
    assert service_build_diagnostics(argv=argv, exit_code=1, stdout=marker.replace('main.py', '../outside.py'),
        design=design, source_files={'main.py': b'# source\n'}) == ()


def test_failed_scope_repair_does_not_offer_a_nonexistent_report():
    data = {'serviceContract': service_request_contract('API 만들어'), 'serviceDesign': service_design()}
    needed = TestFixRequired({'NOT_RUN_SCOPE_FAILED'}, ['qa-unit'])
    feedback = needed.task_input(data, {'kind': 'READY'})['testRunFeedback']
    assert feedback['availableReportRefs'] == [] and feedback['noReportAvailable']
    assert 'models.User' not in feedback['instruction']
    tools = tuple(SimpleNamespace(name=n) for n in ('read_project_file', 'write_test_file', 'read_test_report'))
    assert [t.name for t in available_report_tools(tools, data)] == ['read_project_file', 'write_test_file']
    assert available_report_tools(tools, {}) == tools


class ServiceHostRecipeTests(host_fixtures._HostFixture, unittest.TestCase):
    """Test recipes after the separately tested Host ownership/binding gate."""
    def configured(self, role, delivery):
        execution = self.execution(role)
        contract = service_request_contract('API 또는 웹 앱을 만들어줘')
        execution.configuration = execution.configuration.model_copy(update={
            'frozen_scenario_contract_json': json.dumps(contract,ensure_ascii=False)})
        plan = validate_planner_decision(decision(contract,delivery), ScenarioDefinition.from_contract(contract)).plan
        execution.requirement_artifact = SimpleNamespace(payload=plan.model_dump(mode='json',by_alias=True))
        host = self.host()
        binding = MCPBinding(role=role,agent_role=role,run_id=execution.metadata.run_id,
                             workspace_id=execution.configuration.workspace_id)
        return host,execution,binding

    def test_only_api_service_skips_browser_and_build_recipe_is_startup_not_compile_only(self):
        host,execution,binding = self.configured(AgentRole.QA,'API')
        with patch.object(host,'_binding',return_value=binding):
            self.assertIsNone(host._configuration(execution,AgentRole.QA).browser_test_configuration)
        host,execution,binding = self.configured(AgentRole.QA,'WEB')
        with patch.object(host,'_binding',return_value=binding):
            self.assertIsNotNone(host._configuration(execution,AgentRole.QA).browser_test_configuration)
        host,execution,binding = self.configured(AgentRole.DEVELOPER,'API')
        with patch.object(host,'_binding',return_value=binding):
            build = host._configuration(execution,AgentRole.DEVELOPER).build_configuration
        self.assertEqual(build.profile.name,'service-start')
        self.assertIn('TestClient',build.profile.argv[-2])
        self.assertIn('get_db',build.profile.argv[-2])
        self.assertTrue(encode_build_configuration(build))

    def test_service_does_not_silently_consume_an_experimental_protected_suite(self):
        host,execution,binding = self.configured(AgentRole.QA,'API')
        host._protected_cases = (object(),)
        with self.assertRaises(PlatformConfigurationError):
            host.require_scenario(execution.configuration.scenario_contract)
        with patch.object(host,'_binding',return_value=binding):
            with self.assertRaises(PlatformConfigurationError):
                host._configuration(execution,AgentRole.QA)

    def test_experiment_keeps_its_approved_profile_and_browser(self):
        host = self.host()
        execution = self.execution(AgentRole.DEVELOPER)
        self.assertEqual(host._configuration(execution,AgentRole.DEVELOPER).build_configuration,
                         self.tools.build_configuration)
        execution = self.execution(AgentRole.QA)
        self.assertEqual(host._configuration(execution,AgentRole.QA).browser_test_configuration,
                         self.tools.browser_configuration)
