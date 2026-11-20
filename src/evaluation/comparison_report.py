"""실제 집계 결과를 발표·검토용 비교표로 렌더링한다. 누락을 0으로 채우지 않는다."""

CAUSE_TEXT = {
    'COMPLETED_SUCCESS': '내부 SUCCESS로 종료', 'COMPLETED_FAIL': '내부 FAIL로 종료',
    'COMPLETED_UNVERIFIED': '내부 미검증으로 종료', 'COMPLETED_HUMAN_REVIEW': '사람 검토 판정으로 종료',
    'SEMANTIC_REVIEW_HOLD': '의미 검토 보류', 'PLANNER_QUESTION': 'Planner 질문(입력 대기)',
    'AGENT_QUESTION': '에이전트 질문', 'REQUIREMENT_SCOPE_INVALID': '요구사항 범위 위반',
    'MODEL_API_FAILURE': '모델 API 장애', 'MODEL_AUTH_FAILURE': '모델 인증 문제',
    'MODEL_CONFIGURATION_FAILURE': '모델 설정 문제', 'MODEL_CALL_TIMEOUT': '모델 호출 시간 초과',
    'BUDGET_EXHAUSTED': '호출·토큰 한도 초과', 'MODEL_OUTPUT_INVALID': '모델 출력 형식 오류',
    'MODEL_REFUSED': '모델 거절', 'TOOL_POLICY_VIOLATION': '도구 권한 위반', 'TOOL_FAILURE': '도구 실행 실패',
    'ABORTED': '중단', 'DISPATCH_REVIEW': '전달 실패 검토', 'UNKNOWN_HOLD': '원인 기록 없음',
    'AGENT_STOPPED': '알 수 없는 코드로 멈춤', 'IN_PROGRESS': '진행 중',
    'ASSISTED_CONTINUATION': '사람 보조 재개 결과(자동 비교에서 분리)',
}


