"""Count actual regeneration attempts, never feedback messages or normal calls."""
from agents.llm.recovery import CallRoute, FailureRoute, POLICY


def recovery_assessment(bundle, trace):
    result = {'status': 'UNVERIFIED', 'reason': 'RECOVERY_RECEIPTS_INCOMPLETE',
              'regenerationCount': None, 'regenerations': [], 'decisions': []}
    try:
        rows = trace.get('telemetry')
        events = bundle.get('events')
        if (not isinstance(rows, list) or trace.get('telemetryTotal') != len(rows)
                or not isinstance(events, list) or bundle.get('eventsTotal') != len(events)
                or bundle.get('snapshotConsistent') is not True):
            return result
        run_id = bundle['run']['runId']
        canonical = {}
        for row in events:
            if not isinstance(row, dict) or not isinstance(row.get('eventId'), str):
                raise ValueError()
            previous = canonical.setdefault(row['eventId'], row)
            if previous != row:
                raise ValueError()
        starts, calls, decisions = {}, {}, {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError()
            binding, detail = row.get('binding') or {}, row.get('detail') or {}
            if not isinstance(binding, dict) or not isinstance(detail, dict):
                raise ValueError()
            kind = binding.get('eventType')
            if kind not in ('LLM_MODEL_CALLED', 'LLM_CALL_ROUTED', 'LLM_RECOVERY_DECIDED'):
                continue
            if binding.get('runId') != run_id or binding != canonical.get(binding.get('eventId')):
                raise ValueError()
            identity = tuple(binding.get(key) for key in ('workflowStepId', 'a2aTaskId', 'agentContextId', 'actor'))
            if kind == 'LLM_MODEL_CALLED':
                sequence = detail.get('sequence')
                if type(sequence) is not int or sequence < 1 or sequence in starts:
                    raise ValueError()
                starts[sequence] = identity
            else:
                if not isinstance(detail.get('recovery'), dict) or detail['recovery'].get('policy') != POLICY:
                    raise ValueError()
                record = (CallRoute if kind == 'LLM_CALL_ROUTED' else FailureRoute).model_validate(detail['recovery'])
                target = calls if kind == 'LLM_CALL_ROUTED' else decisions
                if record.sequence in target:
                    raise ValueError()
                target[record.sequence] = (record, identity)
        # Canonical call starts must all have matching telemetry and routing receipts.
        if (len(starts) != sum(row.get('eventType') == 'LLM_MODEL_CALLED' for row in canonical.values())
                or calls.keys() != starts.keys()):
            raise ValueError()
        # Call-start receipts are reservations, not proof that provider dispatch
        # happened: storage/input checks can fail before the provider is entered.
        # A completed/error/canceled usage receipt proves an attempted dispatch;
        # token amounts may still be unknown and are never filled with zero.
        usage = bundle.get('usage') or {}
        if not isinstance(usage, dict) or usage.get('runId') != run_id:
            raise ValueError()
        receipts = usage.get('records')
        if not isinstance(receipts, list) or usage.get('total') != len(receipts):
            raise ValueError()
        measured = set()
        for receipt in receipts:
            if not isinstance(receipt, dict):
                raise ValueError()
            detail = receipt.get('detail') or receipt
            if not isinstance(detail, dict):
                raise ValueError()
            sequence = detail.get('sequence')
            if type(sequence) is not int or sequence in measured:
                raise ValueError()
            binding = receipt.get('binding')
            if (not isinstance(binding, dict) or binding.get('runId') != run_id
                    or tuple(binding.get(key) for key in ('workflowStepId', 'a2aTaskId', 'agentContextId', 'role'))
                       != starts.get(sequence)):
                raise ValueError()
            measured.add(sequence)
        if measured != starts.keys():
            raise ValueError()
        attempts = {}
        for sequence, (call, identity) in sorted(calls.items()):
            root = calls.get(call.operation_sequence)
            if starts[sequence] != identity or root is None or root[0].mode != 'INITIAL' or root[1] != identity:
                raise ValueError()
            if call.mode != 'REGENERATION':
                continue
            decision, source_identity = decisions[call.trigger_sequence]
            if (source_identity != identity or decision.operation_sequence != call.operation_sequence
                    or decision.action != 'REGENERATE' or decision.code != call.code):
                raise ValueError()
            expected = attempts.get(call.operation_sequence, 0) + 1
            if call.correction_attempt != expected:
                raise ValueError()
            attempts[call.operation_sequence] = expected
            result['regenerations'].append({**call.public(), 'workflowStepId': identity[0],
                                            'taskId': identity[1], 'actor': identity[3]})
        for sequence, (decision, identity) in sorted(decisions.items()):
            if sequence not in calls or calls[sequence][1] != identity or calls[sequence][0].operation_sequence != decision.operation_sequence:
                raise ValueError()
            result['decisions'].append({**decision.public(), 'workflowStepId': identity[0],
                                       'taskId': identity[1], 'actor': identity[3]})
        return {**result, 'status': 'PASS', 'reason': None, 'regenerationCount': len(result['regenerations'])}
    except (ValueError, KeyError, TypeError):
        # No optimistic partial count. A budget stop after a correction decision
        # produces no regeneration call and therefore contributes zero retries.
        return {**result, 'regenerations': [], 'decisions': []}
