"""Failure-boundary tests: no model calls; real journal, locks and handoff hashes."""
import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import subprocess
import sys
from uuid import uuid4

import httpx
import pytest

from evaluation import experiment_run as er
from tests.evaluation.test_experiment_run import (
    _approved, _events, _exporter, _handoff, _Host, _matching_plan, _runtime_files,
)


@pytest.fixture
def setup(monkeypatch, tmp_path):
    _runtime_files(tmp_path)
    monkeypatch.setattr(er, 'approved_configuration',
                        lambda plan, architecture, base: _approved(architecture=architecture))
    plan = _matching_plan()
    row = er.schedule(plan)['runs'][0]  # The first registered slot must be submitted first.
    return plan, row, tmp_path / 'results', tmp_path


def test_lost_post_response_leaves_intent_and_never_posts_again(setup):
    plan, row, out, base = setup
    accepted = []

    def handler(request):
        assert request.method == 'POST'
        accepted.append(str(uuid4()))  # the server has already accepted it
        raise httpx.ReadTimeout('response lost', request=request)

    factory = lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(handler))
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_OUTCOME_UNKNOWN'):
        er.run_one(plan, row, out, base, client_factory=factory)
    assert [item['event'] for item in _events(out)] == ['SUBMITTING', 'SUBMISSION_UNCERTAIN']
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_REQUIRES_RECONCILIATION'):
        er.run_one(plan, row, out, base, client_factory=factory)
    assert len(accepted) == 1


def test_journal_failure_before_post_does_not_send_any_request(setup, monkeypatch):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])

    def fail(*args, **kwargs):
        raise OSError('disk full')

    monkeypatch.setattr(er.os, 'replace', fail)
    with pytest.raises(OSError):
        er.run_one(plan, row, out, base, client_factory=host.client)
    assert host.requests == [] and not (out / er.LEDGER).exists()
    assert not list(out.glob('.runs-*'))


def test_atomic_record_failure_preserves_the_previous_journal(setup, monkeypatch):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([]))
    previous = (out / er.LEDGER).read_bytes()

    def fail(*args):
        raise OSError('rename interrupted')

    monkeypatch.setattr(er.os, 'replace', fail)
    with er._run_lock(out), pytest.raises(OSError):
        er._record(out, plan, row, 'ENDED', fingerprint=er._plan_fingerprint(plan, base),
                   runId=host.run_id, status='FINISHED')
    assert (out / er.LEDGER).read_bytes() == previous
    assert not list(out.glob('.runs-*'))


