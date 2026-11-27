"""Run 하나의 정확도·속도·비용·과정·안정성 채점지(axis_scorecard.json·axis_scorecard.md).

요구사항별 채점지(summary.md)·세부 항목 채점지(check_details.md)와 같은 결과 폴더에 쓴다. 이미 계산한 값
(독립 평가 보고서·Run 지표·종료 분류·과정 지표·TIME-05·회귀·Run 기록 검사)만 모으고 새로 측정하지 않는다.
값이 없으면 0으로 바꾸지 않고 미확인(UNVERIFIED)과 사유를 남긴다. 계획 Run 전체가 분모인 비율·분포·변동
(STAB-01~06의 비율, TIME-04, COST-03)은 이 Run이 그 계산에 어떻게 들어가는지만 적고, 값은 비교 결과
(comparison.md)에서 낸다. 지표 ID는 docs/evaluation/final_exam_core.md 5·7절을 따른다.
"""
from typing import Mapping, Optional

from .outcomes import PAUSED_STATUSES
from .scoring import product_accuracy

MEASURED, UNVERIFIED, GROUP_ONLY = 'MEASURED', 'UNVERIFIED', 'GROUP_ONLY'
AXES = (('accuracy', '정확도'), ('speed', '속도'), ('cost', '비용'), ('process', '과정'), ('stability', '안정성'))
STATUS_TEXT = {MEASURED: '측정', UNVERIFIED: '미확인', GROUP_ONLY: '비교에서 계산'}


def _item(metric_id, name, value, *, status=None, reason=None, display=None):
    # 지표 한 줄. status를 주지 않으면 값이 있을 때만 측정으로 본다(없는 값을 0으로 채우지 않는다).
    state = status or (UNVERIFIED if value is None else MEASURED)
    return {'id': metric_id, 'name': name, 'status': state, 'value': value,
            'reason': None if state == MEASURED else reason, 'display': display}


def _group(metric_id, name):
    return _item(metric_id, name, None, status=GROUP_ONLY, display='계획 Run 전체로 comparison.md에서 계산')


def _ms(value):
    return None if value is None else f'{value:,} ms ({value / 1000:.1f}초)'


def _rate(value):
    return None if value is None else f'{value:.3f}'


def _accuracy(report, score, trace, reason):
    names = (('ACC-01', '요구사항 완전 충족률'), ('ACC-02', '제품 시험 통과(9개 모두 PASS)'), ('ACC-03', '보안 검사 결과'),
             ('ACC-04', '검사 판정 가능률'), ('ACC-05', '전체 요구 검증 범위'))
    if report is None:
        return [_item(i, n, None, reason=reason) for i, n in names]
    acc = product_accuracy(report)
    if acc.get('reason'):
        return [_item(i, n, None, reason=acc['reason']) for i, n in names]
    count, passed = acc['requirementCount'], acc['passedRequirements']
    lower, upper = acc['fullSatisfactionLower'], acc['fullSatisfactionUpper']
    titles = {row.get('testId'): (row.get('title') or '').split(' ', 1)[0] for row in report.get('checks', [])}
    security = acc['securityChecks']
    exposure = (trace or {}).get('exposure') or {}
    traceability = (trace or {}).get('traceability') or {}
    scope = {'runRecordExposure': exposure.get('status'),
             'notCollectedOutputs': list((exposure.get('coverage') or {}).get('notCollectedOutputs') or []),
             'traceability': traceability.get('status')}
    items = [
        _item('ACC-01', names[0][1], {'lower': lower, 'upper': upper, 'passed': passed, 'failed': acc['failedRequirements'],
                                       'unverified': acc['unverifiedRequirements'], 'requirements': count},
              display=f'하한 {_rate(lower)} · 상한 {_rate(upper)} (충족 {passed}/{count} · 실패 {acc["failedRequirements"]} · '
                      f'미검증 {acc["unverifiedRequirements"]})'),
        _item('ACC-02', names[1][1], acc['productPass'], display='통과' if acc['productPass'] else '통과 아님'),
        _item('ACC-03', names[2][1], security,
              display=' · '.join(f'{titles.get(row["testId"], row["testId"])} {row["status"]}' for row in security)),
        _item('ACC-04', names[3][1], acc['checkDecidability'],
              display=f'{acc["decidedChecks"]}/{acc["checkCount"]} ({acc["checkDecidability"] * 100:.1f}%)'
              if acc['checkDecidability'] is not None else None),
        _item('ACC-05', names[4][1], scope if trace else None, reason='TRACE_CHECK_UNAVAILABLE',
              display=f'REQ-006 Run 기록 노출 {scope["runRecordExposure"]}'
                      f'(미수집 출력 {len(scope["notCollectedOutputs"])}종) · REQ-008 추적 {scope["traceability"]}'
              if trace else None),
        _item('보조', 'QA 통과율', acc['qaPassRate'], display=_rate(acc['qaPassRate'])),
        _item('보조', '보안 차단 결함 수', acc['blockingSecurityFindings'],
              display=None if acc['blockingSecurityFindings'] is None else f'{acc["blockingSecurityFindings"]}건'),
    ]
    if score is not None:
        items.append(_item('보조', '요구사항별 부분점수(ACC-01과 별개)', score.get('score'), reason=score.get('reason'),
                           display=None if score.get('score') is None else
                           f'{_rate(score["score"])} · 상한 {_rate(score.get("scoreUpperBound"))} · '
                           f'검사 완료율 {score["completionRate"] * 100:.1f}%'))
    return items


