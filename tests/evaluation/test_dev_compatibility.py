"""지금 dev가 실제로 남기는 Run 기록으로 평가 코드를 확인한다. dev 출력이 바뀌면 고정 기록 시험보다 여기서 먼저 드러난다.

dev 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 Run 세 개(수정 1회 뒤 성공, Planner 의미 검토 보류,
Developer 모델 API 오류)를 만들어 dev evaluation_export로 꺼낸다. 하네스 자체를 만들지 못하면(dev 시험 도구가 바뀜)
평가 결함이 아니므로 건너뛰되 이유를 남긴다. 평가 코드가 그 기록을 처리하지 못하면 실패다.
"""
import json
import os
from unittest.mock import patch

import pytest

from evaluation.agent_eval import agent_tables
from evaluation.outcomes import classify_run, review_usage, run_outcome
from evaluation.run_metrics import run_metrics
from evaluation.workflow import ingest, load_handoff
from tests.evaluation.test_final_evaluation import RecordingRunner

try:
    from tests.evaluation import dev_harness_exports as harness
except Exception as error:  # dev 시험 도구를 불러오지 못함
    harness, IMPORT_ERROR = None, f'{type(error).__name__}: {error}'
else:
    IMPORT_ERROR = None

EXPECTED = {'fix-success': ('FINISHED', 'COMPLETED_SUCCESS', 'VALID'),
            'planner-semantic-hold': ('WAITING_INPUT', 'SEMANTIC_REVIEW_HOLD', 'VALID'),
            'developer-provider-error': ('HUMAN_REVIEW', 'MODEL_API_FAILURE', 'UNVERIFIED')}


@pytest.fixture(scope='module')
def exports(tmp_path_factory):
    if harness is None:
        pytest.skip('dev 시험 하네스를 불러오지 못함: ' + IMPORT_ERROR)
    out = tmp_path_factory.mktemp('dev-exports')
    # Check the real three-Run helper without printing or comparing secret env values.
    sentinel = 'CAPSTONE_TEST_EXPORT_CLEANUP_SENTINEL'
    with patch.dict(os.environ, {sentinel: 'inert-cleanup-probe'}):
        result = harness.make_exports(out)
        restored_sentinel = os.environ.get(sentinel)
        assert restored_sentinel == 'inert-cleanup-probe', 'export helper leaked its cleared environment'
    if not result['ok']:
        pytest.skip('dev 시험 하네스로 Run을 만들지 못함: ' + result['log'][:800])
    return out


@pytest.mark.parametrize('name', sorted(EXPECTED))
def test_current_dev_export_passes_the_handoff_check_and_is_classified(exports, name):
    export, run = load_handoff(exports / name)
    result = classify_run(export['bundle'], export['trace'], export['a2a'])
    assert (result['runStatus'], result['cause'], result['comparisonValidity']) == EXPECTED[name]
    assert result['evidenceComplete'] is True and run['runId'] == export['bundle']['run']['runId']


def test_current_dev_success_run_keeps_receipts_reviews_and_auto_completion_conditions(exports):
    export, run = load_handoff(exports / 'fix-success')
    tables = agent_tables(export)
    assert {row['receipt']['status'] for row in tables['handoffs']} == {'PASS'} and tables['firstProblem'] is None
    assert {row['modelInput']['status'] for row in tables['handoffs']} == {'PASS'}
    assert all(row['reviewLlmCalls'] for row in tables['timing'] if row['steps'])
    usage = review_usage(export['bundle'], export['trace'])
    assert usage['complete'] is True and usage['reviewModelCalls'] + usage['workModelCalls'] == \
        export['bundle']['usage']['summary']['modelCalls']
    record = run_outcome(export, run, metrics=run_metrics(export['bundle']))
    conditions = record['autoCompletion']['conditions']
    assert {name: item['status'] for name, item in conditions.items()} == {
        'productPass': 'UNVERIFIED', 'internalSuccess': 'PASS', 'withinLimits': 'PASS', 'packageComplete': 'UNVERIFIED',
        'noHumanIntervention': 'PASS'}


def test_current_dev_hold_is_ingested_with_its_cause(exports, tmp_path):
    runner = RecordingRunner()
    target = ingest(exports / 'planner-semantic-hold', results=tmp_path / 'results', runner=runner, exam='final')
    state = json.loads((target / 'evaluation_status.json').read_text())
    assert runner.calls == 0
    assert (state['cause'], state['resume'], state['automaticSegmentClosed']) == \
        ('SEMANTIC_REVIEW_HOLD', 'ANSWER_THEN_REVIEW', True)


def test_current_dev_snapshot_events_bind_to_exported_version_manifests(exports):
    # 채점 결과는 합성 대역이며, 여기서는 실제 dev가 내보내는 사건/Manifest 연결 형식을 검증한다.
    import copy
    from evaluation.aggregation import Status
    from evaluation.evidence_metrics import first_passing_snapshot
    from tests.evaluation.test_outcomes import report_with
    export, run = load_handoff(exports / 'fix-success')
    reports = []
    for version in run['exportedCodeVersions']:
        report = copy.deepcopy(report_with({20: Status.FAIL} if version < run['finalCodeVersion'] else {},
                                          run_id=run['runId'], code_version=version))
        manifest = json.loads((exports / 'fix-success' / f'v{version}' / 'manifest.json').read_text())
        report['expectedManifest'] = report['observedManifest'] = manifest
        reports.append(report)
    timing = first_passing_snapshot(export['bundle'], reports)
    assert timing['status'] == 'PASS' and timing['firstPassingVersion'] == run['finalCodeVersion']
    assert timing['firstPassingSnapshotMs'] >= 0
