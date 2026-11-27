"""Real owned dispatch/SQLite/export, synthetic providers and inert FakeDocker.

No daemon, Docker process, generated-code execution, or paid model is used.
"""
import asyncio
import copy
import json
from pathlib import Path
import sqlite3
import unittest
from uuid import UUID, uuid4

import pytest

import test_owned_agent_pipeline as pipeline
from agents.platform.evaluation_export import write_evaluation_export
from evaluation.evidence_metrics import process_metrics
from evaluation.host_process import derive_process_evidence
from evaluation.trace_checks import check_trace_exposure
from evaluation.workflow import load_handoff
from orchestrator.observability import ObservationStore, append_output, collect_observations, observe_run
from tests.evaluation.test_trace_checks import CRITERIA, documents


class OwnedObservationPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.base = pipeline.OwnedAgentPipelineTests()
        self.base.setUp()
        for function, arguments, keywords in self.base._cleanups:
            self.addCleanup(function, *arguments, **keywords)
        self.base._cleanups.clear()

    def prepare(self, run_id):
        ObservationStore(self.base.repository).pin_policy(run_id)
        self.base.prepare_workspace(run_id)

    def export(self, run_id):
        folder = self.base.directory / 'export'
        write_evaluation_export(self.base.repository, run_id, folder,
            stores={role.value: setting.database_path for role, setting in self.base.settings.items()})
        return load_handoff(folder)[0]

    async def test_real_boundaries_produce_receipts_and_policy_proven_process_counts(self):
        callbacks = []
        async with self.base.running_platform(prepare=self.prepare) as (client, _):
            async def settled(run_id):
                with self.base.repository._connection() as connection:
                    observation = collect_observations(connection, run_id)
                self.assertTrue(observation['scopesComplete'])  # finalized before package callback
                callbacks.append(run_id)
                self.assertTrue(self.base.platform.dispatcher.is_run_active(run_id))
            self.base.platform.dispatcher.set_after_dispatch(settled)
            response = await asyncio.wait_for(client.post('/api/v1/runs', json=self.base.submission()), 30)
            self.assertEqual(response.status_code, 201)
            run_id = UUID(response.json()['run']['runId'])
            self.assertEqual(callbacks, [run_id])
        export = self.export(run_id)
        observation = export['trace']['observations']
        kinds = [row['kind'] for row in observation['records']]
        self.assertEqual(kinds.count('LLM_OUTPUT'), export['bundle']['usage']['total'])
        self.assertTrue({'HOST_STDOUT', 'HOST_STDERR', 'TOOL_STDOUT', 'TOOL_STDERR'} <= set(kinds))
        for role in export['a2a']['roles'].values():
            self.assertEqual(len(role['outputObservations']['records']), sum(task['version'] for task in role['tasks']))
        process = process_metrics(str(run_id), derive_process_evidence(export))
        self.assertEqual(process['retries']['channels'], {'TOOL': 0, 'A2A': 0, 'LLM': 0})
        self.assertEqual(process['repairs']['routes']['REPLAN'], {'attempted': 0, 'completed': 0})
        self.assertEqual(process['repairs']['routes']['TEST_CHANGE'], {'attempted': 0, 'completed': 0})
        # The fixture has no Security requirement proof, so the real verdict
        # gate pauses it even though all four Agent tasks completed.
        self.assertEqual(process['gateRejections']['count'], 1)
        self.assertEqual(process['gateRejections']['stages'], {'VALIDATION': 1})
        self.assertTrue(export['automaticOutputEvidence']['items'])

        # Even when the manifest count is adjusted, completed physical Tool
        # executions prove both pre-redaction stream receipts must be present.
        complete_trace = {**export['trace'], 'a2aMessages': export['a2a']}
        self.assertNotIn('toolOutputObservations',
            check_trace_exposure(complete_trace, CRITERIA).coverage['incompleteDocuments'])
        damaged_trace = copy.deepcopy(complete_trace)
        removed = damaged_trace['observations']
        removed['records'] = [row for row in removed['records']
                              if row['kind'] not in ('TOOL_STDOUT', 'TOOL_STDERR')]
        removed['recordsTotal'] = len(removed['records'])
        self.assertIn('toolOutputObservations',
            check_trace_exposure(damaged_trace, CRITERIA).coverage['incompleteDocuments'])

        # A lost scope completion cannot be promoted to complete retry coverage.
        damaged = copy.deepcopy(export)
        damaged['trace']['observations']['scopesComplete'] = False
        self.assertIsNone(process_metrics(str(run_id), derive_process_evidence(damaged))['retries']['channels']['LLM'])

        changed = copy.deepcopy(export)
        initial = changed['trace']['observations']['qaTestInputs'][0]
        changed['trace']['observations']['qaTestInputs'].append({**initial, 'captureId': str(uuid4()),
            'testFilesSha256': 'a' * 64})
        measured = process_metrics(str(run_id), derive_process_evidence(changed))
        # A changed input during initial QA is observed, but no approved repair
        # start exists. File changes alone cannot invent a repair round.
        self.assertEqual(measured['repairs']['routes']['TEST_CHANGE'], {'attempted': None, 'completed': None})
        self.assertIsNone(measured['repairs']['roundCount'])

        # Losing the pre-execution declaration must not invent zero retries.
        changed['trace']['observations']['policy'] = None
        self.assertIsNone(process_metrics(str(run_id), derive_process_evidence(changed))['retries']['channels']['A2A'])

    async def test_preparation_gate_and_output_findings_survive_without_raw_secret(self):
        secret = 'fictional-owned-output-canary'
        from orchestrator.core.security import register_secret_values
        register_secret_values(secret)
        def rejected(run_id):
            ObservationStore(self.base.repository).pin_policy(run_id)
            print('password=' + secret)
            raise ValueError('fixture preparation rejection')
        async with self.base.running_platform(prepare=rejected) as (client, _):
            response = await client.post('/api/v1/runs', json=self.base.submission())
            self.assertEqual(response.status_code, 201)
            run_id = UUID(response.json()['run']['runId'])
        export = self.export(run_id)
        process = process_metrics(str(run_id), derive_process_evidence(export))
        self.assertEqual(process['gateRejections']['count'], 1)
        self.assertEqual(process['retries']['channels']['LLM'], 0)
        self.assertEqual(process['repairs']['routes']['REPLAN']['attempted'], 0)
        observation = export['trace']['observations']
        self.assertNotIn(secret, json.dumps(observation))
        result = check_trace_exposure(documents(runId=str(run_id), observations=observation), CRITERIA)
        self.assertEqual(result.status, 'FAIL')
        self.assertIn('REGISTERED_SECRET_VALUE', result.reasons)
        self.assertNotIn(secret, json.dumps(result.to_dict()))

    async def test_settled_callback_failure_releases_control_and_is_not_repeated(self):
        async with self.base.running_platform(prepare=self.prepare) as (client, _):
            response = await client.post('/api/v1/runs', json=self.base.submission())
            run_id = UUID(response.json()['run']['runId'])
            dispatcher = self.base.platform.dispatcher
            calls = []
            async def callback(selected):
                calls.append(selected)
                raise RuntimeError('fixture callback failed')
            async def operation():
                return None
            dispatcher.set_after_dispatch(callback)
            with self.assertRaisesRegex(RuntimeError, 'fixture callback failed'):
                await dispatcher.control_operation(run_id, operation)
            self.assertEqual(calls, [run_id])
            self.assertFalse(dispatcher.is_run_active(run_id))
            token = self.base.repository.acquire_control(run_id)
            self.base.repository.release_control(run_id, token)


