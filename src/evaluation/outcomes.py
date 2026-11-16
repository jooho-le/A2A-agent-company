"""Run이 어떻게 끝났거나 멈췄는지(원인)와 자동 완료 여부를 인계 기록에서 가른다. 모델 호출·개발 실행은 하지 않는다.

최신 dev(2026-11-13, ea06d12)는 에이전트 결과마다 의미 검토 호출을 붙이고, 검토 보류·Planner 질문이면 Run을 입력 대기
(WAITING_INPUT)로, 개발·검증 단계 보류·모델 호출 오류면 사람 검토(HUMAN_REVIEW)로 멈춘다. 측정 실험에서는 답하지 않으므로
그 시점에서 자동 구간이 닫힌다. 이 파일은 그런 Run을 "진행 중"으로 버리지 않고 원인과 함께 남기려고 쓴다.

[원인을 읽는 곳] (실제 dev 하네스로 만든 보류·실패·중단 Run 기록에서 확인한 자리)
- 에이전트 상태 메시지: a2a-messages.json의 TaskStatusUpdateEvent → status.message.parts[].data.code
  (SEMANTIC_REVIEW_INPUT_REQUIRED, PLANNER_INPUT_REQUIRED, LLM_PROVIDER_ERROR, LLM_AUTH_REQUIRED, LLM_BUDGET_EXHAUSTED …)
- 의미 검토 기록: trace.json telemetry의 AGENT_SEMANTIC_REVIEWED (status·reasonCode·resumePolicy·modelCallSequences)
- 호출 영수증: bundle.json usage.records의 detail.outcome(completed가 아니면 오류 코드)
- Host 사건: bundle.json events의 A2A_DISPATCH_REQUIRES_REVIEW·DEVELOPER_INPUT_REQUIRED·RUN_ABORTED·WORKFLOW_RESUMED
여러 곳에 같은 원인이 남으면 에이전트 상태 메시지 → 의미 검토 → 호출 영수증 순서로 가장 직접적인 근거를 쓴다.

[비교 유효성] 개발정의서 11-A "모델 API 장애 → UNVERIFIED로 기록"을 따른다.
- VALID: 구조의 결과로 센다(보류·질문·한도 초과·출력 오류도 실패·보류 결과로 남는다).
- UNVERIFIED: 공급자 장애·인증·설정 문제처럼 구조와 무관한 환경 문제. 비교에서 미검증 Run으로 따로 표시한다.
- REVIEW_REQUIRED: 원인을 하나로 정할 수 없다(호출 시간 초과·도구 실행 실패·원인 기록 없음). 팀 판단 전까지 따로 센다.
  호출 시간 초과(LLM_EXECUTION_TIMEOUT)는 공급자 지연인지 우리 한도인지 기록만으로 가를 수 없어 API 장애로 단정하지 않는다.
"""
from decimal import Decimal, InvalidOperation
from .host_evidence import budget_assessment
from .package_evidence import package_assessment

CLOSED_STATUSES = ('FINISHED', 'ABORTED', 'HUMAN_REVIEW', 'WAITING_INPUT')  # 자동 실행 구간이 닫힌 상태
PAUSED_STATUSES = ('HUMAN_REVIEW', 'WAITING_INPUT')                         # 사람 입력·판단을 기다리며 멈춘 상태


def has_resumed(bundle):
    """원본 사건으로 재개를 확인한다. 파생 outcome 파일이 없거나 오래되어도 확인할 수 있다."""
    return any(event.get('eventType') == 'WORKFLOW_RESUMED'
               for event in (bundle or {}).get('events') or [])

