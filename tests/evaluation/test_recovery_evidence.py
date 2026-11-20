"""Missing or disconnected recovery evidence is unknown, never a made-up zero."""
from copy import deepcopy

import pytest

from agents.llm.recovery import CallRoute, FailureRoute, correction_route
from evaluation.outcomes import classify_run
from evaluation.recovery_evidence import recovery_assessment


def exported(*, execute_retry=True):
    events, telemetry = [], []
    def add(kind, detail, sequence):
        binding = {'eventId': f'{kind}:{sequence}', 'runId': 'run', 'workflowStepId': 'step',
                   'a2aTaskId': 'task', 'agentContextId': 'context', 'actor': 'DEVELOPER', 'eventType': kind}
        events.append(binding)
        telemetry.append({'binding': deepcopy(binding), 'detail': detail})
    def call(sequence, mode, **kwargs):
        add('LLM_MODEL_CALLED', {'sequence': sequence}, sequence)
        route = CallRoute(sequence=sequence, operationSequence=1, mode=mode, **kwargs)
        add('LLM_CALL_ROUTED', {'recovery': route.public()}, sequence)
    call(1, 'INITIAL', correctionAttempt=0)
    decision = correction_route('MODEL_OUTPUT_INVALID', sequence=1, operation_sequence=1,
                                allowed=True, reason='ALLOWED')
    add('LLM_RECOVERY_DECIDED', {'recovery': decision.public()}, 1)
    if execute_retry:
        call(2, 'REGENERATION', correctionAttempt=1, triggerSequence=1, code='MODEL_OUTPUT_INVALID')
        call(3, 'CONTINUATION', correctionAttempt=0)
    usage = [{'sequence': i, 'usageKnown': False, 'binding': {'runId': 'run', 'workflowStepId': 'step',
              'a2aTaskId': 'task', 'agentContextId': 'context', 'role': 'DEVELOPER'}}
             for i in ((1, 2, 3) if execute_retry else (1,))]
    return {'run': {'runId': 'run'}, 'events': events, 'eventsTotal': len(events), 'snapshotConsistent': True,
            'usage': {'runId': 'run', 'records': usage, 'total': len(usage)}}, {
        'telemetry': telemetry, 'telemetryTotal': len(telemetry)}


def test_only_real_regeneration_call_is_counted_not_followup_or_decision():
    bundle, trace = exported()
    result = recovery_assessment(bundle, trace)
    assert (result['status'], result['regenerationCount']) == ('PASS', 1)
    assert result['regenerations'][0]['triggerSequence'] == 1
    bundle, trace = exported(execute_retry=False)
    result = recovery_assessment(bundle, trace)
    assert (result['status'], result['regenerationCount']) == ('PASS', 0)


@pytest.mark.parametrize('damage', ['missing_receipt', 'missing_decision', 'partial_trace', 'foreign_step',
                                   'foreign_run', 'duplicate', 'wrong_cause', 'attempt_gap', 'unknown_code',
                                   'wrong_canonical_binding', 'missing_start', 'missing_usage', 'missing_policy',
                                   'malformed_row', 'conflicting_event', 'foreign_usage_run', 'foreign_usage_task'])
def test_incomplete_or_forged_receipts_never_prove_retry_count(damage):
    bundle, trace = exported()
    calls = [row for row in trace['telemetry'] if row['binding']['eventType'] == 'LLM_CALL_ROUTED']
    retry = calls[1]
    if damage == 'missing_receipt':
        trace['telemetry'].remove(retry)
    elif damage == 'missing_decision':
        trace['telemetry'] = [row for row in trace['telemetry'] if row['binding']['eventType'] != 'LLM_RECOVERY_DECIDED']
    elif damage == 'partial_trace':
        bundle['snapshotConsistent'] = False
    elif damage == 'foreign_step':
        retry['binding']['workflowStepId'] = 'another-step'
    elif damage == 'foreign_run':
        retry['binding']['runId'] = 'another-run'
    elif damage == 'duplicate':
        trace['telemetry'].append(deepcopy(retry))
    elif damage == 'wrong_cause':
        retry['detail']['recovery']['code'] = 'TOOL_ARGUMENTS_INVALID'
    elif damage == 'attempt_gap':
        retry['detail']['recovery']['correctionAttempt'] = 2
    elif damage == 'unknown_code':
        retry['detail']['recovery']['code'] = 'private-submitted-text'
    elif damage == 'missing_start':
        trace['telemetry'] = [row for row in trace['telemetry'] if not (
            row['binding']['eventType'] == 'LLM_MODEL_CALLED' and row['detail']['sequence'] == 2)]
    elif damage == 'missing_usage':
        bundle['usage']['records'].pop(1)
        bundle['usage']['total'] -= 1
    elif damage == 'missing_policy':
        retry['detail']['recovery'].pop('policy')
    elif damage == 'malformed_row':
        trace['telemetry'].append(None)
    elif damage == 'conflicting_event':
        bundle['events'].append({**bundle['events'][0], 'actor': 'SECURITY'})
        bundle['eventsTotal'] += 1
    elif damage == 'foreign_usage_run':
        bundle['usage']['runId'] = 'another-run'
    elif damage == 'foreign_usage_task':
        bundle['usage']['records'][1]['binding']['a2aTaskId'] = 'another-task'
    else:
        retry['binding']['actor'] = 'SECURITY'
    trace['telemetryTotal'] = len(trace['telemetry'])
    result = recovery_assessment(bundle, trace)
    assert result['status'] == 'UNVERIFIED'
    assert result['regenerationCount'] is None
    assert result['regenerations'] == []


def test_legacy_missing_receipts_are_unknown_even_when_telemetry_page_is_complete():
    bundle, trace = exported()
    trace['telemetry'] = [row for row in trace['telemetry'] if row['binding']['eventType'] == 'LLM_MODEL_CALLED']
    trace['telemetryTotal'] = len(trace['telemetry'])
    assert recovery_assessment(bundle, trace)['regenerationCount'] is None


@pytest.mark.parametrize('code,category,cause,validity', [
    ('LLM_SCHEMA_INVALID', 'HOST_CONFIGURATION', 'HOST_INTEGRATION_FAILURE', 'UNVERIFIED'),
    ('LLM_RESPONSE_INVALID', 'EXECUTION_UNCERTAIN', 'MODEL_RESPONSE_UNVERIFIED', 'REVIEW_REQUIRED'),
])
def test_terminal_cause_uses_bound_failure_boundary_not_just_generic_error_code(code, category, cause, validity):
    bundle, trace = exported(execute_retry=False)
    bundle['run'].update(status='HUMAN_REVIEW', verdict=None)
    row = next(row for row in trace['telemetry'] if row['binding']['eventType'] == 'LLM_RECOVERY_DECIDED')
    row['detail']['recovery'] = FailureRoute(sequence=1, operationSequence=1, code=code, category=category,
        action='STOP', reason='NOT_SAFE_TO_RETRY').public()
    a2a = {'roles': {'DEVELOPER': {'events': [{'eventType': 'TaskStatusUpdateEvent', 'taskId': 'task', 'version': 1,
        'event': {'status': {'state': 'TASK_STATE_FAILED', 'message': {'parts': [{'data': {'code': code}}]}}}}]}}}
    result = classify_run(bundle, trace, a2a)
    assert (result['cause'], result['comparisonValidity']) == (cause, validity)
    assert result['details']['failureCategory'] == category
