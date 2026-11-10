import assert from 'node:assert/strict'
import test from 'node:test'
import { createAgentRun, getAgentRunScenarios } from '../src/api/agentRuns.js'
import { createRunSubmission, dispatchMessage, publicRunConfiguration } from '../src/api/agentRunSubmission.js'
import { initialRunSelection, runQueryReducer } from '../src/hooks/agentRunSelection.js'
import { createRunEventFeed } from '../src/hooks/agentRunEventFeed.js'
import { createOfficeTraceController } from '../src/components/atelier/officeTraceEvents.js'

const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const runId = id(1), scenarioId = id(2)
// 테스트 전용 공개 계약입니다. 앱·운영 설정에는 공급하지 않습니다.
const configuration = { architecture: 'MULTI_AGENT', startingCommitHash: 'a'.repeat(40), startingSnapshotSha256: 'b'.repeat(64),
  model: { provider: 'openai', modelId: 'mock-model', modelRevision: null, temperature: 0, seed: null },
  limits: { maxFixAttempts: 3, maxToolRetries: 2, runtimeBudgetMs: 10000 },
  environment: { containerImageDigest: 'sha256:' + 'c'.repeat(64), dependencyLockHash: 'sha256:' + 'd'.repeat(64), hardwareProfile: 'mock-linux', networkPolicy: 'DENY', allowedHosts: [] },
  protectedTestSuiteRef: null, scannerProfileRef: null, experimentId: null, warmupState: null, executionOrder: null }
const approval = { approved: true, scenarioId, configuration }
const request = { approval, requestText: '  테스트용 업무 요청  ', confirmed: true }
const response = dispatchStatus => ({ run: { runId, scenarioId, status: 'RECEIVED', verdict: null }, firstStep: { workflowStepId: id(3), agentRole: 'PLANNER', status: 'PENDING' }, dispatchStatus })
const json = (body, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
function page(rows, calls = []) {
  return async (selectedRun, { limit, offset }) => {
    calls.push({ limit, offset })
    return { runId: selectedRun, total: rows.length, limit, offset, events: rows.slice(offset, offset + limit) }
  }
}
function events(selectedRun = runId) {
  const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
  const steps = roles.map((agentRole, index) => ({ runId: selectedRun, workflowStepId: id(10 + index), agentRole, codeVersion: index ? 1 : null, attempt: 0, inputArtifactIds: index >= 2 ? [id(20)] : [] }))
  const rows = steps.map((step, index) => ({ eventId: id(30 + index), runId: selectedRun, workflowStepId: step.workflowStepId,
    eventType: 'A2A_MESSAGE_SENT', actor: 'ORCHESTRATOR', attempt: 0, codeVersion: step.codeVersion, occurredAt: '2026-11-09T14:00:00Z' }))
  return { steps, rows }
}

test('Mock POST: 승인·등록 시나리오 확인 후 자동 Run 선택, 중복 클릭은 한 요청', async t => {
  const calls = []; let releasePost
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, options })
    if (url === '/api/v1/scenarios') return json({ scenarios: [{ scenarioId }] })
    assert.equal(url, '/api/v1/runs'); assert.equal(options.method, 'POST')
    return new Promise(resolve => { releasePost = () => resolve(json(response('SCHEDULED'))) })
  })
  let selection = initialRunSelection, connected = 0
  const submitter = createRunSubmission({ listScenarios: getAgentRunScenarios, postRun: createAgentRun,
    onCreated: value => { connected++; selection = runQueryReducer(selection, { type: 'created', runId: value.run.runId }) } })
  await assert.rejects(submitter.submit({ ...request, approval: null }))
  await assert.rejects(submitter.submit({ ...request, confirmed: false }))
  await assert.rejects(createAgentRun(scenarioId, '업무 요청'))
  assert.equal(calls.length, 0)
  const first = submitter.submit(request), duplicate = submitter.submit(request)
  assert.strictEqual(first, duplicate)
  while (!releasePost) await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(JSON.parse(calls[1].options.body), { scenarioId, requestText: '테스트용 업무 요청', configuration })
  releasePost(); const created = await first
  assert.equal(connected, 1); assert.equal(selection.runId, runId); assert.equal(selection.traceFromStart, true)
  assert.equal(selection.offset, 0); assert.equal(created.run.status, 'RECEIVED')
  assert.match(dispatchMessage(created.dispatchStatus), /실행 예약/)
  assert.match(dispatchMessage(created.dispatchStatus), /성공 판정은 아직/)
  await submitter.submit(request)
  assert.equal(calls.length, 2); assert.equal(connected, 1)
  selection = runQueryReducer(selection, { type: 'refresh' })
  selection = runQueryReducer(selection, { type: 'latest', runId, total: 600 })
  selection = runQueryReducer(selection, { type: 'page', offset: 0 })
  assert.equal(selection.traceFromStart, true)
})

