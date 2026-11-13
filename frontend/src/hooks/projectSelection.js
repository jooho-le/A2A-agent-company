import { getProject } from '../api/projects.js'
import { runUuid } from '../api/agentRunSubmission.js'
import { hasStoredAccessToken } from '../api/productRunRequest.js'

const selectionKey = 'ai-company.selected-project-id'
let revision = 0

// 로그인 성공/로그아웃 시 호출합니다. 진행 중인 이전 세션 조회도 무효화합니다.
export function clearProjectSelection() {
  revision++
  try { globalThis.sessionStorage.removeItem(selectionKey) } catch { /* 저장소 사용 불가 */ }
}

// 반환값은 복원 후보일 뿐입니다. 이름·이력·접근 권한은 항상 API로 확인합니다.
export function readProjectSelection() {
  if (!hasStoredAccessToken()) { clearProjectSelection(); return null }
  try {
    const projectId = globalThis.sessionStorage.getItem(selectionKey)
    if (typeof projectId === 'string' && runUuid.test(projectId)) return projectId
    clearProjectSelection()
  } catch { /* 저장소 사용 불가 시 선택 복원만 생략합니다. */ }
  return null
}

export async function loadSelectedProject(projectId, { signal } = {}) {
  const requestRevision = ++revision
  try {
    const project = await getProject(projectId, { signal })
    signal?.throwIfAborted()
    if (requestRevision !== revision) throw new DOMException('요청이 취소되었습니다.', 'AbortError')
    // 서버 인증·소유권 검사를 통과한 ID만 저장합니다. Run ID나 JWT를 복사하지 않습니다.
    try { globalThis.sessionStorage.setItem(selectionKey, project.projectId) } catch { /* 상세 조회는 계속 표시 */ }
    return project
  } catch (error) {
    if (requestRevision === revision && ['AUTH_REQUIRED', 'FORBIDDEN', 'NOT_FOUND', 'INVALID_ARGUMENT'].includes(error.code)) {
      clearProjectSelection()
    }
    throw error
  }
}
