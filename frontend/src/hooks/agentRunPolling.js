export const isTerminalRun = status => status === 'FINISHED' || status === 'ABORTED'
export const pollingDelay = failures => Math.min(30000, 3000 * 2 ** Math.min(4, Math.max(0, failures)))

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
    if (stopped || state.terminal || state.architecture === 'SINGLE_AGENT') return
    if (state.pending) { waitFor(state.pending); return }
    timer = setTimer(() => {
      timer = null
      if (stopped || getState().terminal || getState().architecture === 'SINGLE_AGENT') return
      waitFor(refresh())
    }, pollingDelay(state.failures ?? 0))
  }
  schedule()
  return { waitFor, stop() { stopped = true; waiting = null; clear() } }
}
