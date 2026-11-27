"""Concrete-input capture/replay using only trusted synthetic source locally.

No Docker connection: the image-confirmation boundary is an explicit test double.
"""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import stat

import pytest

from evaluation import protected_tests
from evaluation import replay
from evaluation.final_evaluation import LocalRunner
from evaluation.replay import (confirm_regression, digest, load_record, repeat_evaluation,
                                replay_evaluation, validate_replay_binding)
from evaluation.replay_runner import InputTape
from tests.evaluation.test_final_evaluation import CONTRACT, IMAGE, RUNTIME, final, manifest, snapshot


class SyntheticRunner:
    kind = 'docker'

    def __init__(self):
        self.calls = 0

    def run(self, *args, **kwargs):
        self.calls += 1
        outcome = LocalRunner(trusted=True).run(*args, **kwargs)
        return replace(outcome, image_digest=IMAGE, kind=self.kind)


@pytest.fixture(scope='module')
def recorded(tmp_path_factory):
    if not RUNTIME:
        pytest.skip('trusted synthetic fixture dependencies unavailable')
    directory = tmp_path_factory.mktemp('private-inputs')
    archive = snapshot()
    expected = manifest(archive)
    output = final(archive, expected, SyntheticRunner(), private_input_dir=directory)
    assert output['report']['evaluationVerdict'] == 'PASS'
    assert output['inputRecord']['status'] == 'RECORDED'
    path = directory / output['inputRecord']['fileName']
    return output, path, archive, expected


def test_records_actual_values_and_requests_privately(recorded):
    output, path, _archive, _expected = recorded
    record = load_record(path, output['inputRecord']['sha256'])
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert record['sourceReportSha256'] == digest(output['report'])
    assert set(record['scopes']) == {'final', 'practice'}
    events = [row for rows in record['scopes']['final']['tests'].values() for row in rows]
    assert {'password', 'email', 'emails', 'request'} <= {row['kind'] for row in events}
    assert any(row['arguments']['body'] == [] for row in events if row['kind'] == 'request') or any(
        isinstance(row['arguments']['body'], list) for row in events if row['kind'] == 'request')
    public = json.dumps(output)
    for row in events:
        if row['kind'] == 'password' and len(row['value']) >= 8:
            assert row['value'] not in public
        if row['kind'] == 'email':
            assert row['value'] not in public


def test_executes_same_concrete_input_tape_on_same_snapshot(recorded):
    baseline, path, archive, expected = recorded
    before = path.read_bytes()
    runner = SyntheticRunner()
    result = repeat_evaluation(baseline=baseline, input_path=path, archive=archive, expected=expected,
                               scenario_contract=CONTRACT, runner=runner)
    assert runner.calls == 3  # actual exam + practice + supplementary coverage
    assert result['result']['status'] == 'PASS'
    assert result['result']['kind'] == 'EXECUTED_INPUT_REPLAY'
    assert result['output']['report']['evaluationId'] != baseline['report']['evaluationId']
    assert path.read_bytes() == before
    scopes = result['output']['inputReplay']['scopes']
    assert all(value['status'] == 'PASS' for value in scopes.values())
    assert all(row['complete'] and row['consumed'] == row['total'] for value in scopes.values()
               for row in value['checks'].values())


@pytest.mark.parametrize('change', ['snapshot', 'image', 'lock', 'contract', 'suite', 'harness', 'profile', 'run'])
def test_condition_drift_is_rejected_before_execution(recorded, change):
    baseline, path, archive, expected = recorded
    record = load_record(path, baseline['inputRecord']['sha256'])
    contract = deepcopy(CONTRACT)
    run_id = baseline['report']['sourceRunId']
    if change == 'snapshot':
        expected = replace(expected, snapshot_sha256='e' * 64)
    elif change == 'image':
        expected = replace(expected, container_image_digest='sha256:' + 'e' * 64)
    elif change == 'lock':
        expected = replace(expected, dependency_lock_hash='sha256:' + 'e' * 64)
    elif change == 'contract':
        contract['scenarioKey'] = 'OTHER'
    elif change == 'suite':
        record['suites']['final']['suiteSha256'] = 'e' * 64
    elif change == 'harness':
        record['harnessSha256'] = 'e' * 64
    elif change == 'profile':
        record['runnerProfile']['timeout'] = 1
    else:
        run_id = 'different-run'
    runner = SyntheticRunner()
    with pytest.raises(ValueError, match='REPLAY_CONDITIONS'):
        validate_replay_binding(record, expected=expected, source_run_id=run_id,
                                architecture=baseline['report']['architecture'], exam='final',
                                scenario_contract=contract, runner=runner, mode='repeat')
    assert runner.calls == 0


