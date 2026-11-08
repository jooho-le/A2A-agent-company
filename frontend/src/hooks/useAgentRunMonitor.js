import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { getAgentRun, getAgentRunSteps, getAgentRunEvents, getAgentRunArchitecture } from '../api/agentRuns'
import { createRunPolling, isTerminalRun, pollingDelay } from './agentRunPolling'

const resourceNames = ['run', 'steps', 'events', 'architecture']

function initialState(query) {
  const resource = () => ({ data: null, status: query.runId ? 'loading' : 'idle', error: null })
  return { query, requestId: null, failures: 0, run: resource(), steps: resource(), events: resource(),
    architecture: query.includeArchitecture ? resource() : { data: null, status: 'idle', error: null } }
}

function reducer(state, action) {
  if (action.type === 'start') {
    const next = { ...initialState(action.query), requestId: action.requestId }
    if (state.query === action.query) {
      next.failures = state.failures
      for (const name of resourceNames) next[name].data = state[name].data
    }
    return next
  }
  if (state.query !== action.query || state.requestId !== action.requestId) return state

  if (action.type === 'settled') {
    const value = action.value.status === 'error' || action.value.status === 'cancelled'
      ? { ...action.value, data: state[action.resource].data } : action.value
    return { ...state, [action.resource]: value }
  }
  if (action.type === 'complete') return { ...state, failures: action.failures }
  if (action.type === 'cancel') {
    const next = { ...state }
    for (const name of resourceNames) {
      if (state[name].status === 'loading') {
        next[name] = { ...state[name], status: 'cancelled', error: null }
      }
    }
    return next
  }
  return state
}

/**
 * Run ID가 없으면 조회하지 않습니다. polling을 켠 실제 화면에서만 완료 후 3초 간격으로 조회합니다.
 * includeArchitecture를 켠 화면은 공개 configuration에서 architecture만 함께 가져옵니다.
 * status는 조회 상태(idle/loading/success/empty/error/cancelled)이고 Run의 실행 status와 별개입니다.
 * refresh()는 진행 중인 같은 조회를 재사용합니다. cancel() 뒤 refresh()로 다시 조회할 수 있습니다.
 */
