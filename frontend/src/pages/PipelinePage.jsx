import { useState } from 'react'
import './PipelinePage.css'

// 시나리오와 역할 설명은 고정 예시이며, 실행 정보 카드만 Mock Data입니다.
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
  currentAgent: 'Developer Agent',
  result: 'Planner Agent가 구현 계획을 정리했습니다. Developer Agent가 회원가입 구현을 진행 중입니다.',
  error: '현재 오류가 없습니다.',
  revision: '현재 수정 요청이 없습니다.',
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

function PipelinePage({ onBack }) {
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [errorMessage, setErrorMessage] = useState('')
  const [steps, setSteps] = useState(null)
  const [isLoadingSteps, setIsLoadingSteps] = useState(false)
  const [stepsErrorMessage, setStepsErrorMessage] = useState('')
  const [events, setEvents] = useState(null)
  const [eventTotal, setEventTotal] = useState(0)
  const [isLoadingEvents, setIsLoadingEvents] = useState(false)
  const [eventsErrorMessage, setEventsErrorMessage] = useState('')

  // Run 생성 성공 뒤 한 번만 조회하며, 조회 실패는 생성 실패와 구분합니다.
  async function loadSteps(runId) {
    setIsLoadingSteps(true)

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
    if (isLoadingEvents) return

    setIsLoadingEvents(true)
    setEventsErrorMessage('')

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

  // 화면 진입 시 자동 실행하지 않고 버튼 클릭으로만 Run을 생성합니다.
  async function handleRun() {
    if (isSubmitting || isLoadingEvents) return

    setIsSubmitting(true)
    setRunResult(null)
    setErrorMessage('')
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
      await loadSteps(data.run.runId)
      await loadEvents(data.run.runId)
    } catch {
      setErrorMessage('서버에 연결할 수 없습니다.')
    } finally {
      setIsSubmitting(false)
    }
  }

  const infoCards = [
    { title: '현재 Agent', value: mockPipeline.currentAgent },
    { title: '실행 결과', value: mockPipeline.result },
    { title: '오류', value: mockPipeline.error },
    { title: '수정 요청', value: mockPipeline.revision },
  ]

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
        <button type="button" className="app-button" onClick={handleRun} disabled={isSubmitting || isLoadingEvents}>
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
                <dt>firstStep.status</dt>
                <dd>{runResult.firstStep.status}</dd>
                <dt>dispatchStatus</dt>
                <dd>{runResult.dispatchStatus}</dd>
              </dl>
            </>
          )}
        </div>
      </section>

      <p className="pipeline-description">
        Agent 진행 상태와 Trace는 Run 생성 후 실제 API 데이터를 한 번 조회하여 표시합니다.
        현재 Agent, 실행 결과, 오류, 수정 요청 카드는 고정 Mock Data입니다.
      </p>

      <div className="pipeline-layout">
        <section aria-labelledby="pipeline-agents-title">
          <h2 id="pipeline-agents-title">Agent 진행 상태 · 실제 데이터</h2>
          {isLoadingSteps && <p role="status">Step 목록을 조회하고 있습니다.</p>}
          {stepsErrorMessage && (
            <p className="pipeline-run-error" role="alert">Step 조회 오류: {stepsErrorMessage}</p>
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
            {infoCards.map((card) => (
              <article className="pipeline-card" key={card.title}>
                <h3>{card.title} (Mock Data)</h3>
                <p>{card.value}</p>
              </article>
            ))}
            <article className="pipeline-card" aria-labelledby="pipeline-trace-title">
              <div className="pipeline-agent-heading">
                <h3 id="pipeline-trace-title">Trace · 실제 이벤트</h3>
                <button
                  type="button"
                  className="app-button"
                  onClick={() => loadEvents(runResult.run.runId)}
                  disabled={!runResult || isSubmitting || isLoadingEvents}
                >
                  {isLoadingEvents ? '조회 중...' : 'Trace 새로고침'}
                </button>
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