def test_tampered_input_file_and_different_report_rejected(recorded, tmp_path):
    baseline, path, archive, expected = recorded
    tampered = tmp_path / 'tampered.json'
    tampered.write_bytes(path.read_bytes() + b' ')
    runner = SyntheticRunner()
    with pytest.raises(ValueError, match='PRIVATE_INPUT_HASH_MISMATCH'):
        replay_evaluation(baseline=baseline, input_path=tampered, archive=archive, expected=expected,
                          scenario_contract=CONTRACT, runner=runner)
    altered = deepcopy(baseline)
    altered['report']['createdAt'] = '2026-11-15T00:00:00Z'
    with pytest.raises(ValueError, match='INPUT_RECORD_REPORT_MISMATCH'):
        replay_evaluation(baseline=altered, input_path=path, archive=archive, expected=expected,
                          scenario_contract=CONTRACT, runner=runner)
    assert runner.calls == 0


def test_actual_regression_confirmation_replays_both_versions_twice(recorded):
    baseline, path, archive, expected = recorded
    candidate = snapshot('plaintext_storage')
    candidate_manifest = replace(manifest(candidate), code_version=expected.code_version + 1)
    runner = SyntheticRunner()
    result = confirm_regression(baseline=baseline, input_path=path, baseline_archive=archive,
                                candidate_archive=candidate, candidate_expected=candidate_manifest,
                                scenario_contract=CONTRACT, runner=runner)
    assert runner.calls == 12  # 2 versions x 2 replays x 3 actual suites
    report = result['result']
    hash_id = next(row['checkId'] for row in protected_tests.load_suite()['checks'] if row['key'] == 'SEC-SIGNUP-002')
    row = next(row for row in report['checks'] if row['testId'] == hash_id)
    assert row['status'] == 'CONFIRMED'
    assert row['observed'] == {'previous': ['PASS', 'PASS'], 'candidate': ['FAIL', 'FAIL']}
    assert report['confirmedCount'] >= 1
    assert len({output['report']['evaluationId'] for runs in result['outputs'].values() for output in runs}) == 4


def test_input_tape_refuses_new_draw_or_changed_request_without_disclosing_values():
    tape = InputTape()
    tape.begin('a')
    assert tape.value('email', {'template': '{h}@example.com'}, lambda: 'private@example.com') == 'private@example.com'
    tape.value('request', {'body': {'email': 'private@example.com'}}, lambda: None)
    replay = InputTape(tape.tests)
    replay.begin('a')
    assert replay.value('email', {'template': '{h}@example.com'}, lambda: pytest.fail('random generation during replay')) == 'private@example.com'
    with pytest.raises(ValueError, match='^REPLAY_INPUT_MISMATCH$'):
        replay.value('request', {'body': {'email': 'other@example.com'}}, lambda: None)
    assert replay.payload({'tests': [{'testId': 'a', 'outcome': 'SKIP'}]})['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('path,content,reason', [
    ('.private-inputs/run/record.json', b'{}', 'PRIVATE_INPUT_PATH'),
    ('.evaluation-history/run/evaluation/output.json', b'{}', 'PRIVATE_INPUT_PATH'),
    ('notes/copied.json', b'{"kind":"PROTECTED_INPUT_RECORD","schemaVersion":1}', 'PROTECTED_INPUT_RECORD'),
    ('_input_replay.json', b'{}', 'PRIVATE_INPUT_PATH'),
])
def test_private_inputs_cannot_enter_the_source_snapshot(path, content, reason):
    assert protected_tests.exam_exposure({path: content}) == [{'path': path, 'reason': reason}]


def test_known_failure_prefix_is_distinguished_from_missing_or_skipped_inputs():
    original = {'a': [{'kind': 'email', 'arguments': {}, 'value': 'private'},
                      {'kind': 'request', 'arguments': {}, 'value': None}]}
    for outcome, status in [('FAIL', 'MATCHED_FAILURE_PREFIX'), ('PASS', 'UNVERIFIED'), ('SKIP', 'UNVERIFIED')]:
        tape = InputTape(original)
        tape.begin('a')
        tape.value('email', {}, lambda: None)
        result = tape.payload({'tests': [{'testId': 'a', 'outcome': outcome}]})
        assert result['status'] == status
        assert result['checks']['a']['complete'] is False


