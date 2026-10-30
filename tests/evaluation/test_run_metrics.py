"""run_metrics.py를 시험하는 파일이다. 협업 시간 시험 하나(dev 파이프라인 Run 표본)를 빼면 모든 데이터는 가짜이다.

확인하는 것:
  1) 시스템의 실행 기록(JSON)이 지표로 올바르게 바뀌는가? (모르는 값은 0이 아니라 None인가?)
  2) 구조별 요약에서 성공률의 분모, 비용·토큰 합계를 올바르게 다루는가?
  3) Single과 Multi를 "공정하게 비교해도 되는지" 올바르게 판정하는가?
  4) 협업 시간(전달·병렬 겹침·에이전트 밖 시간·사람 대기)을 병렬 구간을 두 번 세지 않고 계산하는가?
  5) Ver1(순환 안 보호 테스트 + 최종)과 Ver2(최종만)를 같은 Multi 구조에서 비교할 때, 의도한 차이(보호 테스트 참조)만
     허용하고 이름표·쌍·시험지를 확인하는가? 정책별 점수와 연습 효과(차이의 차이)를 계산하는가?
"""
import json
from uuid import UUID

import pytest

from evaluation.run_metrics import (comparison_validity, experiment_summary, fetch_run_bundle, loop_policy_effect,
                                    run_metrics)


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
        # 첫 문제: 수정을 요청했고(newCodeVersion=2) 다시 검증해서 통과(PASS)함. 둘째 문제: 수정을 요청하지 않음.
        'issues': [{'runId': run_id, 'reporter': 'SECURITY', 'category': 'SECURITY', 'newCodeVersion': 2,
                    'revalidationResult': 'PASS'},
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
    assert metrics['issues'] == 2 and metrics['issuesFixRequested'] == 1 and metrics['issuesFixed'] == 1
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
                      suite='f' * 64, image='sha256:' + 'c' * 64, lock='sha256:' + 'd' * 64, code_version=2):
    """독립 평가 보고서의 "요약에 필요한 부분만" 흉내 낸 가짜. suite=시험 묶음 해시, image·lock=환경 지문,
    code_version=보고서가 채점한 코드 버전(시험용 Run은 수정을 1번 해서 버전 2다)."""
    return {'sourceRunId': run_id, 'architecture': architecture, 'evaluationVerdict': verdict,
            'requiredRequirementPassRate': requirement_rate, 'requiredCheckPassRate': requirement_rate,
            'sourceRun': {'missedDefect': missed}, 'suite': {'sha256': suite},
            'expectedManifest': {'codeVersion': code_version, 'containerImageDigest': image, 'dependencyLockHash': lock}}


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
    """환경 지문(컨테이너 이미지 digest)만 다르면 비교 전체가 무효가 아니다. 그 Run만 다시 실행하도록(RERUN_RUN) 표시한다.

    실제 흐름에서는 평가 보고서의 환경 지문이 그 Run의 동결된 설정에서 나온다. 그래서 Single의 설정을 바꾸면
    Single의 평가 보고서도 같은 값을 적는다. 그 결과 "설정끼리 다름"과 "보고서끼리 다름" 두 위반이 함께 나온다."""
    digest = 'sha256:' + '9' * 64
    environment = configuration(uid(2))['configuration']['environment'] | {'containerImageDigest': digest}
    data = entries(environment=environment)
    data[1]['evaluation'] = evaluation_report(uid(2), 'SINGLE_AGENT', image=digest)
    result = comparison_validity(data)
    assert result['valid'] is True
    assert {(item['field'], item['severity']) for item in result['violations']} == {
        ('environment.containerImageDigest', 'RERUN_RUN'), ('evaluation.containerImageDigest', 'RERUN_RUN')}


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


# ---------------------------------------------------------------------------------------------
# 외부 점검(2026-10-14)에서 찾은 "공정한 비교인지 확인하지 못하던" 사례들이다. 다시 놓치지 않도록 정식 시험으로 고정한다.
# ---------------------------------------------------------------------------------------------
def test_metrics_must_not_override_the_frozen_architecture():
    """실행을 시작할 때 동결한 구조(Multi)를 인자로 Single이라고 바꿔 기록하면 안 된다. 거짓 결과가 되기 때문이다."""
    with pytest.raises(ValueError):
        run_metrics(bundle(), architecture='SINGLE_AGENT')
    # 같은 값을 다시 적는 것은 문제없다.
    assert run_metrics(bundle(), architecture='MULTI_AGENT')['architecture'] == 'MULTI_AGENT'


def test_summary_rejects_runs_of_an_unplanned_architecture():
    """계획에 없는 구조의 Run이 섞여 있으면 거부한다. 그냥 두면 그 Run의 성공·실패·비용이 요약에서 사라진다."""
    with pytest.raises(ValueError):
        experiment_summary([run_metrics(bundle(architecture='SINGLE_AGENT'))], {'MULTI_AGENT': 1})