def _speed(metrics, record, timing, reason):
    package = (record or {}).get('packageTiming') or {}
    items = [
        _item('TIME-01', '개발·검증 판정 시간', metrics.get('verdictMs'), reason='VERDICT_TIME_NOT_RECORDED',
              display=_ms(metrics.get('verdictMs'))),
        _item('TIME-02', '결과 패키지 완료 시간', package.get('packageMs'),
              reason=package.get('reason') or 'PACKAGE_TIMING_UNAVAILABLE', display=_ms(package.get('packageMs'))),
        _item('TIME-03', '첫 빌드 성공 시간', metrics.get('firstBuildPassMs'), reason='FIRST_BUILD_PASS_NOT_RECORDED',
              display=_ms(metrics.get('firstBuildPassMs'))),
    ]
    timing = timing or {}
    if timing.get('firstPassingSnapshotMs') is not None:
        items.append(_item('TIME-05', '보존 Snapshot 기준 최초 통과 시간', timing['firstPassingSnapshotMs'],
                           display=f'{_ms(timing["firstPassingSnapshotMs"])} · v{timing.get("firstPassingVersion")}에서 처음 통과'))
    elif timing.get('status') == 'PASS' and timing.get('reason') == 'NO_PASSING_SNAPSHOT':
        # 보존된 모든 버전을 채점했고 9개를 모두 통과한 버전이 없다는 것을 확인한 경우(측정된 사실).
        items.append(_item('TIME-05', '보존 Snapshot 기준 최초 통과 시간', None, status=MEASURED,
                           display='통과한 버전 없음'))
    else:
        items.append(_item('TIME-05', '보존 Snapshot 기준 최초 통과 시간', None,
                           reason=timing.get('reason') or reason or 'TIME05_UNAVAILABLE'))
    ended = metrics.get('endedBy')
    items += [
        _item('TIME-06', '종료·중단까지 경과 시간', metrics.get('totalMs'), reason='TOTAL_TIME_NOT_RECORDED',
              display=None if metrics.get('totalMs') is None else
              _ms(metrics['totalMs']) + (' · 멈춘 시점까지' if ended == 'PAUSED' else '')),
        _item('참고', '사람을 기다린 시간', metrics.get('humanWaitMs'), reason='HUMAN_WAIT_NOT_RECORDED',
              display=_ms(metrics.get('humanWaitMs'))),
        _group('TIME-04', '제품 통과 Run의 판정 시간 분포'),
    ]
    return items


