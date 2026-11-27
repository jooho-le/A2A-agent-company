from copy import deepcopy
from types import SimpleNamespace as N
from uuid import UUID

from agents.roles.prompts import build_system_prompt
from agents.roles.work_rules import role_work_rules
from agents.runtime.requirement_scope import signup_observation_plan
from agents.single.prompts import build_single_system_prompt
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario
from orchestrator.domain.states import AgentRole


def test_each_role_procedure_is_shared_with_the_single_identity():
    single = build_single_system_prompt()
    for role in AgentRole:
        multi = build_system_prompt(role)
        for rule in role_work_rules(role):
            assert rule in multi and rule in single
    assert '정체성: SINGLE_AGENT' in single


def test_public_plan_covers_approved_requirements_and_preserves_frozen_criteria():
    source = get_scenario(SCN_001_ID).planner_contract()
    before = deepcopy(source)
    plan = signup_observation_plan(source)
    assert set(plan) == {r['requirementId'] for r in source['requirements']}
    for r in source['requirements']:
        assert plan[r['requirementId']]['acceptanceCriteria'] == r['acceptanceCriteria']
    assert source == before


def test_validator_gets_only_assigned_observations_and_no_protected_exam():
    source = get_scenario(SCN_001_ID).planner_contract()
    for role in (AgentRole.QA, AgentRole.SECURITY):
        assigned = [r for r in source['requirements'] if role.value in r['validators']]
        scope = N(role=role, task_bindings=(), requirements=[N(requirement_id=UUID(r['requirementId'])) for r in assigned])
        plan = signup_observation_plan(source, scope)
        assert set(plan) == {r['requirementId'] for r in assigned}
        assert all(set(v['observationsByRole']) == {role.value} for v in plan.values())
        assert 'exam_final' not in str(plan) and 'protected_files' not in str(plan)


def test_non_signup_contract_keeps_its_own_requirements():
    assert signup_observation_plan({'scenarioKey':'OTHER'}) == {}