# 코드 → (원인, 비교 유효성). 에이전트 상태 메시지 code, 의미 검토 reasonCode, 호출 영수증 outcome에 같은 이름이 쓰인다.
CODE_CAUSES = {
    'SEMANTIC_REVIEW_INPUT_REQUIRED': ('SEMANTIC_REVIEW_HOLD', 'VALID'),
    'PLANNER_INPUT_REQUIRED': ('PLANNER_QUESTION', 'VALID'),
    'REQUIREMENT_SCOPE_INVALID': ('REQUIREMENT_SCOPE_INVALID', 'VALID'),
    'LLM_PROVIDER_ERROR': ('MODEL_API_FAILURE', 'UNVERIFIED'),
    'LLM_AUTH_REQUIRED': ('MODEL_AUTH_FAILURE', 'UNVERIFIED'),
    'LLM_CONFIGURATION_ERROR': ('MODEL_CONFIGURATION_FAILURE', 'UNVERIFIED'),
    'LLM_EXECUTION_TIMEOUT': ('MODEL_CALL_TIMEOUT', 'REVIEW_REQUIRED'),
    'LLM_BUDGET_EXHAUSTED': ('BUDGET_EXHAUSTED', 'VALID'),
    'LLM_RESPONSE_INVALID': ('MODEL_OUTPUT_INVALID', 'VALID'),
    'LLM_SCHEMA_INVALID': ('MODEL_OUTPUT_INVALID', 'VALID'),
    'LLM_RESPONSE_INCOMPLETE': ('MODEL_OUTPUT_INVALID', 'VALID'),
    'LLM_REFUSED': ('MODEL_REFUSED', 'VALID'),
    'LLM_TOOL_POLICY_VIOLATION': ('TOOL_POLICY_VIOLATION', 'VALID'),
    'LLM_TOOL_EXECUTION_FAILED': ('TOOL_FAILURE', 'REVIEW_REQUIRED'),
}
# 의미 검토가 보류한 사유 중 "내용이 승인 범위를 벗어났거나 근거가 부족함"(검토 호출 오류가 아닌 것)
SEMANTIC_HOLD_REASONS = frozenset({'UNSUPPORTED_MEANING', 'UNAPPROVED_DATA_SCOPE', 'INSUFFICIENT_EVIDENCE',
                                   'UNAPPROVED_SCOPE', 'REVIEW_RESPONSE_INVALID'})
STOP_STATES = frozenset({'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED', 'TASK_STATE_FAILED',
                         'TASK_STATE_REJECTED', 'TASK_STATE_CANCELED'})
RESUME_POLICIES = {'REGENERATE_AND_REVIEW': 'ANSWER_THEN_REVIEW', 'NEW_RUN_REQUIRED': 'NEW_RUN_REQUIRED'}
# 결과 패키지: 지금 dev가 실제로 만드는 산출물 기준(최종 보고서 산출물은 dev에 아직 없다 → limitations에 적는다)
PACKAGE_ARTIFACTS = ('REQUIREMENT', 'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT')
VERSIONED_ARTIFACTS = frozenset({'SOURCE', 'CHANGE_REPORT', 'BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT'})


def _detail(record):
    # 사용량·원격 측정 기록의 값은 detail에 있고 dev 응답에 따라 위쪽에도 복사돼 있다. detail을 먼저 본다.
    detail = record.get('detail') if isinstance(record.get('detail'), dict) else {}
    return lambda key: detail.get(key, record.get(key))


def _number(value):
    # protobuf JSON은 정수도 0.0처럼 실수로 줄 수 있다. 같은 값으로 비교하도록 정수로 맞춘다.
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, list):
        return [_number(item) for item in value]
    return value


def stop_signals(a2a):
    """에이전트별 Task의 마지막 상태 사건 중 멈춤·오류 상태와 그 메시지 code. 시각 순서로 돌려준다."""
    signals = []
    for identity, rows in ((a2a or {}).get('roles') or {}).items():
        latest = {}
        for row in rows.get('events') or []:
            status = (row.get('event') or {}).get('status') or {}
            if row.get('eventType') != 'TaskStatusUpdateEvent' or not status.get('state'):
                continue
            previous = latest.get(row.get('taskId'))
            if previous is None or (row.get('version') or 0) >= (previous[0] or 0):
                latest[row.get('taskId')] = (row.get('version'), status)
        for task_id, (_version, status) in latest.items():
            data = {}
            for part in ((status.get('message') or {}).get('parts') or []):
                if isinstance(part, dict) and isinstance(part.get('data'), dict):
                    data = part['data']
                    break
            if status['state'] not in STOP_STATES and not data.get('code'):
                continue
            review = data.get('semanticReview') if isinstance(data.get('semanticReview'), dict) else {}
            signals.append({'source': 'A2A_STATUS', 'identity': identity, 'taskId': task_id,
                            'state': status['state'], 'at': status.get('timestamp'), 'code': data.get('code'),
                            'questions': len(data['questions']) if isinstance(data.get('questions'), list) else 0,
                            'unapprovedAssumptions': (len(data['unapprovedAssumptions'])
                                                      if isinstance(data.get('unapprovedAssumptions'), list) else 0),
                            'resumePolicy': review.get('resumePolicy'), 'reviewReason': review.get('reasonCode')})
    return sorted(signals, key=lambda item: (item['at'] or '', item['identity'], item['taskId'] or ''))


