
export async function createAgentRun(scenarioId, requestText) {
  const response = await fetch('/api/v1/runs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      scenarioId,
      requestText,
    }),
  })

  const data = await response.json().catch(() => null)

  if (!response.ok) {
    const detail = Array.isArray(data?.detail)
      ? data.detail.map(error => error.msg).filter(Boolean).join(' ')
      : data?.detail

    throw new Error(
      typeof detail === 'string' && detail
        ? detail
        : `Agent 실행 요청 실패 (HTTP ${response.status})`
    )
  }

  if (!data?.run?.runId) {
    throw new Error('서버 응답에서 Run ID를 찾을 수 없습니다.')
  }

  return data
}
