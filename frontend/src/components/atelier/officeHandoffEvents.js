const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
const responseEvents = new Set(['A2A_TASK_RECEIVED', 'A2A_TASK_STATE_CHANGED'])
const responseStates = new Set(['TASK_STATE_COMPLETED', 'TASK_STATE_FAILED', 'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_REJECTED', 'TASK_STATE_AUTH_REQUIRED'])

// a2a_tasks.py의 호출·응답 기록만 사용합니다. Artifact 전송이나 다음 역할을 추측하지 않습니다.
export function confirmedHandoffs(events, steps, runId) {
  if (!runId || !Array.isArray(events) || !Array.isArray(steps)) return []
  const candidates = new Map()
  for (const step of steps) {
    if (!step || typeof step.workflowStepId !== 'string' || !step.workflowStepId || step.runId !== runId || !roles.includes(step.agentRole)) continue
    const matches = candidates.get(step.workflowStepId) ?? []
    matches.push(step); candidates.set(step.workflowStepId, matches)
  }
  return events.flatMap(event => {
    if (!event) return []
    const matches = candidates.get(event.workflowStepId)
    if (event.runId !== runId || typeof event.eventId !== 'string' || !event.eventId
      || String(event.actor).toUpperCase() !== 'ORCHESTRATOR' || matches?.length !== 1
      || !Number.isFinite(Date.parse(event.occurredAt))) return []
    const step = matches[0]
    if (!Number.isSafeInteger(event.attempt) || event.attempt < 0) return []
    const worker = roles.indexOf(step.agentRole)
    const sent = event.eventType === 'A2A_MESSAGE_SENT'
    const report = responseEvents.has(event.eventType) && responseStates.has(event.a2aTaskState)
    if (!sent && !report) return []
    return [{ eventId: event.eventId, occurredAt: event.occurredAt, workflowStepId: event.workflowStepId,
      eventType: event.eventType, a2aTaskState: event.a2aTaskState, attempt: event.attempt,
      from: sent ? 4 : worker, to: sent ? worker : 4,
      label: sent ? '호출 요청' : '상태 응답', agentRole: step.agentRole }]
  })
}

// 처음 본 과거 페이지는 기준점으로만 기억합니다. 새로 관측한 이벤트만 한 번 연출합니다.
export function createOfficeHandoffController() {
  let runId = null, page = null, seen = new Set(), initialized = false
  let clock = 0, flights = []
  function clear() { runId = null; page = null; seen = new Set(); initialized = false; clock = 0; flights = [] }
  function observe(snapshot, mode) {
    if (mode !== 'actual' || !snapshot?.runId) { clear(); return }
    if (snapshot.runId !== runId) { clear(); runId = snapshot.runId }
    if (Object.values(snapshot.errors ?? {}).some(Boolean)) { flights = []; return }
    if (snapshot.queryStatus?.events === 'error' || snapshot.queryStatus?.steps === 'error'
      || snapshot.queryStatus?.run === 'error' || snapshot.queryStatus?.architecture === 'error') { flights = []; return }
    if (snapshot.architecture !== 'MULTI_AGENT' || snapshot.queryStatus?.architecture !== 'success'
      || snapshot.queryStatus?.run !== 'success' || !snapshot.run?.status
      || !['success', 'empty'].includes(snapshot.queryStatus?.events)
      || !['success', 'empty'].includes(snapshot.queryStatus?.steps)) return
    const currentPage = `${snapshot.pagination?.offset ?? 0}:${snapshot.pagination?.limit ?? 0}`
    const data = snapshot.handoffs ?? []
    const baseline = !initialized || page !== currentPage
    const incoming = []
    for (const item of data) {
      if (seen.has(item.eventId)) continue
      seen.add(item.eventId); incoming.push(item)
    }
    if (!baseline && incoming.length) {
      const first = Date.parse(incoming[0].occurredAt)
      // DB의 sequence 순서인 API 배열을 유지합니다. 시계 차이로 occurredAt이
      // 앞뒤로 어긋나더라도 순서를 뒤집거나 이벤트를 버리지 않습니다.
      let offset = 0
      incoming.forEach(item => {
        offset = Math.max(offset, (Date.parse(item.occurredAt) - first) / 1000)
        flights.push({ ...item, startsAt: clock + offset })
      })
    }
    initialized = true; page = currentPage
  }
  function advance(delta, enabled = true) {
    if (enabled) clock += Number.isFinite(delta) ? Math.max(0, delta) : 0
    flights = flights.filter(item => clock < item.startsAt + 1.6)
    return flights.filter(item => clock >= item.startsAt).map(item => ({ ...item, progress: Math.min(1, (clock - item.startsAt) / 1.6) }))
  }
  return { observe, advance }
}