test('신규 Run: 생성 응답 전에 저장된 첫 Trace부터 페이지·sequence 순서 및 실제 전달 대상 유지', async () => {
  const { steps, rows } = events(), feed = createRunEventFeed(), queue = createOfficeTraceController()
  const selection = runQueryReducer(initialRunSelection, { type: 'created', runId })
  const logs = [{ ...rows[0], eventId: id(25), eventType: 'RUN_STARTED', workflowStepId: null }, rows[0], rows[0], ...rows.slice(1),
    { ...rows[0], eventId: id(26), workflowStepId: null }]
  logs.push(...Array.from({ length: 1001 }, (_, n) => ({ ...rows[0], eventType: 'UNBOUND_EVENT', eventId: id(100 + n) })))
  const calls = [], trace = await feed.pull(selection.runId, page(logs, calls), undefined, { fromStart: selection.traceFromStart })
  assert.deepEqual(calls.map(c => c.offset), [0, 500, 1000])
  assert.equal(trace.entries[0].event.eventId, id(25)); assert.equal(trace.cursor, logs.length)
  assert.deepEqual(trace.entries.map(e => e.event.eventId), [...new Set(logs.map(e => e.eventId))])
  const snapshot = { runId, architecture: 'MULTI_AGENT', availability: 'ready', run: { status: 'VALIDATING' }, errors: {},
    queryStatus: { architecture: 'success', run: 'success', steps: 'success', trace: 'success' }, traceSteps: steps, trace }
  queue.observe(snapshot, 'actual')
  const delivered = [], seen = new Set(); let parallel = false
  for (let n = 0; n < 150 && delivered.length < 4; n++) {
    const current = queue.advance(.1).filter(a => a.kind === 'handoff')
    if (current.length === 2) { parallel = true; assert.deepEqual(current.map(a => a.to), [2, 3]) }
    for (const action of current) if (!seen.has(action.eventId)) { seen.add(action.eventId); delivered.push({ eventId: action.eventId, from: action.from, to: action.to }) }
  }
  assert.deepEqual(delivered, rows.map((e, to) => ({ eventId: e.eventId, from: 4, to })))
  assert.ok(parallel)
  const again = await feed.pull(runId, page(logs), undefined, { fromStart: true })
  assert.strictEqual(again.entries, trace.entries)
  const count = queue.info().pending; queue.observe({ ...snapshot, trace: again }, 'actual'); assert.equal(queue.info().pending, count)
  queue.observe({ ...snapshot, runId: id(9), trace: { ...trace, runId: id(9), entries: [] } }, 'actual')
  assert.equal(queue.info().pending, 0)
})

test('신규 첫 묶음의 중간 오류·취소는 offset 0에서 복구, 후속 오류는 확정 커서 유지', async () => {
  const { rows } = events(), logs = Array.from({ length: 503 }, (_, n) => ({ ...rows[0], eventId: id(100 + n) }))
  const feed = createRunEventFeed()
  await assert.rejects(feed.pull(runId, async (r, opts) => {
    if (opts.offset === 500) throw Error('mock failure')
    return page(logs)(r, opts)
  }, undefined, { fromStart: true }))
  const abort = new AbortController()
  await assert.rejects(feed.pull(runId, async (r, opts) => {
    const value = await page(logs)(r, opts); abort.abort(); return value
  }, abort.signal, { fromStart: true }), { name: 'AbortError' })
  const calls = [], recovered = await feed.pull(runId, page(logs, calls), undefined, { fromStart: true })
  assert.equal(calls[0].offset, 0); assert.equal(recovered.entries.length, 503)
  await assert.rejects(feed.pull(runId, async () => { throw Error('mock failure') }, undefined, { fromStart: true }))
  logs.push({ ...rows[1], eventId: id(900) })
  const afterCalls = [], after = await feed.pull(runId, page(logs, afterCalls), undefined, { fromStart: true })
  assert.equal(afterCalls[0].offset, 503); assert.equal(after.entries.length, 504)
  feed.reset()
  const returned = await feed.pull(runId, page(logs), undefined, { fromStart: true })
  assert.equal(returned.entries.length, 0)
})

