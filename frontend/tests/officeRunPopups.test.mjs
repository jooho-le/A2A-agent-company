import assert from 'node:assert/strict'
import test from 'node:test'
import { buildAgentRunDisplayState } from '../src/components/atelier/agentRunDisplayState.js'
import { buildOfficeRunPopups, popupResultNotice, renderOfficeRunPopups } from '../src/components/atelier/officeRunPopups.js'
import { createOfficeTraceController, traceAction } from '../src/components/atelier/officeTraceEvents.js'
import { createOfficeRunController } from '../src/components/atelier/office3dRunController.js'
import { createRunEventFeed } from '../src/hooks/agentRunEventFeed.js'
import { createRunSuccessCeremony } from '../src/components/atelier/runSuccessCeremony.js'
import { popupArtifacts, popupIssues } from '../src/api/agentRunResults.js'

const runId = '00000000-0000-4000-8000-000000000001'
const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
function fixture({ states = ['RUNNING', 'RUNNING', 'RUNNING', 'RUNNING'], version = 1, artifacts = [], issues = [], run = {} } = {}) {
  const steps = roles.map((agentRole, index) => ({ runId, workflowStepId: `step-${index}-v${version}`, agentRole, status: states[index],
    attempt: 0, codeVersion: index === 0 ? null : version, a2aTaskId: `task-${index}-v${version}`,
    inputArtifactIds: index >= 2 ? ['source-1'] : [], outputArtifactIds: artifacts.filter(row => row.createdBy === agentRole).map(row => row.artifactId) }))
  const snapshot = buildAgentRunDisplayState({ runId, steps, events: [], architecture: 'MULTI_AGENT',
    run: { runId, status: 'VALIDATING', verdict: null, codeVersion: version, fixAttempt: version - 1, ...run },
    status: { run: 'success', steps: 'success', events: 'empty', architecture: 'success', trace: 'success' }, errors: {},
    popupResults: { runId, artifacts: popupArtifacts(artifacts), issues: popupIssues(issues), status: { artifacts: 'success', issues: 'success' } } })
  snapshot.trace = { runId, epoch: 1, initialized: true, entries: [] }
  return { snapshot, steps }
}
function rows(snapshot) {
  const controller = createOfficeRunController()
  controller.setSnapshot(snapshot)
  return [...controller.advance(.1), { agentRole: 'ORCHESTRATOR', displayState: 'working', label: '실행 관리 중', step: null }]
}
const makeEvent = (f, eventType, index, extra = {}) => ({ runId, eventId: eventType, eventType,
  workflowStepId: index === null ? null : f.steps[index].workflowStepId,
  actor: 'Orchestrator', codeVersion: index === null ? 1 : f.steps[index].codeVersion,
  attempt: 0, occurredAt: '2026-11-09T04:00:00Z', workflowState: f.snapshot.run.status, ...extra })
function render(f, events = [], options = {}) {
  const current = events.map(event => traceAction(event, f.steps, f.snapshot.runId))
  return buildOfficeRunPopups(f.snapshot, rows(f.snapshot), { ready: true, current }, options)
}
const report = (index, artifactType, extra = {}) => ({ artifactId: `report-${index}`, artifactType, runId, workflowStepId: `step-${index}-v1`,
  a2aTaskId: `task-${index}-v1`, createdBy: roles[index], codeVersion: index === 0 ? null : 1, artifactVersion: 1, ...extra })

test('Planner 분석·Developer 개발·QA·Security 병렬 작업은 각각의 실제 Step을 표시한다', () => {
  const cards = render(fixture())
  assert.equal(cards.length, 5)
  assert.deepEqual(cards.slice(0, 4).map(card => card.title), ['요구사항 분석 중', '개발 진행 중', 'QA 검증 중', '보안 검사 중'])
  assert.ok(cards.slice(0, 4).every(card => card.status === '작업'))
  assert.equal(cards[4].title, '검증 진행')
})

test('Developer 수정은 현재 코드 버전·수정 시도를 기준으로 표시한다', () => {
  const cards = render(fixture({ version: 2, run: { status: 'FIXING' }, issues: [{ runId, codeVersion: 1,
    fixWorkflowStepId: 'step-1-v2', newCodeVersion: 2, description: '필수 입력 누락 처리를 수정해 주세요.' }] }))
  assert.equal(cards[1].title, '코드 수정 중')
  assert.match(cards[1].detail, /코드 버전 2/)
  assert.match(cards[1].detail, /필수 입력 누락/)
})

