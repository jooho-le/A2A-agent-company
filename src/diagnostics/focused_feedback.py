"""Read-only Developer repair feedback from frozen, practice-only diagnostic measurements."""
import argparse
import json
from hashlib import sha256
from pathlib import Path

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_progress import changed_functions, changed_handler_decorators
from diagnostics.stage_replay import MAX_INPUT, ReplayError, _json, _read_json, _readonly
from evaluation import check_details, protected_tests
from evaluation.final_evaluation import read_snapshot
from orchestrator.domain.run_configuration import RunConfigurationArtifact
from orchestrator.domain.snapshot_handoff import CodeSnapshotArtifact
from orchestrator.domain.validation_feedback import observation_json


def read_candidate_snapshot(directory, run, version):
    with _readonly(directory / 'attempt/diagnostic.sqlite3') as con:
        records = con.execute("SELECT metadata_json,content FROM artifact_contents WHERE run_id=? "
                              "AND artifact_type='SOURCE' AND code_version=? AND size_bytes<=?",
                              (run, version, MAX_INPUT)).fetchall()
        config = RunConfigurationArtifact.model_validate_json(con.execute(
            'SELECT payload_json FROM run_configurations WHERE run_id=?', (run,)).fetchone()[0])
    if len(records) != 1:
        raise ReplayError('FOCUSED_SOURCE_NOT_UNIQUE')
    source = CodeSnapshotArtifact.model_validate_json(records[0][0])
    archive = records[0][1]
    if (str(source.run_id) != run or source.code_version != version
            or sha256(archive).hexdigest() != source.snapshot_sha256 or config.run_id != source.run_id):
        raise ReplayError('FOCUSED_SNAPSHOT_MISMATCH')
    return source, read_snapshot(archive), config


def read_measurement(directory, *, allow_historical=False):
    directory = Path(directory)
    manifest = _read_json(directory / 'manifest.json')
    result = _read_json(directory / 'focused-result.json')
    if 'replayId' in result and result['replayId'] != manifest['replayId']:
        raise ReplayError('FOCUSED_REPLAY_MISMATCH')
    source, files, config = read_candidate_snapshot(directory, manifest['sourceRunId'], result['codeVersion'])
    row = measurement_feedback(source, config, result, allow_historical=allow_historical)
    return source, files, row


