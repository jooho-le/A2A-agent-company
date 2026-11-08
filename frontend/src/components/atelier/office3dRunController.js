// 실제 조회 상태만 관리합니다. 목업 타임라인·API·저장소·세리머니에 접근하지 않습니다.
export const officeAgentRoles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
const confirmedStates = new Set(['idle', 'pending', 'working', 'succeeded', 'failed', 'waiting_input', 'canceled'])

export function createOfficeRunController() {
  let runId = null
  let records = []
  function reset(nextId) {
    runId = nextId
    records = officeAgentRoles.map(agentRole => ({
      agentRole, confirmed: null, shown: { agentRole, displayState: 'idle', label: '기본 대기', step: null },
      identity: null, motionTime: 0, poseState: 'waiting', blocked: false, refreshing: false,
    }))
  }
  reset(null)

  function setSnapshot(snapshot) {
    const nextId = snapshot?.runId ?? null
    if (nextId !== runId) reset(nextId)
    const queryFailed = snapshot?.queryStatus?.run === 'error' || snapshot?.queryStatus?.steps === 'error'
    const refreshing = !queryFailed && (snapshot?.queryStatus?.run === 'loading' || snapshot?.queryStatus?.steps === 'loading')
    for (const record of records) {
      record.refreshing = refreshing
      if (refreshing) {
        // 같은 Run의 확인된 동작은 계속합니다. 오류 후에는 재조회 성공 전까지 재개하지 않습니다.
        record.shown = record.confirmed && !record.blocked ? record.confirmed
          : { agentRole: record.agentRole, displayState: record.blocked ? 'unknown' : 'loading', label: record.blocked ? '상태 확인 불가' : '조회 중', step: null }
        continue
      }
      const agent = queryFailed ? null : snapshot?.agents?.find(item => item.agentRole === record.agentRole)
      if (!agent || !confirmedStates.has(agent.displayState)) {
        record.blocked = true
        record.shown = { agentRole: record.agentRole, displayState: 'unknown', label: '상태 확인 불가', step: null, reason: agent?.reason }
        continue
      }
      // 같은 Run·Step·재시도·상태의 재조회는 작업 모션을 처음부터 시작하지 않습니다.
      const identity = JSON.stringify([agent.step?.workflowStepId, agent.step?.attempt, agent.step?.codeVersion, agent.displayState])
      if (record.identity !== identity) record.motionTime = 0
      record.identity = identity
      record.confirmed = { ...agent }
      record.shown = record.confirmed
      record.blocked = false
      record.poseState = agent.displayState === 'working' ? 'working' : 'waiting'
    }
  }

  function advance(delta = 0, motionEnabled = true) {
    const elapsed = Number.isFinite(delta) ? Math.max(0, delta) : 0
    return records.map(record => {
      if (motionEnabled && !record.blocked && record.shown.displayState === 'working') record.motionTime += elapsed
      return {
        ...record.shown, refreshing: record.refreshing,
        hasConfirmedState: record.confirmed !== null,
        motionTime: record.motionTime,
        poseTime: record.poseState === 'working' ? record.motionTime : 0,
        poseState: record.poseState,
        expression: record.shown.displayState === 'failed' ? 'worried' : 'normal',
      }
    })
  }

  return { setSnapshot, advance }
}
