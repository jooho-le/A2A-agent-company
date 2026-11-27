import assert from 'node:assert/strict'
import test from 'node:test'
import { createOfficeTraceController, traceAction, isTraceActionCurrent } from '../src/components/atelier/officeTraceEvents.js'
import { createRunSuccessCeremony } from '../src/components/atelier/runSuccessCeremony.js'

const runId = '00000000-0000-4000-8000-000000000001'
const steps = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY'].map((agentRole, i) => ({ runId, workflowStepId: `step-${i}`, agentRole,
  attempt: 0, codeVersion: i === 0 ? null : 1, status: 'RUNNING', inputArtifactIds: i >= 2 ? ['source-1'] : [] }))
const make = (id, eventType, role = null, extra = {}) => ({ eventId: id, runId, eventType, occurredAt: '2026-11-09T03:00:00Z',
  actor: 'ORCHESTRATOR', attempt: 0, workflowStepId: role === null ? null : steps[role].workflowStepId,
  codeVersion: role === null ? null : steps[role].codeVersion, ...extra })
function snapshot(events, extra = {}) {
  return { source: 'orchestrator', runId, architecture: 'MULTI_AGENT', availability: 'ready',
    run: { status: 'VALIDATING', verdict: null }, traceSteps: steps,
    agents: steps.map(step => ({ step, displayState: 'working' })),
    queryStatus: { run: 'success', steps: 'success', events: 'success', architecture: 'success', trace: 'success' }, errors: {},
    trace: { runId, initialized: true, epoch: 1, entries: events.map((event, offset) => ({ event, offset })) }, ...extra }
}
function drain(controller) {
  const played = new Map()
  for (let n = 0; n < 10000 && controller.busy(); n++) {
    for (const item of controller.advance(.05)) if (!played.has(item.eventId)) played.set(item.eventId, item)
  }
  assert.equal(controller.busy(), false)
  return [...played.values()]
}

