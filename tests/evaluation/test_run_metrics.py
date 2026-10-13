"""run_metrics.py를 시험하는 파일이다. 모든 데이터는 가짜이다.

확인하는 것:
  1) 시스템의 실행 기록(JSON)이 지표로 올바르게 바뀌는가? (모르는 값은 0이 아니라 None인가?)
  2) 구조별 요약에서 성공률의 분모, 비용·토큰 합계를 올바르게 다루는가?
  3) Single과 Multi를 "공정하게 비교해도 되는지" 올바르게 판정하는가?
"""
from uuid import UUID

import pytest

from evaluation.run_metrics import (comparison_validity, experiment_summary, fetch_run_bundle, run_metrics)


def uid(number):
    """시험용 가짜 UUIDv4. 같은 숫자면 같은 값이다."""
    return str(UUID(int=number, version=4))


def event(run_id, kind, second, **extra):
    """시스템의 "사건 기록" 하나를 흉내 낸다. kind=사건 종류, second=10시 00분 몇 초에 일어났는지."""
    return {'eventId': uid(900 + second), 'runId': run_id, 'eventType': kind, 'actor': 'Orchestrator',
            'attempt': 0, 'occurredAt': f'2026-10-14T10:00:{second:02d}+09:00', **extra}


def bundle(run_id=None, *, verdict='SUCCESS', status='FINISHED', fixes=1, cost='0.5', complete=True,
           architecture='MULTI_AGENT', with_usage=True):
    """한 개발 실행(Run)에 대한 가짜 자료 묶음을 만든다. (실행 정보, 사건 목록, 문제 목록, 도구 시도, 사용량, 설정)
    기본 시나리오: 0초 시작 → 10초 첫 빌드 성공 → 20초 수정 시작 → 30초 판정 → 31초 끝.
    시험마다 인자로 판정·상태·비용·구조 등을 바꿔서 다른 상황을 만든다."""
    run_id = run_id or uid(1)
    events = [event(run_id, 'RUN_STARTED', 0), event(run_id, 'BUILD_PASSED', 10),
              event(run_id, 'FIX_ATTEMPT_STARTED', 20)] if fixes else [event(run_id, 'RUN_STARTED', 0),
                                                                          event(run_id, 'BUILD_PASSED', 10)]
    events += [event(run_id, 'VERDICT_CREATED', 30), event(run_id, 'RUN_FINISHED', 31)]
    return {
        'run': {'runId': run_id, 'scenarioId': uid(5), 'workspaceId': uid(6), 'status': status, 'verdict': verdict,
                'codeVersion': 1 + fixes, 'fixAttempt': fixes, 'terminationReason': None,
                'createdAt': '2026-10-14T10:00:00+09:00', 'updatedAt': '2026-10-14T10:00:31+09:00'},
        'events': events, 'eventsTotal': len(events),
        'issues': [{'runId': run_id, 'reporter': 'SECURITY', 'category': 'SECURITY', 'newCodeVersion': 2},
                   {'runId': run_id, 'reporter': 'QA', 'category': 'FUNCTIONAL', 'newCodeVersion': None}],
        # 도구를 부른 기록 3건. 그중 attempt 번호가 1인 것이 "다시 시도한" 1건이다.
        'toolAttempts': [{'attempt': {'attempt': 0, 'outcome': 'UNVERIFIED'}}, {'attempt': {'attempt': 1, 'outcome': 'PASS'}},
                         {'attempt': {'attempt': 0, 'outcome': 'PASS'}}],
        'usage': ({'runId': run_id, 'summary': {
            'modelCalls': 12, 'toolCalls': 7, 'knownTotalTokens': 1000, 'totalTokens': 1000 if complete else None,
            'costUsd': cost if complete else None, 'knownCostUsd': cost, 'costComplete': complete}} if with_usage else None),
        'configuration': configuration(run_id, architecture),
    }


