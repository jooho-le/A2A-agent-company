"""끝 상태 원인·의미 검토 호출·자동 완료(outcomes.py)와 정확도·안정성·짝 비교 집계를 시험한다.

고정 기록(fixtures/dev_outcome_runs.json)은 dev ea06d12(2026-11-13) 시험 하네스(가짜 LLM·가짜 docker, 생성 코드 실행 없음)로
만든 Run을 dev evaluation_export로 꺼내 평가 코드가 읽는 칸만 남긴 것이다. 보고서는 실제 보고서 생성기로 만든다.

확인하는 것:
  - 실제 dev 보류·실패·중단 기록 11개가 기록된 근거대로 원인·비교 유효성·재개 방식으로 갈리는가?
  - 근거가 여러 곳에 있을 때 가장 직접적인 근거(에이전트 상태 메시지)를 쓰고, 근거가 없으면 추측하지 않는가?
  - 의미 검토 호출을 작업 호출과 나누고, 영수증이 빠지면 합계를 확정하지 않는가?
  - 자동 완료는 다섯 조건이 모두 확인될 때만 통과이고, 확인 못 한 조건은 미확인인가?
  - 요구사항 완전 충족률 하한·상한, 판정 가능률, QA 통과율, 보안 차단 결함 수가 정의대로 계산되는가?
  - 집계(자동 완료율·원인 분포·환경 장애 Run)와 Single/Multi 짝 비교가 모르는 값을 0으로 채우지 않는가?
"""
import copy
import json
from pathlib import Path
import uuid

import pytest

from evaluation.aggregation import CheckResult, Status
from evaluation.independent import build_evaluation_report
from evaluation.manifest import ExecutionManifest
from evaluation.outcomes import auto_completion, classify_run, review_usage, run_outcome, stop_signals
from evaluation.run_metrics import architecture_pairs, experiment_summary, run_metrics
from evaluation.scoring import exam_score, product_accuracy
from tests.evaluation.test_independent import inputs, uid as iuid
from tests.evaluation.test_run_metrics import bundle, uid

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/dev_outcome_runs.json').read_text(encoding='utf-8'))


def real(name):
    data = copy.deepcopy(FIXTURE[name])
    return {key: data[key] for key in ('bundle', 'trace', 'a2a', 'handoff')}, data['run']


EXPECTED = {
    'multiFix': ('FINISHED', 'COMPLETED_SUCCESS', 'VALID', 'TERMINAL'),
    'single': ('FINISHED', 'COMPLETED_SUCCESS', 'VALID', 'TERMINAL'),
    'plannerSemanticHold': ('WAITING_INPUT', 'SEMANTIC_REVIEW_HOLD', 'VALID', 'ANSWER_THEN_REVIEW'),
    'developerSemanticHold': ('HUMAN_REVIEW', 'SEMANTIC_REVIEW_HOLD', 'VALID', 'NEW_RUN_REQUIRED'),
    'singleSemanticHold': ('HUMAN_REVIEW', 'SEMANTIC_REVIEW_HOLD', 'VALID', 'NEW_RUN_REQUIRED'),
    'plannerQuestion': ('WAITING_INPUT', 'PLANNER_QUESTION', 'VALID', 'ANSWER'),
    'reviewProviderError': ('HUMAN_REVIEW', 'MODEL_API_FAILURE', 'UNVERIFIED', 'OPERATOR_DECISION'),
    'developerProviderError': ('HUMAN_REVIEW', 'MODEL_API_FAILURE', 'UNVERIFIED', 'OPERATOR_DECISION'),
    'reviewAuthRequired': ('HUMAN_REVIEW', 'MODEL_AUTH_FAILURE', 'UNVERIFIED', 'CREDENTIAL_THEN_RESUME'),
    'budgetExhausted': ('HUMAN_REVIEW', 'BUDGET_EXHAUSTED', 'VALID', 'OPERATOR_DECISION'),
    'abortedDuringReview': ('ABORTED', 'ABORTED', 'VALID', 'TERMINAL'),
}


# ---------------------------------------------------------------- 끝 상태 원인

@pytest.mark.parametrize('name', sorted(EXPECTED))
def test_real_dev_runs_are_classified_by_their_recorded_cause(name):
    data, _ = real(name)
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert (result['runStatus'], result['cause'], result['comparisonValidity'], result['resume']) == EXPECTED[name]
    assert result['segmentClosed'] is True and result['evidenceComplete'] is True


def test_planner_question_keeps_the_unapproved_assumption_and_question_counts():
    data, _ = real('plannerQuestion')
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert result['details'] == {'questions': 1, 'unapprovedAssumptions': 1}
    assert result['stoppedBy']['identity'] == 'PLANNER' and result['stoppedBy']['code'] == 'PLANNER_INPUT_REQUIRED'


@pytest.mark.parametrize('name,identity,policy', [('developerSemanticHold', 'DEVELOPER', 'NEW_RUN_REQUIRED'),
                                                  ('singleSemanticHold', 'SINGLE_AGENT', 'NEW_RUN_REQUIRED'),
                                                  ('plannerSemanticHold', 'PLANNER', 'REGENERATE_AND_REVIEW')])
