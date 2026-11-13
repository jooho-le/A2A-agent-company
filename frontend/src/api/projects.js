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

async function requestProjectJson(url, signal, detail = false) {
  const label = detail ? '프로젝트 상세' : '프로젝트 목록'
  signal?.throwIfAborted()
  const token = accessToken()
  if (typeof token !== 'string' || !token.trim()) throw loginRequired()
  let response
  try {
    response = await fetch(url, { method: 'GET', signal, redirect: 'error',
      headers: { Accept: 'application/json', Authorization: `Bearer ${token}` } })
  } catch (error) {
    signal?.throwIfAborted()
    if (error?.name === 'AbortError') throw new DOMException('요청이 취소되었습니다.', 'AbortError')
    throw projectError('제품 서버에 연결할 수 없습니다. 연결 상태를 확인해 주세요.', 'NETWORK_ERROR')
  }
  signal?.throwIfAborted()
  if (accessToken() !== token || response.status === 401) throw loginRequired()
  if (response.status === 403) throw projectError(`${label}를 조회할 권한이 없습니다.`, 'FORBIDDEN', 403)
  if (response.status === 404) throw projectError(detail ? '프로젝트를 찾을 수 없습니다.' : '프로젝트 목록 API를 찾을 수 없습니다.', 'NOT_FOUND', 404)
  if (!response.ok) throw projectError(`${label}를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.`, 'HTTP_ERROR', response.status)
  let data
  try { data = await response.json() } catch {
    signal?.throwIfAborted()
    throw projectError(`${label} 응답을 확인할 수 없습니다.`, 'INVALID_RESPONSE')
  }
  signal?.throwIfAborted()
  if (accessToken() !== token) throw loginRequired()
  return data
}

const validDate = value => typeof value === 'string' && Number.isFinite(Date.parse(value))
const validProject = project => project && runUuid.test(project.projectId) && runUuid.test(project.scenarioId)
  && typeof project.name === 'string' && Boolean(project.name.trim())
  && validDate(project.createdAt) && validDate(project.updatedAt)
const publicProject = ({ projectId, name, scenarioId, createdAt, updatedAt }) => ({ projectId, name, scenarioId, createdAt, updatedAt })

export async function getProjects({ signal } = {}) {
  const data = await requestProjectJson(projectsUrl, signal)
  if (!Array.isArray(data?.projects) || !data.projects.every(validProject)
    || new Set(data.projects.map(project => project.projectId)).size !== data.projects.length) {
    throw projectError('프로젝트 목록 응답을 확인할 수 없습니다.', 'INVALID_RESPONSE')
  }
  return data.projects.map(publicProject)
}

export async function getProject(projectId, { signal } = {}) {
  if (typeof projectId !== 'string' || !runUuid.test(projectId)) {
    throw projectError('프로젝트 ID를 확인할 수 없습니다.', 'INVALID_ARGUMENT')
  }
  const data = await requestProjectJson(`${projectsUrl}/${encodeURIComponent(projectId)}`, signal, true)
  if (!validProject(data) || data.projectId !== projectId || !Array.isArray(data.runs)
    || data.runs.some(run => !run || !runUuid.test(run.runId)
      || typeof run.hostKey !== 'string' || !run.hostKey.trim()
      || typeof run.configurationId !== 'string' || !run.configurationId.trim()
      || !validDate(run.linkedAt))
    || new Set(data.runs.map(run => JSON.stringify([run.hostKey, run.runId]))).size !== data.runs.length) {
    throw projectError('프로젝트 상세 응답을 확인할 수 없습니다.', 'INVALID_RESPONSE')
  }
  // 연결 시각순의 서버 배열을 그대로 보존합니다. Host 상태·원본 설정은 복사하지 않습니다.
  return { ...publicProject(data), runs: data.runs.map(({ hostKey, runId, configurationId, linkedAt }) =>
    ({ hostKey, runId, configurationId, linkedAt })) }
}