def configuration(run_id, architecture='MULTI_AGENT', **override):
    """Run Configuration(실행 설정) 응답을 흉내 낸다. override로 일부 설정만 바꿔서 "다른 조건"을 만든다."""
    settings = {
        'experimentId': uid(7), 'architecture': architecture, 'startingCommitHash': 'a' * 40,
        'startingSnapshotSha256': 'b' * 64,
        'model': {'provider': 'p', 'modelId': 'm', 'modelRevision': 'r', 'temperature': 0.0, 'seed': 1},
        'limits': {'maxFixAttempts': 3, 'maxToolRetries': 2, 'runtimeBudgetMs': 600000},
        'environment': {'containerImageDigest': 'sha256:' + 'c' * 64, 'dependencyLockHash': 'sha256:' + 'd' * 64,
                        'hardwareProfile': 'hw', 'networkPolicy': 'DENY', 'allowedHosts': []},
        'protectedTestSuiteRef': 'suite-1', 'scannerProfileRef': 'scan-1', 'warmupState': 'COLD', 'executionOrder': 0,
    }
    settings.update(override)
    return {'runId': run_id, 'scenarioId': uid(5), 'scenarioContractSha256': 'e' * 64, 'configuration': settings}


def test_metrics_use_event_times_and_keep_unknown_as_none():
    """시간은 사건 시각의 차이로 계산한다(첫 빌드 10초, 판정 30초, 끝 31초). 모르는 값(사람 대기 시간)은 None이다."""
    metrics = run_metrics(bundle())
    assert metrics['verdict'] == 'SUCCESS' and metrics['fixAttempts'] == 1
    assert (metrics['firstBuildPassMs'], metrics['verdictMs'], metrics['totalMs']) == (10000, 30000, 31000)
    assert metrics['toolRetries'] == 1 and metrics['toolAttemptRecords'] == 3
    assert metrics['modelCalls'] == 12 and metrics['costUsd'] == '0.5' and metrics['costComplete'] is True
    assert metrics['issues'] == 2 and metrics['issuesFixed'] == 1
    assert metrics['issuesByReporter'] == {'SECURITY': 1, 'QA': 1}
    assert metrics['humanWaitMs'] is None and metrics['notes'] == []


def test_incomplete_cost_is_never_turned_into_zero():
    """비용이 전부 확인되지 않았으면 전체 비용은 None이다. 0원으로 바꾸지 않고, 확인된 부분 합(knownCostUsd)만 따로 보여 준다."""
    metrics = run_metrics(bundle(complete=False))
    assert metrics['costUsd'] is None and metrics['totalTokens'] is None
    assert metrics['knownCostUsd'] == '0.5' and metrics['costComplete'] is False


def test_missing_usage_and_incomplete_events_are_marked():
    """사용량 기록이 없거나 사건 기록이 일부만 왔으면, 해당 값은 None이고 notes에 이유가 남는다."""
    data = bundle(with_usage=False)
    data['eventsTotal'] = len(data['events']) + 5  # "전체는 5건 더 있다"고 시스템이 알려 준 상황
    metrics = run_metrics(data)
    assert metrics['modelCalls'] is None and metrics['costComplete'] is False
    assert metrics['verdictMs'] is None and metrics['totalMs'] is None
    assert {'USAGE_UNAVAILABLE', 'EVENTS_INCOMPLETE'} <= set(metrics['notes'])


def test_unfinished_run_has_no_total_time():
    """아직 끝나지 않은 실행은 전체 시간과 판정까지 걸린 시간이 None이다. (이미 일어난 "첫 빌드 성공"까지의 시간은 계산된다.)"""
    data = bundle(verdict=None, status='VALIDATING')
    data['events'] = [e for e in data['events'] if e['eventType'] not in ('VERDICT_CREATED', 'RUN_FINISHED')]
    data['eventsTotal'] = len(data['events'])
    metrics = run_metrics(data)
    assert metrics['totalMs'] is None and metrics['verdictMs'] is None and metrics['firstBuildPassMs'] == 10000


