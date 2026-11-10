import { safePopupText } from '../../api/agentRunResults.js'
import { isTraceActionCurrent } from './officeTraceEvents.js'

const names = ['계획에이전트', '개발에이전트', 'QA에이전트', '보안에이전트', '오케스트레이터']
const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY', 'ORCHESTRATOR']
const problem = '검증 과정에서 문제가 발견되었습니다. 상세 결과 확인이 필요합니다.'
const verdicts = { SUCCESS: '성공', FAIL: '실패', UNVERIFIED: '미검증', HUMAN_REVIEW: '사람의 검토 필요' }
const runLabels = { RECEIVED: '요청 접수', PLANNING: '계획 수립', IMPLEMENTING: '개발 진행', SNAPSHOT_READY: '코드 버전 확정',
  VALIDATING: '검증 진행', FIX_REQUIRED: '수정 필요', FIXING: '수정 진행', REVALIDATING: '재검증 진행',
  WAITING_INPUT: '입력 대기', HUMAN_REVIEW: '사람의 검토 필요', FINISHED: '실행 종료', ABORTED: '실행 중단' }
const readable = status => ['success', 'empty', 'loading'].includes(status)
const unique = rows => rows.length === 1 ? rows[0] : null

function artifactsFor(snapshot, step, ids = step?.outputArtifactIds) {
  const results = snapshot.popupResults
  if (!step || results?.runId !== snapshot.runId || !readable(results.status?.artifacts)) return []
  return results.artifacts.filter(item => item.runId === snapshot.runId && item.workflowStepId === step.workflowStepId
    && item.createdBy === step.agentRole && item.codeVersion === (step.codeVersion ?? null)
    && (!step.a2aTaskId || item.a2aTaskId === step.a2aTaskId) && ids?.includes(item.artifactId))
}

function issuesFor(snapshot, predicate) {
  const results = snapshot.popupResults
  return results?.runId === snapshot.runId && readable(results.status?.issues)
    ? results.issues.filter(issue => issue.runId === snapshot.runId && predicate(issue)) : []
}

function outcomeSummary(rows) {
  if (!rows?.length) return '검사 세부 결과를 확인할 수 없습니다.'
  const count = outcome => rows.filter(row => row.outcome === outcome).length
  const unknown = rows.length - count('PASS') - count('FAIL') - count('UNVERIFIED')
  return `검사 ${rows.length}건: 통과 ${count('PASS')} · 실패 ${count('FAIL')} · 미검증 ${count('UNVERIFIED')}`
    + (unknown ? ` · 결과 미확인 ${unknown}` : '')
}