def render_comparison(result):
    groups = result.get('summary') or {}
    names = list(groups)
    cell = lambda value: '미확인' if value is None else str(value).replace('|', '\\|').replace('\n', ' ')
    lines = ['# 공통 평가 비교 결과', '',
             f"- 시험지: {cell(result.get('exam'))}",
             f"- 비교 기준: {cell(result.get('groupBy'))}",
             f"- 계획 Run 수 확인: {'예' if result.get('plannedRunsKnown') else '아니요'}"]
    selection = result.get('selection') or {}
    for key in ('architecture', 'loopPolicy'):
        if selection.get(key) is not None:
            lines.append(f"- 고정 조건: {key} = {cell(selection[key])}")
    if selection.get('architecture') is not None or selection.get('loopPolicy') is not None:
        lines.append('- 계획 Run 수와 모든 비율의 분모는 위 고정 조건에 해당하는 계획 전체다.')
        lines.append(f"- 다른 조건으로 제외한 결과 {len(selection.get('excludedRunIds', []))}개 · "
                     f"실행 원장 슬롯 {selection.get('excludedExecutionSlots', 0)}개")
    validity = result.get('validity')
    ready = (result.get('plannedRunsKnown') and validity is not None and validity.get('valid')
             and not validity.get('violations')
             # 구조 비교·버전 비교 모두 한도·도구 동일 확인과 Run별 고정 정책 확인이 필요하다(점검 5)
             and (result.get('toolParity') or {}).get('status') == 'PASS'
             and (result.get('toolParity') or {}).get('pinnedRunsVerified') is True
             and (result.get('toolParity') or {}).get('groupBy', 'architecture') == result.get('groupBy')
             and not (result.get('readiness') or {}).get('blockers'))
    parity = result.get('toolParity') or {}
    if parity:
        lines.append(f"- 도구·LLM 한도 동일 확인: {cell(parity.get('status'))}")
    lines += [f"- 비교 조건: {'검사 통과' if ready else '공식 우열 결론 유보'}", '',
              '| 지표 | ' + ' | '.join(cell(name) for name in names) + ' |',
              '|---|' + '---|' * len(names)]

    def nested(item, *keys):
        for key in keys:
            item = item.get(key) if isinstance(item, dict) else None
        return item

    count = lambda value: None if value is None else len(value)
    fields = [
        ('계획 Run 수', ('plannedRuns',)), ('기록 Run 수', ('recordedRuns',)), ('미시작 Run 수', ('notStarted',)),
        ('결과 미수집 슬롯 수', ('unrecordedRuns',)),
        ('시작 확인·결과 미수집 Run 수', ('startedWithoutResults',)),
        ('실행 여부 미확인 슬롯 수', ('unknownExecutionSlots',)),
        ('사람 보조 재개로 분리된 Run 수', ('quarantinedAssistedRuns',)),
        ('자동 구간 독립 채점 Run 수', ('evaluation', 'evaluatedRuns')),
        ('내부 SUCCESS 비율', ('successRate',)), ('내부 판정 가능 비율', ('verifiedRate',)),
        ('최종 코드 미채점 Run 수', ('evaluation', 'eligibleRunsMissingGrade')),
        ('독립 제품 시험 통과 비율(STAB-01)', ('evaluation', 'qualifiedRate')),
        ('계획 전체에서 확인된 제품 통과 비율 하한', ('evaluation', 'qualifiedRateLowerBound')),
        ('자동 완료율(STAB-06)', ('autoCompletion', 'rate')),
        ('자동 완료 미확인·기록 없음 Run 수', ('autoCompletion',), lambda item: None if not item
         else item.get('unverified', 0) + item.get('notRecorded', 0)),
        ('요구사항 완전 충족률 하한 평균(ACC-01)', ('accuracy', 'meanFullSatisfactionLower')),
        ('요구사항 완전 충족률 상한 평균(ACC-01)', ('accuracy', 'meanFullSatisfactionUpper')),
        ('충족률 하한 중앙값·최소·최대(STAB-03)', ('accuracy', 'fullSatisfactionLower'), lambda item: None
         if not item or item.get('median') is None else f"{item['median']:.3f} · {item['min']:.3f} · {item['max']:.3f}"),
        ('미검증 요구사항이 있는 Run 수', ('accuracy', 'runsWithUnverifiedRequirements')),
        ('미검증 중 확인된 실패가 있는 요구사항 수', ('accuracy', 'unverifiedWithConfirmedFailures')),
        ('검사 판정 가능률 평균(ACC-04)', ('accuracy', 'meanCheckDecidability')),
        ('QA 통과율 평균', ('accuracy', 'meanQaPassRate')),
        ('보안 차단 결함 수(확인된 보안 필수 검사 실패)', ('accuracy', 'blockingSecurityFindings')),
        ('검사 통과율 평균', ('evaluation', 'meanCheckPassRate')),
        ('보안 위반 확인 Run 수', ('evaluation', 'securityViolationRuns')),
        ('중단 비율', ('stability', 'abortedRate')),
        ('멈춤(입력 대기·사람 검토) 비율', ('stability', 'pausedRate')),
        ('외부 시험 미검증 비율', ('stability', 'externalUnverifiedRate')),
        ('전체 Run 판정 시간 중앙값(ms)', ('stability', 'allVerdictMs', 'median')),
        ('제품 통과 Run 판정 시간 중앙값(ms)', ('evaluation', 'qualifiedVerdictMs', 'median')),
        ('제품 통과 Run 판정 시간 표본 수', ('evaluation', 'qualifiedVerdictMs', 'n')),
        ('결과 패키지 완료 시간 중앙값(ms, TIME-02)', ('stability', 'packageMs', 'median')),
        ('패키지 완료 시간 표본 수', ('stability', 'packageMs', 'n')),
        ('패키지 완료 시간 미검증 Run 수', ('stability', 'packageUnverifiedRuns')),
        ('최초 통과 Snapshot 산출 시간 중앙값(ms, TIME-05)', ('stability', 'firstPassingSnapshotMs', 'median')),
        ('최초 통과 시간 표본 수', ('stability', 'firstPassingSnapshotMs', 'n')),
        ('최초 통과 여부·시간 미검증 Run 수', ('stability', 'firstPassUnverifiedRuns')),
        ('수정 주기 합계(PROC-01)', ('process', 'repairRounds', 'total')),
        ('구현 수정 시도 합계', ('process', 'IMPLEMENTATIONattempted', 'total')),
        ('구현 수정 완료 합계', ('process', 'IMPLEMENTATIONcompleted', 'total')),
        ('재계획 시도 합계', ('process', 'REPLANattempted', 'total')),
        ('재계획 완료 합계', ('process', 'REPLANcompleted', 'total')),
        ('테스트 수정 시도 합계', ('process', 'TEST_CHANGEattempted', 'total')),
        ('테스트 수정 완료 합계', ('process', 'TEST_CHANGEcompleted', 'total')),
        ('관문 거부 합계(PROC-02)', ('process', 'gateRejections', 'total')),
        ('A2A 재시도 합계(PROC-02)', ('process', 'A2ARetries', 'total')),
        ('LLM 재시도 합계(PROC-02)', ('process', 'LLMRetries', 'total')),
        ('도구 재시도 합계(추가 기록)', ('process', 'TOOLRetries', 'total')),
        ('충돌 합계(PROC-07)', ('process', 'conflicts', 'total')),
        ('충돌 중 판정 모순 합계', ('process', 'contradictions', 'total')),
        ('수정 횟수 평균', ('meanFixAttempts',)), ('도구 재시도 평균', ('meanToolRetries',)),
        ('입력 토큰', ('inputTokens',)), ('출력 토큰', ('outputTokens',)), ('전체 토큰', ('totalTokens',)),
        ('그중 의미 검토 호출 수', ('semanticReview', 'reviewModelCalls')),
        ('의미 검토 보류 수', ('semanticReview', 'held')),
        ('API 비용(USD)', ('costUsd',)), ('확인된 비용 부분합(USD)', ('knownCostUsd',)),
        ('제품 통과 1회당 비용(USD)', ('costPerProductPassUsd',)),
        ('환경 장애로 미검증 처리할 Run 수', ('outcomes', 'environmentFailureRuns'), count),
        ('외부 채점 없는 Run 수', ('evaluation', 'unevaluatedRuns')),
    ]
    for field in fields:
        title, keys = field[0], field[1]
        show = field[2] if len(field) > 2 else (lambda value: value)
        lines.append('| ' + title + ' | ' + ' | '.join(cell(show(nested(groups[name], *keys))) for name in names) + ' |')
    lines += ['', '비율은 0~1이다. 내부 SUCCESS와 독립 제품 시험 통과, 자동 완료 통과는 서로 다른 결과다.',
              '요구사항 완전 충족률은 하한(미검증=미충족)과 상한(확인된 실패가 없는 미검증=충족)을 함께 본다.',
              '통과 1회당 비용에는 실패·미검증·중단 Run 비용도 포함한다. 통과 0회 또는 비용 누락이면 미확인이다.',
              '속도는 통과율·표본 수와 함께 해석한다. 미시작·미검증을 분모에서 제외하지 않는다.',
              '정확도 평균은 독립 채점된 자동 구간의 부분집합이다. 계획·채점 Run 수와 함께 해석한다.']
    package_samples = sum((nested(groups[name], 'stability', 'packageMs', 'n') or 0) for name in names)
    lines += [('결과 패키지 완료 시간(TIME-02)은 검증된 패키지 완료 증거가 있는 Run만 집계했다.'
               if package_samples else
               '결과 패키지 완료 시간(TIME-02)은 검증된 패키지 완료 시간 표본이 없어 측정하지 못했다.'), '']
    lines += _assisted_section(groups, names, cell)
    missing = [row for row in result.get('runs', []) if row.get('scoring') in ('RESULT_NOT_COLLECTED', 'SUBMISSION_UNCONFIRMED')]
    if missing:
        lines += ['', '## 실행 원장에서 확인한 미수집 슬롯', '',
                  '| 구조 | 순서 | Run | 상태·사유 |', '|---|---|---|---|']
        for row in missing:
            lines.append(f"| {cell(row.get('architecture'))} | {cell(row.get('executionOrder'))} | "
                         f"{cell(row.get('runId'))} | {cell(row.get('scoring'))}: {cell(row.get('cause'))} |")
        lines.append('')
    for name in names:
        regression = (result.get('regression') or {}).get(name) or {}
        lines.append(f"- {cell(name)} 버전 간 실패 전환(회귀 후보): {cell(regression.get('totalRegressions'))}")
    lines += _outcome_section(groups, names, cell)
    lines += _pair_section(result.get('pairComparison'), cell)
    for blocker in (result.get('readiness') or {}).get('blockers', []):
        lines.append(f'- 공식 비교 보류: {cell(blocker)}')
    if 'FINAL_CODE_NOT_GRADED' in (result.get('readiness') or {}).get('blockers', []):
        lines.append('- 최종 코드 미채점. 독립 제품 통과율은 보류하며 수집된 실행 시간·비용과 확인된 통과 하한을 표시한다.')
    if validity:
        lines += ['', '## 조건 불일치·주의']
        for entry in validity.get('violations', []) + validity.get('warnings', []):
            runs = entry.get('runs')
            suffix = f" ({len(runs)}개 Run)" if runs else ''
            lines.append(f"- {cell(entry.get('field'))}: {cell(entry.get('severity') or entry.get('detail'))}{suffix}")
    lines += ['', '비교 조건 검사만으로 실제 실행·측정의 완전성이 보장되지는 않는다. 원본 Run 기록과 검사 증거를 함께 보관한다.']
    return '\n'.join(lines) + '\n'


