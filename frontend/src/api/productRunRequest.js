// Vite의 /api/v1 대상이 제품 8000으로 전환되고 계약이 준비된 뒤 별도 변경합니다.
// 브라우저 저장소·요청 옵션·환경변수로 활성화하지 않습니다.
const PRODUCT_RUN_API_READY = false

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

export function loginRequired() {
  return new ProductRunRequestError('로그인이 필요합니다. 기존 로그인 화면에서 로그인해 주세요.', 'AUTH_REQUIRED', 401)
}

// 주입 인자는 코드/Mock 테스트 전용입니다. UI에서 전송 대상이나 인증 모드를 받지 않습니다.
export function createProductRunFetch({
  productApiReady = false,
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
    let response
    try {
      const headers = new Headers(options.headers)
      headers.set('Authorization', `Bearer ${token}`)
      response = await fetchImpl(path, { ...options, headers, redirect: 'error' })
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
      throw new ProductRunRequestError('제품 API 요청을 처리할 수 없습니다.', 'HTTP_ERROR', response.status)
    }
    return response
  }
}

const protectedFetch = createProductRunFetch({ productApiReady: PRODUCT_RUN_API_READY })

export const runApiTransport = {
  fetch(path, options) {
    // 기존 8001 경로에는 sessionStorage를 읽거나 제품 JWT를 추가하지 않습니다.
    // 제품 전환 시에만 보호 전송기를 사용하며 익명 요청으로 fallback하지 않습니다.
    return PRODUCT_RUN_API_READY ? protectedFetch(path, options) : globalThis.fetch(path, options)
  },
}
