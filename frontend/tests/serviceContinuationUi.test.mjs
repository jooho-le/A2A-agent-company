import assert from 'node:assert/strict'
import test from 'node:test'
import { access, mkdtemp, writeFile, rm } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import { rolldown } from 'rolldown'

// Real React hook and controls; file:// with all HTTP mocked, no live server/API calls.
test('이어서 수정 UI: 부모 실행 보존·멱등 접수·복원 충돌·승인 실패', {timeout:45000}, async t => {
  const chrome=process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'
  try { await access(chrome) } catch { t.skip('CHROME_BIN 또는 Chrome 필요');return }
  const dir=await mkdtemp(join(tmpdir(),'continuation-ui-'))
  try {
    const build=await rolldown({input:fileURLToPath(new URL('./fixtures/serviceContinuationUi.jsx',import.meta.url)),
      transform:{jsx:'react-jsx',define:{'process.env.NODE_ENV':'"development"',
        'import.meta.env':'{"VITE_PRODUCT_RUN_SUBMISSION_ENABLED":"true"}'}},
      plugins:[{name:'offline',resolveId(source){
        if(source==='react')return fileURLToPath(import.meta.resolve('react'))
        if(source.endsWith('.css'))return '\0style'
      },load(id){if(id==='\0style')return ''}}]})
    const {output}=await build.generate({format:'iife'});await build.close()
    await writeFile(join(dir,'test.js'),output.find(o=>o.type==='chunk').code)
    await writeFile(join(dir,'index.html'),'<!doctype html><meta charset="utf-8"><body><script src="test.js"></script></body>')
    const {stdout}=await promisify(execFile)(chrome,['--headless','--no-first-run','--no-default-browser-check',
      '--disable-background-networking','--disable-component-update','--disable-sync','--disable-extensions',
      '--host-resolver-rules=MAP * 0.0.0.0',`--user-data-dir=${join(dir,'profile')}`,'--dump-dom','--virtual-time-budget=8000',
      pathToFileURL(join(dir,'index.html')).href],{timeout:25000,maxBuffer:2*1024*1024})
    const found=stdout.match(/<pre id="test-result">([^<]+)<\/pre>/)?.[1]
    assert.ok(found,'offline UI result required')
    const result=JSON.parse(found.replaceAll('&quot;','"').replaceAll('&amp;','&').replaceAll('&gt;','>').replaceAll('&lt;','<'))
    assert.equal(result.failed,undefined,result.failed)
    assert.equal(result.passed.length,4)
    result.passed.forEach(message=>t.diagnostic(message))
  } finally {await rm(dir,{recursive:true,force:true})}
})
