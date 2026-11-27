"""A2A metadata to actual provider-bound input. Hash consistency is not semantic understanding."""
from agents.llm.input_evidence import ModelInputEvidence, json_sha256
from orchestrator.a2a.requests import A2AWorkflowMetadata


def model_input_assessment(run, step):
    def result(status='UNVERIFIED', reason=None, count=None):
        return {'status': status, 'value': None if count is None else f'모델 입력 {count}건', 'detail': reason}
    task = run.tasks.get((step.get('agentIdentity'), step.get('a2aTaskId')))
    if task is None or run.telemetry is None or run.review_sequences is None:
        return result(reason='MODEL_INPUT_EVIDENCE_MISSING')
    try:
        expected = json_sha256(A2AWorkflowMetadata.model_validate(task['metadata']).to_a2a_json())
        observed, calls = {}, set()
        for row in run.telemetry:
            binding, detail = row.get('binding') or {}, row.get('detail') or {}
            if binding.get('workflowStepId') != step['workflowStepId']: continue
            event = binding.get('eventType')
            if event not in ('LLM_MODEL_CALLED', 'MODEL_INPUT_OBSERVED'): continue
            seq = detail.get('sequence')
            if type(seq) is not int or seq < 1:
                return result(reason='MODEL_INPUT_SEQUENCE_INVALID')
            if seq in run.review_sequences: continue
            if (binding.get('runId') != run.bundle['run']['runId']
                    or binding.get('a2aTaskId') != task['taskId']
                    or binding.get('agentContextId') != task['contextId']
                    or binding.get('actor') != step.get('agentIdentity')):
                return result('FAIL', 'MODEL_INPUT_BINDING_MISMATCH')
            if event == 'LLM_MODEL_CALLED':
                calls.add(seq)
            else:
                typed = ModelInputEvidence.model_validate({k: v for k, v in detail.items() if k != 'requestedModel'})
                if typed.metadataSha256 != expected:
                    return result('FAIL', 'MODEL_INPUT_METADATA_MISMATCH')
                if seq in observed and observed[seq] != typed:
                    return result('FAIL', 'MODEL_INPUT_RECORD_CHANGED')
                observed[seq] = typed
        if not calls or set(observed) != calls or run.trace.get('telemetryTotal') != len(run.telemetry):
            return result(reason='MODEL_INPUT_COVERAGE_INCOMPLETE')
        initial = {(v.initialInputSha256, v.taskInputSha256, v.metadataSha256, v.systemSha256) for v in observed.values()}
        if len(initial) != 1:
            return result('FAIL', 'MODEL_INPUT_CHANGED_WITHIN_TASK')
        return result('PASS', 'PROVIDER_BOUND_METADATA_ONLY', len(calls))
    except (ValueError, KeyError, TypeError):
        return result(reason='MODEL_INPUT_EVIDENCE_INVALID')