def test_comparison_rejects_a_mislabeled_architecture():
    """이름표는 Single인데 동결한 설정이 Multi이면, 무엇을 비교하는지 알 수 없으므로 비교는 무효다."""
    data = entries()
    data[1]['configuration']['configuration']['architecture'] = 'MULTI_AGENT'
    result = comparison_validity(data)
    assert result['valid'] is False
    assert 'architecture' in {item['field'] for item in result['violations']}


def test_comparison_rejects_an_evaluation_of_another_run():
    """다른 Run의 평가 보고서(sourceRunId가 다름)를 붙이면, 점수가 엉뚱한 실행의 것이므로 비교는 무효다."""
    data = entries()
    data[1]['evaluation']['sourceRunId'] = '00000000-0000-4000-8000-000000000099'
    result = comparison_validity(data)
    assert result['valid'] is False
    assert 'evaluation.sourceRunId' in {item['field'] for item in result['violations']}


def test_comparison_rejects_an_evaluation_of_another_architecture():
    """평가 보고서의 구조 표시가 Run의 구조와 다르면 비교는 무효다."""
    data = entries()
    data[1]['evaluation']['architecture'] = 'MULTI_AGENT'
    assert comparison_validity(data)['valid'] is False


def test_empty_baselines_are_not_comparable():
    """양쪽 설정이 모두 비어 있다고 "같은 조건"은 아니다. 확인된 조건이 없으므로 비교할 수 없다."""
    data = [{'architecture': name, 'configuration': {}, 'evaluation': None}
            for name in ('SINGLE_AGENT', 'MULTI_AGENT')]
    result = comparison_validity(data)
    assert result['valid'] is False
    assert any(item['field'] == 'missing required conditions' for item in result['violations'])


def test_optional_conditions_may_be_absent():
    """모델 버전·seed·허용 주소처럼 원래 없을 수 있는 항목이 비어 있는 것은 비교를 막지 않는다."""
    data = entries()
    for item in data:
        settings = item['configuration']['configuration']
        settings['model'] = {**settings['model'], 'modelRevision': None, 'seed': None}
        settings['environment'] = {**settings['environment'], 'allowedHosts': None}
    assert comparison_validity(data)['valid'] is True


def test_missing_frozen_architecture_is_only_a_warning():
    """동결 설정에 구조가 적혀 있지 않으면(예: Single 실행기) 비교를 막지는 않고 주의로 알린다."""
    data = entries()
    del data[1]['configuration']['configuration']['architecture']
    result = comparison_validity(data)
    assert result['valid'] is True
    assert 'ARCHITECTURE_NOT_FROZEN' in {item['detail'] for item in result['warnings']}


# ---------------------------------------------------------------------------------------------
# 외부 점검(2026-10-15)에서 찾은 사례: 평가 보고서의 환경 지문이 그 Run이 동결한 환경과 다른데도 통과하던 문제.
# 두 구조의 보고서가 "서로 같은 값"을 적으면 보고서끼리 비교하는 기존 검사로는 드러나지 않는다.
# ---------------------------------------------------------------------------------------------
def violation_for(result, field):
    """결과에서 field 이름의 위반을 찾아 돌려준다. 없으면 None."""
    return next((item for item in result['violations'] if item['field'] == field), None)


def test_report_environment_must_match_its_frozen_configuration():
    """두 보고서가 똑같이 다른 컨테이너 이미지를 적어도(서로는 일치), Run이 동결한 환경과 다르면 RERUN_RUN이다.
    환경 문제는 비교 전체를 무효로 만들지 않는다(valid는 True). 해당 Run만 다시 평가하면 된다."""
    data = entries()
    for item in data:
        item['evaluation']['expectedManifest']['containerImageDigest'] = 'sha256:' + '9' * 64
    result = comparison_validity(data)
    found = violation_for(result, 'evaluation.environment')
    assert found is not None and found['severity'] == 'RERUN_RUN'
    assert set(found['values']) == {uid(1), uid(2)}  # 두 Run 모두 표시된다.
    assert found['values'][uid(1)]['containerImageDigest'] == {
        'frozen': 'sha256:' + 'c' * 64, 'evaluation': 'sha256:' + '9' * 64}
    assert result['valid'] is True


def test_only_the_run_with_a_different_dependency_lock_is_marked():
    """의존성 잠금 해시가 다른 보고서가 하나뿐이면 그 Run만 표시한다. 환경이 맞는 Run은 건드리지 않는다."""
    data = entries()
    data[1]['evaluation']['expectedManifest']['dependencyLockHash'] = 'sha256:' + '8' * 64
    found = violation_for(comparison_validity(data), 'evaluation.environment')
    assert found is not None and set(found['values']) == {uid(2)}
    assert set(found['values'][uid(2)]) == {'dependencyLockHash'}


