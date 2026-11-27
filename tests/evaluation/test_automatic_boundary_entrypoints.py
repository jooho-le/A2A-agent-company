"""Direct score/record commands must close paused measurements like workflow ingest."""
import copy
import json

import pytest

from evaluation.agent_eval import run_contract
from evaluation.experiment import ExperimentError, record_run, score_export
from evaluation.workflow import load_handoff
from tests.evaluation.test_final_evaluation import RecordingRunner
from tests.evaluation.test_run_metrics import mark_resumed
from tests.evaluation.test_workflow import export_folder, mark_export_resumed


def contents(folder):
    return {path.relative_to(folder): path.read_bytes() for path in folder.rglob('*') if path.is_file()}


@pytest.mark.parametrize('status', ['HUMAN_REVIEW', 'WAITING_INPUT'])
def test_first_direct_record_freezes_paused_evidence_before_any_workflow_ingest(tmp_path, status):
    source, _ = export_folder(tmp_path / 'export', status='HUMAN_REVIEW', versions=())
    export, _ = load_handoff(source)
    original = export['bundle']
    original['run'].update(status=status, verdict='HUMAN_REVIEW' if status == 'HUMAN_REVIEW' else None)
    target = record_run(tmp_path / 'results', original)
    before = contents(target)
    changed = mark_resumed(copy.deepcopy(original))
    changed['run'].update(status='FINISHED', verdict='SUCCESS')
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        record_run(tmp_path / 'results', changed, force=True)
    assert contents(target) == before
    assert record_run(tmp_path / 'results', original, force=True) == target


def test_first_direct_score_freezes_paused_evidence_and_allows_identical_regrade(tmp_path):
    source, _ = export_folder(tmp_path / 'export', status='HUMAN_REVIEW')
    export, _ = load_handoff(source)
    results = tmp_path / 'results'
    target = score_export(source, results=results, runner=RecordingRunner(), exam='final',
                         contract=run_contract(export), bundle=export['bundle'])
    assert score_export(source, results=results, runner=RecordingRunner(), exam='final',
                        contract=run_contract(export), force=True) == target
    assert json.loads((target / 'automatic_boundary.json').read_text())['automaticSegmentClosed'] is True
    before = contents(target)
    mark_export_resumed(source)
    changed, _ = load_handoff(source)
    runner = RecordingRunner()
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        score_export(source, results=results, runner=runner, exam='final', contract=run_contract(export),
                     bundle=changed['bundle'], force=True)
    assert runner.calls == 0 and contents(target) == before


def test_direct_score_without_bundle_preserves_the_paused_snapshot_identity(tmp_path):
    source, _ = export_folder(tmp_path / 'export', status='HUMAN_REVIEW')
    export, _ = load_handoff(source)
    results = tmp_path / 'results'
    arguments = dict(results=results, runner=RecordingRunner(), exam='final', contract=run_contract(export))
    target = score_export(source, **arguments)
    assert score_export(source, **arguments, force=True) == target
    before = contents(target)
    run_path = source / 'run.json'
    resumed = json.loads(run_path.read_text())
    resumed.update(status='FINISHED', verdict='SUCCESS')
    run_path.write_text(json.dumps(resumed))
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        score_export(source, **arguments, force=True)
    assert contents(target) == before


def test_legacy_direct_record_without_marker_is_still_a_closed_automatic_segment(tmp_path):
    source, _ = export_folder(tmp_path / 'export', status='HUMAN_REVIEW', versions=())
    export, _ = load_handoff(source)
    target = record_run(tmp_path / 'results', export['bundle'])
    (target / 'automatic_boundary.json').unlink(missing_ok=True)
    changed = copy.deepcopy(export['bundle'])
    changed['run']['updatedAt'] = '2026-12-01T00:00:00Z'
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        record_run(tmp_path / 'results', changed, force=True)
