import { act, useEffect, useState } from 'react'
import { createRoot } from 'react-dom/client'
import Dashboard from '../../src/pages/AtelierDashboard.jsx'
import useAgentRunMonitor from '../../src/hooks/useAgentRunMonitor.js'
import { buildAgentRunDisplayState } from '../../src/components/atelier/agentRunDisplayState.js'
import { createOfficeTraceController } from '../../src/components/atelier/officeTraceEvents.js'
import { createOfficeRunController } from '../../src/components/atelier/office3dRunController.js'
import { buildOfficeRunPopups } from '../../src/components/atelier/officeRunPopups.js'
import { managementDisplay } from '../../src/components/atelier/office3dOrchestrator.js'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
const id = n => '80000000-0000-4000-8000-' + String(n).padStart(12, '0')
const scenarioId = 'f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76', a = id(1), b = id(2), projectId = id(3)
const token = 'mock.' + btoa(JSON.stringify({ sub: '1' })) + '.mock'
const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
const check = (condition, message) => { if (!condition) throw new Error(message) }
const equal = (actual, expected, message) => check(JSON.stringify(actual) === JSON.stringify(expected), message)
const run = runId => ({ runId, scenarioId, status: 'VALIDATING', verdict: null, codeVersion: 1, fixAttempt: 0 })
const steps = runId => roles.map((agentRole, index) => ({ runId, workflowStepId: id(20 + index), agentRole,
  status: index < 2 ? 'SUCCEEDED' : 'RUNNING', attempt: 0, codeVersion: index ? 1 : null, inputArtifactIds: [id(40)] }))
const event = (n, runId = a, role = null) => ({ eventId: id(100 + n), runId, eventType: role === null ? 'UNBOUND_EVENT' : 'A2A_MESSAGE_SENT',
  actor: 'Orchestrator', workflowStepId: role === null ? null : id(20 + role), attempt: 0, codeVersion: role ? 1 : null,
  occurredAt: role === 0 ? '2026-11-18T00:00:10Z' : '2026-11-18T00:00:00Z' })
let rows, calls, root, container, mode, httpFailure, failOffset, held, holdOffset, loginCount, monitor
const timers = new Map()
let timerId = 100000
const nativeSetTimeout = globalThis.setTimeout.bind(globalThis), nativeClearTimeout = globalThis.clearTimeout.bind(globalThis)
globalThis.setTimeout = (fn, delay, ...args) => {
  if ([2000, 4000, 8000].includes(delay)) { const key = ++timerId; timers.set(key, { fn, delay }); return key }
  return nativeSetTimeout(fn, delay, ...args)
}
globalThis.clearTimeout = key => { if (!timers.delete(key)) nativeClearTimeout(key) }
globalThis.fetch = async (url, options = {}) => {
  const parsed = new URL(url, 'http://mock.invalid'), path = parsed.pathname
  calls.push({ path, search: parsed.searchParams, options })
  check(new Headers(options.headers).get('Authorization') === 'Bearer ' + token, 'product JWT on every GET/POST')
  let data, status = 200
  if (path === '/api/v1/host/approved-configuration') data = { scenarioId, configurationId: 'c'.repeat(64), configuration: { architecture: 'MULTI_AGENT' } }
  else if (path === '/api/v1/runs' && options.method === 'POST') {
    status = mode === 'unknown' || mode === 'malformed' ? 202 : mode === 'reused' ? 200 : 201
    data = { requestId: new Headers(options.headers).get('Idempotency-Key'), projectId,
      submissionStatus: mode === 'unknown' ? 'UNKNOWN' : 'CREATED', run: run(a), firstStep: { runId: a }, dispatchStatus: 'SCHEDULED' }
    if (mode === 'no-run-id') data.run = {}
  } else if (path === '/api/projects/' + projectId) data = { projectId, name: 'Mock 업무', scenarioId,
    createdAt: '2026-11-18T00:00:00', updatedAt: '2026-11-18T00:00:00', runs: [] }
  else if (path === '/api/projects') data = { projects: [] }
  else if (path.startsWith('/api/v1/runs/')) {
    const [, , , , runId, resource] = path.split('/')
    if (!resource) { data = run(runId); status = httpFailure || 200 }
    else if (resource === 'steps') data = { runId, steps: steps(runId) }
    else if (resource === 'configuration') data = { runId, artifactType: 'RUN_CONFIGURATION', configuration: { architecture: 'MULTI_AGENT' } }
    else if (resource === 'artifacts') data = { runId, artifacts: [] }
    else if (resource === 'issues') data = { runId, issues: [] }
    else if (resource === 'events') {
      const offset = Number(parsed.searchParams.get('offset')), limit = Number(parsed.searchParams.get('limit'))
      check(limit === 100, 'all event GETs use limit 100')
      data = { runId, limit, offset, total: rows[runId].length, events: rows[runId].slice(offset, offset + limit) }
      if (offset === failOffset) { status = 503; failOffset = null }
      if (runId === a && offset === holdOffset) await new Promise(resolve => { held = { resolve, signal: options.signal }; holdOffset = null })
    } else throw new Error('Unexpected Run resource')
  } else throw new Error('Unexpected Mock path')
  return { ok: status >= 200 && status < 300, status, headers: new Headers(), json: async () => data }
}