export function artifactPopupSummary(artifact) {
  switch (artifact.artifactType) {
    case 'REQUIREMENT': return { title: '요구사항 산출물 등록', tone: 'normal',
      detail: `요구사항 ${artifact.requirementCount}건 등록. ${artifact.requirementSummary || '상세 분석 결과는 산출물에서 확인할 수 있습니다.'}` }
    case 'SOURCE': return { title: '코드 산출물 등록', tone: 'normal', detail: `코드 버전 ${artifact.codeVersion}의 산출물이 등록되었습니다.` }
    case 'CHANGE_REPORT': return { title: '코드 변경 결과', tone: 'normal',
      detail: `변경 파일 ${artifact.changedFileCount}개. ${artifact.summary || '수정 보고서가 등록되었습니다.'}` }
    case 'BUILD_REPORT': return { title: '빌드 결과', tone: artifact.executionOutcome === 'FAIL' ? 'error' : 'normal',
      detail: `빌드 ${ { PASS: '통과', FAIL: '실패', UNVERIFIED: '미검증' }[artifact.executionOutcome] ?? '결과 미확인' }`
        + (Number.isInteger(artifact.exitCode) ? ` · 종료 코드 ${artifact.exitCode}` : '')
        + (artifact.failureKind === 'INFRASTRUCTURE' ? ' · 실행 환경 문제' : '')
        + (artifact.diagnostic ? `. ${artifact.diagnostic}` : '') }
    case 'QA_REPORT': {
      const failed = artifact.tests?.find(item => item.outcome === 'FAIL')
      const unverified = artifact.tests?.find(item => item.outcome === 'UNVERIFIED')
      return { title: 'QA 검증 결과', tone: failed ? 'error' : 'normal', detail: outcomeSummary(artifact.tests)
        + (failed ? `. 보고 내용: ${failed.details || failed.actualResult || failed.title || problem}`
          : unverified?.details || unverified?.actualResult ? `. 미검증 사유: ${unverified.details || unverified.actualResult}` : '') }
    }
    case 'SECURITY_REPORT': {
      const failed = artifact.requirementResults?.find(item => item.outcome === 'FAIL')
      const unverified = artifact.requirementResults?.find(item => item.outcome === 'UNVERIFIED')
      const finding = artifact.findings?.find(item => item.disposition === 'CONFIRMED')
        ?? artifact.findings?.find(item => item.disposition === 'SUSPECTED')
      const disposition = { CONFIRMED: '확인', SUSPECTED: '의심', FALSE_POSITIVE: '오탐', UNVERIFIED: '미검증' }
      return { title: '보안 검증 결과', tone: failed || finding?.disposition === 'CONFIRMED' ? 'error' : 'normal',
        detail: outcomeSummary(artifact.requirementResults) + ` · 발견 사항 ${artifact.findings?.length ?? 0}건`
          + (finding ? `. ${disposition[finding.disposition]}: ${finding.description || finding.title || problem}`
            : failed ? `. 보고 내용: ${failed.details || failed.actualResult || problem}`
              : unverified?.details || unverified?.actualResult ? `. 미검증 사유: ${unverified.details || unverified.actualResult}` : '') }
    }
    default: return { title: '산출물 등록', tone: 'normal', detail: '업무 산출물이 등록되었습니다.' }
  }
}

function stepPopup(snapshot, row, agent) {
  const step = row.step
  const base = { agent, name: names[agent], status: row.label, tone: 'normal', source: 'step', eventId: null }
  if (row.refreshing) return { ...base, title: '상태 갱신 중', detail: '마지막으로 확인한 상태를 표시합니다.' }
  if (agent === 4) return { ...base, source: 'run', title: runLabels[snapshot.run?.status] ?? 'Run 상태 확인 중',
    detail: snapshot.run?.verdict ? `현재 Run 판정: ${verdicts[snapshot.run.verdict] ?? '확인 불가'}`
      : snapshot.run?.status ? 'Run 상태에 따라 업무 흐름을 관리합니다.' : '현재 Run 상태를 확인할 수 없습니다.' }
  if (row.displayState === 'failed') return { ...base, tone: 'error', title: '에이전트 실행 실패',
    detail: 'Step 실행이 실패했습니다. 구체적인 원인은 현재 조회 정보에 제공되지 않습니다.' }
  if (row.displayState === 'working') {
    const fixes = agent === 1 ? issuesFor(snapshot, issue => issue.fixWorkflowStepId === step.workflowStepId && issue.newCodeVersion === step.codeVersion) : []
    return { ...base,
      title: ['요구사항 분석 중', step?.codeVersion > 1 ? '코드 수정 중' : '개발 진행 중', 'QA 검증 중', '보안 검사 중'][agent],
      detail: `현재 Step이 실행 중입니다. 시도 ${step.attempt}` + (step.codeVersion !== null ? ` · 코드 버전 ${step.codeVersion}` : '')
        + (fixes.length ? `. 수정 요청 ${fixes.length}건: ${fixes[0].description || fixes[0].title}` : '') }
  }
  if (row.displayState === 'succeeded') {
    const artifacts = artifactsFor(snapshot, step)
    const preferred = ['BUILD_REPORT', 'QA_REPORT', 'SECURITY_REPORT', 'CHANGE_REPORT', 'REQUIREMENT', 'SOURCE']
      .map(type => unique(artifacts.filter(item => item.artifactType === type))).filter(Boolean)
    if (preferred.length) {
      const summaries = preferred.map(artifactPopupSummary)
      const issues = [2, 3].includes(agent) ? issuesFor(snapshot, issue => issue.reporter === step.agentRole
        && issue.codeVersion === step.codeVersion && preferred.some(artifact => artifact.artifactId === issue.reportArtifactId)
        && step.inputArtifactIds?.includes(issue.sourceArtifactId)) : []
      return { ...base, ...summaries[0], source: 'report', detail: issues.length
        ? `${outcomeSummary(preferred[0].tests ?? preferred[0].requirementResults)}. 보고된 문제 ${issues.length}건: ${issues[0].description || issues[0].title}`
        : summaries.map(item => item.detail).join(' / ') }
    }
    return { ...base, title: '업무 단계 완료', detail: 'Step 실행이 완료되었습니다. 개별 검증 결과와 Run 최종 판정은 별도로 확인합니다.' }
  }
  return { ...base, title: row.label ?? '상태 확인 불가', detail: row.displayState === 'unknown' || row.displayState === 'loading'
    ? '현재 상태를 조회할 수 없습니다. 에이전트 실행 실패를 의미하지 않습니다.'
    : step ? '조회한 Step 상태입니다.' : '아직 생성된 작업 Step이 없습니다.' }
}

