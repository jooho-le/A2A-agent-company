from copy import deepcopy
from hashlib import sha256
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agents.runtime.developer_input import compact_fix_input
from agents.runtime.repair_call_history import recorded_call_edits


ENV = {
    'containerImageDigest': 'sha256:c712fdf445fe01217001440459070bf1e2b0a8d94ad3b9103bacfedddf7dae5a',
    'dependencyLockHash': 'sha256:d27679005b83257d24f6f7e86fdc7b1a47c92065bc068c4ab14e904675e6dcc0',
}
OLD = {'main.py': b"from argon2 import PasswordHasher\ndef register():\n    x = PasswordHasher(type='private-old')\n"}
NEW = {'main.py': OLD['main.py'].replace(b"    x", b"    # line shift\n    x").replace(b'private-old', b'private-new')}


def make_rows():
    observations = [{'caseId': 'valid_email_1', 'itemId': item, 'status': 'FAIL', 'actual': value,
                     'kind': 'assertion', 'reason': 'EXPECTATION_MISMATCH'}
                    for item, value in (('http_status', 500), ('account_count', 0), ('user_delta', 0))]
    common = {'requirementId': 'req', 'runId': 'r', 'codeVersion': 1, 'sourceArtifactId': 'v1',
              'executionManifest': {'snapshotSha256': 'snap1'}, 'outcome': 'FAIL', 'observationStatus': 'COMPLETE',
              'testVersion': 'inputs:' + 'c' * 64}
    qa = {**common, 'role': 'QA', 'reportArtifactId': 'q1', 'testId': 'normal', 'observations': observations}
    diagnostic = {'escapedToClient': True, 'responseStarted': False, 'exceptionType': 'builtins.TypeError',
                  'appFrame': {'sourceSha256': sha256(OLD['main.py']).hexdigest(), 'line': 3}}
    security = {**common, 'role': 'SECURITY', 'reportArtifactId': 's1', 'testId': 'boundary',
                'observations': [{'caseId': 'success', 'itemId': 'response_status', 'status': 'UNVERIFIED',
                                  'kind': 'precondition', 'reason': 'UNHANDLED_APP_EXCEPTION', 'actual': None,
                                  'diagnostic': diagnostic}]}
    review = SimpleNamespace(rechecks=[SimpleNamespace(role=r['role'], requirement_id='req', test_id=r['testId'],
                                    prior_report_id=r['reportArtifactId']) for r in (qa, security)])
    return [qa, security], review


def project(rows, edits, mutate=None):
    current = deepcopy(rows[0])
    current.update(codeVersion=2, sourceArtifactId='v2', executionManifest={'snapshotSha256':'snap2'},
                   reportArtifactId='q2')
    issues = [{'category': 'QA_ASSERTION_FAILURE', 'issueId': 'I', 'referenceId': 'normal',
               'reportArtifactId': 'q2', 'requirementIds': ['req']}]
    if mutate:
        mutate(current, issues)
    history = {'beforeVersion': 1, 'afterVersion': 2, 'beforeSnapshot':'snap1', 'afterSnapshot': 'snap2',
               'previousRows': rows, 'afterSourceId':'v2', 'recordedCallEdits': edits, 'changedFiles':['main.py']}
    return compact_fix_input({'fixRequest': {'issues': issues, 'validationFeedback':[current]}}, repair_history=history)


def test_pinned_history_is_available_without_host_library_and_has_no_private_literals():
    rows, review = make_rows()
    with patch('agents.runtime.normal_request_repair._public_exports', return_value=()):
        edits = recorded_call_edits(OLD, NEW, rows, review, ENV)
    task = project(rows, edits)
    edit, = task['previousRepairResult']['measuredCallEdits']
    assert edit['priorEvidence'][0]['testId'] == 'boundary'
    assert edit['priorExceptionSite']['line'] == 3 and edit['candidateSourceSite']['line'] == 4
    assert edit['argumentEdits'] == [{'parameter':'type', 'beforeShape':'str', 'afterShape':'str'}]
    assert edit['libraryContract']['parameters'] == {'type':'argon2.low_level.Type'}
    check, = edit['unchangedChecks']
    assert check['status'] == 'NO_PROGRESS' and check['testId'] == 'normal'
    assert check['unchangedCases'] == [{'caseId':'valid_email_1', 'httpStatus':500, 'statusOrigin':'UNKNOWN', 'account_count':0, 'user_delta':0}]
    assert 'private-' not in json.dumps(task)
    assert 'exceptionTypes' not in edit['candidateSourceSite']


@pytest.mark.parametrize('mutate', [
    lambda row, issues: row.update(testVersion='inputs:' + 'b' * 64),
    lambda row, issues: row.update(runId='other'),
    lambda row, issues: row.update(sourceArtifactId='elsewhere'),
    lambda row, issues: row['executionManifest'].update(snapshotSha256='elsewhere'),
    lambda row, issues: row['observations'][0].update(actual=201, reason=None, status='PASS'),
    lambda row, issues: issues.clear(),
])
def test_only_current_authorized_no_progress_measurements_are_projected(mutate):
    rows, review = make_rows()
    edits = recorded_call_edits(OLD, NEW, rows, review, ENV)
    assert 'measuredCallEdits' not in project(rows, edits, mutate)['previousRepairResult']


