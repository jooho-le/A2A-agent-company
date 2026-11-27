"""실제 사용량 응답의 누락·중복·페이지 변화가 비용 비교를 낙관적으로 만들지 않는지 확인한다."""
import copy

import pytest

from evaluation.run_metrics import fetch_run_bundle, run_metrics, experiment_summary, token_breakdown
from tests.evaluation.test_experiment import real_report
from tests.evaluation.test_run_metrics import bundle, uid


def usage():
    return {'runId': uid(1), 'total': 2, 'records': [
        {'sequence': 1, 'inputTokens': 60, 'outputTokens': 10, 'cachedInputTokens': 20},
        {'sequence': 2, 'inputTokens': 20, 'outputTokens': 10, 'reasoningTokens': 5}],
        'summary': {'modelCalls': 2, 'usageComplete': True, 'totalTokens': 100,
                    'knownTotalTokens': 100, 'pendingModelSequences': [], 'missingUsageSequences': []}}


def test_complete_breakdown_does_not_double_count_cached_or_reasoning_tokens():
    assert token_breakdown(usage()) == {
        'inputTokens': 80, 'outputTokens': 20, 'knownInputTokens': 80,
        'knownOutputTokens': 20, 'tokenBreakdownComplete': True}


@pytest.mark.parametrize('damage', ['missing_page', 'duplicate', 'unknown_token', 'changed', 'total_mismatch', 'pending'])
def test_incomplete_breakdown_cannot_be_used_as_total(damage):
    data = usage()
    if damage == 'missing_page':
        data['records'].pop()
    elif damage == 'duplicate':
        data['records'][1]['sequence'] = 1
    elif damage == 'unknown_token':
        data['records'][1]['inputTokens'] = None
    elif damage == 'changed':
        data['recordsConsistent'] = False
    elif damage == 'total_mismatch':
        data['summary']['totalTokens'] = 101
    elif damage == 'pending':
        data['summary']['pendingModelSequences'] = [3]
    result = token_breakdown(data)
    assert result['inputTokens'] is result['outputTokens'] is None
    assert result['tokenBreakdownComplete'] is False
    if damage == 'duplicate':
        assert result['knownInputTokens'] is None


def test_unknown_empty_receipts_are_not_zero_but_verified_no_calls_are():
    data = usage()
    data['records'] = []
    assert token_breakdown(data)['knownInputTokens'] is None
    data.update(total=0)
    data['summary'].update(modelCalls=0, totalTokens=0)
    assert token_breakdown(data)['inputTokens'] == 0
    assert token_breakdown(None)['knownInputTokens'] is None


@pytest.mark.parametrize('changed', [False, True])
def test_usage_is_collected_across_pages_and_detects_changes(changed):
    data, receipts, offsets = bundle(), usage(), []

    def get(path, query):
        if path.endswith('/events'):
            return {'events': data['events'][query['offset']:query['offset'] + query['limit']],
                    'total': len(data['events'])}
        if path.endswith('/issues'):
            return {'issues': data['issues']}
        if path.endswith('/tool-attempts'):
            return {'attempts': data['toolAttempts']}
        if path.endswith('/configuration'):
            return data['configuration']
        if path.endswith('/usage'):
            offset = query['offset']
            offsets.append(offset)
            page = copy.deepcopy(receipts)
            page['records'] = page['records'][offset:offset + 1]
            if changed and offset:
                page['summary']['totalTokens'] += 1
            return page
        return data['run']

    result = run_metrics(fetch_run_bundle(get, uid(1)))
    assert offsets == [0, 1]
    assert result['inputTokens'] == (None if changed else 80)


def test_summaries_keep_missing_tokens_unknown_and_count_failed_run_cost():
    first = bundle(uid(1), cost='0.5')
    receipts = usage()
    receipts['summary'] = {**first['usage']['summary'], **receipts['summary']}
    first['usage'].update(receipts)
    second = bundle(uid(2), verdict='FAIL', cost='0.25')
    runs = [run_metrics(first), run_metrics(second)]
    report = real_report(uid(1), 'final')
    second_report = real_report(uid(2), 'final', failed=('SEC-SIGNUP-005',))
    result = experiment_summary(runs, {'MULTI_AGENT': 2}, [report, second_report])['MULTI_AGENT']
    assert experiment_summary(runs, {'MULTI_AGENT': 2}, [report])['MULTI_AGENT']['costPerProductPassUsd'] is None
    assert result['inputTokens'] is None and result['knownInputTokens'] == 80
    assert result['costPerProductPassUsd'] == '0.75'
    assert experiment_summary(runs, {'MULTI_AGENT': 3}, [report])['MULTI_AGENT']['costPerProductPassUsd'] is None
    assert experiment_summary(runs, {'MULTI_AGENT': 2})['MULTI_AGENT']['costPerProductPassUsd'] is None