def test_report_manifest_check_failure_marks_the_run_for_rerun():
    """보고서가 스스로 "결과가 나온 코드·환경이 기준과 다르다"(metadataMatches가 False)고 밝힌 Run은 다시 평가해야 한다.
    그 보고서의 모든 검사는 NOT_RUN이라 비교에 쓸 측정값이 없다. 반대로 True이면 아무 문제가 없다."""
    data = entries()
    data[0]['evaluation']['manifestCheck'] = {'errorCode': None, 'mismatchedFields': [], 'metadataMatches': True}
    data[1]['evaluation']['manifestCheck'] = {
        'errorCode': 'MANIFEST_MISMATCH', 'mismatchedFields': ['snapshot_sha256'], 'metadataMatches': False}
    result = comparison_validity(data)
    found = violation_for(result, 'evaluation.manifestCheck')
    assert found is not None and found['severity'] == 'RERUN_RUN'
    assert found['values'] == {uid(2): {'errorCode': 'MANIFEST_MISMATCH', 'mismatchedFields': ['snapshot_sha256']}}
    assert result['valid'] is True


def test_report_without_expected_manifest_is_a_violation_not_a_crash():
    """보고서에 기준 신분증(expectedManifest)이 아예 없어도 프로그램이 멈추지 않고, 환경을 확인할 수 없는 Run으로 표시한다."""
    data = entries()
    del data[1]['evaluation']['expectedManifest']
    found = violation_for(comparison_validity(data), 'evaluation.environment')
    assert found is not None and set(found['values']) == {uid(2)}


# ---------------------------------------------------------------------------------------------
# 외부 점검(2026-10-16)에서 찾은 사례: 수정 요청을 해결로 셈, 이전 코드의 평가를 최종 평가로 집계, 준비 상태 차이를 놓침.
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize('outcome', [None, 'FAIL', 'UNVERIFIED'])
def test_requested_fix_is_not_confirmed_fixed(outcome):
    """수정을 요청한 것(newCodeVersion)만으로는 해결이 아니다. 다시 검증한 결과가 없거나 FAIL·UNVERIFIED이면 해결로 세지 않는다."""
    data = bundle()
    data['issues'][0]['revalidationResult'] = outcome
    metrics = run_metrics(data)
    assert metrics['issuesFixed'] == 0 and metrics['issuesFixRequested'] == 1


def test_old_code_report_is_not_counted_as_final_code_evaluation():
    """Run의 최종 코드는 버전 2인데 버전 1을 채점한 보고서는 최종 평가가 아니다. 집계에 넣지 않고 거부한다."""
    runs = [run_metrics(bundle(uid(1)))]
    assert runs[0]['codeVersion'] == 2
    with pytest.raises(ValueError):
        experiment_summary(runs, {'MULTI_AGENT': 1}, [evaluation_report(uid(1), code_version=1)])
    # 버전이 같으면 받는다.
    assert experiment_summary(runs, {'MULTI_AGENT': 1}, [evaluation_report(uid(1), code_version=2)])


def test_report_without_a_code_version_cannot_be_tied_to_the_final_code():
    """보고서에 코드 버전이 없거나, Run의 코드 버전을 알 수 없으면 최종 코드의 평가라고 확인할 수 없으므로 거부한다."""
    runs = [run_metrics(bundle(uid(1)))]
    report = evaluation_report(uid(1))
    del report['expectedManifest']['codeVersion']
    with pytest.raises(ValueError):
        experiment_summary(runs, {'MULTI_AGENT': 1}, [report])
    unknown = dict(runs[0], codeVersion=None)
    with pytest.raises(ValueError):
        experiment_summary([unknown], {'MULTI_AGENT': 1}, [evaluation_report(uid(1))])


def test_warm_and_cold_runs_are_not_silently_equivalent():
    """한쪽만 WARM이고 다른 쪽은 COLD이면, 처리시간·비용 비교에 캐시·초기화 비용의 차이가 섞인다. 비교는 막지 않지만 주의로 알린다."""
    result = comparison_validity(entries(warmupState='WARM'))
    assert result['valid'] is True
    warning = next(item for item in result['warnings'] if item['detail'] == 'WARMUP_STATE_DIFFERS')
    assert warning['values'] == {'MULTI_AGENT': ['COLD'], 'SINGLE_AGENT': ['WARM']}


def test_same_warmup_state_gives_no_warning_and_execution_order_may_differ():
    """준비 상태가 모두 같으면 주의가 없다. 실행 순서 숫자는 Run마다 달라야 정상이라 비교 조건이 아니다."""
    data = entries(executionOrder=1)
    assert comparison_validity(data) == {'valid': True, 'violations': [], 'warnings': []}


@pytest.mark.parametrize('source_summary', [None, {'missedDefect': False, 'finalVerdict': 'FAIL'}])
def test_missed_defects_use_actual_run_even_without_a_matching_optional_summary(source_summary):
    # 선택 요약을 생략하거나 그 요약의 판정이 오래되어도 실제 Run의 SUCCESS와 확인된 실패가 기준이다.
    metric = run_metrics(bundle())
    report = evaluation_report(metric['runId'], verdict='UNVERIFIED', requirement_rate=0.0)
    report['sourceRun'] = source_summary
    report['confirmedFailureIds'] = [uid(100)]
    result = experiment_summary([metric], {'MULTI_AGENT': 1}, [report])['MULTI_AGENT']['evaluation']
    assert result['missedDefects'] == 1


