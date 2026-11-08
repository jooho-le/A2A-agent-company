import assert from 'node:assert/strict'
import test from 'node:test'
import { buildAgentRunDisplayState } from '../src/components/atelier/agentRunDisplayState.js'

const runId = 'c8d145e2-9695-43bc-9b58-062c5350bd8c'
const otherRunId = '22222222-2222-4222-8222-222222222222'
const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
function step(agentRole, status, overrides = {}) {
  return {
    workflowStepId: `50000000-0000-4000-8000-${String(roles.indexOf(agentRole) + 1).padStart(12, '0')}`,
    runId, agentRole, status, attempt: 0, codeVersion: agentRole === 'PLANNER' ? null : 1,
    a2aTaskState: null, createdAt: '2026-11-08T00:00:00Z', updatedAt: '2026-11-08T00:00:01Z',
    ...overrides,
  }
}
function monitor(steps = [], runOverrides = {}) {
  return {
    runId,
    run: { runId, status: 'RECEIVED', verdict: null, codeVersion: null, fixAttempt: 0, ...runOverrides },
    steps, events: [], status: { run: 'success', steps: steps.length ? 'success' : 'empty', events: 'empty' },
    errors: { run: null, steps: null, events: null }, pagination: { total: 0, limit: 20, offset: 0 },
  }
}
const find = (result, role) => result.agents.find(agent => agent.agentRole === role)

test('RECEIVED + Planner PENDING은 완료가 아니며 다른 세 명은 Step 미생성 대기다', () => {
  const result = buildAgentRunDisplayState(monitor([step('PLANNER', 'PENDING')]))
  assert.equal(result.run.status, 'RECEIVED')
  assert.equal(result.run.isFinalSuccess, false)
  assert.equal(find(result, 'PLANNER').displayState, 'pending')
  assert.deepEqual(result.agents.slice(1).map(agent => [agent.displayState, agent.reason, agent.step]),
    Array.from({ length: 3 }, () => ['idle', 'step_not_created', null]))
})

for (const [status, displayState, label] of [
  ['PENDING', 'pending', '대기'], ['RUNNING', 'working', '작업'],
  ['SUCCEEDED', 'succeeded', '단계 완료'], ['FAILED', 'failed', '단계 실패'],
  ['WAITING_INPUT', 'waiting_input', '입력 대기'], ['CANCELED', 'canceled', '취소'],
]) {
  test(`실제 Step 상태 ${status}를 독립적인 표시 상태로 변환한다`, () => {
    const source = step('PLANNER', status)
    const agent = find(buildAgentRunDisplayState(monitor([source])), 'PLANNER')
    assert.equal(agent.displayState, displayState)
    assert.equal(agent.label, label)
    assert.deepEqual(agent.step, source)
    assert.notEqual(agent.step, source)
  })
}

test('Developer 작업과 QA·Security 동시 작업을 각각 유지한다', () => {
  const developer = buildAgentRunDisplayState(monitor([step('DEVELOPER', 'RUNNING')], { status: 'IMPLEMENTING' }))
  assert.equal(find(developer, 'DEVELOPER').displayState, 'working')
  const concurrent = buildAgentRunDisplayState(monitor([
    step('QA', 'RUNNING'), step('SECURITY', 'RUNNING'),
  ], { status: 'VALIDATING', codeVersion: 1 }))
  assert.deepEqual(concurrent.agents.filter(agent => agent.displayState === 'working').map(agent => agent.agentRole), ['QA', 'SECURITY'])
})

test('실패한 이전 버전보다 현재 수정 버전 Step을 선택하며 attempt를 전역 순번으로 쓰지 않는다', () => {
  const oldStep = step('DEVELOPER', 'FAILED', { attempt: 12 })
  const retry = step('DEVELOPER', 'RUNNING', {
    workflowStepId: '50000000-0000-4000-8000-000000000099', codeVersion: 2,
  })
  for (const steps of [[oldStep, retry], [retry, oldStep]]) {
    const result = buildAgentRunDisplayState(monitor(steps, { status: 'FIXING', codeVersion: 1, fixAttempt: 1 }))
    assert.equal(find(result, 'DEVELOPER').step.workflowStepId, retry.workflowStepId)
    assert.equal(find(result, 'DEVELOPER').step.attempt, 0)
    assert.equal(find(result, 'DEVELOPER').displayState, 'working')
  }
})

