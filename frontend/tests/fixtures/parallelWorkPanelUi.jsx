import { act } from 'react'
import { createRoot } from 'react-dom/client'
import RunWorkPanel from '../../src/components/atelier/RunWorkPanel'
import { buildAgentRunDisplayState } from '../../src/components/atelier/agentRunDisplayState'

globalThis.IS_REACT_ACT_ENVIRONMENT = true
globalThis.fetch = () => { throw new Error('No network allowed') }
const roles = ['PLANNER', 'DEVELOPER', 'QA', 'SECURITY']
const controls = { data: { requests: [], canCancel: false }, commands: {}, refresh() {}, reply() {}, cancel() {} }
function snapshot({ id = 'run-one', qa = 'RUNNING', runQuery = 'success', stepsQuery = 'success' } = {}) {
  return buildAgentRunDisplayState({ runId: id, architecture: 'MULTI_AGENT',
    run: { runId: id, status: 'VALIDATING', verdict: null, codeVersion: 1, fixAttempt: 0 },
    status: { run: runQuery, steps: stepsQuery }, errors: {},
    steps: roles.map((agentRole, i) => ({ runId: id, workflowStepId: `step-${i}`, agentRole,
      attempt: 0, codeVersion: i === 0 ? null : 1, status: i < 2 ? 'SUCCEEDED' : i === 2 ? qa : 'RUNNING' })) })
}
function check(value, message) { if (!value) throw new Error(message) }
const host = document.createElement('div'); document.body.append(host)
const root = createRoot(host)
async function render(displayState, extra = {}) {
  await act(async () => root.render(<RunWorkPanel runId={displayState.runId} projectName="Mock 병렬 작업"
    displayState={displayState} controls={controls} selectedRole={null} onSelectRole={() => {}}
    onResults={() => {}} onNew={() => {}} {...extra} />))
}
const heading = () => document.querySelector('.run-person-heading').textContent
async function verify() {
  const passed = []
  await render(snapshot())
  check(heading().includes('품질 검사 · 보안 검사'), 'both names in one heading')
  check(document.querySelectorAll('.run-parallel-work section').length === 2, 'both work summaries visible')
  const original = heading()
  const qaSection = document.querySelector('.run-parallel-work section')
  qaSection.querySelector('details').open = true
  for (let i = 0; i < 3; i++) {
    await render(snapshot({ runQuery: 'loading', stepsQuery: 'loading' }))
    check(heading() === original, 'polling never switches to leader')
    check(document.querySelectorAll('.run-employee-picker [data-state=working]:not([data-agent-role=ORCHESTRATOR])').length === 2, 'both working highlights retained')
    await render(snapshot())
    check(heading() === original, 'confirmed concurrent work stays grouped')
  }
  check(document.querySelector('.run-parallel-work section') === qaSection && qaSection.querySelector('details').open, 'no remount or collapsed details while polling')
  passed.push('병렬 직원명·작업 내용 동시 표시, 폴링 중 유지')

  await render(snapshot(), { selectedRole: 'QA' })
  check(heading().includes('품질 검사 · 보안 검사'), 'working employee selection retains parallel view')
  await render(snapshot(), { selectedRole: 'PLANNER' })
  check(!document.querySelector('.run-parallel-work') && heading().includes('기획'), 'explicit nonworking employee detail remains accessible')
  const waiting = snapshot({ qa: 'WAITING_INPUT' })
  await render(waiting, { controls: { ...controls, data: { requests: [{ agentRole: 'QA', workflowStepId: 'step-2',
    reason: '확인이 필요해요', questions: ['범위를 확인해주세요'], replyMode: 'ANSWER' }] } } })
  check(document.querySelector('.run-question')?.textContent.includes('범위를 확인해주세요'), 'input request remains actionable')
  await render(snapshot({ qa: 'SUCCEEDED' }))
  check(!document.querySelector('.run-parallel-work') && heading().includes('보안 검사'), 'confirmed completion switches to remaining employee')
  passed.push('직원 선택·확인 요청·한 명 완료 시 실제 상태 반영')

  await render(snapshot())
  await render(snapshot({ stepsQuery: 'error' }))
  check(!document.querySelector('.run-parallel-work'), 'failure does not retain stale working summary')
  await render(snapshot({ runQuery: 'loading', stepsQuery: 'loading' }))
  check(!document.querySelector('.run-parallel-work'), 'loading after failure cannot restore stale work')
  await render(snapshot())
  await render(snapshot({ id: 'another-run', runQuery: 'loading', stepsQuery: 'loading' }))
  check(!document.querySelector('.run-parallel-work'), 'new Run cannot reuse previous employees')
  passed.push('조회 오류·오류 후 재조회·Run 변경 시 이전 작업 정보 제거')
  await act(async () => root.unmount())
  return { passed }
}
verify().then(report => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify(report); document.body.append(result)
}).catch(error => {
  const result = document.createElement('pre'); result.id = 'test-result'; result.textContent = JSON.stringify({ failed: error.stack }); document.body.append(result)
})