test('이벤트 연출 뒤 늦게 도착한 Issue도 같은 Step의 검증 결과에 연결한다', () => {
  const f = fixture({ states: ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'RUNNING'], artifacts: [report(2, 'QA_REPORT', {
    tests: [{ outcome: 'FAIL', title: '입력 검사' }],
  })] })
  assert.doesNotMatch(render(f)[2].detail, /누락 값/)
  f.snapshot.popupResults.issues = popupIssues([{ runId, issueId: 'late-issue', codeVersion: 1, reporter: 'QA',
    reportArtifactId: 'report-2', sourceArtifactId: 'source-1', description: '누락 값의 오류 응답이 잘못되었습니다.' }])
  assert.match(render(f)[2].detail, /누락 값/)
  f.snapshot.popupResults.status.issues = 'error'
  assert.doesNotMatch(render(f)[2].detail, /누락 값/)
})

test('QA 미검증 사유와 Security FAIL의 actualResult를 실제 본문에서 요약한다', () => {
  const f = fixture({ states: ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED'], artifacts: [
    report(2, 'QA_REPORT', { tests: [{ outcome: 'UNVERIFIED', details: '테스트 도구 연결을 확인하지 못했습니다.' }] }),
    report(3, 'SECURITY_REPORT', { requirementResults: [{ outcome: 'FAIL', actualResult: '접근 제한이 적용되지 않았습니다.' }] }),
  ] })
  assert.match(render(f)[2].detail, /미검증 사유: 테스트 도구/)
  assert.match(render(f)[3].detail, /접근 제한이 적용되지/)
})

test('Planner 등록 이벤트는 연결된 REQUIREMENT의 실제 분석 결과를 표시한다', () => {
  const f = fixture({ artifacts: [report(0, 'REQUIREMENT', { requirementIds: ['req-1'], payload: { schemaVersion: 1, requirements: [{ description: '예약 시간 중복을 방지합니다.' }] } })] })
  const event = makeEvent(f, 'ARTIFACT_REGISTERED', 0, { actor: 'PLANNER', outputArtifactIds: ['report-0'] })
  const card = render(f, [event])[0]
  assert.equal(card.title, '요구사항 산출물 등록')
  assert.match(card.detail, /예약 시간 중복/)
  assert.equal(card.eventId, event.eventId)
})

test('코드·수정·빌드 산출물의 실제 결과를 표시한다', () => {
  for (const [type, extra, detail] of [
    ['SOURCE', {}, /코드 버전 1/],
    ['CHANGE_REPORT', { summary: '필수 입력 검사를 추가했습니다.', fileChanges: [{}] }, /필수 입력 검사/],
    ['BUILD_REPORT', { executionOutcome: 'FAIL', exitCode: 1, diagnostics: [{ message: '컴파일 오류가 보고되었습니다.' }] }, /빌드 실패/],
    ['BUILD_REPORT', { executionOutcome: 'UNVERIFIED', failureKind: 'INFRASTRUCTURE', exitCode: -1 }, /빌드 미검증.*실행 환경/],
  ]) {
    const f = fixture({ artifacts: [report(1, type, extra)] })
    const card = render(f, [makeEvent(f, 'ARTIFACT_REGISTERED', 1, { actor: 'DEVELOPER', outputArtifactIds: ['report-1'] })])[1]
    assert.match(card.detail, detail)
  }
})

test('Step SUCCEEDED와 QA FAIL은 함께 표시하며 캐릭터를 실행 실패로 바꾸지 않는다', () => {
  const f = fixture({ states: ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'RUNNING'], artifacts: [report(2, 'QA_REPORT', {
    tests: [{ outcome: 'PASS' }, { outcome: 'FAIL', title: '중복 입력', details: '중복 입력 시 오류 응답이 잘못되었습니다.' }, { outcome: 'UNVERIFIED' }],
  })] })
  const card = render(f)[2]
  assert.equal(card.status, '단계 완료')
  assert.equal(card.title, 'QA 검증 결과')
  assert.match(card.detail, /통과 1 · 실패 1 · 미검증 1/)
  assert.match(card.detail, /중복 입력 시/)
  assert.equal(rows(f.snapshot)[2].expression, 'normal')
  assert.equal(rows(f.snapshot)[2].displayState, 'succeeded')
})