def test_semantic_hold_keeps_reason_identity_and_resume_policy(name, identity, policy):
    data, _ = real(name)
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert result['stoppedBy']['identity'] == identity
    assert (result['details']['reviewReason'], result['details']['resumePolicy']) == ('UNSUPPORTED_MEANING', policy)


def _semantic_failure(name, identity, reason, category):
    data, _ = real(name)
    data['bundle']['run']['status'] = 'HUMAN_REVIEW'
    for row in data['a2a']['roles'][identity]['events']:
        status = row['event']['status']
        for part in (status.get('message') or {}).get('parts') or []:
            message = part.get('data') or {}
            if message.get('code') == 'SEMANTIC_REVIEW_INPUT_REQUIRED':
                status['state'] = 'TASK_STATE_FAILED'
                message['code'] = 'SEMANTIC_REVIEW_FAILED'
                message['questions'] = []
                message['semanticReview'].update(schemaVersion=2, reasonCode=reason, category=category,
                                                issues=[{'fieldPointer': '/summary', 'reasonCode': reason}])
    for row in data['trace']['telemetry']:
        if row['binding'].get('eventType') == 'AGENT_SEMANTIC_REVIEWED' and row['detail']['status'] == 'HELD':
            row['detail'].update(reasonCode=reason, category=category)
    return data


@pytest.mark.parametrize('reason,category', [
    ('SEMANTIC_EVIDENCE_INVALID', 'EVIDENCE'),
    ('SEMANTIC_RESPONSE_INVALID', 'FORMAT'),
    ('SEMANTIC_REVIEW_BINDING_INVALID', 'BINDING'),
])
@pytest.mark.parametrize('name,identity,policy,resume', [
    ('plannerSemanticHold', 'PLANNER', 'REGENERATE_AND_REVIEW', 'OPERATOR_DECISION'),
    ('developerSemanticHold', 'DEVELOPER', 'NEW_RUN_REQUIRED', 'NEW_RUN_REQUIRED'),
    ('singleSemanticHold', 'SINGLE_AGENT', 'NEW_RUN_REQUIRED', 'NEW_RUN_REQUIRED'),
])
def test_technical_semantic_failure_keeps_details_without_requesting_user_requirements(
        reason, category, name, identity, policy, resume):
    data = _semantic_failure(name, identity, reason, category)
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert (result['runStatus'], result['cause'], result['comparisonValidity'], result['resume']) == \
        ('HUMAN_REVIEW', 'SEMANTIC_REVIEW_FAILURE', 'VALID', resume)
    assert result['details'] == {'questions': 0, 'unapprovedAssumptions': 0,
                                 'reviewReason': reason, 'resumePolicy': policy, 'reviewCategory': category}
    assert result['stoppedBy']['code'] == 'SEMANTIC_REVIEW_FAILED'
    assert result['stoppedBy']['identity'] == identity
    assert result['stoppedBy']['state'] == 'TASK_STATE_FAILED'


@pytest.mark.parametrize('reason,category', [
    ('SEMANTIC_EVIDENCE_INVALID', 'EVIDENCE'),
    ('SEMANTIC_RESPONSE_INVALID', 'FORMAT'),
    ('SEMANTIC_REVIEW_BINDING_INVALID', 'BINDING'),
])
def test_technical_semantic_failure_trace_fallback_preserves_reason_and_policy(reason, category):
    data = _semantic_failure('plannerSemanticHold', 'PLANNER', reason, category)
    result = classify_run(data['bundle'], data['trace'], None)
    assert (result['cause'], result['comparisonValidity'], result['resume']) == \
        ('SEMANTIC_REVIEW_FAILURE', 'VALID', 'OPERATOR_DECISION')
    assert result['details'] == {'reviewReason': reason, 'resumePolicy': 'REGENERATE_AND_REVIEW',
                                 'reviewCategory': category}
    assert result['stoppedBy']['source'] == 'SEMANTIC_REVIEW'
    assert result['evidenceComplete'] is False


def test_aborted_run_keeps_the_operator_reason_and_the_canceled_review():
    data, _ = real('abortedDuringReview')
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert result['details'] == {'terminationReason': '합성 중단 점검', 'underlyingCode': 'SEMANTIC_REVIEW_CANCELED'}


def test_agent_status_code_is_preferred_over_usage_receipt():
    # 같은 Run에 다른 오류 영수증을 덧붙여도 에이전트가 직접 남긴 멈춤 이유(의미 검토 보류)를 쓴다.
    data, _ = real('plannerSemanticHold')
    data['bundle']['usage']['records'][-1]['detail']['outcome'] = 'LLM_PROVIDER_ERROR'
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert result['cause'] == 'SEMANTIC_REVIEW_HOLD'
    assert {'source': 'USAGE', 'identity': 'PLANNER', 'code': 'LLM_PROVIDER_ERROR', 'sequence': 2} in result['signals']