test('리더 접수 → Planner 요청·수신·산출물 → Developer 호출은 기록 순서를 따른다', () => {
  const events = [make('start', 'RUN_STARTED'), make('plan', 'A2A_MESSAGE_SENT', 0), make('received', 'A2A_TASK_RECEIVED', 0),
    make('artifact', 'ARTIFACT_REGISTERED', 0, { actor: 'PLANNER', outputArtifactIds: ['plan-1'] }), make('dev', 'A2A_MESSAGE_SENT', 1)]
  const controller = createOfficeTraceController(); controller.observe(snapshot(events), 'actual')
  const actions = drain(controller)
  assert.deepEqual(actions.map(item => item.eventId), events.map(item => item.eventId))
  assert.deepEqual(actions.filter(item => item.kind === 'handoff').map(item => [item.from, item.to]), [[4, 0], [4, 1]])
  assert.equal(actions[3].kind, 'artifact'); assert.equal(actions[3].to, null)
})
test('공통 Snapshot의 실제 QA·Security 호출은 각각 독립 문서로 함께 전달한다', () => {
  const controller = createOfficeTraceController()
  controller.observe(snapshot([make('qa', 'A2A_MESSAGE_SENT', 2), make('sec', 'A2A_MESSAGE_SENT', 3)]), 'actual')
  const active = controller.advance(.1)
  assert.deepEqual(active.map(item => item.to), [2, 3]); assert.equal(active[0].startsAt, active[1].startsAt)
})
test('보안 결함 → 개발 수정 → 새 Snapshot → 병렬 재검증도 고정 역할 순서 없이 재생한다', () => {
  const updated = [...steps, ...['DEVELOPER', 'QA', 'SECURITY'].map((agentRole, i) => ({ ...steps[i + 1], workflowStepId: `v2-${i}`,
    agentRole, codeVersion: 2, inputArtifactIds: ['source-2'] }))]
  const events = [make('issue', 'ISSUE_DETECTED', 3, { actor: 'SECURITY', issueId: 'issue-1' }),
    make('fix', 'FIX_REQUESTED', null, { workflowStepId: 'v2-0', codeVersion: 2 }),
    make('fix-call', 'A2A_MESSAGE_SENT', null, { workflowStepId: 'v2-0', codeVersion: 2 }),
    make('snapshot', 'SNAPSHOT_FROZEN', null, { workflowStepId: 'v2-0', codeVersion: 2, outputArtifactIds: ['source-2'] }),
    make('revalidate', 'REVALIDATION_STARTED', null, { codeVersion: 2, inputArtifactIds: ['source-2'] }),
    make('qa2', 'A2A_MESSAGE_SENT', null, { workflowStepId: 'v2-1', codeVersion: 2 }),
    make('sec2', 'A2A_MESSAGE_SENT', null, { workflowStepId: 'v2-2', codeVersion: 2 })]
  const controller = createOfficeTraceController(); controller.observe(snapshot(events, { traceSteps: updated }), 'actual')
  const actions = drain(controller)
  assert.deepEqual(actions.map(item => item.kind), ['issue', 'manage', 'handoff', 'artifact', 'manage', 'handoff', 'handoff'])
  assert.deepEqual(actions[4].relatedAgents, [2, 3])
  assert.equal(actions[5].startsAt, actions[6].startsAt)
})
test('역할·대상을 확인할 수 없는 이벤트는 문서를 만들지 않고 상태 기록만 남긴다', () => {
  for (const event of [make('no-step', 'A2A_MESSAGE_SENT'), make('config', 'ARTIFACT_REGISTERED', null, { outputArtifactIds: ['configuration'] }),
    make('unknown', 'NOT_IMPLEMENTED'), make('bad-version', 'A2A_MESSAGE_SENT', 1, { codeVersion: 3 }), make('bad-time', 'RUN_STARTED', null, { occurredAt: 'bad' })]) {
    const action = traceAction(event, steps, runId)
    assert.equal(action.kind, 'log'); assert.equal(action.from, null); assert.equal(action.to, null)
  }
  const controller = createOfficeTraceController(); controller.observe(snapshot([make('bad', 'ANY', null, { occurredAt: 'bad' })]), 'actual')
  assert.equal(drain(controller).length, 1)
})
test('동일 ID·일괄 수신·후속 묶음이 중복되거나 빠지지 않는다', () => {
  const controller = createOfficeTraceController()
  const a = make('a', 'A2A_MESSAGE_SENT', 0), b = make('b', 'A2A_TASK_RECEIVED', 0), c = make('c', 'A2A_MESSAGE_SENT', 1)
  controller.observe(snapshot([a, a, b]), 'actual'); controller.advance(.05)
  controller.observe(snapshot([a, a, b, c]), 'actual')
  assert.deepEqual(drain(controller).map(item => item.eventId), ['a', 'b', 'c'])
  controller.observe(snapshot([a, b, c]), 'actual'); assert.equal(controller.busy(), false)
})
test('화면 Trace 페이지 이동은 재생 커서를 바꾸지 않으며 Run·모드 변경은 큐를 비운다', () => {
  const controller = createOfficeTraceController(), input = snapshot([make('a', 'A2A_MESSAGE_SENT', 0)])
  controller.observe(input, 'actual'); drain(controller)
  controller.observe({ ...input, pagination: { offset: 100 } }, 'actual'); assert.equal(controller.busy(), false)
  controller.observe(snapshot([], { runId: 'other', trace: { runId: 'other', epoch: 2, initialized: true, entries: [] } }), 'actual')
  assert.equal(controller.info().completed, 0)
  controller.observe(input, 'mock'); assert.equal(controller.busy(), false)
})
test('모션 끄기와 통신 실패는 큐를 버리지 않고 멈추며 복구 후 이어 재생한다', () => {
  const controller = createOfficeTraceController(), input = snapshot([make('a', 'A2A_MESSAGE_SENT', 0)])
  controller.observe(input, 'actual'); const before = controller.advance(.1)[0].progress
  assert.equal(controller.advance(.1, false)[0].progress, before)
  controller.observe({ ...input, errors: { trace: new Error('network') } }, 'actual')
  assert.equal(controller.advance(.1)[0].progress, before)
  controller.observe(input, 'actual'); assert.equal(drain(controller).length, 1)
})
test('과거 Event가 새 코드 버전·재시도의 현재 Step 자세를 덮어쓰지 않는다', () => {
  const event = make('old', 'A2A_TASK_STATE_CHANGED', 3, { a2aTaskState: 'TASK_STATE_FAILED' })
  const action = traceAction(event, steps, runId)
  assert.equal(isTraceActionCurrent(action, { step: { ...steps[3], codeVersion: 2 } }), false)
  assert.equal(isTraceActionCurrent(action, { step: { ...steps[3], attempt: 1 } }), false)
  assert.equal(isTraceActionCurrent(action, { step: steps[3] }), true)
})
test('수신·작업·대기·완료 이벤트는 최신 Step을 수정하지 않으며 다른 actor는 추측하지 않는다', () => {
  const original = structuredClone(steps)
  for (const a2aTaskState of ['TASK_STATE_WORKING', 'TASK_STATE_INPUT_REQUIRED', 'TASK_STATE_COMPLETED', 'TASK_STATE_FAILED']) {
    const action = traceAction(make('task', 'A2A_TASK_STATE_CHANGED', 2, { a2aTaskState }), steps, runId)
    assert.equal(action.agent, 2)
    assert.equal(action.kind, 'state')
    assert.equal(action.from, null); assert.equal(action.to, null)
  }
  assert.equal(traceAction(make('wrong-actor', 'A2A_MESSAGE_SENT', 2, { actor: 'DEVELOPER' }), steps, runId).kind, 'log')
  assert.deepEqual(steps, original)
})
test('처음 조회한 완료 Run은 수집된 과거 항목이 없는 기준점에서 아무것도 재생하지 않는다', () => {
  const controller = createOfficeTraceController()
  controller.observe(snapshot([], { run: { status: 'FINISHED', verdict: 'SUCCESS' } }), 'actual')
  assert.deepEqual(controller.advance(.1), [])
  assert.equal(controller.info().completed, 0)
})
test('VERDICT_CREATED 자체나 FINISHED+FAIL은 성공 세리머니를 만들지 않는다', () => {
  const values = new Map(), gate = createRunSuccessCeremony({ getStorage: () => ({ getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value) }) })
  const controller = createOfficeTraceController()
  gate.observe(snapshot([]), 'actual')
  for (const verdict of [null, 'FAIL', 'UNVERIFIED', 'HUMAN_REVIEW']) {
    gate.observe(snapshot([], { run: { status: 'FINISHED', verdict } }), 'actual')
    assert.equal(gate.start({ ready: true, motionEnabled: true }), false)
  }
  gate.observe(snapshot([]), 'actual')
  const success = snapshot([make('verdict', 'VERDICT_CREATED')], { run: { status: 'FINISHED', verdict: 'SUCCESS' } })
  gate.observe(success, 'actual'); controller.observe(success, 'actual')
  assert.equal(controller.busy(), true)
  drain(controller)
  assert.equal(!controller.busy() && gate.start({ ready: true, motionEnabled: true }), true)
  assert.equal(gate.info().playback.pose, 'team')
  gate.observe(success, 'actual'); assert.equal(gate.start({ ready: true, motionEnabled: true }), false)
})