export default function useAgentRunMonitor(runId, { limit = 100, offset = 0, includeArchitecture = false, polling = false } = {}) {
  const normalizedRunId = typeof runId === 'string' ? runId.trim() : ''
  const query = useMemo(() => ({ runId: normalizedRunId, limit, offset, includeArchitecture }), [normalizedRunId, limit, offset, includeArchitecture])
  const [state, dispatch] = useReducer(reducer, query, initialState)
  const activeRequest = useRef(null)
  const sequence = useRef(0)
  const activeQuery = useRef(null)
  const poller = useRef(null)
  const lastResult = useRef(null)
  const [pausedRun, setPausedRun] = useState(null)
  const paused = Boolean(query.runId) && pausedRun === query.runId

  const refresh = useCallback(() => {
    if (activeQuery.current !== query || !query.runId) return Promise.resolve()
    if (activeRequest.current?.query === query) return activeRequest.current.promise

    const controller = new AbortController()
    const requestId = ++sequence.current
    const request = { query, requestId, controller, promise: null, results: {}, failed: false }
    activeRequest.current = request
    dispatch({ type: 'start', query, requestId })

    const isCurrent = () => activeQuery.current === query && activeRequest.current === request && !controller.signal.aborted
    const loaders = {
      run: () => getAgentRun(query.runId, { signal: controller.signal }),
      steps: () => getAgentRunSteps(query.runId, { signal: controller.signal }),
      events: () => getAgentRunEvents(query.runId, { limit: query.limit, offset: query.offset, signal: controller.signal }),
      architecture: () => getAgentRunArchitecture(query.runId, { signal: controller.signal }),
    }

    const enabled = query.includeArchitecture ? resourceNames : resourceNames.filter(name => name !== 'architecture')
    request.promise = Promise.all(enabled.map(async (name) => {
      try {
        const data = await loaders[name]()
        if (!isCurrent()) return
        request.results[name] = data
        const empty = (name === 'steps' || name === 'events') && data[name].length === 0
        dispatch({ type: 'settled', query, requestId, resource: name,
          value: { data, status: empty ? 'empty' : 'success', error: null } })
      } catch (error) {
        if (!isCurrent()) return
        const cancelled = error?.name === 'AbortError'
        request.failed = true
        // 원본 오류의 status·code·detail을 유지하고 다른 조회 결과는 계속 받습니다.
        dispatch({ type: 'settled', query, requestId, resource: name,
          value: { data: null, status: cancelled ? 'cancelled' : 'error', error: cancelled ? null : error } })
      }
    })).then(() => {
      if (isCurrent()) {
        const previous = lastResult.current?.query === query ? lastResult.current : null
        const failures = request.failed ? (previous?.failures ?? 0) + 1 : 0
        lastResult.current = { query, failures,
          terminal: isTerminalRun(request.results.run?.status) || Boolean(previous?.terminal),
          architecture: request.results.architecture?.architecture ?? previous?.architecture ?? null }
        dispatch({ type: 'complete', query, requestId, failures })
      }
      if (activeRequest.current === request) activeRequest.current = null
    })
    poller.current?.waitFor(request.promise)
    return request.promise
  }, [query])

  const abortRequest = useCallback(() => {
    const request = activeRequest.current
    if (!request || request.query !== query) return
    activeRequest.current = null
    request.controller.abort()
    dispatch({ type: 'cancel', query, requestId: request.requestId })
  }, [query])

  const pausePolling = useCallback(() => {
    poller.current?.stop()
    setPausedRun(query.runId)
    abortRequest()
  }, [query.runId, abortRequest])
  const resumePolling = useCallback(() => setPausedRun(null), [])

  useEffect(() => {
    activeQuery.current = query
    void refresh()
    return () => {
      if (activeQuery.current === query) activeQuery.current = null
      const request = activeRequest.current
      if (request?.query === query) {
        activeRequest.current = null
        request.controller.abort()
      }
    }
  }, [query, refresh])

  // 목업 복귀·화면 이탈 시 진행 중인 GET도 취소합니다. 최종 상태 확인 중인
  // 같은 조회는 끝까지 받아 Step·Event·세리머니의 마지막 응답을 보존합니다.
  useEffect(() => {
    if (!polling) return
    return abortRequest
  }, [polling, abortRequest])

  // effect가 정리되기 전에도 다른 Run·페이지의 이전 데이터를 표시하지 않습니다.
  const current = state.query === query ? state : initialState(query)
  const eventPage = current.events.data
  const terminal = isTerminalRun(current.run.data?.status)
  const singleAgent = current.architecture.data?.architecture === 'SINGLE_AGENT'
  useEffect(() => {
    if (!polling || paused || !query.runId || terminal || singleAgent) return
    const loop = createRunPolling({ refresh, getState: () => ({
      ...(lastResult.current?.query === query ? lastResult.current : {}),
      pending: activeRequest.current?.query === query ? activeRequest.current.promise : null,
    }) })
    poller.current = loop
    return () => { loop.stop(); if (poller.current === loop) poller.current = null }
  }, [polling, paused, query, terminal, singleAgent, refresh])

  return {
    runId: query.runId || null,
    // FINISHED를 성공으로 변환하지 않고 verdict와 Step의 전체 필드를 그대로 반환합니다.
    run: current.run.data,
    steps: current.steps.data?.steps ?? null,
    events: eventPage?.events ?? null,
    architecture: current.architecture.data?.architecture ?? null,
    pagination: { total: eventPage?.total ?? null, limit: eventPage?.limit ?? limit, offset: eventPage?.offset ?? offset },
    status: { run: current.run.status, steps: current.steps.status, events: current.events.status, architecture: current.architecture.status },
    errors: { run: current.run.error, steps: current.steps.error, events: current.events.error, architecture: current.architecture.error },
    isLoading: resourceNames.some((name) => current[name].status === 'loading'),
    refresh,
    cancel: pausePolling,
    pausePolling,
    resumePolling,
    polling: {
      state: !query.runId ? 'idle' : !polling ? 'inactive' : paused ? 'paused' : terminal ? 'finished'
        : singleAgent ? 'not_multi' : current.failures ? 'retrying' : 'active',
      delayMs: pollingDelay(current.failures),
    },
  }
}
