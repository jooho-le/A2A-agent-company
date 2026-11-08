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

// 기존 2·3개 인자 호출을 유지합니다. 네 번째 인자는 요청 취소에만 사용합니다.
// 승인 configuration은 아직 전달받지 않았으므로 모델·실행 환경을 임의로 채우지 않습니다.
export async function createAgentRun(scenarioId, requestText, architecture = 'MULTI_AGENT', { signal } = {}) {
  if (architecture !== 'MULTI_AGENT' && architecture !== 'SINGLE_AGENT') {
    throw apiError('architecture는 MULTI_AGENT 또는 SINGLE_AGENT여야 합니다.', { code: 'INVALID_ARGUMENT' })
  }

  const data = await requestJson('/api/v1/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenarioId,
      requestText,
      configuration: { architecture },
    }),
    signal,
  }, 'Agent 실행 요청 실패')

  if (!data?.run?.runId) {
    throw apiError('서버 응답에서 Run ID를 찾을 수 없습니다.')
  }

  return data
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
