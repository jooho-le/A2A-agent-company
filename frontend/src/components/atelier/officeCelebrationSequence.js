import { finaleOptions } from './office3dPerformance'

// 실제 Run 기록과 섞이지 않는 3D 발표 시연 전용 저장값입니다.
const storageKey = 'atelier.office3d.ceremony-order.v1'
let fallbackIndex = null

function readIndex() {
  if (fallbackIndex !== null) return fallbackIndex
  let raw
  try {
    raw = globalThis.localStorage.getItem(storageKey)
  } catch {
    fallbackIndex = 0
    return fallbackIndex
  }
  try {
    const index = JSON.parse(raw)?.nextIndex
    return Number.isInteger(index) && index >= 0 && index < finaleOptions.length ? index : 0
  } catch {
    return 0
  }
}

function writeIndex(index) {
  try {
    globalThis.localStorage.setItem(storageKey, JSON.stringify({ nextIndex: index }))
    fallbackIndex = null
    return true
  } catch {
    // 저장이 차단되어도 현재 페이지의 시연 순서는 유지합니다.
    fallbackIndex = index
    return false
  }
}

export function getNextCelebration() {
  const index = readIndex()
  return { ...finaleOptions[index], number: index + 1, persisted: fallbackIndex === null }
}

export function completeCelebration() {
  const selected = getNextCelebration()
  const persisted = writeIndex(selected.number % finaleOptions.length)
  return { ...selected, persisted }
}

export function resetCelebrationOrder() {
  return writeIndex(0)
}