test('Step FAILED는 검증 FAIL과 별도로 Agent 실행 실패라고 표시한다', () => {
  const f = fixture({ states: ['RUNNING', 'RUNNING', 'FAILED', 'RUNNING'], artifacts: [report(2, 'QA_REPORT', { tests: [{ outcome: 'PASS' }] })] })
  assert.equal(render(f)[2].title, '에이전트 실행 실패')
  assert.match(render(f)[2].detail, /원인은.*제공되지/)
  assert.equal(rows(f.snapshot)[2].expression, 'worried')
})

test('Security 미검증·의심 발견을 확정된 취약점이나 통과로 바꾸지 않는다', () => {
  const f = fixture({ states: ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED'], artifacts: [report(3, 'SECURITY_REPORT', {
    requirementResults: [{ outcome: 'UNVERIFIED' }], findings: [{ disposition: 'SUSPECTED', severity: 'HIGH', description: '접근 제한을 추가 확인해야 합니다.' }],
  })] })
  const card = render(f)[3]
  assert.match(card.detail, /미검증 1/)
  assert.match(card.detail, /의심: 접근 제한/)
  assert.equal(card.tone, 'normal')
})

test('QA·Security Issue는 ID·Run·역할·버전·Source·Report가 일치할 때만 연결한다', () => {
  for (const index of [2, 3]) {
    const issue = { runId, issueId: 'issue-1', codeVersion: 1, reporter: roles[index], sourceArtifactId: 'source-1',
      reportArtifactId: `report-${index}`, title: '검사 문제', description: '검사에서 접근 제한 누락이 보고되었습니다.' }
    const f = fixture({ issues: [issue] })
    const event = makeEvent(f, 'ISSUE_DETECTED', index, { actor: roles[index], issueId: 'issue-1', inputArtifactIds: ['source-1'], outputArtifactIds: [`report-${index}`] })
    assert.match(render(f, [event])[index].detail, /접근 제한 누락/)
    for (const mismatch of [{ runId: 'other' }, { codeVersion: 2 }, { reporter: 'DEVELOPER' }, { sourceArtifactId: 'other' }, { reportArtifactId: 'other' }]) {
      f.snapshot.popupResults.issues = popupIssues([{ ...issue, ...mismatch }])
      assert.equal(render(f, [event])[index].detail, '검증 과정에서 문제가 발견되었습니다. 상세 결과 확인이 필요합니다.')
    }
  }
})

test('산출물 ID·Step·task·버전·역할이 다르거나 중복이면 현재 결과로 임의 선택하지 않는다', () => {
  for (const mismatch of [{ artifactId: 'other' }, { workflowStepId: 'other' }, { a2aTaskId: 'old-task' }, { codeVersion: 2 }, { createdBy: 'SECURITY' }]) {
    const f = fixture({ states: ['RUNNING', 'RUNNING', 'SUCCEEDED', 'RUNNING'], artifacts: [report(2, 'QA_REPORT', { tests: [{ outcome: 'FAIL', details: '잘못 연결된 결과' }] })] })
    Object.assign(f.snapshot.popupResults.artifacts[0], mismatch)
    assert.doesNotMatch(render(f)[2].detail, /잘못 연결된 결과/)
  }
  const f = fixture({ states: ['RUNNING', 'RUNNING', 'SUCCEEDED', 'RUNNING'], artifacts: [report(2, 'QA_REPORT', { tests: [{ outcome: 'FAIL' }] })] })
  f.snapshot.popupResults.artifacts.push({ ...f.snapshot.popupResults.artifacts[0] })
  assert.equal(render(f)[2].title, '업무 단계 완료')
})

test('확인된 업무 전달·수정 요청은 리더와 실제 대상에게 표시하고 불명확한 수신자를 만들지 않는다', () => {
  const f = fixture()
  for (const type of ['A2A_MESSAGE_SENT', 'FIX_REQUESTED']) {
    const cards = render(f, [makeEvent(f, type, 1)])
    assert.equal(cards[4].eventId, type)
    assert.equal(cards[1].eventId, type)
    assert.match(cards[4].detail, /개발에이전트/)
    const unknown = render(f, [makeEvent(f, type, null)])
    assert.ok(unknown.every(card => card.eventId === null))
  }
})

