/* eslint-disable react-refresh/only-export-components -- Isolated test entry mounts its own fixture. */
import { act, useState } from 'react'
import { createRoot } from 'react-dom/client'
import RunWorkPanel from '../../src/components/atelier/RunWorkPanel'
import useRunInteraction from '../../src/hooks/useRunInteraction'
import { buildAgentRunDisplayState } from '../../src/components/atelier/agentRunDisplayState'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
sessionStorage.clear()
sessionStorage.setItem('access_token', 'test.' + btoa(JSON.stringify({ sub: '1' })) + '.test')
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const runId = id(1), stepId = id(2)
let phase = 'WAITING_INPUT', questionRole = 'PLANNER', root, calls = [], lost = false, receiptBlocked = false, changed = 0
const receipts = new Map()
function check(value, message) { if (!value) throw new Error(message) }
globalThis.fetch = async (url, options = {}) => {
  const method = options.method || 'GET'; calls.push({ url, method, options })
  if (url.endsWith('/interaction')) return Response.json({ runId, revision: 'a'.repeat(64), status: phase,
    requestText: '회원가입 화면', canCancel: phase !== 'ABORTED', requests: phase === 'WAITING_INPUT' ? [{
      workflowStepId: stepId, agentRole: questionRole, attempt: 0, codeVersion: questionRole === 'PLANNER' ? null : 1,
      replyMode: 'ANSWER', reason: '진행할 내용을 확인해주세요.', questions: ['회원가입 기능 안에서 어떻게 진행할까요?'],
    }] : [] })
  if (url.endsWith('/commands') && method === 'POST') {
    const body = JSON.parse(options.body), key = new Headers(options.headers).get('Idempotency-Key')
    check(!receipts.has(key), 'test does not expect duplicate publication')
    const row = { runId, commandId: key, action: body.action, status: 'SUCCEEDED', createdAt: new Date().toISOString(), resultStatus: body.action === 'CANCEL' ? 'ABORTED' : 'WAITING_INPUT' }
    receipts.set(key, row)
    if (body.action === 'CANCEL') phase = 'ABORTED'
    if (lost) { lost = false; throw new TypeError('simulated lost response') }
    return Response.json({ ...row, status: 'QUEUED' }, { status: 202 })
  }
  if (url.includes('/commands/') && method === 'GET') {
    if (receiptBlocked) throw new TypeError('simulated receipt connection failure')
    const row = receipts.get(url.split('/').pop())
    return Response.json(row || { detail: 'not found' }, { status: row ? 200 : 404 })
  }
  throw new Error('No real network fallback: ' + url)
}

function Fixture() {
  const [selected, select] = useState(null)
  const controls = useRunInteraction(runId, true, () => { changed++ })
  const run = { runId, status: phase, verdict: null, codeVersion: 1, fixAttempt: 0 }
  const displayState = buildAgentRunDisplayState({ runId, run, architecture: 'MULTI_AGENT',
    status: { run: 'success', steps: 'success' }, steps: [{ runId, workflowStepId: stepId,
      agentRole: questionRole, status: phase === 'WAITING_INPUT' ? 'WAITING_INPUT' : phase === 'ABORTED' ? 'CANCELED' : 'RUNNING',
      attempt: 0, codeVersion: questionRole === 'PLANNER' ? null : 1 }] })
  return <RunWorkPanel runId={runId} projectName="나의 회원가입" displayState={displayState} controls={controls}
    selectedRole={selected} onSelectRole={select} onResults={() => {}} onNew={() => {}} />
}
async function settle() { await act(async () => { await new Promise(resolve => setTimeout(resolve, 10)) }) }
async function mount() {
  const host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<Fixture />)); await settle()
}
async function click(text, container = document) {
  await act(async () => Array.from(container.querySelectorAll('button')).find(node => node.textContent.trim() === text).click())
  await settle()
}
async function fill(text) {
  await act(async () => { const input = document.querySelector('textarea')
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, text)
    input.dispatchEvent(new Event('input', { bubbles: true })) })
}
async function refresh() { await act(async () => document.querySelector('.run-refresh').click()); await settle() }
const writes = () => calls.filter(row => row.method === 'POST')

async function verify() {
  const passed = []
  await mount()
  check(document.querySelectorAll('.run-employee-picker button').length === 5, 'five employee choices')
  check(document.querySelector('.run-question').textContent.includes('어떻게 진행할까요'), 'actual question shown')
  await fill('정해진 회원가입 범위에서 진행해주세요.')
  await act(async () => { const form = document.querySelector('.run-reply'); form.requestSubmit(); form.requestSubmit() })
  await settle(); await refresh()
  check(writes().length === 1, 'double click sends once')
  const sent = JSON.parse(writes()[0].options.body)
  check(sent.action === 'RESUME' && sent.workflowStepId === stepId && sent.clarification.includes('회원가입'), 'one selected Step and answer')
  check(!JSON.stringify(sent).includes('configuration'), 'frozen configuration is not replaced')
  passed.push('직원 질문 표시와 특정 직원에게 한 번만 답변 전달')

  lost = true; receiptBlocked = true; await fill('원래 범위를 유지해주세요.')
  await act(async () => document.querySelector('.run-reply').requestSubmit()); await settle()
  check(document.querySelector('.run-command-notice').textContent.includes('확인'), 'lost response stays uncertain')
  const count = writes().length
  await act(async () => root.unmount()); await mount()
  check(writes().length === count && document.querySelector('.run-reply button').disabled, 'remount reads the saved receipt without another POST')
  receiptBlocked = false; await refresh()
  check(writes().length === count && document.querySelector('.run-command-notice').textContent.includes('끝났어요'), 'receipt read recovers without POST replay')
  passed.push('응답 유실은 처리 조회로 복구하고 재전송하지 않음')

  phase = 'IMPLEMENTING'; questionRole = 'DEVELOPER'; await refresh()
  await click('개발', document.querySelector('.run-employee-picker'))
  await click('추가 의견'); await fill('입력 안내를 보기 쉽게 해주세요.')
  await click('기획', document.querySelector('.run-employee-picker'))
  await click('개발', document.querySelector('.run-employee-picker'))
  check(document.querySelector('textarea').value.includes('입력 안내'), 'employee-scoped memo preserved')
  phase = 'WAITING_INPUT'; await refresh()
  check(document.querySelector('textarea').value.includes('입력 안내'), 'memo becomes the answer draft without loss')
  passed.push('진행 중 메모·직원 전환·질문 도착 때 입력 보존')

  await click('작업 중단'); const before = writes().length
  check(document.querySelector('dialog').open, 'stop confirmation')
  await click('계속 작업하기', document.querySelector('dialog'))
  check(writes().length === before, 'dismissal never stops work')
  await click('작업 중단')
  await act(async () => { const button = document.querySelector('dialog .run-stop-confirm'); button.click(); button.click() })
  await settle(); await refresh()
  check(writes().length === before + 1 && JSON.parse(writes().at(-1).options.body).action === 'CANCEL', 'confirmed stop once')
  check(document.querySelector('.run-command-notice').textContent.includes('중단이 확인'), 'only confirmed cancellation is shown')
  passed.push('중단 확인·취소·한 번만 중단·서버 확인 후 종료 표시')
  check(changed > 0, 'run monitor refresh is connected')
  await act(async () => root.unmount())
  return { passed }
}
verify().then(report => { const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify(report); document.body.append(result) })
  .catch(error => { const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.stack || error.message }); document.body.append(result) })