def test_output_receipts_are_immutable_and_do_not_retain_values():
    connection = sqlite3.connect(':memory:')
    run_id = str(uuid4())
    try:
        secret = 'fictional-output-value'
        first = append_output(connection, run_id, 'publication:1', 'A2A_OUTPUT', {'password': secret})
        assert first['findings'] and secret not in json.dumps(first)
        assert append_output(connection, run_id, 'publication:1', 'A2A_OUTPUT', {'password': secret}) == first
        with pytest.raises(ValueError, match='OBSERVATION_IDENTITY_CHANGED'):
            append_output(connection, run_id, 'publication:1', 'A2A_OUTPUT', {'password': 'different'})
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT OR REPLACE INTO run_output_observations SELECT * FROM run_output_observations")
    finally:
        connection.close()


def test_partial_or_malformed_observations_never_prove_clean_output():
    run_id = str(uuid4())
    connection = sqlite3.connect(':memory:')
    append_output(connection, run_id, 'stream', 'HOST_STDOUT', b'prefix', total_bytes=100)
    observation = collect_observations(connection, run_id)
    report = check_trace_exposure(documents(runId=run_id, observations=observation), CRITERIA)
    assert report.status == 'UNVERIFIED'
    observation['records'][0]['rawText'] = 'must not reach a report'
    with pytest.raises(ValueError, match='OUTPUT_OBSERVATION_INVALID'):
        check_trace_exposure(documents(runId=run_id, observations=observation), CRITERIA)
    connection.close()