def test_parts_from_another_run_are_rejected():
    """묶음 안에 다른 실행(Run)의 자료가 섞여 있으면 거부한다. 지표가 엉뚱하게 계산되는 것을 막는다."""
    data = bundle()
    data['issues'][0]['runId'] = uid(99)
    with pytest.raises(ValueError):
        run_metrics(data)
    data = bundle()
    data['configuration']['runId'] = uid(99)
    with pytest.raises(ValueError):
        run_metrics(data)


def test_architecture_is_required_and_can_be_supplied():
    """구조(Single/Multi)를 알 수 없으면 거부한다. 설정에 없을 때는 직접 지정할 수 있다."""
    data = bundle()
    data['configuration'] = None
    with pytest.raises(ValueError):
        run_metrics(data)
    assert run_metrics(data, architecture='SINGLE_AGENT')['architecture'] == 'SINGLE_AGENT'


def test_fetch_pages_events_until_total():
    """사건 목록이 한 번에 다 오지 않아도(여기서는 2건씩) 전체 개수에 닿을 때까지 이어서 받는다.
    진짜 인터넷 요청 대신, 같은 모양으로 답하는 가짜 함수 get을 만들어 넘긴다."""
    data = bundle()
    events = data['events']

    def get(path, query):
        # 요청한 경로에 따라 알맞은 가짜 응답을 돌려준다.
        if path.endswith('/events'):
            start = query['offset']
            return {'runId': uid(1), 'events': events[start:start + 2], 'total': len(events), 'limit': 2, 'offset': start}
        if path.endswith('/issues'):
            return {'runId': uid(1), 'issues': data['issues']}
        if path.endswith('/tool-attempts'):
            return {'runId': uid(1), 'attempts': data['toolAttempts']}
        if path.endswith('/usage'):
            return data['usage']
        if path.endswith('/configuration'):
            return data['configuration']
        return data['run']

    fetched = fetch_run_bundle(get, uid(1))
    assert fetched['events'] == events and fetched['eventsTotal'] == len(events)
    assert run_metrics(fetched) == run_metrics(data)  # 이어 받은 결과로 계산해도 처음부터 다 가진 경우와 같다.


def summary_input():
    """요약 시험에 쓸 Run 3개: 성공(비용 0.5), 실패(비용 0.25), 판정 불가(비용 불완전)."""
    runs = [run_metrics(bundle(uid(1))), run_metrics(bundle(uid(2), verdict='FAIL', cost='0.25')),
            run_metrics(bundle(uid(3), verdict='UNVERIFIED', status='FINISHED', complete=False))]
    return runs


def test_summary_denominator_includes_unstarted_runs():
    """성공률의 분모는 "시작하지 못한 Run까지 포함한 계획 전체"이다. (5개 계획 중 3개만 시작 → 성공 1/5)"""
    result = experiment_summary(summary_input(), {'MULTI_AGENT': 5})['MULTI_AGENT']
    assert (result['plannedRuns'], result['recordedRuns'], result['notStarted']) == (5, 3, 2)
    assert result['successRate'] == 1 / 5 and result['verifiedRate'] == 2 / 5 and result['unverifiedRate'] == 1 / 5
    assert result['successVerdictMs'] == {'n': 1, 'median': 30000, 'min': 30000, 'max': 30000}
    # 시작하지 못한 Run이 있으므로 전체 합계는 확정하지 않고, 확인된 부분의 합만 낸다.
    assert result['costUsd'] is None and result['knownCostUsd'] == '1.25' and result['runsWithUnknownCost'] == 1
    assert result['totalTokens'] is None and result['knownTotalTokens'] == 3000


def test_summary_total_cost_only_when_everything_is_known():
    """계획한 모든 Run의 비용이 확인되었을 때만 전체 비용 합계를 낸다. (0.5 + 0.5 = 1.0)"""
    runs = [run_metrics(bundle(uid(n), cost='0.5')) for n in (1, 2)]
    result = experiment_summary(runs, {'MULTI_AGENT': 2})['MULTI_AGENT']
    assert result['costUsd'] == '1.0' and result['totalTokens'] == 2000