test('같은 Step의 재개된 attempt와 원본 메타데이터를 보존한다', () => {
  const resumed = step('PLANNER', 'RUNNING', { attempt: 3, a2aTaskState: 'TASK_STATE_WORKING' })
  assert.deepEqual(find(buildAgentRunDisplayState(monitor([resumed])), 'PLANNER').step, resumed)
})

for (const [runStatus, stepStatus] of [['WAITING_INPUT', 'WAITING_INPUT'], ['ABORTED', 'CANCELED'], ['FINISHED', 'FAILED']]) {
  test(`${runStatus}에서도 마지막 Developer 수정 시도를 이전 버전으로 되돌리지 않는다`, () => {
    const current = step('DEVELOPER', stepStatus, { workflowStepId: 'developer-v2', codeVersion: 2 })
    const result = buildAgentRunDisplayState(monitor([
      step('DEVELOPER', 'SUCCEEDED'), current,
    ], { status: runStatus, verdict: runStatus === 'FINISHED' ? 'FAIL' : null, codeVersion: 1, fixAttempt: 1 }))
    assert.deepEqual(find(result, 'DEVELOPER').step, current)
  })
}

test('QA·Security는 Run의 현재 검증 버전을 각각 선택한다', () => {
  const result = buildAgentRunDisplayState(monitor([
    step('QA', 'FAILED'), step('QA', 'RUNNING', { workflowStepId: 'qa-v2', codeVersion: 2 }),
    step('SECURITY', 'SUCCEEDED'), step('SECURITY', 'RUNNING', { workflowStepId: 'security-v2', codeVersion: 2 }),
  ], { status: 'REVALIDATING', codeVersion: 2, fixAttempt: 1 }))
  for (const role of ['QA', 'SECURITY']) {
    assert.equal(find(result, role).displayState, 'working')
    assert.equal(find(result, role).step.codeVersion, 2)
  }
})

test('현재 버전 Step 미생성 시 과거 완료를 현재 완료로 표시하지 않는다', () => {
  const result = buildAgentRunDisplayState(monitor([step('QA', 'SUCCEEDED')], { status: 'REVALIDATING', codeVersion: 2 }))
  assert.equal(find(result, 'QA').displayState, 'idle')
  assert.equal(find(result, 'QA').reason, 'step_not_created_for_version')
  assert.equal(find(result, 'QA').step, null)
})

test('동일 역할·버전의 여러 ID는 attempt·배열·시각·RUNNING 우선순위로 추측하지 않는다', () => {
  const older = step('QA', 'FAILED', { attempt: 9 })
  const active = step('QA', 'RUNNING', { workflowStepId: 'different-id', createdAt: '2026-11-08T00:03:00Z' })
  for (const steps of [[older, active], [active, older]]) {
    const result = buildAgentRunDisplayState(monitor(steps, { status: 'VALIDATING', codeVersion: 1 }))
    assert.equal(result.availability, 'partial')
    assert.equal(find(result, 'QA').displayState, 'unknown')
    assert.equal(find(result, 'QA').reason, 'ambiguous_steps')
    assert.equal(find(result, 'QA').step, null)
  }
})

test('Planner 복수 Step 및 기준 버전 없는 복수 Step을 임의 선택하지 않는다', () => {
  for (const agentRole of ['PLANNER', 'QA']) {
    const result = buildAgentRunDisplayState(monitor([
      step(agentRole, 'FAILED'), step(agentRole, 'RUNNING', { workflowStepId: 'new-id', codeVersion: 2 }),
    ]))
    assert.equal(find(result, agentRole).reason, 'ambiguous_steps')
  }
})