def test_without_agent_records_the_review_record_then_the_usage_receipt_decides():
    data, _ = real('reviewProviderError')
    by_review = classify_run(data['bundle'], data['trace'], None)
    assert (by_review['cause'], by_review['stoppedBy']['source'], by_review['evidenceComplete']) == \
        ('MODEL_API_FAILURE', 'SEMANTIC_REVIEW', False)
    by_usage = classify_run(data['bundle'])
    assert (by_usage['cause'], by_usage['stoppedBy']['source']) == ('MODEL_API_FAILURE', 'USAGE')


def _set_code(data, identity, code, state='TASK_STATE_FAILED'):
    for row in data['a2a']['roles'][identity]['events']:
        status = row['event']['status']
        if 'message' in status:
            status['message']['parts'][0]['data'] = {'code': code}
            status['state'] = state
    return data


@pytest.mark.parametrize('code,state,cause,validity,resume', [
    ('LLM_EXECUTION_TIMEOUT', 'TASK_STATE_FAILED', 'MODEL_CALL_TIMEOUT', 'REVIEW_REQUIRED', 'OPERATOR_DECISION'),
    ('LLM_TOOL_EXECUTION_FAILED', 'TASK_STATE_FAILED', 'TOOL_FAILURE', 'REVIEW_REQUIRED', 'OPERATOR_DECISION'),
    ('LLM_SCHEMA_INVALID', 'TASK_STATE_FAILED', 'MODEL_OUTPUT_INVALID', 'VALID', 'OPERATOR_DECISION'),
    ('LLM_SOURCE_REPAIR_EXHAUSTED', 'TASK_STATE_FAILED', 'SOURCE_REPAIR_EXHAUSTED', 'VALID', 'OPERATOR_DECISION'),
    ('REQUIREMENT_SCOPE_INVALID', 'TASK_STATE_FAILED', 'REQUIREMENT_SCOPE_INVALID', 'VALID', 'OPERATOR_DECISION'),
    ('QA_INPUT_REQUIRED', 'TASK_STATE_INPUT_REQUIRED', 'AGENT_QUESTION', 'VALID', 'ANSWER'),
    ('SOMETHING_NEW', 'TASK_STATE_FAILED', 'AGENT_STOPPED', 'REVIEW_REQUIRED', 'OPERATOR_DECISION'),
])
def test_other_stop_codes_and_unknown_codes(code, state, cause, validity, resume):
    data = _set_code(real('developerProviderError')[0], 'DEVELOPER', code, state)
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert (result['cause'], result['comparisonValidity'], result['resume']) == (cause, validity, resume)


def test_hold_without_any_recorded_cause_is_not_guessed():
    data, _ = real('reviewProviderError')
    for rows in data['a2a']['roles'].values():
        rows['events'] = []
    data['trace']['telemetry'] = [row for row in data['trace']['telemetry']
                                  if row['binding'].get('eventType') != 'AGENT_SEMANTIC_REVIEWED']
    for row in data['bundle']['usage']['records']:
        row['detail']['outcome'] = 'completed'
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert (result['cause'], result['comparisonValidity']) == ('DISPATCH_REVIEW', 'REVIEW_REQUIRED')
    data['bundle']['events'] = [event for event in data['bundle']['events']
                                if event['eventType'] != 'A2A_DISPATCH_REQUIRES_REVIEW']
    assert classify_run(data['bundle'], data['trace'], data['a2a'])['cause'] == 'UNKNOWN_HOLD'


@pytest.mark.parametrize('status', ['PLANNING', 'IMPLEMENTING', 'VALIDATING', 'FIXING'])
def test_running_run_is_not_a_closed_outcome(status):
    data, _ = real('plannerSemanticHold')
    data['bundle']['run']['status'] = status
    result = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert (result['cause'], result['segmentClosed']) == ('IN_PROGRESS', False)


def test_latest_task_status_is_used_even_if_an_older_one_has_an_error():
    data, _ = real('plannerSemanticHold')
    rows = data['a2a']['roles']['PLANNER']['events']
    older = copy.deepcopy(rows[-1])
    older['version'] = 0
    older['event']['status'] = {'state': 'TASK_STATE_FAILED', 'timestamp': '2026-01-01T00:00:00Z',
                                'message': {'parts': [{'data': {'code': 'LLM_PROVIDER_ERROR'}}]}}
    rows.append(older)  # 순서가 뒤에 와도 버전이 낮으면 마지막 상태가 아니다
    signals = stop_signals(data['a2a'])
    assert [item['code'] for item in signals] == ['SEMANTIC_REVIEW_INPUT_REQUIRED']


# ---------------------------------------------------------------- 의미 검토 호출

@pytest.mark.parametrize('name,reviews,review_calls,work_calls', [('multiFix', 7, 7, 13), ('single', 4, 4, 7)])
def test_review_calls_are_separated_from_work_calls(name, reviews, review_calls, work_calls):
    data, _ = real(name)
    usage = review_usage(data['bundle'], data['trace'])
    assert (usage['reviews'], usage['held'], usage['reviewModelCalls'], usage['workModelCalls']) == \
        (reviews, 0, review_calls, work_calls)
    assert (usage['reviewInputTokens'], usage['reviewOutputTokens'], usage['complete']) == (10 * reviews, 5 * reviews, True)


