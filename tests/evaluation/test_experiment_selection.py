"""Keep all four experiment arms in place while comparing one controlled axis."""
import json
import shutil
from uuid import uuid4

import pytest

from evaluation import experiment_run as er
from evaluation.comparison_report import render_comparison
from evaluation.experiment import ExperimentError, compare, main
from orchestrator.domain.run_configuration import RunConfiguration
from tests.evaluation.test_experiment import keep, pt, real_report
from tests.evaluation.test_run_metrics import uid

ARCHITECTURES = ('SINGLE_AGENT', 'MULTI_AGENT')
POLICIES = ('IN_LOOP_AND_FINAL', 'FINAL_ONLY')


def matrix(root):
    folders = {}
    for version, policy in enumerate(POLICIES, 1):
        for order, architecture in enumerate(ARCHITECTURES, 1):
            run_id = uid(700 + version * 10 + order)
            folder = keep(root, run_id, pt.loop_suite_ref() if version == 1 else None,
                real_report(run_id, 'pilot', architecture=architecture),
                real_report(run_id, 'practice', architecture=architecture),
                exam='pilot', pair='repetition-1', architecture=architecture)
            bundle_path = folder / 'bundle.json'
            data = json.loads(bundle_path.read_text())
            data['configuration']['configuration']['executionOrder'] = order
            bundle_path.write_text(json.dumps(data))
            folders[policy, architecture] = folder
    return folders


def journal(root, folders):
    for (policy, architecture), folder in folders.items():
        data = json.loads((folder / 'bundle.json').read_text())
        config = RunConfiguration.model_validate(data['configuration']['configuration'])
        plan = {'experimentId': str(config.experiment_id), 'loopPolicy': policy}
        row = {'order': config.execution_order, 'architecture': architecture, 'repetition': 1}
        fields = dict(fingerprint='f' * 64)
        er._record(root, plan, row, 'SUBMITTING', **fields, intentAt='2026-10-14T09:59:00+09:00',
                   submissionSha256='a' * 64,
                   configurationSha256=er._hash(config.model_dump(mode='json', by_alias=True)),
                   hostRequestSha256='b' * 64, idempotencyKey=str(uuid4()))  # 기록 형식 3(dev 85번 중복 방지 키)
        er._record(root, plan, row, 'SUBMITTED', **fields, runId=data['run']['runId'])
        er._record(root, plan, row, 'ENDED', **fields, runId=data['run']['runId'], status='FINISHED')


def parity(path, group_by, folders):
    path.write_text(json.dumps({'status': 'PASS', 'groupBy': group_by, 'pinnedPolicyChecked': True,
        'checks': [{'check': 'pinnedPolicy', 'status': 'PASS', 'runId': folder.name} for folder in folders]}))


@pytest.mark.parametrize('policy', POLICIES)
def test_select_version_compares_single_multi_without_moving_any_result(tmp_path, policy):
    folders = matrix(tmp_path)
    selected = [folder for (version, _), folder in folders.items() if version == policy]
    proof = tmp_path / 'parity-selected.json'
    parity(proof, 'architecture', selected)
    result = compare(tmp_path, dict.fromkeys(ARCHITECTURES, 1), 'architecture',
                     loop_policy=policy, parity_file=proof)
    assert result['validity']['valid'] and result['readiness']['status'] == 'PASS'
    assert {row['runId'] for row in result['runs']} == {folder.name for folder in selected}
    assert len(result['pairComparison']['pairs']) == 1
    assert result['pairComparison']['unpairedRuns'] == []
    assert len(result['selection']['excludedRunIds']) == 2
    assert all(folder.is_dir() for folder in folders.values())
    assert f'loopPolicy = {policy}' in render_comparison(result)


@pytest.mark.parametrize('architecture', ARCHITECTURES)
def test_select_architecture_compares_versions_and_reuses_registered_pair(tmp_path, architecture):
    folders = matrix(tmp_path)
    selected = [folder for (_, mode), folder in folders.items() if mode == architecture]
    proof = tmp_path / 'parity-selected.json'
    parity(proof, 'loopPolicy', selected)
    result = compare(tmp_path, dict.fromkeys(POLICIES, 1), 'loopPolicy',
                     architecture=architecture, parity_file=proof)
    assert result['validity']['valid'] and not result['validity']['violations']
    assert result['readiness']['status'] == 'PASS'
    assert result['practiceEffect']['practiceEffect'] == 0
    assert {row['runId'] for row in result['runs']} == {folder.name for folder in selected}


@pytest.mark.parametrize('status,verdict', [('FINISHED', 'FAIL'), ('HUMAN_REVIEW', 'HUMAN_REVIEW'), ('ABORTED', None)])
def test_selection_preserves_failed_paused_and_aborted_costs_and_denominators(tmp_path, status, verdict):
    folders = matrix(tmp_path)
    folder = folders[POLICIES[0], 'MULTI_AGENT']
    (folder / 'output.json').unlink()
    path = folder / 'bundle.json'
    data = json.loads(path.read_text())
    data['run'].update(status=status, verdict=verdict)
    data['usage']['summary'].update(costUsd='7.5', knownCostUsd='7.5')
    path.write_text(json.dumps(data))
    result = compare(tmp_path, dict.fromkeys(ARCHITECTURES, 1), 'architecture', loop_policy=POLICIES[0])
    group = result['summary']['MULTI_AGENT']
    assert group['recordedRuns'] == group['plannedRuns'] == 1
    assert group['costUsd'] == group['knownCostUsd'] == '7.5'
    assert group['evaluation']['qualifiedRuns'] == 0 and group['evaluation']['qualifiedRate'] == 0
    assert folder.name in result['unscoredRuns']
    assert next(row for row in result['runs'] if row['runId'] == folder.name)['workflowStatus'] == status


