import { useEffect, useRef, useState } from 'react'
import './AgentRunMonitor.css'

const agents = [
  { role: 'PLANNER', label: '기획' },
  { role: 'DEVELOPER', label: '개발' },
  { role: 'QA', label: 'QA' },
  { role: 'SECURITY', label: '보안' },
]
const uuidV4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const queryLabels = { idle: '조회 전', loading: '조회 중', success: '조회 완료', empty: '빈 목록', error: '조회 실패', cancelled: '조회 취소' }
const stepLabels = { PENDING: '대기', RUNNING: '실행 중', SUCCEEDED: '단계 완료', FAILED: '단계 실패', WAITING_INPUT: '입력 대기', CANCELED: '취소됨' }
const dateFormat = new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })

function ErrorNotice({ label, error }) {
  if (!error) return null
  const network = error.code === 'NETWORK_ERROR'
  return <p className="arm-error" role="alert">
    <strong>{label} {network ? '연결 실패' : '조회 실패'}</strong>
    <span>{network ? '서버에 연결할 수 없습니다. 실행 주소와 네트워크를 확인해주세요.' : error.message}</span>
    {error.status && <small>HTTP {error.status}</small>}
    {error.code && error.code !== error.message && <code>{error.code}</code>}
  </p>
}

function formatTime(value) {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : dateFormat.format(date)
}

