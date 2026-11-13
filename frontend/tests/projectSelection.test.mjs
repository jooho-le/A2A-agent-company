import assert from 'node:assert/strict'
import test from 'node:test'
import { clearProjectSelection, loadSelectedProject, readProjectSelection } from '../src/hooks/projectSelection.js'

const key = 'ai-company.selected-project-id'
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const project = { projectId: id(1), name: '저장된 회원가입 프로젝트', scenarioId: id(2),
  createdAt: '2026-11-13T09:00:00', updatedAt: '2026-11-13T09:00:00',
  runs: [{ hostKey: 'local-multi', runId: id(3), configurationId: 'approved-v1', linkedAt: '2026-11-13T10:00:00' }] }

function storage(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  const data = new Map([['access_token', 'mock-user-a-token']])
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: {
    getItem: key => data.get(key) ?? null,
    setItem: (key, value) => data.set(key, String(value)),
    removeItem: key => data.delete(key),
  } })
  clearProjectSelection()
  t.after(() => {
    clearProjectSelection()
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  })
  return data
}

test('선택 후 새로고침: ID만 저장하고 JWT 상세 재조회 결과로 이름·Run 이력 복원', async t => {
  const data = storage(t)
  const requests = []
  let response = project
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    requests.push(url)
    assert.equal(url, `http://127.0.0.1:8000/api/projects/${project.projectId}`)
    assert.equal(options.method, 'GET')
    assert.equal(options.headers.Authorization, 'Bearer mock-user-a-token')
    return Response.json(response)
  })
  assert.equal(readProjectSelection(), null)
  assert.deepEqual(await loadSelectedProject(project.projectId), project)
  assert.deepEqual([...data.entries()], [['access_token', 'mock-user-a-token'], [key, project.projectId]])

  // 메모리 상태 없이 같은 sessionStorage만 남은 새 모듈로 재접속을 모사합니다.
  const reloaded = await import('../src/hooks/projectSelection.js?reload-test')
  response = { ...project, name: '서버에서 수정한 프로젝트 이름', runs: [...project.runs,
    { ...project.runs[0], runId: id(4), configurationId: 'approved-v2' }] }
  const candidate = reloaded.readProjectSelection()
  assert.equal(candidate, project.projectId)
  assert.equal(requests.length, 1) // ID 읽기는 인증 성공을 가정하거나 Host를 조회하지 않습니다.
  assert.deepEqual(await reloaded.loadSelectedProject(candidate), response)
  assert.equal(requests.length, 2)
  assert.equal(data.size, 2)
})

test('로그아웃·새 로그인: 이전 선택 정리 및 늦게 도착한 이전 계정 응답 차단', async t => {
  const data = storage(t)
  let release
  t.mock.method(globalThis, 'fetch', () => new Promise(resolve => { release = resolve }))
  data.set(key, project.projectId)
  const pending = loadSelectedProject(project.projectId)
  // App의 로그아웃과 같은 순서입니다.
  clearProjectSelection()
  data.delete('access_token')
  assert.equal(readProjectSelection(), null)
  // LoginPage가 토큰을 저장한 뒤 App의 로그인 성공 콜백이 선택을 정리합니다.
  data.set('access_token', 'mock-user-b-token')
  clearProjectSelection()
  assert.equal(readProjectSelection(), null)
  release(Response.json(project))
  await assert.rejects(pending, { code: 'AUTH_REQUIRED' })
  assert.equal(data.has(key), false)

  // 재로그인 토큰 값이 같더라도 이전 요청은 선택을 다시 저장하지 못합니다.
  const sameTokenPending = loadSelectedProject(project.projectId)
  clearProjectSelection()
  release(Response.json(project))
  await assert.rejects(sameTokenPending, { name: 'AbortError' })
  assert.equal(data.has(key), false)
})

test('JWT 누락·401·404: 상세 복원 거부 및 잘못된 저장 선택 삭제', async t => {
  const data = storage(t)
  let calls = 0, status = 401, bodyReads = 0
  t.mock.method(globalThis, 'fetch', async () => {
    calls++
    return { status, ok: false, json() { bodyReads++; return project } }
  })
  data.set(key, project.projectId)
  data.delete('access_token')
  assert.equal(readProjectSelection(), null)
  assert.equal(data.has(key), false)
  await assert.rejects(loadSelectedProject(project.projectId), { code: 'AUTH_REQUIRED' })
  assert.equal(calls, 0)
  data.set('access_token', 'mock-user-a-token')
  for (const [httpStatus, code] of [[401, 'AUTH_REQUIRED'], [404, 'NOT_FOUND']]) {
    status = httpStatus
    data.set(key, project.projectId)
    await assert.rejects(loadSelectedProject(readProjectSelection()), { code, status: httpStatus })
    assert.equal(data.has(key), false)
    assert.equal(readProjectSelection(), null)
  }
  assert.equal(calls, 2)
  assert.equal(bodyReads, 0)
})
