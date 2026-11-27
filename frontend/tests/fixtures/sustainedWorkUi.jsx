import { act } from 'react'
import { createRoot } from 'react-dom/client'
import RunWorkPanel from '../../src/components/atelier/RunWorkPanel'
import { buildAgentRunDisplayState } from '../../src/components/atelier/agentRunDisplayState'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.fetch = () => { throw new Error('No network allowed') }
const originalTimeout = globalThis.setTimeout, originalClear = globalThis.clearTimeout
let clock = 0, sequence = -1
const timers = new Map()
globalThis.setTimeout = (fn, delay, ...args) => {
  if (delay !== 3000) return originalTimeout(fn, delay, ...args)
  const id = sequence--; timers.set(id, { fn, at: clock + delay }); return id
}
globalThis.clearTimeout = id => { if (!timers.delete(id)) originalClear(id) }
const host = document.createElement('div'); document.body.append(host)
const root = createRoot(host)
const controls = { data: { requests: [] }, commands: {}, refresh() {} }
function snapshot({ id = 'run-one', state = 'RUNNING', query = 'success' } = {}) {
  return buildAgentRunDisplayState({ runId: id, architecture: 'MULTI_AGENT',
    run: { runId: id, status: 'VALIDATING', verdict: null, codeVersion: 1, fixAttempt: 0 },
    status: { run: query, steps: query }, errors: {},
    steps: [{ runId: id, workflowStepId: 'qa-step', agentRole: 'QA', status: state, attempt: 0, codeVersion: 1 }] })
}
async function render(options) {
  const displayState = snapshot(options)
  await act(async () => root.render(<RunWorkPanel runId={displayState.runId} projectName="Mock"
    displayState={displayState} controls={controls} selectedRole={null} onSelectRole={() => {}}
    onResults={() => {}} onNew={() => {}} />))
}
async function advance(ms) {
  clock += ms
  await act(async () => {
    for (const [id, timer] of [...timers]) if (timer.at <= clock) { timers.delete(id); timer.fn() }
  })
}
const qa = () => document.querySelectorAll('.run-employee-picker button')[3].dataset.state
function check(value, message) { if (!value) throw new Error(message) }
async function verify() {
  const passed = []
  await render(); await advance(2999)
  check(qa() === 'pending', 'no highlight before 3 seconds')
  await render({ query: 'loading' }); await advance(1)
  check(qa() === 'working', 'polling preserves the 3 second clock')
  check(document.querySelector('.run-person-heading').textContent.includes('품질 검사'), 'work panel follows qualified employee')
  await render(); check(qa() === 'working', 'same Step refresh never restarts timer')
  passed.push('3초 경계·정기 조회 유지·작업창 대상 연결')

  await render({ state: 'SUCCEEDED' }); await render(); await advance(2900)
  check(qa() === 'pending', 'same Step restarting must qualify again')
  await render({ state: 'SUCCEEDED' }); await advance(200)
  check(qa() === 'succeeded', 'brief work never becomes active later')
  await render({ state: 'WAITING_INPUT' }); check(qa() === 'waiting_input', 'waiting immediate')
  await render({ state: 'FAILED' }); check(qa() === 'failed', 'failure immediate')
  passed.push('짧은 작업 생략·완료·실패·입력 대기 즉시 표시')

  await render(); await advance(2000)
  await render({ query: 'error' }); await advance(2000)
  check(qa() === 'unknown', 'errors discard pending timer')
  await render({ query: 'loading' }); check(qa() === 'unknown', 'loading after error cannot revive working')
  await render(); await advance(2000)
  await render({ id: 'run-two' }); await advance(1000)
  check(qa() === 'pending', 'new Run cannot inherit old timer')
  await act(async () => root.unmount())
  check(timers.size === 0, 'unmount cancels pending timer')
  passed.push('조회 오류·Run 변경·언마운트 시 지연 작업 정리')
  return { passed }
}
verify().then(report => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify(report); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.stack }); document.body.append(result)
})
