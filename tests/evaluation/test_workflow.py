"""Host 인계 파일을 통째로 받아 결과·Agent 표를 만드는 경로. 소스는 신뢰된 합성 서비스만 쓴다."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path

import pytest

from evaluation.experiment import ExperimentError
from evaluation.workflow import ingest, load_handoff
from tests.evaluation.test_agent_eval import run as agent_run
from tests.evaluation.test_final_evaluation import RUNTIME, RecordingRunner, VerifiedImageRunner, manifest, snapshot
from tests.evaluation.test_run_metrics import bundle as metrics_bundle, uid


def export_folder(root, *, status='FINISHED', versions=(1,), name='single'):
    root.mkdir()
    export = agent_run(name)
    run_id = export['bundle']['run']['runId']
    architecture = export['handoff']['architecture']
    original = export['bundle']
    merged = metrics_bundle(run_id, architecture=architecture, fixes=original['run']['codeVersion'] - 1)
    merged['run'].update(original['run'], status=status)
    if status in ('ABORTED', 'HUMAN_REVIEW'):
        merged['run']['verdict'] = 'HUMAN_REVIEW' if status == 'HUMAN_REVIEW' else None
    merged['issues'] = original['issues']
    export['bundle'] = merged
    export['trace']['runId'] = run_id
    export['a2a']['runId'] = run_id
    export['a2a']['missingRoles'] = []
    for rows in export['a2a']['roles'].values():
        for key in ('contexts', 'tasks', 'history', 'receipts', 'events'):
            rows.setdefault(key, [])
    export['handoff'].update(runStatus=status, orchestratorVerdict=merged['run']['verdict'])
    final_version = merged['run']['codeVersion']
    docs = {'run.json': {'runId': run_id, 'status': status, 'verdict': merged['run']['verdict'], 'finalCodeVersion': final_version,
                         'exportedCodeVersions': list(versions)},
            'bundle.json': merged, 'trace.json': export['trace'], 'a2a-messages.json': export['a2a']}
    for name_, value in docs.items():
        (root / name_).write_text(json.dumps(value))
    for version in versions:
        archive = snapshot('plaintext_storage' if version < final_version else 'good')
        folder = root / f'v{version}'
        folder.mkdir()
        (folder / 'source.tar').write_bytes(archive)
        (folder / 'manifest.json').write_text(json.dumps(replace(manifest(archive), code_version=version).to_payload()))
    export['handoff']['files'] = {
        path.relative_to(root).as_posix(): {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                                           'sizeBytes': path.stat().st_size}
        for path in root.rglob('*') if path.is_file()}
    (root / 'handoff.json').write_text(json.dumps(export['handoff']))
    return root, run_id


@pytest.mark.skipif(not RUNTIME, reason='합성 회원가입 서비스 실행 의존성이 필요하다')
@pytest.mark.parametrize('name,versions', [('single', (1,)), ('multiFix', (1, 2))])
def test_complete_handoff_produces_grade_tables_and_version_evidence(tmp_path, name, versions):
    folder, run_id = export_folder(tmp_path / 'export', name=name, versions=versions)
    secret = 'fictional-workflow-canary-456'
    extra = {'runId': run_id, 'scope': 'DECLARED_FILES_ONLY', 'items': [
        {'kind': 'AGENT_PROCESS_OUTPUT', 'sourceId': 'f' * 64, 'status': 'COLLECTED', 'text': secret}]}
    runner = VerifiedImageRunner()
    from tests.evaluation.test_review_repeatability import review_for
    from tests.evaluation.test_evidence_metrics import process_case
    export, _ = load_handoff(folder)
    review, process = review_for(export), process_case()
    process['runId'] = run_id
    for row in process['events']:
        row['runId'] = run_id
    target = ingest(folder, results=tmp_path / 'results', runner=runner, exam='pilot',
                    pair_id='pair-1', known_values=[secret], additional_outputs=extra,
                    planner_review=review, process_evidence=process)
    output = json.loads((target / 'output.json').read_text())
    assert output['report']['sourceRunId'] == run_id
    assert output['report']['expectedManifest']['codeVersion'] == versions[-1]
    assert output['trace']['exposure']['status'] == 'FAIL'
    assert json.loads((target / 'agent_tables.json').read_text())['runId'] == run_id
    assert (target / 'agent_tables.md').is_file() and (target / 'summary.md').is_file()
    assert (target / 'regression.json').is_file()
    assert (target / 'timing_metrics.json').is_file()
    assert json.loads((target / 'planner_review.json').read_text()) == review
    assert json.loads((target / 'process_metrics.json').read_text())['repairs']['roundCount'] == 1
    row = next(r for r in json.loads((target / 'agent_tables.json').read_text())['accuracy']
               if r['item'].startswith('계획 내용 타당성'))
    assert row['status'] == 'PASS' and row['detail'] == review
    if len(versions) > 1:
        assert (target / 'versions/v1.json').is_file()
    assert json.loads((target / 'meta.json').read_text())['pairId'] == 'pair-1'
    assert secret not in ''.join(path.read_text() for path in target.rglob('*') if path.is_file())
    with pytest.raises(ExperimentError, match='OUTPUT_EXISTS'):
        ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='pilot')


def test_no_source_keeps_run_cost_and_never_invents_grade(tmp_path):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='ABORTED')
    runner = RecordingRunner()
    target = ingest(folder, results=tmp_path / 'results', runner=runner, exam='final')
    assert runner.calls == 0 and not (target / 'output.json').exists()
    assert json.loads((target / 'bundle.json').read_text())['usage']['summary']['costUsd'] == '0.5'
    assert json.loads((target / 'evaluation_status.json').read_text()) == {
        'runId': run_id, 'runStatus': 'ABORTED', 'automaticSegmentClosed': False,
        'status': 'UNVERIFIED', 'reason': 'NO_ARTIFACT', 'cause': 'ABORTED', 'comparisonValidity': 'VALID',
        'resume': 'TERMINAL', 'autoCompletion': 'FAIL'}
    outcome = json.loads((target / 'outcome.json').read_text())
    assert outcome['runId'] == run_id and outcome['autoCompletion']['conditions']['internalSuccess']['status'] == 'FAIL'


@pytest.mark.parametrize('kind', ['scored', 'unscored', 'assisted'])
def test_incomplete_workflow_result_is_not_published_and_can_be_retried(tmp_path, monkeypatch, kind):
    from evaluation import workflow
    folder, run_id = export_folder(tmp_path / 'export', versions=() if kind == 'unscored' else (1,),
                                   status='ABORTED' if kind == 'unscored' else 'FINISHED')
    if kind == 'assisted':
        mark_export_resumed(folder)
    results = tmp_path / 'results'
    write = workflow._write
    failure_name = 'evaluation_status.json' if kind == 'assisted' else 'process_metrics.json'

    def failing_write(path, *args):
        if path.name == failure_name:
            raise OSError('injected final workflow write failure')
        return write(path, *args)

    monkeypatch.setattr(workflow, '_write', failing_write)
    with pytest.raises(OSError, match='final workflow write'):
        ingest(folder, results=results, runner=RecordingRunner(), exam='final')
    assert not (results / run_id).exists()
    assert not list(results.glob('.*.staging-*'))
    monkeypatch.setattr(workflow, '_write', write)
    target = ingest(folder, results=results, runner=RecordingRunner(), exam='final')
    expected = {'scored': 'SCORED', 'unscored': 'UNVERIFIED', 'assisted': 'QUARANTINED_ASSISTED'}[kind]
    assert json.loads((target / 'evaluation_status.json').read_text())['status'] == expected
    assert (target / 'outcome.json').is_file()
    assert (target / 'process_metrics.json').is_file() == (kind != 'assisted')


def test_workflow_completion_failure_preserves_existing_record_and_pair_metadata(tmp_path, monkeypatch):
    from evaluation import workflow
    from evaluation.experiment import record_run
    folder, run_id = export_folder(tmp_path / 'export')
    export, _ = load_handoff(folder)
    results = tmp_path / 'results'
    target = record_run(results, export['bundle'], pair_id='preserved-pair', loop_policy='FINAL_ONLY')
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    write = workflow._write

    def failing_write(path, *args):
        if path.name == 'process_metrics.json':
            raise OSError('injected supplement write failure')
        return write(path, *args)

    monkeypatch.setattr(workflow, '_write', failing_write)
    with pytest.raises(OSError, match='supplement write'):
        ingest(folder, results=results, runner=RecordingRunner(), exam='final')
    assert before == {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    monkeypatch.setattr(workflow, '_write', write)
    assert ingest(folder, results=results, runner=RecordingRunner(), exam='final') == target
    assert json.loads((target / 'meta.json').read_text())['pairId'] == 'preserved-pair'
    assert (target / 'output.json').is_file() and (target / 'process_metrics.json').is_file()


def test_direct_record_force_cannot_replace_a_closed_automatic_segment(tmp_path):
    from evaluation.experiment import record_run
    from tests.evaluation.test_run_metrics import mark_resumed
    folder, run_id = export_folder(tmp_path / 'export', status='HUMAN_REVIEW', versions=())
    target = ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='final', pair_id='p1')
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    original = json.loads((target / 'bundle.json').read_text())
    assert record_run(tmp_path / 'results', original, force=True) == target
    assert before == {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    changed = mark_resumed(copy.deepcopy(original))
    changed['run'].update(status='FINISHED', verdict='SUCCESS')
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        record_run(tmp_path / 'results', changed, force=True)
    assert before == {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}


def test_force_regrade_same_boundary_keeps_marker_and_rejects_later_resumed_export(tmp_path):
    from evaluation.experiment import score_export
    from evaluation.agent_eval import run_contract
    folder, run_id = export_folder(tmp_path / 'export', status='HUMAN_REVIEW')
    results = tmp_path / 'results'
    target = ingest(folder, results=results, runner=RecordingRunner(), exam='final', pair_id='p1')
    export, _ = load_handoff(folder)
    contract = run_contract(export)
    # A direct, explicit regrade can inherit the same immutable boundary bundle.
    assert score_export(folder, results=results, runner=RecordingRunner(), exam='final', contract=contract,
                        force=True) == target
    marker = json.loads((target / 'automatic_boundary.json').read_text())
    assert marker['automaticSegmentClosed'] is True and marker['runStatus'] == 'HUMAN_REVIEW'
    assert json.loads((target / 'meta.json').read_text())['pairId'] == 'p1'
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    mark_export_resumed(folder)
    changed, _ = load_handoff(folder)
    runner = RecordingRunner()
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        score_export(folder, results=results, runner=runner, exam='final', contract=contract,
                     bundle=changed['bundle'], force=True)
    assert runner.calls == 0
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        ingest(folder, results=results, runner=runner, exam='final')
    assert before == {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}


def test_force_regrade_cannot_change_the_closed_boundary_snapshot(tmp_path):
    from evaluation.experiment import score_export
    from evaluation.agent_eval import run_contract
    folder, run_id = export_folder(tmp_path / 'export', status='HUMAN_REVIEW')
    results = tmp_path / 'results'
    target = ingest(folder, results=results, runner=RecordingRunner(), exam='final')
    export, _ = load_handoff(folder)
    before = (target / 'output.json').read_bytes()
    archive = snapshot('plaintext_storage')
    (folder / 'v1/source.tar').write_bytes(archive)
    (folder / 'v1/manifest.json').write_text(json.dumps(manifest(archive).to_payload()))
    runner = RecordingRunner()
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        score_export(folder, results=results, runner=runner, exam='final', contract=run_contract(export),
                     bundle=export['bundle'], force=True)
    assert runner.calls == 0 and (target / 'output.json').read_bytes() == before


@pytest.mark.parametrize('damage', ['file', 'run_id', 'architecture', 'version', 'status'])
def test_changed_or_mixed_handoff_is_rejected_before_running(tmp_path, damage):
    folder, _ = export_folder(tmp_path / 'export')
    if damage == 'file':
        (folder / 'v1/source.tar').write_bytes(b'changed')
    else:
        handoff = json.loads((folder / 'handoff.json').read_text())
        if damage == 'run_id':
            handoff['runId'] = uid(999)
        elif damage == 'architecture':
            handoff['architecture'] = 'MULTI_AGENT'
        elif damage == 'status':
            handoff['runStatus'] = 'ABORTED'
        else:
            data = json.loads((folder / 'run.json').read_text())
            data['finalCodeVersion'] = 4
            (folder / 'run.json').write_text(json.dumps(data))
            raw = (folder / 'run.json').read_bytes()
            handoff['files']['run.json'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'sizeBytes': len(raw)}
        (folder / 'handoff.json').write_text(json.dumps(handoff))
    with pytest.raises(ExperimentError, match='HANDOFF_INVALID_OR_CHANGED'):
        load_handoff(folder)


@pytest.mark.parametrize('status', ['IMPLEMENTING', 'VALIDATING'])
def test_active_run_is_not_accepted_as_final(tmp_path, status):
    folder, _ = export_folder(tmp_path / 'export', status=status)
    runner = RecordingRunner()
    with pytest.raises(ExperimentError, match='RUN_NOT_FINISHED'):
        ingest(folder, results=tmp_path / 'results', runner=runner, exam='final')
    assert runner.calls == 0


def test_human_review_closes_automatic_segment_and_cannot_be_replaced_by_resumed_run(tmp_path):
    folder, run_id = export_folder(tmp_path / 'export', versions=(), status='HUMAN_REVIEW')
    target = ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='final')
    assert json.loads((target / 'evaluation_status.json').read_text())['automaticSegmentClosed'] is True
    before = {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}
    resumed, same_id = export_folder(tmp_path / 'resumed', versions=(), status='FINISHED')
    assert same_id == run_id
    mark_export_resumed(resumed)
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        ingest(resumed, results=tmp_path / 'results', runner=RecordingRunner(), exam='final')
    assert before == {path.relative_to(target): path.read_bytes() for path in target.rglob('*') if path.is_file()}


def mark_export_resumed(folder):
    from tests.evaluation.test_run_metrics import mark_resumed
    path = folder / 'bundle.json'
    path.write_text(json.dumps(mark_resumed(json.loads(path.read_text()))))
    handoff = json.loads((folder / 'handoff.json').read_text())
    raw = path.read_bytes()
    handoff['files']['bundle.json'] = {'sha256': hashlib.sha256(raw).hexdigest(), 'sizeBytes': len(raw)}
    (folder / 'handoff.json').write_text(json.dumps(handoff))


def test_first_export_after_resume_is_quarantined_without_executing_tests(tmp_path):
    folder, run_id = export_folder(tmp_path / 'export')
    mark_export_resumed(folder)
    runner = RecordingRunner()
    target = ingest(folder, results=tmp_path / 'results', runner=runner, exam='final', pair_id='p1')
    assert runner.calls == 0 and not (target / 'output.json').exists()
    assert not (target / 'agent_tables.json').exists() and (target / 'assisted_agent_tables.json').is_file()
    status = json.loads((target / 'evaluation_status.json').read_text())
    assert (status['status'], status['comparisonValidity'], status['autoCompletion']) == ('QUARANTINED_ASSISTED', 'ASSISTED', 'FAIL')
    assert status['automaticSegmentClosed'] is True
    diagnostic = json.loads((target / 'assisted_diagnostics.json').read_text())
    assert diagnostic['scope'] == 'FULL_RUN_INCLUDING_ASSISTED_CONTINUATION'
    assert diagnostic['automaticBoundary'] == {'status': 'UNVERIFIED', 'reason': 'AUTOMATIC_BOUNDARY_NOT_CAPTURED'}
    assert diagnostic['metrics']['knownCostUsd'] == '0.5'
    assert json.loads((target / 'bundle.json').read_text())['usage']['summary']['costUsd'] == '0.5'
    from evaluation.experiment import compare
    result = compare(tmp_path / 'results', {'SINGLE_AGENT': 1}, group_by='architecture')
    group = result['summary']['SINGLE_AGENT']
    assert (group['recordedRuns'], group['notStarted'], group['quarantinedAssistedRuns']) == (1, 0, 1)
    assert group['costUsd'] is None and group['knownCostUsd'] is None
    assert group['evaluation']['qualifiedRate'] == 0 and group['accuracy']['meanFullSatisfactionLower'] is None
    assert result['quarantinedAssistedRuns'] == [run_id]


@pytest.mark.skipif(not RUNTIME, reason='합성 회원가입 서비스 실행 의존성이 필요하다')
def test_human_review_snapshot_is_scored_at_automatic_boundary(tmp_path):
    folder, _ = export_folder(tmp_path / 'export', status='HUMAN_REVIEW')
    target = ingest(folder, results=tmp_path / 'results', runner=VerifiedImageRunner(), exam='pilot')
    assert (target / 'output.json').is_file()
    assert json.loads((target / 'evaluation_status.json').read_text())['automaticSegmentClosed'] is True
    assert json.loads((target / 'bundle.json').read_text())['run']['verdict'] == 'HUMAN_REVIEW'


def test_missing_earlier_snapshot_is_detected_before_any_execution(tmp_path):
    folder, _ = export_folder(tmp_path / 'export', name='multiFix', versions=(2,))
    runner = RecordingRunner()
    with pytest.raises(ExperimentError, match='SNAPSHOT_HISTORY_INCOMPLETE'):
        ingest(folder, results=tmp_path / 'results', runner=runner, exam='final')
    assert runner.calls == 0


# ---------------------------------------------------------------- 최신 dev(ea06d12) 실제 기록으로 보는 끝 상태
# 고정 기록은 dev 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로 만든 Run을 evaluation_export로 꺼낸 것이다.

REAL = json.loads((Path(__file__).parent / 'fixtures/dev_outcome_runs.json').read_text(encoding='utf-8'))


def real_export_folder(root, name, **run_changes):
    # 실제 dev 내보내기 모양(코드 없이 멈춘 Run은 finalCodeVersion이 null) 그대로 인계 폴더를 만든다.
    root.mkdir()
    data = copy.deepcopy(REAL[name])
    data['bundle']['run'].update(run_changes)
    data['run'].update({key: value for key, value in run_changes.items() if key in ('status', 'verdict')})
    data['handoff'].update(runStatus=data['bundle']['run']['status'], orchestratorVerdict=data['bundle']['run']['verdict'])
    docs = {'run.json': data['run'], 'bundle.json': data['bundle'], 'trace.json': data['trace'],
            'a2a-messages.json': data['a2a']}
    for file_name, value in docs.items():
        (root / file_name).write_text(json.dumps(value))
    data['handoff']['files'] = {
        path.name: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'sizeBytes': path.stat().st_size}
        for path in root.iterdir() if path.is_file()}
    (root / 'handoff.json').write_text(json.dumps(data['handoff']))
    return root, data['run']['runId']


@pytest.mark.parametrize('name,status,cause,resume', [
    ('plannerSemanticHold', 'WAITING_INPUT', 'SEMANTIC_REVIEW_HOLD', 'ANSWER_THEN_REVIEW'),
    ('plannerQuestion', 'WAITING_INPUT', 'PLANNER_QUESTION', 'ANSWER'),
    ('developerSemanticHold', 'HUMAN_REVIEW', 'SEMANTIC_REVIEW_HOLD', 'NEW_RUN_REQUIRED'),
    ('reviewProviderError', 'HUMAN_REVIEW', 'MODEL_API_FAILURE', 'OPERATOR_DECISION'),
    ('budgetExhausted', 'HUMAN_REVIEW', 'BUDGET_EXHAUSTED', 'OPERATOR_DECISION'),
    ('abortedDuringReview', 'ABORTED', 'ABORTED', 'TERMINAL'),
])
def test_real_runs_stopped_before_any_code_are_kept_with_their_cause(tmp_path, name, status, cause, resume):
    # 회귀: finalCodeVersion=null을 형식 오류로 거부하면 코드 전에 멈춘 Run이 결과에서 사라진다.
    folder, run_id = real_export_folder(tmp_path / 'export', name)
    runner = RecordingRunner()
    target = ingest(folder, results=tmp_path / 'results', runner=runner, exam='final')
    assert runner.calls == 0 and not (target / 'output.json').exists()
    state = json.loads((target / 'evaluation_status.json').read_text())
    assert (state['runStatus'], state['status'], state['reason']) == (status, 'UNVERIFIED', 'NO_ARTIFACT')
    assert (state['cause'], state['resume'], state['autoCompletion']) == (cause, resume, 'FAIL')
    assert state['automaticSegmentClosed'] is (status in ('WAITING_INPUT', 'HUMAN_REVIEW'))
    outcome = json.loads((target / 'outcome.json').read_text())
    assert outcome['outcome']['cause'] == cause and outcome['semanticReview'] is not None
    assert json.loads((target / 'agent_tables.json').read_text())['runId'] == run_id


def test_waiting_input_segment_cannot_be_replaced_by_a_resumed_run(tmp_path):
    folder, run_id = real_export_folder(tmp_path / 'export', 'plannerSemanticHold')
    ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='final')
    resumed, same_id = real_export_folder(tmp_path / 'resumed', 'plannerSemanticHold', status='FINISHED', verdict='FAIL')
    assert same_id == run_id
    with pytest.raises(ExperimentError, match='AUTOMATIC_SEGMENT_ALREADY_CLOSED'):
        ingest(resumed, results=tmp_path / 'results', runner=RecordingRunner(), exam='final')


@pytest.mark.parametrize('final,versions', [(None, [1]), ('1', []), (5, []), (True, [])])
def test_null_final_version_is_only_accepted_without_exported_code(tmp_path, final, versions):
    folder, _ = real_export_folder(tmp_path / 'export', 'plannerQuestion')
    data = json.loads((folder / 'run.json').read_text())
    data.update(finalCodeVersion=final, exportedCodeVersions=versions)
    (folder / 'run.json').write_text(json.dumps(data))
    bundle = json.loads((folder / 'bundle.json').read_text())
    bundle['run']['codeVersion'] = final
    (folder / 'bundle.json').write_text(json.dumps(bundle))
    handoff = json.loads((folder / 'handoff.json').read_text())
    for file_name in ('run.json', 'bundle.json'):
        raw = (folder / file_name).read_bytes()
        handoff['files'][file_name] = {'sha256': hashlib.sha256(raw).hexdigest(), 'sizeBytes': len(raw)}
    (folder / 'handoff.json').write_text(json.dumps(handoff))
    with pytest.raises(ExperimentError, match='EXPORTED_VERSIONS_INVALID|BUNDLE_INVALID'):
        ingest(folder, results=tmp_path / 'results', runner=RecordingRunner(), exam='final')


def test_real_stopped_runs_flow_into_the_comparison_report(tmp_path):
    # 실제 dev 기록(보류·API 장애·중단) → workflow → compare 명령 → comparison.json·md. 계획 4개 중 1개는 시작 못 함.
    from evaluation.experiment import main as experiment_main
    results = tmp_path / 'results'
    ids = {}
    for name in ('plannerSemanticHold', 'reviewProviderError', 'abortedDuringReview'):
        folder, ids[name] = real_export_folder(tmp_path / name, name)
        ingest(folder, results=results, runner=RecordingRunner(), exam='final')
    assert experiment_main(['compare', '--results', str(results), '--group-by', 'architecture',
                            '--planned', 'MULTI_AGENT=4']) == 0
    result = json.loads((results / 'comparison.json').read_text())
    group = result['summary']['MULTI_AGENT']
    assert group['notStarted'] is None and group['unrecordedRuns'] == 1 and group['autoCompletion'] == {'pass': 0, 'fail': 3, 'unverified': 0,
                                                                    'notRecorded': 0, 'rate': 0.0}
    assert group['outcomes']['causes'] == {'SEMANTIC_REVIEW_HOLD': 1, 'MODEL_API_FAILURE': 1, 'ABORTED': 1}
    assert group['outcomes']['environmentFailureRuns'] == [ids['reviewProviderError']]
    assert group['semanticReview']['held'] == 3 and group['stability']['pausedRuns'] == {'HUMAN_REVIEW': 1, 'WAITING_INPUT': 1}
    assert {row['runId']: row['cause'] for row in result['runs']} == {
        ids['plannerSemanticHold']: 'SEMANTIC_REVIEW_HOLD', ids['reviewProviderError']: 'MODEL_API_FAILURE',
        ids['abortedDuringReview']: 'ABORTED'}
    text = (results / 'comparison.md').read_text()
    assert '| 의미 검토 보류 | 1 |' in text and '| 모델 API 장애 | 1 |' in text and '| 중단 | 1 |' in text
    assert '| 환경 장애로 미검증 처리할 Run 수 | 1 |' in text and '| 자동 완료율(STAB-06) | 0.0 |' in text
