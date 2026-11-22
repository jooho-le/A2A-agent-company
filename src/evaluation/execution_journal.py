"""측정 원장의 제출 증거를 비교에 연결한다. 결과가 없다고 미시작을 추정하지 않는다."""
from hashlib import sha256
from pathlib import Path


def read_execution_journals(paths):
    from .experiment_run import ARCHITECTURES, LOOP_POLICIES, LEDGER, _ledger
    slots, run_ids, fingerprints, sources = {}, {}, {}, []
    experiment_requests = {}
    for supplied in paths:
        path = Path(supplied).resolve()
        if path.is_dir():
            path /= LEDGER
        if path.name != LEDGER:
            raise ValueError('EXECUTION_JOURNAL_FILENAME_INVALID')
        before = path.read_bytes()
        states = _ledger(path.parent)
        if path.read_bytes() != before:
            raise ValueError('EXECUTION_JOURNAL_CHANGED')
        sources.append({'sha256': sha256(before).hexdigest(), 'slotCount': len(states)})
        for (experiment, policy, order), state in states.items():
            if (policy not in LOOP_POLICIES or state.get('architecture') not in ARCHITECTURES
                    or type(state.get('repetition')) is not int or state['repetition'] < 1):
                raise ValueError('EXECUTION_JOURNAL_SLOT_INVALID')
            request_digest = state.get('requestTextSha256')
            if request_digest is not None and experiment_requests.setdefault(experiment, request_digest) != request_digest:
                raise ValueError('EXECUTION_JOURNAL_REQUEST_MISMATCH')
            key = experiment, policy, order
            bound = fingerprints.setdefault((experiment, policy), state['planFingerprint'])
            if bound != state['planFingerprint']:
                raise ValueError('EXECUTION_JOURNAL_PLAN_CONFLICT')
            row = {'experimentId': experiment, 'loopPolicy': policy, 'order': order,
                   'architecture': state['architecture'], 'repetition': state['repetition'],
                   'runId': state.get('runId'), 'workflowStatus': state.get('status'),
                   'configurationSha256': state['configurationSha256'],
                   'requestTextSha256': state.get('requestTextSha256'),
                   'state': 'SUBMISSION_UNCONFIRMED' if not state.get('runId') else
                   'EXPORTED' if state.get('export') else 'EXPORT_FAILED' if state.get('exportError') else
                   'ENDED' if state.get('status') not in (None, 'TIMEOUT') else 'SUBMITTED',
                   'reason': state.get('exportError') or ('RUN_WAIT_TIMEOUT' if state.get('status') == 'TIMEOUT' else None)}
            if key in slots and slots[key] != row:
                raise ValueError('EXECUTION_JOURNAL_SLOT_CONFLICT')
            if row['runId'] and run_ids.setdefault(row['runId'], key) != key:
                raise ValueError('EXECUTION_JOURNAL_DUPLICATE_RUN')
            slots[key] = row
    return list(slots.values()), sources


def validate_bindings(slots, entries):
    """원장이 다른 실험이나 설정의 결과에 붙지 않도록 실행 선언을 대조한다."""
    from .experiment_run import _hash
    from orchestrator.domain.run_configuration import RunConfiguration
    by_id = {row['configuration']['runId']: row for row in entries}
    experiments = {row['experimentId'] for row in slots}
    if len(experiments) > 1:
        raise ValueError('EXECUTION_JOURNAL_EXPERIMENT_MISMATCH')
    for entry in entries:
        if experiments and entry['configuration']['configuration'].get('experimentId') not in experiments:
            raise ValueError('EXECUTION_JOURNAL_EXPERIMENT_MISMATCH')
        config = entry['configuration']['configuration']
        # A different Run cannot fill an already bound scheduled slot. Joining
        # only by runId would count the original as missing and its replacement
        # as another attempt, silently consuming two slots in the denominator.
        for slot in slots:
            if (config.get('experimentId') == slot['experimentId']
                    and entry['loopPolicy'] == slot['loopPolicy']
                    and config.get('executionOrder') == slot['order']
                    and entry['configuration']['runId'] != slot['runId']):
                raise ValueError('EXECUTION_JOURNAL_SLOT_RUN_MISMATCH')
    for slot in slots:
        entry = by_id.get(slot['runId'])
        if entry is None:
            continue
        if slot.get('requestTextSha256') is not None and slot['requestTextSha256'] != entry.get('requestTextSha256'):
            raise ValueError('EXECUTION_JOURNAL_REQUEST_MISMATCH')
        config = RunConfiguration.model_validate(entry['configuration']['configuration'])
        if (entry['architecture'] != slot['architecture'] or entry['loopPolicy'] != slot['loopPolicy']
                or config.execution_order != slot['order']
                or _hash(config.model_dump(mode='json', by_alias=True)) != slot['configurationSha256']):
            raise ValueError('EXECUTION_JOURNAL_RUN_MISMATCH')