@pytest.mark.parametrize('verdict, failures', [('FAIL', [uid(100)]), ('SUCCESS', [])])
def test_claimed_missed_defect_cannot_override_actual_run_and_confirmed_failures(verdict, failures):
    # 실제 성공이 없거나 필수 실패가 확인되지 않았다면 선택 요약에 True라고 적혀 있어도 놓친 결함이 아니다.
    metric = run_metrics(bundle(verdict=verdict))
    report = evaluation_report(metric['runId'], verdict='FAIL', missed=True)
    report['sourceRun']['finalVerdict'] = 'SUCCESS'
    report['confirmedFailureIds'] = failures
    result = experiment_summary([metric], {'MULTI_AGENT': 1}, [report])['MULTI_AGENT']['evaluation']
    assert result['missedDefects'] == 0


@pytest.mark.parametrize('measured, planned, total, known, complete', [
    ([None], 1, None, None, False),
    ([12, None], 2, None, 12, False),
    ([12], 2, None, 12, False),
    ([12, 8], 2, 20, 20, True),
    ([0], 1, 0, 0, True),
    ([], 1, None, None, False),
])
def test_model_calls_distinguish_complete_partial_unknown_and_zero(measured, planned, total, known, complete):
    # 확인한 부분 합과 전체 합계를 분리하고, 기록이 전혀 없는 상태를 실제 0회와 구분한다.
    runs = []
    for index, calls in enumerate(measured):
        data = bundle(uid(index + 1), with_usage=calls is not None)
        if calls is not None:
            data['usage']['summary']['modelCalls'] = calls
        runs.append(run_metrics(data))
    result = experiment_summary(runs, {'MULTI_AGENT': planned})['MULTI_AGENT']
    assert result['modelCalls'] == total
    assert result['knownModelCalls'] == known
    assert result['modelCallsComplete'] is complete
    assert result['runsWithoutUsage'] == measured.count(None)


@pytest.mark.parametrize('measured, planned, total, known', [
    ([None, None], 2, None, None),
    ([1000, None], 2, None, 1000),
    ([1000, 500], 2, 1500, 1500),
    ([0], 1, 0, 0),
])
def test_tokens_keep_unknown_as_none_instead_of_zero(measured, planned, total, known):
    # 토큰도 모델 호출 수·비용과 같은 기준이다. 한 Run도 사용량을 모르면 확인된 합계는 0이 아니라 None이다(원칙 1).
    runs = []
    for index, tokens in enumerate(measured):
        data = bundle(uid(index + 1), with_usage=tokens is not None)
        if tokens is not None:
            data['usage']['summary']['knownTotalTokens'] = data['usage']['summary']['totalTokens'] = tokens
        runs.append(run_metrics(data))
    result = experiment_summary(runs, {'MULTI_AGENT': planned})['MULTI_AGENT']
    assert result['totalTokens'] == total
    assert result['knownTotalTokens'] == known


def test_unknown_event_total_does_not_claim_complete_timing():
    # 시작·끝이 목록에 있어도 전체 개수를 모르면 더 이른 시작이나 마지막 판정이 빠졌을 수 있다.
    data = bundle()
    del data['eventsTotal']
    metrics = run_metrics(data)
    assert metrics['firstBuildPassMs'] is None
    assert metrics['verdictMs'] is None and metrics['totalMs'] is None
    assert {'EVENTS_COMPLETENESS_UNKNOWN', 'RUN_START_UNKNOWN'} <= set(metrics['notes'])


def test_fetch_empty_page_keeps_partial_records_and_marks_timings_unknown():
    # API가 total보다 일찍 빈 페이지를 주면 받은 자료는 보존하되 완전한 기록이라고 주장하지 않는다.
    data = bundle()

    def get(path, query):
        if path.endswith('/events'):
            return {'events': data['events'][:1] if query['offset'] == 0 else [],
                    'total': len(data['events'])}
        if path.endswith('/issues'):
            return {'issues': data['issues']}
        if path.endswith('/tool-attempts'):
            return {'attempts': data['toolAttempts']}
        if path.endswith('/usage'):
            return data['usage']
        if path.endswith('/configuration'):
            return data['configuration']
        return data['run']

    fetched = fetch_run_bundle(get, data['run']['runId'])
    assert fetched['events'] == data['events'][:1]
    metrics = run_metrics(fetched)
    assert 'EVENTS_INCOMPLETE' in metrics['notes']
    assert metrics['firstBuildPassMs'] is None and metrics['totalMs'] is None


def test_fetch_page_error_is_propagated_instead_of_returning_partial_bundle():
    # get의 실패는 빈 페이지와 다르다. 호출자는 실패를 확인하고 재수집해야 한다.
    data = bundle()

    def get(path, query):
        if query['offset'] == 0:
            return {'events': data['events'][:1], 'total': len(data['events'])}
        raise RuntimeError('page request failed')

    with pytest.raises(RuntimeError, match='page request failed'):
        fetch_run_bundle(get, data['run']['runId'])