@pytest.mark.parametrize('case,reason', [
    ('status_varies', 'EXECUTION_VARIATION_OBSERVED'),
    ('consumption_varies', 'EXECUTION_VARIATION_OBSERVED'),
    ('unknown_result', 'REPLAY_OR_RESULT_UNVERIFIED'),
    ('different_input', 'REPLAY_OR_RESULT_UNVERIFIED'),
])
def test_unknown_or_varying_replays_do_not_confirm_regression(recorded, monkeypatch, case, reason):
    baseline, path, archive, expected = recorded
    suite = protected_tests.load_suite()
    check_id = suite['checks'][0]['checkId']
    unit_id = protected_tests.unit_test_id(suite['checks'][0], 'final', suite)
    candidate_calls = 0

    def controlled_replay(**kwargs):
        nonlocal candidate_calls
        output = deepcopy(baseline)
        per_test = {protected_tests.unit_test_id(check, 'final', suite): {
            'consumed': 3, 'total': 3, 'complete': True, 'error': None, 'matchedFailurePrefix': False}
                    for check in suite['checks']}
        tape = {'status': 'PASS', 'error': None, 'checks': per_test}
        output['inputReplay'] = {'scopes': {'final': tape}}
        if kwargs['mode'] == 'regression':
            candidate_calls += 1
            target = next(row for row in output['report']['checks'] if row['testId'] == check_id)
            target['status'] = 'FAIL'
            if case == 'status_varies' and candidate_calls == 2:
                target['status'] = 'PASS'
            if case == 'consumption_varies':
                per_test[unit_id].update(consumed=candidate_calls, complete=False, matchedFailurePrefix=True)
                tape['status'] = 'MATCHED_FAILURE_PREFIX'
            if case == 'unknown_result':
                target['status'] = 'ERROR'
            if case == 'different_input':
                tape['status'] = 'UNVERIFIED'
        return output

    monkeypatch.setattr(replay, 'replay_evaluation', controlled_replay)
    result = confirm_regression(baseline=baseline, input_path=path, baseline_archive=archive,
                                candidate_archive=archive, candidate_expected=replace(expected, code_version=3),
                                scenario_contract=CONTRACT, runner=SyntheticRunner())['result']
    row = next(row for row in result['checks'] if row['testId'] == check_id)
    assert row['status'] == 'UNVERIFIED' and row['reason'] == reason
    assert result['confirmedCount'] == 0


def test_partial_capture_is_preserved_privately_but_not_replayable(recorded, tmp_path):
    baseline, path, _archive, expected = recorded
    record = load_record(path, baseline['inputRecord']['sha256'])
    from evaluation.final_evaluation import RunnerOutcome
    tape = deepcopy(record['scopes']['final'])
    tape['status'] = 'UNVERIFIED'
    outcome = RunnerOutcome(None, 2, 'TEST_RUNNER_ERROR', IMAGE, 'docker', private_inputs=tape)
    descriptor = replay.record_inputs(tmp_path / 'partial', outcomes={'final': outcome, 'practice': outcome},
                                      report=baseline['report'], reference_report=baseline['referenceReport'],
                                      expected=expected, scenario_contract=CONTRACT,
                                      source_run_id=baseline['report']['sourceRunId'],
                                      architecture=baseline['report']['architecture'], exam='final', runner=SyntheticRunner())
    assert descriptor['status'] == 'UNVERIFIED' and descriptor['reason'] == 'INPUT_CAPTURE_INCOMPLETE'
    partial = load_record(tmp_path / 'partial' / descriptor['fileName'], descriptor['sha256'])
    assert partial['partialScopes']['final']['tests'] == tape['tests']
    assert partial['scopes'] == {}
    with pytest.raises(ValueError, match='INCOMPLETE'):
        validate_replay_binding(partial, expected=expected, source_run_id=baseline['report']['sourceRunId'],
                                architecture=baseline['report']['architecture'], exam='final',
                                scenario_contract=CONTRACT, runner=SyntheticRunner(), mode='repeat')


@pytest.fixture(scope='module', params=['case_sensitive_email', 'password_logged'])
def failing_record(request, tmp_path_factory):
    if not RUNTIME:
        pytest.skip('trusted synthetic fixture dependencies unavailable')
    folder = tmp_path_factory.mktemp('failure-inputs')
    archive = snapshot(request.param)
    expected = manifest(archive)
    output = final(archive, expected, SyntheticRunner(), private_input_dir=folder)
    assert output['report']['evaluationVerdict'] == 'FAIL'
    return request.param, output, folder / output['inputRecord']['fileName'], archive, expected


