import test from 'node:test'
import assert from 'node:assert/strict'
import { createOfficeLabelPresentation } from '../src/components/atelier/officeLabelPresentation.js'

const snapshot = { runId: 'run', queryStatus: { run: 'success', steps: 'success' } }
const row = (state, role = 'QA') => ({ agentRole: role, displayState: state, label: state,
  step: { workflowStepId: role, attempt: 0, codeVersion: 1 } })

test('명찰은 문구와 색상을 함께 3초 뒤 반영하고 짧은 중간 상태를 생략한다', () => {
  const labels = createOfficeLabelPresentation(), working = row('working')
  assert.equal(labels.advance(snapshot, [working], 2.9)[0].displayState, 'idle')
  assert.equal(labels.advance(snapshot, [working], .11)[0].displayState, 'working')
  assert.equal(labels.advance(snapshot, [row('succeeded')], 1)[0].displayState, 'working')
  assert.equal(labels.advance(snapshot, [row('pending')], 2)[0].displayState, 'working')
  assert.equal(labels.advance(snapshot, [row('succeeded')], 2.9)[0].displayState, 'working')
  assert.deepEqual(labels.advance(snapshot, [row('succeeded')], .11)[0], { displayState: 'succeeded', label: 'succeeded' })
  assert.equal(working.displayState, 'working', '원본 상태를 변경하지 않는다')
})

test('반복 조회·병렬 직원·새 Run의 명찰 상태는 서로 섞이지 않는다', () => {
  const labels = createOfficeLabelPresentation()
  const rows = [row('working'), row('pending', 'SECURITY')]
  labels.advance(snapshot, rows, 2)
  labels.advance({ ...snapshot, queryStatus: { run: 'loading', steps: 'loading' } }, rows.map(item => ({ ...item, displayState: 'loading' })), 1)
  const next = [row('working'), row('working', 'SECURITY')]
  let shown = labels.advance(snapshot, next, 1)
  assert.equal(shown[0].displayState, 'working')
  assert.equal(shown[1].displayState, 'idle')
  shown = labels.advance(snapshot, next, 2)
  assert.ok(shown.every(item => item.displayState === 'working'))
  assert.ok(labels.advance({ ...snapshot, runId: 'other' }, next, 0).every(item => item.displayState === 'idle'))
})

test('실패·입력 요청·조회 오류는 즉시 보이고 오류 뒤 갱신으로 덮이지 않는다', () => {
  const labels = createOfficeLabelPresentation()
  labels.advance(snapshot, [row('working')], 3)
  for (const state of ['failed', 'waiting_input', 'canceled']) {
    assert.equal(labels.advance(snapshot, [row(state)], 0)[0].displayState, state)
  }
  const failed = { ...snapshot, queryStatus: { run: 'loading', steps: 'error' } }
  assert.equal(labels.advance(failed, [row('loading')], 0)[0].displayState, 'unknown')
  assert.equal(labels.advance({ ...snapshot, queryStatus: { run: 'loading', steps: 'loading' } }, [row('loading')], 4)[0].displayState, 'unknown')
})
