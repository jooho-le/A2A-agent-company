import assert from 'node:assert/strict'
import test from 'node:test'
import { getAgentRun, getAgentRunEvents, getAgentRunSteps } from '../src/api/agentRuns.js'
import { createProductRunFetch, runApiTransport } from '../src/api/productRunRequest.js'

const runId = '70000000-0000-4000-8000-000000000001'
const token = 'mock-product-jwt-only'
const run = { runId, status: 'RECEIVED' }

test('JWT: 기존 sessionStorage 토큰으로 보호 요청 헤더 구성, 제품 API에만 전송', async t => {
  const reads = [], requests = []
  const storageDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'sessionStorage')
  Object.defineProperty(globalThis, 'sessionStorage', { configurable: true,
    value: { getItem(key) { reads.push(key); return token } } })
  t.after(() => {
    if (storageDescriptor) Object.defineProperty(globalThis, 'sessionStorage', storageDescriptor)
    else delete globalThis.sessionStorage
  })
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    requests.push({ path, options })
    return Response.json(path.includes('/events')
      ? { runId, events: [], total: 0, limit: 2, offset: 0 } : run)
  })
  // 앱 경로도 기존 JWT를 사용하며 인증되지 않은 Host 직접 연결은 없습니다.
  await getAgentRun(runId)
  assert.equal(new Headers(requests[0].options.headers).get('Authorization'), `Bearer ${token}`)
  assert.deepEqual(reads, ['access_token'])
  // 보호 전송기도 기본값으로는 실제 프록시에 보내지 못합니다.
  await assert.rejects(createProductRunFetch()(`/api/v1/runs/${runId}`), { code: 'PRODUCT_API_NOT_READY' })
  assert.equal(requests.length, 1)
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch({ productApiReady: true }))
  assert.deepEqual(await getAgentRun(runId), run)
  await getAgentRunEvents(runId, { limit: 2, offset: 0 })
  for (const { path, options } of requests.slice(1)) {
    assert.equal(options.headers.get('Authorization'), `Bearer ${token}`)
    assert.equal(options.redirect, 'error')
    assert.ok(!path.includes(token))
  }
  assert.ok(reads.every(key => key === 'access_token'))
})

test('접수 전 실패 헤더만 미접수로 인정하고 일반 503은 불명확한 전송으로 유지', async () => {
  for (const header of [null, 'NOT_ACCEPTED', 'UNKNOWN']) {
    const fetch = createProductRunFetch({ productApiReady: true, runSubmissionEnabled: true, readAccessToken: () => token,
      fetchImpl: async () => new Response(null, { status: 503, headers: header ? { 'X-Submission-Status': header } : {} }) })
    await assert.rejects(fetch('/api/v1/runs', { method: 'POST', confirmed: true }), error => {
      assert.equal(error.status, 503)
      assert.equal(error.submissionNotAccepted, header === 'NOT_ACCEPTED')
      return true
    })
    await assert.rejects(fetch('/api/v1/scenarios', { method: 'GET' }), error => {
      assert.equal(error.submissionNotAccepted, false)
      return true
    })
  }
})

test('실제 생성은 연결 스위치와 명시적 사용자 요청을 모두 통과해야 전송된다', async () => {
  let calls = 0
  const options = { productApiReady: true, readAccessToken: () => token,
    fetchImpl: async () => { calls++; return Response.json({}) } }
  await assert.rejects(createProductRunFetch({ ...options, runSubmissionEnabled: false })('/api/v1/runs', {
    method: 'POST', confirmed: true,
  }), { code: 'RUN_SUBMISSION_DISABLED' })
  await assert.rejects(createProductRunFetch({ ...options, runSubmissionEnabled: true })('/api/v1/runs', {
    method: 'POST',
  }), { code: 'EXPLICIT_SUBMISSION_REQUIRED' })
  assert.equal(calls, 0)
})

test('JWT 없음: 보호 Run·Step·Trace 요청은 fetch 전에 차단', async t => {
  let calls = 0
  const fetchImpl = async () => { calls++; throw Error('Mock must not be called') }
  for (const readAccessToken of [() => null, () => '', () => { throw Error('storage unavailable') }]) {
    t.mock.method(runApiTransport, 'fetch', createProductRunFetch({ productApiReady: true, readAccessToken, fetchImpl }))
    for (const read of [getAgentRun, getAgentRunSteps, getAgentRunEvents]) {
      await assert.rejects(read(runId), error => error.code === 'AUTH_REQUIRED'
        && error.status === 401 && /로그인/.test(error.message))
    }
  }
  assert.equal(calls, 0)
})

test('401: 로그인 안내만 반환하고 응답·원본 오류·토큰을 노출하거나 재요청하지 않음', async t => {
  let calls = 0, bodyReads = 0
  t.mock.method(runApiTransport, 'fetch', createProductRunFetch({
    productApiReady: true,
    readAccessToken: () => token,
    fetchImpl: async () => {
      calls++
      return { status: 401, ok: false, json() { bodyReads++; return { detail: token } } }
    },
  }))
  await assert.rejects(getAgentRun(runId), error => {
    assert.equal(error.code, 'AUTH_REQUIRED')
    assert.equal(error.status, 401)
    assert.match(error.message, /로그인/)
    assert.equal(error.detail, null)
    assert.equal(error.cause, undefined)
    assert.ok(!`${error.message}${JSON.stringify(error)}`.includes(token))
    return true
  })
  assert.equal(calls, 1)
  assert.equal(bodyReads, 0)
})