def _assisted_section(groups, names, cell):
    diagnostics = [(name, item) for name in names for item in groups[name].get('assistedDiagnostics', [])]
    if not diagnostics:
        return []
    lines = ['', '## 사람 보조 재개 진단(자동 비교 제외)', '',
             '이 Run은 시작한 계획 슬롯으로 유지한다. 재개 뒤 최종 코드·시간·사용량을 자동 구간 값으로 대체하지 않는다.',
             '아래 지출·시간은 재개 이후를 포함한 전체 기록이다. 자동 구간 경계의 값은 미확인으로 남긴다.', '',
             '| 그룹 | Run | 전체 기록 경과 시간(ms) | 전체 기록 확인 비용(USD) |', '|---|---|---|---|']
    for name, item in diagnostics:
        lines.append(f"| {cell(name)} | {cell(item.get('runId'))} | {cell(item.get('totalMs'))} | "
                     f"{cell(item.get('knownCostUsd'))} |")
    return lines + ['']


def _outcome_section(groups, names, cell):
    # 끝 상태 원인 분포. 기록이 있는 그룹만 보인다.
    causes = sorted({cause for name in names
                     for cause in (((groups[name].get('outcomes') or {}).get('causes')) or {})})
    if not causes:
        return []
    lines = ['', '## 끝 상태 원인(Run 수)', '', '| 원인 | ' + ' | '.join(cell(name) for name in names) + ' |',
             '|---|' + '---|' * len(names)]
    for cause in causes:
        values = [((groups[name].get('outcomes') or {}).get('causes') or {}).get(cause, 0) for name in names]
        lines.append(f"| {CAUSE_TEXT.get(cause, cell(cause))} | " + ' | '.join(str(value) for value in values) + ' |')
    lines.append('')
    lines.append('모델 API·인증·설정 문제는 개발정의서 11-A대로 미검증 Run으로 따로 표시한다. 호출 시간 초과·도구 실행 실패는'
                 ' 원인을 하나로 정할 수 없어 검토 대상으로 남긴다.')
    return lines


