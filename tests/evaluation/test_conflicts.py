"""Conflict counts preserve individual tests, Snapshot boundaries and unknowns."""
from copy import deepcopy
import json

import pytest

from evaluation.conflicts import EVENT, PRODUCT_LINKED, TEST_FIXED, conflict_analysis, render


def records():
    event = dict(eventId='event-1', eventType=EVENT, runId='run', actor='Orchestrator', codeVersion=1,
                 snapshotSha256='a' * 64, requirementIds=['req'], outputArtifactIds=['qa', 'security'])
    qa = dict(artifactId='qa', artifactType='QA_REPORT', runId='run', codeVersion=1,
              executionManifest={'snapshotSha256': 'a' * 64}, tests=[
                  dict(testId='run_unit_tests:generated:one', requirementId='req', outcome='FAIL'),
                  dict(testId='run_unit_tests:generated:two', requirementId='req', outcome='UNVERIFIED'),
                  dict(testId='run_unit_tests:protected:baseline', requirementId='req', outcome='PASS')])
    security = dict(artifactId='security', artifactType='SECURITY_REPORT', runId='run', codeVersion=1,
                    executionManifest={'snapshotSha256': 'a' * 64}, requirementResults=[
                        dict(requirementId='req', outcome='FAIL', details=json.dumps({
                            'code': 'MEASURED_SECURITY_TESTS', 'tests': [
                                {'testId': 'run_unit_tests:security:one', 'outcome': 'FAIL'}]}))])
    return {'run': {'runId': 'run'}, 'events': [event], 'eventsTotal': 1, 'snapshotConsistent': True}, {
        'runId': 'run', 'artifacts': [qa, security]}


def review_for(row, result=TEST_FIXED):
    return {'conflictId': row['conflictId'], 'snapshotSha256': row['snapshotSha256'],
            'result': result, 'evidenceSha256': ['f' * 64]}


def test_each_excluded_test_is_counted_and_no_reproduction_means_undetermined():
    bundle, trace = records()
    before = deepcopy((bundle, trace))
    data = conflict_analysis(bundle, trace)
    assert (data['status'], data['excluded'], data['productDefectConfirmed'],
            data['testDefectConfirmed'], data['undetermined']) == ('MEASURED', 3, 0, 0, 3)
    assert len({row['conflictId'] for row in data['conflicts']}) == 3
    assert before == (bundle, trace)
    assert '미확정: 3' in render(data)


def test_duplicate_events_and_repeated_report_delivery_do_not_double_count():
    bundle, trace = records()
    bundle['events'] += [deepcopy(bundle['events'][0]), {**bundle['events'][0], 'eventId': 'event-2'}]
    bundle['eventsTotal'] = 3
    trace['artifacts'].append(deepcopy(trace['artifacts'][0]))
    data = conflict_analysis(bundle, trace)
    assert data['excluded'] == 3
    assert data['conflicts'][0]['eventIds'] == ['event-1', 'event-2']


def test_identical_test_ids_on_different_snapshots_are_separate_conflicts():
    bundle, trace = records()
    event = {**bundle['events'][0], 'eventId': 'event-2', 'codeVersion': 2,
             'snapshotSha256': 'b' * 64, 'outputArtifactIds': ['qa2', 'security2']}
    bundle['events'].append(event)
    bundle['eventsTotal'] = 2
    for report in deepcopy(trace['artifacts']):
        report['artifactId'] += '2'
        report['codeVersion'] = 2
        report['executionManifest']['snapshotSha256'] = 'b' * 64
        trace['artifacts'].append(report)
    data = conflict_analysis(bundle, trace)
    old = next(row for row in data['conflicts'] if row['codeVersion'] == 1)
    reviewed = conflict_analysis(bundle, trace, review={'schemaVersion': 1, 'runId': 'run',
                                                       'reviews': [review_for(old)]})
    assert (reviewed['excluded'], reviewed['testDefectConfirmed'], reviewed['undetermined']) == (6, 1, 5)
    assert all(row['classification'] == 'UNDETERMINED' for row in reviewed['conflicts'] if row['codeVersion'] == 2)


def test_distinct_reproduction_reviews_can_confirm_different_causes_in_one_requirement():
    bundle, trace = records()
    rows = conflict_analysis(bundle, trace)['conflicts']
    review = {'schemaVersion': 1, 'runId': 'run', 'reviews': [
        review_for(rows[0], PRODUCT_LINKED), review_for(rows[1], TEST_FIXED)]}
    data = conflict_analysis(bundle, trace, review=review)
    assert [data[key] for key in ('excluded', 'productDefectConfirmed', 'testDefectConfirmed', 'undetermined')] == [3, 1, 1, 1]
    assert data['conflicts'][0]['classificationBasis'] == 'SUPPLIED_REPRODUCTION_REVIEW'


