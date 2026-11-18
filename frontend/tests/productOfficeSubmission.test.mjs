import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'
import { createProductRunFetch, runApiTransport } from '../src/api/productRunRequest.js'
import { loadOfficeApproval, officeScenarioId, submitOfficeWork } from '../src/api/officeService.js'

const id = n => '70000000-0000-4000-8000-' + String(n).padStart(12, '0')

test('JWT → 승인 GET → 제품 POST: 기본 차단·명시 클릭·본문·동일 접수 키', async t => {
  const calls = [], key = id(1), projectId = id(2), token = 'mock-product-jwt'
  const fetchImpl = async (path, options) => {
    calls.push({ path, options })
    return Response.json(options.method === 'POST'
      ? { requestId: key, projectId, submissionStatus: 'CREATED', run: { runId: id(3), scenarioId: officeScenarioId },
          firstStep: { runId: id(3) }, dispatchStatus: 'SCHEDULED' }
      : { scenarioId: officeScenarioId, configurationId: 'c'.repeat(64), configuration: { architecture: 'MULTI_AGENT' } },
    { status: options.method === 'POST' ? 201 : 200 })
  }
  const options = { productApiReady: true, readAccessToken: () => token, fetchImpl }
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch(options))
  await assert.rejects(loadOfficeApproval(), { code: 'RUN_SUBMISSION_DISABLED' })
  const body = { projectName: '회원가입', projectId, scenarioId: officeScenarioId,
    configurationId: 'c'.repeat(64), requestText: '회원가입 사이트를 만들어줘.' }
  await assert.rejects(submitOfficeWork(body, key, { confirmed: true }), { code: 'RUN_SUBMISSION_DISABLED' })
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch({ ...options, runSubmissionEnabled: true, readAccessToken: () => null }))
  await assert.rejects(loadOfficeApproval(), { code: 'AUTH_REQUIRED' })
  assert.equal(calls.length, 0)
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch({ ...options, runSubmissionEnabled: true }))
  await assert.rejects(submitOfficeWork(body, key), { code: 'EXPLICIT_SUBMISSION_REQUIRED' })
  await assert.rejects(submitOfficeWork({ ...body, configuration: {} }, key, { confirmed: true }), { code: 'INVALID_ARGUMENT' })
  assert.equal(calls.length, 0)
  const approval = await loadOfficeApproval()
  assert.deepEqual(approval, { scenarioId: officeScenarioId, configurationId: body.configurationId })
  const result = await submitOfficeWork({ ...body, ...approval }, key, { confirmed: true })
  assert.equal(result.httpStatus, 201)
  assert.equal(result.run.runId, id(3))
  assert.equal(calls[0].path, '/api/v1/host/approved-configuration?scenarioId=' + officeScenarioId)
  assert.equal(calls[1].path, '/api/v1/runs')
  assert.deepEqual(JSON.parse(calls[1].options.body), body)
  assert.equal(calls[1].options.headers.get('Idempotency-Key'), key)
  for (const call of calls) {
    assert.equal(call.options.headers.get('Authorization'), 'Bearer ' + token)
    assert.equal(call.options.redirect, 'error')
    assert.equal(call.options.confirmed, undefined)
    assert.ok(!call.path.includes(token))
  }
})

// 기존 isolated UI harness 방식 재사용: 로컬 파일 + HTTP 전부 Mock + 3D renderer 대체.
// Vite/제품/Host 서버와 실제 Run, 105초 시연은 실행하지 않습니다.
let reportPromise
function uiReport() {
  reportPromise ??= (async () => {
    const dir = await mkdtemp(join(tmpdir(), 'product-office-submission-'))
    try {
      const build = await rolldown({
        input: fileURLToPath(new URL('./fixtures/productOfficeSubmission.jsx', import.meta.url)),
        transform: { jsx: 'react-jsx', define: { 'process.env.NODE_ENV': '"development"',
          'import.meta.env': '{"VITE_PRODUCT_RUN_SUBMISSION_ENABLED":"true"}' } },
        plugins: [{ name: 'isolated-office', resolveId(source) {
          if (source === 'react') return fileURLToPath(import.meta.resolve('react'))
          if (source.endsWith('.css')) return '\0style'
          if (source.endsWith('/AgentOffice3D')) return '\0office'
          if (source.endsWith('/useAtelierMotion')) return '\0motion'
        }, load(id) {
          if (id === '\0office') return `import { createElement } from 'react'; export default function Office(props) { globalThis.__officeProps = props; return createElement('section', {}, props.requestPanel); }`
          if (id === '\0motion') return 'export default function useAtelierMotion() {} export function useMediaQuery() { return false }'
          if (id === '\0style') return ''
        } }],
      })
      const { output } = await build.generate({ format: 'iife' }); await build.close()
      await writeFile(join(dir, 'test.js'), output.find(item => item.type === 'chunk').code)
      await writeFile(join(dir, 'test.html'), '<!doctype html><meta charset="utf-8"><body><script src="test.js"></script></body>')
      const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
      const { stdout } = await promisify(execFile)(chrome, ['--headless', '--no-first-run', '--no-default-browser-check',
        '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
        '--host-resolver-rules=MAP * 0.0.0.0', `--user-data-dir=${join(dir, 'profile')}`, '--dump-dom', '--virtual-time-budget=12000',
        pathToFileURL(join(dir, 'test.html')).href], { timeout: 30000, maxBuffer: 2 * 1024 * 1024 })
      const result = stdout.match(/<pre id="test-result">([^<]+)<\/pre>/)?.[1]
      assert.ok(result, 'Mock UI result must be present')
      const report = JSON.parse(result.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&gt;', '>').replaceAll('&lt;', '<'))
      assert.equal(report.failed, undefined, report.failed)
      return report
    } finally { await rm(dir, { recursive: true, force: true }) }
  })()
  return reportPromise
}

test('200·201: 기존 CLI·Run 선택 연결, 생성/재사용 구분, 중복 클릭 차단', { timeout: 45000 }, async () => {
  assert.equal((await uiReport()).success, true)
})

test('202·409·401·502/503·승인 실패: 안전한 안내와 자동 재전송 차단', { timeout: 45000 }, async () => {
  assert.equal((await uiReport()).failures, true)
})
