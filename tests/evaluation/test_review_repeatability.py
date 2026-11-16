import copy
import json
from uuid import uuid4

import pytest

from evaluation.agent_eval import agent_tables
from evaluation.planner_review import assess_review, review_template
from evaluation.repeatability import compare_files, compare_reports, main
from evaluation.aggregation import Status
from tests.evaluation.test_agent_eval import run
from tests.evaluation.test_outcomes import report_with as make_report


def report_with(*args, **kwargs):
    report = copy.deepcopy(make_report(*args, **kwargs))
    report['evaluationId'] = str(uuid4())
    return report


def review_for(export):
    review = review_template(export)
    review.update(reviewer='independent-reviewer', reviewedAt='2026-11-13T15:00:00+09:00')
    for item in review['criteria'].values():
        item.update(status='PASS', evidence='계획의 해당 작업과 공개 요구사항을 대조한 근거')
    return review


def test_review_is_bound_to_plan_and_displayed_in_agent_table():
    export = run()
    review = review_for(export)
    tables = agent_tables(export, planner_review=review)
    row = next(row for row in tables['accuracy'] if row['item'].startswith('계획 내용 타당성'))
    assert row['status'] == 'PASS' and row['detail'] == review
    assert next(row for row in agent_tables(export)['accuracy'] if row['item'].startswith('계획 내용 타당성'))['status'] == 'UNVERIFIED'


@pytest.mark.parametrize('damage', ['run', 'hash', 'plan', 'reviewer', 'time', 'criteria', 'evidence', 'ambiguous'])
def test_review_cannot_be_reused_without_matching_target_and_evidence(damage):
    export = run()
    review = review_for(export)
    if damage == 'run': review['runId'] = 'other'
    if damage == 'hash': review['planSha256'] = 'f' * 64
    if damage == 'plan':
        next(a for a in export['trace']['artifacts'] if a['artifactType'] == 'REQUIREMENT')['payload']['changed'] = True
    if damage == 'reviewer': review['reviewer'] = ''
    if damage == 'time': review['reviewedAt'] = '2026-11-13T00:00:00'
    if damage == 'criteria': review['criteria'].pop('taskSpecificity')
    if damage == 'evidence': review['criteria']['taskSpecificity']['evidence'] = ''
    if damage == 'ambiguous':
        export['trace']['artifacts'].append(copy.deepcopy(next(a for a in export['trace']['artifacts'] if a['artifactType'] == 'REQUIREMENT')))
    with pytest.raises(ValueError): assess_review(export, review)


def test_partial_review_is_unknown_and_negative_review_is_preserved():
    export = run()
    review = review_for(export)
    review['criteria']['taskSpecificity']['status'] = 'UNVERIFIED'
    assert assess_review(export, review)['status'] == 'UNVERIFIED'
    review['criteria']['requirementPreservation']['status'] = 'FAIL'
    assert assess_review(export, review)['status'] == 'FAIL'


def test_matching_grades_ignore_evaluation_identity_and_creation_time():
    first, second = report_with({}), report_with({})
    second['evaluationId'] = '00000000-0000-4000-8000-000000008888'
    second['createdAt'] = '2026-11-14T00:00:00Z'
    assert compare_reports(first, second)['status'] == 'PASS'


@pytest.mark.parametrize('statuses,status', [({20: Status.FAIL}, 'FAIL'), ({20: Status.ERROR}, 'UNVERIFIED')])
def test_repeat_changes_are_reported_without_conflating_error_and_product_failure(statuses, status):
    result = compare_reports(report_with({}), report_with(statuses))
    assert result['status'] == status and result['differences']


def test_two_unverified_results_are_not_reproducibility_pass():
    result = compare_reports(report_with({20: Status.ERROR}), report_with({20: Status.ERROR}))
    assert result['status'] == 'UNVERIFIED' and result['reason'] == 'REPEAT_CHECKS_UNVERIFIED'


def test_copy_of_same_evaluation_is_not_a_second_execution():
    first = report_with({})
    assert compare_reports(first, copy.deepcopy(first))['reason'] == 'TWO_SEPARATE_EVALUATIONS_REQUIRED'


def test_unverified_causes_are_preserved_even_when_statuses_match():
    first, second = report_with({20: Status.ERROR}), report_with({20: Status.ERROR})
    second['checks'][0]['reason'] = '연결 실패'
    result = compare_reports(first, second)
    assert result['status'] == 'UNVERIFIED' and result['differences'][0]['field'] == 'reason'
    assert result['unverifiedChecks']['second'][0]['reason'] == '연결 실패'


@pytest.mark.parametrize('field', ['sourceRunId', 'suite', 'expectedManifest', 'observedManifest'])
def test_repeat_requires_same_target_suite_and_environment(field):
    first, second = report_with({}), report_with({})
    if field == 'sourceRunId': second[field] = '00000000-0000-4000-8000-000000008888'
    elif field == 'suite': second[field]['sha256'] = '1' * 64
    else: second[field]['snapshotSha256'] = 'f' * 64
    result = compare_reports(first, second)
    assert result['status'] == 'UNVERIFIED' and result['reason'] == 'REPEAT_CONDITIONS_DIFFER'


def test_repeat_cli_preserves_inputs_and_refuses_overwrite(tmp_path):
    a, b, out = [tmp_path / n for n in ('a.json', 'b.json', 'repeatability.json')]
    a.write_text(json.dumps({'report': report_with({})}))
    b.write_text(json.dumps({'report': report_with({})}))
    original = a.read_bytes()
    assert main(['--first', str(a), '--second', str(b), '--out', str(out)]) == 0
    assert a.read_bytes() == original and len(json.loads(out.read_text())['inputSha256']) == 2
    with pytest.raises(FileExistsError): compare_files(a, b, out)
    with pytest.raises(ValueError, match='SEPARATE'): compare_files(a, a, tmp_path / 'other.json')
