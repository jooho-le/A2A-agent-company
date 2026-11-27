"""Evaluator-private concrete inputs and executable reproduction workflows.

Only digests, counts, and verdicts leave this module. Private input records must
never be attached to agent messages, public reports, or source snapshots.
"""
import argparse
import base64
import hashlib
import json
import os
import re
from copy import deepcopy
from pathlib import Path
from urllib.parse import quote

from . import protected_tests
from .manifest import ExecutionManifest

MAX_RECORD_BYTES = 8 * 1024 * 1024


def public_unit_report(report, tape):
    """Redact untrusted human-readable details before reports and their hashes.

    Never replace text in IDs, statuses, or digests: a one-character rejected
    password can also occur coincidentally in any such field. Short/blank inputs
    are hidden when quoted or standalone, not inside unrelated words.
    """
    if report is None:
        return None
    result = deepcopy(report)
    values = []

    def collect(value):
        if isinstance(value, str):
            if value not in values:
                values.append(value)
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    tests = tape.get('tests', {}) if isinstance(tape, dict) else {}
    for rows in (tests.values() if isinstance(tests, dict) else ()):
        if not isinstance(rows, list):
            continue
        for event in rows:
            if not isinstance(event, dict):
                continue
            if event.get('kind') in ('email', 'emails', 'password'):
                collect(event.get('value'))
            if event.get('kind') == 'request' and isinstance(event.get('arguments'), dict):
                collect(event['arguments'].get('body'))
    # Equality failure diffs can contain canonical or case-modified addresses,
    # not just the originally sent spelling.
    for value in list(values):
        if '@' in value:
            for form in (value.strip(), value.strip().lower(), value.upper(), value.swapcase()):
                collect(form)
    replacements, short = {}, []
    for index, value in enumerate(values, 1):
        marker = f'[PRIVATE_INPUT_{index:03d}]'
        forms = {repr(value), json.dumps(value, ensure_ascii=False), json.dumps(value, ensure_ascii=True)}
        if value.strip() and len(value) >= 3:
            raw = value.encode('utf-8')
            forms.update((value, quote(value, safe=''), raw.hex(),
                          base64.b64encode(raw).decode(), base64.b64encode(raw).decode().rstrip('='),
                          base64.urlsafe_b64encode(raw).decode().rstrip('='),
                          repr(value)[1:-1], json.dumps(value, ensure_ascii=True)[1:-1]))
        elif value.strip():
            short.append((value, marker))
        for form in forms:
            if form:
                replacements.setdefault(form, marker)
    pattern = re.compile('|'.join(re.escape(value) for value in sorted(replacements, key=lambda x: (-len(x), x)))) if replacements else None
    for row in result.get('tests', []):
        detail = row.get('details')
        if not isinstance(detail, str):
            continue
        # Keep verified machine-readable skip reasons intact.
        reason = re.match(r'^UNVERIFIED:([A-Z][A-Z0-9_]{0,127})(?:\b|$)', detail)
        if reason:
            row['details'] = ('UNVERIFIED:' + reason.group(1) if reason.group(1) not in values
                              else 'UNVERIFIED:PRIVATE_DETAILS_WITHHELD')
            continue
        if not values:
            row['details'] = 'Protected check detail withheld: concrete input record unavailable.'
            continue
        detail = pattern.sub(lambda match: replacements[match.group(0)], detail) if pattern else detail
        for value, marker in short:
            detail = re.sub(r'(?<!\w)' + re.escape(value) + r'(?!\w)', lambda _match: marker, detail)
        row['details'] = detail
    return result


def _bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _harness_digest():
    from .final_evaluation import _runner_source
    return hashlib.sha256((Path(__file__).with_name('replay_runner.py')).read_bytes() + b'\0' + _runner_source()).hexdigest()