test('검증 시작·최종 판정은 실제 이벤트와 같은 버전의 Run 판정으로 표시한다', () => {
  const f = fixture({ run: { status: 'FINISHED', verdict: 'FAIL' } })
  assert.match(render(f, [makeEvent(f, 'VALIDATION_STARTED', null)])[4].title, /검증 시작/)
  assert.match(render(f, [makeEvent(f, 'VERDICT_CREATED', null)])[4].detail, /판정: 실패/)
  for (const change of [{ codeVersion: 2 }, { attempt: 1 }, { workflowState: 'HUMAN_REVIEW' }]) {
    const card = render(f, [makeEvent(f, 'VERDICT_CREATED', null, change)])[4]
    assert.match(card.detail, /판정값은 확인되지/)
  }
})

test('과거 이벤트는 현재 Step 상태 배지를 덮어쓰지 않는다', () => {
  const f = fixture({ states: ['RUNNING', 'SUCCEEDED', 'RUNNING', 'RUNNING'] })
  const card = render(f, [makeEvent(f, 'A2A_MESSAGE_SENT', 1)])[1]
  assert.equal(card.status, '단계 완료')
  assert.match(card.title, /기록/)
  assert.doesNotMatch(card.detail, /진행 중|실행 중/)
})

test('Trace 큐를 그대로 사용해 묶음·중복을 순서대로 표시하고 QA·보안 병렬 호출을 유지한다', () => {
  const f = fixture(), queue = createOfficeTraceController()
  const events = [makeEvent(f, 'A2A_MESSAGE_SENT', 0, { eventId: 'p' }), makeEvent(f, 'A2A_MESSAGE_SENT', 1, { eventId: 'd' }),
    makeEvent(f, 'A2A_MESSAGE_SENT', 2, { eventId: 'q' }), makeEvent(f, 'A2A_MESSAGE_SENT', 3, { eventId: 's' })]
  f.snapshot.trace.entries = [...events, events[0]].map((event, offset) => ({ event, offset }))
  queue.observe(f.snapshot, 'actual')
  const shown = new Set(), order = []
  let concurrent = false
  for (let n = 0; queue.busy() && n < 1000; n++) {
    queue.advance(.05)
    const cards = buildOfficeRunPopups(f.snapshot, rows(f.snapshot), queue.info())
    if (cards[2].eventId === 'q' && cards[3].eventId === 's') concurrent = true
    for (const card of cards) if (card.eventId && !shown.has(card.eventId)) { shown.add(card.eventId); order.push(card.eventId) }
  }
  assert.equal(queue.busy(), false)
  assert.deepEqual(order, ['p', 'd', 'q', 's'])
  assert.equal(concurrent, true)
  queue.observe(f.snapshot, 'actual')
  assert.equal(queue.busy(), false)
})

test('과거 완료 Run 첫 조회는 기존 Feed의 기준점 이후 이벤트만 팝업에 전달한다', async () => {
  const f = fixture({ states: ['SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED', 'SUCCEEDED'], run: { status: 'FINISHED', verdict: 'SUCCESS' } })
  const feed = createRunEventFeed(), queue = createOfficeTraceController()
  f.snapshot.trace = await feed.pull(runId, async (id, { limit, offset }) => ({ runId: id, total: 100, limit, offset,
    events: [makeEvent(f, 'ISSUE_DETECTED', 2, { issueId: 'old' })] }))
  queue.observe(f.snapshot, 'actual')
  assert.equal(queue.busy(), false)
  assert.ok(buildOfficeRunPopups(f.snapshot, rows(f.snapshot), queue.info()).every(card => card.eventId === null))
})