def test_real_failure_evidence_is_redacted_before_hashing_and_still_replays(failing_record):
    variant, output, path, archive, expected = failing_record
    raw = load_record(path, output['inputRecord']['sha256'])
    public = json.dumps(output)
    for scope in raw['scopes'].values():
        for rows in scope['tests'].values():
            for event in rows:
                if event['kind'] == 'email':
                    assert event['value'] not in public
                    assert event['value'].strip().lower() not in public
                    assert event['value'].upper() not in public
                if event['kind'] == 'password' and len(event['value']) >= 8:
                    assert event['value'] not in public
    if variant == 'case_sensitive_email':
        # Canonical-email observations now record only a boolean, so no raw
        # email enters the assertion message and no redaction marker is needed.
        canonical = [row for check in output['checkDetails']['checks'] for row in check['items']
                     if row['itemId'] == 'canonical' and row['status'] == 'FAIL']
        assert canonical and all(row['actual'] is False for row in canonical)
        assert 'AssertionError' in public
        assert '[PRIVATE_INPUT_' not in json.dumps(raw['privateDiagnostics'])
    assert raw['sourceReportSha256'] == digest(output['report'])
    repeated = repeat_evaluation(baseline=output, input_path=path, archive=archive, expected=expected,
                                 scenario_contract=CONTRACT, runner=SyntheticRunner())
    assert repeated['output']['report']['evaluationVerdict'] == 'FAIL'
    assert repeated['result']['status'] == 'PASS'  # the observed failure repeats


def test_original_failure_cannot_be_ignored_when_subsequent_baseline_passes(failing_record, monkeypatch):
    _variant, baseline, path, archive, expected = failing_record
    changed = next(row['testId'] for row in baseline['report']['checks'] if row['status'] == 'FAIL')
    suite = protected_tests.load_suite()

    def controlled_replay(**kwargs):
        output = deepcopy(baseline)
        output['inputReplay'] = {'scopes': {'final': {'status': 'PASS', 'error': None, 'checks': {
            protected_tests.unit_test_id(check, 'final', suite): {
                'consumed': 1, 'total': 1, 'complete': True, 'error': None, 'matchedFailurePrefix': False}
            for check in suite['checks']}}}}
        target = next(row for row in output['report']['checks'] if row['testId'] == changed)
        target['status'] = 'PASS' if kwargs['mode'] == 'repeat' else 'FAIL'
        return output

    monkeypatch.setattr(replay, 'replay_evaluation', controlled_replay)
    result = confirm_regression(baseline=baseline, input_path=path, baseline_archive=archive,
                                candidate_archive=archive, candidate_expected=replace(expected, code_version=3),
                                scenario_contract=CONTRACT, runner=SyntheticRunner())['result']
    row = next(row for row in result['checks'] if row['testId'] == changed)
    assert row['status'] == 'UNVERIFIED' and row['reason'] == 'EXECUTION_VARIATION_OBSERVED'
    assert row['initialBaselineStatus'] == 'FAIL'


def test_private_values_in_nested_bodies_and_short_quoted_inputs_are_redacted():
    password, email = 'PrivatePass234', 'Alice@Example.net'
    tape = {'tests': {'test': [
        {'kind': 'password', 'value': password},
        {'kind': 'request', 'arguments': {'body': {'nested': [{'secret': password, 'email': email,
                                                            'short': 'x', 'blank': ''}]}}},
    ]}}
    detail = f"AssertionError: '{password}' != '{email.lower()}' -- nested={password!r}, short='x', blank=''"
    report = {'tests': [{'testId': 'fixed-id-x', 'outcome': 'FAIL', 'details': detail}]}
    result = replay.public_unit_report(report, tape)
    safe = result['tests'][0]
    assert safe['testId'] == 'fixed-id-x' and safe['outcome'] == 'FAIL'
    assert password not in safe['details'] and email.lower() not in safe['details']
    assert "'x'" not in safe['details'] and "''" not in safe['details']
    assert 'AssertionError' in safe['details'] and '!=' in safe['details']
    assert '[PRIVATE_INPUT_' in safe['details']
    assert report['tests'][0]['details'] == detail


def test_missing_input_capture_hides_raw_details_without_losing_failure_or_skip_code():
    report = {'tests': [{'testId': 'a', 'outcome': 'FAIL', 'details': 'AssertionError: PrivatePass234'},
                        {'testId': 'b', 'outcome': 'SKIP', 'details': 'UNVERIFIED:APP_IMPORT_FAILED'}]}
    result = replay.public_unit_report(report, None)
    assert result['tests'][0]['outcome'] == 'FAIL' and 'PrivatePass234' not in json.dumps(result)
    assert result['tests'][1]['details'] == 'UNVERIFIED:APP_IMPORT_FAILED'

