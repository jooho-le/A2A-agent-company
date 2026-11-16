import { act, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from '../../src/pages/AtelierDashboard.jsx'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
const id = n => '70000000-0000-4000-8000-' + String(n).padStart(12, '0')
const scenarioId = id(1), runId = id(2), stepId = id(3), projectId = id(5)
const configurationId = 'c'.repeat(64)
const run = { runId, scenarioId, status: 'PLANNING', verdict: null, codeVersion: null, fixAttempt: 0 }
const step = { runId, workflowStepId: stepId, agentRole: 'PLANNER', status: 'RUNNING', attempt: 0, codeVersion: null }
const first = { eventId: id(4), runId, workflowStepId: stepId, eventType: 'A2A_MESSAGE_SENT', actor: 'Orchestrator', occurredAt: '2026-11-16T00:00:00Z', attempt: 0, codeVersion: null }
const project = { projectId, name: '테스트 프로젝트', scenarioId, createdAt: '2026-11-16T00:00:00', updatedAt: '2026-11-16T00:00:00', runs: [{ hostKey: 'local-multi', runId, configurationId, linkedAt: '2026-11-16T00:00:00' }] }
let calls = [], root, host, scenario = 'ready', releasePost, receipt
function check(value, message) { if (!value) throw new Error(message) }
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options })
  const path = new URL(url, 'http://mock.invalid').pathname
  let data, status = 200
  if (path === '/api/v1/scenarios') {
    if (scenario === 'offline') { status = 503; data = { detail: 'Host unavailable' } }
    else data = { scenarios: [{ scenarioId }] }
  } else if (path === '/api/v1/host/approved-configuration') data = { scenarioId, configurationId, configuration: { architecture: 'MULTI_AGENT' } }
  else if (path === '/api/v1/runs' && options.method === 'POST') {
    if (scenario === 'pending') await new Promise(resolve => { releasePost = resolve })
    const key = new Headers(options.headers).get('Idempotency-Key')
    receipt = { run, firstStep: step, dispatchStatus: 'SCHEDULED', submissionStatus: 'CREATED', requestId: key, projectId }
    data = scenario === 'unknown' ? { submissionStatus: 'UNKNOWN', projectId, requestId: key } : receipt
  } else if (path.startsWith('/api/v1/submissions/')) data = receipt
  else if (path === '/api/projects/' + projectId) data = project
  else if (path === '/api/projects') data = { projects: [project] }
  else if (path === '/api/v1/runs/' + runId) data = scenario === 'finished' ? { ...run, status: 'FINISHED', verdict: 'SUCCESS' } : run
  else if (path.endsWith('/steps')) data = { runId, steps: [step] }
  else if (path.endsWith('/configuration')) data = { runId, artifactType: 'RUN_CONFIGURATION', configuration: { architecture: 'MULTI_AGENT' } }
  else if (path.endsWith('/events')) {
    const search = new URL(url, 'http://mock.invalid').searchParams
    const offset = Number(search.get('offset')), limit = Number(search.get('limit'))
    data = { runId, offset, limit, total: 1, events: [first].slice(offset, offset + limit) }
  } else if (path.endsWith('/issues')) data = { runId, issues: [] }
  else if (path.endsWith('/artifacts')) data = { runId, artifacts: [] }
  else throw new Error('Unexpected local test request: ' + path)
  return { ok: status === 200, status, json: async () => data }
}
async function mount(authenticated = true) {
  if (root) { await act(async () => root.unmount()); host.remove() }
  sessionStorage.clear()
  if (authenticated) sessionStorage.setItem('access_token', 'test-product-jwt')
  calls = []; releasePost = null; receipt = null
  window.history.replaceState(null, '', '#/home')
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<StrictMode><Dashboard /></StrictMode>))
}
function fill() {
  document.querySelector('#request-name').value = '테스트 프로젝트'
  document.querySelector('#request-text').value = '이메일로 회원가입하는 기능을 만들어줘.'
}
async function submit(twice = false) {
  await act(async () => {
    const form = document.querySelector('#request-form')
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    if (twice) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
}
const postCount = () => calls.filter(call => call.options.method === 'POST').length
async function verify() {
  const checks = []
  await mount(false)
  check(document.querySelectorAll('#request-form').length === 1, 'one composer')
  check(!document.querySelector('[data-office3d=timeline]') && !document.querySelector('[data-office3d=error-mode]'), 'no presentation controls')
  check(document.querySelector('#request-form button[type=submit]').disabled, 'login required')
  fill(); await submit(); check(postCount() === 0, 'no guest POST')
  checks.push('guest cannot submit; no mock presentation controls')

  scenario = 'offline'; await mount()
  check(document.querySelector('#request-form button[type=submit]').disabled, 'offline blocks submit')
  check(document.querySelector('.composer-feedback').textContent.includes('준비되지'), 'offline notice')
  fill(); await submit(); check(postCount() === 0, 'offline does not invent approval')
  checks.push('server readiness comes from backend; offline does not start fake work')

  scenario = 'pending'; await mount(); fill(); await submit(true)
  check(postCount() === 1 && releasePost, 'one POST for double click')
  await act(async () => releasePost())
  const post = calls.find(call => call.options.method === 'POST')
  const body = JSON.parse(post.options.body)
  check(body.projectName === '테스트 프로젝트' && body.requestText === '이메일로 회원가입하는 기능을 만들어줘.', 'natural language and project name are separate')
  check(body.configurationId === configurationId && !Object.hasOwn(body, 'configuration'), 'server-owned configuration')
  check(new Headers(post.options.headers).get('Authorization') === 'Bearer test-product-jwt', 'product JWT required')
  check(/^[\w-]{36}$/.test(new Headers(post.options.headers).get('Idempotency-Key')), 'request identifier')
  check(globalThis.__officeProps.runDisplayState.runId === runId, 'office connected to real receipt')
  check(globalThis.__officeProps.runDisplayState.trace?.entries.length === 1, 'Trace connected')
  check(!document.querySelector('.composer-feedback').textContent.includes('성공적으로 완료'), 'scheduled is not success')
  await submit(); check(postCount() === 1, 'accepted submit locked')
  checks.push('JWT + idempotency + natural input + Run/Trace connection; duplicate POST blocked')

  scenario = 'unknown'; await mount(); fill(); await submit()
  check(document.querySelector('#request-form button[type=submit]').disabled, 'unknown blocks another creation')
  const recover = [...document.querySelectorAll('.composer-feedback button')].find(button => button.textContent.includes('접수 상태 확인'))
  check(recover, 'explicit lookup control')
  await act(async () => recover.click())
  check(postCount() === 1, 'GET recovery never repeats POST')
  check(globalThis.__officeProps.runDisplayState.runId === runId, 'recovered Run connected')
  checks.push('lost response recovers by the same request ID without a second POST')
  await act(async () => root.unmount())
  return checks
}
verify().then(checks => {
  document.body.innerHTML = ''
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ passed: checks }); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.message }); document.body.append(result)
})
