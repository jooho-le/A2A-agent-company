"""Malformed runner outcomes must remain unverified instead of aborting scoring."""
from uuid import uuid4

import pytest

from evaluation.aggregation import Status
from evaluation.check_adapters import check_results_from_unit_report


@pytest.mark.parametrize('outcome', [[], {}, None, True, 1])
def test_non_string_runner_outcome_invalidates_entire_report(outcome):
    cases = {'good': str(uuid4()), 'bad': str(uuid4())}
    report = {
        'format': 'unittest-v1', 'total': 2, 'passed': 2, 'failed': 0, 'skipped': 0,
        'tests': [{'testId': 'good', 'outcome': 'PASS'},
                  {'testId': 'bad', 'outcome': outcome}],
    }

    checks = check_results_from_unit_report(report, cases, evidence_id=str(uuid4()))

    assert len(checks) == 2
    assert all(check.status == Status.ERROR for check in checks)
    assert all(check.error_code == 'UNIT_REPORT_INVALID' for check in checks)
    assert all(not check.evidence_ids for check in checks)
