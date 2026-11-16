// 계정별 복원 후보 키만 만듭니다. JWT 인증·소유권 판단은 제품 API가 수행합니다.
// 토큰/이메일/요청 본문을 추가 저장하지 않고 요청 UUID만 보관합니다.
export function pendingOfficeSubmissionKey() {
  try {
    const token = globalThis.sessionStorage.getItem('access_token')
    const encoded = token.split('.')[1].replaceAll('-', '+').replaceAll('_', '/')
    const { sub } = JSON.parse(globalThis.atob(encoded))
    return typeof sub === 'string' && /^[1-9][0-9]*$/.test(sub)
      ? 'ai-company.pending-office-submission.' + sub : null
  } catch { return null }
}
