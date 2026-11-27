import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { rolldown } from 'rolldown'
import { fileURLToPath } from 'node:url'
import { getProject } from '../src/api/projects.js'
import { savedProjectCards } from '../src/components/atelier/agentRunProject.js'

// JSX 표시와 카드 선택만 격리합니다. 서버·Host·3D 런타임은 실행하지 않습니다.
async function component(file) {
  const bundle = await rolldown({
    input: fileURLToPath(new URL(file, import.meta.url)),
    transform: { jsx: 'react-jsx' },
    plugins: [{ name: 'isolated-project-detail', resolveId(source) {
      if (source === 'react' || source.startsWith('react/')) return { id: import.meta.resolve(source), external: true }
      if (source.endsWith('.css')) return '\0style'
    }, load(id) { if (id === '\0style') return '' } }],
  })
  const { output } = await bundle.generate({ format: 'esm' })
  await bundle.close()
  return import(`data:text/javascript;base64,${Buffer.from(output.find(item => item.type === 'chunk').code).toString('base64')}`)
}
const { SavedProjectDetailContent } = await component('../src/components/atelier/SavedProjectDetail.jsx')
const { ProjectsView } = await component('../src/components/atelier/AtelierViews.jsx')
const id = n => `70000000-0000-4000-8000-${String(n).padStart(12, '0')}`
const token = 'mock-project-detail-token'
const project = { projectId: id(1), name: '회원가입 프로젝트', scenarioId: id(2),
  createdAt: '2026-11-13T09:00:00', updatedAt: '2026-11-13T09:00:00' }
const runs = [
  { hostKey: 'local-multi', runId: id(4), configurationId: 'approved-config-v2', linkedAt: '2026-11-13T11:00:00' },
  { hostKey: 'local-multi', runId: id(3), configurationId: 'approved-config-v1', linkedAt: '2026-11-13T10:00:00' },
]
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
function view(props) {
  return renderToStaticMarkup(createElement(SavedProjectDetailContent, { onLogin() {}, onRetry() {}, ...props }))
}
function findElement(node, predicate) {
  if (!node || typeof node !== 'object') return null
  if (Array.isArray(node)) return node.map(child => findElement(child, predicate)).find(Boolean)
  return predicate(node) ? node : findElement(node.props?.children, predicate)
}

test('카드 상세 선택: 제품 JWT GET, 여러 Run 공개 이력과 서버 순서 표시', async t => {
  storage(t, token)
  let calls = 0, selected
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    calls++
    assert.equal(url, `/api/projects/${project.projectId}`)
    assert.equal(options.method, 'GET')
    assert.equal(options.headers.Authorization, `Bearer ${token}`)
    assert.equal(options.redirect, 'error')
    return Response.json({ ...project, owner_user_id: 99, runs: runs.map(run => ({ ...run, status: 'FAKE_SUCCESS', internal: token })) })
  })
  const tree = ProjectsView({ projects: savedProjectCards([project]), search: '', actual: true,
    saved: { status: 'success' }, onSelect() { assert.fail('저장된 카드가 현재 Run 선택을 호출하면 안 됩니다.') },
    onStoredSelect(projectId) { selected = projectId } })
  const card = findElement(tree, node => node.props?.project?.projectId === project.projectId)
  assert.ok(card)
  const button = card.type(card.props)
  assert.equal(button.type, 'button')
  assert.equal(button.props.className, 'project-card')
  button.props.onClick()
  assert.equal(selected, project.projectId)
  const data = await getProject(selected)
  assert.deepEqual(data, { ...project, runs })
  const opened = []
  const onOpenRun = (selectedProject, run) => opened.push([selectedProject.projectId, run.runId])
  const html = view({ status: 'success', project: data, onOpenRun })
  for (const value of [project.name, '기타 시나리오', '작업 1', '작업 2',
    ...[project.createdAt, ...runs.map(run => run.linkedAt)].map(value => value.replace('T', ' '))]) {
    assert.ok(html.includes(value), value)
  }
  const content = SavedProjectDetailContent({ status: 'success', project: data, onOpenRun })
  const history = findElement(content, node => node.type === 'ol' && node.props['aria-label'] === '저장된 Run 연결 이력')
  for (const row of history.props.children) {
    findElement(row, node => node.type === 'button').props.onClick()
  }
  assert.deepEqual(opened, runs.map(run => [project.projectId, run.runId]), 'server history order and Run identity preserved')
  for (const raw of [project.projectId, project.scenarioId, ...runs.flatMap(run => [run.runId, run.configurationId])]) {
    assert.ok(!html.includes(raw), 'technical IDs stay out of the simplified display')
  }
  assert.ok(!html.includes(token) && !html.includes('FAKE_SUCCESS') && !html.includes('owner_user_id'))
  assert.equal(calls, 1)
})

test('빈 Run 이력 표시: 로딩·네트워크 오류·잘못된 응답을 빈 이력으로 표시하지 않음', async t => {
  storage(t, token)
  let response = () => Response.json({ ...project, runs: [] })
  t.mock.method(globalThis, 'fetch', async () => response())
  const data = await getProject(project.projectId)
  assert.match(view({ status: 'success', project: data }), /아직 시작한 작업이 없어요/)
  const loading = view({ status: 'loading' })
  assert.match(loading, /role="status"/)
  assert.ok(!loading.includes('아직 시작한 작업'))
  response = () => { throw new Error('private-network-detail') }
  let failure
  await assert.rejects(getProject(project.projectId), error => { failure = error; return error.code === 'NETWORK_ERROR' })
  const html = view({ status: 'error', error: failure })
  assert.match(html, /role="alert"/)
  assert.match(html, /다시 불러오기/)
  assert.ok(!html.includes('아직 시작한 작업') && !html.includes('private-network-detail'))
  response = () => Response.json({ ...project, runs: null })
  await assert.rejects(getProject(project.projectId), { code: 'INVALID_RESPONSE' })
})

test('JWT 누락 요청 차단·401 로그인 안내·소유권 없는 프로젝트 404 안전 처리', async t => {
  let currentToken = null, calls = 0, bodyReads = 0, status = 401
  storage(t, () => currentToken)
  t.mock.method(globalThis, 'fetch', async () => {
    calls++
    return { status, ok: false, json() { bodyReads++; return { detail: token } } }
  })
  await assert.rejects(getProject(project.projectId), { code: 'AUTH_REQUIRED', status: 401 })
  assert.equal(calls, 0)
  currentToken = token
  let failure
  await assert.rejects(getProject(project.projectId), error => { failure = error; return error.code === 'AUTH_REQUIRED' })
  const auth = view({ status: 'error', error: failure })
  assert.match(auth, />로그인<\/button>/)
  assert.ok(!auth.includes(token))
  status = 404
  await assert.rejects(getProject(project.projectId), error => { failure = error; return error.code === 'NOT_FOUND' && error.status === 404 })
  const missing = view({ status: 'error', error: failure })
  assert.match(missing, /프로젝트를 찾을 수 없습니다/)
  assert.ok(!missing.includes('아직 연결된 Run') && !missing.includes(token))
  assert.equal(bodyReads, 0)
  // 조회 도중 계정이 바뀐 응답도 표시하지 않습니다.
  t.mock.method(globalThis, 'fetch', async () => {
    currentToken = 'another-mock-session'
    return Response.json({ ...project, runs })
  })
  await assert.rejects(getProject(project.projectId), { code: 'AUTH_REQUIRED' })
})