def test_interrupted_scope_is_visible_and_bounded_streams_remain_partial(monkeypatch):
    from contextlib import contextmanager
    from orchestrator import observability as obs
    class Repository:
        def __init__(self):
            self.connection = sqlite3.connect(':memory:')
        @contextmanager
        def _transaction(self):
            with self.connection:
                yield self.connection
    repo, run_id = Repository(), str(uuid4())
    monkeypatch.setattr(obs, 'MAX_OUTPUT_BYTES', 8)
    with observe_run(repo, run_id):
        assert collect_observations(repo.connection, run_id)['scopesComplete'] is False
        print('0123456789abcdef')
    result = collect_observations(repo.connection, run_id)
    assert result['scopesComplete'] is True
    stdout = next(row for row in result['records'] if row['kind'] == 'HOST_STDOUT')
    assert stdout['status'] == 'PARTIAL' and stdout['observedBytes'] == 8 and stdout['bytes'] == 17
    assert '01234567' not in json.dumps(result)
    repo.connection.close()


def test_historical_receipt_cannot_claim_arbitrary_known_value_matching():
    run_id = str(uuid4())
    connection = sqlite3.connect(':memory:')
    scope_id = uuid4().hex
    for kind in ('HOST_STDOUT', 'HOST_STDERR'):
        append_output(connection, run_id, f'HOST_DISPATCH:{scope_id}:{kind}', kind, 'ordinary output')
    observation = collect_observations(connection, run_id)
    observation.update(scopes=[{'scopeId': scope_id, 'scope': 'HOST_DISPATCH', 'state': state}
                               for state in ('STARTED', 'ENDED')], scopesComplete=True)
    values = documents(runId=run_id, observations=observation)
    assert check_trace_exposure(values, CRITERIA).status == 'PASS'
    result = check_trace_exposure(values, CRITERIA, known_values=['arbitrary-future-canary'])
    assert result.status == 'UNVERIFIED'
    assert 'runtimeKnownValueMatching' in result.coverage['incompleteDocuments']
    connection.close()


def test_redaction_candidate_in_code_is_not_a_confirmed_product_leak():
    run_id = str(uuid4())
    connection = sqlite3.connect(':memory:')
    append_output(connection, run_id, 'model', 'LLM_OUTPUT', {'outputText': 'password = supplied_password'})
    observation = collect_observations(connection, run_id)
    result = check_trace_exposure(documents(runId=run_id, observations=observation), CRITERIA)
    assert result.status == 'UNVERIFIED'
    assert 'outputPatternReview' in result.coverage['incompleteDocuments']
    connection.close()


@pytest.mark.parametrize('payload,kind', [
    ({'sequence': 1, 'outputText': json.dumps({'password': 'synthetic-canary'})}, 'PASSWORD_FIELD_VALUE'),
    ({'toolCalls': [{'arguments': json.dumps({'password': 'synthetic-canary'})}]}, 'PASSWORD_FIELD_VALUE'),
    ('$scrypt$ln=16,r=8,p=1$YWJjZGVmZ2g=$YWJjZGVmZ2hpamtsbW5vcA==', 'PASSWORD_HASH_FORMAT'),
])
def test_wire_json_fields_and_scrypt_are_inspected_before_redaction(payload, kind):
    from orchestrator.observability import output_receipt
    result = output_receipt(uuid4(), 'public-model-output', 'LLM_OUTPUT', payload)
    assert kind in result['findings']
    assert 'synthetic-canary' not in json.dumps(result)