def semantic_reviews(trace):
    """의미 검토 기록(AGENT_SEMANTIC_REVIEWED). 원격 측정 기록이 없으면 None(모름)."""
    telemetry = (trace or {}).get('telemetry')
    if not isinstance(telemetry, list):
        return None
    reviews = []
    for row in telemetry:
        binding = row.get('binding') or {}
        if binding.get('eventType') != 'AGENT_SEMANTIC_REVIEWED':
            continue
        value = _detail(row)
        sequences = value('modelCallSequences')
        reviews.append({'reviewId': value('reviewId'), 'workflowStepId': binding.get('workflowStepId'),
                        'actor': binding.get('actor'), 'stepRole': value('stepRole'), 'phase': value('phase'),
                        'status': value('status'), 'reasonCode': value('reasonCode'),
                        'resumePolicy': value('resumePolicy'), 'at': binding.get('occurredAt'),
                        'modelCallSequences': [_number(item) for item in sequences] if isinstance(sequences, list) else None})
    return sorted(reviews, key=lambda item: item['at'] or '')


def review_usage(bundle, trace):
    """의미 검토 호출과 작업 호출을 나눈 사용량. 검토가 가리킨 호출 번호가 영수증에 모두 있을 때만 합계를 확정한다."""
    reviews = semantic_reviews(trace)
    usage = (bundle or {}).get('usage') or {}
    records = usage.get('records')
    if reviews is None or not isinstance(records, list):
        return None
    wanted, unknown_sequences = set(), False
    for review in reviews:
        if review['modelCallSequences'] is None:
            unknown_sequences = True
            continue
        for sequence in review['modelCallSequences']:
            if type(sequence) is not int or sequence < 1:
                unknown_sequences = True
            else:
                wanted.add(sequence)
    by_sequence, invalid = {}, set()
    for row in records:
        if not isinstance(row, dict):
            unknown_sequences = True
            continue
        sequence = _number(_detail(row)('sequence'))
        if type(sequence) is not int or sequence < 1:
            unknown_sequences = True
            continue
        if sequence in by_sequence or (row.get('binding') or {}).get('runId') not in (None, bundle['run']['runId']):
            invalid.add(sequence)
        detail = row.get('detail') or {}
        if any(key in row and key in detail and _number(row[key]) != _number(detail[key]) for key in
               ('sequence', 'inputTokens', 'outputTokens', 'totalTokens', 'usageKnown', 'costUsd', 'durationMs')):
            invalid.add(sequence)
        by_sequence[sequence] = {**row, 'detail': {**row, **detail}}
    for sequence in invalid:
        by_sequence.pop(sequence, None)
    found = [by_sequence[key] for key in sorted(wanted) if key in by_sequence]
    summary = usage.get('summary') or {}
    calls = summary.get('modelCalls')
    complete = not unknown_sequences and len(found) == len(wanted) and usage.get('recordsConsistent', True) is True \
        and type(usage.get('total')) is int and usage['total'] == len(records) \
        and set(by_sequence) == set(range(1, len(records) + 1)) \
        and type(calls) is int and calls == len(records) \
        and not summary.get('pendingModelSequences') and not summary.get('missingUsageSequences')
    from .agent_eval import _measurement, _measurement_summary
    tokens_confirmed, cost_confirmed, warnings = _measurement_summary(usage, list(by_sequence.values()))

    def total(key, money=False):
        values = [_measurement(row, key, money=money) for row in found]
        if any(value is None for value in values):
            return None
        if money:
            try:
                return format(sum((Decimal(str(value)) for value in values), Decimal(0)), 'f')
            except InvalidOperation:
                return None
        return sum(values)

    by_role = {}
    for review in reviews:
        role = review['stepRole'] or review['actor'] or 'UNKNOWN'
        entry = by_role.setdefault(role, {'reviews': 0, 'held': 0, 'modelCalls': 0})
        entry['reviews'] += 1
        entry['held'] += review['status'] == 'HELD'
        entry['modelCalls'] += len(review['modelCallSequences'] or ())
    review_calls = len(found) if complete else None
    return {'reviews': len(reviews), 'held': sum(item['status'] == 'HELD' for item in reviews),
            'supported': sum(item['status'] == 'SUPPORTED' for item in reviews),
            'reviewModelCalls': review_calls, 'knownReviewModelCalls': len(found),
            'workModelCalls': calls - review_calls if type(calls) is int and review_calls is not None else None,
            'reviewInputTokens': total('inputTokens') if complete and tokens_confirmed else None,
            'reviewOutputTokens': total('outputTokens') if complete and tokens_confirmed else None,
            'reviewCostUsd': total('costUsd', money=True) if complete and cost_confirmed else None,
            'reviewDurationMs': total('durationMs') if complete else None,
            'complete': complete and not warnings, 'callRecordsComplete': complete,
            'measurementWarnings': warnings, 'byRole': by_role}