@pytest.mark.parametrize('axis,selection,missing_key,group', [
    ('architecture', {'loop_policy': POLICIES[0]}, (POLICIES[0], 'SINGLE_AGENT'), 'SINGLE_AGENT'),
    ('loopPolicy', {'architecture': 'SINGLE_AGENT'}, (POLICIES[1], 'SINGLE_AGENT'), POLICIES[1]),
])
def test_selection_keeps_accepted_missing_export_and_unknown_planned_slot(tmp_path, axis, selection, missing_key, group):
    results, execution = tmp_path / 'results', tmp_path / 'execution'
    folders = matrix(results)
    journal(execution, folders)
    missing_id = folders[missing_key].name
    shutil.rmtree(folders[missing_key])
    planned = dict.fromkeys(ARCHITECTURES if axis == 'architecture' else POLICIES, 1)
    planned[group] = 2  # one accepted but uncollected Run, one slot with no execution evidence
    result = compare(results, planned, axis, run_journals=[execution], **selection)
    measured = result['summary'][group]
    assert measured['plannedRuns'] == measured['unrecordedRuns'] == 2
    assert measured['startedWithoutResults'] == measured['unknownExecutionSlots'] == 1
    assert measured['costUsd'] is None and measured['successRate'] == 0
    assert measured['notStarted'] is None
    assert result['selection']['excludedExecutionSlots'] == 2
    assert len(result['executionSlots']) == 2
    assert any(row['runId'] == missing_id and row['scoring'] == 'RESULT_NOT_COLLECTED' for row in result['runs'])
    assert 'PLANNED_SLOT_RESULTS_NOT_COLLECTED' in result['readiness']['blockers']


def test_selection_cannot_hide_a_contradictory_loop_label(tmp_path):
    folders = matrix(tmp_path)
    folder = folders[POLICIES[0], 'MULTI_AGENT']
    path = folder / 'meta.json'
    meta = json.loads(path.read_text()); meta['loopPolicy'] = POLICIES[1]
    path.write_text(json.dumps(meta))
    result = compare(tmp_path, dict.fromkeys(ARCHITECTURES, 1), 'architecture', loop_policy=POLICIES[0])
    assert folder.name in {row['runId'] for row in result['runs']}
    assert not result['validity']['valid']
    assert any(row['field'] == 'loopPolicy' for row in result['validity']['violations'])


def test_selection_does_not_hide_an_unselected_journal_identity_conflict(tmp_path):
    results, execution = tmp_path / 'results', tmp_path / 'execution'
    folders = matrix(results); journal(execution, folders)
    path = folders[POLICIES[1], 'MULTI_AGENT'] / 'bundle.json'
    data = json.loads(path.read_text())
    data['configuration']['configuration']['executionOrder'] = 99
    path.write_text(json.dumps(data))
    with pytest.raises(ExperimentError, match='EXECUTION_JOURNAL_MISMATCH'):
        compare(results, dict.fromkeys(ARCHITECTURES, 1), 'architecture',
                loop_policy=POLICIES[0], run_journals=[execution])


def test_cli_selection_and_distinct_parity_reports_keep_outputs_separate(tmp_path):
    folders = matrix(tmp_path / 'results')
    proof = tmp_path / 'parity-ver1.json'
    parity(proof, 'architecture', [folder for (policy, _), folder in folders.items() if policy == POLICIES[0]])
    out = tmp_path / 'comparisons' / 'ver1'
    assert main(['compare', '--results', str(tmp_path / 'results'), '--group-by', 'architecture',
                 '--loop-policy', POLICIES[0], '--planned', 'SINGLE_AGENT=1', '--planned', 'MULTI_AGENT=1',
                 '--parity', str(proof), '--out', str(out)]) == 0
    report = json.loads((out / 'comparison.json').read_text())
    assert report['readiness']['status'] == 'PASS' and report['toolParity']['source'] == proof.name
    assert 'loopPolicy = IN_LOOP_AND_FINAL' in (out / 'comparison.md').read_text()


@pytest.mark.parametrize('axis,selection', [
    ('architecture', {'architecture': 'SINGLE_AGENT'}), ('loopPolicy', {'loop_policy': 'FINAL_ONLY'}),
    ('architecture', {'loop_policy': 'UNKNOWN'}), ('loopPolicy', {'architecture': 'UNKNOWN'}),
])
def test_selection_cannot_filter_the_compared_axis(tmp_path, axis, selection):
    with pytest.raises(ExperimentError, match='COMPARISON_SELECTION_INVALID'):
        compare(tmp_path, {'SINGLE_AGENT': 1}, axis, **selection)