function eventPopup(snapshot, action, agent, row) {
  const past = !isTraceActionCurrent(action, row) || action.step && row.displayState !== 'working'
  const base = { source: 'event', eventId: action.eventId, tone: 'normal', historical: Boolean(past),
    // 이벤트 시점의 결과를 현재 Step 자세와 혼동하지 않도록 항상 기록으로 표시합니다.
    detail: past ? '이전 작업 이벤트 기록입니다.' : '확인된 실행 이벤트 기록입니다.' }
  switch (action.eventType) {
    case 'A2A_MESSAGE_SENT': return { ...base, title: '업무 전달 기록',
      detail: `오케스트레이터가 ${names[action.to]}에게 업무 호출을 요청했습니다.` }
    case 'ARTIFACT_REGISTERED':
    case 'SNAPSHOT_FROZEN': {
      const artifacts = artifactsFor(snapshot, action.step, action.outputArtifactIds)
      const summaries = artifacts.map(artifactPopupSummary)
      return { ...base, title: summaries.length === 1 ? summaries[0].title : '업무 산출물 등록',
        tone: summaries.some(item => item.tone === 'error') ? 'error' : 'normal',
        detail: summaries.length ? summaries.map(item => item.detail).join(' / ') : '해당 Step의 산출물 등록을 확인했습니다. 상세 결과는 아직 확인되지 않았습니다.' }
    }
    case 'ISSUE_DETECTED': {
      const results = snapshot.popupResults
      const issue = results?.runId === snapshot.runId && readable(results.status?.issues)
        ? unique(results.issues.filter(item => item.issueId === action.issueId && item.runId === snapshot.runId
          && item.reporter === roles[agent] && item.codeVersion === action.codeVersion
          && action.inputArtifactIds?.includes(item.sourceArtifactId)
          && (!item.reportArtifactId || action.outputArtifactIds?.includes(item.reportArtifactId)))) : null
      return { ...base, title: '문제 발견 기록', tone: 'error', detail: issue?.description || issue?.title || problem }
    }
    case 'FIX_REQUESTED': return { ...base, title: '코드 수정 요청 기록', detail: '오케스트레이터가 개발에이전트에게 수정을 요청했습니다.' }
    case 'VALIDATION_STARTED':
    case 'REVALIDATION_STARTED': return { ...base, title: action.eventType === 'REVALIDATION_STARTED' ? '재검증 시작 기록' : '검증 시작 기록',
      detail: `오케스트레이터가 코드 버전 ${action.codeVersion ?? '미확인'}의 검증을 시작했습니다.` }
    case 'VERDICT_CREATED': {
      // 과거 판정 이벤트에 현재의 다른 버전 판정값을 붙이지 않습니다.
      const verdict = action.codeVersion === snapshot.run?.codeVersion && action.workflowState === snapshot.run?.status
        && action.attempt === snapshot.run?.fixAttempt ? verdicts[snapshot.run?.verdict] : null
      return { ...base, title: 'Run 판정 기록', detail: verdict ? `확인된 Run 판정: ${verdict}` : '판정 이벤트가 등록되었습니다. 해당 시점의 판정값은 확인되지 않았습니다.' }
    }
    case 'RUN_STARTED': return { ...base, title: '업무 접수 기록', detail: '오케스트레이터가 Run 실행 요청을 접수했습니다.' }
    default: return null
  }
}

