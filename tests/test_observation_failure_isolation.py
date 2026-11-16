"""관찰 기록(평가 증거)이 실패해도 제품 Run은 바뀌지 않고, 평가는 그 구간을 미검증으로 본다(점검 2026-11-15).

실제 owned dispatch·SQLite·내보내기와 시험용 공급자·FakeDocker만 쓴다. Docker·유료 모델·생성 코드 실행은 없다.
"""
import asyncio
from contextlib import contextmanager
import logging
import sqlite3
import unittest
from unittest import mock
from uuid import UUID, uuid4

import pytest

import test_owned_agent_pipeline as pipeline
from agents.platform.evaluation_export import write_evaluation_export
from agents.platform.telemetry import BoundRuntimeTelemetry
from evaluation.evidence_metrics import process_metrics
from evaluation.host_process import derive_process_evidence
from evaluation.trace_checks import check_trace_exposure
from evaluation.workflow import load_handoff
from orchestrator import observability as obs
from orchestrator.core.logging import SecretRedactionFilter
from orchestrator.domain import WorkflowStatus
from tests.evaluation.test_trace_checks import CRITERIA


class _Repository:
    """In-memory transaction owner; fail_after counts successful transactions before failures."""

    def __init__(self, fail_after=None):
        self.connection = sqlite3.connect(':memory:')
        self.fail_after, self.calls = fail_after, 0

    @contextmanager
    def _transaction(self):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise sqlite3.OperationalError('database is locked')
        with self.connection:
            yield self.connection


def _scopes(repository, run_id):
    return obs.collect_observations(repository.connection, run_id)


def test_unavailable_store_runs_the_product_path_unobserved():
    repository, run_id = _Repository(fail_after=0), str(uuid4())
    with obs.observe_run(repository, run_id):
        assert obs.output_observation_active() is False
        result = 'product-result'
    assert result == 'product-result'


def test_failed_log_receipt_never_raises_into_the_caller_and_closes_the_scope_incomplete():
    repository, run_id = _Repository(), str(uuid4())
    logger = logging.getLogger('observation-failure-fixture')
    handler = logging.Handler()
    handler.emit = lambda record: None
    handler.addFilter(SecretRedactionFilter())
    logger.addHandler(handler)
    try:
        with mock.patch.object(obs.ObservationStore, 'observe', side_effect=sqlite3.OperationalError('locked')):
            with obs.observe_run(repository, run_id):
                logger.warning('product message during a locked receipt store')  # must not raise
                print('stdout still observed')
    finally:
        logger.removeHandler(handler)
    observation = _scopes(repository, run_id)
    assert sorted(row['state'] for row in observation['scopes']) == ['INCOMPLETE', 'STARTED']
    assert observation['scopesComplete'] is False and obs.scopes_complete(observation['scopes']) is False
    # Stream receipts are still written; only the missing log receipt makes the scope incomplete.
    assert {row['kind'] for row in observation['records']} == {'HOST_STDOUT', 'HOST_STDERR'}


def test_scope_finalization_failure_preserves_the_product_outcome():
    repository, run_id = _Repository(fail_after=1), str(uuid4())
    with obs.observe_run(repository, run_id):
        print('product output')
    observation = _scopes(repository, run_id)
    assert [row['state'] for row in observation['scopes']] == ['STARTED']
    assert observation['scopesComplete'] is False
    with pytest.raises(ValueError, match='product failure'):
        with obs.observe_run(_Repository(fail_after=1), str(uuid4())):
            raise ValueError('product failure')


def test_model_input_and_output_receipt_failures_do_not_stop_the_model_call():
    class Store:
        def append_model_input(self, *args):
            raise RuntimeError('telemetry store unavailable')
    fake = mock.Mock(spec=['store', '_event', 'model', 'repository', 'binding'])
    fake.store, fake.model = Store(), None
    fake._event.return_value = object()
    fake.repository, fake.binding = _Repository(fail_after=0), {'runId': str(uuid4())}
    assert BoundRuntimeTelemetry.model_input(fake, {'sequence': 1}) is None
    assert BoundRuntimeTelemetry.model_output(fake, {'sequence': 1, 'outputText': '{}', 'toolCalls': []}) is None


class ReceiptFailureRunTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = pipeline.OwnedAgentPipelineTests()
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()

    def prepare(self, run_id):
        obs.ObservationStore(self.base.repository).pin_policy(run_id)
        self.base.prepare_workspace(run_id)

    async def run_once(self):
        async with self.base.running_platform(prepare=self.prepare) as (client, _):
            response = await asyncio.wait_for(client.post('/api/v1/runs', json=self.base.submission()), 30)
            self.assertEqual(response.status_code, 201)
            run_id = UUID(response.json()['run']['runId'])
        folder = self.base.directory / f'export-{run_id}'
        write_evaluation_export(self.base.repository, run_id, folder,
            stores={role.value: setting.database_path for role, setting in self.base.settings.items()})
        return run_id, load_handoff(folder)[0]

    async def test_every_receipt_failing_keeps_the_run_outcome_and_reports_unverified_coverage(self):
        with mock.patch.object(obs, 'append_output', side_effect=sqlite3.OperationalError('database is locked')):
            run_id, export = await self.run_once()
        run = self.base.repository.get_run(run_id)
        # Same product outcome as the receipt-producing fixture run: all Agent tasks complete and
        # the real verdict gate pauses the Run for missing Security proof. Nothing failed because
        # of evaluation evidence.
        self.assertEqual(run.status, WorkflowStatus.HUMAN_REVIEW)
        kinds = [event['eventType'] for event in export['bundle']['events']]
        self.assertNotIn('RUN_ABORTED', kinds)
        self.assertEqual(sum(kind == 'VALIDATION_FINISHED' for kind in kinds), 2)
        self.assertGreater(export['bundle']['usage']['total'], 0)
        observation = export['trace']['observations']
        self.assertEqual(observation['records'], [])
        self.assertFalse(observation['scopesComplete'])
        for role in export['a2a']['roles'].values():
            self.assertTrue(role['tasks'])
            self.assertEqual(role['outputObservations']['records'], [])
        result = check_trace_exposure({**export['trace'], 'a2aMessages': export['a2a']}, CRITERIA)
        self.assertEqual(result.status, 'UNVERIFIED')
        incomplete = result.coverage['incompleteDocuments']
        self.assertIn('outputScopes', incomplete)
        self.assertIn('modelOutputObservations', incomplete)
        self.assertTrue(any(name.startswith('a2aOutputObservations:') for name in incomplete))
        process = process_metrics(str(run_id), derive_process_evidence(export))
        self.assertIsNone(process['retries']['channels']['LLM'])