def _profile(runner):
    # Runtime resource limits are part of the conditions, even if the image is
    # unchanged. Do not serialize arbitrary runner state or credentials.
    return {key: getattr(runner, key, None) for key in
            ('kind', 'python', 'cpus', 'memory_bytes', 'pids', 'tmpfs_bytes', 'timeout')}


def _suite_refs(exam):
    suite = protected_tests.load_suite()
    return {name: {'suiteId': suite['suiteId'], 'suiteVersion': suite['suiteVersion'],
                   'suiteSha256': protected_tests.suite_sha256(name, suite)} for name in (exam, 'practice')}


def _valid_tape(tape, names):
    if (not isinstance(tape, dict) or tape.get('schemaVersion') != 1
            or tape.get('status') not in ('RECORDED', 'PASS', 'MATCHED_FAILURE_PREFIX')
            or not isinstance(tape.get('tests'), dict) or set(tape['tests']) != set(names)
            or not isinstance(tape.get('checks'), dict) or set(tape['checks']) != set(names)):
        return False
    for rows in tape['tests'].values():
        if not isinstance(rows, list) or len(rows) > 5000:
            return False
        for row in rows:
            if (not isinstance(row, dict) or set(row) != {'kind', 'arguments', 'value'}
                    or row['kind'] not in ('email', 'emails', 'password', 'request')
                    or not isinstance(row['arguments'], dict)):
                return False
            if row['kind'] in ('email', 'password') and not isinstance(row['value'], str):
                return False
            if row['kind'] == 'emails' and (not isinstance(row['value'], list)
                                          or not all(isinstance(item, str) for item in row['value'])):
                return False
            if row['kind'] == 'request' and row['value'] is not None:
                return False
    return True


