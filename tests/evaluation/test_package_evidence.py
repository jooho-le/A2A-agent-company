"""TIME-02 accepts real file evidence and a distinct completion event, never export time."""
from datetime import timedelta
from hashlib import sha256
import json
import shutil
from uuid import uuid4

import pytest

from evaluation.evidence_metrics import _time
from evaluation.experiment import ExperimentError
from evaluation.outcomes import run_outcome
from evaluation.package_evidence import package_assessment
from evaluation.run_metrics import run_metrics
from evaluation.workflow import load_handoff
from tests.evaluation.test_dev_compatibility import exports  # noqa: F401


def write(root, name, value):
    (root / name).write_text(json.dumps(value), encoding='utf-8')


def seal_handoff(root):
    handoff = json.loads((root / 'handoff.json').read_text())
    handoff['files'] = {p.relative_to(root).as_posix(): {'sha256': sha256(p.read_bytes()).hexdigest(),
                          'sizeBytes': p.stat().st_size} for p in root.rglob('*') if p.is_file() and p.name != 'handoff.json'}
    write(root, 'handoff.json', handoff)


@pytest.fixture
def package(exports, tmp_path):
    root = tmp_path / 'export'
    shutil.copytree(exports / 'fix-success', root)
    export, _ = load_handoff(root)
    bundle = export['bundle']
    run = bundle['run']
    version = run['codeVersion']
    manifest = json.loads((root / f'v{version}/manifest.json').read_text())
    end = next(e for e in bundle['events'] if e['eventType'] == 'RUN_FINISHED')
    completed_at = (_time(end['occurredAt']) + timedelta(seconds=1)).isoformat()
    event = {**end, 'eventId': str(uuid4()), 'eventType': 'RESULT_PACKAGE_COMPLETED',
             'occurredAt': completed_at, 'snapshotSha256': manifest['snapshotSha256']}
    bundle['events'].append(event)
    bundle['eventsTotal'] += 1
    write(root, 'bundle.json', bundle)
    report = {'artifactType': 'FINAL_REPORT', 'runId': run['runId'], 'codeVersion': version,
              'snapshotSha256': manifest['snapshotSha256'], 'verdict': run['verdict'],
              'summary': 'Synthetic completion evidence for evaluator tests only.'}
    write(root, 'final-report.json', report)
    selected = ('final-report.json', f'v{version}/source.tar', f'v{version}/manifest.json')
    evidence = {'schemaVersion': 1, 'producer': 'HOST_AUTOMATIC', 'eventId': event['eventId'],
                'runId': run['runId'], 'codeVersion': version, 'snapshotSha256': manifest['snapshotSha256'],
                'completedAt': completed_at, 'finalReportPath': 'final-report.json',
                'files': {name: {'sha256': sha256((root / name).read_bytes()).hexdigest(),
                                'sizeBytes': (root / name).stat().st_size} for name in selected}}
    write(root, 'package-completion.json', evidence)
    seal_handoff(root)
    export, run_doc = load_handoff(root)
    return export, run_doc, root


def test_separate_completion_event_supplies_time_and_package_condition(package):
    export, run_doc, _ = package
    proof = package_assessment(export)
    assert proof['status'] == 'PASS'
    start = next(e['occurredAt'] for e in export['bundle']['events'] if e['eventType'] == 'RUN_STARTED')
    assert proof['packageMs'] == int((_time(proof['completedAt']) - _time(start)).total_seconds() * 1000)
    record = run_outcome(export, run_doc, metrics=run_metrics(export['bundle']))
    assert record['autoCompletion']['conditions']['packageComplete']['status'] == 'PASS'
    assert record['packageTiming'] == proof


@pytest.mark.parametrize('damage', ['missing_event', 'changed_time', 'wrong_run', 'wrong_hash', 'wrong_report',
                                   'late_resume', 'operator', 'not_verified', 'manifest', 'empty_file', 'missing_file'])