@pytest.mark.parametrize('damage', ['partial_events', 'inconsistent_snapshot', 'missing_report', 'foreign_run',
                                   'wrong_version', 'wrong_snapshot', 'missing_details', 'empty_details',
                                   'unknown_outcome', 'conflicting_artifact', 'conflicting_event', 'foreign_trace'])
def test_incomplete_or_contradictory_evidence_never_becomes_zero(damage):
    bundle, trace = records()
    qa, security = trace['artifacts']
    if damage == 'partial_events': bundle['eventsTotal'] = 2
    elif damage == 'inconsistent_snapshot': bundle['snapshotConsistent'] = False
    elif damage == 'missing_report': trace['artifacts'].pop()
    elif damage == 'foreign_run': qa['runId'] = 'different'
    elif damage == 'wrong_version': qa['codeVersion'] = 2
    elif damage == 'wrong_snapshot': qa['executionManifest']['snapshotSha256'] = 'b' * 64
    elif damage == 'missing_details': security['requirementResults'][0]['details'] = '{bad'
    elif damage == 'empty_details': security['requirementResults'][0]['details'] = json.dumps({'code': 'MEASURED_SECURITY_TESTS', 'tests': []})
    elif damage == 'unknown_outcome': qa['tests'][0]['outcome'] = 'SKIP'
    elif damage == 'conflicting_artifact': trace['artifacts'].append({**qa, 'codeVersion': 2})
    elif damage == 'conflicting_event':
        bundle['events'].append({**bundle['events'][0], 'codeVersion': 2})
        bundle['eventsTotal'] = 2
    else: trace['runId'] = 'another'
    data = conflict_analysis(bundle, trace)
    assert data['status'] == 'UNVERIFIED'
    assert all(data[key] is None for key in ('excluded', 'productDefectConfirmed', 'testDefectConfirmed', 'undetermined'))
    assert '미확인' in render(data)


@pytest.mark.parametrize('damage', ['run', 'snapshot', 'id', 'duplicate', 'evidence', 'result', 'unknown_field'])
def test_review_requires_exact_identity_and_explicit_reproduction_evidence(damage):
    bundle, trace = records()
    row = conflict_analysis(bundle, trace)['conflicts'][0]
    item = review_for(row)
    review = {'schemaVersion': 1, 'runId': 'run', 'reviews': [item]}
    if damage == 'run': review['runId'] = 'another'
    elif damage == 'snapshot': item['snapshotSha256'] = 'b' * 64
    elif damage == 'id': item['conflictId'] = 'another'
    elif damage == 'duplicate': review['reviews'].append(deepcopy(item))
    elif damage == 'evidence': item['evidenceSha256'] = []
    elif damage == 'result': item['result'] = 'FINAL_EXAM_PASSED'
    else: item['freeText'] = 'untrusted text'
    with pytest.raises(ValueError, match='CONFLICT_REVIEW'):
        conflict_analysis(bundle, trace, review=review)


def test_no_recorded_events_is_zero_but_missing_trace_is_unknown():
    bundle, trace = records()
    bundle.update(events=[], eventsTotal=0)
    data = conflict_analysis(bundle, trace)
    assert data['excluded'] == 0 and data['coverage'] == 'RECORDED_POLICY_EXCLUSIONS'
    del bundle['events']
    assert conflict_analysis(bundle, trace)['excluded'] is None


def test_final_verdict_does_not_classify_conflicts_and_markdown_ids_are_escaped():
    bundle, trace = records()
    bundle['run']['verdict'] = 'FAIL'
    trace['artifacts'][0]['tests'][0]['testId'] = 'generated:<script>|case'
    data = conflict_analysis(bundle, trace)
    assert data['undetermined'] == 3
    text = render(data)
    assert '<script>' not in text and '&lt;script&gt;&#124;case' in text


def test_security_technical_not_run_rows_are_preserved():
    bundle, trace = records()
    row = trace['artifacts'][1]['requirementResults'][0]
    row.update(outcome='UNVERIFIED', details=json.dumps({'code': 'SECURITY_TESTS_NOT_RUN',
               'tests': [{'testId': 'run_unit_tests:security:one', 'outcome': 'UNVERIFIED'}]}))
    assert conflict_analysis(bundle, trace)['excluded'] == 3
