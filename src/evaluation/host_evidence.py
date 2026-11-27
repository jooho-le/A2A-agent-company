"""Host가 남긴 실행 증거의 평가. 기록이 없거나 서로 다르면 확인된 통과를 만들지 않는다."""
from datetime import timedelta
from hashlib import sha256

from .evidence_metrics import _time, _unique


def _result(status='UNVERIFIED', reason=None, **details):
    return {'status': status, 'reason': reason, **details}


def budget_assessment(bundle):
    evidence = (bundle or {}).get('budgetEvidence')
    if not evidence:
        return _result(reason='LLM_LIMIT_EVIDENCE_MISSING')
    try:
        from agents.llm.budget import BudgetSnapshot
        from orchestrator.domain.run_configuration import RunConfigurationArtifact
        run = bundle['run']
        if type(evidence.get('schemaVersion')) is not int or evidence['schemaVersion'] != 1 or evidence.get('runId') != run['runId']:
            raise ValueError('BUDGET_RUN_MISMATCH')
        if evidence.get('snapshot') is None:
            return _result(reason=evidence.get('reason') or 'BUDGET_LEDGER_MISSING')
        budget = BudgetSnapshot.model_validate(evidence['snapshot'])
        config = {key: value for key, value in bundle['configuration'].items()
                  if key not in ('artifactUri', 'scenarioContract', 'scenarioContractSha256')}
        configuration = RunConfigurationArtifact.model_validate(config)
        fingerprint = sha256(configuration.model_dump_json(by_alias=True).encode('utf-8')).hexdigest()
        if (str(budget.run_id) != run['runId'] or str(configuration.run_id) != run['runId']
                or budget.configuration_fingerprint != fingerprint):
            raise ValueError('BUDGET_CONFIGURATION_MISMATCH')
        created = _time(run['createdAt'])
        runtime = configuration.configuration.limits.runtime_budget_ms
        if (runtime is None or budget.created_at_utc != created
                or budget.deadline_utc != created + timedelta(milliseconds=runtime)):
            raise ValueError('BUDGET_DEADLINE_MISMATCH')
        if budget.invalidated:
            return _result(reason='BUDGET_LEDGER_INVALIDATED')
        usage = bundle.get('usage') or {}
        rows = usage.get('records')
        if (not isinstance(rows, list) or usage.get('total') != len(rows)
                or usage.get('recordsConsistent') is False or budget.pending_model_sequences or budget.pending_tool_sequences):
            return _result(reason='BUDGET_ACCOUNTING_INCOMPLETE')
        receipts = {}
        for row in rows:
            detail = row.get('detail') or row
            if any(key in row and row[key] != detail.get(key) for key in (
                    'sequence', 'usageKnown', 'inputTokens', 'outputTokens', 'totalTokens',
                    'cachedInputTokens', 'reasoningOutputTokens', 'cacheWriteInputTokens')):
                raise ValueError('BUDGET_USAGE_VALUE_MISMATCH')
            sequence = detail.get('sequence')
            if isinstance(sequence, float) and sequence.is_integer(): sequence = int(sequence)
            if type(sequence) is not int or sequence in receipts:
                raise ValueError('BUDGET_USAGE_SEQUENCE_INVALID')
            if (row.get('binding') or {}).get('runId') not in (None, run['runId']):
                raise ValueError('BUDGET_USAGE_RUN_MISMATCH')
            receipts[sequence] = detail
        if set(receipts) != set(budget.accounted_usage):
            return _result(reason='BUDGET_USAGE_SEQUENCE_MISMATCH')
        fields = ('input_tokens', 'output_tokens', 'total_tokens', 'cached_input_tokens',
                  'reasoning_output_tokens', 'cache_write_input_tokens')
        aliases = ('inputTokens', 'outputTokens', 'totalTokens', 'cachedInputTokens', 'reasoningOutputTokens', 'cacheWriteInputTokens')
        for sequence, measured in budget.accounted_usage.items():
            receipt = receipts[sequence]
            if measured is None or receipt.get('usageKnown') is not True:
                return _result(reason='BUDGET_USAGE_UNKNOWN')
            if any(receipt.get(alias) != getattr(measured, key) for key, alias in zip(fields, aliases)):
                raise ValueError('BUDGET_USAGE_VALUE_MISMATCH')
        if not budget.usage_complete:
            return _result(reason='BUDGET_USAGE_UNKNOWN')
        details = {'limits': budget.limits.model_dump(mode='json'), 'modelCalls': budget.model_calls,
                   'modelToolCalls': budget.tool_calls, 'totalTokens': budget.known_total_tokens,
                   'revision': budget.revision, 'configurationFingerprint': fingerprint}
        if budget.limits.max_total_tokens is not None and budget.known_total_tokens > budget.limits.max_total_tokens:
            return _result('FAIL', 'TOTAL_TOKEN_LIMIT_EXCEEDED', **details)
        if any(value.output_tokens > budget.limits.max_output_tokens for value in budget.accounted_usage.values()):
            return _result('FAIL', 'OUTPUT_TOKEN_LIMIT_EXCEEDED', **details)
        events = bundle.get('events')
        if (not isinstance(events, list) or bundle.get('eventsTotal') != len(events)
                or bundle.get('snapshotConsistent') is not True):
            return _result(reason='BUDGET_END_EVIDENCE_INCOMPLETE', **details)
        events = _unique(events, 'eventId')
        if any(row.get('runId') != run['runId'] for row in events):
            raise ValueError('BUDGET_EVENT_RUN_MISMATCH')
        ends = [_time(row['occurredAt']) for row in events if row.get('eventType') == 'RUN_FINISHED']
        if run.get('status') != 'FINISHED' or len(ends) != 1:
            return _result(reason='BUDGET_RUN_NOT_FINISHED', **details)
        if min(ends) < created:
            return _result(reason='BUDGET_END_TIME_INVALID', **details)
        if max(ends) > budget.deadline_utc:
            return _result('FAIL', 'RUNTIME_BUDGET_EXCEEDED', **details)
        return _result('PASS', **details)
    except Exception as error:
        # Pydantic/DB errors can carry input values. Only our stable reason strings leave this boundary.
        code = str(error) if type(error) is ValueError and str(error).startswith('BUDGET_') else 'BUDGET_EVIDENCE_INVALID'
        return _result(reason=code)