def _usage_errors(bundle):
    # 완료되지 않은 호출 영수증(오류 코드). 호출 번호 순서.
    rows = []
    for row in ((bundle or {}).get('usage') or {}).get('records') or []:
        value = _detail(row)
        outcome = value('outcome')
        if outcome not in (None, 'completed'):
            rows.append({'source': 'USAGE', 'identity': value('role'), 'code': outcome,
                         'sequence': _number(value('sequence'))})
    return sorted(rows, key=lambda item: item['sequence'] if type(item['sequence']) is int else -1)


def _cause_of(code):
    if code in CODE_CAUSES:
        return CODE_CAUSES[code]
    if isinstance(code, str) and code.endswith('_INPUT_REQUIRED'):
        return 'AGENT_QUESTION', 'VALID'  # Planner 밖 역할의 질문(의미 검토 보류가 아닌 것)
    return 'AGENT_STOPPED', 'REVIEW_REQUIRED'  # 알 수 없는 코드는 그대로 남기고 판단을 미룬다


def classify_run(bundle, trace=None, a2a=None):
    """한 Run의 끝 상태 원인. trace·a2a가 없으면 Run 상태·호출 영수증만으로 가르고 evidenceComplete=False로 남긴다."""
    run = bundle['run']
    status, verdict = run.get('status'), run.get('verdict')
    events = bundle.get('events') or []
    event_types = [event.get('eventType') for event in events]
    signals = stop_signals(a2a) if a2a is not None else []
    reviews = semantic_reviews(trace) if trace is not None else None
    held = [item for item in (reviews or []) if item['status'] == 'HELD']
    errors = _usage_errors(bundle)
    result = {'schemaVersion': 1, 'runId': run.get('runId'), 'runStatus': status, 'verdict': verdict,
              'resumeState': run.get('resumeState'), 'terminationReason': run.get('terminationReason'),
              'segmentClosed': status in CLOSED_STATUSES, 'cause': None, 'comparisonValidity': None,
              'resume': None, 'stoppedBy': None, 'details': {},
              'evidenceComplete': trace is not None and a2a is not None and reviews is not None,
              'signals': [{key: item.get(key) for key in ('source', 'identity', 'state', 'code')}
                          for item in signals] + [{'source': 'SEMANTIC_REVIEW', 'identity': item['stepRole'],
                                                   'state': item['status'], 'code': item['reasonCode']}
                                                  for item in held] + errors,
              'resumedDuringRun': has_resumed(bundle)}
    if result['resumedDuringRun']:
        # 첫 인계가 재개 뒤에 왔다면 그 최종 코드·누적 사용량은 자동 구간의 결과가 아니다.
        # 이전 자동 구간의 Snapshot/비용/시간을 이 기록만으로 추정하지 않는다.
        return {**result, 'cause': 'ASSISTED_CONTINUATION', 'comparisonValidity': 'ASSISTED',
                'resume': 'TERMINAL' if status in CLOSED_STATUSES else 'OPERATOR_DECISION',
                'stoppedBy': {'source': 'EVENT', 'code': 'WORKFLOW_RESUMED'},
                'details': {'automaticBoundary': 'NOT_CAPTURED_IN_THIS_EXPORT'}}
    if status == 'FINISHED':
        cause = {'SUCCESS': 'COMPLETED_SUCCESS', 'FAIL': 'COMPLETED_FAIL', 'UNVERIFIED': 'COMPLETED_UNVERIFIED',
                 'HUMAN_REVIEW': 'COMPLETED_HUMAN_REVIEW'}.get(verdict, 'COMPLETED_UNKNOWN')
        return {**result, 'cause': cause, 'resume': 'TERMINAL', 'stoppedBy': {'source': 'RUN', 'code': verdict},
                'comparisonValidity': 'VALID' if cause != 'COMPLETED_UNKNOWN' else 'REVIEW_REQUIRED'}
    if status not in CLOSED_STATUSES:
        return {**result, 'cause': 'IN_PROGRESS', 'comparisonValidity': 'REVIEW_REQUIRED'}
    # 가장 직접적인 근거부터: 에이전트가 낸 멈춤 상태 메시지 → 의미 검토 보류 → 호출 영수증 오류
    chosen = next((item for item in reversed(signals) if item['code']), None) or \
        next((item for item in reversed(signals)), None)
    review = held[-1] if held else None
    if chosen is not None and chosen['code']:
        cause, validity = _cause_of(chosen['code'])
        stopped = {key: chosen[key] for key in ('source', 'identity', 'taskId', 'state', 'code')}
        details = {'questions': chosen['questions'], 'unapprovedAssumptions': chosen['unapprovedAssumptions']}
        policy = None
        if cause == 'SEMANTIC_REVIEW_HOLD':
            policy = chosen['resumePolicy'] or (review or {}).get('resumePolicy')
            details.update(reviewReason=chosen['reviewReason'] or (review or {}).get('reasonCode'), resumePolicy=policy)
    elif review is not None:
        reason = review['reasonCode']
        cause, validity = (('SEMANTIC_REVIEW_HOLD', 'VALID') if reason in SEMANTIC_HOLD_REASONS or reason not in CODE_CAUSES
                           else CODE_CAUSES[reason])
        stopped = {'source': 'SEMANTIC_REVIEW', 'identity': review['stepRole'], 'taskId': None,
                   'state': review['status'], 'code': reason}
        policy = review['resumePolicy'] if cause == 'SEMANTIC_REVIEW_HOLD' else None
        details = {'reviewReason': reason, 'resumePolicy': policy} if cause == 'SEMANTIC_REVIEW_HOLD' else {}
    elif errors:
        cause, validity = _cause_of(errors[-1]['code'])
        stopped, policy, details = errors[-1], None, {}
    else:
        cause, validity, policy, details = None, 'REVIEW_REQUIRED', None, {}
        stopped = None
        if chosen is not None:  # 코드 없는 멈춤 상태(예: 취소·실패만 기록)
            stopped = {key: chosen[key] for key in ('source', 'identity', 'taskId', 'state', 'code')}
    if status == 'ABORTED':
        # 중단은 운영자 조작이 끝 원인이다. 그 직전에 보인 멈춤·오류 근거는 details에 남긴다.
        underlying = None if stopped is None else stopped.get('code')
        return {**result, 'cause': 'ABORTED', 'comparisonValidity': 'VALID', 'resume': 'TERMINAL',
                'stoppedBy': {'source': 'RUN', 'code': 'ABORTED'},
                'details': {'terminationReason': run.get('terminationReason'), 'underlyingCode': underlying}}
    if cause is None:
        cause = 'DISPATCH_REVIEW' if 'A2A_DISPATCH_REQUIRES_REVIEW' in event_types else 'UNKNOWN_HOLD'
    if cause == 'SEMANTIC_REVIEW_HOLD':
        resume = RESUME_POLICIES.get(policy, 'OPERATOR_DECISION')
    elif cause in ('PLANNER_QUESTION', 'AGENT_QUESTION'):
        resume = 'ANSWER'
    elif cause == 'MODEL_AUTH_FAILURE':
        resume = 'CREDENTIAL_THEN_RESUME'
    else:
        resume = 'OPERATOR_DECISION'
    return {**result, 'cause': cause, 'comparisonValidity': validity, 'resume': resume, 'stoppedBy': stopped,
            'details': details}