# ---------------------------------------------------------------- 협업 시간(D4)

def test_collaboration_times_on_real_dev_run():
    """dev 파이프라인 실제 Run: QA·Security 병렬 구간은 한 번만 세고, 에이전트가 일한 시간은 전체 경과 시간을 넘지 않는다."""
    from pathlib import Path
    from evaluation.run_metrics import collaboration_metrics
    runs = json.loads((Path(__file__).parent / 'fixtures/dev_pipeline_timing.json').read_text(encoding='utf-8'))['runs']
    fixed = collaboration_metrics(runs['qa_failure_actual_fix_and_revalidation_preserve_all_lineages_and_shared_budget'])
    assert fixed['stepsByRole']['DEVELOPER']['steps'] == 2 and fixed['stepsByRole']['QA']['steps'] == 2
    assert 0 < fixed['agentActiveMs'] <= fixed['wallToVerdictMs'] and fixed['parallelOverlapMs'] > 0
    assert fixed['outsideAgentMs'] == fixed['wallToVerdictMs'] - fixed['agentActiveMs']
    assert fixed['handoffMs'] > 0 and fixed['llm']['DEVELOPER']['calls'] > 0 and fixed['stepsWithoutEnd'] == 0
    assert (fixed['humanWaitMs'], fixed['humanWaitOpen']) == (0, False)
    waiting = collaboration_metrics(runs['repeated_issue_stops_after_two_unsolved_fix_cycles'])
    assert (waiting['humanWaitMs'], waiting['humanWaitOpen']) == (0, True)  # HUMAN_REVIEW에서 아직 끝나지 않은 대기


def stamp(seconds):
    return f'2026-10-14T00:00:{seconds:02d}+00:00'


def test_parallel_steps_and_closed_human_wait():
    from evaluation.run_metrics import collaboration_metrics
    events = [
        {'eventType': 'RUN_STARTED', 'occurredAt': stamp(0)},
        {'eventType': 'A2A_MESSAGE_SENT', 'workflowStepId': 'qa', 'occurredAt': stamp(1)},
        {'eventType': 'A2A_TASK_RECEIVED', 'workflowStepId': 'qa', 'occurredAt': stamp(2)},
        {'eventType': 'A2A_MESSAGE_SENT', 'workflowStepId': 'sec', 'occurredAt': stamp(1)},
        {'eventType': 'A2A_TASK_RECEIVED', 'workflowStepId': 'sec', 'occurredAt': stamp(3)},
        {'eventType': 'LLM_CALL_FINISHED', 'workflowStepId': 'qa', 'actor': 'QA', 'durationMs': 4000, 'occurredAt': stamp(6)},
        {'eventType': 'A2A_TASK_STATE_CHANGED', 'workflowStepId': 'qa', 'a2aTaskState': 'TASK_STATE_COMPLETED', 'occurredAt': stamp(11)},
        {'eventType': 'MCP_TOOL_FINISHED', 'workflowStepId': 'sec', 'actor': 'SECURITY', 'durationMs': 500, 'occurredAt': stamp(7)},
        {'eventType': 'A2A_TASK_STATE_CHANGED', 'workflowStepId': 'sec', 'a2aTaskState': 'TASK_STATE_COMPLETED', 'occurredAt': stamp(9)},
        {'eventType': 'WORKFLOW_STATE_CHANGED', 'workflowState': 'HUMAN_REVIEW', 'occurredAt': stamp(12)},
        {'eventType': 'WORKFLOW_STATE_CHANGED', 'workflowState': 'REVALIDATING', 'occurredAt': stamp(20)},
        {'eventType': 'A2A_MESSAGE_SENT', 'workflowStepId': 'stuck', 'occurredAt': stamp(21)},
        {'eventType': 'VERDICT_CREATED', 'occurredAt': stamp(25)},
    ]
    result = collaboration_metrics(events)
    assert result['agentStepSumMs'] == 18000 and result['agentActiveMs'] == 10000 and result['parallelOverlapMs'] == 8000
    assert result['wallToVerdictMs'] == 25000 and result['outsideAgentMs'] == 15000
    assert result['handoffMs'] == 3000 and result['humanWaitMs'] == 8000 and result['humanWaitOpen'] is False
    assert result['stepsWithoutEnd'] == 1 and result['llm']['QA'] == {'calls': 1, 'ms': 4000}
    assert result['tools']['SECURITY'] == {'calls': 1, 'ms': 500} and result['stepsByRole']['QA']['steps'] == 1


# ---------------------------------------------------------------- 외부 점검(2026-10-29) F2·F3: Step 구간 끊기

def a2a(step, kind, second, state=None, attempt=0):
    """Step 하나의 A2A 사건(dev a2a_tasks.py가 남기는 모양). state는 'COMPLETED'처럼 TASK_STATE_ 뒤 이름만 쓴다."""
    event = {'eventType': kind, 'workflowStepId': step, 'actor': 'ORCHESTRATOR', 'attempt': attempt,
             'occurredAt': stamp(second)}
    if state:
        event['a2aTaskState'] = 'TASK_STATE_' + state
    return event