test('Run 변경·목업·세리머니에서는 이전 실제 팝업을 제거하고 HTML 문자열은 textContent로만 쓴다', () => {
  const f = fixture(), cards = render(f)
  const elements = new Map()
  const root = { querySelector(selector) {
    if (!elements.has(selector)) elements.set(selector, { dataset: {}, hidden: false, set innerHTML(value) { throw Error(`HTML 삽입 금지: ${value}`) } })
    return elements.get(selector)
  } }
  cards[2].detail = '<img src=x onerror=alert(1)>'
  renderOfficeRunPopups(root, cards)
  assert.equal(root.querySelector('[data-office3d="actual-detail-2"]').textContent, cards[2].detail)
  for (const options of [{ mode: 'mock' }, { ceremony: true }]) {
    const hidden = render(f, [], options)
    assert.deepEqual(hidden, [])
    renderOfficeRunPopups(root, hidden)
    assert.equal(root.querySelector('[data-office3d="actual-popups"]').hidden, true)
    assert.equal(root.querySelector('[data-office3d="actual-detail-2"]').textContent, '')
  }
  f.snapshot.runId = 'other-run'
  f.snapshot.popupResults.artifacts = [report(2, 'QA_REPORT', { tests: [{ outcome: 'FAIL', details: '이전 Run 비밀 결과' }] })]
  f.snapshot.agents[2].displayState = 'succeeded'
  assert.doesNotMatch(JSON.stringify(render(f)), /이전 Run 비밀 결과/)
})

test('부가 조회 실패는 실행 실패·Trace 정지·성공 세리머니 차단이 아니며 상세 오류 원문을 표시하지 않는다', () => {
  const f = fixture({ run: { status: 'FINISHED', verdict: 'SUCCESS' } })
  f.snapshot.popupResults.status = { artifacts: 'error', issues: 'error' }
  assert.match(popupResultNotice(f.snapshot), /상세 조회 실패/)
  assert.equal(render(f)[2].title, 'QA 검증 중')
  f.snapshot.trace.entries = [{ offset: 0, event: makeEvent(f, 'A2A_MESSAGE_SENT', 2) }]
  const queue = createOfficeTraceController()
  queue.observe(f.snapshot, 'actual')
  assert.equal(queue.info().ready, true)
  const ledger = new Map()
  const gate = createRunSuccessCeremony({ getStorage: () => ({ getItem: key => ledger.get(key) ?? null, setItem: (key, value) => ledger.set(key, value) }) })
  gate.observe(f.snapshot, 'actual')
  assert.equal(gate.start({ manual: true, ready: true, motionEnabled: true }), true)
  const hidden = buildOfficeRunPopups(f.snapshot, rows(f.snapshot), queue.info(), { ceremony: true })
  assert.deepEqual(hidden, [])
})

test('Run·Step 조회 실패 또는 갱신 중에는 과거 이벤트를 현재 작업처럼 표시하지 않는다', () => {
  const f = fixture()
  const action = makeEvent(f, 'A2A_MESSAGE_SENT', 2)
  f.snapshot.queryStatus.steps = 'error'
  assert.equal(render(f, [action])[2].title, '상태 확인 불가')
  f.snapshot.queryStatus.steps = 'loading'
  assert.equal(render(f, [action])[2].title, '상태 갱신 중')
})

test('진행 중 Trace 팝업은 폴링 중 유지하고 조회 오류·Run 변경·목업·세리머니에는 유지하지 않는다', () => {
  const f = fixture(), event = makeEvent(f, 'A2A_MESSAGE_SENT', 2)
  const current = [traceAction(event, f.steps, runId)]
  const popups = buildOfficeRunPopups(f.snapshot, rows(f.snapshot), { ready: true, current })
  const previous = { runId, popups }
  const loading = { ...f.snapshot, run: { status: null }, availability: 'loading',
    queryStatus: { ...f.snapshot.queryStatus, run: 'loading', steps: 'loading', trace: 'loading' } }
  const paused = { ready: false, current }
  assert.strictEqual(buildOfficeRunPopups(loading, rows(loading), paused, { previous }), popups)
  for (const changed of [
    { ...loading, errors: { run: '조회 실패' } },
    { ...loading, queryStatus: { ...loading.queryStatus, run: 'error' } },
    { ...loading, queryStatus: { ...loading.queryStatus, trace: 'cancelled' } },
    { ...loading, runId: 'another-run' },
  ]) assert.ok(buildOfficeRunPopups(changed, rows(changed), paused, { previous }).every(card => !card.eventId))
  for (const options of [{ mode: 'mock' }, { ceremony: true }]) {
    assert.deepEqual(buildOfficeRunPopups(loading, rows(loading), paused, { previous, ...options }), [])
  }
  const after = buildOfficeRunPopups(f.snapshot, rows(f.snapshot), { ready: true, current: [] }, { previous })
  assert.equal(after[2].eventId, null)
  assert.equal(after[2].title, 'QA 검증 중')
})