def test_incomplete_or_mixed_package_evidence_stays_unverified(package, damage):
    export, _, _ = package
    evidence = export['packageCompletion']
    if damage == 'missing_event':
        export['bundle']['events'].pop(); export['bundle']['eventsTotal'] -= 1
    elif damage == 'changed_time': evidence['completedAt'] = '2020-01-01T00:00:00+00:00'
    elif damage == 'wrong_run': evidence['runId'] = str(uuid4())
    elif damage == 'wrong_hash': evidence['snapshotSha256'] = '0' * 64
    elif damage == 'wrong_report': export['packageFinalReport']['verdict'] = 'FAIL'
    elif damage == 'late_resume': export['bundle']['events'][0]['eventType'] = 'WORKFLOW_RESUMED'
    elif damage == 'operator': evidence['producer'] = 'OPERATOR'
    elif damage == 'not_verified': del export['packageFilesVerified']
    elif damage == 'manifest': export['packageManifest']['projectArtifactId'] = str(uuid4())
    elif damage == 'empty_file': evidence['files']['final-report.json']['sizeBytes'] = 0
    else: del evidence['files']['final-report.json']
    assert package_assessment(export)['status'] == 'UNVERIFIED'
    assert package_assessment(export)['packageMs'] is None


def test_changed_final_report_bytes_are_rejected_before_scoring(package):
    _, _, root = package
    write(root, 'final-report.json', {'changed': True})
    with pytest.raises(ExperimentError, match='HANDOFF_INVALID_OR_CHANGED'):
        load_handoff(root)


def test_old_exports_do_not_acquire_package_time(exports):
    export, _ = load_handoff(exports / 'fix-success')
    assert package_assessment(export)['packageMs'] is None


def test_native_collector_attaches_only_existing_matching_completion(package):
    from agents.platform.package_records import collect_package_records
    export, _, root = package
    base = {name: (root / name).read_bytes() for name in export['handoff']['files']
            if name not in ('final-report.json', 'package-completion.json')}
    assert set(collect_package_records(root, base, export['bundle'])) == {'final-report.json', 'package-completion.json'}
    export['bundle']['events'] = [row for row in export['bundle']['events'] if row['eventType'] != 'RESULT_PACKAGE_COMPLETED']
    with pytest.raises(ValueError, match='PACKAGE_COMPLETION_NOT_RECORDED'):
        collect_package_records(root, base, export['bundle'])


def test_native_collector_rejects_report_symlink(package):
    from agents.platform.package_records import collect_package_records
    export, _, root = package
    path = root / 'final-report.json'
    original = root / 'original.json'
    path.rename(original)
    path.symlink_to(original)
    with pytest.raises((ValueError, OSError)):
        collect_package_records(root, {}, export['bundle'])


def test_package_completion_after_limit_cannot_pass_auto_completion(package):
    export, run_doc, _ = package
    export['bundle']['configuration']['configuration']['limits']['runtimeBudgetMs'] = 1
    record = run_outcome(export, run_doc, metrics=run_metrics(export['bundle']))
    assert record['autoCompletion']['conditions']['withinLimits'] == {
        'status': 'FAIL', 'reason': 'PACKAGE_RUNTIME_BUDGET_EXCEEDED'}


def test_final_report_is_included_in_declared_output_scan(package):
    from evaluation.trace_checks import check_trace_exposure
    from tests.evaluation.test_trace_checks import CRITERIA
    export, _, _ = package
    # An ordinary canary already in the hashed report must be found without copying its value to the result.
    canary = 'Synthetic completion evidence for evaluator tests only.'
    trace = {**export['trace'], 'additionalOutputs': export['packageOutputEvidence']}
    result = check_trace_exposure(trace, CRITERIA, known_values=[canary])
    assert result.status == 'FAIL'
    assert canary not in str(result)


@pytest.mark.parametrize('value', [[], 'invalid', 1, True])
def test_malformed_completion_record_is_unverified(package, value):
    export, _, _ = package
    export['packageCompletion'] = value
    assert package_assessment(export)['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('status', ['PENDING', 'NOT_COMPLETED', 'ERROR'])
def test_unfinished_host_producer_cannot_be_replaced_by_external_completion_files(package, status):
    export, _, _ = package
    export['handoff']['packageProduction'] = {'schemaVersion': 1, 'runId': export['bundle']['run']['runId'],
                                             'status': status, 'eventId': None, 'completedAt': None, 'reason': None}
    assert package_assessment(export)['status'] == 'UNVERIFIED'
    assert package_assessment(export)['reason'] == 'PACKAGE_PRODUCTION_' + status