def run_events(*events, verdict=None):
    """RUN_STARTED(0초) + 사건들 + (verdict초에) VERDICT_CREATED"""
    tail = [{'eventType': 'VERDICT_CREATED', 'occurredAt': stamp(verdict)}] if verdict is not None else []
    return [{'eventType': 'RUN_STARTED', 'occurredAt': stamp(0)}, *events, *tail]


def wait(second, state):
    """Run 상태 변화 사건(사람 대기 HUMAN_REVIEW·WAITING_INPUT 판단용)"""
    return {'eventType': 'WORKFLOW_STATE_CHANGED', 'workflowState': state, 'occurredAt': stamp(second)}


@pytest.mark.parametrize('state', ['COMPLETED', 'FAILED', 'REJECTED', 'CANCELED', 'INPUT_REQUIRED', 'AUTH_REQUIRED'])
def test_task_finished_in_first_response_ends_the_step(state):
    """F2: 첫 응답(A2A_TASK_RECEIVED)이 이미 끝·중단 상태면 dev는 더 폴링하지 않는다. 그 시각을 Step의 끝으로 본다."""
    from evaluation.run_metrics import collaboration_metrics
    result = collaboration_metrics(run_events(
        a2a('qa', 'WORKFLOW_STEP_DISPATCH_STARTED', 1), a2a('qa', 'A2A_MESSAGE_SENT', 2),
        a2a('qa', 'A2A_TASK_RECEIVED', 4, state), verdict=5))
    assert (result['agentActiveMs'], result['stepsWithoutEnd'], result['outsideAgentMs']) == (3000, 0, 2000)
    assert result['handoffMs'] == 2000


@pytest.mark.parametrize('interrupt', ['INPUT_REQUIRED', 'AUTH_REQUIRED'])
def test_resumed_step_does_not_count_the_human_wait_as_agent_time(interrupt):
    """F3: 입력·인증을 기다리는 동안(3~13초)은 에이전트가 일한 시간이 아니다. 재개(attempt+1)는 새 구간이다.
    활동 1~3초 + 13~16초 = 5초, 사람 대기 10초, 에이전트 밖 17 - 5 = 12초."""
    from evaluation.run_metrics import collaboration_metrics
    result = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1, attempt=1), a2a('qa', 'A2A_TASK_RECEIVED', 2, 'WORKING', 1),
        a2a('qa', 'A2A_TASK_STATE_CHANGED', 3, interrupt, 1), wait(3, 'WAITING_INPUT'),
        wait(13, 'VALIDATING'), a2a('qa', 'A2A_MESSAGE_SENT', 13, attempt=2),
        a2a('qa', 'A2A_TASK_STATE_CHANGED', 14, 'WORKING', 2), a2a('qa', 'A2A_TASK_STATE_CHANGED', 16, 'COMPLETED', 2),
        verdict=17))
    assert (result['agentActiveMs'], result['agentStepSumMs'], result['outsideAgentMs']) == (5000, 5000, 12000)
    assert (result['humanWaitMs'], result['humanWaitOpen'], result['stepsWithoutEnd']) == (10000, False, 0)
    assert result['stepsByRole'] == {'UNKNOWN': {'steps': 1, 'ms': 5000}}


def test_other_agent_working_during_the_wait_is_still_agent_time():
    """QA가 사람을 기다리는 동안(3~13초) Security가 5~10초에 일했으면 그 5초는 에이전트 시간이다.
    (사람 대기를 전체에서 무조건 빼면 틀린다.) 활동 = QA 2초 + Security 5초 + QA 3초 = 10초."""
    from evaluation.run_metrics import collaboration_metrics
    result = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1, attempt=1), a2a('qa', 'A2A_TASK_STATE_CHANGED', 3, 'INPUT_REQUIRED', 1),
        wait(3, 'WAITING_INPUT'), a2a('sec', 'A2A_MESSAGE_SENT', 5), a2a('sec', 'A2A_TASK_RECEIVED', 6, 'WORKING'),
        a2a('sec', 'A2A_TASK_STATE_CHANGED', 10, 'COMPLETED'), wait(13, 'VALIDATING'),
        a2a('qa', 'A2A_MESSAGE_SENT', 13, attempt=2), a2a('qa', 'A2A_TASK_STATE_CHANGED', 16, 'COMPLETED', 2),
        verdict=17))
    assert (result['agentActiveMs'], result['parallelOverlapMs'], result['outsideAgentMs']) == (10000, 0, 7000)
    assert result['humanWaitMs'] == 10000