def _cost(metrics, record):
    calls = {key: metrics.get(key) for key in ('modelCalls', 'toolCalls', 'inputTokens', 'outputTokens', 'totalTokens',
                                               'knownTotalTokens', 'tokenBreakdownComplete')}
    tokens = calls['totalTokens']
    split_known = (calls['tokenBreakdownComplete'] is True
                   and type(calls['inputTokens']) is int and type(calls['outputTokens']) is int)
    breakdown = (f' (입력 {calls["inputTokens"]:,} / 출력 {calls["outputTokens"]:,})'
                 if split_known else ' (입력·출력 구분 미확인)')
    usage_known = (calls['modelCalls'] is not None and tokens is not None
                   and (calls['tokenBreakdownComplete'] is not True or split_known))
    book = metrics.get('priceBook') or {}
    complete = metrics.get('costComplete') is True
    cost, known = metrics.get('costUsd'), metrics.get('knownCostUsd')
    review = (record or {}).get('semanticReview') or {}
    items = [
        _item('COST-01', '토큰·호출 수', calls if usage_known else None, reason='USAGE_INCOMPLETE',
              display=f'모델 {calls["modelCalls"]}회 · 도구 {calls["toolCalls"]}회 · 토큰 {tokens:,}{breakdown}'
              if usage_known else None),
        _item('COST-02', 'API 비용', cost if complete and cost is not None else None,
              reason='PRICE_BOOK_NOT_RECORDED' if not book else 'COST_INCOMPLETE',
              display=f'${cost} {book.get("currency") or "USD"} · 가격표 {book.get("priceBookId")} {book.get("priceBookVersion")}'
              if complete and cost is not None else
              (f'확인된 부분합 ${known}' if known is not None else None)),
        _item('COST-04', '비용 완전성', complete, display='모든 호출의 비용 확인' if complete else '비용을 모르는 호출 있음'),
        _item('참고', '의미 검토 호출 수', review.get('reviewModelCalls'), reason='REVIEW_USAGE_UNAVAILABLE',
              display=None if review.get('reviewModelCalls') is None else
              f'{review["reviewModelCalls"]}회(작업 호출 {review.get("workModelCalls")}회) · 보류 {review.get("held")}건'),
        _group('COST-03', '제품 통과 1회당 비용'),
    ]
    return items


def _process(metrics, process, regression, trace, tables, reason):
    process = process or {}
    repairs, retries = process.get('repairs') or {}, process.get('retries') or {}
    gates, conflicts = process.get('gateRejections') or {}, process.get('conflicts') or {}

    def routes():
        parts = []
        for route, label in (('IMPLEMENTATION', '구현 수정'), ('REPLAN', '재계획'), ('TEST_CHANGE', '테스트 수정')):
            row = (repairs.get('routes') or {}).get(route) or {}
            parts.append(f'{label} ' + ('미확인' if row.get('attempted') is None else f'시도 {row["attempted"]}·완료 {row["completed"]}'))
        return ' · '.join(parts)

    channels = retries.get('channels') or {}
    items = [
        _item('참고', '수정 횟수', metrics.get('fixAttempts'), reason='FIX_ATTEMPTS_NOT_RECORDED',
              display=None if metrics.get('fixAttempts') is None else
              f'{metrics["fixAttempts"]}회 · 수정 요청 Issue {metrics.get("issuesFixRequested")}건 · '
              f'재검증 통과 {metrics.get("issuesFixed")}건'),
        _item('PROC-01', '수정 경로', repairs or None, status=MEASURED if repairs.get('status') == 'PASS' else UNVERIFIED,
              reason=repairs.get('reason') or 'PROCESS_RECORDS_UNAVAILABLE', display=routes() if repairs else None),
        _item('PROC-02', '재시도·관문 거부', {'retries': retries, 'gateRejections': gates} if retries or gates else None,
              status=MEASURED if retries.get('status') == 'PASS' and gates.get('status') == 'PASS' else UNVERIFIED,
              reason=retries.get('reason') or gates.get('reason') or 'PROCESS_RECORDS_UNAVAILABLE',
              display=' · '.join(f'{label} ' + ('미확인' if channels.get(key) is None else f'{channels[key]}회')
                                 for key, label in (('TOOL', '도구 재시도'), ('A2A', 'A2A 재시도'), ('LLM', 'LLM 재시도')))
              + ' · 관문 거부 ' + ('미확인' if gates.get('count') is None else f'{gates["count"]}건')),
    ]
    if tables is not None and 'firstProblem' in tables and (
            tables['firstProblem'] is not None or tables.get('handoffs') or tables.get('timing')):
        first = tables.get('firstProblem')
        items.append(_item('PROC-03', '오류 발생 지점', first, status=MEASURED,
                           display='기록된 단계에서 오류 미발견' if first is None else
                           f'{first.get("role")} · {first.get("kind")} {first.get("status")}'
                           + ('' if first.get('codeVersion') is None else f' · v{first["codeVersion"]}')))
    else:
        items.append(_item('PROC-03', '오류 발생 지점', None,
                           reason='AGENT_TABLES_UNAVAILABLE' if tables is None else 'NO_OBSERVED_STEPS'))
    if regression is not None:
        items.append(_item('PROC-04', '회귀 후보(버전 간 PASS → FAIL)', regression.get('regressionCount'),
                           status=MEASURED if regression.get('regressionCount') is not None else UNVERIFIED,
                           reason='REGRESSION_UNAVAILABLE',
                           display=f'{"" if regression.get("complete") else "최소 "}{regression.get("regressionCount")}건 · '
                                   f'비교한 버전 쌍 {regression.get("comparedVersionPairs")}개 · '
                                   f'비교 못 한 검사 {regression.get("uncomparedChecks")}개'))
    else:
        items.append(_item('PROC-04', '회귀 후보(버전 간 PASS → FAIL)', None, reason=reason or 'REGRESSION_UNAVAILABLE'))
    for metric_id, name, key in (('PROC-05', '추적 가능성(REQ-008)', 'traceability'),
                                 ('PROC-06', '출력 노출 점검(REQ-006 Run 기록)', 'exposure')):
        check = (trace or {}).get(key) or {}
        items.append(_item(metric_id, name, check.get('status'), reason=reason or 'TRACE_CHECK_UNAVAILABLE',
                           display=None if not check else
                           check['status'] + (f' ({", ".join(check.get("reasons") or [])})' if check.get('reasons') else '')))
    items.append(_item('PROC-07', '충돌', conflicts.get('count'),
                       status=MEASURED if conflicts.get('status') == 'PASS' else UNVERIFIED,
                       reason=conflicts.get('reason') or 'PROCESS_RECORDS_UNAVAILABLE',
                       display=None if conflicts.get('count') is None else f'{conflicts["count"]}건'))
    return items