def test_another_process_cannot_enter_the_same_journal(setup):
    plan, row, out, base = setup
    script = (
        'import sys; from pathlib import Path; from evaluation.experiment_run import _run_lock; '
        'ctx=_run_lock(Path(sys.argv[1])); ctx.__enter__(); print("LOCKED",flush=True); sys.stdin.readline()'
    )
    process = subprocess.Popen([sys.executable, '-c', script, str(out)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == 'LOCKED'
        host = _Host(['FINISHED'])
        with pytest.raises(er.ExperimentRunError, match='EXPERIMENT_RUN_BUSY'):
            er.run_one(plan, row, out, base, client_factory=host.client)
        assert host.requests == []
    finally:
        process.communicate('\n', timeout=10)
    assert process.returncode == 0


@pytest.mark.parametrize('field', ['model', 'commit', 'request', 'counts', 'schedule', 'host'])
def test_finished_slot_rejects_plan_changes_before_any_external_action(setup, field):
    plan, row, out, base = setup
    host, calls = _Host(['FINISHED']), []
    er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    changed = copy.deepcopy(plan)
    if field == 'model': changed['model']['modelId'] = 'another-model'
    elif field == 'commit': changed['source']['commitHash'] = 'f' * 40
    elif field == 'request': changed['requestText'] = 'A different task'
    elif field == 'counts': changed['plannedRuns']['MULTI_AGENT'] = 6
    elif field == 'schedule': changed['executionOrder'] = 'RANDOMIZED_RECORDED'
    elif field == 'host': changed['hostConfigurations']['MULTI_AGENT']['api'] = 'http://another-host'
    with pytest.raises(er.ExperimentRunError, match='LEDGER_PLAN_CHANGED|ORDER_NOT_PLANNED'):
        er.run_one(changed, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    assert len(calls) == 1 and sum(method == 'POST' for method, _ in host.requests) == 1


def test_plan_is_bound_across_later_slots_not_only_the_first_slot(setup):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([]))
    changed = copy.deepcopy(plan)
    changed['requestText'] = 'A different task'
    next_row = er.schedule(changed)['runs'][2]
    with pytest.raises(er.ExperimentRunError, match='LEDGER_PLAN_CHANGED'):
        er.run_one(changed, next_row, out, base, client_factory=host.client)
    assert sum(method == 'POST' for method, _ in host.requests) == 1


def test_unbound_legacy_journal_cannot_be_adopted_silently(setup):
    plan, row, out, base = setup
    out.mkdir()
    (out / er.LEDGER).write_text(json.dumps({'experimentId': plan['experimentId'],
        'loopPolicy': plan['loopPolicy'], 'order': row['order'], 'runId': str(uuid4()),
        'architecture': row['architecture'], 'event': 'SUBMITTED'}) + '\n')
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='LEGACY_OR_UNBOUND_LEDGER'):
        er.run_one(plan, row, out, base, client_factory=host.client)
    assert host.requests == []


def test_completed_export_is_recovered_after_last_journal_write_fails(setup, monkeypatch):
    plan, row, out, base = setup
    host, calls = _Host(['FINISHED']), []
    original = er._record

    def interrupted(*args, **kwargs):
        if args[3] == 'EXPORTED':
            raise OSError('interrupted after files were saved')
        return original(*args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(er, '_record', interrupted)
        with pytest.raises(OSError):
            er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    assert [item['event'] for item in _events(out)] == ['SUBMITTING', 'SUBMITTED', 'ENDED']
    again = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    assert again['reusedSubmission'] is True and len(calls) == 1
    assert _events(out)[-1]['event'] == 'EXPORTED' and _events(out)[-1]['recovered'] is True
    assert sum(method == 'POST' for method, _ in host.requests) == 1


def test_partial_export_is_preserved_and_same_run_is_exported_again(setup):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])

    def interrupted(plan, architecture, run_id, folder, base):
        folder.mkdir()
        (folder / 'partial.txt').write_text('keep this evidence')
        raise OSError('interrupted export')

    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=interrupted)
    again = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([]))
    quarantine = list(out.glob('.*.incomplete-*'))
    assert len(quarantine) == 1 and (quarantine[0] / 'partial.txt').read_text() == 'keep this evidence'
    assert again['runId'] == host.run_id and sum(method == 'POST' for method, _ in host.requests) == 1


def test_even_recorded_export_bytes_are_rechecked_on_retry(setup):
    plan, row, out, base = setup
    host, calls = _Host(['FINISHED']), []
    first = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    from pathlib import Path
    (Path(first['export']) / 'bundle.json').write_text('changed after export')
    again = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter(calls))
    assert len(calls) == 2 and again['runId'] == first['runId']
    assert len(list(out.glob('.*.incomplete-*'))) == 1


def test_recovered_export_clears_current_failure_without_erasing_history(setup):
    from evaluation.execution_journal import read_execution_journals
    plan, row, out, base = setup
    host, calls = _Host(['FINISHED']), []
    exporter = _exporter(calls, fail=1)
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=exporter)
    before, _ = read_execution_journals([out])
    assert before[0]['state'] == 'EXPORT_FAILED' and before[0]['reason'] == 'OSError'

    er.run_one(plan, row, out, base, client_factory=host.client, exporter=exporter)
    after, _ = read_execution_journals([out])
    assert after[0]['state'] == 'EXPORTED' and after[0]['reason'] is None
    assert [event['event'] for event in _events(out)][-2:] == ['EXPORT_FAILED', 'EXPORTED']
    assert sum(method == 'POST' for method, _ in host.requests) == 1


