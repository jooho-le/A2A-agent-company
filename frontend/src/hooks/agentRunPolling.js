export const isTerminalRun = status => status === 'FINISHED' || status === 'ABORTED'
export const maxPollingRetries = 3
export const pollingDelay = failures => failures > 0 ? 2000 * 2 ** Math.min(2, failures - 1) : 2000
export const canRetryRunRead = error => error?.code === 'NETWORK_ERROR' || [408, 429, 500, 502, 503, 504].includes(error?.status)
const halted = state => state.terminal || state.blocked || state.failures > maxPollingRetries || state.architecture === 'SINGLE_AGENT'

// refresh의 진행 중 Promise도 공유합니다. 수동 조회가 시작되면 기존 예약을 취소하고,
// 그 조회가 끝난 뒤부터 다음 간격을 계산합니다.
export function createRunPolling({ refresh, getState, setTimer = setTimeout, clearTimer = clearTimeout }) {
  let stopped = false, timer = null, waiting = null
  const clear = () => { if (timer !== null) clearTimer(timer); timer = null }
  function waitFor(promise) {
    clear()
    if (stopped || waiting === promise) return
    waiting = promise
    const settled = () => {
      if (stopped || waiting !== promise) return
      waiting = null
      schedule()
    }
    Promise.resolve(promise).then(settled, settled)
  }
  function schedule() {
    clear()
    const state = getState()
    if (stopped || halted(state)) return
    if (state.pending) { waitFor(state.pending); return }
    timer = setTimer(() => {
      timer = null
      if (stopped || halted(getState())) return
      waitFor(refresh({ automatic: true }))
    }, pollingDelay(state.failures ?? 0))
  }
  schedule()
  return { waitFor, stop() { stopped = true; waiting = null; clear() } }
}
