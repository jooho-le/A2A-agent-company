from copy import deepcopy
from hashlib import sha256
import json

import pytest

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_input_failures import INVALID_INPUT


SOURCE = b'def canonicalize(value):\n    local, domain = value.rsplit("@", 1)\n    return local, domain\n'
FRAME = {'sourceSha256': sha256(SOURCE).hexdigest(), 'line': 2}
BEFORE_ENTRY = 'RESPONSE_BEFORE_SIGNUP_HANDLER'
FINGERPRINT = '7' * 64
PROTECTED_SUITE = 'artifact://protected-tests/scenario/practice-' + FINGERPRINT


def request(*, generated=False):
    manifest = {'codeVersion': 2, 'projectArtifactId': 'source', 'snapshotSha256': '1' * 64,
                'treeHash': '2' * 40, 'commitHash': '3' * 40, 'gitObjectFormat': 'sha1',
                'repositoryId': 'repo', 'containerImageDigest': 'sha256:' + '4' * 64,
                'dependencyLockHash': 'sha256:' + '5' * 64}
    source = {k: v for k, v in manifest.items() if k != 'projectArtifactId'}
    source.update(artifactId='source', runId='run')
    identity = {'runId': 'run', 'codeVersion': 2, 'sourceArtifactId': 'source', 'outcome': 'FAIL',
                'observationStatus': 'COMPLETE', 'executionManifest': manifest,
                'testVersion': ('inputs:' if generated else 'practice:') + FINGERPRINT}

    def status(case):
        return {'caseId': case, 'itemId': 'http_status', 'kind': 'assertion', 'actual': 500,
                'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH'}

    rows = [{**identity, 'testId': 'format', 'reportArtifactId': 'qa', 'requirementId': 'r2',
             'role': 'QA', 'observations': [status('invalid_email_1'), status('invalid_email_2')]},
            {**identity, 'testId': 'consistency', 'reportArtifactId': 'qa', 'requirementId': 'r7',
             'role': 'QA', 'observations': [status('step_3_invalid')]},
            {**identity, 'testId': 'exposure', 'reportArtifactId': 'sec', 'requirementId': 'r6',
             'role': 'SECURITY', 'observations': [
                 {'caseId': 'invalid_email', 'itemId': 'response_status', 'kind': 'precondition',
                  'status': 'UNVERIFIED', 'actual': None, 'reason': 'UNHANDLED_APP_EXCEPTION',
                  'handlerEntry': 'ENTERED', 'diagnostic': {'exceptionType': 'builtins.ValueError',
                      'escapedToClient': True, 'responseStarted': True, 'appFrame': FRAME}},
                 {'caseId': 'malformed_list', 'itemId': 'response_status', 'kind': 'precondition',
                  'status': 'PASS', 'actual': 422, 'reason': None, 'handlerEntry': 'NOT_ENTERED'},
                 {'caseId': 'malformed_list', 'itemId': 'password_exposed', 'kind': 'assertion', 'actual': True,
                  'status': 'FAIL', 'reason': 'EXPECTATION_MISMATCH', 'diagnostic': {'locations': ['BODY.detail[0].input']}}]}]
    issues = []
    for row in rows:
        issue = {k: row[k] for k in ('reportArtifactId', 'sourceArtifactId', 'codeVersion')}
        issue.update(issueId=row['testId'], requirementIds=[row['requirementId']], referenceId=row['testId'])
        if row['role'] == 'QA':
            issue['category'] = 'QA_ASSERTION_FAILURE'
        else:
            issue.update(category='SECURITY_REQUIREMENT_FAILURE', description=json.dumps({
                'code': 'MEASURED_SECURITY_TESTS', 'tests': [{'testId': row['testId'], 'outcome': 'FAIL'}]}))
        issues.append(issue)
    return {'runConfiguration': {'configuration': {'protectedTestSuiteRef': None if generated else PROTECTED_SUITE}},
            'fixRequest': {'inputArtifacts': [{'artifactType': 'SOURCE', 'artifactId': 'source', 'record': source}],
                           'issues': issues, 'validationFeedback': rows}}


def project(task, files=None):
    return compact_fix_input(task, source_files=lambda: {'main.py': SOURCE} if files is None else files)['activeRepairFocus']['brief']


@pytest.mark.parametrize('generated', [False, True], ids=['protected', 'observed-generated'])
def test_selected_measured_input_failures_share_one_path_and_do_not_inherit_another_cases_stack(generated):
    task = request(generated=generated)
    original = deepcopy(task)
    brief = project(task)
    assert task == original
    assert brief['readyReportPaths'] == sorted([INVALID_INPUT, BEFORE_ENTRY])
    group = brief['invalidInputServerError']
    assert [r['httpStatus'] for r in group['failedCases']] == [500, 500, 500]
    assert [r['caseId'] for r in group['failedCases']] == ['invalid_email_1','invalid_email_2','step_3_invalid']
    assert all('appFrame' not in c and 'expected' not in c for c in group['failedCases'])
    exc, = group['relatedExceptions']
    assert (exc['role'], exc['caseId'], exc['exceptionType'], exc['handlerEntry']) == (
        'SECURITY', 'invalid_email', 'builtins.ValueError', 'ENTERED')
    site, = group['sourceSites']
    assert (site['sourceRef'], site['line']) == ('source/main.py::canonicalize', 2)
    assert brief['sourceInspection']['byObservedPath'][INVALID_INPUT][0] == {
        'toolPath':'source/main.py', 'line':2, 'operation':'RELATED_EXCEPTION_FRAME'}
    paths = [t['checks'][0]['observedFailurePaths'] for t in brief['targets']]
    assert paths == [{INVALID_INPUT: ['invalid_email_1','invalid_email_2']},
                     {INVALID_INPUT: ['step_3_invalid']}, {BEFORE_ENTRY: ['malformed_list']}]