def test_review_usage_is_not_totaled_when_a_review_receipt_is_missing():
    data, _ = real('multiFix')
    first_review = next(row for row in data['trace']['telemetry'] if row['binding'].get('eventType') == 'AGENT_SEMANTIC_REVIEWED')
    sequence = first_review['detail']['modelCallSequences'][0]
    data['bundle']['usage']['records'] = [row for row in data['bundle']['usage']['records']
                                          if row['detail']['sequence'] != sequence]
    usage = review_usage(data['bundle'], data['trace'])
    assert (usage['complete'], usage['reviewModelCalls'], usage['knownReviewModelCalls'], usage['reviewInputTokens']) == \
        (False, None, 6, None)


def test_review_usage_is_unknown_without_telemetry():
    data, _ = real('multiFix')
    del data['trace']['telemetry']
    assert review_usage(data['bundle'], data['trace']) is None


@pytest.mark.parametrize('damage', ['call_count', 'duplicate_receipt', 'pending_call'])
def test_review_usage_cannot_invent_negative_work_calls_or_accept_duplicate_receipts(damage):
    data, _ = real('multiFix')
    usage = data['bundle']['usage']
    if damage == 'call_count': usage['summary']['modelCalls'] = 0
    elif damage == 'pending_call': usage['summary']['pendingModelSequences'] = [21]
    else:
        usage['records'].append(copy.deepcopy(usage['records'][0]))
        usage['total'] += 1
        usage['summary']['modelCalls'] += 1
    result = review_usage(data['bundle'], data['trace'])
    assert result['complete'] is False and result['callRecordsComplete'] is False
    assert result['workModelCalls'] is None and result['reviewModelCalls'] is None
    assert result['reviewInputTokens'] is None


@pytest.mark.parametrize('damage', ['usage_unknown', 'token_total', 'cost_total'])
def test_review_usage_preserves_call_counts_but_rejects_unconfirmed_measurement_sums(damage):
    data, _ = real('multiFix')
    usage = data['bundle']['usage']
    if damage == 'usage_unknown': usage['summary'].update(usageComplete=False, totalTokens=None)
    elif damage == 'token_total': usage['summary']['totalTokens'] += 1
    else: usage['summary'].update(costComplete=True, costUsd='999')
    result = review_usage(data['bundle'], data['trace'])
    assert result['complete'] is False and result['callRecordsComplete'] is True
    assert result['reviewModelCalls'] == 7 and result['workModelCalls'] == 13
    assert result['reviewCostUsd'] is None and result['measurementWarnings']
    if damage != 'cost_total':
        assert result['reviewInputTokens'] is None and result['reviewOutputTokens'] is None


# ---------------------------------------------------------------- 자동 완료

def _auto(name='multiFix', report=None, mutate=None):
    data, run_doc = real(name)
    if mutate:
        mutate(data, run_doc)
    metrics = run_metrics(data['bundle'])
    return auto_completion(data, run_doc, report=report, metrics=metrics)


def test_current_host_cannot_prove_full_budget_and_final_package_even_after_product_pass():
    result = _auto(report={'evaluationVerdict': 'PASS'})
    assert result['status'] == 'UNVERIFIED'
    assert result['conditions']['withinLimits']['reason'] == 'LLM_LIMIT_EVIDENCE_MISSING'
    assert result['conditions']['packageComplete']['reason'] == 'FINAL_REPORT_AND_PACKAGE_COMPLETION_NOT_EXPORTED'
    assert result['packageDefinition'][0] == 'REQUIREMENT' and result['limitations']


def _drop_final_qa(data, run_doc):
    data['trace']['artifacts'] = [item for item in data['trace']['artifacts']
                                  if not (item['artifactType'] == 'QA_REPORT' and item.get('codeVersion') == run_doc['finalCodeVersion'])]


def _resumed(data, _run_doc):
    event = dict(data['bundle']['events'][-1], eventType='WORKFLOW_RESUMED', eventId=uid(777))
    data['bundle']['events'].insert(-1, event)
    data['bundle']['eventsTotal'] += 1


def test_finished_after_resume_is_assisted_even_when_internal_verdict_is_success():
    data, run_doc = real('multiFix')
    _resumed(data, run_doc)
    outcome = classify_run(data['bundle'], data['trace'], data['a2a'])
    assert outcome['cause'] == 'ASSISTED_CONTINUATION' and outcome['comparisonValidity'] == 'ASSISTED'
    assert outcome['details']['automaticBoundary'] == 'NOT_CAPTURED_IN_THIS_EXPORT'
    completion = auto_completion(data, run_doc, report={'evaluationVerdict': 'PASS'}, metrics=run_metrics(data['bundle']))
    assert completion['status'] == 'FAIL'
    assert completion['conditions']['noHumanIntervention']['reason'] == 'PAUSED_OR_RESUMED'


def _budget(value):
    def change(data, _run_doc):
        limits = data['bundle']['configuration']['configuration']['limits']
        if value is None:
            limits.pop('runtimeBudgetMs')
        else:
            limits['runtimeBudgetMs'] = value
    return change


