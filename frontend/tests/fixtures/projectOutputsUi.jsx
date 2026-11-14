import { act, StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from '../../src/pages/AtelierDashboard.jsx'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
sessionStorage.setItem('access_token', 'mock-project-outputs-only')
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
let calls = [], root, host, scenario = 'ready'
function check(value, message) { if (!value) throw new Error(message) }
// All requests are handled locally. No fallback to the native fetch, even for unknown URLs.
globalThis.fetch = async (url, options = {}) => {
  calls.push({ url, options })
  const parsed = new URL(url, 'http://mock.invalid'), path = parsed.pathname
  let data, status = 200
  if (path === '/api/projects') data = { projects: [] }
  else if (path === '/api/v1/scenarios') data = { scenarios: scenario === 'unregistered' ? [] : [{ scenarioId }] }
  else if (path === '/api/v1/runs' && options.method === 'POST') {
    if (scenario === 'failure') { status = 422; data = { detail: 'mock rejection' } }
    else { data = { run, firstStep: step, dispatchStatus: 'SCHEDULED' } }
  } else if (path === `/api/v1/runs/${runId}`) data = scenario === 'finished' ? { ...run, status: 'FINISHED', verdict: 'SUCCESS' } : run
  else if (path.endsWith('/steps')) data = { runId, steps: [step] }
  else if (path.endsWith('/configuration')) data = { runId, artifactType: 'RUN_CONFIGURATION', configuration: { architecture: 'MULTI_AGENT' } }
  else if (path.endsWith('/events')) {
    const offset = Number(parsed.searchParams.get('offset')), limit = Number(parsed.searchParams.get('limit'))
    data = { runId, offset, limit, total: 2, events: [first, first].slice(offset, offset + limit) }
  } else if (path.endsWith('/issues')) data = { runId, issues: [] }
  else if (path.endsWith('/artifacts')) {
    if (scenario === 'artifact-error') { status = 503; data = { detail: 'mock error' } }
    else data = { runId, artifacts: scenario === 'empty' ? [] : artifacts }
  }
  else throw new Error('Unexpected mock request')
  return { ok: status === 200, status, json: async () => data }
}
async function mount(approved) {
  if (root) { await act(async () => root.unmount()); host.remove() }
  calls = []
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
const artifacts = [
  { runId, artifactId: id(10), artifactType: 'RUN_CONFIGURATION', createdBy: 'Orchestrator', artifactVersion: 1,
    configuration: { internalPath: '/private/mock-only', password: 'hidden-test-value' } },
  { runId, artifactId: id(11), artifactType: 'SOURCE', createdBy: 'DEVELOPER', artifactVersion: 1,
    artifactUri: `artifact://${id(11)}/source.bundle`, createdAt: '2026-11-10T00:00:00Z', codeVersion: 1,
    snapshotUri: 'file:///private/mock-only', executionManifest: { password: 'hidden-test-value' } },
  { runId, artifactId: id(12), artifactType: 'CHANGE_REPORT', createdBy: 'DEVELOPER', artifactVersion: 1,
    artifactUri: `artifact://${id(12)}/change-report.json`, summary: '입력 검증 추가',
    fileChanges: [{ path: 'src/form.js', action: 'MODIFIED' }] },
  { runId, artifactId: id(13), artifactType: 'QA_REPORT', createdBy: 'QA', artifactVersion: 1,
    tests: [{ title: '입력 검사', outcome: 'FAIL', details: '입력 안내를 확인해야 합니다.' }] },
]
async function navigate(view) { await act(async () => document.querySelector(`[data-view="${view}"]`).click()) }
async function verify() {
  const checks = []
  await mount(null); await navigate('projects')
  check(document.querySelector('#projects-view').textContent.includes('저장된 프로젝트가 없습니다'), 'projects empty')
  check(!document.querySelector('#projects-view .project-card'), 'no demo projects')
  await navigate('outputs')
  check(document.querySelector('#outputs-view').textContent.includes('접수한 실제 업무가 없습니다'), 'outputs empty')
  check(!document.querySelector('#outputs-view .output-card') && calls.every(call => call.url === 'http://127.0.0.1:8000/api/projects'), 'no demo files or Run API calls without run')
  checks.push('empty projects and outputs contain no demo data')

  scenario = 'ready'; await mount(approval); await navigate('home'); fill(); await submit()
  await navigate('projects')
  const card = document.querySelector('#projects-view .project-card')
  check(card.textContent.includes('테스트 프로젝트') && card.textContent.includes('요구사항 분석'), 'name and run state')
  check(card.textContent.includes('계획에이전트 · 작업') && !card.textContent.includes('%'), 'step state and no invented percent')
  check(!card.querySelector('.progress-track'), 'no numeric progress graphic')
  await act(async () => card.click())
  check(globalThis.__officeProps.runDisplayState.runId === runId && globalThis.__officeProps.displayMode === 'actual', 'project selects same actual office run')
  check(postCount() === 1, 'project navigation does not create runs')
  checks.push('project card connects current Run and Step without progress percentage')

  await navigate('outputs')
  const output = document.querySelector('#outputs-view')
  check(output.querySelectorAll('.output-card').length === 4, 'artifact list reused')
  check(output.textContent.includes('source.bundle') && output.textContent.includes('입력 검증 추가'), 'registered names and summary')
  check(output.textContent.includes('src/form.js') && output.textContent.includes('입력 검사 · FAIL'), 'report paths and verdicts')
  check(!output.querySelector('a') && !output.querySelector('.file-open') && !output.querySelector('.output-actions'), 'no invented content/download action')
  check(!output.textContent.includes('hidden-test-value') && !output.textContent.includes('/private/mock-only'), 'private data absent')
  check(output.textContent.includes('다운로드 API는 제공되지 않습니다'), 'unsupported download explained')
  checks.push('artifact metadata/report summary shown without secrets or fake downloads')

  scenario = 'artifact-error'; await navigate('home'); await navigate('outputs')
  check(output.querySelector('[role=alert]') && !output.textContent.includes('아직 등록된 산출물이 없습니다'), 'error is not empty result')
  scenario = 'empty'
  await act(async () => output.querySelector('[role=alert] button').click())
  check(output.textContent.includes('아직 등록된 산출물이 없습니다') && !output.querySelector('.output-card'), 'retry empty response clears old artifacts')
  check(postCount() === 1, 'retry only GET')
  checks.push('artifact error and empty results are distinct; retry uses GET only')
  await act(async () => root.unmount())
  return checks
}
verify().then(checks => {
  document.body.innerHTML = ''
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ passed: checks }); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.message }); document.body.append(result)
})
