"""Actual Host package producer with SQLite/Git and synthetic model/tool transports.

No Docker binary, daemon, image, generated product code, or paid API is used.
"""
import asyncio
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace
import unittest
from uuid import UUID
from unittest.mock import Mock, patch

import pytest

from agents.platform.evaluation_export import write_evaluation_export
from agents.platform.package_service import HostPackageService, PackageProductionError, install_package_service
from agents.platform.package_records import package_production_status
from evaluation.package_evidence import package_assessment
from evaluation.workflow import load_handoff
from orchestrator.domain import TraceEvent, WorkflowStatus, FinalVerdict
from orchestrator.infrastructure import SQLiteWorkflowRepository
from orchestrator.infrastructure.sqlite_workflows import _insert_event
import test_owned_agent_fix_pipeline as fixtures


@pytest.fixture(scope='module')
def completed_run():
    owner = unittest.TestCase()
    harness = fixtures._FixHarness(owner, qa_mode='pass')
    participating = HostPackageService(harness.base.repository)
    prepare = harness.base.prepare_workspace
    def enrolled_prepare(run_id):
        participating.enroll(run_id)
        prepare(run_id)
    harness.base.prepare_workspace = enrolled_prepare

    async def run():
        async with harness.running() as (client, _):
            response = await client.post('/api/v1/runs', json=harness.base.submission())
            assert response.status_code == 201, response.text
            return UUID(response.json()['run']['runId'])

    try:
        run_id = asyncio.run(run())
        assert harness.base.repository.get_run(run_id).status is WorkflowStatus.FINISHED
        yield harness, run_id
    finally:
        owner.doCleanups()


@pytest.fixture
def case(completed_run, tmp_path):
    harness, run_id = completed_run
    path = tmp_path / 'host.sqlite3'
    with sqlite3.connect(harness.base.repository.database_path) as source, sqlite3.connect(path) as target:
        source.backup(target)
    repository = SQLiteWorkflowRepository(path)
    service = HostPackageService(repository)
    stores = {role.value: harness.base.directory / (role.value.lower() + '-tasks.sqlite3')
              for role in fixtures.AgentRole}
    return service, repository, run_id, stores


def export_case(case, path):
    _service, repository, run_id, stores = case
    write_evaluation_export(repository, run_id, path, stores=stores)
    return load_handoff(path)[0]


def completions(repository, run_id):
    events, _ = repository.list_events(run_id, limit=500, offset=0)
    return [event for event in events if event.event_type == 'RESULT_PACKAGE_COMPLETED']


def test_native_package_is_exported_without_operator_files_and_survives_restart(case, tmp_path):
    service, repository, run_id, _ = case
    assert service.complete(run_id) == {'status': 'COMPLETED', 'reason': None}
    first = export_case(case, tmp_path / 'first')
    proof = package_assessment(first)
    assert proof['status'] == 'PASS', proof
    assert set(first['packageFinalReport']['artifacts']) == {
        'REQUIREMENT', 'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'}
    original = first['packageCompletion']
    restored = HostPackageService(repository)
    assert restored.recover() == []
    assert restored.complete(run_id)['status'] == 'COMPLETED'
    again = export_case(case, tmp_path / 'again')
    assert again['packageCompletion'] == original
    assert len(completions(repository, run_id)) == 1


@pytest.mark.parametrize('kind', ['REQUIREMENT', 'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'])
def test_missing_required_artifact_cannot_publish_completion(case, kind):
    service, repository, run_id, _ = case
    from agents.platform.export_readonly import ReadOnlyWorkflowRepository
    original = ReadOnlyWorkflowRepository.list_project_artifacts
    with patch.object(ReadOnlyWorkflowRepository, 'list_project_artifacts',
                      lambda self, selected: [a for a in original(self, selected) if a.artifact_type != kind]):
        assert service.complete(run_id) == {'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_MISSING_' + kind}
    assert not completions(repository, run_id)


@pytest.mark.parametrize('event_type,state', [('WORKFLOW_STATE_CHANGED', WorkflowStatus.HUMAN_REVIEW),
                                           ('WORKFLOW_STATE_CHANGED', WorkflowStatus.WAITING_INPUT),
                                           ('WORKFLOW_RESUMED', WorkflowStatus.PLANNING)])
def test_paused_or_assisted_history_cannot_produce_automatic_package(case, event_type, state):
    service, repository, run_id, _ = case
    with repository._transaction() as connection:
        _insert_event(connection, TraceEvent(run_id=run_id, event_type=event_type, actor='Orchestrator',
                                             workflow_state=state, attempt=0))
    assert service.complete(run_id) == {'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_OUTSIDE_AUTOMATIC_SEGMENT'}
    assert not completions(repository, run_id)


