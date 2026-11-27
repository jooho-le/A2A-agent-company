// 브라우저는 제품 API만 호출하며 Host 서비스 토큰을 보유하지 않습니다.
// 비밀정보가 아닌 개발 연결 스위치입니다. Vite 재시작 후 적용되며 기본은 차단입니다.
export const productRunSubmissionEnabled = import.meta.env?.VITE_PRODUCT_RUN_SUBMISSION_ENABLED === 'true'

// 존재 확인만 합니다. JWT 유효성·소유권은 실제 제품 API가 검증합니다.
export function hasStoredAccessToken() {
  try {
    const token = globalThis.sessionStorage.getItem('access_token')
    return typeof token === 'string' && Boolean(token.trim())
  } catch { return false }
}

export class ProductRunRequestError extends Error {
  constructor(message, code, status = null) {
    super(message)
    this.name = 'AgentRunApiError'
    this.code = code
    this.status = status
    this.detail = null
  }
}

export const continuationMessages = {
  CONTINUATION_TARGET_REQUIRED: '이전 오류를 수정하려면 내 프로젝트에서 원래 작업을 선택하고 이어서 수정·재시도를 눌러주세요.',
  CONTINUATION_PARENT_NOT_FOUND: '원래 작업을 찾을 수 없어요. 수정할 프로젝트와 작업을 다시 선택해주세요.',
  CONTINUATION_PARENT_NOT_READY: '원래 작업이 아직 진행 중이거나 이어갈 수 없는 상태예요. 최신 상태를 확인해주세요.',
  CONTINUATION_ALREADY_ACTIVE: '이 작업의 수정 실행이 이미 진행 중이에요. 프로젝트의 최신 실행을 확인해주세요.',
  CONTINUATION_PARENT_CHANGED: '원래 작업의 상태가 바뀌었어요. 최신 상태를 확인한 뒤 다시 선택해주세요.',
  CONTINUATION_BASELINE_CHANGED: '원래 코드와 현재 승인 환경이 달라 이어갈 수 없어요. 실행 환경을 확인해주세요.',
  CONTINUATION_SOURCE_UNAVAILABLE: '저장된 코드를 안전하게 불러오지 못했어요. 기존 기록은 보존돼요. 실행 환경 확인이 필요해요.',
  CONTINUATION_SOURCE_INVALID: '저장된 코드가 승인 기준과 일치하지 않아요. 기존 기록과 실행 환경을 확인해주세요.',
  CONTINUATION_CONTEXT_TOO_LARGE: '이전 검사 기록이 전달 한도를 넘었어요. 실행 기록 확인이 필요해요.',
  CONTINUATION_UNSAFE_CONTEXT: '이전 기록을 안전하게 전달하지 못했어요. 기존 작업의 민감정보를 확인해주세요.',
  CONTINUATION_INTEGRITY_FAILED: '저장된 작업의 무결성을 확인하지 못했어요. 실행 기록 확인이 필요해요.',
  CONTINUATION_BINDING_INVALID: '이전 작업 연결 정보를 확인하지 못했어요. 프로젝트에서 원래 작업을 다시 선택해주세요.',
  CONTINUATION_SERVICE_ONLY: '이어서 수정은 서비스 작업에서 이용할 수 있어요.',
}

export function loginRequired() {
  return new ProductRunRequestError('로그인이 필요합니다. 기존 로그인 화면에서 로그인해 주세요.', 'AUTH_REQUIRED', 401)
}

