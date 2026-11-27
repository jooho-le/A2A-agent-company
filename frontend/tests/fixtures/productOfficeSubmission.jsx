import { act, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from '../../src/pages/AtelierDashboard.jsx'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
const id = n => '70000000-0000-4000-8000-' + String(n).padStart(12, '0')
const scenarioId = '03d94bc5-1a31-473b-8527-c4acf8ea58a1'
const runId = id(1), projectId = id(2), configurationId = 'c'.repeat(64)
const token = 'test.' + btoa(JSON.stringify({ sub: '1' })) + '.test'
const run = { runId, scenarioId, status: 'RECEIVED', verdict: null, codeVersion: null, fixAttempt: 0 }
const project = { projectId, name: '회원가입 프로젝트', scenarioId, createdAt: '2026-11-18T00:00:00', updatedAt: '2026-11-18T00:00:00', runs: [] }
let root, host, calls, mode, loginCount, releasePost
const check = (value, message) => { if (!value) throw new Error(message) }
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options })
  const path = new URL(url, 'http://mock.invalid').pathname
  const key = new Headers(options.headers).get('Idempotency-Key')
  let data, status = 200
  const headers = new Headers()
  if (path === '/api/v1/host/approved-configuration') {
    data = { scenarioId, configurationId, configuration: { architecture: 'MULTI_AGENT' } }
    if (mode === 'approval-fail') status = 503
    if (mode === 'approval-401') status = 401
    if (mode === 'approval-invalid') data.configurationId = 'invalid'
  } else if (path === '/api/v1/runs' && options.method === 'POST') {
    if (mode === 'pending') await new Promise(resolve => { releasePost = resolve })
    data = { run, firstStep: { runId }, dispatchStatus: 'SCHEDULED', requestId: key, projectId, submissionStatus: 'CREATED' }
    status = mode === 'replayed' ? 200 : 201
    if (mode === 'unknown') { status = 202; data = { requestId: key, projectId, submissionStatus: 'UNKNOWN', run } }
    if (mode === 'invalid-202') status = 202 // CREATED와 202가 모순되면 성공으로 처리하지 않습니다.
    if (mode === 'configuration-conflict') { status = 409; data = { detail: 'CONFIGURATION_CHANGED' } }
    if (mode === 'key-conflict') { status = 409; data = { detail: 'IDEMPOTENCY_KEY_CONFLICT' } }
    if (mode === '401') { status = 401; data = { detail: 'private-mock-error-must-not-display' } }
    if (mode === '502' || mode === '503' || mode === 'not-accepted') {
      status = mode === '502' ? 502 : 503; data = { detail: 'private-mock-error-must-not-display' }
      if (mode === 'not-accepted') headers.set('X-Submission-Status', 'NOT_ACCEPTED')
    }
    if (mode === 'network') throw new Error('private-mock-error-must-not-display')
  } else if (path.endsWith('/retire') && options.method === 'POST') {
    const requestId = path.split('/').at(-2)
    data = mode === 'cleanup-missing' ? { requestId, outcome: 'RETIRED' }
      : { requestId, outcome: 'EXISTING', submission: { requestId, projectId,
          submissionStatus: mode === 'cleanup-existing' ? 'CREATED' : 'UNKNOWN',
          run, firstStep: { runId }, dispatchStatus: 'SCHEDULED' } }
  } else if (path.startsWith('/api/v1/submissions/')) {
    status = 404; data = { detail: '접수 기록을 찾을 수 없습니다.' }
  } else if (path === '/api/projects/' + projectId) data = project
  else if (path === '/api/projects') data = { projects: [] }
  else if (path === '/api/v1/runs/' + runId) data = run
  else if (path.endsWith('/configuration')) data = { runId, artifactType: 'RUN_CONFIGURATION', configuration: { architecture: 'MULTI_AGENT' } }
  else if (path.endsWith('/steps')) data = { runId, steps: [] }
  else if (path.endsWith('/events')) {
    const search = new URL(url, 'http://mock.invalid').searchParams
    data = { runId, events: [], offset: Number(search.get('offset')), limit: Number(search.get('limit')), total: 0 }
  } else if (path.endsWith('/issues')) data = { runId, issues: [] }
  else if (path.endsWith('/artifacts')) data = { runId, artifacts: [] }
  else throw new Error('Unexpected Mock path')
  return { ok: status >= 200 && status < 300, status, headers, json: async () => data }
}

async function mount(next, { preserveStorage = false, guest = false } = {}) {
  if (root) { await act(async () => root.unmount()); host.remove() }
  if (!preserveStorage) sessionStorage.clear()
  if (!guest) sessionStorage.setItem('access_token', token)
  calls = []; mode = next; loginCount = 0; releasePost = null
  window.history.replaceState(null, '', '#/home')
  host = document.createElement('div'); document.body.append(host); root = createRoot(host)
  await act(async () => root.render(<StrictMode><Dashboard onLogin={() => { loginCount++ }} /></StrictMode>))
}

function fill() {
  document.querySelector('#request-name').value = '회원가입 프로젝트'
  document.querySelector('#request-text').value = '이메일 회원가입 웹 사이트를 만들어줘.'
}
async function submit(twice = false) {
  await act(async () => {
    const form = document.querySelector('#request-form')
    form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
    if (twice) form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))
  })
}
const posts = () => calls.filter(call => call.options.method === 'POST')
const feedback = () => document.querySelector('.composer-feedback').textContent
const noRun = () => check(!globalThis.__officeProps.runDisplayState.runId, 'unconfirmed receipt must not select/animate a Run')

