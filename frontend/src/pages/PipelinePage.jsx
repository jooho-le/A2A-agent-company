import { useState } from 'react'
import './PipelinePage.css'

// 아래 Agent 상태와 Trace는 협업 흐름을 보여주기 위한 고정 Mock Data입니다.
const mockPipeline = {
  scenario: '회원가입 기능 구현',
  request: '회원가입 기능을 구현해주세요.',
  agents: [
    {
      id: 'planner',
      name: 'Planner Agent',
      status: '완료',
      state: 'completed',
      task: '회원가입 요구사항과 구현 계획 정리',
    },
    {
      id: 'developer',
      name: 'Developer Agent',
      status: '진행 중',
      state: 'running',
      task: 'Planner의 계획을 바탕으로 회원가입 코드 작성',
    },
    {
      id: 'qa',
      name: 'QA Agent',
      status: '대기',
      state: 'waiting',
      task: '구현 결과의 동작과 요구사항 검증',
    },
    {
      id: 'security',
      name: 'Security Agent',
      status: '대기',
      state: 'waiting',
      task: '검증 결과를 바탕으로 보안 점검',
    },
  ],
  currentAgent: 'Developer Agent',
  result: 'Planner Agent가 구현 계획을 정리했습니다. Developer Agent가 회원가입 구현을 진행 중입니다.',
  error: '현재 오류가 없습니다.',
  revision: '현재 수정 요청이 없습니다.',
  trace: [
    '사용자 → Orchestrator: 회원가입 기능 구현 요청 전달',
    'Orchestrator → Planner Agent: 요구사항 분석과 계획 작성 요청',
    'Planner Agent → Developer Agent: 구현 계획 전달 완료',
    'Developer Agent: 구현 진행 중 · QA / Security Agent: 대기',
  ],
}

function PipelinePage({ onBack }) {
  const [isSubmitting, setIsSubmitting] = useState(false)
  const [runResult, setRunResult] = useState(null)
  const [errorMessage, setErrorMessage] = useState('')

  // 화면 진입 시 자동 실행하지 않고 버튼 클릭으로만 Run을 생성합니다.
  async function handleRun() {
    if (isSubmitting) return

    setIsSubmitting(true)
    setRunResult(null)
    setErrorMessage('')

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

  return (
    <main className="pipeline-page">
      <header className="pipeline-header">
        <div>
          <span className="pipeline-mock-label">Mock Data · 고정 시나리오</span>
          <h1>A2A Agent Pipeline</h1>
          <p>시나리오: {mockPipeline.scenario}</p>
        </div>
        <button type="button" className="app-button" onClick={onBack}>
          처음 화면으로 돌아가기
        </button>
      </header>

      <section className="pipeline-card pipeline-request" aria-labelledby="pipeline-request-title">
        <h2 id="pipeline-request-title">사용자 요청</h2>
        <p>{mockPipeline.request}</p>
      </section>

      <section className="pipeline-card pipeline-run" aria-labelledby="pipeline-run-title">
        <h2 id="pipeline-run-title">실제 Run 생성</h2>
        <button type="button" className="app-button" onClick={handleRun} disabled={isSubmitting}>
          {isSubmitting ? '실행 중...' : '실제 시나리오 실행'}
        </button>
        {errorMessage && <p className="pipeline-run-error" role="alert">{errorMessage}</p>}
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
        Orchestrator가 요청을 전달하고 Agent들이 순서대로 협업하는 A2A 흐름의 예시입니다.
        아래 Agent 진행 상태와 실행 정보는 실제 Run 생성 결과와 별개인 고정 Mock Data입니다.
      </p>

      <div className="pipeline-layout">
        <section aria-labelledby="pipeline-agents-title">
          <h2 id="pipeline-agents-title">Agent 진행 상태</h2>
          <ol className="pipeline-agents">
            {mockPipeline.agents.map((agent, index) => (
              <li key={agent.id} aria-current={agent.state === 'running' ? 'step' : undefined}>
                <div className={`pipeline-card pipeline-agent pipeline-agent--${agent.state}`}>
                  <div className="pipeline-agent-heading">
                    <h3>{agent.name}</h3>
                    <span className={`pipeline-status pipeline-status--${agent.state}`}>
                      {agent.status}
                    </span>
                  </div>
                  <p>{agent.task}</p>
                </div>
                {index < mockPipeline.agents.length - 1 && (
                  <div className="pipeline-arrow" aria-hidden="true">↓</div>
                )}
              </li>
            ))}
          </ol>
        </section>

        <section aria-labelledby="pipeline-info-title">
          <h2 id="pipeline-info-title">실행 정보</h2>
          <div className="pipeline-info">
            {infoCards.map((card) => (
              <article className="pipeline-card" key={card.title}>
                <h3>{card.title}</h3>
                <p>{card.value}</p>
              </article>
            ))}
            <article className="pipeline-card">
              <h3>Trace</h3>
              <ol className="pipeline-trace">
                {mockPipeline.trace.map((entry) => <li key={entry}>{entry}</li>)}
              </ol>
            </article>
          </div>
        </section>
      </div>
    </main>
  )
}

export default PipelinePage
