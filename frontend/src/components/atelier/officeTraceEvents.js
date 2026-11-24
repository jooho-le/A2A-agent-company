const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
const durations = { handoff: 1.3, artifact: 1, issue: 1, receive: .65, state: .55, manage: .75, verdict: .8, log: .2 }

// API에는 from/to 역할이 없습니다. 같은 Run의 유일한 Step으로만 연결합니다.
export function traceAction(event, steps, runId) {
  const matches = (steps ?? []).filter(step => step?.runId === runId && step.workflowStepId === event.workflowStepId)
  const candidate = event.workflowStepId && matches.length === 1 ? matches[0] : null
  const step = candidate && roles.includes(candidate.agentRole)
    && (event.codeVersion == null || event.codeVersion === candidate.codeVersion) ? candidate : null
  const agent = step ? roles.indexOf(step.agentRole) : null
  const actor = String(event.actor ?? '').toUpperCase(), manager = actor === 'ORCHESTRATOR'
  const base = { ...event, kind: 'log', agent: null, label: '상태 기록 · 전달 대상 미확인', step,
    codeVersion: event.codeVersion ?? null, from: null, to: null }
  if (event.runId !== runId || !Number.isFinite(Date.parse(event.occurredAt)) || !Number.isSafeInteger(event.attempt) || event.attempt < 0) {
    return { ...base, label: '연결할 수 없는 이벤트 기록' }
  }
  const knownActor = manager || actor === step?.agentRole
  switch (event.eventType) {
    case 'RUN_STARTED': return manager ? { ...base, kind: 'receive', agent: 4, label: '업무 접수' } : base
    case 'A2A_MESSAGE_SENT': return manager && step ? { ...base, kind: 'handoff', agent, from: 4, to: agent, label: '업무 호출 요청' } : base
    case 'A2A_TASK_RECEIVED': return knownActor && step ? { ...base, kind: 'receive', agent, label: 'Task 수신 기록' } : base
    case 'A2A_TASK_STATE_CHANGED':
      // 상태 기록만으로 문서의 수신자나 실제 전달을 추측하지 않습니다.
      return knownActor && step ? { ...base, kind: 'state', agent, label: `Task 상태 기록 · ${event.a2aTaskState ?? '-'}` } : base
    case 'ARTIFACT_REGISTERED':
      return knownActor && step && event.outputArtifactIds?.length
        ? { ...base, kind: 'artifact', agent, label: '산출물 등록 기록' }
        : { ...base, label: '산출물 등록 · 연결 Step 또는 종류 미확인' }
    case 'SNAPSHOT_FROZEN':
      return manager && step ? { ...base, kind: 'artifact', agent, label: 'Snapshot 확정 기록' } : base
    case 'VALIDATION_STARTED':
    case 'REVALIDATION_STARTED': {
      // 버전·입력 Artifact가 함께 맞는 실제 검증 Step만 보조 강조합니다.
      const relatedAgents = [...new Set((steps ?? []).filter(item => item.runId === runId && ['QA', 'SECURITY'].includes(item.agentRole)
        && Number.isInteger(event.codeVersion) && item.codeVersion === event.codeVersion
        && event.inputArtifactIds?.some(id => item.inputArtifactIds?.includes(id))).map(item => roles.indexOf(item.agentRole)))]
      return manager ? { ...base, kind: 'manage', agent: 4, relatedAgents,
        label: event.eventType === 'REVALIDATION_STARTED' ? '재검증 시작 기록' : '검증 시작 기록' } : base
    }
    case 'ISSUE_DETECTED': return knownActor && step && event.issueId ? { ...base, kind: 'issue', agent, label: '결함 발견·보고 기록' } : base
    case 'FIX_REQUESTED': return manager && step?.agentRole === 'DEVELOPER'
      ? { ...base, kind: 'manage', agent: 4, relatedAgents: [agent], label: '개발 수정 지시 기록' } : base
    case 'VERDICT_CREATED': return manager ? { ...base, kind: 'verdict', agent: 4, label: '최종 판정 기록 · 판정값은 현재 Run 참조' } : base
    default: return { ...base, label: '상태 기록 · 동작 미연결' }
  }
}

function parallel(a, b) {
  return a?.kind === 'handoff' && b.kind === 'handoff' && a.agent !== b.agent
    && [2, 3].includes(a.agent) && [2, 3].includes(b.agent)
    && Number.isInteger(a.codeVersion) && a.codeVersion === b.codeVersion
    && a.step?.inputArtifactIds?.some(id => b.step?.inputArtifactIds?.includes(id))
    && Math.abs(Date.parse(a.occurredAt) - Date.parse(b.occurredAt)) <= 500
}

export function isTraceActionCurrent(item, selected) {
  return !item.step || selected?.step?.workflowStepId === item.workflowStepId
    && (!item.eventType.startsWith('A2A_') || selected.step.attempt === item.attempt)
    && selected.step.codeVersion === item.codeVersion
}

export function createOfficeTraceController() {
  let run = null, epoch = null, seen = new Set(), queue = [], clock = 0, tail = 0, last = null
  let recent = [], completed = 0, ready = false
  const reset = () => { run = null; epoch = null; seen = new Set(); queue = []; clock = 0; tail = 0; last = null; recent = []; completed = 0; ready = false }
  function observe(snapshot, mode) {
    if (mode !== 'actual' || !snapshot?.runId) { reset(); return }
    if (run !== snapshot.runId) { reset(); run = snapshot.runId }
    const feed = snapshot.trace
    if (feed && feed.runId === run && feed.epoch !== epoch) { reset(); run = snapshot.runId; epoch = feed.epoch }
    ready = snapshot.architecture === 'MULTI_AGENT' && snapshot.queryStatus?.architecture === 'success'
      && snapshot.run?.status && snapshot.availability !== 'unavailable'
      && snapshot.queryStatus?.run === 'success' && ['success', 'empty'].includes(snapshot.queryStatus?.steps)
      && snapshot.queryStatus?.trace === 'success' && feed?.runId === run && feed.initialized
      && !Object.values(snapshot.errors ?? {}).some(Boolean)
    if (!ready) return
    for (const entry of feed.entries) {
      const event = entry.event
      if (seen.has(event.eventId)) continue
      seen.add(event.eventId)
      const action = traceAction(event, snapshot.traceSteps ?? snapshot.agents.map(item => item.step).filter(Boolean), run)
      let startsAt = Math.max(clock, tail)
      if (parallel(last, action) && last.endsAt > clock) startsAt = last.startsAt
      else if (last) {
        const gap = (Date.parse(action.occurredAt) - Date.parse(last.occurredAt)) / 1000
        if (Number.isFinite(gap)) startsAt += Math.min(.35, Math.max(0, gap))
      }
      const item = { ...action, offset: entry.offset, startsAt, endsAt: startsAt + durations[action.kind] }
      queue.push(item); tail = Math.max(tail, item.endsAt); last = item
    }
  }
  function advance(delta, enabled = true) {
    if (ready && enabled) clock += Math.min(.1, Math.max(0, Number.isFinite(delta) ? delta : 0))
    const finished = queue.filter(item => clock >= item.endsAt)
    if (finished.length) {
      completed += finished.length; recent = [...recent, ...finished].slice(-8)
      queue = queue.filter(item => clock < item.endsAt)
    }
    return queue.filter(item => item.startsAt <= clock).map(item => ({ ...item, progress: (clock - item.startsAt) / durations[item.kind] }))
  }
  return { observe, advance, reset, busy: () => queue.length > 0,
    info: () => ({ ready, pending: queue.length, completed, recent, current: queue.filter(item => item.startsAt <= clock) }) }
}
