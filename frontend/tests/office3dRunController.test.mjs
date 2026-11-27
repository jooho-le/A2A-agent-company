import assert from 'node:assert/strict'
import test from 'node:test'
import { buildAgentRunDisplayState } from '../src/components/atelier/agentRunDisplayState.js'
import { createOfficeRunController, officeAgentRoles } from '../src/components/atelier/office3dRunController.js'

const runId = 'c8d145e2-9695-43bc-9b58-062c5350bd8c'
function snapshot(states = {}, options = {}) {
  const id = options.runId ?? runId
  const steps = Object.entries(states).map(([agentRole, status]) => ({
    agentRole, status, runId: id, workflowStepId: `${id}-${agentRole}-${options.version ?? 1}`,
    attempt: options.attempt ?? 0, codeVersion: agentRole === 'PLANNER' ? null : options.version ?? 1,
  }))
  return buildAgentRunDisplayState({
    runId: id, run: { runId: id, status: options.status ?? 'VALIDATING', verdict: options.verdict ?? null,
      codeVersion: options.version ?? 1, fixAttempt: (options.version ?? 1) - 1 },
    steps, events: [], errors: {}, status: { run: 'success', steps: steps.length ? 'success' : 'empty', events: 'empty' },
  })
}
const role = (rows, name) => rows.find(row => row.agentRole === name)

test('같은 작업이 3초 유지된 뒤에만 작업 표시·모션을 함께 시작한다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  const before = role(controller.advance(2.999), 'QA')
  assert.equal(before.displayState, 'pending')
  assert.equal(before.poseState, 'waiting')
  assert.equal(before.step.status, 'RUNNING')
  const after = role(controller.advance(.002), 'QA')
  assert.equal(after.displayState, 'working')
  assert.equal(after.poseState, 'working')
})

test('3초 미만 완료·실패·입력 대기는 작업 모션 없이 즉시 표시한다', () => {
  for (const [status, display] of [['SUCCEEDED', 'succeeded'], ['FAILED', 'failed'], ['WAITING_INPUT', 'waiting_input']]) {
    const controller = controllerWith({ DEVELOPER: 'RUNNING' })
    controller.advance(2.9)
    controller.setSnapshot(snapshot({ DEVELOPER: status }))
    const row = role(controller.advance(.2), 'DEVELOPER')
    assert.equal(row.displayState, display)
    assert.equal(row.poseState, 'waiting')
  }
})

test('병렬 직원별 3초를 따로 계산하고 정기 조회는 시간을 초기화하지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  controller.advance(2)
  controller.setSnapshot(snapshot({ QA: 'RUNNING', SECURITY: 'RUNNING' }))
  let rows = controller.advance(1)
  assert.equal(role(rows, 'QA').displayState, 'working')
  assert.equal(role(rows, 'SECURITY').displayState, 'pending')
  controller.setSnapshot({ runId, queryStatus: { run: 'loading', steps: 'loading' } })
  rows = controller.advance(2)
  assert.equal(role(rows, 'SECURITY').displayState, 'working')
  controller.setSnapshot(snapshot({ QA: 'RUNNING', SECURITY: 'RUNNING' }, { runId: 'new' }))
  assert.ok(controller.advance(.1).filter(row => row.step).every(row => row.displayState === 'pending'))
})

function controllerWith(states) {
  const controller = createOfficeRunController()
  controller.setSnapshot(snapshot(states))
  return controller
}

test('RECEIVED + Planner PENDING은 네 캐릭터의 기본 대기 자세다', () => {
  const controller = createOfficeRunController()
  controller.setSnapshot(snapshot({ PLANNER: 'PENDING' }, { status: 'RECEIVED' }))
  const rows = controller.advance(200)
  assert.deepEqual(rows.map(row => row.agentRole), officeAgentRoles)
  assert.ok(rows.every(row => row.poseState === 'waiting' && row.poseTime === 0 && row.expression === 'normal'))
})

for (const name of officeAgentRoles) {
  test(`${name} RUNNING이면 해당 캐릭터만 작업한다`, () => {
    const controller = controllerWith({ [name]: 'RUNNING' })
    const rows = controller.advance(3)
    assert.equal(role(rows, name).motionTime, 3)
    assert.equal(role(rows, name).poseState, 'working')
    assert.ok(rows.filter(row => row.agentRole !== name).every(row => row.motionTime === 0 && row.poseState === 'waiting'))
  })
}

test('QA·Security는 서로 다른 시점에 시작해도 독립적인 시간으로 함께 작업한다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  controller.advance(4)
  controller.setSnapshot(snapshot({ QA: 'RUNNING', SECURITY: 'RUNNING' }))
  const rows = controller.advance(2)
  assert.equal(role(rows, 'QA').motionTime, 6)
  assert.equal(role(rows, 'SECURITY').motionTime, 2)
})

test('같은 Run·Step 재조회는 모션 시간을 초기화하지 않는다', () => {
  const controller = controllerWith({ DEVELOPER: 'RUNNING' })
  controller.advance(5)
  controller.setSnapshot(snapshot({ DEVELOPER: 'RUNNING' }))
  assert.equal(role(controller.advance(1), 'DEVELOPER').motionTime, 6)
})

