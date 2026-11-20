"""Conflict sidecars publish atomically with the existing scorecard, without API calls."""
import json
import hashlib

import pytest

from evaluation import workflow
from tests.evaluation.test_conflicts import records
from tests.evaluation.test_final_evaluation import RecordingRunner
from tests.evaluation.test_workflow import export_folder, mark_export_resumed


def test_conflicts_are_next_to_accuracy_and_do_not_change_accuracy(tmp_path, monkeypatch):
    folder, _ = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    runner = RecordingRunner()
    bundle, trace = records()
    data = workflow.conflict_analysis(bundle, trace)
    monkeypatch.setattr(workflow, 'conflict_analysis', lambda *args, **kwargs: data)
    target = workflow.ingest(folder, results=tmp_path / 'results', runner=runner, exam='pilot')
    assert runner.calls == 0
    sidecar = json.loads((target / 'conflict_analysis.json').read_text())
    card = json.loads((target / 'axis_scorecard.json').read_text())
    assert sidecar['excluded'] == 3 and card['conflictAnalysis']['undetermined'] == 3
    assert all(row['value'] is None for row in card['axes']['accuracy'])
    assert '정책 제외 충돌(ACC와 별도)' in (target / 'axis_scorecard.md').read_text()
    assert 'conflict_analysis.md' in (target / 'axis_scorecard.md').read_text()


@pytest.mark.parametrize('complete', [False, True])
def test_zero_requires_complete_handoff_evidence(tmp_path, complete):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    if complete:
        path = folder / 'bundle.json'
        bundle = json.loads(path.read_text())
        bundle['snapshotConsistent'] = True
        path.write_text(json.dumps(bundle))
        handoff = json.loads((folder / 'handoff.json').read_text())
        handoff['files']['bundle.json'] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                          'sizeBytes': path.stat().st_size}
        (folder / 'handoff.json').write_text(json.dumps(handoff))
    target = workflow.ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='pilot')
    data = json.loads((target / 'conflict_analysis.json').read_text())
    count = 0 if complete else None
    assert (data['runId'], data['excluded'], data['undetermined']) == (run_id, count, count)


def test_conflict_write_failure_never_publishes_partial_result(tmp_path, monkeypatch):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    original = workflow._write
    def failing(path, *args):
        if path.name == 'conflict_analysis.md': raise OSError('injected conflict write failure')
        return original(path, *args)
    monkeypatch.setattr(workflow, '_write', failing)
    with pytest.raises(OSError, match='conflict write'):
        workflow.ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='pilot')
    assert not (tmp_path / 'results' / run_id).exists()
    assert not list((tmp_path / 'results').glob('.*.staging-*'))


def test_assisted_results_are_separate_from_automatic_conflicts(tmp_path):
    folder, _ = export_folder(tmp_path / 'export')
    mark_export_resumed(folder)
    runner = RecordingRunner()
    target = workflow.ingest(folder, results=tmp_path / 'results', runner=runner, exam='pilot')
    assert runner.calls == 0 and not (target / 'conflict_analysis.json').exists()
    data = json.loads((target / 'assisted_conflict_analysis.json').read_text())
    assert data['scope'] == 'FULL_RUN_INCLUDING_ASSISTED_CONTINUATION'


def test_foreign_reproduction_review_is_rejected_before_scoring(tmp_path):
    folder, _ = export_folder(tmp_path / 'export')
    runner = RecordingRunner()
    with pytest.raises(ValueError, match='CONFLICT_REVIEW_INVALID'):
        workflow.ingest(folder, results=tmp_path / 'results', runner=runner, exam='pilot',
                        conflict_review={'schemaVersion': 1, 'runId': 'another-run', 'reviews': []})
    assert runner.calls == 0 and not (tmp_path / 'results').exists()


def test_cli_preserves_explicit_review_without_starting_scoring_for_no_source(tmp_path, monkeypatch):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    review = {'schemaVersion': 1, 'runId': run_id, 'reviews': []}
    review_file = tmp_path / 'review.json'
    review_file.write_text(json.dumps(review))
    runner = RecordingRunner()
    monkeypatch.setattr(workflow, 'DockerRunner', lambda image: runner)
    assert workflow.main(['--export', str(folder), '--results', str(tmp_path / 'results'),
                          '--image', 'unused-fixture', '--exam', 'pilot',
                          '--conflict-review', str(review_file)]) == 0
    assert runner.calls == 0
    assert json.loads((tmp_path / 'results' / run_id / 'conflict_review.json').read_text()) == review