def test_reexport_failure_supersedes_old_success_and_recovery_is_recorded(setup):
    from pathlib import Path
    from evaluation.execution_journal import read_execution_journals
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    first = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([]))
    (Path(first['export']) / 'bundle.json').write_text('changed after export')
    calls = []
    exporter = _exporter(calls, fail=1)
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=exporter)
    failed, _ = read_execution_journals([out])
    assert failed[0]['state'] == 'EXPORT_FAILED' and failed[0]['reason'] == 'OSError'

    again = er.run_one(plan, row, out, base, client_factory=host.client, exporter=exporter)
    recovered, _ = read_execution_journals([out])
    assert recovered[0]['state'] == 'EXPORTED' and recovered[0]['reason'] is None
    assert again['runId'] == first['runId'] and again['export'] == first['export']
    assert [event['event'] for event in _events(out)][-3:] == ['EXPORTED', 'EXPORT_FAILED', 'EXPORTED']
    assert len(list(out.glob('.*.incomplete-*'))) == 1
    assert sum(method == 'POST' for method, _ in host.requests) == 1


def test_hash_valid_export_with_wrong_configuration_is_rejected(setup):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])

    def wrong(plan, architecture, run_id, folder, base):
        config = er.submission(plan, _approved(architecture=architecture), row['order'])['configuration']
        config['model']['modelId'] = 'wrong-model'
        return _handoff(folder, plan, architecture, run_id, configuration=config)

    with pytest.raises(er.ExperimentRunError, match='EXPORT_CONFIGURATION_MISMATCH'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=wrong)


@pytest.mark.parametrize('final,versions', [(1, [1]), (2, [2]), (1, [True]), (2, [1, 1])])
def test_core_only_handoff_cannot_hide_missing_snapshot_versions(setup, final, versions):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    config = er.submission(plan, _approved(architecture=row['architecture']), row['order'])['configuration']
    folder = _handoff(base / 'incomplete', plan, row['architecture'], host.run_id, configuration=config)
    documents = {name: json.loads((folder / name).read_text()) for name in ('run.json', 'bundle.json', 'handoff.json')}
    documents['run.json'].update(finalCodeVersion=final, exportedCodeVersions=versions)
    documents['bundle.json']['run']['codeVersion'] = final
    for name in ('run.json', 'bundle.json'):
        data = json.dumps(documents[name]).encode('utf-8')
        (folder / name).write_bytes(data)
        documents['handoff.json']['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'sizeBytes': len(data)}
    (folder / 'handoff.json').write_text(json.dumps(documents['handoff.json']))
    # Every declared byte is valid, but required source/manifest bytes are absent.
    from evaluation.workflow import load_handoff
    load_handoff(folder)
    assert er._verified_export(folder, host.run_id, row['architecture'], 'FINISHED', er._hash(config)) is False


def _lost_then(host, lose_first=True, created=True):
    """첫 POST의 응답을 잃는 Host. created=True면 서버가 이미 접수(기록)한 뒤 응답만 잃은 경우다."""
    state = {'lost': False}

    def handler(request):
        if request.method == 'POST' and lose_first and not state['lost']:
            state['lost'] = True
            if created:
                host.handler(request)  # 서버는 접수했다
            else:
                host.requests.append((request.method, request.url.path))  # 서버에 닿지 않았다
                host.headers.append(dict(request.headers))
            raise httpx.ReadTimeout('response lost', request=request)
        return host.handler(request)
    return lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(handler))


