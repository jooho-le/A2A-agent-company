import { useEffect, useReducer } from 'react'
import { getProjects } from '../api/projects.js'

const initial = { projects: [], status: 'idle', error: null }

export default function useSavedProjects(enabled) {
  const [state, update] = useReducer((_state, next) => next, initial)
  const [attempt, retry] = useReducer(value => value + 1, 0)
  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    let current = true
    update({ ...initial, status: 'loading' })
    getProjects({ signal: controller.signal }).then(projects => {
      if (current) update({ projects, status: 'success', error: null })
    }).catch(error => {
      // 메뉴 전환·로그인 전환 후 늦게 도착한 응답은 적용하지 않습니다.
      if (current && error.name !== 'AbortError') update({ ...initial, status: 'error', error })
    })
    return () => { current = false; controller.abort() }
  }, [enabled, attempt])
  return { ...state, retry }
}
