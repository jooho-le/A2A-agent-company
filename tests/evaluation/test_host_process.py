"""Host test-input observations must not multiply approved repair cycles."""
import copy
from hashlib import sha256
import io
import unittest
from uuid import uuid4

import pytest

from evaluation.evidence_metrics import process_metrics
from evaluation.host_process import derive_process_evidence


def repaired_export():
    run_id, qa1, qa2, dev2 = (str(uuid4()) for _ in range(4))
    source1, source2 = (str(uuid4()) for _ in range(2))
    events = [{'eventId': str(uuid4()), 'runId': run_id, 'eventType': kind, 'attempt': 1,
               'workflowStepId': dev2, 'codeVersion': 2}
              for kind in ('FIX_ATTEMPT_STARTED', 'FIX_COMPLETED')]
    for step, source, version in ((qa1, source1, 1), (qa2, source2, 2)):
        for kind in ('VALIDATION_STARTED' if version == 1 else 'REVALIDATION_STARTED', 'WORKFLOW_STEP_CREATED'):
            events.append({'eventId': str(uuid4()), 'runId': run_id, 'eventType': kind, 'attempt': version - 1,
                           'workflowStepId': step if kind == 'WORKFLOW_STEP_CREATED' else None,
                           'codeVersion': version, 'inputArtifactIds': [source]})
    captures = [{'captureId': str(uuid4()), 'workflowStepId': step, 'toolName': 'run_unit_tests',
                 'sourceArtifactId': source, 'selector': 'tests', 'testFilesSha256': digest * 64,
                 'executionManifestId': str(uuid4())}
                for step, source, digest in ((qa1, source1, 'a'), (qa2, source2, 'b'))]
    return {'bundle': {'run': {'runId': run_id, 'status': 'FINISHED', 'fixAttempt': 1}, 'events': events,
                       'eventsTotal': len(events), 'snapshotConsistent': True,
                       'configuration': {'configuration': {'architecture': 'MULTI_AGENT'}}},
            'trace': {'telemetry': [], 'telemetryTotal': 0, 'artifacts': [], 'observations': {
                'runId': run_id, 'snapshotConsistent': True, 'scopesComplete': True,
                'scopes': [{'scopeId': 'c' * 32, 'scope': 'HOST_DISPATCH', 'state': state}
                           for state in ('STARTED', 'ENDED')],
                'policy': {'schemaVersion': 1, 'runId': run_id, 'producer': 'OWNED_HOST_OBSERVATION_V1',
                           'architecture': 'MULTI_AGENT', 'scope': 'OWNED_AUTOMATIC_SEGMENT',
                           'automaticRetries': {'A2A': 'DISABLED', 'LLM': 'DISABLED'},
                           'repairRoutes': {'REPLAN': 'UNSUPPORTED'}},
                'qaTestInputs': captures}},
            'handoff': {'steps': [
                {'workflowStepId': qa1, 'phase': 'QA', 'attempt': 0, 'codeVersion': 1},
                {'workflowStepId': qa2, 'phase': 'QA', 'attempt': 0, 'codeVersion': 2},
                {'workflowStepId': dev2, 'phase': 'FIXING', 'attempt': 0, 'codeVersion': 2}]},
            'snapshotTestsComplete': True,
            'snapshotTests': [{'codeVersion': 1, 'testFilesSha256': 'a' * 64},
                              {'codeVersion': 2, 'testFilesSha256': 'b' * 64}]}


def metrics(export):
    evidence = derive_process_evidence(export)
    return evidence, process_metrics(export['bundle']['run']['runId'], evidence)['repairs']


def test_snapshot_and_repeated_qa_changes_share_the_actual_implementation_repair_round():
    export = repaired_export()
    capture = export['trace']['observations']['qaTestInputs'][-1]
    export['trace']['observations']['qaTestInputs'].append({**capture, 'captureId': str(uuid4()),
                                                         'testFilesSha256': 'd' * 64})
    evidence, repairs = metrics(export)
    assert repairs['status'] == 'PASS' and repairs['roundCount'] == 1
    assert repairs['routes']['IMPLEMENTATION'] == {'attempted': 1, 'completed': 1}
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': 1, 'completed': 1}
    assert len(evidence['observedTestChanges']) == 3  # observations are retained, not counted as three operations
    assert {row['repairRoundId'] for row in evidence['observedTestChanges']} == {'repair:1'}


@pytest.mark.parametrize('damage', ['missing_step', 'initial_qa', 'missing_start', 'wrong_phase', 'missing_snapshot_step',
                                   'wrong_source', 'missing_revalidation', 'wrong_fix_step'])
