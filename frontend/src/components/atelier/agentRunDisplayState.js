import { confirmedHandoffs } from './officeHandoffEvents.js'

// API 원본 상태를 3D 표시용 데이터로만 변환합니다. 조회·모션·진행률·세리머니는 처리하지 않습니다.
const roles = [
  ['PLANNER', '계획에이전트'], ['DEVELOPER', '개발에이전트'], ['QA', 'QA에이전트'], ['SECURITY', '보안에이전트'],
]
const runStates = new Set([
  'RECEIVED', 'PLANNING', 'WAITING_INPUT', 'IMPLEMENTING', 'SNAPSHOT_READY',
  'VALIDATING', 'FIX_REQUIRED', 'FIXING', 'REVALIDATING', 'HUMAN_REVIEW', 'FINISHED', 'ABORTED',
])
const verdicts = new Set(['SUCCESS', 'FAIL', 'UNVERIFIED', 'HUMAN_REVIEW'])
const stepStates = {
  PENDING: ['pending', '대기'],
  RUNNING: ['working', '작업'],
  SUCCEEDED: ['succeeded', '단계 완료'],
  FAILED: ['failed', '단계 실패'],
  WAITING_INPUT: ['waiting_input', '입력 대기'],
  CANCELED: ['canceled', '취소'],
}
const isVersion = value => value === null || (Number.isSafeInteger(value) && value >= 1)
const isAttempt = value => Number.isSafeInteger(value) && value >= 0
const hasId = value => typeof value === 'string' && value.trim().length > 0

function unavailable(reason, displayState = 'unknown', label = '상태 확인 불가') {
  return { displayState, label, reason, step: null, candidateStepIds: [] }
}

function selectStep(agentRole, steps, run) {
  const candidates = steps.filter(step => step.agentRole === agentRole)
  const candidateStepIds = candidates.map(step => step.workflowStepId).filter(hasId)
  const unknown = reason => ({ ...unavailable(reason), candidateStepIds })
  if (candidates.length === 0) return unavailable('step_not_created', 'idle', '기본 대기')
  if (candidates.some(step => !hasId(step.workflowStepId) || !isAttempt(step.attempt) || !isVersion(step.codeVersion))) {
    return unknown('incomplete_step')
  }

  // Developer 생성 시의 code_version_for_fix_attempt와 같은 기준입니다.
  // 입력 대기·중단 중에도 마지막 개발 시도는 fixAttempt + 1이며, QA·Security는
  // Run의 확정된 코드 버전을 검증합니다. Planner는 코드 버전과 무관합니다.
  const targetVersion = agentRole === 'PLANNER' ? null
    : agentRole === 'DEVELOPER' ? run.fixAttempt + 1 : run.codeVersion
  // Run과 Step은 별도 GET이므로 시점이 엇갈릴 수 있습니다. 더 새 버전이나 다른 버전의
  // 활성 Step이 보이면 과거 결과를 현재 상태로 확정하지 않고 다음 조회를 기다립니다.
  if (targetVersion !== null && candidates.some(step => step.codeVersion > targetVersion)) {
    return unknown('step_version_ahead_of_run')
  }
  if (targetVersion !== null && candidates.some(step => step.codeVersion !== targetVersion
    && ['PENDING', 'RUNNING', 'WAITING_INPUT'].includes(step.status))) {
    return unknown('conflicting_active_steps')
  }
  const matching = agentRole !== 'PLANNER' && targetVersion !== null
    ? candidates.filter(step => step.codeVersion === targetVersion) : candidates
  if (matching.length === 0) {
    return { ...unavailable('step_not_created_for_version', 'idle', '기본 대기'), candidateStepIds }
  }
  // attempt는 같은 workflowStepId의 재개 횟수입니다(a2a_tasks.py). 다른 ID끼리의 순번이
  // 아닙니다. 같은 역할·버전의 여러 Step은 UUID·배열 순서·시각·활성 상태로 임의 선택하지 않습니다.
  if (matching.length !== 1) return unknown('ambiguous_steps')
  const step = matching[0]
  const mapping = Object.hasOwn(stepStates, step.status) ? stepStates[step.status] : null
  return {
    displayState: mapping?.[0] ?? 'unknown',
    label: mapping?.[1] ?? '상태 확인 불가',
    reason: mapping ? null : 'undefined_step_status',
    // workflowStepId·agentRole·status·attempt·codeVersion 및 나머지 원본 필드를 보존합니다.
    step: { ...step },
    candidateStepIds,
  }
}

/**
 * useAgentRunMonitor의 현재 스냅샷 → 네 캐릭터의 표시 상태.
 * queryStatus는 API 조회 결과이고 run.status·step.status는 실행 상태입니다.
 * Event로 역할/상태를 추정하지 않으며, 이 반환값으로 목업 런타임을 구동하지 않습니다.
 */
export function buildAgentRunDisplayState(monitor = {}) {
  const runId = hasId(monitor.runId) ? monitor.runId : null
  const queryStatus = { ...monitor.status }
  const run = monitor.run
  let blocked = null
  if (!runId) blocked = unavailable('run_not_selected', 'idle', '기본 대기')
  else if (queryStatus.run === 'error' || queryStatus.steps === 'error') blocked = unavailable('query_failed')
  else if (queryStatus.run === 'loading' || queryStatus.steps === 'loading') blocked = unavailable('query_loading', 'loading', '조회 중')
  else if (queryStatus.run !== 'success' || !['success', 'empty'].includes(queryStatus.steps)) blocked = unavailable('query_not_available')

  const validRun = runId !== null && run?.runId === runId && runStates.has(run?.status)
    && (run.verdict === null || verdicts.has(run.verdict)) && isVersion(run.codeVersion) && isAttempt(run.fixAttempt)
  if (!blocked && !validRun) blocked = unavailable('invalid_run_response')
  if (!blocked && (!Array.isArray(monitor.steps) || monitor.steps.some(step =>
    !step || step.runId !== runId || !roles.some(([role]) => role === step.agentRole)))) {
    blocked = unavailable('invalid_steps_response')
  }

  const agents = roles.map(([agentRole, name]) => ({
    agentRole, name,
    ...(blocked ? { ...blocked, candidateStepIds: [] } : selectStep(agentRole, monitor.steps, run)),
  }))
  const runAvailable = queryStatus.run === 'success' && validRun
  return {
    source: 'orchestrator',
    architecture: monitor.architecture ?? null,
    runId,
    queryStatus,
    // 오류 원본과 Event 페이지 정보를 보존하지만 캐릭터 상태 결정에는 사용하지 않습니다.
    errors: { ...monitor.errors },
    pagination: { ...monitor.pagination },
    trace: monitor.trace ?? null,
    popupResults: monitor.popupResults?.runId === runId ? monitor.popupResults : null,
    traceSteps: Array.isArray(monitor.steps) ? monitor.steps : [],
    handoffs: confirmedHandoffs(monitor.events, monitor.steps, runId),
    run: {
      status: runAvailable ? run.status : null,
      verdict: runAvailable ? run.verdict : null,
      codeVersion: runAvailable ? run.codeVersion : null,
      fixAttempt: runAvailable ? run.fixAttempt : null,
      isFinalSuccess: runAvailable && run.status === 'FINISHED' && run.verdict === 'SUCCESS',
    },
    availability: !runId ? 'idle' : blocked ? blocked.displayState === 'loading' ? 'loading' : 'unavailable'
      : agents.some(agent => agent.displayState === 'unknown') ? 'partial' : 'ready',
    agents,
  }
}