@pytest.mark.parametrize('field,value', [('runId', 'other'), ('sourceArtifactId', 'old'), ('codeVersion', 1),
                                         ('observationStatus', 'UNVERIFIED'), ('testVersion', 'final:' + FINGERPRINT)])
def test_unbound_security_row_cannot_become_related_evidence(field, value):
    task = request()
    task['fixRequest']['validationFeedback'][2][field] = value
    group = project(task)['invalidInputServerError']
    assert group['failedCases'] and not group['relatedExceptions'] and not group['sourceSites']


def test_only_known_source_and_authorized_test_versions_can_request_new_ready_path():
    for mutation in ('other-suite', 'ver2', 'stale-manifest', 'source-missing', 'unmeasured'):
        task = request()
        fix = task['fixRequest']
        if mutation in ('other-suite', 'ver2'):
            task['runConfiguration']['configuration']['protectedTestSuiteRef'] = (
                None if mutation == 'ver2' else 'artifact://protected-tests/scenario/practice-' + '8' * 64)
        elif mutation == 'stale-manifest':
            fix['inputArtifacts'][0]['record']['snapshotSha256'] = '9' * 64
        elif mutation == 'source-missing':
            fix['inputArtifacts'] = []
        else:
            for row in fix['validationFeedback']:
                row.update(observationStatus='UNVERIFIED', observations=[])
        brief = project(task)
        assert 'invalidInputServerError' not in brief, mutation
        assert INVALID_INPUT not in brief.get('readyReportPaths', []), mutation


def test_stale_frame_duplicate_files_excluded_targets_and_absent_generated_details_are_not_promoted():
    group = project(request(), {'main.py': SOURCE + b'# another revision\n'})['invalidInputServerError']
    assert group['relatedExceptions'] and not group['sourceSites']
    assert project(request(), {'a.py': SOURCE, 'b.py': SOURCE})['invalidInputServerError']['sourceSites'] == []
    task = request(generated=True)
    task['fixRequest']['issues'] = task['fixRequest']['issues'][2:]
    assert 'invalidInputServerError' not in project(task)
    task = request(generated=True)
    for row in task['fixRequest']['validationFeedback']:
        row.update(observationStatus='UNVERIFIED', observations=[])
    assert not project(task).get('readyReportPaths')


def test_case_summary_is_bounded_and_full_selected_checks_remain_available():
    task = request()
    row = task['fixRequest']['validationFeedback'][0]
    first = row['observations'][0]
    row['observations'] = [{**first, 'caseId': f'invalid_email_{i}'} for i in range(32)]
    brief = project(task)
    assert brief['invalidInputServerError']['failedCaseCount'] == 33
    assert len(brief['invalidInputServerError']['failedCases']) == 16
    assert len(brief['targets'][0]['checks'][0]['observedFailurePaths'][INVALID_INPUT]) == 32
    assert brief['targets'][1]['checks'][0]['observedFailurePaths'][INVALID_INPUT] == ['step_3_invalid']


def test_unverified_or_non_5xx_values_never_generate_an_invalid_input_status():
    task = request()
    for i, row in enumerate(task['fixRequest']['validationFeedback'][:2]):
        for obs in row['observations']:
            obs.update(status='UNVERIFIED' if i else 'FAIL', actual=None if i else 422)
    brief = project(task)
    assert 'invalidInputServerError' not in brief
    assert INVALID_INPUT not in brief.get('readyReportPaths', [])


@pytest.mark.parametrize('handler,status,case', [('UNKNOWN','UNVERIFIED','invalid_email'),
                                                ('NOT_ENTERED','UNVERIFIED','invalid_email'),
                                                ('ENTERED','NOT_RUN','invalid_email'),
                                                ('ENTERED','UNVERIFIED','db_error')])
def test_other_response_boundaries_do_not_link_to_input_validation(handler,status,case):
    task = request()
    task['fixRequest']['validationFeedback'][2]['observations'][0].update(handlerEntry=handler,status=status,caseId=case)
    assert not project(task)['invalidInputServerError']['relatedExceptions']


@pytest.mark.parametrize('generated', [False, True], ids=['practice', 'generated-evidence-only'])
def test_each_recorded_500_carries_its_own_origin_without_inheriting_security_observations(generated):
    task = request(generated=generated)
    first, second = task['fixRequest']['validationFeedback'][0]['observations']
    first['statusOrigin'], second['statusOrigin'] = 'HTTP_RESPONSE', 'ESCAPED_EXCEPTION'
    brief = project(task)
    expected = {'invalid_email_1':'HTTP_RESPONSE', 'invalid_email_2':'ESCAPED_EXCEPTION', 'step_3_invalid':'UNKNOWN'}
    assert {row['caseId']:row['statusOrigin'] for row in brief['invalidInputServerError']['failedCases']} == expected
    for target in brief['targets']:
        for check in target['checks']:
            for case, sample in check['caseFeedback'].items():
                if case in expected:
                    assert sample['statusOrigin'] == expected[case]
