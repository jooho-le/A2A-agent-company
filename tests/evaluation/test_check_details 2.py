"""Safe sub-check observations from synthetic fixtures; no model/API calls."""
from copy import deepcopy
from dataclasses import replace
import json

import pytest

from evaluation.check_details import build_details, expected_plans, render_details, write_details
from evaluation.final_evaluation import LocalRunner, _parse_details
from evaluation.protected_tests.tests.signup.signup_details import Recorder, matches
from tests.evaluation.test_final_evaluation import IMAGE, LOCK, canonical_tar, final, manifest, snapshot
from tests.evaluation import signup_service


class CaptureRunner:
    kind = 'docker'

    def __init__(self):
        self.receipts = []

    def run(self, *args, **kwargs):
        result = LocalRunner(trusted=True).run(*args, **kwargs)
        self.receipts.append(result)
        return replace(result, image_digest=IMAGE, kind=self.kind)


@pytest.fixture(scope='module')
def good():
    archive = snapshot()
    runner = CaptureRunner()
    output = final(archive, manifest(archive), runner)
    return output, runner


def test_all_nine_checks_have_complete_same_execution_details(good):
    output, runner = good
    assert len(runner.receipts) == 3  # existing final/practice/coverage only, no extra run
    details = output['checkDetails']
    assert details['recordStatus'] == 'COMPLETE'
    assert details['evaluationId'] == output['report']['evaluationId']
    assert details['suite'] == output['report']['suite']
    assert len(details['checks']) == 9
    for check in details['checks']:
        assert check['parentStatus'] == 'PASS'
        assert check['recordIssue'] is None
        assert all(row['status'] == 'PASS' for row in check['items'])
        assert all(row['actual'] is not None for row in check['items'])
    qa1 = details['checks'][0]
    assert len([row for row in qa1['items'] if row['kind'] == 'assertion']) == 6
    assert sum(len(row['items']) for row in details['checks']) == 139


def test_public_detail_values_do_not_contain_private_inputs(good):
    output, runner = good
    text = json.dumps(output['checkDetails']) + render_details(output['checkDetails'])
    tape = runner.receipts[0].private_inputs
    for events in tape['tests'].values():
        for event in events:
            if event['kind'] in ('password', 'email') and len(event['value']) >= 8:
                assert event['value'] not in text
    assert '$argon2id$' not in text


@pytest.mark.parametrize('variant', ['weak_argon2_memory', 'plaintext_storage', 'no_commit'])
def test_failed_observations_keep_independent_results_and_block_only_dependencies(variant):
    archive = snapshot(variant)
    output = final(archive, manifest(archive), CaptureRunner())
    assert output['report']['evaluationVerdict'] in ('FAIL', 'UNVERIFIED')
    rows = [row for check in output['checkDetails']['checks'] for row in check['items']]
    assert any(row['status'] == 'FAIL' and row['actual'] is not None for row in rows)
    if variant == 'weak_argon2_memory':
        check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-002')
        by_key = {(r['caseId'], r['itemId']): r for r in check['items']}
        assert check['parentStatus'] == 'FAIL' and check['recordIssue'] is None
        assert by_key['account_1', 'm']['actual'] == 1024
        assert by_key['account_2', 'm']['status'] == 'FAIL'
        for key in [('account_1', 't'), ('account_1', 'p'), ('storage', 'distinct_salts'),
                    ('account_1', 'correct_password'), ('account_1', 'wrong_password')]:
            assert by_key[key]['status'] == 'PASS'
        assert not any(r['status'] == 'NOT_RUN' for r in check['items'])
    else:
        assert any(row['status'] == 'NOT_RUN' and row['actual'] is None for row in rows)
    if variant == 'plaintext_storage':
        check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-002')
        by_key = {(r['caseId'], r['itemId']): r for r in check['items']}
        assert by_key['storage', 'no_plaintext']['status'] == 'FAIL'
        assert by_key['storage', 'argon2id']['status'] == 'FAIL'
        for row in check['items']:
            if row['status'] == 'NOT_RUN':
                assert row['reason'] == 'HASH_FORMAT_REQUIRED'


