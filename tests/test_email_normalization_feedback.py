"""Bind canonical creation failures to the active Source, with no final-test leakage."""
from copy import deepcopy
from hashlib import sha256
import json

import pytest

from agents.runtime.developer_input import compact_fix_input
from test_email_normalization_repair import CONTRACT, CALLER, SAFE_HANDLER, BAD
from test_invalid_input_repair_feedback import request as input_request, PROTECTED_SUITE, FINGERPRINT


SOURCE = (CALLER + SAFE_HANDLER + BAD).encode()


def task(generated=False):
    value = input_request(generated=generated)
    fix = value['fixRequest']
    row = deepcopy(fix['validationFeedback'][2])
    requirement = next(r['requirementId'] for r in CONTRACT['requirements'] if r['key'] == 'REQ-003')
    row.update(testId='canonical', requirementId=requirement, observations=[
        {'caseId': 'canonical_example', 'itemId': 'http_status', 'kind': 'assertion',
         'status': 'FAIL', 'actual': 422, 'reason': 'EXPECTATION_MISMATCH'},
        {'caseId': 'canonical_example', 'itemId': 'canonical', 'kind': 'assertion',
         'status': 'UNVERIFIED', 'actual': None, 'reason': 'OBSERVATION_UNAVAILABLE'}])
    issue = {k: row[k] for k in ('reportArtifactId', 'sourceArtifactId', 'codeVersion')}
    issue.update(issueId='canonical', category='SECURITY_REQUIREMENT_FAILURE', requirementIds=[requirement],
        description=json.dumps({'code': 'MEASURED_SECURITY_TESTS', 'tests': [{'testId': 'canonical', 'outcome': 'FAIL'}]}))
    fix.update(issues=[issue], validationFeedback=[row])
    value['runConfiguration']['scenarioContract'] = deepcopy(CONTRACT)
    return value


def brief(value, source=SOURCE):
    return compact_fix_input(value, source_files=lambda: {'main.py': source})['activeRepairFocus']['brief']


@pytest.mark.parametrize('generated', [False, True], ids=['ver1-protected', 'ver2-generated'])
def test_selected_current_canonical_failure_gets_parser_function_and_first_read(generated):
    value = task(generated)
    before = deepcopy(value)
    data = brief(value)
    group = data['emailNormalizationRepair']
    assert (group['sourceArtifactId'], group['codeVersion']) == ('source', 2)
    assert group['failedCases'][0]['httpStatus'] == 422
    site, = group['sourceSites']
    assert site['sourceRef'] == 'source/main.py::normalize_email'
    assert site['sourceSha256'] == sha256(SOURCE).hexdigest()
    assert site['evidenceKind'] == 'STATIC_CANDIDATE_ON_SELECTED_SNAPSHOT'
    assert site['trimBeforeValidationObserved'] is False
    assert data['sourceInspection']['firstReadBatch'] == ['source/main.py']
    assert value == before


@pytest.mark.parametrize('mutation', ['not-selected','source','manifest','version','final-test','other-suite',
    'ver2-protected','unmeasured','other-requirement','canonical-passed','invalid-only'])
def test_stale_excluded_or_unrelated_observation_is_never_a_new_target(mutation):
    value = task()
    row = value['fixRequest']['validationFeedback'][0]
    if mutation=='not-selected': value['fixRequest']['issues']=[]
    elif mutation=='source': row['sourceArtifactId']='old'
    elif mutation=='manifest': row['executionManifest']['snapshotSha256']='0'*64
    elif mutation=='version': row['codeVersion']=1
    elif mutation=='final-test': row['testVersion']='final:'+FINGERPRINT
    elif mutation=='other-suite': value['runConfiguration']['configuration']['protectedTestSuiteRef']='other'
    elif mutation=='ver2-protected': value['runConfiguration']['configuration']['protectedTestSuiteRef']=None
    elif mutation=='unmeasured': row['observationStatus']='UNVERIFIED'
    elif mutation=='other-requirement': row['requirementId']='another'
    elif mutation=='canonical-passed': row['observations'][1].update(status='PASS',actual=True)
    elif mutation=='invalid-only': row['observations'].pop()
    assert 'emailNormalizationRepair' not in brief(value)


def test_missing_source_files_keeps_measured_failure_without_inventing_coordinates():
    value = compact_fix_input(task())['activeRepairFocus']['brief']
    assert value['emailNormalizationRepair']['failedCases']
    assert value['emailNormalizationRepair']['sourceSites'] == []


def test_model_and_case_literals_are_not_promoted_into_the_new_brief():
    value=task()
    value['fixRequest']['validationFeedback'][0]['observations'][0]['rawEmail']='PRIVATE_REQUEST_LITERAL'
    data=brief(value)
    assert 'PRIVATE_REQUEST_LITERAL' not in str(data['emailNormalizationRepair'])
    assert 'PRIVATE_LITERAL' not in str(data['emailNormalizationRepair'])