def test_observed_change_without_actual_repair_binding_is_not_zero_or_a_new_round(damage):
    export = repaired_export()
    if damage == 'missing_step':
        export['trace']['observations']['qaTestInputs'][-1]['workflowStepId'] = str(uuid4())
    elif damage == 'initial_qa':
        export['trace']['observations']['qaTestInputs'][-1]['workflowStepId'] = export['handoff']['steps'][0]['workflowStepId']
    elif damage == 'wrong_phase':
        export['handoff']['steps'][1]['phase'] = 'SECURITY'
    elif damage == 'missing_snapshot_step':
        export['handoff']['steps'].pop()
    elif damage == 'wrong_source':
        export['trace']['observations']['qaTestInputs'][-1]['sourceArtifactId'] = str(uuid4())
    elif damage == 'missing_revalidation':
        export['bundle']['events'] = [row for row in export['bundle']['events'] if row['eventType'] != 'REVALIDATION_STARTED']
        export['bundle']['eventsTotal'] = len(export['bundle']['events'])
    elif damage == 'wrong_fix_step':
        export['bundle']['events'][0]['workflowStepId'] = str(uuid4())
    else:
        export['bundle']['events'] = []
        export['bundle']['eventsTotal'] = 0
        export['bundle']['run']['fixAttempt'] = 0
    evidence, repairs = metrics(export)
    assert repairs['status'] == 'UNVERIFIED' and repairs['roundCount'] is None
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': None, 'completed': None}
    assert any(row['status'] == 'UNVERIFIED' and row['repairRoundId'] is None for row in evidence['observedTestChanges'])


def test_changed_capture_without_execution_receipt_does_not_become_completed_from_snapshot():
    export = repaired_export()
    export['trace']['observations']['qaTestInputs'][-1]['executionManifestId'] = None
    _evidence, repairs = metrics(export)
    assert repairs['roundCount'] == 1
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': 1, 'completed': 0}


def test_unchanged_tests_remain_confirmed_zero_with_complete_observation_scope():
    export = repaired_export()
    export['trace']['observations']['qaTestInputs'][-1]['testFilesSha256'] = 'a' * 64
    export['snapshotTests'][-1]['testFilesSha256'] = 'a' * 64
    evidence, repairs = metrics(export)
    assert not evidence['observedTestChanges']
    assert repairs['roundCount'] == 1  # the implementation repair still happened
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': 0, 'completed': 0}


def test_old_disabled_llm_policy_without_call_receipts_cannot_certify_zero_retries():
    export = repaired_export()
    result = process_metrics(export['bundle']['run']['runId'], derive_process_evidence(export))
    assert result['retries']['channels']['A2A'] == 0
    assert result['retries']['channels']['LLM'] is None


def test_second_actual_repair_gets_a_second_round_not_a_capture_sequence_number():
    export = repaired_export()
    run_id = export['bundle']['run']['runId']
    developer_step = str(uuid4())
    export['bundle']['run']['fixAttempt'] = 2
    export['bundle']['events'] += [{'eventId': str(uuid4()), 'runId': run_id, 'eventType': kind, 'attempt': 2,
                                  'workflowStepId': developer_step, 'codeVersion': 3}
                                   for kind in ('FIX_ATTEMPT_STARTED', 'FIX_COMPLETED')]
    export['bundle']['eventsTotal'] = len(export['bundle']['events'])
    step = {**export['handoff']['steps'][1], 'workflowStepId': str(uuid4()), 'attempt': 0, 'codeVersion': 3}
    export['handoff']['steps'].append(step)
    export['handoff']['steps'].append({'workflowStepId': developer_step, 'phase': 'FIXING', 'attempt': 0, 'codeVersion': 3})
    capture = copy.deepcopy(export['trace']['observations']['qaTestInputs'][-1])
    capture.update(captureId=str(uuid4()), workflowStepId=step['workflowStepId'], testFilesSha256='c' * 64,
                   sourceArtifactId=str(uuid4()))
    export['trace']['observations']['qaTestInputs'].append(capture)
    for kind in ('REVALIDATION_STARTED', 'WORKFLOW_STEP_CREATED'):
        export['bundle']['events'].append({'eventId': str(uuid4()), 'runId': run_id, 'eventType': kind, 'attempt': 2,
            'workflowStepId': step['workflowStepId'] if kind == 'WORKFLOW_STEP_CREATED' else None,
            'codeVersion': 3, 'inputArtifactIds': [capture['sourceArtifactId']]})
    export['bundle']['eventsTotal'] = len(export['bundle']['events'])
    _evidence, repairs = metrics(export)
    assert repairs['roundCount'] == 2
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': 2, 'completed': 2}


def test_a2a_continuation_attempt_is_not_used_as_a_repair_round():
    export = repaired_export()
    for step in export['handoff']['steps']:
        step['attempt'] = 3
    _evidence, repairs = metrics(export)
    assert repairs['roundCount'] == 1
    assert repairs['routes']['TEST_CHANGE'] == {'attempted': 1, 'completed': 1}


