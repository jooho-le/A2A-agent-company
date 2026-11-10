import assert from 'node:assert/strict'
import test from 'node:test'
import { access, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'

// Isolated UI test only: project/output navigation with real Dashboard/monitor hooks, mocked HTTP and 3D renderer.
// No Vite/Host server, network fallback, 105-second playback, or production build.
test('프로젝트·보관함: 현재 Run 연결, 실제 목록, 빈 상태와 조회 오류', { timeout: 45000 }, async t => {
  const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  try { await access(chrome) } catch { t.skip('CHROME_BIN 또는 설치된 Chrome 필요'); return }
  const dir = await mkdtemp(join(tmpdir(), 'project-outputs-ui-'))
  try {
    const build = await rolldown({
      input: fileURLToPath(new URL('./fixtures/projectOutputsUi.jsx', import.meta.url)),
      transform: { jsx: 'react-jsx', define: { 'process.env.NODE_ENV': '"development"' } },
      plugins: [{ name: 'isolated-office', resolveId(source) {
        if (source === 'react') return fileURLToPath(import.meta.resolve('react'))
        if (source.endsWith('.css')) return '\0style'
        if (source.endsWith('/AgentOffice3D')) return '\0office'
        if (source.endsWith('/useAtelierMotion')) return '\0motion'
      }, load(id) {
        if (id === '\0office') return `import { createElement } from 'react'; export default function Office(props) { globalThis.__officeProps = props; return createElement('section', { 'data-testid': 'office' }); }`
        if (id === '\0motion') return 'export default function useAtelierMotion() {} export function useMediaQuery() { return false }'
        if (id === '\0style') return ''
      } }],
    })
    const { output } = await build.generate({ format: 'iife' }); await build.close()
    const code = output.find(item => item.type === 'chunk').code
    await writeFile(join(dir, 'test.js'), code)
    await writeFile(join(dir, 'test.html'), '<!doctype html><meta charset="utf-8"><body><script src="test.js"></script></body>')
    const { stdout } = await promisify(execFile)(chrome, ['--headless', '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
      '--host-resolver-rules=MAP * 0.0.0.0', `--user-data-dir=${join(dir, 'profile')}`, '--dump-dom', '--virtual-time-budget=8000',
      pathToFileURL(join(dir, 'test.html')).href], { timeout: 25000, maxBuffer: 2 * 1024 * 1024 })
    const result = stdout.match(/<pre id="test-result">([^<]+)<\/pre>/)?.[1]
    assert.ok(result, `isolated browser produced a result: ${stdout.slice(-1500)}`)
    const report = JSON.parse(result.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&gt;', '>').replaceAll('&lt;', '<'))
    assert.equal(report.failed, undefined, report.failed)
    assert.equal(report.passed.length, 4)
    report.passed.forEach(check => t.diagnostic(check))
  } finally { await rm(dir, { recursive: true, force: true }) }
})

import { artifactCatalog } from '../src/api/agentArtifactCatalog.js'
import { submittedRunProject } from '../src/components/atelier/agentRunProject.js'
import { getAgentRunArtifacts } from '../src/api/agentRuns.js'
const id = n => `80000000-0000-4000-8000-${String(n).padStart(12, '0')}`

test('산출물 API: 공개 등록 필드만 보관, 다른 Run·민감 내용·임의 경로 제외', async t => {
  const runId = id(1)
  const rows = [{ runId, artifactId: id(2), artifactType: 'CHANGE_REPORT', artifactVersion: 1,
    artifactUri: `artifact://${id(2)}/changes.json`, createdBy: 'DEVELOPER', summary: '실제 수정 요약',
    fileChanges: [{ path: 'src/form.js', action: 'MODIFIED' }, { path: '../secret', action: 'ADDED' }],
    configuration: { password: 'private-fixture' }, executionManifest: { workingDir: '/private/fixture' } },
  { runId, artifactId: id(3), artifactType: 'QA_REPORT', tests: [{ outcome: 'FAIL', details: 'apiKey=private-fixture' }] }]
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    assert.equal(path, `/api/v1/runs/${runId}/artifacts`); assert.equal(options.method, 'GET')
    return { ok: true, json: async () => ({ runId, artifacts: rows }) }
  })
  const result = await getAgentRunArtifacts(runId)
  assert.equal(result.artifacts.length, 2) // Existing popup projection remains available.
  assert.equal(result.catalog[0].name, 'changes.json')
  assert.deepEqual(result.catalog[0].changes, [{ path: 'src/form.js', action: '수정' }])
  assert.equal(result.catalog[0].details[0], '실제 수정 요약')
  assert.ok(!JSON.stringify(result.catalog).includes('private-fixture'))
  assert.ok(!JSON.stringify(result.catalog).includes('/private/fixture'))
  assert.equal(artifactCatalog(rows, id(9)).length, 0)
  assert.equal(artifactCatalog([{ ...rows[0], artifactUri: 'https://example.invalid/file?token=private-fixture' }], runId)[0].name, '')
})

test('프로젝트: QA·Security 병렬 Step, FINISHED와 성공 구분, 조회 오류·Run 격리', () => {
  const runId = id(1), run = { runId, status: 'VALIDATING', verdict: null, codeVersion: 1, fixAttempt: 0 }
  const submission = { result: { run }, name: '실제 요청', requestText: '기능 검증' }
  const monitor = { runId, run, architecture: 'MULTI_AGENT', status: { run: 'success', steps: 'success' }, errors: {},
    steps: ['QA', 'SECURITY'].map((agentRole, n) => ({ runId, workflowStepId: id(n + 10), agentRole, status: 'RUNNING', attempt: 0, codeVersion: 1 })) }
  const project = submittedRunProject(submission, monitor)
  assert.equal(project.id, runId); assert.equal(project.status.text, '검증 중')
  assert.deepEqual(project.agents.filter(a => a.assigned).map(a => a.label), ['작업', '작업'])
  assert.ok(!Object.hasOwn(project, 'progress'))
  assert.equal(submittedRunProject(submission, { ...monitor, run: { ...run, status: 'FINISHED', verdict: 'FAIL' } }).status.text, '종료 · 실패')
  assert.equal(submittedRunProject(submission, { ...monitor, run: { ...run, status: 'FINISHED', verdict: 'SUCCESS' } }).status.text, '업무 완료')
  assert.equal(submittedRunProject(submission, { ...monitor, errors: { run: new Error('offline') } }).status.text, '상태 조회 오류')
  assert.equal(submittedRunProject(submission, { ...monitor, runId: id(8) }), null)
  assert.equal(submittedRunProject({ result: null }, monitor), null)
})