test('별도 GET의 조회 시점이 엇갈리면 과거 완료를 현재 상태로 확정하지 않는다', () => {
  const result = buildAgentRunDisplayState(monitor([
    step('QA', 'SUCCEEDED'), step('QA', 'RUNNING', { workflowStepId: 'qa-v2', codeVersion: 2 }),
  ], { status: 'VALIDATING', codeVersion: 1 }))
  assert.equal(find(result, 'QA').reason, 'step_version_ahead_of_run')
  assert.equal(find(result, 'QA').displayState, 'unknown')
})

test('다른 버전에 활성 Step이 남으면 임의 우선순위를 정하지 않는다', () => {
  const result = buildAgentRunDisplayState(monitor([
    step('QA', 'RUNNING'), step('QA', 'PENDING', { workflowStepId: 'qa-v2', codeVersion: 2 }),
  ], { status: 'REVALIDATING', codeVersion: 2 }))
  assert.equal(find(result, 'QA').reason, 'conflicting_active_steps')
  assert.equal(find(result, 'QA').displayState, 'unknown')
})

for (const verdict of [null, 'FAIL', 'UNVERIFIED', 'HUMAN_REVIEW', 'SUCCESS']) {
  test(`FINISHED와 verdict=${verdict}를 구분한다`, () => {
    const result = buildAgentRunDisplayState(monitor([], { status: 'FINISHED', verdict }))
    assert.equal(result.run.verdict, verdict)
    assert.equal(result.run.isFinalSuccess, verdict === 'SUCCESS')
    assert.ok(result.agents.every(agent => agent.displayState === 'idle'))
  })
}

test('조회 success나 비종료 Run의 SUCCESS만으로 최종 성공 처리하지 않는다', () => {
  assert.equal(buildAgentRunDisplayState(monitor([])).run.isFinalSuccess, false)
  assert.equal(buildAgentRunDisplayState(monitor([], { verdict: 'SUCCESS' })).run.isFinalSuccess, false)
})

test('Run 또는 Step 조회 실패 시 남아 있는 과거 데이터를 쓰지 않고 오류 원본을 보존한다', () => {
  for (const resource of ['run', 'steps']) {
    const input = monitor([step('PLANNER', 'RUNNING')])
    const error = Object.assign(new Error('조회 실패'), { status: 503, code: 'UNAVAILABLE' })
    input.status[resource] = 'error'
    input.errors[resource] = error
    const result = buildAgentRunDisplayState(input)
    assert.equal(result.availability, 'unavailable')
    assert.ok(result.agents.every(agent => agent.displayState === 'unknown' && agent.step === null))
    assert.equal(result.errors[resource], error)
    if (resource === 'run') assert.equal(result.run.status, null)
  }
})

test('Event 조회 실패는 성공한 Run·Step 변환을 막지 않는다', () => {
  const input = monitor([step('PLANNER', 'RUNNING')])
  input.status.events = 'error'
  input.errors.events = new Error('Event 연결 실패')
  const result = buildAgentRunDisplayState(input)
  assert.equal(result.availability, 'ready')
  assert.equal(find(result, 'PLANNER').displayState, 'working')
  assert.equal(result.errors.events, input.errors.events)
})

test('Run 전환 중 이전 Run 또는 이전 Step이 섞이면 상태를 재사용하지 않는다', () => {
  const input = monitor([step('PLANNER', 'RUNNING')])
  input.runId = otherRunId
  let result = buildAgentRunDisplayState(input)
  assert.ok(result.agents.every(agent => agent.displayState === 'unknown'))
  assert.equal(result.run.status, null)
  input.run.runId = otherRunId
  result = buildAgentRunDisplayState(input)
  assert.ok(result.agents.every(agent => agent.reason === 'invalid_steps_response'))
})

