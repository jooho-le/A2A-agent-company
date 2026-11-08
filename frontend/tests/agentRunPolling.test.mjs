import assert from 'node:assert/strict'
import test from 'node:test'
import { createRunPolling, isTerminalRun, pollingDelay } from '../src/hooks/agentRunPolling.js'

const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve() }
function deferred() {
  let resolve, reject
  const promise = new Promise((ok, fail) => { resolve = ok; reject = fail })
  return { promise, resolve, reject }
}
function setup(overrides = {}) {
  const state = { failures: 0, terminal: false, architecture: 'MULTI_AGENT', ...overrides }
  const timers = new Map(), requests = []
  let sequence = 0, loop
  loop = createRunPolling({
    getState: () => state,
    setTimer(fn, delay) { const id = ++sequence; timers.set(id, { fn, delay }); return id },
    clearTimer(id) { timers.delete(id) },
    refresh() {
      if (state.pending) return state.pending
      const request = deferred()
      state.pending = request.promise
      requests.push(request)
      state.pending.then(() => { state.pending = null }, () => { state.pending = null })
      // 실제 Hook도 수동·자동 refresh가 시작되면 같은 Promise를 전달합니다.
      loop.waitFor(state.pending)
      return state.pending
    },
  })
  return { state, timers, requests, loop,
    fire() { const [id, timer] = timers.entries().next().value; timers.delete(id); timer.fn() } }
}

test('3초 후 시작하고 요청 완료 전에는 다음 조회를 예약하지 않는다', async () => {
  const env = setup()
  assert.equal([...env.timers.values()][0].delay, 3000)
  env.fire()
  assert.equal(env.requests.length, 1)
  assert.equal(env.timers.size, 0)
  env.requests[0].resolve(); await flush()
  assert.equal(env.timers.size, 1)
  assert.equal([...env.timers.values()][0].delay, 3000)
  env.loop.stop()
})

test('수동 조회도 완료 후 3초부터 예약하며 같은 Promise로 중복 예약하지 않는다', async () => {
  const env = setup(), manual = deferred()
  env.state.pending = manual.promise
  env.loop.waitFor(manual.promise); env.loop.waitFor(manual.promise)
  assert.equal(env.timers.size, 0)
  env.state.pending = null; manual.resolve(); await flush()
  assert.equal(env.timers.size, 1)
  assert.equal(env.requests.length, 0)
  env.loop.stop()
})

test('처음부터 진행 중인 조회가 있으면 완료를 기다린다', async () => {
  const pending = deferred(), env = setup({ pending: pending.promise })
  assert.equal(env.timers.size, 0)
  env.state.pending = null; pending.resolve(); await flush()
  assert.equal(env.timers.size, 1)
  env.loop.stop()
})

test('중지·Run 전환 뒤 늦게 끝난 이전 요청은 다시 예약하지 않는다', async () => {
  const old = setup(); old.fire(); old.loop.stop()
  const next = setup()
  old.requests[0].resolve(); await flush()
  assert.equal(old.timers.size, 0)
  assert.equal(next.timers.size, 1)
  next.loop.stop()
})

test('최종 상태 FINISHED·ABORTED만 자동 중지하고 판정·대기 상태와 구분한다', async () => {
  assert.equal(isTerminalRun('FINISHED'), true)
  assert.equal(isTerminalRun('ABORTED'), true)
  for (const status of ['WAITING_INPUT', 'HUMAN_REVIEW', 'FAILED', 'SUCCEEDED']) assert.equal(isTerminalRun(status), false)
  const env = setup(); env.fire(); env.state.terminal = true
  env.requests[0].resolve(); await flush()
  assert.equal(env.timers.size, 0)
  env.loop.stop()
})

test('확인된 싱글 Run에는 폴링하지 않는다', () => {
  const env = setup({ architecture: 'SINGLE_AGENT' })
  assert.equal(env.timers.size, 0)
  assert.equal(env.requests.length, 0)
  env.loop.stop()
})

test('오류 재시도는 6·12·24·최대 30초, 복구 후 3초로 돌아온다', async () => {
  assert.deepEqual([0,1,2,3,4,10].map(pollingDelay), [3000,6000,12000,24000,30000,30000])
  const env = setup(); env.fire(); env.state.failures = 1
  env.requests[0].reject(new Error('Mock 연결 오류')); await flush()
  assert.equal([...env.timers.values()][0].delay, 6000)
  env.fire(); env.state.failures = 0; env.requests[1].resolve(); await flush()
  assert.equal([...env.timers.values()][0].delay, 3000)
  env.loop.stop()
})

test('예약을 취소하면 타이머도 제거된다', () => {
  const env = setup(); env.loop.stop()
  assert.equal(env.timers.size, 0)
  assert.equal(env.requests.length, 0)
})
