import assert from 'node:assert/strict'
import test from 'node:test'
import { confirmedHandoffs, createOfficeHandoffController } from '../src/components/atelier/officeHandoffEvents.js'

const runId = 'run-a'
const steps = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY'].map((agentRole, index) => ({ workflowStepId: `step-${index}`, runId, agentRole }))
const event = (id, index, type = 'A2A_MESSAGE_SENT', extra = {}) => ({ eventId: id, runId, workflowStepId: steps[index].workflowStepId,
  eventType: type, actor: 'ORCHESTRATOR', attempt: 0, occurredAt: '2026-11-09T01:00:00Z', ...extra })
const snapshot = (events, extra = {}) => ({ runId, architecture: 'MULTI_AGENT', run: { status: 'VALIDATING' },
  queryStatus: { run: 'success', architecture: 'success', events: 'success', steps: 'success' },
  pagination: { offset: 0, limit: 20 }, handoffs: confirmedHandoffs(events, steps, runId), ...extra })

test('실제 A2A 호출은 관리 서버에서 해당 Step 역할로만 전달한다', () => {
  assert.deepEqual(confirmedHandoffs([event('a', 1)], steps, runId).map(({ from, to }) => [from, to]), [[4, 1]])
})
test('QA·Security 병렬 호출은 두 대상 모두 보존한다', () => {
  assert.deepEqual(confirmedHandoffs([event('q', 2), event('s', 3)], steps, runId).map(item => item.to), [2, 3])
})
test('완료·실패 응답은 해당 역할에서 관리 서버로 반환한다', () => {
  for (const a2aTaskState of ['TASK_STATE_COMPLETED', 'TASK_STATE_FAILED']) {
    const rows = confirmedHandoffs([event('a', 2, 'A2A_TASK_RECEIVED', { a2aTaskState })], steps, runId)
    assert.equal(rows[0].from, 2); assert.equal(rows[0].to, 4); assert.equal(rows[0].label, '상태 응답')
  }
})
test('Step SUCCEEDED나 Run 성공만으로 다음 전달을 만들지 않는다', () => {
  assert.deepEqual(confirmedHandoffs([event('a', 0, 'RUN_FINISHED'), event('b', 0, 'STEP_SUCCEEDED')], steps, runId), [])
})
test('알 수 없는 역할·다른 Run·없는 Step·중복 Step·잘못된 시각은 추측하지 않는다', () => {
  for (const data of [[], [...steps, steps[0]], [{ ...steps[0], agentRole: 'UNKNOWN' }]]) {
    assert.deepEqual(confirmedHandoffs([event('a', 0)], data, runId), [])
  }
  for (const extra of [{ runId: 'other' }, { occurredAt: 'bad' }, { actor: 'UNKNOWN' }, { attempt: -1 }]) {
    assert.deepEqual(confirmedHandoffs([event('a', 0, 'A2A_MESSAGE_SENT', extra)], steps, runId), [])
  }
})
test('처음 조회한 과거 이벤트는 재생하지 않는다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([event('a', 0)]), 'actual')
  assert.deepEqual(c.advance(.1), [])
})
test('새 QA·Security Event는 독립적으로 동시에 재생하며 중복 조회로 반복하지 않는다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  const input = snapshot([event('q', 2), event('s', 3)])
  c.observe(input, 'actual'); assert.deepEqual(c.advance(.1).map(item => item.to), [2, 3])
  c.advance(2); c.observe(input, 'actual'); assert.deepEqual(c.advance(.1), [])
})
test('서버 발생 시각 차이를 유지해 순서대로 연출한다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  c.observe(snapshot([event('a', 0), event('b', 1, 'A2A_MESSAGE_SENT', { occurredAt: '2026-11-09T01:00:01Z' })]), 'actual')
  assert.deepEqual(c.advance(.2).map(item => item.to), [0])
  assert.deepEqual(c.advance(1).map(item => item.to), [0, 1])
})
test('시각이 역전되어도 API의 저장 순서를 유지하고 중복 Event를 제거한다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  c.observe(snapshot([event('a', 0, 'A2A_MESSAGE_SENT', { occurredAt: '2026-11-09T01:00:05Z' }), event('b', 1), event('b', 1)]), 'actual')
  assert.deepEqual(c.advance(.1).map(item => item.eventId), ['a', 'b'])
})
test('실패 보고 후 Developer 재작업 요청도 역할 고정 순서 없이 반영한다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  c.observe(snapshot([event('failed', 2, 'A2A_TASK_STATE_CHANGED', { a2aTaskState: 'TASK_STATE_FAILED' }),
    event('fix', 1, 'A2A_MESSAGE_SENT', { attempt: 1, occurredAt: '2026-11-09T01:00:01Z' })]), 'actual')
  assert.deepEqual(c.advance(1.1).map(item => [item.from, item.to]), [[2, 4], [4, 1]])
})
test('Event 페이지 이동·Run 변경·목업 복귀는 과거 전달을 재생하지 않는다', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  c.observe(snapshot([event('a', 0)], { pagination: { offset: 20, limit: 20 } }), 'actual')
  assert.deepEqual(c.advance(.1), [])
  c.observe(snapshot([event('a', 0)], { runId: 'run-b' }), 'actual'); assert.deepEqual(c.advance(.1), [])
  c.observe(snapshot([event('a', 0)]), 'mock'); assert.deepEqual(c.advance(.1), [])
})
test('조회 오류 시 전달 중단, 모션 끄기 시 이동 시간 고정', () => {
  const c = createOfficeHandoffController(); c.observe(snapshot([]), 'actual')
  c.observe(snapshot([event('a', 0)]), 'actual')
  assert.equal(c.advance(.5, false)[0].progress, 0)
  c.observe(snapshot([], { queryStatus: { events: 'error', steps: 'success' } }), 'actual')
  assert.deepEqual(c.advance(.1), [])
})
