from types import SimpleNamespace
from uuid import uuid4

import pytest

from diagnostics.focused_measurement import _select_case, measure_practice
from diagnostics.stage_replay import ReplayError
from evaluation import protected_tests
from orchestrator.domain.scenario_registry import SCN_001_ID, get_scenario


@pytest.fixture(scope='module')
def original_case():
    contract = get_scenario(SCN_001_ID).planner_contract()
    suite = protected_tests.load_suite()
    ref = protected_tests.loop_suite_ref(suite)
    config = SimpleNamespace(configuration=SimpleNamespace(protected_test_suite_ref=ref), scenario_contract=contract)
    source = SimpleNamespace(run_id=uuid4(), previous_artifact_id=uuid4(), code_version=2)
    case = next(x for x in protected_tests.owned_cases(contract, 'QA', suite)['cases']
                if 'test_qa_signup_001_' in x['testId'])
    row = {'hostDecision': 'FIX_TARGET', 'role': 'QA', 'outcome': 'FAIL',
           'codeVersion': 1, 'runId': str(source.run_id), 'sourceArtifactId': str(source.previous_artifact_id),
           'testId': ':'.join(case[k] for k in ('toolName', 'selector', 'testId')),
           'requirementId': case['requirementId'], 'testVersion': 'practice:' + ref.rsplit('practice-', 1)[1]}
    return config, source, row


def _select(config, source, rows):
    return _select_case(config, source, 'QA-SIGNUP-001', {'fixRequest': {'validationFeedback': rows}})


def test_retest_reuses_original_practice_role_files(original_case):
    config, source, row = original_case
    test_id, files = _select(config, source, [row])
    assert row['testId'].endswith(':' + test_id)
    assert files == protected_tests.protected_files('in-loop-qa')


@pytest.mark.parametrize('different', [
    {'testVersion': 'other'}, {'sourceArtifactId': str(uuid4())}, {'runId': str(uuid4())},
    {'hostDecision': 'KEEP_PASSING'}, {'outcome': 'PASS'}, {'role': 'SECURITY'}, {'requirementId': str(uuid4())},
])
def test_foreign_or_unauthorized_measurements_do_not_trigger_test(original_case, different):
    config, source, row = original_case
    with pytest.raises(ReplayError, match='FOCUSED_PREVIOUS_FAILURE_REQUIRED'):
        _select(config, source, [{**row, **different}])


def test_missing_or_duplicate_origin_is_rejected(original_case):
    config, source, row = original_case
    for rows in ([], [row, row]):
        with pytest.raises(ReplayError, match='FOCUSED_PREVIOUS_FAILURE_REQUIRED'):
            _select(config, source, rows)


def test_ver2_without_protected_suite_cannot_receive_its_measurement(original_case):
    config, source, row = original_case
    ver2 = SimpleNamespace(configuration=SimpleNamespace(protected_test_suite_ref=None), scenario_contract=config.scenario_contract)
    with pytest.raises(ReplayError, match='FOCUSED_SUITE_CHANGED'):
        _select(ver2, source, [row])


def test_existing_result_is_preserved_before_any_docker_work(tmp_path):
    result = tmp_path / 'focused-result.json'
    result.write_bytes(b'preserve first measurement')
    with pytest.raises(ReplayError, match='FOCUSED_RESULT_ALREADY_EXISTS'):
        measure_practice(tmp_path, 'QA-SIGNUP-001')
    assert result.read_bytes() == b'preserve first measurement'


@pytest.mark.parametrize('observed', [
    None,
    {'status': 'UNVERIFIED', 'sha256': 'lock', 'imageDigest': 'image'},
    {'status': 'OBSERVED', 'sha256': 'other-lock', 'imageDigest': 'image'},
    {'status': 'OBSERVED', 'sha256': 'lock', 'imageDigest': 'other-image'},
])
def test_stored_measurement_cannot_borrow_a_different_image_or_lock(observed):
    from diagnostics.focused_feedback import measurement_feedback
    source = SimpleNamespace(snapshot_sha256='snap', code_version=2, artifact_id='src',
                             dependency_lock_hash='lock', container_image_digest='image')
    result = {'snapshotSha256': 'snap', 'sourceArtifactId': 'src', 'codeVersion': 2, 'imageDependencyLock': observed}
    with pytest.raises(ReplayError, match='FOCUSED_IMAGE_OR_LOCK_UNAVAILABLE'):
        measurement_feedback(source, None, result)


def test_historical_076_can_be_read_only_with_its_original_observations_and_fingerprint(original_case):
    from diagnostics.focused_feedback import measurement_feedback
    from evaluation import check_details

    ref = 'artifact://protected-tests/scn-001/signup-0.7.6/practice-ceac570cbc9662a26c2d77e1e6d7452281c73b5c4ea25f51e4579648516a86e5'
    current_config, _, _ = original_case
    config = SimpleNamespace(configuration=SimpleNamespace(protected_test_suite_ref=ref),
                             scenario_contract=current_config.scenario_contract)
    source = SimpleNamespace(snapshot_sha256='snap', code_version=2, run_id=uuid4(), artifact_id=uuid4())
    key = 'QA-SIGNUP-001'
    suite = protected_tests.load_suite()
    test_id = protected_tests.unit_test_id(next(c for c in suite['checks'] if c['key'] == key), 'practice', suite)
    rows = []
    for definition in check_details.expected_plans('practice', suite)[key]:
        expectation = definition['expected']
        value = expectation['value'][0] if expectation['operator'] == 'range' else expectation['value']
        rows.append({**definition, 'actual':value, 'status':'PASS', 'reason':None})
    failed = next(r for r in rows if r['itemId'] == 'http_status')
    failed.update(actual=500, status='FAIL', reason='EXPECTATION_MISMATCH')
    result = {'snapshotSha256':'snap', 'codeVersion':2,
              'scopes':{'practice':{'testId': test_id, 'outcome':'FAIL', 'details':{'checks':{key:rows}}}}}
    with pytest.raises(ReplayError, match='FOCUSED_SUITE_CHANGED'):
        measurement_feedback(source, config, result)
    old = measurement_feedback(source, config, result, allow_historical=True)
    assert old['testVersion'] == 'practice:' + ref.rsplit('practice-', 1)[1]
    assert all('statusOrigin' not in r for r in old['observations'])
    failed['statusOrigin'] = 'HTTP_RESPONSE'
    with pytest.raises(ReplayError, match='FOCUSED_SUITE_CHANGED'):
        measurement_feedback(source, config, result, allow_historical=True)