def _events_missing(data, _run_doc):
    data['bundle']['eventsTotal'] += 1


def _not_exported(_data, run_doc):
    run_doc['exportedCodeVersions'] = [1]


@pytest.mark.parametrize('report,mutate,condition,status,reason', [
    ({'evaluationVerdict': 'FAIL'}, None, 'productPass', 'FAIL', 'EVALUATION_FAIL'),
    ({'evaluationVerdict': 'UNVERIFIED'}, None, 'productPass', 'UNVERIFIED', 'EVALUATION_UNVERIFIED'),
    (None, None, 'productPass', 'UNVERIFIED', 'NO_INDEPENDENT_REPORT'),
    ({'evaluationVerdict': 'PASS'}, _drop_final_qa, 'packageComplete', 'FAIL', 'MISSING_QA_REPORT'),
    ({'evaluationVerdict': 'PASS'}, _not_exported, 'packageComplete', 'FAIL', 'FINAL_SNAPSHOT_NOT_EXPORTED'),
    ({'evaluationVerdict': 'PASS'}, _resumed, 'noHumanIntervention', 'FAIL', 'PAUSED_OR_RESUMED'),
    ({'evaluationVerdict': 'PASS'}, _budget(1), 'withinLimits', 'FAIL', 'RUNTIME_BUDGET_EXCEEDED'),
    ({'evaluationVerdict': 'PASS'}, _budget(None), 'withinLimits', 'UNVERIFIED', 'RUNTIME_BUDGET_UNKNOWN'),
    ({'evaluationVerdict': 'PASS'}, _events_missing, 'noHumanIntervention', 'UNVERIFIED', 'EVENTS_INCOMPLETE'),
    ({'evaluationVerdict': 'PASS'}, _events_missing, 'withinLimits', 'UNVERIFIED', 'ELAPSED_TIME_UNKNOWN'),
])
def test_each_auto_completion_condition_fails_or_stays_unverified(report, mutate, condition, status, reason):
    result = _auto(report=report, mutate=mutate)
    assert result['conditions'][condition] == {'status': status, 'reason': reason}
    assert result['status'] == ('FAIL' if status == 'FAIL' else 'UNVERIFIED')


@pytest.mark.parametrize('name', [name for name in EXPECTED if not EXPECTED[name][1].startswith('COMPLETED')])
def test_stopped_runs_are_never_auto_completed(name):
    result = _auto(name, report={'evaluationVerdict': 'PASS'})
    assert result['status'] == 'FAIL' and result['conditions']['internalSuccess']['status'] == 'FAIL'


def test_run_outcome_bundles_cause_review_usage_and_auto_completion():
    data, run_doc = real('budgetExhausted')
    record = run_outcome(data, run_doc, metrics=run_metrics(data['bundle']))
    assert record['runId'] == run_doc['runId'] and record['outcome']['cause'] == 'BUDGET_EXHAUSTED'
    assert record['semanticReview']['held'] == 1 and record['semanticReview']['reviewModelCalls'] == 0
    assert record['autoCompletion']['conditions']['withinLimits'] == {'status': 'FAIL', 'reason': 'BUDGET_EXHAUSTED'}


# ---------------------------------------------------------------- 정확도(실제 보고서 생성기)

def report_with(statuses, *, observed=None, run_id=None, code_version=1):
    # 요구사항 uid(10)=QA 검사 uid(20), uid(11)=SECURITY 검사 uid(21)·uid(22). statuses: {검사 번호: Status}
    args = inputs()
    args['evaluation_id'] = iuid(4000)  # 시험용 Run 번호(uid(1)·uid(2)…)와 겹치지 않게
    args['results'] = [CheckResult(iuid(n), statuses.get(n, Status.PASS), (iuid(n + 100),)) for n in (20, 21, 22)]
    if code_version != 1:
        manifest = ExecutionManifest('test-repo', code_version, iuid(1), 'sha1', 'a' * 40, 'b' * 40,
                                     'c' * 64, 'sha256:' + 'd' * 64, 'sha256:' + 'e' * 64)
        args.update(expected=manifest, observed=manifest)
    if observed is not None:
        args['observed'] = observed
    if run_id is not None:
        args['source_run_id'] = run_id
    return build_evaluation_report(**args)


@pytest.mark.parametrize('statuses,expected', [
    ({}, dict(P=2, F=0, U=0, lower=1.0, upper=1.0, decided=1.0, qa=1.0, blocking=0, product=True)),
    # 보안 검사 하나 FAIL·하나 미검증: 요구사항 판정은 미검증으로 보존하되, 충족률 상한에도 넣지 않는다.
    ({21: Status.FAIL, 22: Status.ERROR}, dict(P=1, F=0, U=1, lower=0.5, upper=0.5, decided=2 / 3, qa=1.0, blocking=1,
                                                product=False)),
    # 미검증만 있는 요구사항은 하한에서는 미충족, 상한에서는 충족이다.
    ({22: Status.NOT_RUN}, dict(P=1, F=0, U=1, lower=0.5, upper=1.0, decided=2 / 3, qa=1.0, blocking=0, product=False)),
    ({20: Status.FAIL}, dict(P=1, F=1, U=0, lower=0.5, upper=0.5, decided=1.0, qa=0.0, blocking=0, product=False)),
])
def test_product_accuracy_bounds_decidability_qa_and_blocking_findings(statuses, expected):
    result = product_accuracy(report_with(statuses))
    assert (result['passedRequirements'], result['failedRequirements'], result['unverifiedRequirements']) == \
        (expected['P'], expected['F'], expected['U'])
    assert result['fullSatisfactionLower'] == expected['lower'] and result['fullSatisfactionUpper'] == expected['upper']
    assert result['checkDecidability'] == pytest.approx(expected['decided']) and result['checkCount'] == 3
    assert result['qaPassRate'] == expected['qa'] and result['blockingSecurityFindings'] == expected['blocking']
    assert result['productPass'] is expected['product']