// 별도 이벤트 큐/타이머를 만들지 않습니다. 기존 Trace 큐의 current만 팝업으로 투영합니다.
export function buildOfficeRunPopups(snapshot, rows, traceInfo, { mode = 'actual', ceremony = false, previous = null } = {}) {
  if (mode !== 'actual' || ceremony || !snapshot?.runId || snapshot.architecture !== 'MULTI_AGENT') return []
  // 같은 Run의 조회 중에는 멈춘 Trace와 마지막 팝업을 함께 유지합니다.
  // 조회 완료·실패·Run 변경 후에는 반드시 현재 데이터로 다시 구성합니다.
  const querying = ['run', 'steps', 'trace'].some(key => snapshot.queryStatus?.[key] === 'loading')
  const failed = Object.values(snapshot.errors ?? {}).some(Boolean)
    || Object.values(snapshot.queryStatus ?? {}).some(value => ['error', 'cancelled'].includes(value))
  if (querying && !failed && previous?.runId === snapshot.runId && previous.popups?.length) return previous.popups
  const popups = rows.map((row, agent) => stepPopup(snapshot, row, agent))
  const coreReady = snapshot.queryStatus?.run === 'success' && ['success', 'empty'].includes(snapshot.queryStatus?.steps)
  if (coreReady && traceInfo?.ready) {
    const seen = new Set()
    for (const action of traceInfo.current ?? []) {
      if (seen.has(action.eventId) || action.runId !== snapshot.runId || action.kind === 'log') continue
      seen.add(action.eventId)
      const targets = action.kind === 'handoff' ? [4, action.to] : action.eventType === 'FIX_REQUESTED' ? [4, ...(action.relatedAgents ?? [])] : [action.agent]
      for (const agent of targets) {
        if (!rows[agent]) continue
        const eventRow = action.step ? rows[roles.indexOf(action.step.agentRole)] : rows[agent]
        const content = eventPopup(snapshot, action, agent, eventRow)
        if (content) popups[agent] = { ...popups[agent], ...content }
      }
    }
  }
  return popups.map(popup => ({ ...popup, title: safePopupText(popup.title, 45), detail: safePopupText(popup.detail, 220) }))
}

export function popupResultNotice(snapshot) {
  const status = Object.values(snapshot?.popupResults?.status ?? {})
  return status.includes('error') ? 'Issue·산출물 상세 조회 실패 · 확인된 Step·이벤트만 표시합니다. 새로고침으로 다시 조회할 수 있습니다.'
    : status.includes('cancelled') ? '상세 결과 조회가 취소되었습니다.'
      : status.includes('loading') ? 'Issue·산출물 상세 결과를 갱신하고 있습니다.' : ''
}

// 고정된 다섯 DOM을 재사용합니다. 서버 문자열은 HTML로 해석하지 않습니다.
export function renderOfficeRunPopups(root, popups, notice = '') {
  const element = key => root.querySelector(`[data-office3d="${key}"]`)
  element('actual-popups').hidden = popups.length === 0
  element('popup-result-notice').textContent = notice
  for (let agent = 0; agent < 5; agent++) {
    const card = element(`actual-popup-${agent}`), popup = popups.find(item => item.agent === agent)
    card.hidden = !popup
    card.dataset.eventId = popup?.eventId ?? ''
    card.dataset.tone = popup?.tone ?? 'normal'
    card.dataset.source = popup?.source ?? ''
    element(`actual-status-${agent}`).textContent = popup?.status ?? ''
    element(`actual-source-${agent}`).textContent = popup ? popup.source === 'event' ? popup.historical ? '이전 작업 이벤트 · 현재 상태는 위 배지 참조' : '실행 이벤트 기록 · 현재 상태는 위 배지 참조'
      : popup.source === 'report' ? '현재 Step의 산출물 요약' : '현재 조회 상태' : ''
    element(`actual-title-${agent}`).textContent = popup?.title ?? ''
    element(`actual-detail-${agent}`).textContent = popup?.detail ?? ''
  }
}
