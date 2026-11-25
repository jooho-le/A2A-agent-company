import { runApiTransport, loginRequired, ProductRunRequestError } from './productRunRequest.js'
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
  return { data, status: response.status }
}

export const officeScenarioId = '03d94bc5-1a31-473b-8527-c4acf8ea58a1'
// Old experiment receipts remain recoverable; new product requests use SERVICE-WEB.
const receiptScenarios = new Set([officeScenarioId, 'f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76'])

export async function loadOfficeApproval(signal) {
  const scenarioId = officeScenarioId
  const { data: approved } = await json('/api/v1/host/approved-configuration?' + new URLSearchParams({ scenarioId }), { signal })
  if (approved.scenarioId !== scenarioId || !/^[0-9a-f]{64}$/.test(approved.configurationId)
    || approved.configuration?.architecture !== 'MULTI_AGENT') throw new Error('승인된 실행 설정을 확인할 수 없습니다.')
  return { scenarioId, configurationId: approved.configurationId }
}

function validateReceipt(data, key, status, expectedScenarioId = null) {
  if (data?.requestId !== key || !runUuid.test(data.projectId)
    || !['PENDING', 'UNKNOWN', 'REJECTED', 'CREATED'].includes(data.submissionStatus)
    || ![200, 201, 202].includes(status)
    || status === 201 && data.submissionStatus !== 'CREATED'
    || status === 202 && data.submissionStatus === 'CREATED'
    || data.submissionStatus === 'CREATED' && (!runUuid.test(data.run?.runId)
      || !receiptScenarios.has(data.run.scenarioId)
      || expectedScenarioId != null && data.run.scenarioId !== expectedScenarioId
      || !['SCHEDULED', 'NOT_CONFIGURED'].includes(data.dispatchStatus)
      || data.firstStep?.runId != null && data.firstStep.runId !== data.run.runId)) {
    throw new Error('접수 결과를 확인할 수 없습니다. 접수 상태를 다시 조회해 주세요.')
  }
  const receipt = { requestId: data.requestId, projectId: data.projectId, submissionStatus: data.submissionStatus, httpStatus: status }
  // UNKNOWN에 Run 형태의 데이터가 섞여 있어도 UI/3D에 연결하지 않습니다.
  return data.submissionStatus === 'CREATED'
    ? { ...receipt, run: data.run, firstStep: data.firstStep, dispatchStatus: data.dispatchStatus } : receipt
}

export async function submitOfficeWork(body, key, { confirmed = false } = {}) {
  if (!body || Object.keys(body).some(field => !['projectName', 'projectId', 'scenarioId', 'configurationId', 'requestText'].includes(field))
    || !runUuid.test(key) || body.scenarioId !== officeScenarioId || !/^[0-9a-f]{64}$/.test(body.configurationId)
    || body.projectId != null && !runUuid.test(body.projectId)
    || typeof body.projectName !== 'string' || !body.projectName.trim() || body.projectName.length > 60
    || typeof body.requestText !== 'string' || !body.requestText.trim() || body.requestText.length > 20000) {
    throw new ProductRunRequestError('업무 요청과 승인 설정 식별자를 확인해 주세요.', 'INVALID_ARGUMENT')
  }
  const { projectName, projectId, scenarioId, configurationId, requestText } = body
  const { data, status } = await json('/api/v1/runs', { method: 'POST', confirmed,
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
    body: JSON.stringify({ projectName, ...(projectId == null ? {} : { projectId }), scenarioId, configurationId, requestText }) })
  return validateReceipt(data, key, status, scenarioId)
}

export async function lookupOfficeWork(key) {
  if (!runUuid.test(key)) throw new Error('접수 식별자가 올바르지 않습니다.')
  const { data, status } = await json('/api/v1/submissions/' + key)
  return validateReceipt(data, key, status)
}

export async function retireOfficeRequest(key, { confirmed = false } = {}) {
  if (!runUuid.test(key) || confirmed !== true) {
    throw new ProductRunRequestError('이전 요청 정리 버튼을 눌러 확인해 주세요.', 'INVALID_ARGUMENT')
  }
  const { data, status } = await json('/api/v1/submissions/' + key + '/retire', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
  })
  if (status !== 200 || data?.requestId !== key || !['RETIRED', 'EXISTING'].includes(data.outcome)) {
    throw new Error('이전 요청의 처리 상태를 확인하지 못했어요. 접수 상태를 다시 확인해 주세요.')
  }
  return data.outcome === 'EXISTING'
    ? { outcome: data.outcome, submission: validateReceipt(data.submission, key, 200) }
    : { outcome: data.outcome }
}

