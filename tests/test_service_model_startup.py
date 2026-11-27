"""Recorded missing-key startup regression; never execute generated product code."""
from types import SimpleNamespace
import json

import pytest
from sqlalchemy.exc import ArgumentError, InvalidRequestError

from agents.llm.corrections import feedback
from agents.roles.planner_contract import PlannerContractError, build_planner_output_contract, validate_planner_decision
from agents.runtime.service_interface import service_interface_check
from agents.runtime.start_precheck import start_precheck_for
from orchestrator.application.planner_output import PlannerPlan
from orchestrator.domain.scenario_registry import ScenarioDefinition, service_request_contract
from orchestrator.domain.service_design import validate_service_plan
from orchestrator.sandbox.service_start_contract import service_build_argv, service_build_diagnostics, service_start_failure_code
from service_test_helpers import service_design


DATABASE = '''from sqlalchemy.orm import DeclarativeBase
class Base(DeclarativeBase):
    pass
def get_db():
    yield None
'''
MODEL = '''from sqlalchemy import Column, String, Integer
from sqlalchemy.orm import Mapped
from database import Base
class User(Base):
    __tablename__ = "users"
    email: Mapped[str] = Column(String, unique=True, nullable=False)
    password_hash: Mapped[str] = Column(String, nullable=False)
'''


@pytest.mark.parametrize('extra',['','from database import engine\n','def create_tables():\n    Base.metadata.create_all(bind=engine)\n'])
def test_missing_primary_key_is_reported_before_ready_even_after_the_recorded_wrong_repairs(tmp_path,extra):
    (tmp_path/'main.py').write_text('import models\n')
    (tmp_path/'database.py').write_text(DATABASE)
    (tmp_path/'models.py').write_text(MODEL+extra)
    contract=service_request_contract('이메일과 비밀번호로 회원가입 API 만들어줘.')
    checkpoint=SimpleNamespace(source_root=tmp_path)
    issues=service_interface_check(checkpoint,contract,service_design('API','/signup',['email','password']))()
    keys=[item for item in issues if item['reasonCode']=='MODEL_PRIMARY_KEY_MISSING']
    assert keys and keys[0]['sourcePath']=='models.py' and keys[0]['lineNumber']==4
    notice=feedback('READY_SERVICE_INTERFACE_REVIEW',issues=keys,remaining=1)['hostCorrection']
    assert 'primary key' in notice['instruction'] and 'create_tables()' in notice['instruction']
    assert 'Implement the requested usable local HTML' not in notice['instruction']
    (tmp_path/'models.py').write_text(MODEL+'    id = Column(Integer, primary_key=True)\n')
    assert not service_interface_check(checkpoint,contract,service_design())()


@pytest.mark.parametrize('marker',['__mapper_args__ = {}','__table__ = None','__abstract__ = True',
                                  'id = Column(Integer, primary_key=True)'])
def test_uninspected_dynamic_or_declared_mappings_are_left_for_sandbox(tmp_path,marker):
    (tmp_path/'main.py').write_text('import models\n')
    (tmp_path/'database.py').write_text(DATABASE)
    (tmp_path/'models.py').write_text(MODEL+'    '+marker+'\n')
    contract=service_request_contract('API 만들어줘')
    assert not any(item['reasonCode']=='MODEL_PRIMARY_KEY_MISSING' for item in start_precheck_for(
        SimpleNamespace(source_root=tmp_path),contract)())


def test_custom_non_orm_base_is_not_guessed_to_need_a_primary_key(tmp_path):
    (tmp_path/'main.py').write_text('import models\n')
    (tmp_path/'database.py').write_text('class Base: pass\ndef get_db(): return None\n')
    (tmp_path/'models.py').write_text(MODEL)
    issues=start_precheck_for(SimpleNamespace(source_root=tmp_path),service_request_contract('API 만들어줘'))()
    assert not any(item['reasonCode']=='MODEL_PRIMARY_KEY_MISSING' for item in issues)