@pytest.mark.parametrize('variant,check_id,case', [
    ('seven_char_password', 'QA-SIGNUP-004', 'reject_length_7'),
    ('invalid_email_accepted', 'QA-SIGNUP-002', 'invalid_email_1'),
])
def test_bad_response_does_not_skip_database_observations(variant, check_id, case):
    archive = snapshot(variant)
    output = final(archive, manifest(archive), CaptureRunner())
    check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == check_id)
    rows = {r['itemId']: r for r in check['items'] if r['caseId'] == case}
    assert check['parentStatus'] == 'FAIL'
    assert rows['http_status']['actual'] == 201 and rows['http_status']['status'] == 'FAIL'
    assert rows['user_delta']['actual'] == 1 and rows['user_delta']['status'] == 'FAIL'
    assert rows['rows_unchanged']['actual'] is False and rows['rows_unchanged']['status'] == 'FAIL'


def test_canonical_failure_does_not_skip_independent_unique_check():
    archive = snapshot('case_sensitive_email')
    output = final(archive, manifest(archive), CaptureRunner())
    check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-003')
    rows = {(r['caseId'], r['itemId']): r for r in check['items']}
    failed = [r for r in check['items'] if r['itemId'] == 'canonical' and r['status'] == 'FAIL']
    assert failed and check['parentStatus'] == 'FAIL'
    assert all(rows[r['caseId'], 'unique']['status'] == 'PASS' for r in failed)


@pytest.mark.parametrize('problem', ['invalid_hash', 'signup_failed', 'response_error'])
def test_one_unusable_account_does_not_block_other_accounts(problem):
    files = signup_service.build('good')
    if problem == 'invalid_hash':
        files['security.py'] += '''
_real_hash = hash_password
_count = 0
def hash_password(password):
    global _count
    _count += 1
    return password if _count == 1 else _real_hash(password)
'''
    elif problem == 'response_error':
        files['routers/auth.py'] = files['routers/auth.py'].replace(
            '    return {"success": True}', '    raise HTTPException(status_code=503)')
    else:
        files['routers/auth.py'] = files['routers/auth.py'].replace(
            'router = APIRouter', '_count = 0\nrouter = APIRouter').replace(
            '    if db.scalar', '    global _count\n    _count += 1\n    if _count == 1:\n'
            '        raise HTTPException(status_code=503)\n    if db.scalar')
    files['requirements.lock'] = LOCK.decode()
    archive = canonical_tar({path: value.encode() for path, value in files.items()})
    output = final(archive, manifest(archive), CaptureRunner())
    check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-002')
    rows = {(r['caseId'], r['itemId']): r for r in check['items']}
    if problem == 'response_error':
        assert check['parentStatus'] == 'ERROR'
        assert rows['account_1_setup', 'http_status']['status'] == 'FAIL'
        assert all(row['status'] == 'PASS' for row in check['items'] if row['kind'] == 'assertion')
        return
    reason = 'HASH_FORMAT_REQUIRED' if problem == 'invalid_hash' else 'HASH_VALUES_REQUIRED'
    assert rows['account_1', 'm']['status'] == 'NOT_RUN'
    assert rows['account_1', 'm']['reason'] == reason
    for item in ('m', 't', 'p', 'correct_password', 'wrong_password'):
        assert rows['account_2', item]['status'] == 'PASS'
    assert check['parentStatus'] == ('FAIL' if problem == 'invalid_hash' else 'ERROR')


def test_wrong_argon2_algorithm_does_not_block_readable_parameters_or_verification():
    archive = snapshot('argon2i_type')
    output = final(archive, manifest(archive), CaptureRunner())
    check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-002')
    rows = {(r['caseId'], r['itemId']): r for r in check['items']}
    assert check['parentStatus'] == 'FAIL'
    assert rows['storage', 'argon2id']['status'] == 'FAIL'
    assert all(row['status'] == 'PASS' for row in check['items'] if row['itemId'] != 'argon2id')


