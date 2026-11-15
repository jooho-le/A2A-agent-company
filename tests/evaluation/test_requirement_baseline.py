"""시험지가 전제로 하는 요구사항 기준(점검 7)을 시험한다.

시험지는 SCN-001의 요구사항 문장·완료 조건으로 만들었다. 동결 계약의 요구사항이 바뀌거나(예: 최소 길이 8→12자)
늘거나 줄면 이 시험지로는 맞게 판정할 수 없으므로, 설정·채점·보호 케이스 생성이 모두 거부해야 한다.
dev 코드는 읽기만 한다.
"""
import copy
import json
from uuid import uuid4

import pytest

from evaluation import protected_tests as pt
from evaluation.experiment import ExperimentError, _check_contract
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario


def _contract():
    return copy.deepcopy(get_scenario(SCN_001_ID).planner_contract())


def _requirement(contract, key):
    return next(item for item in contract['requirements'] if item['key'] == key)


def test_pinned_baseline_equals_the_dev_scenario():
    assert pt.load_suite()['requirements'] == {
        item['key']: {'description': item['description'], 'acceptanceCriteria': item['acceptanceCriteria'],
                      'validators': item['validators']} for item in _contract()['requirements']}
    pt.check_requirements(_contract())


CHANGES = {
    'criterion_changed': lambda c: _requirement(c, 'REQ-004').update(
        acceptanceCriteria=['11자 이하는 거부, 12자 이상 정상 처리']),
    'description_changed': lambda c: _requirement(c, 'REQ-004').update(description='비밀번호는 최소 12자 이상이어야 함'),
    'validator_changed': lambda c: _requirement(c, 'REQ-005').update(validators=['QA']),
    'requirement_added': lambda c: c['requirements'].append(
        dict(_requirement(c, 'REQ-001'), key='REQ-009', requirementId=str(uuid4()), description='프로필 사진을 올릴 수 있어야 함')),
    'requirement_removed': lambda c: c['requirements'].remove(_requirement(c, 'REQ-008')),
}


@pytest.mark.parametrize('name', sorted(CHANGES))
def test_changed_requirements_are_refused_before_grading(name):
    contract = _contract()
    CHANGES[name](contract)
    with pytest.raises(ValueError):
        pt.check_requirements(contract)
    with pytest.raises(ExperimentError, match='CONTRACT_NOT_SUPPORTED'):
        _check_contract(contract)  # configure·score의 계약 확인
    with pytest.raises(ValueError):
        pt.evaluation_inputs(contract, 'final')  # 최종 채점 입력
    with pytest.raises(ValueError):
        pt.owned_cases(contract, 'QA')  # Ver1 순환 안 보호 케이스


def test_duplicate_requirement_keys_are_refused():
    contract = _contract()
    contract['requirements'].append(dict(_requirement(contract, 'REQ-001'), requirementId=str(uuid4())))
    with pytest.raises(ValueError):
        pt.check_requirements(contract)


def test_fingerprint_covers_the_pinned_baseline():
    suite = pt.load_suite()
    changed = copy.deepcopy(suite)
    changed['requirements']['REQ-004']['acceptanceCriteria'] = ['11자 이하는 거부, 12자 이상 정상 처리']
    assert pt.suite_sha256('final', changed) != pt.suite_sha256('final', suite)


@pytest.mark.parametrize('mutate', [
    lambda data: data.pop('requirements'),
    lambda data: data['requirements'].pop('REQ-004'),  # 검사가 가리키는 요구사항이 기준에 없음
    lambda data: data['requirements']['REQ-001'].update(acceptanceCriteria=[]),
])
def test_suite_without_a_complete_baseline_is_refused(tmp_path, mutate):
    data = json.loads(pt.SUITE_FILE.read_text(encoding='utf-8'))
    mutate(data)
    path = tmp_path / 'suite.json'
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError):
        pt.load_suite(path)
