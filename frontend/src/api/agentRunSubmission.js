// 승인된 공개 RunConfiguration만 받습니다. Host 파일·키·환경변수·사용자 JSON 입력은 받지 않습니다.
export const runUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const secret = /api[ _-]?key|access[ _-]?key|secret|token|password|passwd|authorization|cookie|bearer|비밀번호|토큰|BEGIN .*PRIVATE KEY|\bsk-[\w-]+|\bAKIA[0-9A-Z]+|eyJ[\w-]+\.[\w-]+\.[\w-]+/i
const text = value => typeof value === 'string' && value.trim().length > 0 && value.length <= 200
  && !secret.test(value) && !/[\p{Cc}]/u.test(value)
const nullableText = value => value == null || text(value)
const fields = (value, allowed) => value && Object.getPrototypeOf(value) === Object.prototype
  && Object.keys(value).every(key => allowed.includes(key))
const fail = () => { throw new Error('승인된 공개 실행 설정을 확인할 수 없어 요청을 보내지 않았습니다.') }

// 운영 승인 자체는 Host가 최종 대조합니다. 여기서는 누락·비밀 필드·잘못된 공개 계약을 차단합니다.
export function publicRunConfiguration(value) {
  if (!fields(value, ['architecture', 'startingCommitHash', 'startingSnapshotSha256', 'model', 'limits', 'environment',
    'protectedTestSuiteRef', 'scannerProfileRef', 'experimentId', 'warmupState', 'executionOrder'])
    || value.architecture !== 'MULTI_AGENT') fail()
  const { model, limits, environment } = value
  if (!fields(model, ['provider', 'modelId', 'modelRevision', 'temperature', 'seed'])
    || model.provider !== 'openai' || !text(model.modelId) || !nullableText(model.modelRevision)
    || !Number.isFinite(model.temperature) || model.temperature < 0 || model.temperature > 2 || model.seed != null) fail()
  if (!fields(limits, ['maxFixAttempts', 'maxToolRetries', 'runtimeBudgetMs'])
    || limits.maxFixAttempts !== 3 || limits.maxToolRetries !== 2
    || !Number.isSafeInteger(limits.runtimeBudgetMs) || limits.runtimeBudgetMs <= 0) fail()
  if (!fields(environment, ['containerImageDigest', 'dependencyLockHash', 'hardwareProfile', 'networkPolicy', 'allowedHosts'])
    || !/^sha256:[0-9a-f]{64}$/.test(environment.containerImageDigest)
    || !/^sha256:[0-9a-f]{64}$/.test(environment.dependencyLockHash) || !text(environment.hardwareProfile)
    // Owned Host의 baseline은 DENY로 고정되어 있습니다.
    || environment.networkPolicy !== 'DENY' || !Array.isArray(environment.allowedHosts) || environment.allowedHosts.length) fail()
  if (!/^(?:[0-9a-f]{40}|[0-9a-f]{64})$/.test(value.startingCommitHash)
    || !/^[0-9a-f]{64}$/.test(value.startingSnapshotSha256)
    || !nullableText(value.protectedTestSuiteRef) || !nullableText(value.scannerProfileRef)
    || !nullableText(value.warmupState)
    || value.experimentId != null && !runUuid.test(value.experimentId)
    || value.executionOrder != null && (!Number.isSafeInteger(value.executionOrder) || value.executionOrder < 0)) fail()
  if (value.experimentId != null && (value.scannerProfileRef == null || value.warmupState == null || value.executionOrder == null)) fail()
  return JSON.parse(JSON.stringify(value))
}

export function approvedRunSubmission(approval) {
  if (!fields(approval, ['approved', 'scenarioId', 'configuration']) || approval.approved !== true
    || !runUuid.test(approval.scenarioId)) fail()
  return { scenarioId: approval.scenarioId, configuration: publicRunConfiguration(approval.configuration) }
}

export const dispatchMessage = status => status === 'SCHEDULED'
  ? 'Run이 생성되어 실행 예약되었습니다. 작업 완료나 성공 판정은 아직 확인되지 않았습니다.'
  : 'Run은 생성되었지만 실행기가 연결되지 않았습니다(NOT_CONFIGURED). 새로 생성하지 말고 Host 연결을 확인하세요.'

// UI와 Mock 검증이 같은 제출 경로를 사용합니다. 진행 중·생성 완료·결과 불명 상태에서 재전송하지 않습니다.
export function createRunSubmission({ listScenarios, postRun, onCreated }) {
  let pending = null, result = null, uncertain = false, disposed = false
  function submit({ approval, requestText, confirmed }) {
    if (disposed || uncertain) return Promise.reject(new Error('생성 결과를 확인할 수 없습니다. 기존 Run을 확인하기 전에는 다시 요청하지 마세요.'))
    if (pending) return pending
    if (result) return Promise.resolve(result)
    let selection
    try {
      selection = approvedRunSubmission(approval)
      if (confirmed !== true || typeof requestText !== 'string' || !requestText.trim()) throw new Error('실제 실행 선택과 요청 내용을 확인해주세요.')
    } catch (error) { return Promise.reject(error) }
    let sent = false
    pending = Promise.resolve().then(async () => {
      try {
        const scenarios = await listScenarios()
        if (disposed) throw new Error()
        if (!scenarios.some(item => item.scenarioId === selection.scenarioId)) throw new Error('등록된 시나리오를 확인할 수 없습니다.')
        sent = true
        result = await postRun(selection.scenarioId, requestText.trim(), 'MULTI_AGENT', {
          configuration: selection.configuration, approved: true,
        })
      } catch (error) {
        // POST 응답 유실/5xx는 생성 여부가 불명확합니다. 자동·반복 제출을 막습니다.
        uncertain = sent && ![400, 401, 403, 404, 405, 413, 415, 422, 429].includes(error.status)
        // eslint-disable-next-line preserve-caught-error -- 서버 응답·인증정보를 포함할 수 있는 원본 오류를 UI에 보존하지 않습니다.
        throw new Error(uncertain ? '생성 결과를 확인할 수 없습니다. 중복 생성 방지를 위해 재전송을 막았습니다. Run 기록을 확인해주세요.'
          : sent ? 'Run 생성 요청이 거절되었습니다. 승인 설정과 서버 연결을 확인해주세요.'
            : '승인된 시나리오를 조회하지 못했습니다. 기존 화면은 유지됩니다.')
      } finally { pending = null }
      if (!disposed) onCreated(result)
      return result
    })
    return pending
  }
  return { submit, blocked: () => uncertain || Boolean(result), dispose: () => { disposed = true } }
}