def _stability(metrics, record, status, product_pass):
    outcome = (record or {}).get('outcome') or {}
    auto = (record or {}).get('autoCompletion') or {}
    run_status = outcome.get('runStatus') or metrics.get('workflowStatus')
    conditions = auto.get('conditions') or {}
    labels = {'productPass': '제품 통과', 'internalSuccess': '내부 SUCCESS', 'withinLimits': '한도 안 종료',
              'packageComplete': '패키지 완성', 'noHumanIntervention': '사람 개입 없음'}
    flags = {'evaluation': status.get('status'), 'aborted': run_status == 'ABORTED',
             'paused': run_status in PAUSED_STATUSES, 'unverifiedVerdict': outcome.get('verdict') in (None, 'UNVERIFIED')}
    state_known = bool(run_status) and status.get('status') is not None
    return [
        _item('분류', '종료 분류', {key: outcome.get(key) for key in ('runStatus', 'verdict', 'cause', 'comparisonValidity', 'resume')}
              if outcome else None, reason='OUTCOME_UNAVAILABLE',
              display=f'{run_status} · 내부 판정 {outcome.get("verdict")} · 원인 {outcome.get("cause")} · '
                      f'비교 유효성 {outcome.get("comparisonValidity")}' if outcome else None),
        _item('STAB-01', '제품 시험 통과(통과율 분자에 들어가는가)', product_pass,
              display='들어감' if product_pass else '들어가지 않음'),
        _item('STAB-06', '자동 완료(자동 완료율 분자에 들어가는가)', auto.get('status'),
              status=MEASURED if auto.get('status') in ('PASS', 'FAIL') else UNVERIFIED,
              reason='AUTO_COMPLETION_CONDITION_UNVERIFIED',
              display=f'{auto.get("status")} · ' + ' · '.join(f'{labels.get(k, k)} {v.get("status")}' for k, v in conditions.items())
              if auto else None),
        _item('STAB-02', '미검증·중단·멈춤 여부', flags if state_known else None,
              reason='STABILITY_STATE_NOT_RECORDED',
              display=f'채점 {flags["evaluation"]} · 중단 {"예" if flags["aborted"] else "아니오"} · '
                      f'멈춤 {"예" if flags["paused"] else "아니오"}' if state_known else None),
        _item('참고', 'Run 중 사람 보조 재개', metrics.get('resumedDuringRun'), reason='RESUME_RECORD_UNAVAILABLE',
              display=None if metrics.get('resumedDuringRun') is None else
              ('있음' if metrics['resumedDuringRun'] else '없음')),
        _group('STAB-01·06', '제품 통과율·자동 완료율'),
        _group('STAB-03·04·05', '정확도·시간 변동, Single/Multi 짝 비교'),
    ]


