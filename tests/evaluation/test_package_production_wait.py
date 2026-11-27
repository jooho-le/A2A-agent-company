"""Export waits for the producer's commit, never creates completion evidence."""
from uuid import uuid4

import pytest

from agents.platform import package_records
from evaluation import experiment_run as er


def test_pending_package_waits_then_returns_producer_result(monkeypatch):
    run_id = str(uuid4())
    rows = iter({'runId': run_id, 'status': status} for status in ('PENDING', 'PENDING', 'COMPLETED'))
    monkeypatch.setattr(package_records, 'package_production_status', lambda *_: next(rows))
    sleeps = []
    result = er.wait_package_production(object(), run_id, clock=lambda: 0, sleep=sleeps.append)
    assert result['status'] == 'COMPLETED' and len(sleeps) == 2


@pytest.mark.parametrize('status', ['NOT_CONFIGURED', 'NOT_COMPLETED', 'ERROR'])
def test_unavailable_package_is_preserved_without_inventing_completion(monkeypatch, status):
    run_id = str(uuid4())
    monkeypatch.setattr(package_records, 'package_production_status',
                        lambda *_: {'runId': run_id, 'status': status})
    assert er.wait_package_production(object(), run_id)['status'] == status


def test_pending_package_timeout_can_be_retried_for_same_run(monkeypatch):
    run_id = str(uuid4())
    monkeypatch.setattr(package_records, 'package_production_status',
                        lambda *_: {'runId': run_id, 'status': 'PENDING'})
    times = iter([0, 0, 31])
    with pytest.raises(er.ExperimentRunError, match='PACKAGE_PRODUCTION_TIMEOUT'):
        er.wait_package_production(object(), run_id, clock=lambda: next(times), sleep=lambda _: None)


def test_foreign_package_status_is_rejected(monkeypatch):
    monkeypatch.setattr(package_records, 'package_production_status',
                        lambda *_: {'runId': str(uuid4()), 'status': 'COMPLETED'})
    with pytest.raises(er.ExperimentRunError, match='PACKAGE_STATUS_UNAVAILABLE'):
        er.wait_package_production(object(), str(uuid4()))