def test_partial_score_is_not_full_satisfaction():
    report = report_with({22: Status.FAIL})
    assert exam_score(report)['score'] == 0.75  # (1 + 1/2) / 2: 부분점수
    assert product_accuracy(report)['fullSatisfactionLower'] == 0.5  # 완전 충족은 요구사항 1개뿐


def test_unconfirmed_identity_produces_no_accuracy_values():
    other = ExecutionManifest('test-repo', 1, iuid(1), 'sha1', 'f' * 40, 'b' * 40, 'c' * 64, 'sha256:' + 'd' * 64,
                              'sha256:' + 'e' * 64)
    result = product_accuracy(report_with({}, observed=other))
    assert result['reason'] == 'MANIFEST_NOT_CONFIRMED'
    assert result['fullSatisfactionLower'] is None and result['checkDecidability'] is None and result['productPass'] is False


# ---------------------------------------------------------------- 집계·짝 비교

def _with_outcome(metrics, name=None, *, auto='PASS', cause='COMPLETED_SUCCESS', validity='VALID', review=None):
    record = {'runId': metrics['runId'], 'outcome': {'cause': cause, 'comparisonValidity': validity, 'resume': 'TERMINAL'},
              'semanticReview': review, 'autoCompletion': {'status': auto}}
    return {**metrics, 'outcomeRecord': record}


def _review(calls, tokens=(10, 5), complete=True):
    return {'reviews': calls, 'held': 0, 'knownReviewModelCalls': calls, 'reviewModelCalls': calls if complete else None,
            'reviewInputTokens': tokens[0] if complete else None, 'reviewOutputTokens': tokens[1] if complete else None,
            'reviewCostUsd': None, 'complete': complete}


def test_summary_reports_accuracy_stability_outcomes_and_auto_completion():
    runs = [_with_outcome(run_metrics(bundle(uid(1), fixes=0)), review=_review(3)),
            _with_outcome(run_metrics(bundle(uid(2), fixes=0, verdict='FAIL')), auto='FAIL', cause='COMPLETED_FAIL',
                          review=_review(2)),
            _with_outcome(run_metrics(bundle(uid(3), fixes=0, verdict=None, status='HUMAN_REVIEW')), auto='FAIL',
                          cause='MODEL_API_FAILURE', validity='UNVERIFIED', review=_review(1))]
    reports = [report_with({}, run_id=uid(1)), report_with({22: Status.NOT_RUN}, run_id=uid(2))]
    for report in reports:
        report['architecture'] = 'MULTI_AGENT'
    result = experiment_summary(runs, {'MULTI_AGENT': 4}, reports)['MULTI_AGENT']
    accuracy = result['accuracy']
    assert accuracy['measuredReports'] == 2 and accuracy['meanFullSatisfactionLower'] == 0.75
    assert accuracy['meanFullSatisfactionUpper'] == 1.0 and accuracy['runsWithUnverifiedRequirements'] == 1
    assert accuracy['requirementOutcomes'] == {'passed': 3, 'failed': 0, 'unverified': 1}
    assert accuracy['fullSatisfactionLower'] == {'n': 2, 'median': 0.75, 'min': 0.5, 'max': 1.0}
    assert accuracy['meanQaPassRate'] == 1.0 and accuracy['blockingSecurityFindings'] == 0
    assert result['autoCompletion'] == {'pass': 1, 'fail': 2, 'unverified': 0, 'notRecorded': 0, 'rate': 0.25}
    assert result['outcomes']['causes'] == {'COMPLETED_SUCCESS': 1, 'COMPLETED_FAIL': 1, 'MODEL_API_FAILURE': 1}
    assert result['outcomes']['environmentFailureRuns'] == [uid(3)]
    # 계획 4개 중 3개만 기록돼 검토 호출 합계는 확정하지 않는다(확인된 부분 합만).
    assert result['semanticReview']['knownReviewModelCalls'] == 6 and result['semanticReview']['reviewModelCalls'] is None
    stability = result['stability']
    assert stability['notStartedRate'] is None and stability['unrecordedRate'] == 0.25
    assert stability['pausedRuns'] == {'HUMAN_REVIEW': 1, 'WAITING_INPUT': 0}
    assert stability['externalUnverifiedRuns'] == 1 and stability['packageMs']['n'] == 0
    assert stability['packageUnverifiedRuns'] == 3
    assert stability['allVerdictMs']['n'] == 3


