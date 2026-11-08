// 목업 키와 분리된 실제 성공 Run의 재생 시작 이력입니다. 단일 탭에서 사용합니다.
export const successCeremonyStorageKey = 'ai-agent-company.office3d.success-ceremonies.v1'
const poses = ['team', 'cheer', 'hug']
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const emptyLedger = () => ({ version: 1, nextIndex: 0, runs: [] })

export function readSuccessCeremonies(storage) {
  const raw = storage.getItem(successCeremonyStorageKey)
  if (raw === null) return emptyLedger()
  const ledger = JSON.parse(raw)
  if (ledger?.version !== 1 || !Number.isInteger(ledger.nextIndex) || ledger.nextIndex < 0 || ledger.nextIndex >= poses.length
    || !Array.isArray(ledger.runs) || ledger.runs.some(item => !uuid.test(item?.runId) || !poses.includes(item?.pose))
    || new Set(ledger.runs.map(item => item.runId.toLowerCase())).size !== ledger.runs.length
    || ledger.nextIndex !== ledger.runs.length % poses.length
    || ledger.runs.some((item, index) => item.pose !== poses[index % poses.length])) {
    throw new Error('실제 성공 세리머니 기록을 확인할 수 없습니다.')
  }
  return ledger
}

export function claimSuccessCeremony(runId, storage) {
  if (!uuid.test(runId)) throw new Error('올바른 Run UUID가 필요합니다.')
  const id = runId.toLowerCase()
  const ledger = readSuccessCeremonies(storage)
  const existing = ledger.runs.find(item => item.runId.toLowerCase() === id)
  if (existing) return { ...existing, claimed: false }
  const entry = { runId: id, pose: poses[ledger.nextIndex] }
  // 재생 시작 직전에 한 번의 원자적 setItem으로 이력과 순서를 함께 저장합니다.
  // 이 쓰기가 실패하면 재생하지 않습니다. 중간 종료된 재생도 자동으로 다시 시작하지 않습니다.
  storage.setItem(successCeremonyStorageKey, JSON.stringify({ version: 1,
    nextIndex: (ledger.nextIndex + 1) % poses.length, runs: [...ledger.runs, entry] }))
  return { ...entry, claimed: true }
}

export function resetSuccessCeremonies(storage) {
  storage.setItem(successCeremonyStorageKey, JSON.stringify(emptyLedger()))
}

export function isSuccessCeremonyEligible(snapshot, mode) {
  return mode === 'actual' && snapshot?.source === 'orchestrator' && uuid.test(snapshot.runId)
    && snapshot.architecture === 'MULTI_AGENT' && snapshot.availability === 'ready'
    && snapshot.queryStatus?.run === 'success' && snapshot.queryStatus?.architecture === 'success'
    && ['success', 'empty'].includes(snapshot.queryStatus?.steps) && ['success', 'empty'].includes(snapshot.queryStatus?.events)
    && !Object.values(snapshot.errors ?? {}).some(Boolean)
    && snapshot.run?.status === 'FINISHED' && snapshot.run.verdict === 'SUCCESS'
}

// React 마운트부터 관측하므로 3D가 늦게 준비되어도 성공 전환을 놓치지 않습니다.
export function createRunSuccessCeremony({ getStorage = () => globalThis.localStorage, duration = 12.125 } = {}) {
  let snapshot = null, mode = 'mock', runId = null
  let sawActive = false, wasSuccess = false, autoPending = false, playback = null
  let processed = null, nextPose = 'team', storageError = false, interrupted = false

  function inspect() {
    try {
      const ledger = readSuccessCeremonies(getStorage())
      processed = ledger.runs.find(item => item.runId.toLowerCase() === runId?.toLowerCase()) ?? null
      nextPose = poses[ledger.nextIndex]
      storageError = false
    } catch {
      storageError = true
      autoPending = false
      if (playback) { playback = null; interrupted = true }
    }
  }
  function observe(next, nextMode) {
    if ((next?.runId ?? null) !== runId || mode !== nextMode) {
      runId = next?.runId ?? null
      sawActive = false; wasSuccess = false; autoPending = false; playback = null
      processed = null; interrupted = false; storageError = false; nextPose = 'team'
    }
    snapshot = next; mode = nextMode
    const successfulRun = next?.run?.status === 'FINISHED' && next.run.verdict === 'SUCCESS'
    if (mode === 'actual' && next?.queryStatus?.run === 'success') {
      if (next.run?.status && !['FINISHED', 'ABORTED'].includes(next.run.status)) sawActive = true
      if (successfulRun && !wasSuccess && sawActive) autoPending = true
      wasSuccess = successfulRun
    }
    const failedQuery = Object.values(next?.errors ?? {}).some(Boolean)
      || Object.values(next?.queryStatus ?? {}).some(value => ['error', 'cancelled'].includes(value))
    const loading = Object.values(next?.queryStatus ?? {}).includes('loading')
    if (failedQuery || (!loading && !isSuccessCeremonyEligible(next, mode))) autoPending = false
    if (playback && (failedQuery || (!loading && !isSuccessCeremonyEligible(next, mode)))) {
      playback = null; interrupted = true
    }
    if (isSuccessCeremonyEligible(next, mode)) inspect()
  }
  function start({ ready = false, motionEnabled = false, manual = false } = {}) {
    if (playback || (!manual && !autoPending) || (!manual && storageError)
      || !ready || !motionEnabled || !isSuccessCeremonyEligible(snapshot, mode)) return false
    try {
      const entry = claimSuccessCeremony(runId, getStorage())
      processed = entry; storageError = false; autoPending = false
      if (!entry.claimed) return false
      playback = { runId, pose: entry.pose, elapsed: 0, completed: false }
      interrupted = false
      return true
    } catch {
      storageError = true; autoPending = false
      return false
    }
  }
  function advance(delta, { ready, motionEnabled, duration: clipDuration = duration }) {
    // 새로고침 중에는 같은 재생을 일시 정지합니다. 다른 Run·오류로 바뀌면 observe가 중단합니다.
    if (playback && ready && motionEnabled && isSuccessCeremonyEligible(snapshot, mode)) {
      playback.elapsed = Math.min(clipDuration, playback.elapsed + (Number.isFinite(delta) ? Math.max(0, delta) : 0))
      playback.completed = playback.elapsed >= clipDuration
    }
    return playback ? { ...playback } : null
  }
  function reset() {
    try {
      resetSuccessCeremonies(getStorage())
      playback = null; processed = null; storageError = false; interrupted = false
      nextPose = 'team'; autoPending = false; sawActive = false
      return true
    } catch { storageError = true; autoPending = false; return false }
  }
  function info() {
    return { eligible: isSuccessCeremonyEligible(snapshot, mode), autoPending, storageError,
      processed, nextPose, playback: playback ? { ...playback } : null, interrupted }
  }
  return { observe, start, advance, reset, info }
}