export function Harness({ runId }) {
  const value = useAgentRunMonitor(runId, { polling: true, collectEvents: true, includeResults: true, includeArchitecture: true, traceFromStart: true })
  useEffect(() => { monitor = value }, [value])
  return null
}
export function Office() {
  const [authenticated, setAuthenticated] = useState(true)
  return <Dashboard hasAccessToken={authenticated} onLogin={() => { loginCount++ }}
    onLogout={() => { sessionStorage.removeItem('access_token'); setAuthenticated(false) }} />
}
async function mount(kind, nextMode = 'new') {
  if (root) { await act(async () => root.unmount()); container.remove() }
  check(timers.size === 0, 'unmount clears polling timer')
  rows = { [a]: [event(1, a, 0)], [b]: [event(1, b, 3)] }
  calls = []; mode = nextMode; httpFailure = 0; failOffset = null; holdOffset = null; held = null; loginCount = 0
  sessionStorage.clear(); sessionStorage.setItem('access_token', token)
  window.history.replaceState(null, '', '#/home')
  container = document.createElement('div'); document.body.append(container); root = createRoot(container)
  if (kind === 'office') await act(async () => root.render(<Office />))
  else await act(async () => root.render(<Harness runId="" />))
}
async function start() {
  document.querySelector('#request-name').value = 'Mock 업무'
  document.querySelector('#request-text').value = '회원가입 기능을 만들어줘.'
  await act(async () => document.querySelector('#request-form').dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })))
}
async function tick(delay = 2000) {
  check(timers.size === 1, 'only one polling timer')
  const [key, task] = timers.entries().next().value
  check(task.delay === delay, `expected polling delay ${delay}, got ${task.delay}`)
  timers.delete(key)
  await act(async () => task.fn())
}
const eventCalls = runId => calls.filter(call => call.path === `/api/v1/runs/${runId}/events`)
const state = () => buildAgentRunDisplayState(monitor)
async function created() {
  for (const receipt of ['new', 'reused']) {
    await mount('office', receipt); await start()
    const snapshot = globalThis.__officeProps.runDisplayState
    check(snapshot.runId === a && snapshot.trace.entries.length === 1, '200/201 selects Run and receives first Trace')
    equal(eventCalls(a).map(call => Number(call.search.get('offset'))), [0], 'no duplicate historical page GET')
    check(snapshot.agents[2].displayState === 'working' && snapshot.agents[3].displayState === 'working', 'QA/Security stay independent')
    await tick()
    equal(eventCalls(a).map(call => Number(call.search.get('offset'))), [0, 1], 'next GET starts at saved offset')
  }
}
async function trace() {
  await mount('hook')
  const startEvent = { ...event(0), eventType: 'RUN_STARTED' }
  rows[a] = [startEvent, ...roles.map((_, index) => event(index + 1, a, index)), event(1, a, 0),
    ...Array.from({ length: 199 }, (_, index) => event(index + 10))]
  await act(async () => root.render(<Harness runId={a} />))
  equal(eventCalls(a).map(call => Number(call.search.get('offset'))), [0, 100, 200], 'sequential full paging')
  check(monitor.trace.cursor === 205 && monitor.trace.entries.length === 204, 'duplicate consumes row cursor once, not animation twice')
  const controller = createOfficeTraceController(), agents = createOfficeRunController()
  controller.observe(state(), 'actual'); agents.setSnapshot(state())
  const rendered = agents.advance(.1)
  check(rendered[2].displayState === 'working' && rendered[3].displayState === 'working', 'real Step state feeds character controller')
  check(buildOfficeRunPopups(state(), [...rendered, managementDisplay(state())], controller.info()).length === 5, 'five actual popup mappings reused')
  const handoffs = [], seen = new Set()
  for (let i = 0; i < 1000; i++) {
    for (const action of controller.advance(.1)) {
      if (action.kind === 'handoff' && !seen.has(action.eventId)) { seen.add(action.eventId); handoffs.push(action.to) }
    }
  }
  equal(handoffs, [0, 1, 2, 3], 'document targets follow stored order, not occurredAt sorting')
  await tick(); controller.observe(state(), 'actual')
  check(controller.info().pending === 0, 'no duplicate animation on repeated GET')
  rows[a].push(...Array.from({ length: 150 }, (_, index) => event(index + 300)))
  failOffset = 305
  await tick()
  check(monitor.trace.cursor === 205 && monitor.run.status === 'VALIDATING', 'failed page keeps cursor and confirmed Run status')
  const mark = eventCalls(a).length
  await tick(2000)
  equal(eventCalls(a).slice(mark).map(call => Number(call.search.get('offset'))), [205, 305], 'retry does not lose uncommitted page')
  check(monitor.trace.cursor === 355, 'recovered cursor commits all pages')
  holdOffset = 355
  await tick()
  check(held && timers.size === 0, 'no overlapping polls while GET waits')
  await act(async () => root.render(<Harness runId={b} />))
  check(held.signal.aborted, 'previous Run request aborted')
  await act(async () => held.resolve())
  check(monitor.runId === b && monitor.trace.entries.length === 1 && monitor.trace.entries[0].event.runId === b, 'late old response cannot overwrite new Run')
  controller.observe(state(), 'actual')
  check(controller.advance(.1)[0]?.to === 3, 'same eventId on different Run is independently handled')
}
async function errors() {
  for (const receipt of ['unknown', 'malformed', 'no-run-id']) {
    await mount('office', receipt); await start()
    check(!globalThis.__officeProps.runDisplayState.runId && !eventCalls(a).length && timers.size === 0, '202 never starts monitoring')
  }
  await mount('hook'); await act(async () => root.render(<Harness runId={a} />))
  httpFailure = 503
  const startCount = calls.filter(call => call.path === '/api/v1/runs/' + a).length
  for (const delay of [2000, 2000, 4000, 8000]) await tick(delay)
  check(monitor.polling.state === 'exhausted' && timers.size === 0, 'initial failure + three retries then stop')
  check(calls.filter(call => call.path === '/api/v1/runs/' + a).length - startCount === 4, 'bounded Run GET count')
  check(monitor.run.status === 'VALIDATING' && !state().run.isFinalSuccess, 'transport failure is not a Workflow failure/success')
  httpFailure = 0
  await act(async () => monitor.refresh())
  check(monitor.polling.state === 'active' && timers.size === 1, 'explicit refresh can recover')
  for (const status of [401, 403, 404]) {
    httpFailure = status
    await tick()
    check(monitor.polling.state === 'blocked' && timers.size === 0, 'access error stops automatic GET')
    check(monitor.errors.run.status === status, 'safe HTTP status preserved')
    httpFailure = 0; await act(async () => monitor.refresh())
  }
  await mount('office'); httpFailure = 401; await start()
  const login = [...document.querySelectorAll('button')].find(button => button.textContent === '다시 로그인')
  check(login && timers.size === 0, '401 exposes existing login callback without retry loop')
  await act(async () => login.click()); check(loginCount === 1, 'existing login entry invoked')
  await mount('office'); await start(); holdOffset = 1; await tick()
  await act(async () => document.querySelector('.owner-profile').click())
  await act(async () => [...document.querySelectorAll('#atelier-account-menu button')].find(button => button.textContent === '로그아웃').click())
  check(held.signal.aborted && timers.size === 0 && !globalThis.__officeProps.runDisplayState.runId, 'logout clears selected Run and aborts GET')
  const before = calls.length
  await act(async () => held.resolve())
  check(calls.length === before && timers.size === 0, 'late logout response never restarts polling')
  await act(async () => root.unmount()); root = null
  check(timers.size === 0, 'unmount leaves no polling timer')
}
const results = {}
async function verify() {
  for (const [key, fn] of Object.entries({ created, trace, errors })) { await fn(); results[key] = true }
}
verify().catch(error => { results.failed = error.message }).finally(() => {
  const node = document.createElement('pre'); node.id = 'test-result'; node.textContent = JSON.stringify(results); document.body.append(node)
})
