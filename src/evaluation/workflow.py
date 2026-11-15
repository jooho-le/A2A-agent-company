"""Host 인계 폴더 → 최종 채점 + Agent 평가표. 모델 호출·개발 Run 실행은 하지 않는다."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

from .agent_eval import agent_tables, render_markdown, run_contract
from .experiment import ExperimentError, _check_automatic_boundary, _checked_metrics, _write, record_run, score_export
from .final_evaluation import DockerRunner
from .outcomes import CLOSED_STATUSES, PAUSED_STATUSES, has_resumed, run_outcome
from .output_evidence import _read_file, collect_output_evidence, read_known_values


def load_handoff(folder):
    """수집기가 남긴 파일 목록·해시와 Run 정체성을 먼저 대조한다."""
    root = Path(folder).resolve(strict=True)
    try:
        handoff = json.loads(_read_file(root, 'handoff.json', 16 * 1024 * 1024))
        files = handoff['files']
        needed = {'run.json', 'bundle.json', 'trace.json', 'a2a-messages.json'}
        if not isinstance(files, dict) or not needed <= files.keys() or len(files) > 4096:
            raise ValueError()
        documents, snapshots, observed_files = {}, {}, []
        for name, expected in files.items():
            data = _read_file(root, name, 256 * 1024 * 1024)
            if len(data) != expected['sizeBytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
                raise ValueError()
            if name in needed or name == 'package-completion.json' or (
                    'package-completion.json' in files and name.endswith('.json')):
                documents[name] = json.loads(data)
            if name.startswith('v') and name.endswith('/source.tar') and name.count('/') == 1:
                version = int(name[1:name.index('/')])
                from .final_evaluation import read_snapshot
                snapshots[version] = read_snapshot(data)
            if name.startswith('observed-files/'):
                observed_files.append({'kind': 'ARTIFACT_FILE_CONTENT', 'sourceId': hashlib.sha256(name.encode()).hexdigest(),
                    'sha256': expected['sha256'], 'bytes': len(data), 'status': 'COLLECTED', 'error': None,
                    'text': data.decode('utf-8', errors='replace')})
        run_id = handoff['runId']
        if (documents['run.json']['runId'] != run_id or documents['bundle.json']['run']['runId'] != run_id
                or documents['trace.json']['runId'] != run_id or documents['a2a-messages.json']['runId'] != run_id):
            raise ValueError()
        export = {'bundle': documents['bundle.json'], 'trace': documents['trace.json'],
                  'a2a': documents['a2a-messages.json'], 'handoff': handoff}
        from .output_evidence import snapshot_output_evidence
        export['automaticOutputEvidence'], export['snapshotTests'] = snapshot_output_evidence(run_id, snapshots)
        export['automaticOutputEvidence']['items'].extend(observed_files)
        export['automaticOutputEvidence']['collectionScope'] = 'REGISTERED_SNAPSHOTS_AND_CAPTURED_QA_INPUTS'
        final = documents['run.json']['finalCodeVersion']
        export['snapshotTestsComplete'] = (sorted(snapshots) == list(range(1, final + 1)) if type(final) is int
                                           else final is None and not snapshots)
        if (handoff['architecture'] != export['bundle']['configuration']['configuration']['architecture']
                or documents['run.json']['finalCodeVersion'] != export['bundle']['run']['codeVersion']
                or handoff['runStatus'] != export['bundle']['run']['status']
                or documents['run.json']['status'] != export['bundle']['run']['status']
                or documents['run.json']['verdict'] != export['bundle']['run']['verdict']
                or handoff['orchestratorVerdict'] != export['bundle']['run']['verdict']):
            raise ValueError()
        if 'package-completion.json' in files:
            # Every referenced byte was already checked above; nothing outside the handoff list is trusted.
            completion = documents['package-completion.json']
            if not isinstance(completion, dict):
                raise ValueError()
            export['packageCompletion'] = completion
            report_path = completion.get('finalReportPath')
            manifest_path = f"v{documents['run.json']['finalCodeVersion']}/manifest.json"
            if report_path not in files or manifest_path not in files:
                raise ValueError()
            export['packageFinalReport'] = documents[report_path]
            export['packageManifest'] = documents[manifest_path]
            export['packageFilesVerified'] = True
            # Scan the already hash-checked JSON, never a later reread that could differ.
            export['packageOutputEvidence'] = {'runId': run_id, 'scope': 'DECLARED_FILES_ONLY', 'items': [{
                'kind': 'ARTIFACT_FILE_CONTENT', 'sourceId': hashlib.sha256(report_path.encode()).hexdigest(),
                'status': 'COLLECTED', 'sha256': files[report_path]['sha256'], 'bytes': files[report_path]['sizeBytes'],
                'text': json.dumps(documents[report_path], ensure_ascii=False), 'error': None}]}
        return export, documents['run.json']
    except (OSError, ValueError, KeyError, TypeError):
        raise ExperimentError('HANDOFF_INVALID_OR_CHANGED') from None


def _status_fields(record):
    # evaluation_status.json에 함께 적는 끝 상태 요약(자세한 근거는 outcome.json)
    outcome = record['outcome']
    return {'cause': outcome['cause'], 'comparisonValidity': outcome['comparisonValidity'], 'resume': outcome['resume'],
            'autoCompletion': record['autoCompletion']['status']}


def ingest(folder, *, results, runner, exam, pair_id=None, known_values=(), additional_outputs=None,
           planner_review=None, process_evidence=None):
    export, run = load_handoff(folder)
    run_status = export['bundle']['run']['status']
    # 입력 대기(WAITING_INPUT)도 측정 실험에서는 답하지 않으므로 자동 구간이 닫힌 끝 상태로 받는다(최신 dev의 Planner
    # 질문·의미 검토 보류). 계획·구현·검증 중인 Run은 받지 않는다.
    if run_status not in CLOSED_STATUSES:
        raise ExperimentError('RUN_NOT_FINISHED')
    metrics = _checked_metrics(export['bundle'])
    contract = run_contract(export)
    # 다른 Run·계약이나 손상된 평가표 입력은 컨테이너를 띄우기 전에 거부한다.
    tables = agent_tables(export, contract, planner_review=planner_review)
    automatic_outputs = export.get('automaticOutputEvidence')
    if automatic_outputs is not None:
        if additional_outputs is not None:
            if additional_outputs.get('runId') != run['runId'] or not isinstance(additional_outputs.get('items'), list):
                raise ExperimentError('TRACE_RUN_MISMATCH')
            automatic_outputs = {**automatic_outputs, 'items': [*automatic_outputs['items'], *additional_outputs['items']]}
        additional_outputs = automatic_outputs
    if export.get('packageOutputEvidence') is not None:
        package_outputs = export['packageOutputEvidence']
        if additional_outputs is not None:
            if additional_outputs.get('runId') != run['runId'] or not isinstance(additional_outputs.get('items'), list):
                raise ExperimentError('TRACE_RUN_MISMATCH')
            package_outputs = {**package_outputs, 'items': [*package_outputs['items'], *additional_outputs['items']]}
        additional_outputs = package_outputs
    from .evidence_metrics import process_metrics
    if process_evidence is None:
        from .host_process import derive_process_evidence
        process_evidence = derive_process_evidence(export)
    process = process_metrics(run['runId'], process_evidence)

    def supplement(target, report=None):
        if planner_review is not None:
            reviewed = agent_tables(export, contract, report, planner_review=planner_review)
            # 이 ingest 호출이 방금 만든 표만 검토 결과로 갱신한다(기존 Run 덮어쓰기는 상위 경로에서 거부).
            _write(target / 'agent_tables.json', reviewed, True)
            _write(target / 'agent_tables.md', render_markdown(reviewed), True)
            _write(target / 'planner_review.json', planner_review, False)
        _write(target / 'process_metrics.json', process, False)
        if process_evidence is not None:
            _write(target / 'process_evidence.json', process_evidence, False)
        if export.get('packageCompletion') is not None:
            _write(target / 'package_completion_evidence.json', {k: export[k] for k in
                   ('packageCompletion', 'packageManifest')}, False)
    versions = run.get('exportedCodeVersions')
    final = run.get('finalCodeVersion')
    # 코드가 만들어지기 전에 멈춘 Run(Planner 보류·API 장애·한도 초과 등)은 dev 내보내기에서 최종 버전이 null이고
    # 내보낸 버전이 없다(2026-11-13 dev 하네스 기록으로 확인). 이 경우만 null을 받는다.
    if (not (final is None and versions == [] or type(final) is int and 0 <= final <= 4)
            or not isinstance(versions, list) or len(versions) > 4
            or any(type(v) is not int or not 1 <= v <= 4 for v in versions)
            or len(set(versions)) != len(versions)):
        raise ExperimentError('EXPORTED_VERSIONS_INVALID')
    target = Path(results) / run['runId']
    _check_automatic_boundary(target, export['bundle'], run=run)
    status = {'runId': run['runId'], 'runStatus': run_status,
              'automaticSegmentClosed': run_status in PAUSED_STATUSES}
    if has_resumed(export['bundle']):
        # 첫 인계가 사람 보조 재개 뒤에 왔다. 최종 생성물을 자동 실험으로 채점하지 않고,
        # 시도한 계획 슬롯과 실제 누적 지출은 보존한다. 이전 자동 경계의 값은 복원하지 않는다.
        record = run_outcome(export, run, report=None, metrics=metrics)

        def finish_assisted(target):
            _write(target / 'assisted_agent_tables.json', tables, False)
            _write(target / 'assisted_agent_tables.md', render_markdown(tables), False)
            _write(target / 'assisted_diagnostics.json', {
                'scope': 'FULL_RUN_INCLUDING_ASSISTED_CONTINUATION', 'metrics': metrics,
                'automaticBoundary': {'status': 'UNVERIFIED', 'reason': 'AUTOMATIC_BOUNDARY_NOT_CAPTURED'}}, False)
            _write(target / 'outcome.json', record, False)
            _write(target / 'evaluation_status.json', {**status, 'automaticSegmentClosed': True,
                'status': 'QUARANTINED_ASSISTED', 'reason': 'AUTOMATIC_BOUNDARY_NOT_CAPTURED',
                'requestedExam': exam, **_status_fields(record)}, False)

        return record_run(Path(results), export['bundle'], pair_id=pair_id, loop_policy=metrics['loopPolicy'],
                          _complete=finish_assisted)
    if run.get('finalCodeVersion') not in versions:
        record = run_outcome(export, run, report=None, metrics=metrics)

        def finish_unscored(target):
            _write(target / 'agent_tables.json', tables, False)
            _write(target / 'agent_tables.md', render_markdown(tables), False)
            _write(target / 'outcome.json', record, False)
            _write(target / 'evaluation_status.json', {**status, 'status': 'UNVERIFIED',
                   'reason': 'NO_ARTIFACT' if not versions else 'FINAL_SNAPSHOT_NOT_FOUND', **_status_fields(record)}, False)
            supplement(target)

        return record_run(Path(results), export['bundle'], pair_id=pair_id, _complete=finish_unscored)
    # 수정 전 버전을 빠뜨린 채 회귀가 없다고 보이지 않도록, 실행 전에 전체 이력을 요구한다.
    if sorted(versions) != list(range(1, run['finalCodeVersion'] + 1)):
        raise ExperimentError('SNAPSHOT_HISTORY_INCOMPLETE')
    required = {f'v{v}/{name}' for v in versions for name in ('source.tar', 'manifest.json')}
    if not required <= export['handoff']['files'].keys():
        raise ExperimentError('SNAPSHOT_NOT_IN_HANDOFF')

    def finish_scored(target):
        report = json.loads((target / 'output.json').read_text(encoding='utf-8'))['report']
        record = run_outcome(export, run, report=report, metrics=metrics)
        _write(target / 'outcome.json', record, False)
        _write(target / 'evaluation_status.json', {**status, 'status': 'SCORED', **_status_fields(record)}, False)
        supplement(target, report)

    return score_export(Path(folder), runner=runner, exam=exam, results=Path(results), contract=contract,
                         bundle=export['bundle'], trace=export['trace'], a2a=export['a2a'], pair_id=pair_id,
                         architecture=export['handoff']['architecture'], all_versions=True,
                         known_values=known_values, additional_outputs=additional_outputs, agent_export=export,
                         _complete=finish_scored)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', required=True, type=Path)
    parser.add_argument('--results', required=True, type=Path)
    parser.add_argument('--image', required=True)
    parser.add_argument('--exam', choices=('pilot', 'final'), required=True)
    parser.add_argument('--pair-id')
    parser.add_argument('--planner-review', type=Path)
    parser.add_argument('--process-evidence', type=Path)
    parser.add_argument('--secrets-file', type=Path)
    parser.add_argument('--output-root', type=Path)
    parser.add_argument('--output-manifest', type=Path)
    args = parser.parse_args(argv)
    try:
        known = read_known_values(args.secrets_file) if args.secrets_file else ()
        if bool(args.output_root) != bool(args.output_manifest):
            raise ExperimentError('OUTPUT_ROOT_AND_MANIFEST_REQUIRED')
        extra = None
        if args.output_manifest:
            export, _ = load_handoff(args.export)
            extra = collect_output_evidence(args.output_root, json.loads(args.output_manifest.read_text()),
                                            export['handoff']['runId'])
        folder = ingest(args.export, results=args.results, runner=DockerRunner(args.image), exam=args.exam,
                        pair_id=args.pair_id, known_values=known, additional_outputs=extra,
                        planner_review=json.loads(args.planner_review.read_text(encoding='utf-8')) if args.planner_review else None,
                        process_evidence=json.loads(args.process_evidence.read_text(encoding='utf-8')) if args.process_evidence else None)
        print(f'results: {folder}')
    except ExperimentError as error:
        sys.stderr.write(error.code + '\n')
        return 1
    except (OSError, ValueError, KeyError, TypeError):
        sys.stderr.write('EVALUATION_INPUT_INVALID\n')
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