def _condition(status, reason=None):
    return {'status': status, 'reason': reason}


def auto_completion(export, run_document, *, report=None, outcome=None, metrics=None):
    """자동 완료 통과(시험지 v4): 제품 시험 통과 + 내부 SUCCESS + 한도 안 종료 + 결과 패키지 완성 + 사람 개입 없음.

    다섯 조건 중 하나라도 FAIL이면 FAIL, 확인하지 못한 조건이 있으면 UNVERIFIED, 모두 확인되면 PASS다.
    """
    bundle, trace, a2a, handoff = export['bundle'], export.get('trace') or {}, export.get('a2a') or {}, export.get('handoff') or {}
    run = bundle['run']
    outcome = outcome or classify_run(bundle, trace, a2a)
    conditions = {}
    # ① 제품 시험 통과(ACC-02). 독립 보고서가 없거나 미검증이면 통과로 세지 않는다.
    if report is None:
        conditions['productPass'] = _condition('UNVERIFIED', 'NO_INDEPENDENT_REPORT')
    else:
        verdict = report.get('evaluationVerdict')
        conditions['productPass'] = _condition('PASS' if verdict == 'PASS' else 'FAIL' if verdict == 'FAIL'
                                               else 'UNVERIFIED', None if verdict == 'PASS' else f'EVALUATION_{verdict}')
    # ② 내부 SUCCESS
    if run.get('status') not in CLOSED_STATUSES:
        conditions['internalSuccess'] = _condition('UNVERIFIED', 'RUN_IN_PROGRESS')
    else:
        success = run.get('status') == 'FINISHED' and run.get('verdict') == 'SUCCESS'
        conditions['internalSuccess'] = _condition('PASS' if success else 'FAIL',
                                                   None if success else f"{run.get('status')}_{run.get('verdict')}")
    # ③ 한도 안 종료: 한도 초과 근거가 있으면 FAIL. 끝난 Run은 경과 시간과 동결 시간 한도를 비교한다.
    budget = ((((bundle.get('configuration') or {}).get('configuration') or {}).get('limits') or {})
              .get('runtimeBudgetMs'))
    elapsed = (metrics or {}).get('totalMs')
    if outcome['cause'] in ('BUDGET_EXHAUSTED', 'MODEL_CALL_TIMEOUT'):
        conditions['withinLimits'] = _condition('FAIL', outcome['cause'])
    elif run.get('status') != 'FINISHED':
        conditions['withinLimits'] = _condition('FAIL' if run.get('status') in CLOSED_STATUSES else 'UNVERIFIED',
                                                f"ENDED_{run.get('status')}")
    elif type(budget) is not int:
        conditions['withinLimits'] = _condition('UNVERIFIED', 'RUNTIME_BUDGET_UNKNOWN')
    elif type(elapsed) is not int:
        conditions['withinLimits'] = _condition('UNVERIFIED', 'ELAPSED_TIME_UNKNOWN')
    else:
        ledger = budget_assessment(bundle)
        conditions['withinLimits'] = (_condition(ledger['status'], ledger['reason']) if elapsed <= budget
                                     else _condition('FAIL', 'RUNTIME_BUDGET_EXCEEDED'))
    # ④ 결과 패키지: 최종 코드 내보내기·최종 버전 보고서·전체 기록이 모두 있는가(지금 dev 산출물 기준)
    final = (run_document or {}).get('finalCodeVersion')
    exported = (run_document or {}).get('exportedCodeVersions') or []
    artifacts = trace.get('artifacts') if isinstance(trace.get('artifacts'), list) else None
    missing = []
    if artifacts is None:
        package = _condition('UNVERIFIED', 'ARTIFACTS_NOT_IN_EXPORT')
    elif type(final) is not int or final < 1 or final not in exported:
        package = _condition('FAIL', 'FINAL_SNAPSHOT_NOT_EXPORTED')
    else:
        for kind in PACKAGE_ARTIFACTS:
            if not any(item.get('artifactType') == kind and (kind not in VERSIONED_ARTIFACTS or item.get('codeVersion') == final)
                       for item in artifacts):
                missing.append(kind)
        coverage = handoff.get('coverage') or {}
        records_complete = (bundle.get('eventsTotal') == len(bundle.get('events') or [])
                            and bundle.get('snapshotConsistent') is not False
                            and trace.get('telemetryTotal') == len(trace.get('telemetry') or []))
        if missing:
            package = _condition('FAIL', 'MISSING_' + '_'.join(missing))
        elif coverage.get('a2aComplete') is not True or not records_complete:
            package = _condition('UNVERIFIED', 'RECORDS_INCOMPLETE')
        else:
            proof = package_assessment(export)
            package = _condition(proof['status'], proof['reason'] if export.get('packageCompletion') is not None
                                 else 'FINAL_REPORT_AND_PACKAGE_COMPLETION_NOT_EXPORTED')
            if proof['packageMs'] is not None and type(budget) is int and proof['packageMs'] > budget:
                conditions['withinLimits'] = _condition('FAIL', 'PACKAGE_RUNTIME_BUDGET_EXCEEDED')
    conditions['packageComplete'] = package
    # ⑤ 사람 개입 없음: 사건 전체를 받았을 때만 판단한다. 입력 대기·사람 검토·재개가 한 번이라도 있으면 FAIL.
    events = bundle.get('events') or []
    paused = any(event.get('workflowState') in PAUSED_STATUSES for event in events)
    resumed = any(event.get('eventType') == 'WORKFLOW_RESUMED' for event in events)
    asked = any(item['state'] in ('TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_AUTH_REQUIRED') for item in stop_signals(a2a)) \
        if a2a else False
    if paused or resumed or asked or run.get('status') in PAUSED_STATUSES:
        conditions['noHumanIntervention'] = _condition('FAIL', 'PAUSED_OR_RESUMED')
    elif bundle.get('eventsTotal') != len(events) or bundle.get('snapshotConsistent') is False:
        conditions['noHumanIntervention'] = _condition('UNVERIFIED', 'EVENTS_INCOMPLETE')
    else:
        conditions['noHumanIntervention'] = _condition('PASS')
    statuses = [item['status'] for item in conditions.values()]
    overall = 'FAIL' if 'FAIL' in statuses else 'UNVERIFIED' if 'UNVERIFIED' in statuses else 'PASS'
    return {'status': overall, 'conditions': conditions, 'packageDefinition': list(PACKAGE_ARTIFACTS),
            'limitations': ['최종 보고서·패키지 완료 증거가 없는 Run은 패키지 완성을 확정하지 않는다.',
                            '예산 장부가 없는 이전 인계에서는 시간 한도 준수만으로 전체 한도 준수를 확정하지 않는다.']}


def run_outcome(export, run_document=None, *, report=None, metrics=None):
    """workflow가 Run마다 남기는 outcome.json 내용: 원인·의미 검토 사용량·자동 완료."""
    bundle, trace, a2a = export['bundle'], export.get('trace'), export.get('a2a')
    outcome = classify_run(bundle, trace, a2a)
    return {'schemaVersion': 1, 'runId': bundle['run']['runId'], 'outcome': outcome,
            'budgetEvidence': budget_assessment(bundle),
            'packageTiming': package_assessment(export),
            'semanticReview': review_usage(bundle, trace),
            'autoCompletion': auto_completion(export, run_document, report=report, outcome=outcome, metrics=metrics)}
