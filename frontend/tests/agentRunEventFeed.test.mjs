import assert from 'node:assert/strict'
import test from 'node:test'
import { createRunEventFeed } from '../src/hooks/agentRunEventFeed.js'

const event = (n, runId = 'a') => ({ eventId: String(n), runId, eventType: 'A2A_MESSAGE_SENT' })
function server(rows, calls = []) {
  return async (runId, { limit, offset }) => {
    calls.push({ limit, offset })
    return { runId, total: rows.length, limit, offset, events: rows.slice(offset, offset + limit) }
  }
}
test('첫 과거 조회는 기준점이며 새로 추가된 이벤트만 수집한다', async () => {
  const feed = createRunEventFeed(), rows = [event(1), event(2)]
  const baseline = await feed.pull('a', server(rows))
  assert.equal(baseline.cursor, 2); assert.deepEqual(baseline.entries, [])
  rows.push(event(3))
  const next = await feed.pull('a', server(rows))
  assert.deepEqual(next.entries.map(item => item.event.eventId), ['3'])
  assert.equal(next.cursor, 3)
})
test('한 번에 여러 페이지가 도착해도 offset 순서로 전부 읽는다', async () => {
  const feed = createRunEventFeed(), rows = [event(0)], calls = []
  await feed.pull('a', server(rows))
  rows.push(...Array.from({ length: 1205 }, (_, i) => event(i + 1)))
  const next = await feed.pull('a', server(rows, calls))
  assert.equal(next.entries.length, 1205)
  assert.deepEqual(calls.map(item => item.offset), [1, 501, 1001])
  assert.deepEqual(next.entries.map(item => item.offset), Array.from({ length: 1205 }, (_, i) => i + 1))
})
test('중복 eventId도 원본 행 수만큼 offset을 전진하되 재생 항목은 한 번만 만든다', async () => {
  const feed = createRunEventFeed(), rows = []
  await feed.pull('a', server(rows)); rows.push(event(1), event(1), event(2))
  const a = await feed.pull('a', server(rows)), b = await feed.pull('a', server(rows))
  assert.equal(a.cursor, 3); assert.equal(a.entries.length, 2)
  assert.equal(b.entries, a.entries); assert.equal(b.revision, a.revision)
})
test('중간 페이지 실패는 커서를 소비하지 않고 다음 요청에서 전체 묶음을 복구한다', async () => {
  const feed = createRunEventFeed(), rows = []
  await feed.pull('a', server(rows)); rows.push(...Array.from({ length: 501 }, (_, i) => event(i)))
  await assert.rejects(feed.pull('a', async (runId, options) => {
    if (options.offset === 500) throw new Error('network')
    return server(rows)(runId, options)
  }), /network/)
  const calls = [], recovered = await feed.pull('a', server(rows, calls))
  assert.equal(calls[0].offset, 0); assert.equal(recovered.entries.length, 501)
})
test('조회 중 늘어난 total은 다음 폴링에 이어 읽는다', async () => {
  const feed = createRunEventFeed(), rows = []
  await feed.pull('a', server(rows)); rows.push(event(1))
  const next = await feed.pull('a', async (runId, options) => {
    const page = await server(rows)(runId, options); rows.push(event(2)); return page
  })
  assert.equal(next.cursor, 1)
  const last = await feed.pull('a', server(rows))
  assert.deepEqual(last.entries.map(item => item.event.eventId), ['1', '2'])
})
test('Run 전환 후 늦은 응답이 다른 Run의 커서를 덮어쓰지 않는다', async () => {
  const feed = createRunEventFeed(); let resolve
  const old = feed.pull('a', () => new Promise(done => { resolve = done }))
  const result = await feed.pull('b', server([event(1, 'b')]))
  resolve({ runId: 'a', limit: 1, offset: 0, total: 1, events: [event(1)] })
  await assert.rejects(old, { name: 'AbortError' }); assert.equal(result.runId, 'b')
})
test('요청 취소 시 신규 이벤트를 소비하지 않는다', async () => {
  const feed = createRunEventFeed(), rows = [], controller = new AbortController()
  await feed.pull('a', server(rows)); rows.push(event(1))
  await assert.rejects(feed.pull('a', async (runId, options) => {
    const page = await server(rows)(runId, options); controller.abort(); return page
  }, controller.signal), { name: 'AbortError' })
  assert.equal((await feed.pull('a', server(rows))).entries.length, 1)
})
test('빈 중간 페이지와 다른 Run 응답은 누락으로 넘기지 않고 오류를 표시한다', async () => {
  const feed = createRunEventFeed(); await feed.pull('a', server([]))
  await assert.rejects(feed.pull('a', async (runId, { limit, offset }) => ({ runId, limit, offset, total: 1, events: [] })), /비어/)
  await assert.rejects(feed.pull('a', server([event(1, 'b')])), /eventId/)
  assert.equal((await feed.pull('a', server([event(1)]))).entries.length, 1)
})