def record_inputs(directory, *, outcomes, report, reference_report, expected, scenario_contract,
                  source_run_id, architecture, exam, runner):
    result = {'schemaVersion': 1, 'status': 'UNVERIFIED', 'reason': None,
              'sha256': None, 'fileName': None, 'scopes': {}}
    if directory is None:
        return {**result, 'reason': 'PRIVATE_INPUT_STORAGE_NOT_CONFIGURED'}
    suite = protected_tests.load_suite()
    scopes, partial = {}, {}
    for name in (exam, 'practice'):
        outcome = outcomes[name]
        names = [protected_tests.unit_test_id(check, name, suite) for check in suite['checks']]
        tape = outcome.private_inputs
        valid = outcome.error is None and _valid_tape(tape, names)
        result['scopes'][name] = {'status': 'RECORDED' if valid else 'UNVERIFIED',
                                  'events': sum(len(rows) for rows in tape['tests'].values()) if valid else None}
        if valid:
            scopes[name] = tape
        elif isinstance(tape, dict):
            partial[name] = tape
    # Preserve available inputs even after partial/failed test execution, but do
    # not advertise a partial record as an executable complete replay.
    record = {'schemaVersion': 1, 'kind': 'PROTECTED_INPUT_RECORD', 'evaluationId': report['evaluationId'],
              'sourceRunId': source_run_id, 'architecture': architecture, 'exam': exam,
              'manifest': expected.to_payload(), 'suites': _suite_refs(exam),
              'scenarioContractSha256': digest(dict(scenario_contract)), 'harnessSha256': _harness_digest(),
              'runnerProfile': _profile(runner), 'sourceReportSha256': digest(report),
              'referenceReportSha256': digest(reference_report), 'scopes': scopes, 'partialScopes': partial,
              'privateDiagnostics': {name: outcomes[name].report for name in (exam, 'practice')}}
    data = _bytes(record)
    if len(data) > MAX_RECORD_BYTES:
        return {**result, 'reason': 'PRIVATE_INPUT_RECORD_TOO_LARGE'}
    folder = Path(directory)
    if folder.is_symlink():
        raise ValueError('PRIVATE_INPUT_DIRECTORY_SYMLINK')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(folder, 0o700)
    filename = report['evaluationId'] + '.json'
    fd = os.open(folder / filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    complete = len(scopes) == 2
    return {**result, 'status': 'RECORDED' if complete else 'UNVERIFIED',
            'reason': None if complete else 'INPUT_CAPTURE_INCOMPLETE',
            'sha256': hashlib.sha256(data).hexdigest(), 'fileName': filename}


def load_record(path, sha256):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_RECORD_BYTES:
        raise ValueError('PRIVATE_INPUT_RECORD_INVALID')
    data = path.read_bytes()
    if not isinstance(sha256, str) or hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError('PRIVATE_INPUT_HASH_MISMATCH')
    try:
        record = json.loads(data)
    except ValueError:
        raise ValueError('PRIVATE_INPUT_RECORD_INVALID') from None
    if not isinstance(record, dict) or record.get('schemaVersion') != 1 or record.get('kind') != 'PROTECTED_INPUT_RECORD':
        raise ValueError('PRIVATE_INPUT_RECORD_INVALID')
    return record


def validate_replay_binding(record, *, expected, source_run_id, architecture, exam, scenario_contract, runner, mode):
    if mode not in ('repeat', 'regression'):
        raise ValueError('REPLAY_MODE_INVALID')
    try:
        original = ExecutionManifest.from_payload(record['manifest'])
        valid = (record['schemaVersion'] == 1 and record['kind'] == 'PROTECTED_INPUT_RECORD'
                 and record['sourceRunId'] == source_run_id and record['architecture'] == architecture
                 and record['exam'] == exam and record['suites'] == _suite_refs(exam)
                 and record['scenarioContractSha256'] == digest(dict(scenario_contract))
                 and record['harnessSha256'] == _harness_digest() and record['runnerProfile'] == _profile(runner))
        if mode == 'repeat':
            valid = valid and original == expected
        else:
            valid = valid and all(getattr(original, key) == getattr(expected, key) for key in
                                  ('repository_id', 'container_image_digest', 'dependency_lock_hash', 'git_object_format'))
            valid = valid and original.code_version < expected.code_version
        suite = protected_tests.load_suite()
        valid = valid and set(record['scopes']) == {exam, 'practice'}
        for name in (exam, 'practice'):
            names = [protected_tests.unit_test_id(check, name, suite) for check in suite['checks']]
            valid = valid and _valid_tape(record['scopes'].get(name), names)
    except (KeyError, TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('REPLAY_CONDITIONS_DIFFER_OR_INPUTS_INCOMPLETE')


def public_replay_status(outcomes, exam, record):
    checks = {}
    for name in (exam, 'practice'):
        outcome = outcomes[name]
        tape = outcome.private_inputs or {}
        checks[name] = {'status': tape.get('status', 'UNVERIFIED'), 'checks': tape.get('checks', {}),
                        'error': outcome.error}
    return {'schemaVersion': 1, 'sourceEvaluationId': record['evaluationId'],
            'inputRecordSha256': digest(record), 'scopes': checks,
            'limitations': ['Concurrency scheduling and generated password-hash salts are not fixed.']}


def replay_evaluation(*, baseline, input_path, archive, expected, scenario_contract, runner, mode='repeat'):
    """Actually execute the protected suite with the recorded concrete inputs."""
    from .final_evaluation import evaluate_final_snapshot
    descriptor = baseline.get('inputRecord') or {}
    if descriptor.get('status') != 'RECORDED':
        raise ValueError('COMPLETE_INPUT_RECORD_REQUIRED')
    record = load_record(input_path, descriptor.get('sha256'))
    if (digest(baseline['report']) != record.get('sourceReportSha256')
            or digest(baseline['referenceReport']) != record.get('referenceReportSha256')):
        raise ValueError('INPUT_RECORD_REPORT_MISMATCH')
    return evaluate_final_snapshot(archive=archive, expected=expected, scenario_contract=scenario_contract,
                                   source_run_id=record['sourceRunId'], architecture=record['architecture'],
                                   exam=record['exam'], runner=runner, replay_record=record, replay_mode=mode)


def _usable_replay(output, *, allow_prefix=False):
    scopes = (output.get('inputReplay') or {}).get('scopes', {})
    item = scopes.get(output.get('exam'), {})
    allowed = ('PASS', 'MATCHED_FAILURE_PREFIX') if allow_prefix else ('PASS',)
    return item.get('status') in allowed and item.get('error') is None


def repeat_evaluation(**kwargs):
    """Re-execute one frozen Snapshot, then compare the observed outcomes."""
    from .repeatability import compare_reports
    baseline = kwargs['baseline']
    output = replay_evaluation(**kwargs, mode='repeat')
    result = compare_reports(baseline['report'], output['report'])
    result['kind'] = 'EXECUTED_INPUT_REPLAY'
    result['inputRecordSha256'] = baseline['inputRecord']['sha256']
    result['replay'] = output['inputReplay']
    result['repeatedEvaluationId'] = output['report']['evaluationId']
    if not _usable_replay(output):
        result.update(status='UNVERIFIED', reason='EXACT_INPUT_REPLAY_UNVERIFIED')
    return {'result': result, 'output': output}


def confirm_regression(*, baseline, input_path, baseline_archive, candidate_archive,
                       candidate_expected, scenario_contract, runner, repetitions=2):
    """Re-run both versions at least twice on the same private input record.

    A confirmed row means a repeated, controlled PASS->FAIL observation. It is
    not a proof of the causal source line or absence of scheduler variability.
    """
    if type(repetitions) is not int or not 2 <= repetitions <= 10:
        raise ValueError('REGRESSION_REQUIRES_2_TO_10_REPEATS')
    previous = ExecutionManifest.from_payload(baseline['report']['expectedManifest'])
    # Validate both conditions before either side executes.
    descriptor = baseline.get('inputRecord') or {}
    record = load_record(input_path, descriptor.get('sha256'))
    for manifest, mode in ((previous, 'repeat'), (candidate_expected, 'regression')):
        validate_replay_binding(record, expected=manifest, source_run_id=baseline['report']['sourceRunId'],
                                architecture=baseline['report']['architecture'], exam=baseline['exam'],
                                scenario_contract=scenario_contract, runner=runner, mode=mode)
    outputs = {'previous': [], 'candidate': []}
    for _ in range(repetitions):
        for side, archive, expected, mode in (('previous', baseline_archive, previous, 'repeat'),
                                               ('candidate', candidate_archive, candidate_expected, 'regression')):
            outputs[side].append(replay_evaluation(baseline=baseline, input_path=input_path, archive=archive,
                                                  expected=expected, scenario_contract=scenario_contract,
                                                  runner=runner, mode=mode))
    rows = []
    suite = protected_tests.load_suite()
    unit_ids = {check['checkId']: protected_tests.unit_test_id(check, baseline['exam'], suite)
                for check in suite['checks']}
    for check in baseline['report']['checks']:
        test_id = check['testId']
        statuses, consumption, conditions = {}, {}, True
        for side, runs in outputs.items():
            statuses[side] = []
            consumption[side] = []
            for output in runs:
                report = output['report']
                current = next((row for row in report['checks'] if row['testId'] == test_id), {})
                statuses[side].append(current.get('status', 'UNVERIFIED'))
                replay_checks = ((output.get('inputReplay') or {}).get('scopes', {})
                                 .get(baseline['exam'], {}).get('checks', {}))
                consumption[side].append(replay_checks.get(unit_ids.get(test_id)))
                conditions = (conditions and _usable_replay(output, allow_prefix=True)
                              and report['manifestCheck']['metadataMatches'] is True
                              and report['manifestCheck']['errorCode'] is None)
        initial_status = check.get('status', 'UNVERIFIED')
        known = (initial_status in ('PASS', 'FAIL')
                 and all(status in ('PASS', 'FAIL') for values in statuses.values() for status in values))
        stable = (all(len(set(values)) == 1 for values in statuses.values())
                  and all(status == initial_status for status in statuses['previous'])
                  and all(all(value == values[0] for value in values) for values in consumption.values()))
        if not conditions or not known:
            status, reason = 'UNVERIFIED', 'REPLAY_OR_RESULT_UNVERIFIED'
        elif not stable:
            status, reason = 'UNVERIFIED', 'EXECUTION_VARIATION_OBSERVED'
        elif statuses['previous'][0] == 'PASS' and statuses['candidate'][0] == 'FAIL':
            status, reason = 'CONFIRMED', 'REPEATED_SAME_INPUT_PASS_TO_FAIL'
        else:
            status, reason = 'NOT_REPRODUCED', 'NO_STABLE_PASS_TO_FAIL'
        rows.append({'testId': test_id, 'status': status, 'reason': reason, 'observed': statuses,
                     'initialBaselineStatus': initial_status, 'inputConsumption': consumption})
    result = {'schemaVersion': 1, 'kind': 'EXECUTED_REGRESSION_CONFIRMATION',
              'sourceRunId': baseline['report']['sourceRunId'], 'repetitionsPerVersion': repetitions,
              'previousManifest': previous.to_payload(), 'candidateManifest': candidate_expected.to_payload(),
              'inputRecordSha256': descriptor['sha256'], 'checks': rows,
              'confirmedCount': sum(row['status'] == 'CONFIRMED' for row in rows),
              'unverifiedCount': sum(row['status'] == 'UNVERIFIED' for row in rows),
              'limitations': ['Controlled observed regression; no causal source-line claim.',
                              'Finite repetitions cannot rule out scheduler-dependent or rare failures.']}
    return {'result': result, 'outputs': outputs}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--contract', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--candidate-snapshot', type=Path)
    parser.add_argument('--candidate-manifest', type=Path)
    parser.add_argument('--repetitions', type=int, default=2)
    args = parser.parse_args(argv)
    if bool(args.candidate_snapshot) != bool(args.candidate_manifest):
        parser.error('candidate snapshot and manifest must be supplied together')
    from .final_evaluation import DockerRunner
    baseline = json.loads(args.baseline.read_text(encoding='utf-8'))
    contract = json.loads(args.contract.read_text(encoding='utf-8'))
    record = load_record(args.inputs, (baseline.get('inputRecord') or {}).get('sha256'))
    profile = record.get('runnerProfile', {})
    if profile.get('kind') != 'docker':
        raise ValueError('CLI_REPLAY_REQUIRES_DOCKER_RECORD')
    runner = DockerRunner(args.image, **{key: profile[key] for key in
                          ('python', 'cpus', 'memory_bytes', 'pids', 'tmpfs_bytes', 'timeout')})
    if args.out.exists():
        raise ValueError('REPLAY_OUTPUT_ALREADY_EXISTS')
    common = dict(baseline=baseline, input_path=args.inputs, scenario_contract=contract, runner=runner)
    if args.candidate_snapshot:
        value = confirm_regression(**common, baseline_archive=args.snapshot.read_bytes(),
                                   candidate_archive=args.candidate_snapshot.read_bytes(),
                                   candidate_expected=ExecutionManifest.from_payload(json.loads(args.candidate_manifest.read_text())),
                                   repetitions=args.repetitions)
    else:
        value = repeat_evaluation(**common, archive=args.snapshot.read_bytes(),
                                 expected=ExecutionManifest.from_payload(baseline['report']['expectedManifest']))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    result = value['result']
    return 0 if result.get('status') == 'PASS' or result.get('unverifiedCount') == 0 else 2


if __name__ == '__main__':
    raise SystemExit(main())
