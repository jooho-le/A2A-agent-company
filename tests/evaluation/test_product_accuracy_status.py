"""Accuracy preserves requirement verdicts while bounding mixed failure/unknown results."""
import copy

import pytest

from evaluation.aggregation import CheckPlan, CheckResult, Status
from evaluation.independent import build_evaluation_report
from evaluation.scoring import product_accuracy
from tests.evaluation.test_independent import inputs, uid


def report_with(statuses, *, plan=None):
    args = inputs()
    args['results'] = [CheckResult(uid(n), statuses.get(n, Status.PASS), (uid(n + 100),))
                       for n in (20, 21, 22)]
    if plan is not None:
        args['plan'] = plan
    return build_evaluation_report(**args)


@pytest.mark.parametrize('statuses,counts,bounds,mixed', [
    ({}, (2, 0, 0), (1.0, 1.0), []),
    ({21: Status.FAIL, 22: Status.PASS}, (1, 1, 0), (0.5, 0.5), []),
    ({21: Status.FAIL, 22: Status.ERROR}, (1, 0, 1), (0.5, 0.5), [uid(11)]),
    ({21: Status.FAIL, 22: Status.NOT_RUN}, (1, 0, 1), (0.5, 0.5), [uid(11)]),
    ({22: Status.ERROR}, (1, 0, 1), (0.5, 1.0), []),
    ({20: Status.FAIL, 21: Status.FAIL, 22: Status.ERROR}, (0, 1, 1), (0.0, 0.0), [uid(11)]),
    ({20: Status.ERROR, 21: Status.FAIL, 22: Status.NOT_RUN}, (0, 0, 2), (0.0, 0.5), [uid(11)]),
    ({20: Status.NOT_RUN, 21: Status.NOT_RUN, 22: Status.NOT_RUN}, (0, 0, 2), (0.0, 1.0), []),
])
def test_requirement_status_counts_match_report_without_losing_known_failures(statuses, counts, bounds, mixed):
    report = report_with(statuses)
    original = copy.deepcopy(report)
    result = product_accuracy(report)

    assert report == original
    observed = tuple(result[key] for key in ('passedRequirements', 'failedRequirements', 'unverifiedRequirements'))
    assert observed == counts
    assert sum(observed) == result['requirementCount'] == 2
    assert observed == tuple(sum(row['validationVerdict'] == verdict for row in report['requirements'])
                             for verdict in ('PASS', 'FAIL', 'UNVERIFIED'))
    assert (result['fullSatisfactionLower'], result['fullSatisfactionUpper']) == bounds
    assert result['unverifiedWithConfirmedFailures'] == len(mixed)
    assert result['unverifiedWithConfirmedFailureRequirementIds'] == mixed


def test_excluded_or_optional_checks_do_not_create_mixed_failure_requirements():
    plan = (CheckPlan(uid(20)), CheckPlan(uid(21), required=False),
            CheckPlan(uid(22), exclusion_approval_ref='approval://excluded', exclusion_reason='not applicable'))
    result = product_accuracy(report_with({21: Status.FAIL, 22: Status.NOT_APPLICABLE}, plan=plan))
    assert result['requirementCount'] == result['passedRequirements'] == 1
    assert result['failedRequirements'] == result['unverifiedRequirements'] == 0
    assert result['unverifiedWithConfirmedFailures'] == 0
    assert result['unverifiedWithConfirmedFailureRequirementIds'] == []
    assert result['fullSatisfactionLower'] == result['fullSatisfactionUpper'] == 1.0


def test_unconfirmed_manifest_does_not_present_mixed_failures_as_measured_counts():
    report = report_with({21: Status.FAIL, 22: Status.ERROR})
    report['manifestCheck'] = {'metadataMatches': False, 'errorCode': 'MANIFEST_MISSING'}
    result = product_accuracy(report)
    assert result['reason'] == 'MANIFEST_NOT_CONFIRMED'
    assert result['unverifiedWithConfirmedFailures'] is None
    assert result['unverifiedWithConfirmedFailureRequirementIds'] == []
    assert result['fullSatisfactionLower'] is result['fullSatisfactionUpper'] is None
