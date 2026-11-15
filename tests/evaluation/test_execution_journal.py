"""실행했지만 결과가 없는 Run을 미시작이나 비용 0으로 바꾸지 않는다."""
import json

import pytest

from evaluation import experiment_run as er
from evaluation.experiment import compare, ExperimentError, main
from evaluation.comparison_report import render_comparison
from tests.evaluation.test_experiment_run import _Host, _matching_plan, _approved, _runtime_files, _exporter
from tests.evaluation.test_experiment import keep, real_report
from tests.evaluation.test_run_metrics import uid


def failed_export(tmp_path, monkeypatch):
    _runtime_files(tmp_path)
    plan = _matching_plan(plannedRuns={'SINGLE_AGENT': 1, 'MULTI_AGENT': 1})
    row = er.schedule(plan)['runs'][0]
    host = _Host(['PLANNING', 'IMPLEMENTING', 'FINISHED'])
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: _approved(architecture=architecture))
    out = tmp_path / 'execution'
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, tmp_path, client_factory=host.client, sleep=lambda _: None, exporter=_exporter([], fail=1))
    return plan, host.run_id, out


def recorded_result(root, plan):
    folder = keep(root, uid(19), None, real_report(uid(19), 'final'), real_report(uid(19), 'practice'))
    path = folder / 'bundle.json'
    data = json.loads(path.read_text())
    data['configuration']['configuration']['experimentId'] = plan['experimentId']
    path.write_text(json.dumps(data))


def test_actual_accepted_finished_run_remains_visible_after_export_failure(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    results = tmp_path / 'results'
    results.mkdir()
    recorded_result(results, plan)
    comparison = compare(results, plan['plannedRuns'], 'architecture', run_journals=[execution])
    single = comparison['summary']['SINGLE_AGENT']
    assert (single['recordedRuns'], single['startedRuns'], single['startedWithoutResults']) == (0, 1, 1)
    assert single['notStarted'] == single['unknownExecutionSlots'] == 0
    assert single['costUsd'] is None and single['successRate'] == 0
    row = next(row for row in comparison['runs'] if row['runId'] == run_id)
    assert row['workflowStatus'] == 'FINISHED' and row['scoring'] == 'RESULT_NOT_COLLECTED'
    assert 'PLANNED_SLOT_RESULTS_NOT_COLLECTED' in comparison['readiness']['blockers']
    assert run_id in render_comparison(comparison)


def test_missing_results_without_journal_are_unknown_not_unstarted(tmp_path):
    result = compare(tmp_path, {'SINGLE_AGENT': 1}, 'architecture')
    group = result['summary']['SINGLE_AGENT']
    assert group['notStarted'] is None and group['unknownExecutionSlots'] == 1
    assert group['startedRuns'] == 0 and group['unrecordedRuns'] == 1
    assert group['stability']['notStartedRate'] is None and group['costUsd'] is None


def test_journal_in_results_root_is_loaded_without_extra_flag(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    result = compare(execution, plan['plannedRuns'], 'architecture')
    assert result['summary']['SINGLE_AGENT']['startedWithoutResults'] == 1
    assert any(row['runId'] == run_id for row in result['runs'])
    assert result['summary']['MULTI_AGENT']['unknownExecutionSlots'] == 1


def test_uncertain_submission_is_not_counted_as_accepted(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    path = execution / 'runs.jsonl'
    path.write_text(path.read_text().splitlines()[0] + '\n')
    result = compare(execution, plan['plannedRuns'], 'architecture')
    group = result['summary']['SINGLE_AGENT']
    assert group['startedRuns'] == 0 and group['notStarted'] is None
    assert group['submissionUnconfirmedSlots'] == group['unknownExecutionSlots'] == 1


def test_journal_for_another_experiment_is_rejected(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    results = tmp_path / 'results'; results.mkdir()
    recorded_result(results, dict(plan, experimentId=uid(999)))
    with pytest.raises(ExperimentError, match='EXECUTION_JOURNAL_MISMATCH'):
        compare(results, plan['plannedRuns'], 'architecture', run_journals=[execution])


def test_damaged_journal_is_not_ignored(tmp_path):
    (tmp_path / 'runs.jsonl').write_text('{broken')
    with pytest.raises(ExperimentError, match='EXECUTION_JOURNAL_INVALID'):
        compare(tmp_path, {'SINGLE_AGENT': 1}, 'architecture')


def test_compare_cli_accepts_journal_and_keeps_missing_slot(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    results = tmp_path / 'results'; results.mkdir()
    assert main(['compare', '--results', str(results), '--group-by', 'architecture',
                 '--planned', 'SINGLE_AGENT=1', '--planned', 'MULTI_AGENT=1',
                 '--run-journal', str(execution / 'runs.jsonl')]) == 0
    result = json.loads((results / 'comparison.json').read_text())
    assert result['summary']['SINGLE_AGENT']['startedWithoutResults'] == 1


@pytest.mark.parametrize('uncertain', [False, True])
def test_different_run_cannot_fill_the_same_bound_slot(tmp_path, monkeypatch, uncertain):
    from evaluation.execution_journal import read_execution_journals, validate_bindings
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    if uncertain:
        path = execution / 'runs.jsonl'
        path.write_text(path.read_text().splitlines()[0] + '\n')
    slots, _ = read_execution_journals([execution])
    configuration = er.submission(plan, _approved(architecture='SINGLE_AGENT'), 1)['configuration']
    replacement = {'architecture': 'SINGLE_AGENT', 'loopPolicy': 'FINAL_ONLY',
                   'configuration': {'runId': uid(99), 'configuration': configuration}}
    with pytest.raises(ValueError, match='EXECUTION_JOURNAL_SLOT_RUN_MISMATCH'):
        validate_bindings(slots, [replacement])
    # An actual matching export still binds, and another planned order is unrelated.
    if not uncertain:
        replacement['configuration']['runId'] = run_id
        validate_bindings(slots, [replacement])
    replacement['configuration']['runId'] = uid(99)
    replacement['configuration']['configuration']['executionOrder'] = 3
    validate_bindings(slots, [replacement])


def test_comparison_rejects_replacement_instead_of_counting_two_attempts(tmp_path, monkeypatch):
    plan, run_id, execution = failed_export(tmp_path, monkeypatch)
    results = tmp_path / 'results'
    folder = keep(results, uid(19), None, real_report(uid(19), 'final', architecture='SINGLE_AGENT'),
                  real_report(uid(19), 'practice', architecture='SINGLE_AGENT'), architecture='SINGLE_AGENT')
    path = folder / 'bundle.json'
    bundle = json.loads(path.read_text())
    bundle['configuration']['configuration'].update(experimentId=plan['experimentId'], executionOrder=1)
    path.write_text(json.dumps(bundle))
    # There is room in the planned denominator, so the count limit alone cannot
    # detect a second Run claiming the first accepted Run's scheduled slot.
    with pytest.raises(ExperimentError, match='EXECUTION_JOURNAL_MISMATCH'):
        compare(results, {'SINGLE_AGENT': 2, 'MULTI_AGENT': 1}, 'architecture', run_journals=[execution])