def test_open_wait_and_polling_timeout():
    """아직 재개되지 않은 대기: 중단 시각까지가 에이전트 시간이고 Step은 끝난 것으로 센다(사람 대기는 열림).
    폴링 시간 초과(A2A_POLL_TIMED_OUT)는 에이전트가 계속 일하는 중이므로 구간을 끊지 않는다."""
    from evaluation.run_metrics import collaboration_metrics
    waiting = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1), a2a('qa', 'A2A_TASK_STATE_CHANGED', 3, 'INPUT_REQUIRED'),
        wait(3, 'WAITING_INPUT')))
    assert (waiting['agentActiveMs'], waiting['stepsWithoutEnd'], waiting['humanWaitOpen']) == (2000, 0, True)
    timeout = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1), a2a('qa', 'A2A_TASK_RECEIVED', 2, 'WORKING'),
        a2a('qa', 'A2A_POLL_TIMED_OUT', 5, 'WORKING'), a2a('qa', 'A2A_TASK_STATE_CHANGED', 9, 'COMPLETED'), verdict=10))
    assert (timeout['agentActiveMs'], timeout['stepsWithoutEnd']) == (8000, 0)


def test_work_observed_again_after_interrupt_reopens_the_step():
    """중단 뒤 새 요청 없이 폴링에서 다시 WORKING이 보이면(dev resume_polling) 그 시각부터 새 구간이다.
    활동 1~3초 + 8~10초 = 4초. 두 번 중단·재개해도 구간마다 끊는다."""
    from evaluation.run_metrics import collaboration_metrics
    observed = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1), a2a('qa', 'A2A_TASK_STATE_CHANGED', 3, 'INPUT_REQUIRED'),
        a2a('qa', 'A2A_TASK_STATE_CHANGED', 8, 'WORKING'), a2a('qa', 'A2A_TASK_STATE_CHANGED', 10, 'COMPLETED'),
        verdict=11))
    assert observed['agentActiveMs'] == 4000
    twice = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1, attempt=1), a2a('qa', 'A2A_TASK_STATE_CHANGED', 2, 'AUTH_REQUIRED', 1),
        a2a('qa', 'A2A_MESSAGE_SENT', 5, attempt=2), a2a('qa', 'A2A_TASK_STATE_CHANGED', 7, 'INPUT_REQUIRED', 2),
        a2a('qa', 'A2A_MESSAGE_SENT', 20, attempt=3), a2a('qa', 'A2A_TASK_UPDATED', 21, 'WORKING', 3),
        a2a('qa', 'A2A_TASK_STATE_CHANGED', 24, 'COMPLETED', 3), verdict=25))
    assert (twice['agentActiveMs'], twice['stepsByRole']['UNKNOWN']) == (1000 + 2000 + 4000, {'steps': 1, 'ms': 7000})
    unfinished = collaboration_metrics(run_events(
        a2a('qa', 'A2A_MESSAGE_SENT', 1, attempt=1), a2a('qa', 'A2A_TASK_STATE_CHANGED', 3, 'INPUT_REQUIRED', 1),
        a2a('qa', 'A2A_MESSAGE_SENT', 8, attempt=2), verdict=11))
    assert (unfinished['agentActiveMs'], unfinished['stepsWithoutEnd']) == (2000, 1)  # 끝나지 않은 재개 구간은 빼고 센다


# ---------------------------------------------------------------- Ver1·Ver2(순환 안 테스트 정책) 비교


def policy_entry(run_id, policy, pair, *, suite='f' * 64, **override):
    """Ver1·Ver2 비교 입력 한 개. Ver1은 보호 테스트 참조가 있고 Ver2는 없다(override로 일부 설정만 바꿀 수 있다)."""
    settings = {'protectedTestSuiteRef': 'suite-1' if policy == 'IN_LOOP_AND_FINAL' else None, **override}
    return {'architecture': 'MULTI_AGENT', 'loopPolicy': policy, 'pairId': pair,
            'configuration': configuration(run_id, **settings), 'evaluation': evaluation_report(run_id, suite=suite)}


def policy_entries(**ver2_override):
    """Ver1·Ver2 두 쌍(Run 4개). ver2_override로 Ver2 첫 Run의 설정만 다르게 만들 수 있다."""
    return [policy_entry(uid(1), 'IN_LOOP_AND_FINAL', 'p1'), policy_entry(uid(2), 'FINAL_ONLY', 'p1', **ver2_override),
            policy_entry(uid(3), 'IN_LOOP_AND_FINAL', 'p2'), policy_entry(uid(4), 'FINAL_ONLY', 'p2')]


def test_run_metrics_reads_loop_policy_from_frozen_configuration():
    """순환 안 테스트 정책은 동결 설정의 보호 테스트 참조로 정한다(있으면 Ver1, 없으면 Ver2)."""
    ver1 = bundle(uid(1))
    ver2 = bundle(uid(2))
    ver2['configuration'] = configuration(uid(2), protectedTestSuiteRef=None)
    assert run_metrics(ver1)['loopPolicy'] == 'IN_LOOP_AND_FINAL' and run_metrics(ver2)['loopPolicy'] == 'FINAL_ONLY'


def test_ver1_and_ver2_with_only_the_protected_suite_different_are_comparable():
    """같은 Multi 구조에서 보호 테스트 참조만 다르고 쌍이 맞으면 비교해도 된다. Single/Multi 기준으로는 비교할 수 없다."""
    assert comparison_validity(policy_entries(), group_by='loopPolicy') == {'valid': True, 'violations': [], 'warnings': []}
    with pytest.raises(ValueError):
        comparison_validity(policy_entries())  # 구조 기준으로는 두 조건이 없다(둘 다 MULTI_AGENT)


