"""계획의 limits.outputCorrections·questionReminders가 Single·Multi Host 실행 설정에 같은 값으로 들어가고, 다르면 제출 전에 잡히는지.

값이 없으면 0(끔)으로 본다. 기존 계획·설정은 그대로 통과해야 한다(agents.llm.corrections 참고).
"""
import json

from evaluation import experiment_run as er
from tests.evaluation.test_experiment_run import ROOT, RUNTIME_LIMITS, _approved, _matching_plan, _pilot


def _plan(value=None):
    plan = _matching_plan()
    if value is not None:
        plan['limits'] = dict(plan['limits'], outputCorrections=value)
    return plan


def test_missing_on_both_sides_means_off_and_matches():
    assert er.plan_differences(_plan(), _approved(), {'llmLimits': RUNTIME_LIMITS}) == []
    assert er.plan_differences(_plan(0), _approved(), {'llmLimits': RUNTIME_LIMITS}) == []


def test_plan_and_host_must_agree_on_the_correction_limit():
    host_on = {'llmLimits': dict(RUNTIME_LIMITS, max_output_corrections=2)}
    assert er.plan_differences(_plan(2), _approved(), host_on) == []
    assert er.plan_differences(_plan(2), _approved(), {'llmLimits': RUNTIME_LIMITS}) == ['limits.outputCorrections']
    assert er.plan_differences(_plan(), _approved(), host_on) == ['limits.outputCorrections']
    assert er.plan_differences(_plan(1), _approved(), host_on) == ['limits.outputCorrections']


def test_both_architectures_get_the_same_value_and_the_dev_loader_accepts_it(tmp_path):
    plan = _pilot()
    plan['limits'] = dict(plan['limits'], outputCorrections=2)
    written = {}
    for architecture, example in (('MULTI_AGENT', 'owned-runtime.example.json'),
                                  ('SINGLE_AGENT', 'single-runtime.example.json')):
        out = er.write_runtime_configuration(ROOT / 'configs' / example, plan, architecture,
                                             tmp_path / f'{architecture}.json')
        written[architecture] = json.loads(out.read_text(encoding='utf-8'))
    assert written['MULTI_AGENT']['llmLimits']['max_output_corrections'] == 2
    assert written['MULTI_AGENT']['llmLimits'] == written['SINGLE_AGENT']['llmLimits']


def test_question_reminders_follow_the_same_rule():
    host = {'llmLimits': dict(RUNTIME_LIMITS, max_question_reminders=1)}
    plan = _matching_plan()
    plan['limits'] = dict(plan['limits'], questionReminders=1)
    assert er.plan_differences(plan, _approved(), host) == []
    assert er.plan_differences(_matching_plan(), _approved(), host) == ['limits.questionReminders']


def test_provider_retries_follow_the_same_rule():
    host = {'llmLimits': dict(RUNTIME_LIMITS, max_provider_retries=1)}
    plan = _matching_plan()
    plan['limits'] = dict(plan['limits'], providerRetries=1)
    assert er.plan_differences(plan, _approved(), host) == []
    assert er.plan_differences(_matching_plan(), _approved(), host) == ['limits.providerRetries']


def test_pilot_plans_set_the_same_values_for_both_conditions_and_use_experiment_hosts():
    for key in ('outputCorrections', 'questionReminders', 'providerRetries'):
        assert len({json.dumps(_pilot(index)['limits'].get(key)) for index in (0, 1)}) == 1
    for index in (0, 1):
        for entry in _pilot(index)['hostConfigurations'].values():
            # 측정 전용 동작(재생성·질문 안내)은 제품 Host가 쓰는 공용 설정이 아니라 실험 설정에만 둔다.
            assert entry['runtime'].startswith('../../configs/experiment/')
