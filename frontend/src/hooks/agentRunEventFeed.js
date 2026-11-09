const cancelled = () => new DOMException('조회가 취소되었습니다.', 'AbortError')

// 화면의 Trace 페이지와 별개인 append-only offset입니다. 저장 순서는 서버 배열을 따릅니다.
export function createRunEventFeed() {
  let run = null, epoch = 0, request = 0, state = null, seen = new Set()
  function reset() { run = null; epoch++; request++; state = null; seen = new Set() }
  async function pull(runId, loadPage, signal) {
    if (run !== runId) { reset(); run = runId }
    const ticket = ++request, generation = epoch
    const check = () => {
      signal?.throwIfAborted()
      if (run !== runId || generation !== epoch || ticket !== request) throw cancelled()
    }
    const read = async (limit, offset) => {
      check()
      const page = await loadPage(runId, { limit, offset, signal })
      check()
      if (page.runId !== runId || page.limit !== limit || page.offset !== offset || !Array.isArray(page.events)
        || !Number.isSafeInteger(page.total) || page.total < offset || page.events.length > limit) {
        throw new Error('Trace 페이지 계약을 확인할 수 없습니다.')
      }
      return page
    }
    if (!state) {
      const page = await read(1, 0)
      // 최초 과거 기록은 재생하지 않습니다. 이후 추가된 행부터 읽습니다.
      state = { runId, epoch, revision: 0, initialized: true, cursor: page.total, fromCursor: page.total, total: page.total, entries: [] }
      return state
    }
    const start = state, additions = [], ids = new Set(seen)
    let offset = start.cursor, target = null
    do {
      const page = await read(500, offset)
      if (target === null) target = page.total
      const rows = page.events.slice(0, Math.max(0, target - offset))
      if (offset < target && rows.length === 0) throw new Error('Trace 중간 페이지가 비어 있어 다음 조회에서 재시도합니다.')
      rows.forEach((event, index) => {
        if (!event || event.runId !== runId || typeof event.eventId !== 'string' || !event.eventId) {
          throw new Error('Trace 이벤트의 Run 또는 eventId를 확인할 수 없습니다.')
        }
        if (!ids.has(event.eventId)) { ids.add(event.eventId); additions.push({ offset: offset + index, event }) }
      })
      offset += rows.length
    } while (offset < target)
    check()
    // 전체 묶음이 성공한 뒤에만 커서를 전진합니다. 실패·취소는 다음 폴링에서 재시도합니다.
    seen = ids
    state = { ...start, cursor: offset, fromCursor: start.cursor, total: target, revision: start.revision + (additions.length ? 1 : 0),
      entries: additions.length ? [...start.entries, ...additions] : start.entries }
    return state
  }
  return { pull, reset }
}