def test_unused_or_conditionally_imported_model_is_left_to_runtime(tmp_path):
    (tmp_path/'database.py').write_text(DATABASE)
    (tmp_path/'models.py').write_text(MODEL)
    for entry in ('# no model import\n','if False:\n    import models\n'):
        (tmp_path/'main.py').write_text(entry)
        issues=start_precheck_for(SimpleNamespace(source_root=tmp_path),service_request_contract('API 만들어줘'))()
        assert not any(item['reasonCode']=='MODEL_PRIMARY_KEY_MISSING' for item in issues)


def test_startup_error_classification_and_published_diagnostic_do_not_echo_private_exception_text():
    error=ArgumentError("Mapper User could not assemble any primary key columns for mapped table 'private-value'")
    code=service_start_failure_code(error,'SERVICE_START_FAILED')
    assert code=='SERVICE_MODEL_PRIMARY_KEY_REQUIRED'
    assert service_start_failure_code(InvalidRequestError('private-value'),'SERVICE_START_FAILED')=='SERVICE_MODEL_DEFINITION_INVALID'
    assert service_start_failure_code(ValueError('private-value'),'SERVICE_ENDPOINT_MISSING')=='SERVICE_ENDPOINT_MISSING'
    design=service_design();argv=service_build_argv(design)
    assert len(argv[-2].encode())<8192
    stdout='SERVICE_START:'+json.dumps({'code':code,'path':'models.py','line':4,'missing':None})
    result=service_build_diagnostics(argv=argv,exit_code=1,stdout=stdout,design=design,source_files={'models.py':MODEL.encode()})
    assert result[0].rule_id==code and result[0].normalized_location=='models.py:4'
    assert 'private-value' not in result[0].model_dump_json()


def test_explicit_api_request_cannot_be_published_as_a_web_plan():
    contract=service_request_contract('이메일과 비밀번호로 회원가입 API 만들어줘.')
    scenario=ScenarioDefinition.from_contract(contract)
    schema=build_planner_output_contract(scenario).schema
    schema.require_openai_strict()
    assert schema.to_dict()['properties']['serviceDesign']['anyOf'][0]['properties']['delivery']['enum']==['API']
    data={'kind':'PLAN','implementationPlan':[{'taskId':'TASK-SIGNUP','title':'가입 API',
        'description':'원문 API를 구현하고 검사한다.','requirementIds':[r['requirementId'] for r in contract['requirements']],
        'dependsOn':[]}],'questions':[],'unapprovedAssumptions':[],'serviceDesign':service_design('WEB')}
    with pytest.raises(PlannerContractError):validate_planner_decision(data,scenario)
    data['serviceDesign']=service_design('API')
    plan=validate_planner_decision(data,scenario).plan
    validate_service_plan(contract,plan)
    data['serviceDesign']['endpoints'][0]['responseFormat']='HTML'
    plan=PlannerPlan.model_validate({**plan.model_dump(by_alias=True),'serviceDesign':data['serviceDesign']})
    with pytest.raises(ValueError,match='API_DELIVERY'):validate_service_plan(contract,plan)


def test_explicit_request_for_both_screen_and_api_keeps_web_choice():
    scenario=ScenarioDefinition.from_contract(service_request_contract('회원가입 API와 웹 화면을 같이 만들어줘.'))
    schema=build_planner_output_contract(scenario).schema.to_dict()
    assert schema['properties']['serviceDesign']['anyOf'][0]['properties']['delivery']['enum']==['API','WEB']


@pytest.mark.parametrize('user_text',['API는 필요 없고 기능을 설명해줘.','API 만들지 말고 설계 설명만 해줘.'])
def test_negated_or_non_build_api_mentions_do_not_force_an_api_product(user_text):
    scenario=ScenarioDefinition.from_contract(service_request_contract(user_text))
    schema=build_planner_output_contract(scenario).schema.to_dict()
    assert schema['properties']['serviceDesign']['anyOf'][0]['properties']['delivery']['enum']==['API','WEB']


def test_dependency_feedback_does_not_invent_a_missing_web_screen():
    notice=feedback('READY_SERVICE_INTERFACE_REVIEW',issues=[{'fieldPath':'/source/imports/jinja2',
        'reasonCode':'SERVICE_DEPENDENCY_UNAVAILABLE'}],remaining=1)['hostCorrection']
    assert 'runtimeCapabilities' in notice['instruction']
    assert 'submitted GET / only returns JSON' not in notice['instruction']
