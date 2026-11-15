"""세 번째 채점지(정확도·속도·비용·과정·안정성, axis_scorecard)가 Run 결과 폴더마다 나오고, 같은 폴더의 원본 값과 맞는지.

새로 측정하지 않고 이미 쓴 결과에서 모으므로, 원본 파일(output.json·timing_metrics.json·process_metrics.json·
regression.json·outcome.json·bundle.json)의 값과 하나씩 대조한다. 없는 값은 0이 아니라 미확인이어야 한다.
"""
import json

import pytest

from evaluation.axis_scorecard import GROUP_ONLY, MEASURED, UNVERIFIED, build_scorecard, render_scorecard
from evaluation.scoring import product_accuracy
from evaluation.workflow import ingest
from tests.evaluation.test_final_evaluation import RUNTIME, RecordingRunner, VerifiedImageRunner
from tests.evaluation.test_workflow import export_folder

AXES = ('accuracy', 'speed', 'cost', 'process', 'stability')


def _rows(card, axis):
    return {(row['id'], row['name']): row for row in card['axes'][axis]}


def _by_id(card, axis, metric_id):
    found = [row for row in card['axes'][axis] if row['id'] == metric_id]
    assert len(found) == 1, metric_id
    return found[0]


def _load(target, name):
    return json.loads((target / name).read_text(encoding='utf-8'))


@pytest.mark.skipif(not RUNTIME, reason='합성 회원가입 서비스 실행 의존성이 필요하다')
@pytest.mark.parametrize('name,versions', [('single', (1,)), ('multiFix', (1, 2))])
def test_scored_run_writes_the_third_scorecard_from_the_same_folder(tmp_path, name, versions):
    folder, run_id = export_folder(tmp_path / 'export', name=name, versions=versions)
    secret = 'fictional-axis-canary-789'
    target = ingest(folder, results=tmp_path / 'results', runner=VerifiedImageRunner(), exam='final',
                    known_values=[secret])
    # 세 채점지가 같은 폴더에 있다.
    for file in ('summary.md', 'check_details.md', 'axis_scorecard.md', 'axis_scorecard.json'):
        assert (target / file).is_file(), file
    card = _load(target, 'axis_scorecard.json')
    output, outcome, bundle = _load(target, 'output.json'), _load(target, 'outcome.json'), _load(target, 'bundle.json')
    assert card['runId'] == run_id and card['evaluationStatus'] == 'SCORED' and set(card['axes']) == set(AXES)
    assert card['evaluationId'] == output['report']['evaluationId']

    accuracy = product_accuracy(output['report'])
    acc01 = _by_id(card, 'accuracy', 'ACC-01')
    assert acc01['status'] == MEASURED
    assert (acc01['value']['lower'], acc01['value']['upper']) == (accuracy['fullSatisfactionLower'], accuracy['fullSatisfactionUpper'])
    assert _by_id(card, 'accuracy', 'ACC-02')['value'] is accuracy['productPass']
    assert _by_id(card, 'accuracy', 'ACC-04')['value'] == accuracy['checkDecidability']
    assert _by_id(card, 'accuracy', 'ACC-05')['value']['traceability'] == output['trace']['traceability']['status']
    partial = _rows(card, 'accuracy')[('보조', '요구사항별 부분점수(ACC-01과 별개)')]
    assert partial['value'] == output['score']['score']

    timing = _load(target, 'timing_metrics.json')
    time05 = _by_id(card, 'speed', 'TIME-05')
    assert time05['value'] == timing['firstPassingSnapshotMs']
    assert _by_id(card, 'speed', 'TIME-02')['value'] == outcome['packageTiming']['packageMs']
    assert _by_id(card, 'speed', 'TIME-04')['status'] == GROUP_ONLY

    summary = bundle['usage']['summary']
    assert _by_id(card, 'cost', 'COST-01')['value']['modelCalls'] == summary['modelCalls']
    cost = _by_id(card, 'cost', 'COST-02')
    if summary.get('costComplete'):
        assert cost['status'] == MEASURED and cost['value'] == summary['costUsd']
    else:
        assert cost['status'] == UNVERIFIED and cost['value'] is None and cost['reason']

    process, regression = _load(target, 'process_metrics.json'), _load(target, 'regression.json')
    assert _by_id(card, 'process', 'PROC-07')['value'] == process['conflicts']['count']
    assert _by_id(card, 'process', 'PROC-04')['value'] == regression['regressionCount']
    assert _by_id(card, 'process', 'PROC-05')['value'] == output['trace']['traceability']['status']

    stab01 = _by_id(card, 'stability', 'STAB-01')
    assert stab01['value'] is accuracy['productPass'] and stab01['status'] == MEASURED
    assert _by_id(card, 'stability', 'STAB-06')['value'] == outcome['autoCompletion']['status']

    text = (target / 'axis_scorecard.md').read_text(encoding='utf-8')
    for title in ('## 정확도', '## 속도', '## 비용', '## 과정', '## 안정성'):
        assert title in text
    assert secret not in text and secret not in (target / 'axis_scorecard.json').read_text(encoding='utf-8')