async function verify() {
  await mount('pending'); fill(); await submit(true)
  check(posts().length === 1 && releasePost, 'double click sends one POST')
  await act(async () => releasePost())
  check(globalThis.__officeProps.runDisplayState.runId === runId, '201 connects existing Run selection')
  check(feedback().includes('새 Run') && feedback().includes('예약'), '201 is scheduled, not finished')
  const body = JSON.parse(posts()[0].options.body)
  check(body.scenarioId === scenarioId && body.configurationId === configurationId && !('configuration' in body), 'product request fields')
  check(new Headers(posts()[0].options.headers).get('Authorization') === 'Bearer ' + token, 'product JWT')
  await submit(); check(posts().length === 1, 'accepted submit stays locked')
  await mount('replayed'); fill(); await submit()
  check(globalThis.__officeProps.runDisplayState.runId === runId && feedback().includes('기존 Run을 재사용'), '200 reuses selected Run')

  await mount('ready', { guest: true }); fill(); await submit()
  check(posts().length === 0, 'no JWT blocks creation')
  for (const problem of ['approval-fail', 'approval-invalid', 'approval-401']) {
    await mount(problem); fill(); await submit()
    check(posts().length === 0 && feedback(), 'failed approval blocks POST')
    noRun()
    if (problem === 'approval-401') {
      await act(async () => document.querySelector('.composer-bottom button').click())
      check(loginCount === 1, 'approval 401 enters existing login')
    }
  }
  for (const problem of ['unknown', 'invalid-202', 'key-conflict', '502', '503', 'network']) {
    await mount(problem); fill(); await submit(true); await submit()
    check(posts().length === 1, problem + ': no automatic/duplicate POST')
    noRun()
    check(!feedback().includes('private-mock-error-must-not-display') && feedback().includes('접수'), 'safe recovery notice')
    if (problem === 'key-conflict') check(feedback().includes('접수 키') && feedback().includes('충돌'), 'key conflict distinguished')
    if (problem === '502') check(feedback().includes('Host 서비스 인증'), 'Host authentication is distinguished')
    if (problem === '503') check(feedback().includes('준비되지'), 'Host readiness failure is visible')
    const key = sessionStorage.getItem('ai-company.pending-office-submission.1')
    check(key === new Headers(posts()[0].options.headers).get('Idempotency-Key'), 'same pending key retained')
    await mount(problem, { preserveStorage: true }); fill(); await submit()
    check(posts().length === 0 && sessionStorage.getItem('ai-company.pending-office-submission.1') === key, 'remount never replaces uncertain key')
    noRun()
  }
  for (const problem of ['configuration-conflict', 'not-accepted', '401']) {
    await mount(problem); fill(); await submit(true); await submit()
    check(posts().length === 1 && !sessionStorage.getItem('ai-company.pending-office-submission.1'), 'known refusal never auto-reposts')
    noRun()
    if (problem === 'configuration-conflict') check(feedback().includes('승인 설정이 변경'), 'configuration conflict distinguished')
    if (problem === '401') {
      check(feedback().includes('로그인') && !feedback().includes('private-mock'), 'safe 401 message')
      await act(async () => document.querySelector('.composer-bottom button').click())
      check(loginCount === 1, 'expired JWT returns to existing login')
    }
  }
  const pendingKey = 'ai-company.pending-office-submission.1'
  for (const next of ['cleanup-missing', 'cleanup-existing', 'cleanup-unknown']) {
    await mount(next)
    const key = id(77)
    sessionStorage.setItem(pendingKey, key)
    await mount(next, { preserveStorage: true })
    fill()
    await submit()
    check(posts().length === 0, 'restored pending key still blocks new creation')
    const click = label => act(async () => Array.from(document.querySelectorAll('.composer-feedback button')).find(b => b.textContent === label).click())
    await click('접수 상태 확인')
    check(feedback().includes('접수 기록') && !feedback().includes('Run을 찾을'), 'submission 404 is not a Run error')
    check(sessionStorage.getItem(pendingKey) === key && posts().length === 0, '404 alone never unlocks or posts')
    await click('이전 요청 확인하고 다시 입력')
    check(posts().length === 1 && posts()[0].url === '/api/v1/submissions/' + key + '/retire', 'explicit cleanup only retires the original key')
    check(sessionStorage.getItem('access_token') === token, 'JWT preserved')
    if (next === 'cleanup-missing') {
      check(!sessionStorage.getItem(pendingKey), 'confirmed retirement releases only the pending marker')
      check(!document.querySelector('#request-name').disabled && !document.querySelector('#request-text').disabled, 'inputs re-enabled')
      check(document.querySelector('#request-name').value === '회원가입 프로젝트', 'input draft preserved')
      check(!document.querySelector('.composer-bottom button').disabled, 'start button ready')
      check(!posts().some(call => call.url === '/api/v1/runs'), 'cleanup never automatically creates a new Run')
      await submit()
      check(posts().filter(call => call.url === '/api/v1/runs').length === 1, 'next explicit click creates once')
    } else if (next === 'cleanup-existing') {
      check(globalThis.__officeProps.runDisplayState.runId === runId, 'existing Run restored instead of discarded')
    } else {
      check(sessionStorage.getItem(pendingKey) === key && feedback().includes('서버에 접수된 요청'), 'unknown server receipt remains locked')
      noRun()
    }
  }
  await act(async () => root.unmount())
  return { success: true, failures: true, retirement: true }
}
verify().then(report => {
  document.body.innerHTML = ''
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify(report); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.message }); document.body.append(result)
})