def measurement_feedback(source, config, result, *, allow_historical=False):
    if (source.snapshot_sha256 != result['snapshotSha256']
            or str(source.artifact_id) != result.get('sourceArtifactId', str(source.artifact_id))
            or source.code_version != result['codeVersion']):
        raise ReplayError('FOCUSED_SNAPSHOT_MISMATCH')
    if 'imageDependencyLock' in result:
        observed = result['imageDependencyLock']
        if (not isinstance(observed, dict) or observed.get('status') != 'OBSERVED'
                or observed.get('sha256') != source.dependency_lock_hash
                or observed.get('imageDigest') != source.container_image_digest):
            raise ReplayError('FOCUSED_IMAGE_OR_LOCK_UNAVAILABLE')
    suite = protected_tests.load_suite()
    ref = config.configuration.protected_test_suite_ref
    plans = check_details.expected_plans('practice', suite)
    historical = ref != protected_tests.loop_suite_ref(suite)
    if historical:
        # Read-only historical measurements. Keep their own testVersion, and only
        # interpret their rows when the complete historical observation plan matches.
        prior = 'artifact://protected-tests/scn-001/signup-0.7.3/practice-c815b0128b1d83e7c93ea95a90701124ddba1de836160658aa02fdcb0c55cf07'
        plan_digest = sha256(json.dumps(plans, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        v074 = 'artifact://protected-tests/scn-001/signup-0.7.4/practice-f424548cc0b2956286399bc74ecb2447ed20a3d09b438dc5eb426a97a94743ef'
        v076 = 'artifact://protected-tests/scn-001/signup-0.7.6/practice-ceac570cbc9662a26c2d77e1e6d7452281c73b5c4ea25f51e4579648516a86e5'
        if (not allow_historical or ref not in (prior, v074, v076)
                or plan_digest != '941fa0e21ffe052b9152565310f8909db0d48b225c2737da6bb353b74922d42b'):
            raise ReplayError('FOCUSED_SUITE_CHANGED')
    practice = result['scopes']['practice']
    checks = practice['details']['checks']
    if len(checks) != 1:
        raise ReplayError('FOCUSED_EXPECTED_ONE_CHECK')
    key, raw = next(iter(checks.items()))
    check = next((item for item in suite['checks'] if item['key'] == key), None)
    if not check or protected_tests.unit_test_id(check, 'practice', suite) != practice['testId']:
        raise ReplayError('FOCUSED_TEST_ID_MISMATCH')
    role = check['perspective']
    binding = next(case for case in protected_tests.owned_cases(config.scenario_contract, role, suite)['cases']
                   if case['testId'] == practice['testId'])
    test_id = ':'.join(binding[name] for name in ('toolName', 'selector', 'testId'))
    validated = check_details._validated(raw, plans[key])
    if historical and any('statusOrigin' in item or (ref in (prior, v074) and 'diagnostic' in item)
                          or (ref == prior and 'handlerEntry' in item) for item in validated):
        raise ReplayError('FOCUSED_SUITE_CHANGED')
    observations = [{k: item[k] for k in ('caseId', 'itemId', 'kind', 'actual', 'status', 'reason', 'handlerEntry', 'diagnostic', 'statusOrigin') if k in item}
                    for item in validated]
    observation_json(observations)
    outcome = practice['outcome']
    if outcome not in ('FAIL', 'PASS', 'UNVERIFIED') or (
            (outcome == 'FAIL') != any(row['status'] == 'FAIL' and row['kind'] == 'assertion' for row in observations)
            or outcome == 'PASS' and any(row['status'] != 'PASS' for row in observations)):
        raise ReplayError('FOCUSED_VERDICT_MISMATCH')
    return {
        'role': role, 'reportArtifactId': 'practice:' + sha256(_json(practice).encode()).hexdigest(),
        'requirementId': binding['requirementId'], 'runId': str(source.run_id), 'sourceArtifactId': str(source.artifact_id),
        'codeVersion': source.code_version, 'testId': test_id, 'testVersion': 'practice:' + ref.rsplit('practice-', 1)[1],
        'observationStatus': 'COMPLETE', 'reason': None, 'outcome': outcome, 'observations': observations}


def feedback_between(before_dir, after_dir):
    parent, old_files, old = read_measurement(before_dir, allow_historical=True)
    source, new_files, new = read_measurement(after_dir, allow_historical=True)
    if (parent.run_id != source.run_id or source.previous_artifact_id != parent.artifact_id
            or parent.code_version + 1 != source.code_version or parent.repository_id != source.repository_id
            or parent.dependency_lock_hash != source.dependency_lock_hash
            or parent.container_image_digest != source.container_image_digest):
        raise ReplayError('FOCUSED_PARENT_MISMATCH')
    changed = sorted(k for k in old_files.keys() | new_files.keys() if old_files.get(k) != new_files.get(k))
    history = {'beforeVersion': parent.code_version, 'afterVersion': source.code_version,
               'beforeSnapshot': parent.snapshot_sha256, 'afterSnapshot': source.snapshot_sha256,
               'changedFiles': changed, 'changedFunctions': changed_functions(old_files, new_files, changed),
               'previousRows': [old]}
    history['changedHandlerDecorators'] = changed_handler_decorators(new_files, history['changedFunctions'])
    issue = {'issueId': new['testId'], 'category': 'QA_ASSERTION_FAILURE' if new['role'] == 'QA'
             else 'SECURITY_REQUIREMENT_FAILURE', 'referenceId': new['testId'], 'codeVersion': source.code_version,
             'reportArtifactId': new['reportArtifactId'], 'sourceArtifactId': new['sourceArtifactId'],
             'requirementIds': [new['requirementId']], 'description': _json({'code': 'MEASURED_SECURITY_TESTS',
                'tests': [{'testId': new['testId'], 'outcome': new['outcome']}]})}
    projected = compact_fix_input({'fixRequest': {'issues': [issue] if new['outcome'] == 'FAIL' else [],
                                                   'validationFeedback': [new]}}, repair_history=history,
                                  source_files=lambda: new_files)
    return {field: projected[field] for field in ('activeRepairFocus', 'previousRepairResult')}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    args = parser.parse_args()
    print(json.dumps(feedback_between(args.before, args.after), ensure_ascii=False, indent=2))