const sourceDownloads = new Set()
const maxSourceBytes = 20 * 1024 * 1024

// 실제 목록에 등록된 SOURCE의 두 ID만 사용합니다. URI와 Host 주소를 받지 않습니다.
export async function downloadOfficeSource(runId, artifactId, codeVersion, { signal } = {}) {
  if (!runUuid.test(runId) || !runUuid.test(artifactId) || !Number.isSafeInteger(codeVersion) || codeVersion < 1) {
    throw new ProductRunRequestError('다운로드할 Source 정보를 확인할 수 없습니다.', 'INVALID_ARGUMENT')
  }
  const key = `${runId}:${artifactId}`
  if (sourceDownloads.has(key)) throw new ProductRunRequestError('이미 다운로드 중입니다.', 'DOWNLOAD_PENDING')
  sourceDownloads.add(key)
  let reader
  try {
    const response = await runApiTransport.fetch(`/api/v1/runs/${runId}/artifacts/${artifactId}/download`, {
      signal, cache: 'no-store', headers: { Accept: 'application/zip' },
    })
    const headers = response.headers
    const length = headers.get('Content-Length') || ''
    const digest = headers.get('X-Archive-Sha256') || ''
    if (response.status !== 200 || headers.get('Content-Type')?.split(';')[0].trim() !== 'application/zip'
      || headers.get('X-Source-Artifact-Id') !== artifactId || headers.get('X-Code-Version') !== String(codeVersion)
      || !/^[0-9]{1,8}$/.test(length) || Number(length) < 22 || Number(length) > maxSourceBytes
      || !/^[0-9a-f]{64}$/.test(digest) || !/^[0-9a-f]{64}$/.test(headers.get('X-Snapshot-Sha256') || '')) {
      await response.body?.cancel()
      throw new Error('invalid archive')
    }
    reader = response.body.getReader()
    const chunks = []
    let size = 0
    for (;;) {
      const { done, value } = await reader.read()
      signal?.throwIfAborted()
      if (done) break
      size += value.byteLength
      if (size > Number(length)) throw new Error('invalid archive size')
      chunks.push(value)
    }
    const blob = new Blob(chunks, { type: 'application/zip' })
    const bytes = await blob.arrayBuffer()
    const prefix = new Uint8Array(bytes, 0, Math.min(4, size))
    const checksum = Array.from(new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes)),
      byte => byte.toString(16).padStart(2, '0')).join('')
    if (size !== Number(length) || checksum !== digest || prefix[0] !== 80 || prefix[1] !== 75
      || !(prefix[2] === 3 && prefix[3] === 4 || prefix[2] === 5 && prefix[3] === 6)) throw new Error('invalid archive')
    signal?.throwIfAborted()
    // 서버의 임의 파일명은 사용하지 않습니다. Host의 고정 파일명 계약만 재구성합니다.
    return { blob, filename: `source-${runId}-v${codeVersion}.zip` }
  } catch (error) {
    if (signal?.aborted) throw new DOMException('다운로드가 취소되었습니다.', 'AbortError')
    const message = ({ 401: '로그인이 필요합니다. 기존 로그인 화면에서 로그인해 주세요.',
      403: '이 결과물에 접근할 권한이 없습니다.', 404: 'Source가 아직 준비되지 않았거나 접근할 수 없습니다.',
      409: 'Source 안전성 검증으로 다운로드가 차단되었습니다.', 413: 'Source ZIP이 허용 용량을 초과했습니다.',
      502: 'Host 서비스 인증 또는 연결을 확인해 주세요.', 503: '다운로드 서버가 준비되지 않았습니다. 잠시 후 다시 시도해 주세요.' })[error.status]
    throw new ProductRunRequestError(message || 'Source ZIP을 확인하지 못했습니다. 다시 시도해 주세요.', 'DOWNLOAD_FAILED', error.status ?? null)
  } finally {
    try { await reader?.cancel() } catch { /* 전송 종료/취소 후 원본 오류는 노출하지 않습니다. */ }
    reader?.releaseLock()
    sourceDownloads.delete(key)
  }
}
