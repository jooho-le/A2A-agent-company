import { act, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from '../../src/pages/AtelierDashboard.jsx'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
sessionStorage.setItem('access_token', 'mock-office-request-only')
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const scenarioId = id(1), runId = id(2), stepId = id(3)
const approval = { approved: true, scenarioId, configuration: {
  architecture: 'MULTI_AGENT', startingCommitHash: 'a'.repeat(40), startingSnapshotSha256: 'b'.repeat(64),
  model: { provider: 'openai', modelId: 'mock-model', temperature: 0 },
  limits: { maxFixAttempts: 3, maxToolRetries: 2, runtimeBudgetMs: 10000 },
  environment: { containerImageDigest: `sha256:${'c'.repeat(64)}`, dependencyLockHash: `sha256:${'d'.repeat(64)}`,
    hardwareProfile: 'mock-only', networkPolicy: 'DENY', allowedHosts: [] },
} }
const run = { runId, scenarioId, status: 'PLANNING', verdict: null, codeVersion: null, fixAttempt: 0 }
const step = { runId, workflowStepId: stepId, agentRole: 'PLANNER', status: 'RUNNING', attempt: 0, codeVersion: null }
const first = { eventId: id(4), runId, workflowStepId: stepId, eventType: 'A2A_MESSAGE_SENT', actor: 'Orchestrator',
  occurredAt: '2026-11-10T00:00:00Z', attempt: 0, codeVersion: null }
let calls = [], root, host, scenario = 'ready', releasePost
function check(value, message) { if (!value) throw new Error(message) }
// All requests are handled locally. No fallback to the native fetch, even for unknown URLs.
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options })
  const parsed = new URL(url, 'http://mock.invalid'), path = parsed.pathname
  let data, status = 200
  if (path === '/api/v1/scenarios') data = { scenarios: scenario === 'unregistered' ? [] : [{ scenarioId }] }
  else if (path === '/api/v1/runs' && options.method === 'POST') {
    if (scenario === 'failure') { status = 422; data = { detail: 'mock rejection' } }
    else { if (scenario === 'pending') await new Promise(resolve => { releasePost = resolve }); data = { run, firstStep: step, dispatchStatus: 'SCHEDULED' } }
  } else if (path === `/api/v1/runs/${runId}`) data = scenario === 'finished' ? { ...run, status: 'FINISHED', verdict: 'SUCCESS' } : run
  else if (path.endsWith('/steps')) data = { runId, steps: [step] }
  else if (path.endsWith('/configuration')) data = { runId, artifactType: 'RUN_CONFIGURATION', configuration: { architecture: 'MULTI_AGENT' } }
  else if (path.endsWith('/events')) {
    const offset = Number(parsed.searchParams.get('offset')), limit = Number(parsed.searchParams.get('limit'))
    data = { runId, offset, limit, total: 2, events: [first, first].slice(offset, offset + limit) }
  } else if (path.endsWith('/issues')) data = { runId, issues: [] }
  else if (path.endsWith('/artifacts')) data = { runId, artifacts: [] }
  else throw new Error('Unexpected mock request')
  return { ok: status === 200, status, json: async () => data }
}
async function mount(approved) {
  if (root) { await act(async () => root.unmount()); host.remove() }
  calls = []; releasePost = null
  host = document.createElement('div'); document.body.append(host)
  root = createRoot(host)
  await act(async () => root.render(<StrictMode><Dashboard approvedRunSubmission={approved} /></StrictMode>))
}
function fill() {
  document.querySelector('#request-name').value = '테스트 프로젝트'
  document.querySelector('#request-text').value = '요구사항 분석'
}
async function submit(twice = false) {
  await act(async () => {
    const form = document.querySelector('#request-form')
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    if (twice) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
}
const postCount = () => calls.filter(c => c.options.method === 'POST').length
async function verify() {
  const checks = []
  await mount(null)
  const home = document.querySelector('#home-view')
  check(home.querySelectorAll('form').length === 1 && home.querySelector('.request-composer'), 'single blue input')
  check(!document.querySelector('.agent-run-monitor') && !document.querySelector('#new-form'), 'no debug or duplicate forms')
  check(home.children.length === 2, 'home has only request and office')
  check(document.querySelector('#request-form button[type=submit]').disabled, 'missing approval disables submit')
  fill(); await submit(); check(calls.length === 0, 'missing approval sends no request')
  check(document.querySelector('.composer-feedback').textContent.includes('승인된 공개 실행 설정'), 'inline reason')
  const newButton = [...document.querySelectorAll('.topbar button')].find(button => button.textContent.includes('새 업무'))
  await act(async () => newButton.click())
  check(!document.querySelector('#new-form'), 'header uses same composer')
  checks.push('single composer, no Run panel, missing approval blocks POST')

  scenario = 'unregistered'; await mount(approval); fill(); await submit()
  check(postCount() === 0 && document.querySelector('.composer-feedback [role=alert]'), 'unregistered scenario blocked inline')
  check(document.querySelector('#request-text').value === '요구사항 분석', 'rejected input preserved')
  checks.push('unregistered scenario blocks POST and preserves input')

  scenario = 'pending'; await mount(approval); fill(); await submit(true)
  check(postCount() === 1 && releasePost, 'duplicate click has one POST')
  check(document.querySelector('#request-form button[type=submit]').disabled, 'pending disabled')
  await act(async () => releasePost())
  const body = JSON.parse(calls.find(c => c.options.method === 'POST').options.body)
  check(body.requestText === '프로젝트: 테스트 프로젝트\n\n요구사항:\n요구사항 분석', 'blue inputs in actual body')
  check(body.scenarioId === scenarioId && body.configuration.model.modelId === 'mock-model', 'approved configuration reused')
  check(!Object.hasOwn(body, 'approved'), 'client approval flag not a server proof')
  check(globalThis.__officeProps.displayMode === 'actual' && globalThis.__officeProps.runDisplayState.runId === runId, 'automatic office connection')
  const trace = globalThis.__officeProps.runDisplayState.trace
  check(trace?.initialized && trace.entries.length === 1 && trace.entries[0].event.eventId === first.eventId, 'first event and dedup to office')
  check(calls.some(c => c.url.includes('/events?limit=500&offset=0')), 'fromStart preserved')
  check(calls.some(c => c.url.endsWith('/issues')) && calls.some(c => c.url.endsWith('/artifacts')), 'results still monitored')
  check(document.querySelector('.composer-feedback').textContent.includes('실행 예약'), 'SCHEDULED not complete')
  await submit(); check(postCount() === 1, 'completed submission not duplicated')
  checks.push('blue submit connects Run + first Trace + results, duplicates blocked')

  scenario = 'finished'; await mount(approval); fill(); await submit()
  const finalNotice = document.querySelector('.composer-feedback').textContent
  check(finalNotice.includes('성공적으로 완료') && !finalNotice.includes('아직'), 'final status replaces scheduled notice')

  scenario = 'failure'; await mount(approval); fill(); await submit()
  check(document.querySelector('.composer-feedback [role=alert]').textContent.includes('거절'), 'failure inline')
  check(!globalThis.__officeProps.runDisplayState.runId, 'failure does not select fake run')
  check(document.querySelector('#request-name').value === '테스트 프로젝트', 'failure preserves input')
  check(!document.querySelector('.toast'), 'no mock success toast')
  checks.push('failed POST keeps input and does not simulate success')
  await act(async () => root.unmount())
  return checks
}
verify().then(checks => {
  document.body.innerHTML = ''
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ passed: checks }); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.message }); document.body.append(result)
})
