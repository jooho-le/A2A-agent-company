import { useRef, useState } from 'react'
import './PipelinePage.css'

// 시나리오와 역할 설명은 고정 예시이며, 미연동 카드에는 실행 결과를 넣지 않습니다.
const mockPipeline = {
  scenario: '회원가입 기능 구현',
  request: '회원가입 기능을 구현해주세요.',
  agents: [
    {
      id: 'planner',
      name: 'Planner Agent',
      task: '회원가입 요구사항과 구현 계획 정리',
    },
    {
      id: 'developer',
      name: 'Developer Agent',
      task: 'Planner의 계획을 바탕으로 회원가입 코드 작성',
    },
    {
      id: 'qa',
      name: 'QA Agent',
      task: '구현 결과의 동작과 요구사항 검증',
    },
    {
      id: 'security',
      name: 'Security Agent',
      task: '검증 결과를 바탕으로 보안 점검',
    },
  ],
  result: 'Artifact API 미연동: 실제 실행 결과는 아직 조회하지 않습니다.',
  error: 'Issue·Tool API 미연동: 실제 실행 오류 여부를 확인하지 않았습니다.',
  revision: 'Issue API 미연동: 실제 수정 요청은 아직 조회하지 않습니다.',
}

// API 상태값은 그대로 표시하고, 기존 카드 색상에만 연결합니다.
const stepStatusStyles = {
  PENDING: 'waiting',
  RUNNING: 'running',
  SUCCEEDED: 'completed',
  FAILED: 'failed',
  WAITING_INPUT: 'waiting',
  CANCELED: 'waiting',
}

const stepStatusLabels = {
  PENDING: '실행 대기',
  RUNNING: '실행 중',
  SUCCEEDED: '작업 완료',
  FAILED: '작업 실패',
  WAITING_INPUT: '입력·인증 대기',
  CANCELED: '취소됨',
}

function agentNames(agentSteps) {
  return [...new Set(agentSteps.map((step) => (
    mockPipeline.agents.find((agent) => agent.id.toUpperCase() === step.agentRole)?.name ?? step.agentRole
  )))].join(', ')
}