// 주입 인자는 코드/Mock 테스트 전용입니다. UI에서 전송 대상이나 인증 모드를 받지 않습니다.
export function createProductRunFetch({
  productApiReady = false,
  runSubmissionEnabled = productRunSubmissionEnabled,
  readAccessToken = () => globalThis.sessionStorage.getItem('access_token'),
  fetchImpl = (...args) => globalThis.fetch(...args),
} = {}) {
  return async function productRunFetch(path, options = {}) {
    options.signal?.throwIfAborted()
    let token
    try { token = readAccessToken() } catch { throw loginRequired() }
    if (typeof token !== 'string' || !token.trim()) throw loginRequired()
    if (!productApiReady) {
      throw new ProductRunRequestError('제품 API 연결 준비가 완료되지 않았습니다.', 'PRODUCT_API_NOT_READY')
    }
    if (typeof path !== 'string' || !path.startsWith('/api/v1/') || /[\\\r\n#]/.test(path)) {
      throw new ProductRunRequestError('지원하지 않는 API 경로입니다.', 'INVALID_ARGUMENT')
    }
    const method = (options.method || 'GET').toUpperCase()
    const createsRun = path.split('?')[0] === '/api/v1/runs' && method === 'POST'
    const readsSubmission = method === 'GET' && /^\/api\/v1\/submissions\/[^/?]+$/.test(path)
    const readsApproval = path.split('?')[0] === '/api/v1/host/approved-configuration'
    const { confirmed, ...requestOptions } = options
    if ((createsRun || readsApproval) && !runSubmissionEnabled) {
      throw new ProductRunRequestError('실제 업무 요청 연결이 비활성화되어 있습니다.', 'RUN_SUBMISSION_DISABLED')
    }
    if (createsRun && confirmed !== true) {
      throw new ProductRunRequestError('업무 시작을 눌러 실행을 요청해 주세요.', 'EXPLICIT_SUBMISSION_REQUIRED')
    }
    let response
    try {
      const headers = new Headers(options.headers)
      headers.set('Authorization', `Bearer ${token}`)
      response = await fetchImpl(path, { ...requestOptions, headers, redirect: 'error' })
    } catch (error) {
      options.signal?.throwIfAborted()
      if (error?.name === 'AbortError') throw new DOMException('요청이 취소되었습니다.', 'AbortError')
      // 원본 오류에는 요청/인증 헤더가 들어 있을 수 있으므로 cause도 전달하지 않습니다.
      throw new ProductRunRequestError('서버에 연결할 수 없습니다.', 'NETWORK_ERROR')
    }
    options.signal?.throwIfAborted()
    // 인증 실패 본문을 읽거나 재출력하지 않고, 자동 갱신·재전송하지 않습니다.
    if (response.status === 401) throw loginRequired()
    if (!response.ok) {
      // 알려진 충돌 코드만 해석합니다. 원본 오류/설정/인증정보는 보관하지 않습니다.
      let code = 'HTTP_ERROR'
      let message = ({ 403: '이 Run에 접근할 권한이 없습니다.', 404: 'Run을 찾을 수 없거나 접근할 수 없습니다.',
        502: 'Host 서비스 인증 또는 연결에 실패했습니다. 서버 설정을 확인해 주세요.',
        503: '실행 서버가 준비되지 않았습니다. 서버 연결과 승인 설정을 확인해 주세요.' })[response.status]
        || '제품 API 요청을 처리할 수 없습니다.'
      if (response.status === 404 && readsSubmission) {
        code = 'SUBMISSION_NOT_FOUND'
        message = '이전 요청의 접수 기록을 찾지 못했어요. 아래에서 이전 요청을 확인하고 다시 입력할 수 있어요.'
      }
      if ([409, 422].includes(response.status) && createsRun) {
        let detail
        try { detail = (await response.json())?.detail } catch { /* 확인 불가 충돌은 불확실 상태로 유지 */ }
        if (Object.hasOwn(continuationMessages, detail)) {
          code = detail; message = continuationMessages[detail]
        } else if (detail === 'CONFIGURATION_CHANGED') {
          code = detail; message = '승인 설정이 변경되었습니다. 다시 연결한 뒤 업무 시작을 눌러 주세요.'
        } else if (detail === 'IDEMPOTENCY_KEY_CONFLICT' || detail === '같은 요청 식별자로 다른 업무를 접수할 수 없습니다.') {
          code = 'IDEMPOTENCY_KEY_CONFLICT'; message = '동일 접수 키의 요청 내용이 충돌했습니다. 기존 접수 상태를 확인해 주세요.'
        } else if (detail === 'SUBMISSION_KEY_RETIRED') {
          code = detail; message = '이미 정리한 이전 요청이에요. 입력 내용을 확인한 뒤 새로 시작해 주세요.'
        } else {
          message = '요청 충돌 원인을 확인하지 못했습니다. 기존 접수 상태를 확인해 주세요.'
        }
      }
      const error = new ProductRunRequestError(message, code, response.status)
      // 제품 서버가 외부 POST 전 미접수를 확정한 경우만 재입력을 허용합니다.
      error.submissionNotAccepted = createsRun && (code === 'SUBMISSION_KEY_RETIRED'
        || (response.status === 503 || Object.hasOwn(continuationMessages, code))
          && response.headers?.get('X-Submission-Status') === 'NOT_ACCEPTED')
      throw error
    }
    return response
  }
}

const protectedFetch = createProductRunFetch({ productApiReady: true })
export const runApiTransport = { fetch: protectedFetch }