def _pair_section(pairs, cell):
    # STAB-05 짝 비교(Multi − Single). 쌍 양쪽 값이 확인된 지표만 계산한다.
    if not pairs:
        return []
    titles = {'fullSatisfactionLower': '충족률 하한', 'fullSatisfactionUpper': '충족률 상한', 'productPass': '제품 통과(1/0)',
              'verdictMs': '판정 시간(ms)', 'costUsd': '비용(USD)', 'modelCalls': '모델 호출 수', 'totalTokens': '전체 토큰'}
    lines = ['', '## 짝 비교(STAB-05, Multi − Single)', '',
             f"- 비교한 쌍 {len(pairs.get('pairs', []))}개 · 제외한 쌍 {len(pairs.get('excludedPairs', []))}개 · "
             f"짝 없는 Run {len(pairs.get('unpairedRuns', []))}개", '',
             '| 지표 | 쌍 수 | 차이 중앙값 | 최소 | 최대 | Multi가 큼 | Single이 큼 | 같음 |', '|---|---|---|---|---|---|---|---|']
    for metric, entry in (pairs.get('differences') or {}).items():
        lines.append(f"| {titles.get(metric, cell(metric))} | {entry.get('n', 0)} | {cell(entry.get('median'))} | "
                     f"{cell(entry.get('min'))} | {cell(entry.get('max'))} | {entry.get('multiHigher', 0)} | "
                     f"{entry.get('singleHigher', 0)} | {entry.get('equal', 0)} |")
    for item in pairs.get('excludedPairs', []):
        lines.append(f"- 제외: 쌍 {cell(item.get('pairId'))} · {cell(item.get('reason'))}")
    lines.append('')
    lines.append('차이의 크기·방향은 표본 수와 함께 본다. 시간·비용은 작을수록, 충족률·통과는 클수록 좋다.')
    return lines
