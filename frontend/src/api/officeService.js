import { runApiTransport, loginRequired } from './productRunRequest.js'
import { runUuid } from './agentRunSubmission.js'

async function json(path, options = {}) {
  const response = await runApiTransport.fetch(path, { ...options, cache: 'no-store' })
  if (response.status === 401) throw loginRequired()
  let data
  try { data = await response.json() } catch { throw new Error('서버 응답을 확인할 수 없습니다.') }
  if (!response.ok) {
    const message = ({ 404: '접수 기록을 찾을 수 없습니다.', 409: '요청 또는 승인 설정이 변경되었습니다. 설정을 다시 확인해 주세요.',
      422: '현재 실행 가능한 시나리오와 요청 내용을 확인해 주세요.', 503: '실행 서버가 준비되지 않았습니다. 서버 연결과 승인 설정을 확인해 주세요.' })[response.status]
    throw Object.assign(new Error(message || '업무 요청을 처리하지 못했습니다.'), { status: response.status })
  }
  return data
}

export async function loadOfficeApproval(signal) {
  const { scenarios } = await json('/api/v1/scenarios', { signal })
  if (!Array.isArray(scenarios) || scenarios.length !== 1 || !runUuid.test(scenarios[0]?.scenarioId)) {
    throw new Error('실행 가능한 시나리오를 확인할 수 없습니다.')
  }
  const scenarioId = scenarios[0].scenarioId
  const approved = await json('/api/v1/host/approved-configuration?' + new URLSearchParams({ scenarioId }), { signal })
  if (approved.scenarioId !== scenarioId || !/^[0-9a-f]{64}$/.test(approved.configurationId)
    || approved.configuration?.architecture !== 'MULTI_AGENT') throw new Error('승인된 실행 설정을 확인할 수 없습니다.')
  return { scenarioId, configurationId: approved.configurationId }
}

function validateReceipt(data, key) {
  if (data?.requestId !== key || !runUuid.test(data.projectId)
    || !['PENDING', 'UNKNOWN', 'REJECTED', 'CREATED'].includes(data.submissionStatus)
    || data.submissionStatus === 'CREATED' && !runUuid.test(data.run?.runId)) {
    throw new Error('접수 결과를 확인할 수 없습니다. 접수 상태를 다시 조회해 주세요.')
  }
  return data
}

export async function submitOfficeWork(body, key) {
  const data = await json('/api/v1/runs', { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key }, body: JSON.stringify(body) })
  return validateReceipt(data, key)
}

export async function lookupOfficeWork(key) {
  if (!runUuid.test(key)) throw new Error('접수 식별자가 올바르지 않습니다.')
  return validateReceipt(await json('/api/v1/submissions/' + key), key)
}
