"""발표용 비교표에서도 결측·비교 불가능·표본 수가 그대로 드러나는지 확인한다."""
from evaluation.comparison_report import render_comparison
from evaluation.run_metrics import experiment_summary, run_metrics
from tests.evaluation.test_run_metrics import bundle


def test_real_summary_shape_renders_unknowns_and_does_not_assert_superiority():
    summary = experiment_summary([run_metrics(bundle())], {'SINGLE_AGENT': 2, 'MULTI_AGENT': 2})
    report = render_comparison({'summary': summary, 'exam': 'final', 'groupBy': 'architecture',
                               'plannedRunsKnown': True, 'validity': None})
    assert '공식 우열 결론 유보' in report
    assert '| 계획 Run 수 | 2 | 2 |' in report
    assert '| 입력 토큰 | 미확인 | 미확인 |' in report
    assert '| 제품 통과 Run 판정 시간 표본 수 | 0 | 0 |' in report


def test_invalid_conditions_remain_visible():
    report = render_comparison({'summary': {}, 'validity': {'valid': False, 'warnings': [],
        'violations': [{'field': 'model.modelId', 'severity': 'INVALID_COMPARISON'}]}, 'plannedRunsKnown': True})
    assert '공식 우열 결론 유보' in report and 'model.modelId: INVALID_COMPARISON' in report


def test_new_accuracy_auto_completion_outcome_and_pair_sections_render():
    from tests.evaluation.test_outcomes import _pair_runs, _review, _with_outcome
    from evaluation.run_metrics import architecture_pairs
    from tests.evaluation.test_run_metrics import uid
    runs = [_with_outcome(run_metrics(bundle(uid(1), fixes=0)), review=_review(2)),
            _with_outcome(run_metrics(bundle(uid(2), fixes=0, verdict=None, status='WAITING_INPUT')), auto='FAIL',
                          cause='SEMANTIC_REVIEW_HOLD', review=_review(1))]
    summary = experiment_summary(runs, {'MULTI_AGENT': 2})
    pair_runs, reports, pairs = _pair_runs()
    report = render_comparison({'summary': summary, 'exam': 'pilot', 'groupBy': 'architecture', 'plannedRunsKnown': True,
                                'validity': {'valid': True, 'violations': [], 'warnings': [
                                    {'field': 'outcome', 'detail': 'OUTCOME_NOT_RECORDED', 'runs': [uid(3)]}]},
                                'toolParity': {'status': 'NOT_CHECKED'},
                                'pairComparison': architecture_pairs(pair_runs, reports, pairs)})
    assert '- 도구·LLM 한도 동일 확인: NOT_CHECKED' in report
    assert '- 비교 조건: 공식 우열 결론 유보' in report
    assert '| 자동 완료율(STAB-06) | 0.5 |' in report
    assert '| 요구사항 완전 충족률 하한 평균(ACC-01) | 미확인 |' in report  # 독립 보고서가 없으면 0이 아니라 미확인
    assert '| 그중 의미 검토 호출 수 | 3 |' in report and '| 멈춤(입력 대기·사람 검토) 비율 | 0.5 |' in report
    assert '## 끝 상태 원인(Run 수)' in report and '| 의미 검토 보류 | 1 |' in report
    assert '## 짝 비교(STAB-05, Multi − Single)' in report and '제외: 쌍 p2 · NOT_ONE_SINGLE_AND_ONE_MULTI' in report
    assert '- outcome: OUTCOME_NOT_RECORDED (1개 Run)' in report
    assert '결과 패키지 완료 시간(TIME-02)은' in report


def test_report_without_new_fields_still_renders():
    # 예전 비교 결과(새 칸 없음)도 미확인으로 보이고 멈추지 않는다.
    report = render_comparison({'summary': {'MULTI_AGENT': {'plannedRuns': 1}}, 'plannedRunsKnown': False, 'validity': None})
    assert '| 자동 완료율(STAB-06) | 미확인 |' in report and '짝 비교' not in report and '끝 상태 원인' not in report


def test_assisted_diagnostics_are_explicit_and_package_note_follows_actual_evidence():
    from tests.evaluation.test_run_metrics import mark_resumed
    summary = experiment_summary([run_metrics(mark_resumed(bundle(cost='7.25')))], {'MULTI_AGENT': 1})
    data = {'summary': summary, 'groupBy': 'architecture', 'plannedRunsKnown': True, 'validity': None}
    report = render_comparison(data)
    assert '| 사람 보조 재개로 분리된 Run 수 | 1 |' in report
    assert '## 사람 보조 재개 진단(자동 비교 제외)' in report and '| 31000 | 7.25 |' in report
    assert '| API 비용(USD) | 미확인 |' in report and '| 미시작 Run 수 | 0 |' in report
    assert '검증된 패키지 완료 시간 표본이 없어' in report
    summary['MULTI_AGENT']['stability']['packageMs'] = {'n': 1, 'median': 32000}
    report = render_comparison(data)
    assert '검증된 패키지 완료 증거가 있는 Run만 집계했다' in report
    assert '표본이 없어' not in report and 'dev에 패키지 저장 사건이 없어' not in report


def test_architecture_readiness_requires_parity_and_all_explicit_blockers_clear():
    data = {'summary': {}, 'groupBy': 'architecture', 'plannedRunsKnown': True,
            'validity': {'valid': True, 'violations': [], 'warnings': []}}
    for parity in ({}, {'status': 'NOT_CHECKED'}, {'status': 'UNVERIFIED'}, {'status': 'FAIL'}):
        assert '- 비교 조건: 공식 우열 결론 유보' in render_comparison({**data, 'toolParity': parity})
    data['toolParity'] = {'status': 'PASS'}
    assert '- 비교 조건: 공식 우열 결론 유보' in render_comparison(data)
    data['toolParity']['pinnedRunsVerified'] = True
    assert '- 비교 조건: 검사 통과' in render_comparison(data)
    data['readiness'] = {'blockers': ['PLANNED_RUNS_UNKNOWN']}
    assert '- 비교 조건: 공식 우열 결론 유보' in render_comparison(data)


def test_version_readiness_also_requires_parity_for_its_own_comparison():
    # 점검 5: Ver1·Ver2(loopPolicy) 비교도 한도 확인 없이 "검사 통과"로 쓰지 않는다.
    data = {'summary': {}, 'groupBy': 'loopPolicy', 'plannedRunsKnown': True,
            'validity': {'valid': True, 'violations': [], 'warnings': []}}
    for parity in ({}, {'status': 'NOT_CHECKED'}, {'status': 'PASS'},
                   {'status': 'PASS', 'pinnedRunsVerified': True, 'groupBy': 'architecture'}):
        assert '- 비교 조건: 공식 우열 결론 유보' in render_comparison({**data, 'toolParity': parity})
    data['toolParity'] = {'status': 'PASS', 'pinnedRunsVerified': True, 'groupBy': 'loopPolicy'}
    assert '- 비교 조건: 검사 통과' in render_comparison(data)