function PipelinePage({ onBack }) {
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [runStatus, setRunStatus] = useState(null)
  const [isLoadingRun, setIsLoadingRun] = useState(false)
  const [runStatusErrorMessage, setRunStatusErrorMessage] = useState('')
  const [isRefreshing, setIsRefreshing] = useState(false)
  const requestInProgress = useRef(false)
  const [steps, setSteps] = useState(null)
  const [isLoadingSteps, setIsLoadingSteps] = useState(false)
  const [stepsErrorMessage, setStepsErrorMessage] = useState('')
  const [events, setEvents] = useState(null)
  const [eventTotal, setEventTotal] = useState(0)
  const [isLoadingEvents, setIsLoadingEvents] = useState(false)
  const [eventsErrorMessage, setEventsErrorMessage] = useState('')

  async function loadRunStatus(runId) {
    setIsLoadingRun(true)
    setRunStatusErrorMessage('')
    setRunStatus(null)

    try {
      const response = await fetch(`/api/v1/runs/${runId}`)
      const data = await response.json().catch(() => null)

      if (!response.ok) {
        setRunStatusErrorMessage(
          typeof data?.detail === 'string' && data.detail
            ? data.detail : `Run 상태를 조회하지 못했습니다. (HTTP ${response.status})`,
        )
        return
      }

      // GET /runs/{runId}는 감싸는 객체 없이 Run 자체를 반환합니다.
      if (data?.runId !== runId || typeof data?.status !== 'string' || !data.status
        || !(data.verdict === null || ['SUCCESS', 'FAIL', 'UNVERIFIED', 'HUMAN_REVIEW'].includes(data.verdict))
        || !(data.codeVersion === null || (Number.isInteger(data.codeVersion) && data.codeVersion >= 1))
        || !Number.isInteger(data.fixAttempt) || data.fixAttempt < 0) {
        setRunStatusErrorMessage('서버 응답에서 Run 상태를 확인할 수 없습니다.')
        return
      }
      setRunStatus(data)
    } catch {
      setRunStatusErrorMessage('서버에 연결할 수 없어 Run 상태를 조회하지 못했습니다.')
    } finally {
      setIsLoadingRun(false)
    }
  }

  // 조회 실패는 Run 생성 실패와 구분하고, 새 조회에서는 이전 오류·결과를 비웁니다.
  async function loadSteps(runId) {
    setIsLoadingSteps(true)
    setStepsErrorMessage('')
    setSteps(null)

    try {
      const response = await fetch(`/api/v1/runs/${runId}/steps`)
      const data = await response.json().catch(() => null)

      if (!response.ok) {
        setStepsErrorMessage(
          typeof data?.detail === 'string' && data.detail
            ? data.detail
            : `Step 목록을 조회하지 못했습니다. (HTTP ${response.status})`,
        )
        return
      }

      if (data?.runId !== runId || !Array.isArray(data?.steps)) {
        setStepsErrorMessage('서버 응답에서 Step 목록을 확인할 수 없습니다.')
        return
      }

      setSteps(data.steps)
    } catch {
      setStepsErrorMessage('서버에 연결할 수 없어 Step 목록을 조회하지 못했습니다.')
    } finally {
      setIsLoadingSteps(false)
    }
  }

  // Run 생성 후 또는 수동 새로고침 때만 조회하고 자동 polling은 사용하지 않습니다.
  async function loadEvents(runId) {
    setIsLoadingEvents(true)
    setEventsErrorMessage('')
    setEvents(null)
    setEventTotal(0)

    try {
      const response = await fetch(`/api/v1/runs/${runId}/events`)
      const data = await response.json().catch(() => null)

      if (!response.ok) {
        setEventsErrorMessage(
          typeof data?.detail === 'string' && data.detail
            ? data.detail
            : `실행 이벤트를 조회하지 못했습니다. (HTTP ${response.status})`,
        )
        return
      }

      if (data?.runId !== runId || !Array.isArray(data?.events) || !Number.isInteger(data?.total) || data.total < 0) {
        setEventsErrorMessage('서버 응답에서 실행 이벤트 목록을 확인할 수 없습니다.')
        return
      }

      setEvents(data.events)
      setEventTotal(data.total)
    } catch {
      setEventsErrorMessage('서버에 연결할 수 없어 실행 이벤트를 조회하지 못했습니다.')
    } finally {
      setIsLoadingEvents(false)
    }
  }

  async function loadRunData(runId) {
    // 서로 독립적인 조회이므로 하나가 실패해도 다른 조회는 완료합니다.
    await Promise.all([loadRunStatus(runId), loadSteps(runId), loadEvents(runId)])
  }

  async function handleRefresh() {
    if (!runResult || requestInProgress.current) return
    requestInProgress.current = true
    setIsRefreshing(true)
    try {
      await loadRunData(runResult.run.runId)
    } finally {
      setIsRefreshing(false)
      requestInProgress.current = false
    }
  }

  // 화면 진입 시 자동 실행하지 않고 버튼 클릭으로만 Run을 생성합니다.
  async function handleRun() {
    if (requestInProgress.current) return
    requestInProgress.current = true

    setIsSubmitting(true)
    setRunResult(null)
    setErrorMessage('')
    setRunStatus(null)
    setRunStatusErrorMessage('')
    setSteps(null)
    setStepsErrorMessage('')
    setEvents(null)
    setEventTotal(0)
    setEventsErrorMessage('')

    try {
      const response = await fetch('/api/v1/runs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          scenarioId: 'f7f9e5c3-ffc3-4b3f-918b-21e1b956ce76',
          requestText: '회원가입 기능을 구현해주세요.',
        }),
      })
      // Proxy 오류처럼 JSON이 아닌 응답도 HTTP 실패 메시지로 표시합니다.
      const data = await response.json().catch(() => null)

      if (!response.ok) {
        const detail = Array.isArray(data?.detail)
          ? data.detail.map((error) => error.msg).filter(Boolean).join(' ')
          : data?.detail
        setErrorMessage(
          typeof detail === 'string' && detail
            ? detail
            : `시나리오 실행 요청에 실패했습니다. (HTTP ${response.status})`,
        )
        return
      }

      if (!data?.run?.runId || !data?.firstStep?.agentRole || !data?.firstStep?.status || !data?.dispatchStatus) {
        setErrorMessage('서버 응답에서 Run 생성 결과를 확인할 수 없습니다.')
        return
      }

      setRunResult(data)
      await loadRunData(data.run.runId)
    } catch {
      setErrorMessage('서버에 연결할 수 없습니다.')
    } finally {
      setIsSubmitting(false)
      requestInProgress.current = false
    }
  }

  const infoCards = [
    { title: '실행 결과', value: mockPipeline.result },
    { title: '오류', value: mockPipeline.error },
    { title: '수정 요청', value: mockPipeline.revision },
  ]

  const runningSteps = steps?.filter((step) => step.status === 'RUNNING') ?? []
  const inactiveStepGroups = Object.entries(stepStatusLabels)
    .filter(([status]) => status !== 'RUNNING')
    .map(([status, label]) => ({
      status, label, agentSteps: steps?.filter((step) => step.status === status) ?? [],
    }))
    .filter((group) => group.agentSteps.length > 0)

  const emptyStepStatus = isLoadingSteps
    ? 'Step 조회 중...'
    : stepsErrorMessage
      ? 'Step 조회 실패'
      : steps === null
        ? '아직 조회하지 않음'
        : '아직 Step 미생성'

  return (
    <main className="pipeline-page">
      <header className="pipeline-header">
        <div>
          <span className="pipeline-mock-label">A2A Agent Company · 실제 Run / Step / Trace 조회</span>
          <h1>A2A Agent Pipeline</h1>
          <p>시나리오: {mockPipeline.scenario}</p>
        </div>
        <button type="button" className="app-button" onClick={onBack}>
          시나리오 선택으로 돌아가기
        </button>
      </header>

      <section className="pipeline-card pipeline-request" aria-labelledby="pipeline-request-title">
        <h2 id="pipeline-request-title">사용자 요청</h2>
        <p>{mockPipeline.request}</p>
      </section>

      <section className="pipeline-card pipeline-run" aria-labelledby="pipeline-run-title">
        <h2 id="pipeline-run-title">실제 Run 생성</h2>
        <button type="button" className="app-button" onClick={handleRun} disabled={isSubmitting || isRefreshing}>
          {isSubmitting ? '실행 중...' : '실제 시나리오 실행'}
        </button>
        {errorMessage && <p className="pipeline-run-error" role="alert">Run 생성 오류: {errorMessage}</p>}
        <div role="status">
          {runResult && (
            <>
              <p>Run 생성 요청이 접수되었습니다.</p>
              <dl className="pipeline-run-result">
                <dt>runId</dt>
                <dd>{runResult.run.runId}</dd>
                <dt>firstStep.agentRole</dt>
                <dd>{runResult.firstStep.agentRole}</dd>
                <dt>firstStep.status (생성 당시)</dt>
                <dd>{runResult.firstStep.status}</dd>
                <dt>dispatchStatus</dt>
                <dd>{runResult.dispatchStatus}</dd>
              </dl>
            </>
          )}
        </div>
      </section>

      <section className="pipeline-card pipeline-run" aria-labelledby="pipeline-run-status-title">
        <div className="pipeline-agent-heading">
          <h2 id="pipeline-run-status-title">현재 Run 상태 · 실제 데이터</h2>
          <button
            type="button"
            className="app-button"
            onClick={handleRefresh}
            disabled={!runResult || isSubmitting || isRefreshing}
          >
            {isRefreshing ? '새로고침 중...' : 'Run · Step · Trace 새로고침'}
          </button>
        </div>
        {isLoadingRun && <p role="status">Run 상태를 조회하고 있습니다.</p>}
        {runStatusErrorMessage && (
          <p className="pipeline-run-error" role="alert">Run 상태 조회 오류: {runStatusErrorMessage}</p>
        )}
        {!isLoadingRun && !runStatusErrorMessage && !runStatus && <p>Run 생성 후 현재 상태를 조회합니다.</p>}
        {runStatus && (
          <dl className="pipeline-run-result">
            <dt>Run ID</dt><dd>{runStatus.runId}</dd>
            <dt>현재 Run 상태 (status)</dt><dd>{runStatus.status}</dd>
            <dt>최종 판정 (verdict)</dt><dd>{runStatus.verdict ?? '아직 최종 판정 없음'}</dd>
            <dt>코드 버전 (codeVersion)</dt><dd>{runStatus.codeVersion ?? '아직 코드 버전 없음'}</dd>
            <dt>수정 시도 횟수 (fixAttempt)</dt><dd>{runStatus.fixAttempt}</dd>
          </dl>
        )}
        <p>FINISHED는 실행 종료 상태입니다. 성공 여부는 최종 판정(verdict)과 구분하여 확인해주세요.</p>
      </section>

      <p className="pipeline-description">
        Run 상태, Agent 진행 상태와 Trace는 생성 직후와 통합 새로고침 시 조회합니다.
        QA와 Security는 동시에 실행될 수 있습니다.
        실행 결과, 오류, 수정 요청 카드는 아직 해당 API와 미연동 상태입니다.
      </p>

      <div className="pipeline-layout">
        <section aria-labelledby="pipeline-agents-title">
          <h2 id="pipeline-agents-title">Agent 진행 상태 · 실제 데이터</h2>
          {isLoadingSteps && <p role="status">Step 목록을 조회하고 있습니다.</p>}
          {stepsErrorMessage && (
            <p className="pipeline-run-error" role="alert">Step 조회 오류: {stepsErrorMessage}</p>
          )}
          {!isLoadingSteps && !stepsErrorMessage && steps?.length === 0 && (
            <p>아직 생성된 Step이 없습니다.</p>
          )}
          <ol className="pipeline-agents">
            {mockPipeline.agents.map((agent, index) => {
              const agentRole = agent.id.toUpperCase()
              const agentSteps = steps?.filter((step) => step.agentRole === agentRole) ?? []
              const isRunning = agentSteps.some((step) => step.status === 'RUNNING')

              return (
                <li key={agent.id} aria-current={isRunning ? 'step' : undefined}>
                  <div className={`pipeline-card pipeline-agent${isRunning ? ' pipeline-agent--running' : ''}`}>
                    <div className="pipeline-agent-heading">
                      <h3>{agent.name}</h3>
                      {agentSteps.length === 0 && (
                        <span className="pipeline-status pipeline-status--waiting">{emptyStepStatus}</span>
                      )}
                    </div>
                    <p>역할 설명 (예시): {agent.task}</p>
                    {agentSteps.length === 0 && <p>agentRole: {agentRole}</p>}
                    {agentSteps.map((step) => (
                      <div className="pipeline-step" key={step.workflowStepId}>
                        <span className={`pipeline-status pipeline-status--${stepStatusStyles[step.status] ?? 'waiting'}`}>
                          {step.status}
                        </span>
                        <dl className="pipeline-run-result">
                          <dt>agentRole</dt>
                          <dd>{step.agentRole}</dd>
                          <dt>status</dt>
                          <dd>{step.status}</dd>
                          <dt>attempt</dt>
                          <dd>{step.attempt}</dd>
                          <dt>a2aTaskState</dt>
                          <dd>{step.a2aTaskState ?? '-'}</dd>
                        </dl>
                      </div>
                    ))}
                  </div>
                  {index < mockPipeline.agents.length - 1 && (
                    <div className="pipeline-arrow" aria-hidden="true">↓</div>
                  )}
                </li>
              )
            })}
          </ol>
        </section>

        <section aria-labelledby="pipeline-info-title">
          <h2 id="pipeline-info-title">실행 정보와 Trace</h2>
          <div className="pipeline-info">
            <article className="pipeline-card" aria-labelledby="pipeline-current-agent-title">
              <h3 id="pipeline-current-agent-title">현재 Agent · 실제 Step 기준</h3>
              {isLoadingSteps ? (
                <p role="status">현재 Agent를 조회하고 있습니다.</p>
              ) : stepsErrorMessage ? (
                <p>Step 조회에 실패하여 현재 Agent를 확인할 수 없습니다.</p>
              ) : steps === null ? (
                <p>Run 생성 후 현재 Agent를 조회합니다.</p>
              ) : steps.length === 0 ? (
                <p>아직 생성된 Step이 없습니다. 현재 실행 중인 Agent가 없습니다.</p>
              ) : runningSteps.length > 0 ? (
                <p>실행 중 (RUNNING): {agentNames(runningSteps)}</p>
              ) : (
                <>
                  <p>현재 RUNNING 상태인 Agent가 없습니다.</p>
                  <p>조회된 Step 상태 요약 (이전 단계 포함)</p>
                  <ul className="pipeline-agent-summary">
                    {inactiveStepGroups.map((group) => (
                      <li key={group.status}>{group.label} ({group.status}): {agentNames(group.agentSteps)}</li>
                    ))}
                  </ul>
                  <p>Step 작업 완료와 Run의 최종 성공 판정은 별개입니다.</p>
                </>
              )}
            </article>
            {infoCards.map((card) => (
              <article className="pipeline-card" key={card.title}>
                <h3>{card.title} (API 미연동)</h3>
                <p>{card.value}</p>
              </article>
            ))}
            <article className="pipeline-card" aria-labelledby="pipeline-trace-title">
              <div className="pipeline-agent-heading">
                <h3 id="pipeline-trace-title">Trace · 실제 이벤트</h3>
              </div>
              {isLoadingEvents && <p role="status">실행 이벤트를 조회하고 있습니다.</p>}
              {eventsErrorMessage && (
                <p className="pipeline-run-error" role="alert">Trace 조회 오류: {eventsErrorMessage}</p>
              )}
              {!isLoadingEvents && !eventsErrorMessage && events === null && (
                <p>Run 생성 후 실행 이벤트를 조회합니다.</p>
              )}
              {!isLoadingEvents && !eventsErrorMessage && events?.length === 0 && (
                <p>아직 기록된 실행 이벤트가 없습니다.</p>
              )}
              {events?.length > 0 && (
                <>
                  <p>전체 {eventTotal}건 · 처음 {events.length}건 표시</p>
                  <ol className="pipeline-trace">
                    {events.map((event) => (
                      <li key={event.eventId}>
                        <strong className="pipeline-event-type">{event.eventType}</strong>
                        <time className="pipeline-event-time" dateTime={event.occurredAt}>
                          {new Date(event.occurredAt).toLocaleString('ko-KR')}
                        </time>
                        <dl className="pipeline-run-result">
                          <dt>담당자</dt>
                          <dd>{event.actor}</dd>
                          <dt>Run 상태</dt>
                          <dd>{event.workflowState ?? '-'}</dd>
                          <dt>A2A Task 상태</dt>
                          <dd>{event.a2aTaskState ?? '-'}</dd>
                          <dt>시도 번호</dt>
                          <dd>{event.attempt}</dd>
                          {event.workflowStepId && (
                            <><dt>Step ID</dt><dd>{event.workflowStepId}</dd></>
                          )}
                          {event.a2aTaskId && (
                            <><dt>Task ID</dt><dd>{event.a2aTaskId}</dd></>
                          )}
                        </dl>
                      </li>
                    ))}
                  </ol>
                </>
              )}
            </article>
          </div>
        </section>
      </div>
    </main>
  )
}

export default PipelinePage