// 입력란 등 표시용 상태만 관리하며 실제 조회 결과와 제어 함수는 Dashboard에서 받습니다.
export default function AgentRunMonitor({ monitor, query, onSelectRun, onRefresh, onChangePage, onClear }) {
  const [draftId, setDraftId] = useState('')
  const [validation, setValidation] = useState('')
  const eventListRef = useRef(null)
  const { run, steps, events, pagination, status, errors, isLoading, cancel } = monitor

  useEffect(() => {
    if (eventListRef.current) eventListRef.current.scrollTop = 0
  }, [events])

  function submit(event) {
    event.preventDefault()
    const nextId = draftId.trim().toLowerCase()
    if (!uuidV4.test(nextId)) {
      setValidation('기존 Run의 올바른 UUIDv4를 입력해주세요.')
      return
    }
    setValidation('')
    setDraftId(nextId)
    onSelectRun(nextId)
  }
  function clear() {
    onClear()
    setDraftId('')
    setValidation('')
  }

  const running = agents.filter(agent => steps?.some(step => step.agentRole === agent.role && step.status === 'RUNNING'))
  const failed = Object.values(status).filter(value => value === 'error').length
  const cancelled = Object.values(status).some(value => value === 'cancelled')
  const hasData = run !== null || steps !== null || events !== null
  const finalSuccess = run?.status === 'FINISHED' && run.verdict === 'SUCCESS'
  const stepsLoaded = status.steps === 'success' || status.steps === 'empty'
  const eventRows = [...(events ?? [])].reverse()
  const maxOffset = Math.max(0, (pagination.total ?? 0) - pagination.limit)

  return <section className="agent-run-monitor" aria-labelledby="agent-run-monitor-title">
    <header className="arm-heading"><div><p className="eyebrow">ORCHESTRATOR / READ ONLY</p><h2 id="agent-run-monitor-title">실제 Run 조회</h2></div><span className="arm-source">실제 API 데이터</span></header>
    <p className="arm-intro">기존 멀티 에이전트 Run의 실행 기록을 조회합니다. 3D의 실제 Run 모드는 Step 상태를 표시하며, 목업 프로젝트·진행률·시연은 별도로 유지됩니다.</p>
    <form className="arm-form" onSubmit={submit} noValidate>
      <label htmlFor="agent-run-id">기존 Run UUID</label>
      <div className="arm-input-row"><input id="agent-run-id" value={draftId} onChange={event => { setDraftId(event.target.value); setValidation('') }} placeholder="기존 Run의 UUIDv4를 입력해주세요" autoComplete="off" spellCheck={false} aria-invalid={Boolean(validation)} aria-describedby="agent-run-input-note" /><button type="submit" className="button start" disabled={isLoading && draftId.trim().toLowerCase() === query.runId}>Run 조회</button></div>
      <p id="agent-run-input-note" className={validation ? 'arm-validation' : 'arm-note'} role={validation ? 'alert' : undefined}>{validation || '멀티 에이전트 Run UUID를 입력해주세요. 새 Run을 생성하지 않습니다.'}</p>
    </form>
    <div className="arm-toolbar"><div className="arm-query-status" role="status">{!query.runId ? 'Run을 선택하면 실행 상태를 확인할 수 있습니다.' : isLoading ? '실행 기록을 조회하고 있습니다.' : failed ? hasData ? '일부 조회에 실패했습니다. 성공한 결과는 아래에서 확인할 수 있습니다.' : '실행 기록을 조회하지 못했습니다.' : cancelled ? '진행 중이던 조회를 취소했습니다.' : '조회가 끝났습니다. 조회 완료는 Agent 실행 성공을 뜻하지 않습니다.'}</div><div className="arm-actions"><button type="button" className="button" onClick={onRefresh} disabled={!query.runId || isLoading}>전체 새로고침</button>{isLoading && <button type="button" className="button" onClick={cancel}>조회 취소</button>}<button type="button" className="button" onClick={clear} disabled={!query.runId && !draftId}>조회 해제</button></div></div>

    <section className="arm-section" aria-labelledby="arm-run-heading" aria-busy={status.run === 'loading'}>
      <div className="arm-section-heading"><h3 id="arm-run-heading">Run 실행 상태</h3><span>{queryLabels[status.run]}</span></div>
      <ErrorNotice label="Run" error={errors.run} />
      <ErrorNotice label="실행 방식" error={errors.architecture} />
      {monitor.architecture && <p className="arm-note">설정에서 확인한 실행 방식: {monitor.architecture}</p>}
      {run ? <>
        <dl className="arm-run-fields"><div><dt>Run ID</dt><dd><code>{run.runId}</code></dd></div><div><dt>실행 상태 (status)</dt><dd><code>{run.status}</code></dd></div><div data-final-success={finalSuccess}><dt>최종 판정 (verdict)</dt><dd><strong>{run.verdict ?? '아직 최종 판정 없음'}</strong></dd></div><div><dt>코드 버전</dt><dd>{run.codeVersion ?? '아직 코드 버전 없음'}</dd></div><div><dt>수정 시도</dt><dd>{run.fixAttempt}</dd></div></dl>
        <p className="arm-note">{finalSuccess ? '최종 성공이 확인되었습니다. 이번 단계에서는 3D 세리머니를 실행하지 않습니다.' : run.status === 'FINISHED' ? '실행이 종료되었습니다. FINISHED만으로 성공을 뜻하지 않으며 최종 판정을 확인해야 합니다.' : '전체 실행 상태와 최종 판정은 별개입니다. 아직 실행 중이거나 최종 확인이 필요할 수 있습니다.'}</p>
        {run.terminationReason && <p className="arm-note">종료 사유: {run.terminationReason}</p>}
      </> : !errors.run && <p className="arm-empty">{status.run === 'loading' ? 'Run 상태를 조회하고 있습니다.' : status.run === 'cancelled' ? 'Run 조회를 취소했습니다.' : '조회할 Run이 선택되지 않았습니다.'}</p>}
    </section>

    <section className="arm-section" aria-labelledby="arm-steps-heading" aria-busy={status.steps === 'loading'}>
      <div className="arm-section-heading"><h3 id="arm-steps-heading">Agent별 실제 Step</h3><span>{queryLabels[status.steps]}</span></div>
      <ErrorNotice label="Step" error={errors.steps} />
      <p className="arm-note">{running.length ? `현재 실행 중: ${running.map(agent => `${agent.label} (${agent.role})`).join(', ')}` : stepsLoaded ? '현재 RUNNING 상태인 Agent가 없습니다.' : 'Step 조회가 완료되면 현재 실행 중인 Agent를 확인할 수 있습니다.'}</p>
      {status.steps === 'empty' && <p className="arm-empty">아직 생성된 Step이 없습니다.</p>}
      <div className="arm-agent-grid">{agents.map(agent => {
        const agentSteps = steps?.filter(step => step.agentRole === agent.role) ?? []
        return <article key={agent.role} className="arm-agent" data-agent-role={agent.role} data-running={agentSteps.some(step => step.status === 'RUNNING')}><h4>{agent.label} Agent <small>{agent.role}</small></h4>{agentSteps.length ? <ul className="arm-step-list">{agentSteps.map(step => <li key={step.workflowStepId}>
          <span className="arm-step-status" data-step-status={step.status}>{stepLabels[step.status] ?? '상태 확인'} · <code>{step.status}</code></span>
          <dl><div><dt>Step ID</dt><dd><code>{step.workflowStepId}</code></dd></div><div><dt>시도 (attempt)</dt><dd>{step.attempt}</dd></div><div><dt>코드 버전</dt><dd>{step.codeVersion ?? '-'}</dd></div><div><dt>A2A 상태</dt><dd><code>{step.a2aTaskState ?? '-'}</code></dd></div></dl>
        </li>)}</ul> : <p className="arm-empty">{stepsLoaded ? '아직 Step 미생성' : queryLabels[status.steps]}</p>}</article>
      })}</div>
    </section>

    <section className="arm-section" aria-labelledby="arm-events-heading" aria-busy={status.events === 'loading'}>
      <div className="arm-section-heading"><h3 id="arm-events-heading">최근 Event</h3><span>{queryLabels[status.events]}</span></div>
      <ErrorNotice label="Event" error={errors.events} />
      {status.events === 'loading' || (query.followLatest && !errors.events && status.events !== 'cancelled') ? <p className="arm-empty">최신 이벤트 구간을 조회하고 있습니다.</p> : status.events === 'empty' ? <p className="arm-empty">{pagination.total === 0 ? '아직 기록된 실행 이벤트가 없습니다.' : '현재 조회 구간에 이벤트가 없습니다.'}</p> : eventRows.length > 0 ? <>
        <p className="arm-note">조회 구간 내 최신 기록부터 표시합니다. 시각은 한국 시간 기준입니다.</p>
        <ol ref={eventListRef} className="arm-event-list" tabIndex={0} aria-label="실행 이벤트 목록">{eventRows.map(event => {
          const step = steps?.find(item => item.workflowStepId === event.workflowStepId)
          return <li key={event.eventId}><div className="arm-event-heading"><code>{event.eventType}</code><time dateTime={event.occurredAt}>{formatTime(event.occurredAt)}</time></div><p>발생 주체: {event.actor}{step && <> · Agent 역할: {step.agentRole}</>}</p><p>Run 상태: <code>{event.workflowState ?? '-'}</code> · A2A 상태: <code>{event.a2aTaskState ?? '-'}</code> · 시도: {event.attempt}</p>{event.workflowStepId && <p>Step ID: <code>{event.workflowStepId}</code></p>}</li>
        })}</ol>
      </> : !errors.events && <p className="arm-empty">{status.events === 'cancelled' ? 'Event 조회를 취소했습니다.' : 'Run을 선택하면 Event를 확인할 수 있습니다.'}</p>}
      {pagination.total !== null && <div className="arm-event-pages"><p className="arm-note">전체 {pagination.total}개 · {events?.length ? `${pagination.offset + 1}–${pagination.offset + events.length}번째 기록` : '현재 구간 0개'}</p><div className="arm-actions"><button type="button" className="button" disabled={isLoading || pagination.offset === 0} onClick={() => onChangePage(Math.max(0, pagination.offset - pagination.limit))}>이전 기록</button><button type="button" className="button" disabled={isLoading || pagination.offset >= maxOffset} onClick={() => onChangePage(Math.min(maxOffset, pagination.offset + pagination.limit))}>다음 기록</button><button type="button" className="button" disabled={isLoading || pagination.offset === maxOffset} onClick={() => onChangePage(maxOffset)}>최신 기록</button></div></div>}
    </section>
  </section>
}
