import assert from 'node:assert/strict'
import test from 'node:test'
import { access, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'

// Isolated UI test only: real Dashboard/submit/monitor hooks, mocked HTTP and 3D renderer.
// No Vite/Host server, network fallback, 105-second playback, or production build.
test('통합 업무 입력창: 단일 UI·승인 차단·자동 Run/Trace 연결·중복/실패 처리', { timeout: 45000 }, async t => {
  const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  try { await access(chrome) } catch { t.skip('CHROME_BIN 또는 설치된 Chrome 필요'); return }
  const dir = await mkdtemp(join(tmpdir(), 'office-request-ui-'))
  try {
    const build = await rolldown({
      input: fileURLToPath(new URL('./fixtures/officeRequestUi.jsx', import.meta.url)),
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
