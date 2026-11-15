"""Actual Host exports plus damaged copies; no network or paid model calls."""
import copy
import json

import pytest

from evaluation.agent_eval import agent_tables
from evaluation.evidence_metrics import process_metrics
from evaluation.host_evidence import budget_assessment
from evaluation.host_process import derive_process_evidence
from evaluation.workflow import load_handoff
from tests.evaluation.test_dev_compatibility import exports  # noqa: F401


@pytest.fixture
def exported(exports):
    return load_handoff(exports / 'fix-success')[0]


def test_budget_matches_actual_ledger_and_all_work_and_review_receipts(exported):
    result = budget_assessment(exported['bundle'])
    assert result['status'] == 'PASS'
    assert result['modelCalls'] == exported['bundle']['usage']['total']
    assert result['totalTokens'] > 0


@pytest.mark.parametrize('damage', [
    'missing', 'wrong_run', 'fingerprint', 'deadline', 'invalidated', 'receipt_missing',
    'receipt_value', 'receipt_sequence', 'event_run', 'end_missing', 'event_total', 'usage_unknown',
])
def test_unprovable_budget_is_not_pass(exported, damage):
    bundle = exported['bundle']
    ledger = bundle['budgetEvidence']['snapshot']
    if damage == 'missing': del bundle['budgetEvidence']
    elif damage == 'wrong_run': bundle['budgetEvidence']['runId'] = 'wrong'
    elif damage == 'fingerprint': ledger['configuration_fingerprint'] = 'f' * 64
    elif damage == 'deadline': ledger['deadline_utc'] = ledger['created_at_utc']
    elif damage == 'invalidated': ledger['invalidated'] = True
    elif damage == 'receipt_missing': bundle['usage']['records'].pop(); bundle['usage']['total'] -= 1
    elif damage == 'receipt_value': bundle['usage']['records'][0]['totalTokens'] += 1
    elif damage == 'receipt_sequence': bundle['usage']['records'][0]['sequence'] = 999
    elif damage == 'usage_unknown': bundle['usage']['records'][0]['usageKnown'] = False
    elif damage == 'event_run': bundle['events'][0]['runId'] = 'wrong'
    elif damage == 'end_missing':
        bundle['events'] = [e for e in bundle['events'] if e['eventType'] != 'RUN_FINISHED']
        bundle['eventsTotal'] = len(bundle['events'])
    else: bundle['eventsTotal'] += 1
    assert budget_assessment(bundle)['status'] != 'PASS'


def test_actual_process_counts_do_not_invent_other_routes(exported):
    evidence = derive_process_evidence(exported)
    metrics = process_metrics(exported['bundle']['run']['runId'], evidence)
    assert metrics['repairs']['routes']['IMPLEMENTATION'] == {'attempted': 1, 'completed': 1}
    assert metrics['repairs']['routes']['REPLAN']['attempted'] is None
    assert metrics['retries']['channels']['TOOL'] == 0
    assert metrics['retries']['channels']['LLM'] is None
    assert metrics['conflicts']['status'] == 'PASS'
    assert metrics['conflicts']['contradictions'] is None
    assert metrics['gateRejections']['count'] is None


def test_incomplete_events_and_legacy_tool_rows_remain_unknown(exported):
    exported['bundle']['eventsTotal'] += 1
    for row in exported['trace']['telemetry']:
        row['detail'].pop('logicalCallId', None)
    evidence = derive_process_evidence(exported)
    metrics = process_metrics(exported['bundle']['run']['runId'], evidence)
    assert metrics['repairs']['routes']['IMPLEMENTATION']['attempted'] is None
    assert metrics['retries']['channels']['TOOL'] is None


@pytest.mark.parametrize('damage,status', [('missing','UNVERIFIED'), ('hash','FAIL'),
                                          ('context','FAIL'), ('duplicate','FAIL'), ('truncated','UNVERIFIED')])
def test_model_input_checks_actual_provider_evidence(exported, damage, status):
    rows = exported['trace']['telemetry']
    row = next(r for r in rows if r['binding'].get('eventType') == 'MODEL_INPUT_OBSERVED')
    step_id = row['binding']['workflowStepId']
    if damage == 'missing': rows.remove(row); exported['trace']['telemetryTotal'] -= 1
    elif damage == 'hash': row['detail']['metadataSha256'] = '0' * 64
    elif damage == 'context': row['binding']['agentContextId'] = 'wrong'
    elif damage == 'duplicate':
        other = copy.deepcopy(row); other['detail']['taskInputSha256'] = '1' * 64
        rows.append(other); exported['trace']['telemetryTotal'] += 1
    else: exported['trace']['telemetryTotal'] += 1
    table = next(r for r in agent_tables(exported)['handoffs'] if r['workflowStepId'] == step_id)
    assert table['modelInput']['status'] == status


def test_prompt_body_is_not_in_hash_observation(exported):
    rows = [r for r in exported['trace']['telemetry'] if r['binding'].get('eventType') == 'MODEL_INPUT_OBSERVED']
    assert rows
    expected = {'sequence', 'metadataSha256', 'taskInputSha256', 'initialInputSha256',
                'systemSha256', 'inputItemsSha256', 'requestedModel'}
    for row in rows:
        assert set(row['detail']) == expected
        for key in expected - {'sequence', 'requestedModel'}:
            assert len(row['detail'][key]) == 64
