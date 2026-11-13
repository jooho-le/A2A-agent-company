import { loginRequired } from './productRunRequest.js'
import { runUuid } from './agentRunSubmission.js'

// 기존 제품 로그인과 같은 주소입니다. /api/v1 Orchestrator 프록시는 사용하지 않습니다.
const projectsUrl = 'http://127.0.0.1:8000/api/projects'

function projectError(message, code, status = null) {
  return Object.assign(new Error(message), { name: 'ProjectApiError', code, status })
}

function accessToken() {
  try { return globalThis.sessionStorage.getItem('access_token') } catch { return null }
}

export async function getProjects({ signal } = {}) {
  signal?.throwIfAborted()
  const token = accessToken()
  if (typeof token !== 'string' || !token.trim()) throw loginRequired()
  let response
  try {
    response = await fetch(projectsUrl, { method: 'GET', signal, redirect: 'error',
      headers: { Accept: 'application/json', Authorization: `Bearer ${token}` } })
  } catch (error) {
    signal?.throwIfAborted()
    if (error?.name === 'AbortError') throw new DOMException('요청이 취소되었습니다.', 'AbortError')
    throw projectError('제품 서버에 연결할 수 없습니다. 연결 상태를 확인해 주세요.', 'NETWORK_ERROR')
  }
  signal?.throwIfAborted()
  if (accessToken() !== token || response.status === 401) throw loginRequired()
  if (response.status === 403) throw projectError('프로젝트 목록을 조회할 권한이 없습니다.', 'FORBIDDEN', 403)
  if (response.status === 404) throw projectError('프로젝트 목록 API를 찾을 수 없습니다.', 'NOT_FOUND', 404)
  if (!response.ok) throw projectError('프로젝트 목록을 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.', 'HTTP_ERROR', response.status)
  let data
  try { data = await response.json() } catch {
    signal?.throwIfAborted()
    throw projectError('프로젝트 목록 응답을 확인할 수 없습니다.', 'INVALID_RESPONSE')
  }
  signal?.throwIfAborted()
  if (accessToken() !== token) throw loginRequired()
  if (!Array.isArray(data?.projects) || data.projects.some(project => !project
    || !runUuid.test(project.projectId) || !runUuid.test(project.scenarioId)
    || typeof project.name !== 'string' || !project.name.trim()
    || typeof project.createdAt !== 'string' || !Number.isFinite(Date.parse(project.createdAt))
    || typeof project.updatedAt !== 'string' || !Number.isFinite(Date.parse(project.updatedAt)))
    || new Set(data.projects.map(project => project.projectId)).size !== data.projects.length) {
    throw projectError('프로젝트 목록 응답을 확인할 수 없습니다.', 'INVALID_RESPONSE')
  }
  return data.projects.map(({ projectId, name, scenarioId, createdAt, updatedAt }) => ({ projectId, name, scenarioId, createdAt, updatedAt }))
}
