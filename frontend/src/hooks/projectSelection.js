import { getProject } from '../api/projects.js'
import { runUuid } from '../api/agentRunSubmission.js'
import { hasStoredAccessToken } from '../api/productRunRequest.js'
import { pendingOfficeSubmissionKey } from './pendingOfficeSubmission.js'

const selectionKey = 'ai-company.selected-project-id'
const runSelectionKey = 'ai-company.selected-project-run'
let revision = 0

// 로그인 성공/로그아웃 시 호출합니다. 진행 중인 이전 세션 조회도 무효화합니다.
export function clearProjectSelection() {
  revision++
  try {
    globalThis.sessionStorage.removeItem(selectionKey)
    globalThis.sessionStorage.removeItem(runSelectionKey)
  } catch { /* 저장소 사용 불가 */ }
}

function readRunCandidate() {
  try {
    const saved = JSON.parse(globalThis.sessionStorage.getItem(runSelectionKey))
    const owner = pendingOfficeSubmissionKey()
    if (owner && saved?.owner === owner && runUuid.test(saved.projectId) && runUuid.test(saved.runId)
        && saved.hostKey === 'local-multi') return saved
  } catch { /* An untrusted or malformed hint never selects a Run. */ }
  return null
}

function rememberRunHint(projectId, runId, hostKey) {
  if (!hasStoredAccessToken() || !runUuid.test(projectId) || !runUuid.test(runId) || hostKey !== 'local-multi') return false
  const owner = pendingOfficeSubmissionKey()
  try {
    globalThis.sessionStorage.setItem(selectionKey, projectId)
    if (owner) globalThis.sessionStorage.setItem(runSelectionKey, JSON.stringify({
      owner, projectId, runId, hostKey,
    }))
  } catch { /* The current selection still works without storage. */ }
  return true
}

export function rememberProjectRun(project, run) {
  if (!project.runs.some(item => item.runId === run.runId && item.hostKey === run.hostKey)) return false
  return rememberRunHint(project.projectId, run.runId, run.hostKey)
}

// An authenticated CREATED receipt supplies a restore hint immediately, before
// the optional project-name GET finishes. Membership is checked again on reload.
export function rememberCreatedRun(receipt) {
  if (receipt.submissionStatus !== 'CREATED') return false
  return rememberRunHint(receipt.projectId, receipt.run?.runId, 'local-multi')
}

// The fresh, authenticated project response is the authority for membership.
// A missing selected Run must not silently select a different Run.
export function resolveProjectRun(project) {
  const saved = readRunCandidate()
  if (saved?.projectId === project.projectId) {
    return project.runs.find(item => item.runId === saved.runId && item.hostKey === saved.hostKey) || null
  }
  return project.runs.find(item => item.hostKey === 'local-multi') || null
}

// 반환값은 복원 후보일 뿐입니다. 이름·이력·접근 권한은 항상 API로 확인합니다.
export function readProjectSelection() {
  if (!hasStoredAccessToken()) { clearProjectSelection(); return null }
  const run = readRunCandidate()
  if (run) return run.projectId
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