def build_scorecard(*, run_id: str, architecture: Optional[str], exam: str, metrics: Mapping, record: Mapping,
                    status: Mapping, report: Optional[Mapping] = None, score: Optional[Mapping] = None,
                    trace: Optional[Mapping] = None, timing: Optional[Mapping] = None,
                    regression: Optional[Mapping] = None, process: Optional[Mapping] = None,
                    tables: Optional[Mapping] = None, conflicts: Optional[Mapping] = None) -> dict:
    """채점한 Run은 report·score·trace·timing·regression을, 채점하지 못한 Run은 status의 사유를 쓴다."""
    reason = None if report is not None else (status.get('reason') or 'NOT_SCORED')
    accuracy = _accuracy(report, score, trace, reason)
    product_pass = next((row['value'] for row in accuracy if row['id'] == 'ACC-02'), None) is True
    axes = {'accuracy': accuracy, 'speed': _speed(metrics, record, timing, reason), 'cost': _cost(metrics, record),
            'process': _process(metrics, process, regression, trace, tables, reason),
            'stability': _stability(metrics, record, status, product_pass)}
    return {'schemaVersion': 1, 'kind': 'RUN_AXIS_SCORECARD', 'runId': run_id, 'architecture': architecture,
            'loopPolicy': metrics.get('loopPolicy'), 'exam': exam, 'evaluationStatus': status.get('status'),
            'evaluationReason': status.get('reason'),
            'evaluationId': None if report is None else report.get('evaluationId'),
            'axes': axes, 'conflictAnalysis': None if conflicts is None else dict(conflicts),
            'notes': ['값은 이미 계산된 결과(output.json·outcome.json·timing_metrics.json·process_metrics.json·'
                      'regression.json·bundle.json·conflict_analysis.json)에서 모았다. 새로 측정하지 않는다.',
                      '미확인은 0이 아니라 값을 확인하지 못했다는 뜻이다. 사유 코드를 함께 남긴다. 미확인이어도 일부 확인된 '
                      '값(예: PROC-01의 구현 수정 횟수)은 그대로 보여 준다.',
                      '비율·분포·변동(STAB-01~06의 비율, TIME-04, COST-03)은 계획 Run 전체를 분모로 comparison.md에서 계산한다.',
                      '정확도의 주 지표는 ACC-01(요구사항 완전 충족률)이다. 요구사항별 부분점수는 보조 지표다.']}


def _cell(value):
    text = '' if value is None else str(value)
    return text.replace('|', '\\|').replace('\n', ' ')


def render_scorecard(data: Mapping) -> str:
    lines = ['# 정확도·속도·비용·과정·안정성 채점지', '',
             f'- Run `{_cell(data["runId"])}` · 구조 {_cell(data.get("architecture"))} · 순환 방식 {_cell(data.get("loopPolicy"))}',
             f'- 시험지 {_cell(data.get("exam"))} · 채점 상태 {_cell(data.get("evaluationStatus"))}'
             + (f' ({_cell(data["evaluationReason"])})' if data.get('evaluationReason') else '')
             + (f' · 채점 ID `{_cell(data["evaluationId"])}`' if data.get('evaluationId') else ''), '']
    lines += ['- ' + _cell(note) for note in data['notes']]
    for key, title in AXES:
        lines += ['', f'## {title}', '', '| ID | 지표 | 값 | 상태 | 사유 |', '|---|---|---|---|---|']
        for row in data['axes'][key]:
            shown = row['display'] if row['display'] is not None else ('미확인' if row['status'] == UNVERIFIED else '—')
            lines.append(f'| {_cell(row["id"])} | {_cell(row["name"])} | {_cell(shown)} | '
                         f'{STATUS_TEXT[row["status"]]} | {_cell(row["reason"]) or "—"} |')
        if key == 'accuracy' and data.get('conflictAnalysis') is not None:
            conflict = data['conflictAnalysis']
            lines += ['', '### 정책 제외 충돌(ACC와 별도)', '',
                      '| 제외 수 | 제품 결함 확인 | 시험 결함 확인 | 미확정 |', '|---|---|---|---|',
                      '| ' + ' | '.join('미확인' if conflict[name] is None else str(conflict[name]) for name in
                          ('excluded', 'productDefectConfirmed', 'testDefectConfirmed', 'undetermined')) + ' |', '',
                      f'집계 상태: {_cell(conflict["status"])}' +
                      (f' · {_cell(conflict["reason"])}' if conflict['reason'] else ''), '',
                      '기록된 정책 제외 사건을 집계한다. 상세 항목과 재현 검토 근거: [충돌 분석](conflict_analysis.md).']
    return '\n'.join(lines) + '\n'