def test_ambiguous_source_or_unselected_old_exception_report_has_no_derived_link():
    rows, review = make_rows()
    assert not recorded_call_edits(OLD, {**NEW, 'argon2.py': b''}, rows, review, ENV)
    assert not recorded_call_edits({**OLD, 'clone.py':OLD['main.py']}, NEW, rows, review, ENV)
    assert not recorded_call_edits(OLD, {'main.py': NEW['main.py'] + b"    y = PasswordHasher(type='other')\n"}, rows, review, ENV)
    assert not recorded_call_edits(OLD, NEW, rows, SimpleNamespace(rechecks=review.rechecks[:1]), ENV)
    rows[1]['sourceArtifactId'] = 'other-source'
    assert not recorded_call_edits(OLD, NEW, rows, review, ENV)


@pytest.mark.parametrize('scenario', ['same', 'changed-inputs', 'bad-prior-diagnostic', 'bad-current-diagnostic',
                                      'missing-prior-receipt', 'wrong-current-manifest', 'unrelated-test'])
def test_generated_ver2_channel_only_recheck_uses_bound_unit_evidence(scenario):
    from agents.runtime.repair_call_history import channel_receipts

    detailed, _ = make_rows()
    diagnostic = detailed[1]['observations'][0]['diagnostic']
    template = {**detailed[0], 'testId':'run_unit_tests:qa-unit:local.Case.test_output',
                'observationStatus':'UNVERIFIED', 'reason':'DETAIL_RECORD_MISSING', 'observations':[],
                'outputChecks':{'SUCCESS_RESPONSE':'INCOMPLETE', 'REJECTION_RESPONSE':'EXPOSED'},
                'outputDiagnostics':{'SUCCESS_RESPONSE':[diagnostic]}}
    old = {**template, 'evidenceRefs':['unit-v1']}
    new = {**deepcopy(template), 'evidenceRefs':['unit-v2'], 'reportArtifactId':'q2', 'codeVersion':2,
           'sourceArtifactId':'v2', 'executionManifest':{'snapshotSha256':'snap2'}}
    def receipts(row, bad):
        manifest = row['executionManifest']
        proof = SimpleNamespace(tool_name='run_unit_tests', outcome=SimpleNamespace(value='PASS'),
                                execution_manifest={'snapshotSha256':'other'} if bad else manifest,
                                evidence_ref=row['evidenceRefs'][0])
        test = SimpleNamespace(requirement_id='req',test_id=row['testId'],tool_evidence=proof,execution_evidence=[])
        report = SimpleNamespace(artifact_type='QA_REPORT', artifact_id=row['reportArtifactId'], run_id='r',
                                 execution_manifest=manifest, tests=[test])
        snapshot = SimpleNamespace(run_id='r', execution_manifest=lambda: manifest)
        return channel_receipts([report], snapshot, [row])
    if scenario == 'changed-inputs':
        new['testVersion'] = 'inputs:' + 'd' * 64
    if scenario == 'bad-prior-diagnostic':
        old['outputDiagnostics']['SUCCESS_RESPONSE'][0]['exceptionType'] = 'unsafe'
    if scenario == 'bad-current-diagnostic':
        new['outputDiagnostics']['SUCCESS_RESPONSE'][0]['exceptionType'] = 'unsafe'
    if scenario == 'unrelated-test':
        new['testId'] += '_other'
    old_keys = receipts(old, scenario == 'missing-prior-receipt')
    new_keys = receipts(new, scenario == 'wrong-current-manifest')
    review = SimpleNamespace(rechecks=[SimpleNamespace(role='QA',requirement_id='req',test_id=old['testId'],
                                                       prior_report_id=old['reportArtifactId'])])
    edits = recorded_call_edits(OLD, NEW, [old], review, ENV, measured_channels=old_keys)
    history = {'beforeSnapshot':'snap1','afterSnapshot':'snap2','beforeVersion':1,'afterVersion':2,
               'afterSourceId':'v2','afterChannelReceipts':new_keys,'previousRows':[old],'recordedCallEdits':edits}
    issue = {'issueId':'current','reportArtifactId':'q2','referenceId':new['testId'],'requirementIds':['req'],
             'category':'QA_ASSERTION_FAILURE'}
    value = compact_fix_input({'fixRequest':{'issues':[issue],'validationFeedback':[new]}}, repair_history=history)
    found = value['previousRepairResult'].get('measuredCallEdits')
    if scenario == 'same':
        effect, = found[0]['unchangedChecks']
        assert effect['testId'] == new['testId'] and effect['unchangedOutputChannels'] == new['outputChecks']
        assert 'unchangedCases' not in effect and 'httpStatus' not in json.dumps(found)
    else:
        assert not found


@pytest.mark.parametrize('prior_origin', [None, 'HTTP_RESPONSE', 'ESCAPED_EXCEPTION'])
def test_unchanged_normal_request_requires_the_same_recorded_500_origin(prior_origin):
    rows, review = make_rows()
    if prior_origin:
        rows[0]['observations'][0]['statusOrigin'] = prior_origin
    edits = recorded_call_edits(OLD, NEW, rows, review, ENV)

    def escaped(current, _):
        current['observations'][0]['statusOrigin'] = 'ESCAPED_EXCEPTION'

    result = project(rows, edits, escaped)['previousRepairResult']
    if prior_origin == 'ESCAPED_EXCEPTION':
        case, = result['measuredCallEdits'][0]['unchangedChecks'][0]['unchangedCases']
        assert case['httpStatus'] == 500 and case['statusOrigin'] == prior_origin
    else:
        check, = result['checks']
        assert check['status'] == 'CHANGED' and 'measuredCallEdits' not in result
        item, = [i for i in check['items'] if i['itemId'] == 'http_status']
        assert item['transition'] == 'OBSERVATION_CHANGED'
        assert item['before']['statusOrigin'] == (prior_origin or 'UNKNOWN')
        assert item['after']['statusOrigin'] == 'ESCAPED_EXCEPTION'
