import assert from 'node:assert/strict'
import test from 'node:test'
import { mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'

// 기존 isolated UI 방식: 실제 Hook/Trace/상태 제어기 + Mock HTTP/가상 폴링 시계.
// WebGL renderer와 이번 범위 밖의 Run 제어 API만 대체합니다. 서버·실제 DB는 사용하지 않습니다.
let reportPromise
function report() {
  reportPromise ??= (async () => {
    const dir = await mkdtemp(join(tmpdir(), 'product-trace-connection-'))
    try {
      const build = await rolldown({
        input: fileURLToPath(new URL('./fixtures/productTraceConnection.jsx', import.meta.url)),
        transform: { jsx: 'react-jsx', define: { 'process.env.NODE_ENV': '"development"',
          'import.meta.env': '{"VITE_PRODUCT_RUN_SUBMISSION_ENABLED":"true"}' } },
        plugins: [{ name: 'isolated-trace', resolveId(source) {
          if (source === 'react') return fileURLToPath(import.meta.resolve('react'))
          if (source.endsWith('.css')) return '\0style'
          if (source.endsWith('/AgentOffice3D')) return '\0office'
          if (source.endsWith('/useAtelierMotion')) return '\0motion'
          if (source.endsWith('/useRunInteraction')) return '\0controls'
        }, load(id) {
          if (id === '\0office') return `import { createElement } from 'react'; export default function Office(props) { globalThis.__officeProps = props; return createElement('section', {}, props.requestPanel); }`
          if (id === '\0motion') return 'export default function useAtelierMotion() {} export function useMediaQuery() { return false }'
          if (id === '\0controls') return 'export default function useRunInteraction() { return { data: null, commands: {} } }'
          if (id === '\0style') return ''
        } }],
      })
      const { output } = await build.generate({ format: 'iife' }); await build.close()
      await writeFile(join(dir, 'test.js'), output.find(item => item.type === 'chunk').code)
      await writeFile(join(dir, 'test.html'), '<!doctype html><meta charset="utf-8"><body><script src="test.js"></script></body>')
      const chrome = process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
      const { stdout } = await promisify(execFile)(chrome, ['--headless', '--no-first-run', '--no-default-browser-check',
        '--disable-background-networking', '--disable-component-update', '--disable-sync', '--disable-extensions',
        '--host-resolver-rules=MAP * 0.0.0.0', `--user-data-dir=${join(dir, 'profile')}`, '--dump-dom', '--virtual-time-budget=15000',
        pathToFileURL(join(dir, 'test.html')).href], { timeout: 30000, maxBuffer: 2 * 1024 * 1024 })
      const content = stdout.match(/<pre id="test-result">([^<]+)<\/pre>/)?.[1]
      assert.ok(content, 'Mock Trace test result must be present')
      return JSON.parse(content.replaceAll('&quot;', '"').replaceAll('&amp;', '&').replaceAll('&gt;', '>').replaceAll('&lt;', '<'))
    } finally { await rm(dir, { recursive: true, force: true }) }
  })()
  return reportPromise
}

for (const [key, label] of [
  ['created', '200/201 Run 선택 → JWT 제품 이벤트 GET → 기존 3D 상태'],
  ['trace', '100개 순차 페이지·중복·커서 복구·Run 전환 및 늦은 응답 차단'],
  ['errors', '202·조회 실패 재시도 상한·401/403/404·로그아웃/언마운트 정리'],
]) {
  test(label, { timeout: 45000 }, async () => {
    const result = await report()
    assert.equal(result[key], true, result.failed || key)
  })
}
