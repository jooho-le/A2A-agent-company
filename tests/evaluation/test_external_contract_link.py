"""연결 규격 세 곳(숨긴 시험지 연결부, 평가 쪽 CONNECTOR, dev 시나리오 계약)이 서로 같은지 확인한다.

하나라도 다르면 에이전트가 다른 이름·경로로 생성물을 만들고, 시험 9개가 연결 단계에서 전부 미검증이 된다.
dev 코드는 읽기만 한다.
"""
import copy
import importlib.util
from pathlib import Path

import pytest

from evaluation import protected_tests as pt
from evaluation.experiment import ExperimentError, _check_contract
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario

LINK = Path(pt.__file__).parent / 'tests/signup/signup_link.py'
SKELETON = Path(__file__).resolve().parents[2] / 'examples/starting-source/scn001-signup'


def _contract():
    return copy.deepcopy(get_scenario(SCN_001_ID).planner_contract())


def _link():
    # 연결부는 표준 라이브러리만 미리 불러오므로 평가 환경에서 그대로 읽을 수 있다.
    spec = importlib.util.spec_from_file_location('_signup_link_for_contract_test', LINK)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_connector_matches_the_hidden_suite_link():
    link, text = _link(), LINK.read_text(encoding='utf-8')
    assert pt.CONNECTOR['app'] == link.APP
    assert pt.CONNECTOR['database'] == link.DB_DEPENDENCY
    assert pt.CONNECTOR['userModel'] == link.USER_MODEL
    assert pt.CONNECTOR['columns'] == (link.EMAIL_FIELD, link.HASH_FIELD)
    assert pt.CONNECTOR['signup'] == ('POST', link.SIGNUP_PATH) and '.post(SIGNUP_PATH' in text
    assert pt.CONNECTOR['bodyFields'] == tuple(link.signup_body('a@example.com', 'x'))
    assert f'os.environ["{pt.CONNECTOR["urlEnv"]}"] = url' in text


def test_dev_contract_matches_the_connector():
    contract = _contract()
    pt.check_external_contract(contract)
    pt.check_policy(contract)
    _check_contract(contract)


@pytest.mark.parametrize('section, field, value', [
    ('signupApi', 'path', '/signup'), ('signupApi', 'method', 'PUT'), ('app', 'module', 'app'),
    ('database', 'dependency', 'get_session'), ('userModel', 'columns', ['email', 'hashed_password']),
    ('signupApi', 'bodyFields', ['username', 'password']), ('database', 'urlEnv', 'DB_URL'),
])
def test_mismatched_contract_is_rejected_before_grading(section, field, value):
    contract = _contract()
    contract['externalContract'][section][field] = value
    with pytest.raises(ValueError):
        pt.check_external_contract(contract)
    with pytest.raises(ExperimentError, match='CONTRACT_NOT_SUPPORTED'):
        _check_contract(contract)


def test_malformed_contract_is_rejected():
    contract = _contract()
    del contract['externalContract']['signupApi']
    with pytest.raises(ValueError):
        pt.check_external_contract(contract)


def test_contract_frozen_before_the_field_is_still_accepted():
    contract = {key: value for key, value in _contract().items() if key != 'externalContract'}
    pt.check_policy(contract)
    _check_contract(contract)


def test_starting_skeleton_has_the_contract_files():
    # requirements.lock은 시작 저장소를 만들 때 실행 이미지와 같은 잠금 파일로 넣는다.
    names = [name for name in _contract()['externalContract']['startingFiles'] if name != 'requirements.lock']
    assert names and all((SKELETON / name).is_file() for name in names)
