import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { getAgentRun, getAgentRunSteps, getAgentRunEvents, getAgentRunArchitecture, getAgentRunArtifacts, getAgentRunIssues } from '../api/agentRuns'
import { canRetryRunRead, createRunPolling, isTerminalRun, maxPollingRetries, pollingDelay } from './agentRunPolling'
import { createRunEventFeed } from './agentRunEventFeed'

const resourceNames = ['run', 'steps', 'events', 'architecture', 'trace']
const resultNames = ['artifacts', 'issues']
const allResources = [...resourceNames, ...resultNames]

function initialState(query) {
  const resource = () => ({ data: null, status: query.runId ? 'loading' : 'idle', error: null })
  return { query, requestId: null, failures: 0, blocked: false, run: resource(), steps: resource(), events: resource(),
    architecture: query.includeArchitecture ? resource() : { data: null, status: 'idle', error: null },
    trace: query.collectEvents ? resource() : { data: null, status: 'idle', error: null },
    ...Object.fromEntries(resultNames.map(name => [name, query.includeResults ? resource() : { data: null, status: 'idle', error: null }])) }
}

function reducer(state, action) {
  if (action.type === 'start') {
    const next = { ...initialState(action.query), requestId: action.requestId }
    if (state.query === action.query) {
      next.failures = action.automatic ? state.failures : 0
      for (const name of allResources) next[name].data = state[name].data
    }
    return next
  }
  if (state.query !== action.query || state.requestId !== action.requestId) return state

  if (action.type === 'settled') {
    const value = action.value.status === 'error' || action.value.status === 'cancelled'
      ? { ...action.value, data: state[action.resource].data } : action.value
    return { ...state, [action.resource]: value }
  }
  if (action.type === 'complete') return { ...state, failures: action.failures, blocked: action.blocked }
  if (action.type === 'cancel') {
    const next = { ...state }
    for (const name of allResources) {
      if (state[name].status === 'loading') {
        next[name] = { ...state[name], status: 'cancelled', error: null }
      }
    }
    return next
  }
  return state
}

/**
 * Run ID가 없으면 조회하지 않습니다. polling을 켠 실제 화면에서만 완료 후 2초 간격으로 조회합니다.
 * 일시 오류는 2·4·8초 최대 3회 재시도하고, 인증/접근 오류는 자동 조회를 중단합니다.
 * includeArchitecture를 켠 화면은 공개 configuration에서 architecture만 함께 가져옵니다.
 * status는 조회 상태(idle/loading/success/empty/error/cancelled)이고 Run의 실행 status와 별개입니다.
 * refresh()는 진행 중인 같은 조회를 재사용합니다. cancel() 뒤 refresh()로 다시 조회할 수 있습니다.
 */