test('같은 Run의 갱신 중에는 마지막 상태와 모션을 유지한다', () => {
  const controller = controllerWith({ DEVELOPER: 'RUNNING' })
  controller.advance(5)
  controller.setSnapshot({ runId, queryStatus: { run: 'loading', steps: 'loading' } })
  const row = role(controller.advance(2), 'DEVELOPER')
  assert.equal(row.displayState, 'working')
  assert.equal(row.motionTime, 7)
  assert.equal(row.refreshing, true)
  controller.setSnapshot(snapshot({ DEVELOPER: 'RUNNING' }))
  assert.equal(role(controller.advance(1), 'DEVELOPER').motionTime, 8)
})

test('조회 실패는 실패 표정이 아닌 확인 불가·마지막 자세 정지로 처리한다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  controller.advance(4)
  controller.setSnapshot({ runId, queryStatus: { run: 'success', steps: 'error' }, agents: [] })
  const row = role(controller.advance(5), 'QA')
  assert.equal(row.displayState, 'unknown')
  assert.equal(row.expression, 'normal')
  assert.equal(row.motionTime, 4)
  assert.equal(row.poseState, 'working')
  controller.setSnapshot({ runId, queryStatus: { run: 'loading', steps: 'loading' } })
  assert.equal(role(controller.advance(5), 'QA').motionTime, 4)
  controller.setSnapshot(snapshot({ QA: 'RUNNING' }))
  const recovered = role(controller.advance(1), 'QA')
  assert.equal(recovered.motionTime, 1)
  assert.equal(recovered.poseState, 'waiting')
})

test('일부 GET이 조회 중이어도 다른 GET 실패를 감추지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  controller.setSnapshot({ runId, queryStatus: { run: 'loading', steps: 'error' }, agents: [] })
  assert.ok(controller.advance(1).every(row => row.displayState === 'unknown' && !row.refreshing))
})

test('오류 스냅샷에 과거 작업 데이터가 남아 있어도 움직임을 재개하지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  const stale = snapshot({ QA: 'RUNNING' })
  stale.queryStatus.steps = 'error'
  controller.setSnapshot(stale)
  assert.ok(controller.advance(1).every(row => row.displayState === 'unknown' && row.expression === 'normal'))
})

test('FAILED 후 새 Step 재작업은 오류 표정을 해제하고 모션을 다시 시작한다', () => {
  const controller = controllerWith({ DEVELOPER: 'FAILED' })
  assert.equal(role(controller.advance(10), 'DEVELOPER').expression, 'worried')
  controller.setSnapshot(snapshot({ DEVELOPER: 'RUNNING' }, { version: 2 }))
  const row = role(controller.advance(1), 'DEVELOPER')
  assert.equal(row.expression, 'normal')
  assert.equal(row.motionTime, 1)
  assert.equal(row.poseState, 'waiting')
  assert.equal(role(controller.advance(2), 'DEVELOPER').poseState, 'working')
})

test('동일 Step의 attempt 변경 시 해당 Agent의 모션만 새로 시작한다', () => {
  const controller = controllerWith({ QA: 'RUNNING', SECURITY: 'RUNNING' })
  controller.advance(5)
  const data = snapshot({ QA: 'RUNNING', SECURITY: 'RUNNING' })
  data.agents.find(agent => agent.agentRole === 'QA').step.attempt = 1
  controller.setSnapshot(data)
  const rows = controller.advance(1)
  assert.equal(role(rows, 'QA').motionTime, 1)
  assert.equal(role(rows, 'SECURITY').motionTime, 6)
})

for (const state of ['WAITING_INPUT', 'CANCELED', 'SUCCEEDED']) {
  test(`${state}는 서로 다른 표시를 보존하며 작업을 멈추고 안정된 자세를 취한다`, () => {
    const controller = controllerWith({ QA: 'RUNNING' })
    controller.advance(5)
    controller.setSnapshot(snapshot({ QA: state }))
    const row = role(controller.advance(10), 'QA')
    assert.equal(row.step.status, state)
    assert.equal(row.poseState, 'waiting')
    assert.equal(row.poseTime, 0)
    assert.equal(row.expression, 'normal')
  })
}

test('새 Run 조회 중에는 이전 Run의 동작·오류 표정이 남지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING', SECURITY: 'FAILED' })
  controller.advance(5)
  controller.setSnapshot({ runId: 'other-run', queryStatus: { run: 'loading', steps: 'loading' } })
  assert.ok(controller.advance(10).every(row => row.displayState === 'loading' && row.poseState === 'waiting' && row.motionTime === 0 && row.expression === 'normal'))
})

test('모션 끄기와 음수·비정상 시간 입력 시 시계가 움직이지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING' })
  controller.advance(5)
  for (const delta of [-1, NaN, Infinity]) assert.equal(role(controller.advance(delta), 'QA').motionTime, 5)
  assert.equal(role(controller.advance(10, false), 'QA').motionTime, 5)
})

test('105초 이후에도 RUNNING은 유지되며 자동 완료·세리머니를 만들지 않는다', () => {
  const controller = controllerWith({ QA: 'RUNNING', SECURITY: 'RUNNING' })
  const rows = controller.advance(1000)
  assert.equal(role(rows, 'QA').displayState, 'working')
  assert.equal(role(rows, 'SECURITY').displayState, 'working')
  controller.setSnapshot(snapshot({ QA: 'SUCCEEDED', SECURITY: 'SUCCEEDED' }, { status: 'FINISHED', verdict: 'SUCCESS' }))
  assert.ok(controller.advance(1000).every(row => !Object.hasOwn(row, 'finale') && !Object.hasOwn(row, 'progress')))
})
