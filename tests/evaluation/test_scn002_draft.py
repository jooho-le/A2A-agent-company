"""전자문서(SCN-002) 시나리오 계약 초안이 dev 등록 형식과 승인 원장 규칙을 지키는지 시험한다. dev 코드는 읽기만 한다.

확인하는 것:
  - dev ScenarioDefinition.from_contract가 초안을 받고, 다시 꺼낸 Planner 계약이 초안과 같은가?
  - Run 설정에 동결했을 때 dev 승인 원장(freeze_requirement_authority)이 받는가(REQ-번호 형식·UUIDv4·중복 없음)?
  - 요구사항마다 완료 조건 문장이 하나이고, 검증 담당이 시나리오 문서와 같은가(F→QA, S→SECURITY, 추적→ORCHESTRATOR)?
  - 회원가입(SCN-001)의 비밀번호·이메일 정책이 기본값으로 섞여 들어가지 않는가?
"""
import json
from pathlib import Path
from uuid import UUID, uuid4

from orchestrator.domain.requirement_authority import freeze_requirement_authority
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.scenario_registry import ScenarioDefinition

DRAFT = Path(__file__).resolve().parents[2] / 'examples/evaluation/scn002-edocument.contract.draft.json'
CONTRACT = json.loads(DRAFT.read_text(encoding='utf-8'))


def test_dev_scenario_definition_accepts_and_round_trips_the_draft():
    scenario = ScenarioDefinition.from_contract(CONTRACT)
    assert scenario.key == 'SCN-002' and len(scenario.requirements) == 13
    assert scenario.planner_contract() == CONTRACT


def test_dev_requirement_authority_accepts_the_frozen_draft():
    artifact = RunConfigurationArtifact(runId=uuid4(), scenarioId=UUID(CONTRACT['scenarioId']), workspaceId=uuid4(),
                                        frozenScenarioContractJson=json.dumps(CONTRACT, ensure_ascii=False))
    authority = freeze_requirement_authority(artifact)
    assert [item.key for item in authority.requirements] == [f'REQ-{index:03d}' for index in range(1, 14)]


def test_each_requirement_has_one_criterion_and_the_scenario_document_validators():
    validators = [item['validators'] for item in CONTRACT['requirements']]
    assert validators == [['QA']] * 7 + [['SECURITY']] * 5 + [['ORCHESTRATOR']]
    assert all(len(item['acceptanceCriteria']) == 1 for item in CONTRACT['requirements'])
    assert len({item['requirementId'] for item in CONTRACT['requirements']}) == 13


def test_signup_policies_are_not_inherited():
    text = json.dumps(CONTRACT, ensure_ascii=False)
    assert 'argon2' not in text.lower() and 'passwordHashPolicy' not in text and CONTRACT['emailPolicy'] == {}
    assert CONTRACT['securityPolicy']['fileSizeLimitBytes'] == 10 * 1024 * 1024