def test_summary_rejects_more_runs_than_planned_and_duplicates():
    """계획보다 Run이 많거나 같은 Run이 두 번 들어 있으면 거부한다."""
    with pytest.raises(ValueError):
        experiment_summary(summary_input(), {'MULTI_AGENT': 2})
    with pytest.raises(ValueError):
        experiment_summary([run_metrics(bundle(uid(1)))] * 2, {'MULTI_AGENT': 5})


def evaluation_report(run_id, architecture='MULTI_AGENT', requirement_rate=1.0, verdict='PASS', missed=False,
                      suite='f' * 64, image='sha256:' + 'c' * 64, lock='sha256:' + 'd' * 64):
    """독립 평가 보고서의 "요약에 필요한 부분만" 흉내 낸 가짜. suite=시험 묶음 해시, image·lock=환경 지문."""
    return {'sourceRunId': run_id, 'architecture': architecture, 'evaluationVerdict': verdict,
            'requiredRequirementPassRate': requirement_rate, 'requiredCheckPassRate': requirement_rate,
            'sourceRun': {'missedDefect': missed}, 'suite': {'sha256': suite},
            'expectedManifest': {'containerImageDigest': image, 'dependencyLockHash': lock}}


def test_summary_joins_independent_evaluation_and_keeps_unmeasured_apart():
    """독립 평가 결과를 Run과 연결해 요약한다. 통과율이 None(측정 못 함)인 보고서는 평균에서 빼고 "측정 못 한 수"로 따로 센다."""
    reports = [evaluation_report(uid(1)), evaluation_report(uid(2), requirement_rate=None, verdict='UNVERIFIED'),
               ]
    result = experiment_summary(summary_input(), {'MULTI_AGENT': 3}, reports)['MULTI_AGENT']['evaluation']
    assert result['evaluatedRuns'] == 2 and result['unevaluatedRuns'] == 1 and result['unmeasuredRuns'] == 1
    assert result['meanRequirementPassRate'] == 1.0
    assert result['evaluationVerdicts'] == {'PASS': 1, 'FAIL': 0, 'UNVERIFIED': 1}


def test_summary_rejects_evaluation_of_unknown_run_or_other_architecture():
    """실험에 없는 Run의 평가이거나, Run과 구조가 다른 평가는 거부한다."""
    with pytest.raises(ValueError):
        experiment_summary(summary_input(), {'MULTI_AGENT': 3}, [evaluation_report(uid(99))])
    with pytest.raises(ValueError):
        experiment_summary(summary_input(), {'MULTI_AGENT': 3}, [evaluation_report(uid(1), 'SINGLE_AGENT')])


def entries(**single_override):
    """비교 시험용 입력: Multi Run 1개와 Single Run 1개. single_override로 Single의 설정만 다르게 만들 수 있다."""
    return [
        {'architecture': 'MULTI_AGENT', 'configuration': configuration(uid(1)), 'evaluation': evaluation_report(uid(1))},
        {'architecture': 'SINGLE_AGENT',
         'configuration': configuration(uid(2), 'SINGLE_AGENT', **single_override),
         'evaluation': evaluation_report(uid(2), 'SINGLE_AGENT')},
    ]


def test_same_conditions_are_comparable():
    """모든 조건이 같으면 비교해도 된다(valid). 위반도 주의도 없다."""
    result = comparison_validity(entries())
    assert result == {'valid': True, 'violations': [], 'warnings': []}