def test_summary_without_outcome_records_counts_them_as_unknown_not_passed():
    result = experiment_summary([run_metrics(bundle(uid(1)))], {'MULTI_AGENT': 1})['MULTI_AGENT']
    assert result['autoCompletion'] == {'pass': 0, 'fail': 0, 'unverified': 0, 'notRecorded': 1, 'rate': 0.0}
    assert result['outcomes']['notRecorded'] == 1 and result['semanticReview']['reviewModelCalls'] is None


def test_review_totals_are_confirmed_only_when_every_planned_run_is_complete():
    runs = [_with_outcome(run_metrics(bundle(uid(n), fixes=0)), review=_review(n)) for n in (1, 2)]
    result = experiment_summary(runs, {'MULTI_AGENT': 2})['MULTI_AGENT']['semanticReview']
    assert (result['reviewModelCalls'], result['reviewInputTokens'], result['complete']) == (3, 20, True)
    runs[1] = _with_outcome(run_metrics(bundle(uid(2), fixes=0)), review=_review(2, complete=False))
    result = experiment_summary(runs, {'MULTI_AGENT': 2})['MULTI_AGENT']['semanticReview']
    assert (result['reviewModelCalls'], result['knownReviewModelCalls'], result['complete']) == (None, 3, False)


def _pair_runs():
    single = run_metrics(bundle(uid(1), architecture='SINGLE_AGENT', fixes=0))
    multi = run_metrics(bundle(uid(2), fixes=0, cost='0.75'))
    lonely = run_metrics(bundle(uid(3), fixes=0))
    twin_a, twin_b = run_metrics(bundle(uid(4), fixes=0)), run_metrics(bundle(uid(5), fixes=0))
    other_single = run_metrics(bundle(uid(6), architecture='SINGLE_AGENT', fixes=0))
    other_multi = {**run_metrics(bundle(uid(7), fixes=0)), 'scenarioId': uid(99)}
    unknown_single = run_metrics(bundle(uid(8), architecture='SINGLE_AGENT', fixes=0, complete=False))
    unknown_multi = run_metrics(bundle(uid(9), fixes=0))
    runs = [single, multi, lonely, twin_a, twin_b, other_single, other_multi, unknown_single, unknown_multi]
    pairs = {uid(1): 'p1', uid(2): 'p1', uid(4): 'p2', uid(5): 'p2', uid(6): 'p3', uid(7): 'p3', uid(8): 'p4', uid(9): 'p4'}
    reports = [report_with({}, run_id=uid(1)), report_with({22: Status.NOT_RUN}, run_id=uid(2))]
    reports[0]['architecture'], reports[1]['architecture'] = 'SINGLE_AGENT', 'MULTI_AGENT'
    for item in (single, multi):
        item['comparisonConfiguration']['configuration']['environment'].update({
            key: reports[0]['expectedManifest'][key] for key in ('containerImageDigest', 'dependencyLockHash')})
    return runs, reports, pairs


def test_architecture_pairs_compute_only_differences_known_on_both_sides():
    runs, reports, pairs = _pair_runs()
    result = architecture_pairs(runs, reports, pairs)
    assert result['direction'] == 'MULTI_MINUS_SINGLE' and result['unpairedRuns'] == [uid(3)]
    assert {item['pairId']: item['reason'] for item in result['excludedPairs']} == {
        'p2': 'NOT_ONE_SINGLE_AND_ONE_MULTI', 'p3': 'CONDITION_MISMATCH'}
    first = next(item for item in result['pairs'] if item['pairId'] == 'p1')
    assert first['metrics']['fullSatisfactionLower'] == {'single': 1.0, 'multi': 0.5, 'difference': -0.5}
    # Multi 보고서가 미검증이라 제품 통과 여부를 모른다: 0(실패)으로 바꿔 차이를 만들지 않는다.
    assert 'productPass' in first['unknownMetrics'] and 'productPass' not in first['metrics']
    assert first['metrics']['costUsd']['difference'] == pytest.approx(0.25)
    assert first['metrics']['verdictMs']['difference'] == 0
    fourth = next(item for item in result['pairs'] if item['pairId'] == 'p4')
    # 독립 보고서가 없는 쌍은 정확도·통과 차이를 계산하지 않고, 비용을 모르는 쪽이 있으면 비용도 뺀다.
    assert {'fullSatisfactionLower', 'productPass', 'costUsd', 'totalTokens'} <= set(fourth['unknownMetrics'])
    assert result['differences']['verdictMs']['n'] == 2 and result['differences']['fullSatisfactionLower']['n'] == 1
    assert result['differences']['fullSatisfactionLower']['singleHigher'] == 1