def test_report_write_failure_rolls_back_event_and_recovery_uses_real_retry_time(case, tmp_path):
    service, repository, run_id, _ = case
    with repository._transaction() as connection:
        connection.execute('''CREATE TRIGGER fail_package BEFORE INSERT ON host_result_packages
            BEGIN SELECT RAISE(ABORT, 'synthetic disk failure'); END''')
    result = asyncio.run(service.after_dispatch(run_id))
    assert result == {'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_WRITE_FAILED'}
    assert package_production_status(repository, run_id)['status'] == 'ERROR'
    assert not completions(repository, run_id)
    with repository._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM host_final_reports').fetchone()[0] == 0
    before = export_case(case, tmp_path / 'incomplete')
    assert package_assessment(before)['status'] == 'UNVERIFIED'
    with repository._transaction() as connection:
        connection.execute('DROP TRIGGER fail_package')
    assert service.recover() == [{'status': 'COMPLETED', 'reason': None}]
    after = export_case(case, tmp_path / 'recovered')
    assert package_assessment(after)['status'] == 'PASS'


def test_clock_before_run_end_never_creates_negative_package_time(case):
    service, repository, run_id, _ = case
    run = repository.get_run(run_id)
    service.clock = lambda: run.created_at - timedelta(seconds=1)
    with pytest.raises(PackageProductionError, match='PACKAGE_CLOCK_INVALID'):
        service.complete(run_id)
    assert not completions(repository, run_id)


def test_completed_bytes_cannot_be_overwritten_or_replaced(case):
    service, repository, run_id, _ = case
    service.complete(run_id)
    with pytest.raises(sqlite3.IntegrityError), repository._transaction() as connection:
        connection.execute("UPDATE host_final_reports SET report=x'00' WHERE run_id=?", (str(run_id),))
    with pytest.raises(sqlite3.IntegrityError), repository._transaction() as connection:
        connection.execute('INSERT OR REPLACE INTO host_result_packages SELECT * FROM host_result_packages WHERE run_id=?',
                           (str(run_id),))


@pytest.mark.parametrize('status,verdict', [('FINISHED', 'FAIL'), ('FINISHED', 'UNVERIFIED'), ('ABORTED', None)])
def test_available_failed_or_aborted_results_are_packaged_without_claiming_success(case, tmp_path, status, verdict):
    service, repository, run_id, _ = case
    # Clone the complete artifact fixture with a different recorded terminal
    # outcome. It proves packaging is independent of the product verdict.
    run = repository.get_run(run_id).model_copy(update={
        'status': WorkflowStatus(status), 'verdict': None if verdict is None else FinalVerdict(verdict),
        'termination_reason': 'Synthetic interrupted workflow' if status == 'ABORTED' else None})
    with repository._transaction() as connection:
        connection.execute('UPDATE workflow_runs SET status=?,payload_json=? WHERE run_id=?',
                           (status, run.model_dump_json(), str(run_id)))
        if status == 'ABORTED':
            connection.execute('DROP TRIGGER trace_events_no_update')
            row = connection.execute("SELECT event_id,payload_json FROM trace_events WHERE run_id=? AND event_type='RUN_FINISHED'",
                                     (str(run_id),)).fetchone()
            event = TraceEvent.model_validate_json(row[1]).model_copy(update={
                'event_type': 'RUN_ABORTED', 'workflow_state': WorkflowStatus.ABORTED})
            connection.execute('UPDATE trace_events SET event_type=?,payload_json=? WHERE event_id=?',
                               ('RUN_ABORTED', json.dumps(event.to_trace_json()), row[0]))
    assert service.complete(run_id)['status'] == 'COMPLETED'
    export = export_case(case, tmp_path / 'failed')
    assert package_assessment(export)['status'] == 'PASS'
    assert export['packageFinalReport']['verdict'] == verdict
    from evaluation.outcomes import auto_completion
    from evaluation.run_metrics import run_metrics
    assert auto_completion(export, {'finalCodeVersion': run.code_version,
        'exportedCodeVersions': [run.code_version]}, metrics=run_metrics(export['bundle']))['status'] == 'FAIL'


def test_status_reading_does_not_produce_evidence_and_pending_is_distinct(case):
    service, repository, run_id, _ = case
    assert package_production_status(repository, run_id)['status'] == 'PENDING'
    assert not completions(repository, run_id)
    service.complete(run_id)
    status = package_production_status(repository, run_id)
    assert status['status'] == 'COMPLETED'
    assert status['eventId'] == str(completions(repository, run_id)[0].event_id)


