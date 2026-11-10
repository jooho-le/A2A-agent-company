import { artifactCatalog } from './agentArtifactCatalog.js'
import { popupArtifacts, popupIssues } from './agentRunResults.js'
import { publicRunConfiguration, runUuid } from './agentRunSubmission.js'

function apiError(message, { status = null, detail = null, code = 'INVALID_RESPONSE', cause } = {}) {
  const error = new Error(message)
  error.name = 'AgentRunApiError'
  error.status = status
  error.detail = detail
  error.code = code
  if (cause !== undefined) error.cause = cause
  return error
}

// HTTP 실패·통신 실패를 구분하고, AbortError는 그대로 호출자에게 전달합니다.
async function requestJson(path, options, failureMessage) {
  const { signal } = options
  signal?.throwIfAborted()

  let response
  try {
    response = await fetch(path, options)
  } catch (cause) {
    signal?.throwIfAborted()
    if (cause?.name === 'AbortError') throw cause
    throw apiError('서버에 연결할 수 없습니다.', { code: 'NETWORK_ERROR', cause })
  }

  let data = null
  try {
    data = await response.json()
  } catch (cause) {
    // 본문을 읽는 중 취소된 요청도 JSON 형식 오류로 바꾸지 않습니다.
    signal?.throwIfAborted()
    if (cause?.name === 'AbortError') throw cause
  }
  signal?.throwIfAborted()

  if (!response.ok) {
    const detail = data?.detail
    const message = Array.isArray(detail)
      ? detail.map(error => error?.msg).filter(Boolean).join(' ')
      : detail
    throw apiError(
      typeof message === 'string' && message
        ? message : `${failureMessage} (HTTP ${response.status})`,
      {
        status: response.status,
        detail,
        code: typeof detail === 'string' && detail
          ? detail : typeof detail?.code === 'string' ? detail.code : 'HTTP_ERROR',
      },
    )
  }

  return data
}

function runPath(runId) {
  if (typeof runId !== 'string' || !runId.trim()) {
    throw apiError('Run ID가 필요합니다.', { code: 'INVALID_ARGUMENT' })
  }
  return `/api/v1/runs/${encodeURIComponent(runId)}`
}

// 실제 생성은 승인된 공개 설정을 명시적으로 전달한 MULTI_AGENT 경로에서만 허용합니다.
export async function createAgentRun(scenarioId, requestText, architecture = 'MULTI_AGENT', { signal, configuration, approved = false } = {}) {
  if (approved !== true || architecture !== 'MULTI_AGENT' || !runUuid.test(scenarioId)
    || typeof requestText !== 'string' || !requestText.trim()) {
    throw apiError('승인된 실제 실행 요청이 필요합니다.', { code: 'INVALID_ARGUMENT' })
  }
  const publicConfiguration = publicRunConfiguration(configuration)

  const data = await requestJson('/api/v1/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenarioId,
      requestText: requestText.trim(),
      configuration: publicConfiguration,
    }),
    signal,
  }, 'Agent 실행 요청 실패')

  if (!runUuid.test(data?.run?.runId) || data.run.scenarioId !== scenarioId
    || !['SCHEDULED', 'NOT_CONFIGURED'].includes(data.dispatchStatus)) {
    throw apiError('서버의 Run 생성 응답을 확인할 수 없습니다.')
  }

  return data
}

export async function getAgentRunScenarios() {
  const data = await requestJson('/api/v1/scenarios', { method: 'GET' }, '등록 시나리오 조회 실패')
  if (!Array.isArray(data?.scenarios) || data.scenarios.some(item => !runUuid.test(item?.scenarioId))) {
    throw apiError('등록 시나리오 응답을 확인할 수 없습니다.')
  }
  return data.scenarios.map(item => ({ scenarioId: item.scenarioId }))
}

// GET 응답은 { run: ... }으로 감싸지 않은 Run 객체 자체입니다.
export async function getAgentRun(runId, { signal } = {}) {
  const data = await requestJson(runPath(runId), { method: 'GET', signal }, 'Run 상태 조회 실패')
  if (data?.runId !== runId || typeof data?.status !== 'string' || !data.status) {
    throw apiError('서버 응답에서 Run 상태를 확인할 수 없습니다.')
  }
  return data
}

// 공개 configuration 응답에서 architecture만 꺼냅니다. 모델·환경 설정은 상태나 저장소에 보관하지 않습니다.
export async function getAgentRunArchitecture(runId, { signal } = {}) {
  const data = await requestJson(`${runPath(runId)}/configuration`, { method: 'GET', signal }, 'Run 실행 방식 조회 실패')
  const architecture = data?.configuration?.architecture
  if (data?.runId !== runId || data?.artifactType !== 'RUN_CONFIGURATION'
    || !['MULTI_AGENT', 'SINGLE_AGENT'].includes(architecture)) {
    throw apiError('서버 응답에서 Run 실행 방식을 확인할 수 없습니다.')
  }
  return { runId, architecture }
}

export async function getAgentRunSteps(runId, { signal } = {}) {
  const data = await requestJson(`${runPath(runId)}/steps`, { method: 'GET', signal }, 'Step 목록 조회 실패')
  if (data?.runId !== runId || !Array.isArray(data?.steps)) {
    throw apiError('서버 응답에서 Step 목록을 확인할 수 없습니다.')
  }
  return data
}

// 한 페이지씩 조회하며 events와 total·limit·offset을 함께 반환합니다.
// 다음 페이지는 반환된 offset + limit으로 요청하고 total을 기준으로 종료합니다.
export async function getAgentRunEvents(runId, { limit = 100, offset = 0, signal } = {}) {
  const path = runPath(runId)
  if (!Number.isSafeInteger(limit) || limit < 1 || limit > 500
    || !Number.isSafeInteger(offset) || offset < 0) {
    throw apiError('limit은 1~500의 정수, offset은 0 이상의 정수여야 합니다.', { code: 'INVALID_ARGUMENT' })
  }

  const query = new URLSearchParams({ limit: String(limit), offset: String(offset) })
  const data = await requestJson(`${path}/events?${query}`, { method: 'GET', signal }, 'Event 목록 조회 실패')
  if (data?.runId !== runId || !Array.isArray(data?.events)
    || !Number.isSafeInteger(data?.total) || data.total < 0
    || data.limit !== limit || data.offset !== offset || data.events.length > limit) {
    throw apiError('서버 응답에서 Event 목록과 페이지 정보를 확인할 수 없습니다.')
  }
  return data
}

// 목록 API가 Report 본문도 반환하므로 상세 API를 다시 호출하지 않습니다.
export async function getAgentRunArtifacts(runId, { signal } = {}) {
  const data = await requestJson(`${runPath(runId)}/artifacts`, { method: 'GET', signal }, '산출물 조회 실패')
  if (data?.runId !== runId || !Array.isArray(data.artifacts) || data.artifacts.some(row => !row || row.runId !== runId)) {
    throw apiError('서버 응답에서 산출물을 확인할 수 없습니다.')
  }
  return { runId, artifacts: popupArtifacts(data.artifacts), catalog: artifactCatalog(data.artifacts, runId) }
}

export async function getAgentRunIssues(runId, { signal } = {}) {
  const data = await requestJson(`${runPath(runId)}/issues`, { method: 'GET', signal }, 'Issue 조회 실패')
  if (data?.runId !== runId || !Array.isArray(data.issues) || data.issues.some(row => !row || row.runId !== runId)) {
    throw apiError('서버 응답에서 Issue를 확인할 수 없습니다.')
  }
  return { runId, issues: popupIssues(data.issues) }
}