test('기존 Run 수동 조회는 과거 이벤트를 생략하고 이후 이벤트만 처리한다', async () => {
  const { rows } = events(), feed = createRunEventFeed()
  const created = runQueryReducer(initialRunSelection, { type: 'created', runId })
  const manual = runQueryReducer(created, { type: 'select', runId })
  assert.equal(manual.traceFromStart, false)
  const logs = rows.slice(0, 2)
  const first = await feed.pull(manual.runId, page(logs), undefined, { fromStart: manual.traceFromStart })
  assert.deepEqual(first.entries, []); assert.equal(first.cursor, 2)
  logs.push(rows[2])
  const next = await feed.pull(runId, page(logs))
  assert.deepEqual(next.entries.map(e => e.event.eventId), [rows[2].eventId])
  let resolveOld
  const pending = feed.pull(runId, () => new Promise(resolve => { resolveOld = resolve }))
  const changed = await feed.pull(id(9), page(events(id(9)).rows))
  resolveOld({ runId, offset: 3, limit: 500, total: 3, events: [] })
  await assert.rejects(pending, { name: 'AbortError' }); assert.equal(changed.runId, id(9))
})

test('생성 거절·미등록 시나리오·민감 설정은 기존 선택 유지, 응답 유실은 재전송 차단', async t => {
  let selected = id(8), posts = 0, registered = true, mode = 'reject'
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    if (url === '/api/v1/scenarios') return json({ scenarios: registered ? [{ scenarioId }] : [] })
    assert.equal(url, '/api/v1/runs'); assert.equal(options.method, 'POST'); posts++
    if (mode === 'reject') return json({ detail: 'SYNTHETIC_PRIVATE_ERROR_DO_NOT_RENDER' }, 422)
    if (mode === 'lost') throw Error('SYNTHETIC_PRIVATE_NETWORK_ERROR')
    if (mode === 'invalid') return json({ run: { runId: 'invalid' } })
    return json(response('NOT_CONFIGURED'))
  })
  const make = () => createRunSubmission({ listScenarios: getAgentRunScenarios, postRun: createAgentRun,
    onCreated: value => { selected = value.run.runId } })
  for (const cfg of [{ ...configuration, apiKey: 'synthetic' }, { ...configuration, model: { ...configuration.model, apiKey: 'synthetic' } },
    { ...configuration, environment: { ...configuration.environment, hardwareProfile: 'token=synthetic' } },
    { ...configuration, limits: { ...configuration.limits, runtimeBudgetMs: null } }]) {
    assert.throws(() => publicRunConfiguration(cfg))
    await assert.rejects(make().submit({ ...request, approval: { ...approval, configuration: cfg } }))
  }
  assert.equal(posts, 0)
  registered = false; await assert.rejects(make().submit(request)); assert.equal(posts, 0)
  registered = true
  await assert.rejects(make().submit(request), error => !error.message.includes('SYNTHETIC_PRIVATE') && error.message.includes('거절'))
  assert.equal(selected, id(8))
  for (mode of ['lost', 'invalid']) {
    const runner = make(), before = posts
    await assert.rejects(runner.submit(request), /중복 생성 방지/)
    await assert.rejects(runner.submit(request)); assert.equal(posts, before + 1); assert.equal(selected, id(8))
  }
  mode = 'not-configured'
  const result = await make().submit(request)
  assert.equal(selected, runId); assert.equal(result.dispatchStatus, 'NOT_CONFIGURED')
  assert.match(dispatchMessage(result.dispatchStatus), /실행기가 연결되지/)
})