@pytest.mark.parametrize('created', [True, False])
def test_explicit_reconciliation_uses_the_same_idempotency_key_and_never_duplicates(setup, created):
    """응답 유실 뒤 명시적 복구: 접수 기록이 있으면 그 Run을 잇고, 없다고 확인되면 같은 키·같은 본문으로만 다시 보낸다
    (dev 85·86번). 어느 경우든 Host에는 Run이 하나뿐이다."""
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    factory = _lost_then(host, created=created)
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_OUTCOME_UNKNOWN'):
        er.run_one(plan, row, out, base, client_factory=factory)
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_REQUIRES_RECONCILIATION'):
        er.run_one(plan, row, out, base, client_factory=factory)  # 명시적 복구 없이는 다시 보내지 않는다
    result = er.run_one(plan, row, out, base, client_factory=factory, reconcile_submission=True, exporter=_exporter([]))
    assert result['runId'] == host.run_id and result['reusedSubmission'] is True
    events = _events(out)
    intent = next(event for event in events if event['event'] == 'SUBMITTING')
    assert any(event.get('reconciled') is True and event.get('runId') == host.run_id for event in events)
    assert len(host.keys) == 1 and intent['idempotencyKey'] in host.keys  # 키 하나 = Run 하나
    posts = [index for index, (method, _path) in enumerate(host.requests) if method == 'POST']
    assert len(posts) == (1 if created else 2)
    assert {host.headers[index].get('idempotency-key') for index in posts} == {intent['idempotencyKey']}


def test_reconciliation_stops_on_a_conflicting_or_unknown_lookup(setup):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_OUTCOME_UNKNOWN'):
        er.run_one(plan, row, out, base, client_factory=_lost_then(host))
    intent = next(event for event in _events(out) if event['event'] == 'SUBMITTING')
    # 같은 키로 다른 요청이 접수돼 있으면 이어 쓰지 않는다.
    host.keys[intent['idempotencyKey']] = 'f' * 64
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_RECONCILIATION_CONFLICT'):
        er.run_one(plan, row, out, base, client_factory=_lost_then(host, lose_first=False), reconcile_submission=True)
    # 접수 조회 API가 없는 옛 Host(일반 404)면 다시 보내지 않는다.
    def old_host(request):
        host.requests.append((request.method, request.url.path))
        return httpx.Response(404, json={'detail': 'Not Found'})
    before = len(host.requests)
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_RECONCILIATION_UNAVAILABLE'):
        er.run_one(plan, row, out, base, reconcile_submission=True,
                   client_factory=lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(old_host)))
    assert [method for method, _path in host.requests[before:]] == ['GET']


def test_reconciliation_never_resends_a_changed_request(setup, monkeypatch):
    plan, row, out, base = setup
    host = _Host(['FINISHED'])
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_OUTCOME_UNKNOWN'):
        er.run_one(plan, row, out, base, client_factory=_lost_then(host, created=False))
    original = _approved(architecture=row['architecture'])
    changed = original.model_copy(update={'model': original.model.model_copy(update={'model_id': 'changed-model'})})
    monkeypatch.setattr(er, 'approved_configuration', lambda plan, architecture, base: changed)
    before = len(host.requests)
    with pytest.raises(er.ExperimentRunError, match='SUBMISSION_RECONCILIATION_CONFLICT'):
        er.run_one(plan, row, out, base, client_factory=_lost_then(host, lose_first=False), reconcile_submission=True)
    assert [method for method, _path in host.requests[before:]] == ['GET']  # 조회만 하고 보내지 않았다


def test_host_client_reads_only_the_dedicated_token_and_fails_before_any_intent(setup, monkeypatch):
    plan, row, out, base = setup
    monkeypatch.delenv(er.HOST_TOKEN_ENV, raising=False)
    with pytest.raises(er.ExperimentRunError, match='HOST_API_TOKEN_REQUIRED'):
        er.run_one(plan, row, out, base)
    assert not (out / er.LEDGER).exists()  # 토큰이 없으면 제출 의도도 남기지 않는다
    monkeypatch.setenv(er.HOST_TOKEN_ENV, 'fixture-host-token-' + 'x' * 32)
    with er.host_client('http://host') as client:
        assert client.headers['Authorization'] == 'Bearer fixture-host-token-' + 'x' * 32


