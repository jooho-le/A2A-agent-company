import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { rolldown } from 'rolldown'
import { fileURLToPath } from 'node:url'
import { getProjects } from '../src/api/projects.js'
import { savedProjectCards } from '../src/components/atelier/agentRunProject.js'

// 해당 View만 JSX 변환하여 Node에서 표시 내용을 검사합니다. 서버/브라우저/3D는 실행하지 않습니다.
const bundle = await rolldown({
  input: fileURLToPath(new URL('../src/components/atelier/AtelierViews.jsx', import.meta.url)),
  transform: { jsx: 'react-jsx' },
  plugins: [{ name: 'isolated-project-list', resolveId(source) {
    if (source === 'react' || source.startsWith('react/')) return { id: import.meta.resolve(source), external: true }
    if (source.endsWith('.css')) return '\0style'
  }, load(id) { if (id === '\0style') return '' } }],
})
const { output } = await bundle.generate({ format: 'esm' })
await bundle.close()
const { ProjectsView } = await import(`data:text/javascript;base64,${Buffer.from(output.find(item => item.type === 'chunk').code).toString('base64')}`)

const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const token = 'mock-project-list-token'
const row = { projectId: id(1), name: '회원가입 프로젝트', scenarioId: id(2),
  createdAt: '2026-11-13T09:00:00', updatedAt: '2026-11-13T09:00:00' }
function storage(t, value) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true, value: { getItem(key) {
    assert.equal(key, 'access_token'); return typeof value === 'function' ? value() : value
  } } })
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'sessionStorage', previous)
    else delete globalThis.sessionStorage
  })
}
function view(projects, saved, extra = {}) {
  return renderToStaticMarkup(createElement(ProjectsView, { projects, saved, actual: true,
    search: '', onSearch() {}, onSelect() {}, onLogin() {}, ...extra }))
}

test('로그인 사용자: 제품 8000 GET과 JWT, 실제 프로젝트 공개 필드 및 현재 Run 카드 표시', async t => {
  storage(t, token)
  let calls = 0
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++
    assert.equal(url, 'http://127.0.0.1:8000/api/projects')
    assert.equal(options.method, 'GET')
    assert.equal(options.headers.Authorization, `Bearer ${token}`)
    assert.equal(options.redirect, 'error')
    return Response.json({ projects: [{ ...row, internal: 'must-not-copy' }] })
  })
  const rows = await getProjects()
  assert.deepEqual(rows, [row])
  const cards = savedProjectCards(rows)
  const html = view([...cards, { actual: true, id: id(3), name: '현재 접수한 업무', description: '기존 Run',
    status: { text: '검증 중', kind: 'active' }, agents: [], notice: '이 화면에서 접수', time: '현재' }], { status: 'success' })
  for (const text of [row.name, row.projectId, row.scenarioId, '2026-11-13', '현재 접수한 업무']) assert.ok(html.includes(text))
  assert.match(html, /<article class="project-card"/)
  assert.match(html, /<button[^>]+class="project-card"/)
  assert.ok(!html.includes('progress-track') && !html.includes(token) && !html.includes('must-not-copy'))
  assert.equal(cards[0].runId, undefined)
  assert.equal(cards[0].agents.length, 0)
  assert.equal(calls, 1)
})

test('빈 목록: 기존 빈 상태 표시, 로딩 중에는 빈 목록으로 단정하지 않음', async t => {
  storage(t, token)
  t.mock.method(globalThis, 'fetch', async () => Response.json({ projects: [] }))
  const rows = await getProjects()
  const html = view(savedProjectCards(rows), { status: 'success' })
  assert.match(html, /empty-state/)
  assert.match(html, /저장된 프로젝트가 없습니다/)
  assert.ok(!html.includes('class="project-card"'))
  const loading = view([], { status: 'loading' })
  assert.match(loading, /role="status"/)
  assert.ok(!loading.includes('empty-state'))
})

test('JWT 누락·401: 익명 요청 차단, 안전한 로그인 안내와 다른 세션 응답 폐기', async t => {
  let currentToken = null, calls = 0, bodyReads = 0
  storage(t, () => currentToken)
  t.mock.method(globalThis, 'fetch', async () => {
    calls++
    return { status: 401, ok: false, json() { bodyReads++; return { detail: token } } }
  })
  await assert.rejects(getProjects(), { code: 'AUTH_REQUIRED', status: 401 })
  assert.equal(calls, 0)
  currentToken = token
  let failure
  await assert.rejects(getProjects(), error => { failure = error; return error.code === 'AUTH_REQUIRED' && error.status === 401 })
  const html = view([], { status: 'error', error: failure })
  assert.match(html, /role="alert"/)
  assert.match(html, />로그인<\/button>/)
  assert.ok(!html.includes(token) && !html.includes('empty-state'))
  assert.equal(bodyReads, 0)
  t.mock.method(globalThis, 'fetch', async () => {
    currentToken = 'different-mock-session'
    return Response.json({ projects: [row] })
  })
  await assert.rejects(getProjects(), { code: 'AUTH_REQUIRED' })
})