def test_run_without_any_code_counts_as_known_product_failure_in_pairs():
    single = run_metrics(bundle(uid(1), architecture='SINGLE_AGENT', fixes=0))
    multi = run_metrics(bundle(uid(2), fixes=0, verdict=None, status='HUMAN_REVIEW'))
    multi = {**multi, 'codeVersion': None}
    reports = [report_with({}, run_id=uid(1))]
    reports[0]['architecture'] = 'SINGLE_AGENT'
    for item in (single, multi):
        item['comparisonConfiguration']['configuration']['environment'].update({
            key: reports[0]['expectedManifest'][key] for key in ('containerImageDigest', 'dependencyLockHash')})
    result = architecture_pairs([single, multi], reports, {uid(1): 'p', uid(2): 'p'})
    assert result['pairs'][0]['metrics']['productPass'] == {'single': 1, 'multi': 0, 'difference': -1}


@pytest.mark.parametrize('field,value', [
    ('model', {'provider': 'p', 'modelId': 'other', 'modelRevision': 'r', 'temperature': 0.0, 'seed': 1}),
    ('limits', {'maxFixAttempts': 3, 'maxToolRetries': 2, 'runtimeBudgetMs': 1}),
    ('startingCommitHash', 'f' * 40),
    ('experimentId', uid(999)),
])
def test_pairs_exclude_frozen_condition_mismatches(field, value):
    single_bundle = bundle(uid(1), architecture='SINGLE_AGENT', fixes=0)
    multi_bundle = bundle(uid(2), fixes=0)
    multi_bundle['configuration']['configuration'][field] = value
    runs = [run_metrics(single_bundle), run_metrics(multi_bundle)]
    result = architecture_pairs(runs, pairs={uid(1): 'p', uid(2): 'p'})
    assert result['pairs'] == [] and result['differences']['verdictMs']['n'] == 0
    assert result['excludedPairs'][0]['reason'] == 'CONDITION_MISMATCH'
    assert any(row['field'].startswith(field) for row in result['excludedPairs'][0]['violations'])


def test_pairs_exclude_missing_conditions_and_environment_rerun():
    single = run_metrics(bundle(uid(1), architecture='SINGLE_AGENT'))
    multi = run_metrics(bundle(uid(2)))
    multi['comparisonConfiguration']['configuration']['environment']['containerImageDigest'] = 'sha256:' + 'f' * 64
    result = architecture_pairs([single, multi], pairs={uid(1): 'p', uid(2): 'p'})
    assert result['pairs'] == []
    assert result['excludedPairs'][0]['violations'][0]['severity'] == 'RERUN_RUN'
    del multi['comparisonConfiguration']
    result = architecture_pairs([single, multi], pairs={uid(1): 'p', uid(2): 'p'})
    assert result['excludedPairs'][0]['reason'] == 'CONDITION_EVIDENCE_MISSING'


PAUSED_FIXTURES = ('plannerQuestion', 'plannerSemanticHold', 'developerSemanticHold', 'singleSemanticHold',
                   'reviewProviderError', 'developerProviderError', 'budgetExhausted')


@pytest.mark.parametrize('name', PAUSED_FIXTURES)
def test_paused_runs_have_a_total_time_ending_at_the_pause(name):
    # 점검 6: 질문·의미 검토 보류 등으로 멈춘 Run은 재개하지 않고 종료로 집계하므로, 자동 구간이 끝난 시각
    # (그 상태로 바뀐 WORKFLOW_STATE_CHANGED)까지를 전체 시간으로 잰다. 정상 종료 사건이 없다고 None이 되면 안 된다.
    from datetime import datetime
    data = FIXTURE[name]
    bundle = data['bundle']
    metrics = run_metrics(bundle)
    status = bundle['run']['status']
    started = next(event for event in bundle['events'] if event['eventType'] == 'RUN_STARTED')
    paused = [event for event in bundle['events']
              if event['eventType'] == 'WORKFLOW_STATE_CHANGED' and event.get('workflowState') == status][-1]
    expected = round((datetime.fromisoformat(paused['occurredAt'].replace('Z', '+00:00'))
                      - datetime.fromisoformat(started['occurredAt'].replace('Z', '+00:00'))).total_seconds() * 1000)
    assert metrics['totalMs'] is not None and abs(metrics['totalMs'] - expected) <= 1
    assert metrics['endedBy'] == 'PAUSED' and 'TOTAL_TIME_ENDS_AT_PAUSE' in metrics['notes']


def test_pause_followed_by_other_states_is_not_used_as_the_end():
    # 멈춘 뒤 다른 상태로 바뀐 기록이 섞이면(받는 도중 재개 등) 멈춘 시각을 끝으로 쓰지 않는다.
    data = copy.deepcopy(FIXTURE['plannerQuestion'])
    events = data['bundle']['events']
    events.append(dict(events[-1], eventId=str(uuid.uuid4()), workflowState='PLANNING'))
    data['bundle']['eventsTotal'] = len(events)
    metrics = run_metrics(data['bundle'])
    assert metrics['totalMs'] is None and 'PAUSE_TIME_UNKNOWN' in metrics['notes']


def test_finished_runs_still_end_at_run_finished():
    finished = next(name for name, data in FIXTURE.items()
                    if isinstance(data, dict) and 'bundle' in data and data['bundle']['run']['status'] == 'FINISHED')
    metrics = run_metrics(FIXTURE[finished]['bundle'])
    assert metrics['endedBy'] == 'ENDED' and metrics['totalMs'] is not None
