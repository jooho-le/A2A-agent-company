"""Remeasure one authorized Ver1 practice failure on a published Developer replay candidate."""
from hashlib import sha256
import os
from pathlib import Path
import tempfile

from diagnostics import developer_replay as replay
from diagnostics.focused_feedback import measurement_feedback, read_candidate_snapshot
from diagnostics.focused_replay import published_candidate
from evaluation import final_evaluation, protected_tests, replay_runner


def _select_case(config, source, check_key, projection):
    suite = protected_tests.load_suite()
    ref = protected_tests.loop_suite_ref(suite)
    if config.configuration.protected_test_suite_ref != ref:
        raise replay.ReplayError('FOCUSED_SUITE_CHANGED')
    check = next((item for item in suite['checks'] if item['key'] == check_key), None)
    if check is None:
        raise replay.ReplayError('FOCUSED_TEST_ID_MISMATCH')
    role = check['perspective']
    test_id = protected_tests.unit_test_id(check, 'practice', suite)
    binding, = (c for c in protected_tests.owned_cases(config.scenario_contract, role, suite)['cases']
                if c['testId'] == test_id)
    canonical_id = ':'.join(binding[k] for k in ('toolName', 'selector', 'testId'))
    rows = [r for r in projection.get('fixRequest', {}).get('validationFeedback', [])
            if r.get('testId') == canonical_id and r.get('role') == role
            and r.get('requirementId') == binding['requirementId']
            and r.get('testVersion') == 'practice:' + ref.rsplit('practice-', 1)[1]
            and r.get('runId') == str(source.run_id) and r.get('sourceArtifactId') == str(source.previous_artifact_id)
            and r.get('codeVersion') == source.code_version - 1]
    if len(rows) != 1 or rows[0].get('hostDecision') != 'FIX_TARGET' or rows[0].get('outcome') != 'FAIL':
        raise replay.ReplayError('FOCUSED_PREVIOUS_FAILURE_REQUIRED')
    return test_id, protected_tests.protected_files(protected_tests.LOOP_SCOPES[role], suite)


def measure_practice(parent, check_key):
    parent = Path(parent).resolve(strict=True)
    result_file = parent / 'focused-result.json'
    if result_file.exists():
        raise replay.ReplayError('FOCUSED_RESULT_ALREADY_EXISTS')
    manifest = replay._read_json(parent / 'manifest.json')
    source, files, config = read_candidate_snapshot(parent, manifest['sourceRunId'], manifest['codeVersion'])
    published_candidate(parent, manifest, source)
    projection = replay._read_json(parent / 'projected-input.json')
    if sha256(replay._json(projection).encode()).hexdigest() != manifest['projectedInputSha256']:
        raise replay.ReplayError('FOCUSED_INPUT_CHANGED')
    test_id, practice_files = _select_case(config, source, check_key, projection)
    runner = final_evaluation.DockerRunner(source.container_image_digest, timeout=90)
    lock = runner.image_dependency_lock()
    if (lock['status'] != 'OBSERVED' or lock['sha256'] != source.dependency_lock_hash
            or lock['imageDigest'] != source.container_image_digest):
        raise replay.ReplayError('FOCUSED_IMAGE_OR_LOCK_UNAVAILABLE')
    # Only the selected unittest is discovered; each side is mounted read-only.
    pattern = 'focused_case.py'
    directory = 'tests/signup'
    inputs = {name: content.encode() for name, content in practice_files.items()}
    inputs[directory + '/' + pattern] = (
        'def load_tests(loader, cases, pattern):\n    return loader.loadTestsFromName(' + repr(test_id) + ')\n'
    ).encode()
    inputs.update({final_evaluation.RUNNER_FILE: final_evaluation._runner_source(),
                   final_evaluation.REPLAY_FILE: Path(replay_runner.__file__).read_bytes(),
                   '_input_replay.json': b'{}'})
    with tempfile.TemporaryDirectory(prefix='focused-practice-') as tmp:
        root = Path(tmp).resolve()
        final_evaluation._write_tree(root / 'source', files)
        final_evaluation._write_tree(root / 'inputs', inputs)
        out = runner.run(root / 'source', root / 'inputs', directory, pattern, final_evaluation.REPLAY_FILE)
    if (out.error or not out.report or not out.detail_results
            or set(out.detail_results.get('checks', ())) != {check_key}):
        raise replay.ReplayError('FOCUSED_TEST_EXECUTION_INCOMPLETE')
    rows = out.report['tests']
    if len(rows) != 1 or rows[0]['testId'] != test_id:
        raise replay.ReplayError('FOCUSED_TEST_ID_MISMATCH')
    outcome = 'UNVERIFIED' if rows[0]['outcome'] == 'SKIP' else rows[0]['outcome']
    result = {'replayId': manifest['replayId'], 'codeVersion': source.code_version,
              'sourceArtifactId': str(source.artifact_id), 'snapshotSha256': source.snapshot_sha256,
              'measurementEligible': False, 'imageDependencyLock': lock, 'apiCalls': 0,
              'scopes': {'practice': {'testId': test_id, 'outcome': outcome, 'exitCode': out.exit_code,
                                      'details': out.detail_results}}}
    measurement_feedback(source, config, result)
    # Readers either see a complete result or none. A second run cannot replace the first.
    with tempfile.TemporaryDirectory(dir=parent) as staging:
        pending = Path(staging) / 'result.json'
        replay._write(pending, result)
        os.link(pending, result_file)
    return result