def test_unscored_run_keeps_time_cost_and_stability_but_never_invents_accuracy(tmp_path):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    runner = RecordingRunner()
    target = ingest(folder, results=tmp_path / 'results', runner=runner, exam='final')
    assert runner.calls == 0 and not (target / 'output.json').exists()
    card = _load(target, 'axis_scorecard.json')
    assert card['evaluationStatus'] == 'UNVERIFIED' and card['evaluationReason'] == 'NO_ARTIFACT'
    for row in card['axes']['accuracy']:
        assert row['status'] == UNVERIFIED and row['value'] is None and row['reason'] == 'NO_ARTIFACT'
    assert _by_id(card, 'speed', 'TIME-05')['reason'] == 'NO_ARTIFACT'
    # 실패·중단 Run의 비용도 통과당 비용 분모에 들어가야 하므로 그대로 남는다.
    assert _by_id(card, 'cost', 'COST-02')['value'] == '0.5'
    # 채점하지 못한 Run은 제품 통과가 아니다(측정된 사실). 중단 여부도 남긴다.
    stab01 = _by_id(card, 'stability', 'STAB-01')
    assert stab01['status'] == MEASURED and stab01['value'] is False
    assert _by_id(card, 'stability', 'STAB-02')['value']['aborted'] is True
    assert '| ACC-01 | 요구사항 완전 충족률 | 미확인 | 미확인 | NO_ARTIFACT |' in (target / 'axis_scorecard.md').read_text()


def test_missing_values_stay_unverified_instead_of_zero():
    card = build_scorecard(run_id='r', architecture='SINGLE_AGENT', exam='final', metrics={}, record={},
                           status={'status': 'UNVERIFIED', 'reason': 'NO_ARTIFACT'})
    rows = [row for axis in AXES for row in card['axes'][axis]]
    for row in rows:
        if row['status'] == UNVERIFIED:
            # 미확인 줄은 값·표시 없이 사유만 남는다(“없음”·0 같은 확정처럼 보이는 표시 금지).
            assert row['value'] is None and row['reason'], row
            assert row['display'] is None or '미확인' in row['display'] or '부분합' in row['display'], row
    for metric_id in ('TIME-01', 'TIME-02', 'TIME-03', 'TIME-06'):
        assert _by_id(card, 'speed', metric_id)['status'] == UNVERIFIED
    for metric_id in ('COST-01', 'COST-02'):
        assert _by_id(card, 'cost', metric_id)['status'] == UNVERIFIED
    assert _by_id(card, 'cost', 'COST-04')['value'] is False  # 비용을 확인하지 못한 것은 "완전하지 않음"
    text = render_scorecard(card)
    assert '| COST-02 | API 비용 | 미확인 | 미확인 | PRICE_BOOK_NOT_RECORDED |' in text
    assert '0 ms' not in text and '$0' not in text


def test_no_passing_version_is_a_measured_time05_fact():
    card = build_scorecard(run_id='r', architecture='MULTI_AGENT', exam='final', metrics={}, record={},
                           status={'status': 'SCORED'},
                           timing={'status': 'PASS', 'reason': 'NO_PASSING_SNAPSHOT', 'firstPassingSnapshotMs': None})
    time05 = _by_id(card, 'speed', 'TIME-05')
    assert time05['status'] == MEASURED and time05['value'] is None and time05['display'] == '통과한 버전 없음'


@pytest.mark.parametrize('tables', [{}, {'firstProblem': None, 'handoffs': [], 'timing': []}])
def test_missing_step_observations_do_not_claim_no_agent_errors(tables):
    card = build_scorecard(run_id='r', architecture='MULTI_AGENT', exam='final', metrics={}, record={},
                           status={'status': 'UNVERIFIED'}, tables=tables)
    row = _by_id(card, 'process', 'PROC-03')
    assert row['status'] == UNVERIFIED and row['value'] is None
    assert row['reason'] == 'NO_OBSERVED_STEPS' and row['display'] is None
    assert _by_id(card, 'stability', 'STAB-02')['status'] == UNVERIFIED


def test_incomplete_token_parts_do_not_crash_or_publish_complete_usage():
    card = build_scorecard(run_id='r', architecture='SINGLE_AGENT', exam='final',
                           metrics={'modelCalls': 1, 'totalTokens': 10, 'tokenBreakdownComplete': True},
                           record={}, status={'status': 'UNVERIFIED'})
    row = _by_id(card, 'cost', 'COST-01')
    assert row['status'] == UNVERIFIED and row['value'] is None and row['reason'] == 'USAGE_INCOMPLETE'
    assert 'None회' not in render_scorecard(card)
