"""Recorded execution order must match new submissions; recovery never submits twice."""
import pytest

from evaluation import experiment_run as er
from tests.evaluation.test_experiment_run import _approved, _Host, _matching_plan, _runtime_files, _exporter


@pytest.fixture
def setup(monkeypatch, tmp_path):
    _runtime_files(tmp_path)
    monkeypatch.setattr(er, 'approved_configuration',
                        lambda plan, architecture, base: _approved(architecture=architecture))
    return tmp_path, tmp_path / 'execution'


@pytest.mark.parametrize('order_mode', ['ALTERNATING', 'RANDOMIZED_RECORDED'])
@pytest.mark.parametrize('index', [1, 3])
def test_new_submission_cannot_skip_unrecorded_predecessors(setup, order_mode, index):
    base, out = setup
    plan = _matching_plan(executionOrder=order_mode)
    row = er.schedule(plan)['runs'][index]
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='PREVIOUS_ORDER_NOT_COMPLETED'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([]))
    assert host.requests == []
    assert not (out / er.LEDGER).exists()


def test_registered_ceiling_is_required_before_contacting_host(setup):
    base, out = setup
    plan = _matching_plan(requireTotalTokenLimit=True)
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='RUN_TOTAL_TOKEN_LIMIT_NOT_SET'):
        er.run_one(plan, er.schedule(plan)['runs'][0], out, base,
                   client_factory=host.client, exporter=_exporter([]))
    assert host.requests == []
    assert not (out / er.LEDGER).exists()


def test_closed_first_slot_does_not_authorize_skipping_the_second(setup):
    base, out = setup
    plan = _matching_plan()
    rows = er.schedule(plan)['runs']
    first, later = _Host(['FINISHED']), _Host(['FINISHED'])
    er.run_one(plan, rows[0], out, base, client_factory=first.client, exporter=_exporter([]))
    with pytest.raises(er.ExperimentRunError, match='PREVIOUS_ORDER_NOT_COMPLETED'):
        er.run_one(plan, rows[2], out, base, client_factory=later.client, exporter=_exporter([]))
    assert later.requests == []


@pytest.mark.parametrize('status', ['FINISHED', 'ABORTED', 'WAITING_INPUT', 'HUMAN_REVIEW'])
def test_closed_predecessor_with_failed_export_allows_next_submission_and_old_recovery(setup, status):
    base, out = setup
    plan = _matching_plan()
    rows = er.schedule(plan)['runs']
    first, second = _Host([status]), _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, rows[0], out, base, client_factory=first.client, exporter=_exporter([], fail=1))
    er.run_one(plan, rows[1], out, base, client_factory=second.client, exporter=_exporter([]))
    recovered = er.run_one(plan, rows[0], out, base, client_factory=first.client,
                           exporter=_exporter([], status=status))
    assert recovered['reusedSubmission'] is True
    assert sum(method == 'POST' for method, _ in first.requests) == 1
    assert sum(method == 'POST' for method, _ in second.requests) == 1