@pytest.mark.parametrize('change, field', [
    (lambda data: data[1].update(loopPolicy='IN_LOOP_AND_FINAL'), 'pairId'),
    (lambda data: data[1]['configuration']['configuration'].update(protectedTestSuiteRef='suite-1'), 'loopPolicy'),
    (lambda data: data[2]['configuration']['configuration'].update(protectedTestSuiteRef='suite-2'), 'protectedTestSuiteRef'),
    (lambda data: data[1]['configuration']['configuration']['model'].update(modelId='other'), 'model.modelId'),
    (lambda data: data[3].update(evaluation=evaluation_report(uid(4), suite='a' * 64)), 'suite.sha256'),
    (lambda data: data[3].update(pairId='p1'), 'pairId'),
])
def test_ver1_ver2_comparison_rejects_unintended_differences(change, field):
    """의도하지 않은 차이는 비교를 무효로 만든다: 쌍이 맞지 않음(같은 정책 둘·세 Run), 이름표와 동결 설정이 다름,
    Ver1끼리 다른 보호 묶음, 모델 차이, 다른 최종 시험지(예: pilot과 final 혼용)."""
    data = policy_entries()
    change(data)
    result = comparison_validity(data, group_by='loopPolicy')
    assert result['valid'] is False and field in {item['field'] for item in result['violations']}


def test_unpaired_runs_are_flagged_as_a_warning():
    """쌍 번호가 없는 Run은 비교를 막지 않지만 주의로 알린다(실행 순서·시간대 차이가 한쪽에 몰릴 수 있다)."""
    data = policy_entries()
    for item in data:
        item.pop('pairId')
    result = comparison_validity(data, group_by='loopPolicy')
    assert result['valid'] is True and {item['detail'] for item in result['warnings']} == {'RUN_IS_NOT_PAIRED'}


def scored_report(run_id, statuses, *, code_version=2):
    """점수 계산에 필요한 부분만 흉내 낸 독립 평가 보고서. statuses: 요구사항마다 검사 상태 목록."""
    checks, requirements = [], []
    for index, row in enumerate(statuses):
        ids = [f'{index}-{number}' for number in range(len(row))]
        checks += [{'testId': check_id, 'required': True, 'status': status, 'perspective': 'QA', 'title': check_id}
                   for check_id, status in zip(ids, row)]
        requirements.append({'requirementId': uid(100 + index), 'checkIds': ids})
    report = evaluation_report(run_id, code_version=code_version)
    report.update(manifestCheck={'metadataMatches': True, 'errorCode': None}, checks=checks, requirements=requirements)
    return report


def test_summary_and_practice_effect_by_loop_policy():
    """정책별로 요약하고 최종 시험 점수를 평균 낸다. 연습 효과는 (Ver1 순환용 − 최종) − (Ver2 순환용 − 최종)이다:
    Ver1은 순환용 1.0·최종 0.75(차이 0.25), Ver2는 순환용 0.5·최종 0.5(차이 0, 두 시험지의 난이도 차이 없음) → 0.25."""
    runs = []
    for run_id, ref in ((uid(1), 'suite-1'), (uid(2), None)):
        item = bundle(run_id)
        item['configuration'] = configuration(run_id, protectedTestSuiteRef=ref)
        runs.append(run_metrics(item))
    finals = [scored_report(uid(1), [['PASS'], ['PASS', 'FAIL']]), scored_report(uid(2), [['PASS'], ['FAIL']])]
    practices = [scored_report(uid(1), [['PASS'], ['PASS', 'PASS']]), scored_report(uid(2), [['PASS'], ['FAIL']])]
    summary = experiment_summary(runs, {'IN_LOOP_AND_FINAL': 1, 'FINAL_ONLY': 1}, finals, group_by='loopPolicy')
    assert summary['IN_LOOP_AND_FINAL']['evaluation']['meanExamScore'] == 0.75
    assert summary['FINAL_ONLY']['evaluation']['meanExamScore'] == 0.5
    effect = loop_policy_effect(runs, finals, practices)
    assert effect['practiceEffect'] == pytest.approx(0.25)
    assert effect['policies']['IN_LOOP_AND_FINAL']['meanGap'] == pytest.approx(0.25)
    assert effect['policies']['FINAL_ONLY']['meanGap'] == 0
    with pytest.raises(ValueError):  # 참고 보고서가 다른 코드 버전을 채점했으면 짝지을 수 없다.
        loop_policy_effect(runs, finals, [practices[0], scored_report(uid(2), [['PASS']], code_version=1)])
    with pytest.raises(ValueError):  # 정책 기준 요약에 계획하지 않은 정책의 Run이 섞이면 거부한다.
        experiment_summary(runs, {'IN_LOOP_AND_FINAL': 2}, group_by='loopPolicy')
