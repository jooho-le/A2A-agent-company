export const runEventLimit = 20
export const initialRunSelection = { runId: '', offset: 0, followLatest: false, traceFromStart: false }

// 신규 생성과 수동 조회를 구분해 페이지 이동·새로고침으로 최초 수집 옵션을 잃지 않습니다.
export function runQueryReducer(state, action) {
  if (action.type === 'created') return { runId: action.runId, offset: 0, followLatest: false, traceFromStart: true }
  if (action.type === 'select') return { runId: action.runId, offset: 0, followLatest: Boolean(action.runId), traceFromStart: false }
  if (action.type === 'refresh') return { ...state, followLatest: true }
  if (action.type === 'page') return { ...state, offset: action.offset, followLatest: false }
  if (action.type === 'latest' && state.runId === action.runId && state.followLatest) {
    return { ...state, offset: Math.max(0, action.total - runEventLimit), followLatest: false }
  }
  return state
}