@pytest.mark.parametrize('kind', ['TIMEOUT', 'STATUS_ERROR', 'UNCERTAIN_POST'])
def test_another_order_cannot_bypass_an_unclosed_submission(setup, kind):
    plan, row, out, base = setup
    accepted = []

    def handler(request):
        if request.method == 'POST':
            run_id = str(uuid4())
            accepted.append(run_id)
            if kind == 'UNCERTAIN_POST':
                raise httpx.ReadTimeout('reply lost', request=request)
            return httpx.Response(201, json={'run': {'runId': run_id}})
        return httpx.Response(503 if kind == 'STATUS_ERROR' else 200, json={'status': 'IMPLEMENTING'})

    factory = lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(handler))
    ticks = iter(range(0, 100_000, 5000))
    if kind == 'TIMEOUT':
        result = er.run_one(plan, row, out, base, client_factory=factory, clock=lambda: next(ticks))
        assert result['status'] == 'TIMEOUT'
    else:
        with pytest.raises(er.ExperimentRunError, match='RUN_STATUS_UNAVAILABLE_503|SUBMISSION_OUTCOME_UNKNOWN'):
            er.run_one(plan, row, out, base, client_factory=factory)
    different_row = er.schedule(plan)['runs'][1]
    with pytest.raises(er.ExperimentRunError, match='PREVIOUS_RUN_NOT_CLOSED'):
        er.run_one(plan, different_row, out, base, client_factory=factory)
    assert len(accepted) == 1


@pytest.mark.parametrize('closed', ['FINISHED', 'ABORTED', 'WAITING_INPUT', 'HUMAN_REVIEW'])
def test_closed_run_with_failed_export_does_not_block_later_slots_or_recovery(setup, closed):
    plan, row, out, base = setup
    host = _Host([closed])
    with pytest.raises(er.ExperimentRunError, match='EXPORT_FAILED'):
        er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([], fail=1))
    next_host = _Host(['FINISHED'])
    different_row = er.schedule(plan)['runs'][1]
    result = er.run_one(plan, different_row, out, base, client_factory=next_host.client, exporter=_exporter([]))
    assert result['status'] == 'FINISHED'
    restored = er.run_one(plan, row, out, base, client_factory=host.client, exporter=_exporter([], status=closed))
    assert restored['runId'] == host.run_id and restored['reusedSubmission'] is True
    assert sum(method == 'POST' for method, _ in host.requests) == 1


def test_all_stops_at_timeout_and_resumes_only_after_the_same_run_closes(setup, monkeypatch, capsys):
    plan, row, out, base = setup
    plan['plannedRuns'] = {'SINGLE_AGENT': 1, 'MULTI_AGENT': 1}
    plan_file = base / 'plan.json'
    plan_file.write_text(json.dumps(plan))
    accepted, status, active_before_submission = [], {}, []

    def handler(request):
        if request.method == 'POST':
            active_before_submission.append(sum(value == 'IMPLEMENTING' for value in status.values()))
            run_id = str(uuid4())
            accepted.append(run_id)
            status[run_id] = 'IMPLEMENTING' if len(accepted) == 1 else 'FINISHED'
            return httpx.Response(201, json={'run': {'runId': run_id}})
        run_id = request.url.path.rsplit('/', 1)[-1]
        return httpx.Response(200, json={'runId': run_id, 'status': status[run_id]})

    factory = lambda api: httpx.Client(base_url=api, transport=httpx.MockTransport(handler))
    original = er.run_one

    def run(*args, **kwargs):
        ticks = iter(range(0, 100_000, 5000))
        return original(*args, **kwargs, client_factory=factory, exporter=_exporter([]), clock=lambda: next(ticks))

    monkeypatch.setattr(er, 'run_one', run)
    arguments = ['--plan', str(plan_file), '--out', str(out)]
    assert er.main(['all', *arguments]) == 1
    captured = capsys.readouterr()
    assert 'RUN_WAIT_TIMEOUT' in captured.err and json.loads(captured.out)['status'] == 'TIMEOUT'
    assert len(accepted) == 1
    # A new CLI invocation for order 2 cannot bypass the unfinished first slot.
    assert er.main(['run', *arguments, '--order', '2']) == 1
    assert 'PREVIOUS_RUN_NOT_CLOSED' in capsys.readouterr().err
    assert len(accepted) == 1
    status[accepted[0]] = 'FINISHED'
    assert er.main(['all', *arguments]) == 0
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert results[0]['runId'] == accepted[0] and results[0]['reusedSubmission'] is True
    assert results[1]['runId'] == accepted[1] and len(accepted) == 2
    assert active_before_submission == [0, 0]