def test_security_leak_still_fails_parent_after_later_passes():
    archive = snapshot('hash_in_response')
    output = final(archive, manifest(archive), CaptureRunner())
    check = next(c for c in output['checkDetails']['checks'] if c['checkId'] == 'SEC-SIGNUP-005')
    rows = {(r['caseId'], r['itemId']): r for r in check['items']}
    assert rows['success', 'hash_exposed']['status'] == 'FAIL'
    assert rows['application_logs', 'hash_exposed']['status'] == 'PASS'
    assert check['parentStatus'] == 'FAIL' and check['recordIssue'] is None


def test_failed_setup_is_unverified_and_followups_are_not_run():
    archive = snapshot('app_missing')
    output = final(archive, manifest(archive), CaptureRunner())
    for check in output['checkDetails']['checks']:
        assert check['items'][0]['status'] == 'UNVERIFIED'
        assert all(row['status'] == 'NOT_RUN' and row['actual'] is None for row in check['items'][1:])


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'expected', 'actual_secret', 'verdict', 'reason_secret', 'extra_check', 'parent'])
def test_corrupt_receipts_never_create_a_detailed_pass(good, damage):
    output, runner = good
    payload = deepcopy(runner.receipts[0].detail_results)
    report = deepcopy(output['report'])
    key = 'QA-SIGNUP-001'
    row = payload['checks'][key][1]
    if damage == 'missing': payload['checks'][key].pop()
    elif damage == 'duplicate': payload['checks'][key][1] = deepcopy(payload['checks'][key][0])
    elif damage == 'expected': row['expected']['value'] = [100, 599]
    elif damage == 'actual_secret': row['actual'] = 'SECRET_SHOULD_NOT_BE_PUBLISHED'
    elif damage == 'verdict': row['status'] = 'FAIL'
    elif damage == 'reason_secret': row['reason'] = 'SECRET_SHOULD_NOT_BE_PUBLISHED'
    elif damage == 'extra_check': payload['checks']['QA-UNKNOWN'] = []
    elif damage == 'parent': report['checks'][0]['status'] = 'FAIL'
    details = build_details(report, payload, exam='final')
    assert details['recordStatus'] == 'UNVERIFIED'
    assert 'SECRET_SHOULD_NOT_BE_PUBLISHED' not in json.dumps(details)
    assert details['checks'][0]['recordIssue'] is not None


@pytest.mark.parametrize('problem', ['missing', 'runner_error', 'manifest', 'suite'])
def test_missing_or_unbound_evidence_is_unknown_not_not_run(good, problem):
    output, runner = good
    report = deepcopy(output['report'])
    payload = runner.receipts[0].detail_results
    if problem == 'manifest': report['manifestCheck']['metadataMatches'] = False
    if problem == 'suite': report['suite']['sha256'] = '0' * 64
    details = build_details(report, None if problem == 'missing' else payload, exam='final',
                            runner_error='TIMEOUT' if problem == 'runner_error' else None)
    assert details['recordStatus'] == 'UNVERIFIED'
    assert all(row['status'] == 'UNVERIFIED' and row['actual'] is None for c in details['checks'] for row in c['items'])


def test_separate_files_are_written_from_existing_result(good, tmp_path):
    output, runner = good
    calls = len(runner.receipts)
    def write(path, data, force):
        path.write_text(data if isinstance(data, str) else json.dumps(data))
    write_details(tmp_path, output, write)
    assert json.loads((tmp_path / 'check_details.json').read_text()) == output['checkDetails']
    text = (tmp_path / 'check_details.md').read_text()
    assert '기대값 | 실제값 | 결과' in text
    assert all(check['checkId'] in text for check in output['checkDetails']['checks'])
    assert len(runner.receipts) == calls


def test_each_exam_has_a_plan_for_every_check():
    for exam in ('practice', 'pilot', 'final'):
        plans = expected_plans(exam)
        assert len(plans) == 9
        assert all(len({(r['caseId'], r['itemId']) for r in rows}) == len(rows) for rows in plans.values())