@pytest.mark.parametrize('override,field', [
    ({'model': {'provider': 'p', 'modelId': 'better', 'modelRevision': 'r', 'temperature': 0.0, 'seed': 1}}, 'model.modelId'),
    ({'limits': {'maxFixAttempts': 3, 'maxToolRetries': 2, 'runtimeBudgetMs': 1}}, 'limits.runtimeBudgetMs'),
    ({'startingCommitHash': 'f' * 40}, 'startingCommitHash'),
    ({'protectedTestSuiteRef': 'suite-2'}, 'protectedTestSuiteRef'),
])
def test_different_conditions_invalidate_the_comparison(override, field):
    """한쪽만 더 좋은 모델을 쓰거나, 시간 한도·시작 코드·보호 테스트가 다르면 비교 자체가 무효(INVALID_COMPARISON)이다."""
    result = comparison_validity(entries(**override))
    assert result['valid'] is False
    assert field in {item['field'] for item in result['violations'] if item['severity'] == 'INVALID_COMPARISON'}


def test_environment_digest_difference_marks_run_for_rerun_not_invalid():
    """환경 지문(컨테이너 이미지 digest)만 다르면 비교 전체가 무효가 아니다. 그 Run만 다시 실행하도록(RERUN_RUN) 표시한다."""
    environment = configuration(uid(2))['configuration']['environment'] | {'containerImageDigest': 'sha256:' + '9' * 64}
    result = comparison_validity(entries(environment=environment))
    assert result['valid'] is True
    assert {(item['field'], item['severity']) for item in result['violations']} == {
        ('environment.containerImageDigest', 'RERUN_RUN')}


def test_different_test_suite_hash_invalidates_the_comparison():
    """두 구조가 서로 다른 시험지(시험 묶음 해시가 다름)로 채점되었다면 비교는 무효다."""
    data = entries()
    data[1]['evaluation'] = evaluation_report(uid(2), 'SINGLE_AGENT', suite='0' * 64)
    result = comparison_validity(data)
    assert result['valid'] is False and result['violations'][0]['field'] == 'suite.sha256'


def test_warnings_for_missing_evaluation_order_and_experiment():
    """평가가 없는 Run이 있거나 실행 순서를 기록하지 않았다면, 비교는 가능하지만 "주의"로 알린다."""
    data = entries(executionOrder=None)
    data[0]['evaluation'] = None
    result = comparison_validity(data)
    assert result['valid'] is True
    assert {item['detail'] for item in result['warnings']} == {'SOME_RUNS_HAVE_NO_EVALUATION', 'ORDER_OR_WARMUP_NOT_RECORDED'}


def test_comparison_needs_both_architectures():
    """한 구조의 Run만 있으면 비교할 대상이 없으므로 거부한다."""
    with pytest.raises(ValueError):
        comparison_validity(entries()[:1])


def test_result_does_not_depend_on_input_order_and_checks_every_run():
    """같은 구조의 반복 Run끼리 시험지가 달라도(입력 순서와 상관없이) 비교는 무효다."""
    data = entries()
    data.append({'architecture': 'MULTI_AGENT', 'configuration': configuration(uid(3)),
                 'evaluation': evaluation_report(uid(3), suite='0' * 64)})
    first = comparison_validity(data)
    second = comparison_validity(list(reversed(data)))  # 목록 순서를 거꾸로 뒤집어도 결과가 같아야 한다.
    assert first['valid'] is False and second['valid'] is False
    assert first['violations'] == second['violations']
    assert first['violations'][0]['field'] == 'suite.sha256'
    assert first['violations'][0]['values']['MULTI_AGENT'] == ['0' * 64, 'f' * 64]


def test_repetitions_inside_one_architecture_must_match_too():
    """한 구조의 반복 Run들끼리 설정이 달라도 위반이다(다른 구조와 우연히 같아 보여도)."""
    data = entries()
    data.append({'architecture': 'MULTI_AGENT',
                 'configuration': configuration(uid(3), startingCommitHash='f' * 40),
                 'evaluation': evaluation_report(uid(3))})
    result = comparison_validity(data)
    assert result['valid'] is False
    assert 'startingCommitHash' in {item['field'] for item in result['violations']}