test('조회 중·취소·해제 상태는 이전 작업 상태와 구분한다', () => {
  const input = monitor([step('PLANNER', 'RUNNING')])
  input.status.steps = 'loading'
  assert.ok(buildAgentRunDisplayState(input).agents.every(agent => agent.displayState === 'loading'))
  input.status.steps = 'cancelled'
  assert.ok(buildAgentRunDisplayState(input).agents.every(agent => agent.displayState === 'unknown'))
  input.runId = null
  const result = buildAgentRunDisplayState(input)
  assert.equal(result.availability, 'idle')
  assert.ok(result.agents.every(agent => agent.step === null && agent.reason === 'run_not_selected'))
})

test('알 수 없는 Step 상태는 해당 Agent만 확인 불가로 표시한다', () => {
  const input = monitor([step('QA', 'NEW_SERVER_STATE'), step('SECURITY', 'RUNNING')], { codeVersion: 1 })
  const result = buildAgentRunDisplayState(input)
  assert.equal(result.availability, 'partial')
  assert.equal(find(result, 'QA').reason, 'undefined_step_status')
  assert.equal(find(result, 'QA').step.status, 'NEW_SERVER_STATE')
  assert.equal(find(result, 'SECURITY').displayState, 'working')
})

test('알 수 없는 Run 상태·판정과 불완전한 Run 응답을 성공으로 간주하지 않는다', () => {
  for (const overrides of [{ status: 'NEW_STATE' }, { verdict: 'UNKNOWN' }, { codeVersion: undefined }, { verdict: undefined }, { fixAttempt: -1 }]) {
    const result = buildAgentRunDisplayState(monitor([step('PLANNER', 'RUNNING')], overrides))
    assert.equal(result.run.isFinalSuccess, false)
    assert.equal(result.availability, 'unavailable')
    assert.ok(result.agents.every(agent => agent.displayState === 'unknown'))
  }
})

test('누락된 Step 필드·잘못된 배열·역할을 Step 미생성으로 오해하지 않는다', () => {
  for (const overrides of [{ attempt: undefined }, { codeVersion: undefined }, { workflowStepId: '' }, { attempt: -1 }]) {
    const result = buildAgentRunDisplayState(monitor([step('PLANNER', 'RUNNING', overrides)]))
    assert.equal(find(result, 'PLANNER').reason, 'incomplete_step')
  }
  for (const steps of [null, undefined, {}, [null], [step('UNKNOWN', 'RUNNING')]]) {
    const input = monitor()
    input.steps = steps
    const result = buildAgentRunDisplayState(input)
    assert.equal(result.availability, 'unavailable')
    assert.ok(result.agents.every(agent => agent.reason === 'invalid_steps_response'))
  }
})

test('입력 없음은 네 명의 기본 대기이며 Event로 캐릭터 상태를 만들지 않는다', () => {
  assert.equal(buildAgentRunDisplayState().availability, 'idle')
  const input = monitor()
  input.events = [{ agentRole: 'DEVELOPER', status: 'RUNNING', workflowState: 'FINISHED' }]
  const result = buildAgentRunDisplayState(input)
  assert.equal(result.agents.length, 4)
  assert.deepEqual(result.agents.map(agent => agent.agentRole), roles)
  assert.ok(result.agents.every(agent => agent.displayState === 'idle' && agent.step === null))
  assert.equal(Object.hasOwn(result, 'progress'), false)
})

test('동결된 입력을 수정하지 않는 결정적 순수 함수이며 페이지 정보도 보존한다', () => {
  const freeze = value => {
    if (value && typeof value === 'object') {
      Object.values(value).forEach(freeze)
      Object.freeze(value)
    }
    return value
  }
  const input = freeze(monitor([step('PLANNER', 'RUNNING')]))
  const before = JSON.stringify(input)
  const first = buildAgentRunDisplayState(input)
  assert.deepEqual(first, buildAgentRunDisplayState(input))
  assert.equal(JSON.stringify(input), before)
  assert.deepEqual(first.pagination, input.pagination)
})
