import { minimumOfficeWorkSeconds } from './office3dRunController.js'

// 명찰의 문구와 색상만 안정화합니다. 실제 Step·모션·상세 결과는 변경하지 않습니다.
export function createOfficeLabelPresentation() {
  let runId = null
  let records = new Map()
  function advance(snapshot, rows, delta = 0) {
    if (runId !== (snapshot?.runId ?? null)) { runId = snapshot?.runId ?? null; records = new Map() }
    const failed = ['run', 'steps'].some(key => ['error', 'cancelled'].includes(snapshot?.queryStatus?.[key]) || snapshot?.errors?.[key])
    const refreshing = !failed && ['run', 'steps'].some(key => snapshot?.queryStatus?.[key] === 'loading')
    const elapsed = Number.isFinite(delta) ? Math.max(0, delta) : 0
    return rows.map(row => {
      let record = records.get(row.agentRole)
      if (!record) {
        record = { key: null, elapsed: 0, shown: { displayState: 'idle', label: '기본 대기' } }
        records.set(row.agentRole, record)
      }
      if (!failed && (refreshing || row.displayState === 'loading')) return record.shown
      const value = failed ? { displayState: 'unknown', label: '상태 확인 불가' }
        : { displayState: row.displayState, label: row.label }
      const key = JSON.stringify([value, row.step?.workflowStepId, row.step?.attempt, row.step?.codeVersion])
      if (record.key !== key) { record.key = key; record.elapsed = 0 }
      record.elapsed += elapsed
      if (['failed', 'waiting_input', 'unknown', 'canceled'].includes(value.displayState)
        || record.elapsed >= minimumOfficeWorkSeconds) record.shown = value
      return record.shown
    })
  }
  return { advance }
}