@pytest.mark.parametrize('missing', [('HOST_STDOUT',), ('HOST_STDERR',), ('HOST_STDOUT', 'HOST_STDERR')])
def test_ended_scope_requires_both_stream_receipts(missing):
    run_id, scope_id = str(uuid4()), uuid4().hex
    connection = sqlite3.connect(':memory:')
    for kind in ('HOST_STDOUT', 'HOST_STDERR'):
        if kind not in missing:
            append_output(connection, run_id, f'HOST_DISPATCH:{scope_id}:{kind}', kind, '')
    observation = collect_observations(connection, run_id)
    observation.update(scopes=[{'scopeId': scope_id, 'scope': 'HOST_DISPATCH', 'state': state}
                               for state in ('STARTED', 'ENDED')], scopesComplete=True)
    result = check_trace_exposure(documents(runId=run_id, observations=observation), CRITERIA)
    assert result.status == 'UNVERIFIED'
    assert 'outputScopeReceipts' in result.coverage['incompleteDocuments']
    connection.close()


@pytest.mark.parametrize('missing', [(), ('TOOL_STDOUT',), ('TOOL_STDERR',), ('TOOL_STDOUT', 'TOOL_STDERR')])
@pytest.mark.parametrize('outcome', ['PASS', 'FAIL'])
def test_completed_tool_execution_requires_both_stream_receipts(missing, outcome):
    run_id, scope_id, execution_id = str(uuid4()), uuid4().hex, str(uuid4())
    connection = sqlite3.connect(':memory:')
    for kind in ('HOST_STDOUT', 'HOST_STDERR'):
        append_output(connection, run_id, f'HOST_DISPATCH:{scope_id}:{kind}', kind, '')
    for stream, kind in (('stdout', 'TOOL_STDOUT'), ('stderr', 'TOOL_STDERR')):
        if kind not in missing:
            append_output(connection, run_id, f'sandbox:{execution_id}:{stream}', kind, '')
    observation = collect_observations(connection, run_id)
    observation.update(policy={'producer': 'OWNED_HOST_OBSERVATION_V1'},
        scopes=[{'scopeId': scope_id, 'scope': 'HOST_DISPATCH', 'state': state}
                for state in ('STARTED', 'ENDED')], scopesComplete=True)
    telemetry = [{'recordType': 'EVENT', 'kind': 'MCP', 'binding': {'eventType': 'MCP_TOOL_FINISHED'},
                  'detail': {'executionId': execution_id, 'status': 'FINISHED', 'outcome': outcome,
                             'deliveryState': 'REPLIED', 'resultUnknown': False}}]
    values = documents(runId=run_id, observations=observation, telemetry=telemetry, telemetryTotal=1)
    result = check_trace_exposure(values, CRITERIA)
    assert result.status == ('UNVERIFIED' if missing else 'PASS')
    assert ('toolOutputObservations' in result.coverage['incompleteDocuments']) == bool(missing)
    # Non-sandbox writes and interrupted calls have no returned execution ID.
    # Do not manufacture a missing stream for these unsupported boundaries.
    observation['records'] = [row for row in observation['records'] if row['kind'].startswith('HOST_')]
    observation['recordsTotal'] = len(observation['records'])
    telemetry[0]['detail'].update(executionId=None, outcome='UNVERIFIED', resultUnknown=True)
    assert check_trace_exposure(values, CRITERIA).status == 'PASS'
    connection.close()


def test_log_observation_includes_traceback_stack_and_structured_extras(monkeypatch):
    import logging
    from orchestrator import observability as obs
    from orchestrator.core.logging import SecretRedactionFilter
    captured = []
    monkeypatch.setattr(obs, 'observe_log', captured.append)
    monkeypatch.setattr(obs, 'output_observation_active', lambda: True)
    failure = ValueError('password=traceback-canary')
    record = logging.LogRecord('fixture', logging.ERROR, 'fixture.py', 1, 'operation failed', (),
                               (ValueError, failure, None), sinfo='password=stack-canary')
    record.password = 'structured-canary'
    record.location = Path('/fictional/location')
    record.failure = ValueError('structured error')
    assert SecretRedactionFilter().filter(record)
    assert 'traceback-canary' in captured[0]['exception']
    assert 'stack-canary' in captured[0]['stack']
    assert captured[0]['fields']['password'] == 'structured-canary'
    assert captured[0]['fields']['location'] == '/fictional/location'
    assert captured[0]['fields']['failure'] == 'structured error'
    assert 'traceback-canary' not in record.exc_text
    assert 'stack-canary' not in record.stack_info
    assert record.password == '[REDACTED]'