def test_real_host_export_keeps_repair_and_validation_cycles_separate(tmp_path):
    """Actual SQLite/dispatch/export producer; fake LLM and inert Docker transport."""
    from evaluation.run_metrics import run_metrics
    from evaluation.workflow import load_handoff
    from tests.evaluation import dev_harness_exports as harness

    case = harness.FixSuccess()
    case.out = tmp_path
    result = unittest.TextTestRunner(stream=io.StringIO()).run(unittest.TestSuite([case]))
    assert result.wasSuccessful(), result.errors + result.failures
    export, _run = load_handoff(tmp_path / 'fix-success')
    expected_digest = sha256('보호된 회원가입 요구사항을 구현해줘.'.encode('utf-8')).hexdigest()
    assert export['bundle']['run']['requestTextSha256'] == expected_digest
    assert run_metrics(export['bundle'])['requestTextSha256'] == expected_digest
    assert export['bundle']['run']['fixAttempt'] == 1
    assert all(step['attempt'] == 0 for step in export['handoff']['steps'])
    evidence = derive_process_evidence(export)
    conflicts = [row for row in evidence['events'] if row['kind'] == 'CONFLICT']
    assert not conflicts  # v1/v2 were separate validation cycles, not mismatching pairs
    assert process_metrics(export['bundle']['run']['runId'], evidence)['conflicts']['count'] == 0

    # Vary only observed test bytes. Retain actual producer step IDs, Source
    # IDs, code versions, continuation attempts and all lifecycle events.
    changed = copy.deepcopy(export)
    qa2 = next(step['workflowStepId'] for step in changed['handoff']['steps']
               if step['phase'] == 'QA' and step['codeVersion'] == 2)
    captures = changed['trace']['observations']['qaTestInputs']
    targets = [row for row in captures if row['workflowStepId'] == qa2]
    assert targets
    for row in targets:
        row['testFilesSha256'] = 'a' * 64
    changed['snapshotTests'][-1]['testFilesSha256'] = 'a' * 64
    evidence = derive_process_evidence(changed)
    test_repairs = [row for row in evidence['events'] if row['kind'] == 'REPAIR' and row['route'] == 'TEST_CHANGE']
    assert len(test_repairs) == 2
    assert {row['action'] for row in test_repairs} == {'STARTED', 'COMPLETED'}
    assert {row['roundId'] for row in test_repairs} == {'repair:1'}
    assert all(row['status'] == 'LINKED' for row in evidence['observedTestChanges'])

    # A report for another Snapshot inside the same actual cycle remains a
    # VERSION_MISMATCH; grouping must not use the reported version itself.
    damaged = copy.deepcopy(export)
    reports = damaged['trace']['artifacts']
    old_security = next(row for row in reports if row['artifactType'] == 'SECURITY_REPORT' and row['codeVersion'] == 1)
    new_security = next(row for row in reports if row['artifactType'] == 'SECURITY_REPORT' and row['codeVersion'] == 2)
    replacement = {**copy.deepcopy(old_security), 'workflowStepId': new_security['workflowStepId'],
                   'artifactId': new_security['artifactId']}
    reports[reports.index(new_security)] = replacement
    evidence = derive_process_evidence(damaged)
    assert any(row['kind'] == 'CONFLICT' and 'VERSION_MISMATCH' in row['reasons'] for row in evidence['events'])


def test_orchestrator_gate_rejection_event_is_counted_under_its_own_stage():
    """dev가 HUMAN_REVIEW 전환 때 남기는 PROCESS_GATE_REJECTED는 Orchestrator 관문 거부 1건이다(점검 2026-11-15)."""
    export = repaired_export()
    run_id = export['bundle']['run']['runId']
    rejected = {'eventId': str(uuid4()), 'runId': run_id, 'eventType': 'PROCESS_GATE_REJECTED', 'attempt': 0,
                'workflowStepId': str(uuid4()), 'codeVersion': 1, 'workflowState': 'HUMAN_REVIEW'}
    export['bundle']['events'].append(rejected)
    export['bundle']['eventsTotal'] += 1
    evidence = derive_process_evidence(export)
    gates = [row for row in evidence['events'] if row['kind'] == 'GATE_REJECTION']
    assert [(row['rejectionId'], row['stage'], row['operationId']) for row in gates] == [
        (rejected['eventId'], 'ORCHESTRATOR_OUTPUT', rejected['workflowStepId'])]
    assert 'GATE_REJECTION' in evidence['completeKinds']
    measured = process_metrics(run_id, evidence)['gateRejections']
    assert measured['count'] == 1 and measured['stages'] == {'ORCHESTRATOR_OUTPUT': 1}