def test_installing_producer_cannot_enroll_or_complete_a_historical_run(case):
    _service, repository, run_id, _ = case
    # This copied database represents a Run completed before this producer was
    # installed. Schema installation alone must not imply prior participation.
    with repository._transaction() as connection:
        connection.execute('DROP TABLE host_package_enrollments')
    service = HostPackageService(repository)
    assert service.recover() == []
    assert service.complete(run_id) == {
        'status': 'NOT_COMPLETED', 'reason': 'PACKAGE_PRODUCER_NOT_ENROLLED'}
    with pytest.raises(PackageProductionError, match='PACKAGE_ENROLLMENT_TOO_LATE'):
        service.enroll(run_id)
    assert package_production_status(repository, run_id)['status'] == 'NOT_CONFIGURED'
    assert not completions(repository, run_id)
    with repository._connection() as connection:
        assert connection.execute('SELECT COUNT(*) FROM host_final_reports').fetchone()[0] == 0
        assert connection.execute('SELECT COUNT(*) FROM host_package_attempts').fetchone()[0] == 0


def test_pre_execution_enrollment_is_immutable_and_survives_terminal_callback_crash(case):
    service, repository, run_id, _ = case
    with repository._connection() as connection:
        before = connection.execute('SELECT payload_json FROM host_package_enrollments WHERE run_id=?',
                                    (str(run_id),)).fetchone()[0]
    # The fixture enrolled before dispatch and deliberately omitted the end
    # callback. Retrying enrollment preserves the original participation time.
    assert service.enroll(run_id) == json.loads(before)
    with pytest.raises(sqlite3.IntegrityError), repository._transaction() as connection:
        connection.execute("UPDATE host_package_enrollments SET payload_json='{}' WHERE run_id=?", (str(run_id),))
    with pytest.raises(sqlite3.IntegrityError), repository._transaction() as connection:
        connection.execute('INSERT OR REPLACE INTO host_package_enrollments SELECT * FROM host_package_enrollments WHERE run_id=?',
                           (str(run_id),))
    assert HostPackageService(repository).recover() == [{'status': 'COMPLETED', 'reason': None}]
    assert len(completions(repository, run_id)) == 1


def test_recovery_does_not_take_over_unknown_live_control_lease(case):
    service, repository, run_id, _ = case
    with repository._transaction() as connection:
        connection.execute('INSERT INTO control_leases(run_id,token,owner_pid) VALUES(?,?,?)',
                           (str(run_id), 'unknown-owner', None))
    assert service.recover() == []
    assert not completions(repository, run_id)


def test_host_lifespan_recovers_an_interrupted_package_before_accepting_work(case):
    from fastapi import FastAPI
    _service, repository, run_id, _ = case
    app = FastAPI()
    platform = SimpleNamespace(dispatcher=Mock(), orchestrator_app=app)
    installed = install_package_service(platform, repository)
    platform.dispatcher.set_after_dispatch.assert_called_once_with(installed.after_dispatch)

    async def startup():
        async with app.router.lifespan_context(app):
            assert package_production_status(repository, run_id)['status'] == 'COMPLETED'
            assert len(completions(repository, run_id)) == 1

    asyncio.run(startup())


def test_installation_finishes_package_before_dispatch_returns_without_export_trigger(tmp_path):
    owner = unittest.TestCase()
    harness = fixtures._FixHarness(owner, qa_mode='pass')
    prepare = harness.base.prepare_workspace
    def enrolled_prepare(run_id):
        harness.base.platform.orchestrator_app.state.package_service.enroll(run_id)
        prepare(run_id)
    harness.base.prepare_workspace = enrolled_prepare

    async def run():
        async with harness.running() as (client, _):
            service = install_package_service(harness.base.platform, harness.base.repository)
            response = await client.post('/api/v1/runs', json=harness.base.submission())
            assert response.status_code == 201
            run_id = UUID(response.json()['run']['runId'])
            assert len(completions(harness.base.repository, run_id)) == 1
            assert service.complete(run_id)['status'] == 'COMPLETED'
            stores = {role.value: harness.base.directory / (role.value.lower() + '-tasks.sqlite3')
                      for role in fixtures.AgentRole}
            output = tmp_path / 'automatic'
            write_evaluation_export(harness.base.repository, run_id, output, stores=stores)
            assert package_assessment(load_handoff(output)[0])['status'] == 'PASS'

    try:
        asyncio.run(run())
    finally:
        owner.doCleanups()