def test_recorder_marks_interrupted_observation_unverified():
    rec = Recorder('QA-SIGNUP-001', {'emails': ['x'], 'length': 8})
    def fail(): raise RuntimeError('PRIVATE_DATABASE_ERROR')
    with pytest.raises(RuntimeError): rec.observe('setup', 'ready', fail)
    rec.finish('PRECONDITION_OR_OBSERVATION_UNAVAILABLE')
    assert rec.rows[0]['status'] == 'UNVERIFIED'
    assert all(row['status'] == 'NOT_RUN' for row in rec.rows[1:])
    assert 'PRIVATE_DATABASE_ERROR' not in json.dumps(rec.rows)


def test_dependency_reason_survives_finalization_and_cannot_be_overwritten():
    rec = Recorder('SEC-SIGNUP-002', {'accounts': 2})
    rec.block('account_1', ['m'], 'HASH_FORMAT_REQUIRED')
    rec.finish('PRECONDITION_OR_OBSERVATION_UNAVAILABLE')
    row = rec.index['account_1', 'm']
    assert (row['status'], row['actual'], row['reason']) == ('NOT_RUN', None, 'HASH_FORMAT_REQUIRED')
    with pytest.raises(ValueError, match='DETAIL_OBSERVATION_DUPLICATE'):
        rec.observe('account_1', 'm', 19456)


def test_duplicate_detail_frame_is_not_accepted():
    line = json.dumps({'protected-details-v1': {'schemaVersion': 1, 'checks': {}}})
    assert _parse_details(line + '\n' + line + '\n{}') is None


def test_boolean_is_not_a_numeric_observation():
    assert not matches({'operator': 'range', 'value': [0, 400]}, True)
    assert not matches({'operator': 'eq', 'value': 1}, True)


def test_duplicate_json_keys_and_oversized_frames_are_not_accepted():
    duplicate = '{"protected-details-v1":{"schemaVersion":1,"schemaVersion":2,"checks":{}}}\n{}'
    assert _parse_details(duplicate) is None
    assert _parse_details('{"protected-details-v1":' + ' ' * (1024 * 1024) + '{}}\n{}') is None


def test_metadata_number_and_boolean_are_not_interchangeable(good):
    output, runner = good
    payload = deepcopy(runner.receipts[0].detail_results)
    payload['checks']['QA-SIGNUP-001'][0]['expected']['value'] = 1
    details = build_details(output['report'], payload, exam='final')
    assert details['checks'][0]['recordIssue'] == 'DETAIL_RECORD_INVALID'


def test_compacted_protected_copy_cannot_hide_in_a_source_snapshot():
    from evaluation import protected_tests as pt
    content = pt.protected_files('final')['tests/signup/signup_details.py']
    assert pt.exam_exposure({'disguised.py': content.encode()})[0]['reason'] == 'SAME_AS_PROTECTED_FILE'


@pytest.mark.parametrize('source', [
    'x = " spaces # inside a string "\n',
    'def f(x):\n    """line one\n    line two\n\n    last line"""\n    return f"value {x + 1}"\n',
    'x = 1 .real\ny = not False and True\n',
    'values = (\n    " spaces # inside ",\n    1 + 2,\n    f"value {3 + 1}",\n)\n',
])
def test_transport_compaction_preserves_literals_and_token_boundaries(source):
    import ast
    from evaluation.protected_tests import compact_source
    compact = compact_source(source)
    assert ast.dump(ast.parse(compact)) == ast.dump(ast.parse(source))
    ast.parse(compact, feature_version=(3, 10))


def test_transport_spelling_is_independent_of_ast_unparse_version():
    from evaluation.protected_tests import compact_source
    source = 'a, b = (1, 2)  # remove this comment\nfor (i, item) in enumerate([a]):\n    text = f"value {i + 1}: {item}"\n'
    # In 3.12+ f-strings have inner tokens; in 3.10 they are one STRING.
    # Neither inner spacing nor existing tuple parentheses may be rewritten.
    assert compact_source(source) == 'a,b=(1,2)\nfor(i,item)in enumerate([a]):\n text=f"value {i + 1}: {item}"\n'
