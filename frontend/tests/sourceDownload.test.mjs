import assert from 'node:assert/strict'
import test from 'node:test'
import { createHash } from 'node:crypto'
import { downloadOfficeSource } from '../src/api/officeService.js'
import { createProductRunFetch, runApiTransport } from '../src/api/productRunRequest.js'

const runId = '11111111-1111-4111-8111-111111111111'
const artifactId = '22222222-2222-4222-8222-222222222222'
// 고정 테스트 전용 빈 ZIP 바이트. 실제 산출물이나 서버를 만들지 않습니다.
const zip = new Uint8Array(22)
zip.set([80, 75, 5, 6])
const hash = bytes => createHash('sha256').update(bytes).digest('hex')
const headers = {
  'Content-Type': 'application/zip', 'Content-Length': '22',
  'X-Archive-Sha256': hash(zip), 'X-Snapshot-Sha256': 'a'.repeat(64),
  'X-Source-Artifact-Id': artifactId, 'X-Code-Version': '2',
  'Content-Disposition': 'attachment; filename="../../private.db"',
}
function transport(t, handler, token = 'mock-product-jwt') {
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch({ productApiReady: true,
    readAccessToken: () => token, fetchImpl: handler }))
}

test('등록 Source: 제품 JWT GET, ZIP 해시와 안전한 저장 파일명', async t => {
  transport(t, async (path, options) => {
    assert.equal(path, `/api/v1/runs/${runId}/artifacts/${artifactId}/download`)
    assert.equal(options.headers.get('Authorization'), 'Bearer mock-product-jwt')
    assert.equal(options.headers.get('Accept'), 'application/zip')
    assert.equal(options.redirect, 'error')
    assert.equal(options.cache, 'no-store')
    return new Response(zip, { headers })
  })
  const file = await downloadOfficeSource(runId, artifactId, 2)
  assert.equal(file.filename, `source-${runId}-v2.zip`)
  assert.equal(file.blob.type, 'application/zip')
  assert.deepEqual(new Uint8Array(await file.blob.arrayBuffer()), zip)
})

test('JWT 누락·타 사용자·잘못된 Artifact·토큰 오류 및 중복 클릭 차단', async t => {
  let calls = 0
  transport(t, async () => { calls++; throw new Error('must not call') }, '')
  await assert.rejects(downloadOfficeSource(runId, artifactId, 2), { status: 401 })
  await assert.rejects(downloadOfficeSource(runId, '../invalid', 2), { code: 'INVALID_ARGUMENT' })
  assert.equal(calls, 0)
  for (const status of [401, 403, 404, 502, 503]) {
    transport(t, async () => new Response('private upstream details', { status }))
    await assert.rejects(downloadOfficeSource(runId, artifactId, 2), error => error.status === status && !error.message.includes('private'))
  }
  let finish
  transport(t, () => { calls++; return new Promise(resolve => { finish = resolve }) })
  const first = downloadOfficeSource(runId, artifactId, 2)
  await assert.rejects(downloadOfficeSource(runId, artifactId, 2), { code: 'DOWNLOAD_PENDING' })
  assert.equal(calls, 1)
  finish(new Response(zip, { headers }))
  await first
})

test('미생성·정책 차단·통신 실패·잘못된 ZIP·취소에는 저장 결과를 반환하지 않음', async t => {
  for (const status of [404, 409, 413, 503]) {
    transport(t, async () => new Response('private details', { status }))
    await assert.rejects(downloadOfficeSource(runId, artifactId, 2), { status })
  }
  transport(t, async () => { throw new Error('private connection') })
  await assert.rejects(downloadOfficeSource(runId, artifactId, 2), /Source ZIP을 확인하지 못했습니다/)
  for (const changes of [{ 'Content-Type': 'application/json' }, { 'X-Source-Artifact-Id': runId },
    { 'X-Code-Version': '3' }, { 'X-Archive-Sha256': 'b'.repeat(64) }, { 'Content-Length': '20971521' }]) {
    transport(t, async () => new Response(zip, { headers: { ...headers, ...changes } }))
    await assert.rejects(downloadOfficeSource(runId, artifactId, 2), { code: 'DOWNLOAD_FAILED' })
  }
  for (const bytes of [zip.slice(0, 20), new Uint8Array(22)]) {
    transport(t, async () => new Response(bytes, { headers: { ...headers, 'X-Archive-Sha256': hash(bytes) } }))
    await assert.rejects(downloadOfficeSource(runId, artifactId, 2), { code: 'DOWNLOAD_FAILED' })
  }
  const controller = new AbortController()
  transport(t, async () => { controller.abort(); return new Response(zip, { headers }) })
  await assert.rejects(downloadOfficeSource(runId, artifactId, 2, { signal: controller.signal }), { name: 'AbortError' })
})
