import { humanAgents } from './humanAgents'
import { roleLabel } from './roleLabels'
import { StudioObject } from './StudioVisuals'
import { Icon } from './AtelierArtwork'

// 조회된 Run과 Trace만 표시합니다. 역할 소개와 실제 실행 기록을 구분합니다.
export default function WorkflowRunView({ runId, projectName, monitor, displayState, status, onNew, onResults, workPanel }) {
  if (!runId) return <div className="empty-state workflow-empty studio-empty">
    <div className="studio-empty-art"><StudioObject kind="monitor" /></div>
    <h2>아직 시작한 작업이 없어요</h2>
    <p>만들고 싶은 것을 알려주세요.</p>
    <button type="button" className="button start" onClick={onNew}>새 업무 맡기기<Icon name="arrow" /></button>
  </div>

  const entries = monitor.trace?.entries || []
  const hasError = Object.values(monitor.errors || {}).some(Boolean)
  return <>
    <div className="workflow-summary"><div><h2>{projectName || '우리 팀의 업무 흐름'}</h2></div>
      <button type="button" className="button start" onClick={onResults}>결과 보기<Icon name="arrow" /></button></div>
    <p role={hasError ? 'alert' : 'status'} className="workflow-status">{status} {hasError && <button type="button" className="button" onClick={monitor.refresh}>다시 확인</button>}</p>
    {workPanel && <div className="workflow-work-controls">{workPanel}</div>}
    <div className="workflow-run-layout">
      <details className="workflow-staff-overview ux-details"><summary>직원 상태 한눈에 보기</summary>
      <section className="workflow-people studio-flowboard" aria-labelledby="workflow-people-title"><header><h3 id="workflow-people-title">직원 상태</h3><span className="studio-flow-leader" aria-hidden="true"><img src={humanAgents[4].portrait} alt="" />팀장</span></header>
        <ol>{displayState.agents.map((agent, index) => {
          const employee = humanAgents.find(person => person.id.toUpperCase() === agent.agentRole)
          return <li key={agent.agentRole} data-state={agent.displayState} data-studio-tilt="" data-palette={index % 5}>
            {employee ? <img src={employee.portrait} alt="" /> : <Icon name="team" />}
            <div><strong>{roleLabel(agent.agentRole, agent.name)}</strong><span>{agent.label}</span></div>
          </li>
        })}</ol>
      </section></details>
      <details className="workflow-records ux-details"><summary>작업 기록 보기 <span className="count-pill">{entries.length}</span></summary>
        <button type="button" className="button" onClick={monitor.refresh}>새로고침</button>
        {entries.length ? <ol className="workflow-event-list">{entries.map(({ event }) => <li key={event.eventId}>
          <span className="workflow-event-dot" aria-hidden="true" /><div><strong>{event.actor}</strong><p>{event.eventType}</p><time dateTime={event.occurredAt}>{event.occurredAt}</time></div>
        </li>)}</ol> : <p className="workflow-records-empty">아직 기록이 없어요.</p>}
      </details>
    </div>
  </>
}