export default function useAgentRunMonitor(runId, { limit = 100, offset = 0, includeArchitecture = false, polling = false, collectEvents = false, includeResults = collectEvents, traceFromStart = false } = {}) {
  const normalizedRunId = typeof runId === 'string' ? runId.trim() : ''
  const query = useMemo(() => ({ runId: normalizedRunId, limit, offset, includeArchitecture, collectEvents, includeResults, traceFromStart }), [normalizedRunId, limit, offset, includeArchitecture, collectEvents, includeResults, traceFromStart])
  const [state, dispatch] = useReducer(reducer, query, initialState)
  const activeRequest = useRef(null)
  const sequence = useRef(0)
  const activeQuery = useRef(null)
  const poller = useRef(null)
  const lastResult = useRef(null)
  const [eventFeed] = useState(createRunEventFeed)
  const [pausedRun, setPausedRun] = useState(null)
  const paused = Boolean(query.runId) && pausedRun === query.runId

  const refresh = useCallback(({ automatic = false } = {}) => {
    if (activeQuery.current !== query || !query.runId) return Promise.resolve()
    if (activeRequest.current?.query === query) return activeRequest.current.promise
    if (!automatic) lastResult.current = null

    const controller = new AbortController()
    const requestId = ++sequence.current
    const request = { query, requestId, controller, promise: null, results: {}, failed: false, blocked: false, accessError: null }
    activeRequest.current = request
    dispatch({ type: 'start', query, requestId, automatic })

    const isCurrent = () => activeQuery.current === query && activeRequest.current === request && !controller.signal.aborted
    let runRequest, traceRequest
    const readRun = () => runRequest ??= getAgentRun(query.runId, { signal: controller.signal })
    const readTrace = () => traceRequest ??= (async () => {
      // 종료 Run의 마지막 이벤트도 포함하도록 Run 응답 후에 로그를 수집합니다.
      await readRun()
      return eventFeed.pull(query.runId, getAgentRunEvents, controller.signal, { fromStart: query.traceFromStart })
    })()
    const readyForResource = async () => {
      await readRun()
      if (query.collectEvents) {
        try { await readTrace() } catch (error) {
          if ([401, 403, 404].includes(error?.status)) throw error
          // 일시적인 Trace 오류가 독립적인 Step/결과 조회를 막지는 않습니다.
        }
      }
      if (request.accessError) throw request.accessError
      controller.signal.throwIfAborted()
    }
    const loaders = {
      run: readRun,
      steps: async () => {
        // 새 Event보다 먼저 읽은 Step이 오래된 상태가 되지 않도록 뒤에서 조회합니다.
        // Trace 조회 실패가 별도의 Step 조회까지 막지는 않습니다.
        await readyForResource()
        return getAgentRunSteps(query.runId, { signal: controller.signal })
      },
      // 3D Trace 수집 시 같은 GET/커서를 공유합니다. 과거 offset 페이지를 매번 다시 요청하지 않습니다.
      events: async () => {
        if (query.collectEvents) return (await readTrace()).page
        await readRun()
        return getAgentRunEvents(query.runId, { limit: query.limit, offset: query.offset, signal: controller.signal })
      },
      architecture: async () => {
        await readRun()
        return getAgentRunArchitecture(query.runId, { signal: controller.signal })
      },
      trace: readTrace,
      ...Object.fromEntries(resultNames.map(name => [name, async () => {
        // Trace 수집 후 결과를 조회해 새 이벤트가 참조하는 산출물을 함께 받습니다.
        await readyForResource()
        return (name === 'artifacts' ? getAgentRunArtifacts : getAgentRunIssues)(query.runId, { signal: controller.signal })
      }])),
    }

    const enabled = allResources.filter(name => (name !== 'architecture' || query.includeArchitecture)
      && (name !== 'trace' || query.collectEvents) && (!resultNames.includes(name) || query.includeResults))
    request.promise = Promise.all(enabled.map(async (name) => {
      try {
        const data = await loaders[name]()
        if (!isCurrent()) return
        request.results[name] = data
        const empty = ['steps', 'events', ...resultNames].includes(name) && data[name].length === 0
        dispatch({ type: 'settled', query, requestId, resource: name,
          value: { data, status: empty ? 'empty' : 'success', error: null } })
      } catch (error) {
        if (!isCurrent()) return
        const cancelled = error?.name === 'AbortError'
        request.failed = true
        if (!canRetryRunRead(error)) request.blocked = true
        if ([401, 403, 404].includes(error?.status)) request.accessError = error
        // 원본 오류의 status·code·detail을 유지하고 다른 조회 결과는 계속 받습니다.
        dispatch({ type: 'settled', query, requestId, resource: name,
          value: { data: null, status: cancelled ? 'cancelled' : 'error', error: cancelled ? null : error } })
      }
    })).then(() => {
      if (isCurrent()) {
        const previous = lastResult.current?.query === query ? lastResult.current : null
        const failures = request.failed ? (previous?.failures ?? 0) + 1 : 0
        lastResult.current = { query, failures, blocked: request.blocked,
          terminal: (isTerminalRun(request.results.run?.status) || Boolean(previous?.terminal))
            && (!query.collectEvents || !request.failed && Boolean(request.results.trace?.initialized)),
          architecture: request.results.architecture?.architecture ?? previous?.architecture ?? null }
        dispatch({ type: 'complete', query, requestId, failures, blocked: request.blocked })
      }
      if (activeRequest.current === request) activeRequest.current = null
    })
    poller.current?.waitFor(request.promise)
    return request.promise
  }, [query, eventFeed])

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
    if (!query.collectEvents || !query.runId) eventFeed.reset()
    void refresh()
    return () => {
      if (activeQuery.current === query) activeQuery.current = null
      const request = activeRequest.current
      if (request?.query === query) {
        activeRequest.current = null
        request.controller.abort()
      }
    }
  }, [query, refresh, eventFeed])

  // 목업 복귀·화면 이탈 시 진행 중인 GET도 취소합니다. 최종 상태 확인 중인
  // 같은 조회는 끝까지 받아 Step·Event·세리머니의 마지막 응답을 보존합니다.
  useEffect(() => {
    if (!polling) return
    return abortRequest
  }, [polling, abortRequest])

  // effect가 정리되기 전에도 다른 Run·페이지의 이전 데이터를 표시하지 않습니다.
  const current = state.query === query ? state : initialState(query)
  const eventPage = current.events.data
  const terminal = isTerminalRun(current.run.data?.status) && (!collectEvents || allResources
    .filter(name => (name !== 'architecture' || includeArchitecture) && (!resultNames.includes(name) || includeResults))
    .every(name => ['success', 'empty'].includes(current[name].status)))
  const singleAgent = current.architecture.data?.architecture === 'SINGLE_AGENT'
  const halted = current.blocked || current.failures > maxPollingRetries
  useEffect(() => {
    if (!polling || paused || !query.runId || terminal || singleAgent || halted) return
    const loop = createRunPolling({ refresh, getState: () => ({
      ...(lastResult.current?.query === query ? lastResult.current : {}),
      pending: activeRequest.current?.query === query ? activeRequest.current.promise : null,
    }) })
    poller.current = loop
    return () => { loop.stop(); if (poller.current === loop) poller.current = null }
  }, [polling, paused, query, terminal, singleAgent, halted, refresh])

  return {
    runId: query.runId || null,
    // FINISHED를 성공으로 변환하지 않고 verdict와 Step의 전체 필드를 그대로 반환합니다.
    run: current.run.data,
    steps: current.steps.data?.steps ?? null,
    events: eventPage?.events ?? null,
    architecture: current.architecture.data?.architecture ?? null,
    trace: current.trace.data,
    // 부가 결과의 실패는 기존 Trace·성공 판정의 errors/queryStatus에 섞지 않습니다.
    artifactCatalog: { runId: query.runId, items: current.artifacts.data?.catalog ?? [], status: current.artifacts.status },
    popupResults: { runId: query.runId, artifacts: current.artifacts.data?.artifacts ?? [], issues: current.issues.data?.issues ?? [],
      status: Object.fromEntries(resultNames.map(name => [name, current[name].status])) },
    pagination: { total: eventPage?.total ?? null, limit: eventPage?.limit ?? limit, offset: eventPage?.offset ?? offset },
    status: { run: current.run.status, steps: current.steps.status, events: current.events.status, architecture: current.architecture.status, trace: current.trace.status },
    errors: { run: current.run.error, steps: current.steps.error, events: current.events.error, architecture: current.architecture.error, trace: current.trace.error },
    readErrors: Object.fromEntries(allResources.map(name => [name, current[name].error])),
    isLoading: resourceNames.some((name) => current[name].status === 'loading'),
    refresh,
    cancel: pausePolling,
    pausePolling,
    resumePolling,
    polling: {
      state: !query.runId ? 'idle' : !polling ? 'inactive' : paused ? 'paused' : terminal ? 'finished'
        : singleAgent ? 'not_multi' : current.blocked ? 'blocked' : halted ? 'exhausted' : current.failures ? 'retrying' : 'active',
      delayMs: pollingDelay(current.failures),
    },
  }
}
