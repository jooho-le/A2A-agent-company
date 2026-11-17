import { useEffect, useRef, useState } from 'react'
import { mountAgentOffice } from './agentOfficeRuntime'
import { createRunSuccessCeremony } from './runSuccessCeremony'
import { Icon } from './AtelierArtwork'
import { roleLabel } from './roleLabels'
import './AgentOffice3D.css'

const stages = [
  { name: '오케스트레이터', index: 4 }, { name: '계획에이전트', index: 0 },
  { name: '개발에이전트', index: 1 }, { name: 'QA에이전트', index: 2 }, { name: '보안에이전트', index: 3 },
]

// 시간표가 아닌 Dashboard에서 확인한 Run·Step·Trace만 관측합니다.
export default function AgentOffice3D({ motionEnabled = true, runDisplayState = null, requestPanel, panelOpen = true, onPanelChange, onRefresh, onProgress, workPanel, onAgentSelect }) {
  const [successCeremony] = useState(() => createRunSuccessCeremony())
  const rootRef = useRef(null)
  const runtimeRef = useRef(null)
  const inputsRef = useRef({ motionEnabled, runDisplayState, successCeremony, onAgentSelect })
  const [error, setError] = useState('')
  const selectedRunId = runDisplayState?.runId ?? null

  useEffect(() => {
    successCeremony.observe(runDisplayState, 'actual')
    inputsRef.current = { motionEnabled, runDisplayState, successCeremony, onAgentSelect }
    runtimeRef.current?.setMotion(motionEnabled)
    runtimeRef.current?.setRunDisplay('actual', runDisplayState)
  }, [motionEnabled, runDisplayState, successCeremony, onAgentSelect])
  useEffect(() => {
    let cancelled = false
    import('./office3dScene').then(engine => {
      if (!cancelled) runtimeRef.current = mountAgentOffice(rootRef.current, { ...inputsRef.current, onAgentSelect: index => inputsRef.current.onAgentSelect?.(index) }, engine)
    }).catch(() => {
      if (!cancelled) setError('3D 오피스를 표시할 수 없습니다. 브라우저의 WebGL 지원을 확인해 주세요.')
    })
    return () => { cancelled = true; runtimeRef.current?.dispose(); runtimeRef.current = null }
  }, [])

  return <section className="agent-office-3d" ref={rootRef} aria-label="실시간 에이전트 오피스"
    data-run-id={selectedRunId ?? undefined} data-run-data-availability={runDisplayState?.availability ?? 'idle'} data-display-mode="actual">
    <div className="office3d-stage" data-office3d="stage">
      <div className="office3d-loading" data-office3d="loading" role={error ? 'alert' : 'status'}>{error || '오피스를 준비하고 있어요…'}</div>
      <article className="office3d-bubble" data-office3d="bubble" hidden />
      <aside className="office3d-celebration" data-office3d="celebration" aria-live="polite" hidden>
        <span data-office3d="finale-source" /><h4 data-office3d="finale-title" /><p data-office3d="finale-caption" /><p data-office3d="finale-note" />
      </aside>
    </div>
    <section className="office3d-status" data-office3d="vector-panel" hidden><h4 data-office3d="selected">직원 상태</h4><pre data-office3d="vector-data" /></section>
    <details className="office3d-tools office3d-view-options"><summary>화면 보기</summary><div><button type="button" data-office3d="home" aria-pressed="true">전체 보기</button><button type="button" data-office3d="fullscreen">전체 화면</button><button type="button" data-office3d="vectors" aria-pressed="false">직원 상태</button></div></details>
    <button type="button" className="office3d-panel-toggle" aria-controls="office-workspace-panel" aria-expanded={panelOpen} onClick={() => onPanelChange?.(!panelOpen)}><Icon name="plan" /><span>{panelOpen ? '접기' : selectedRunId ? '작업 보기' : '요청 쓰기'}</span><Icon name="chevron" /></button>
    <aside className="office3d-console" id="office-workspace-panel" aria-label="업무 요청과 진행 상황" hidden={!panelOpen}>
      <details className="office3d-request" key={'request:' + (selectedRunId || 'new')} open={!selectedRunId}><summary>요청창<Icon name="chevron" /></summary>{requestPanel}</details>
      <div className="office3d-work-panel">{workPanel}</div>
      <details className="office3d-inspector" open={!workPanel && Boolean(selectedRunId)}><summary><span>{workPanel ? '진행 기록 더 보기' : '진행 상황'}</span><Icon name="chevron" /></summary>
        {!selectedRunId && <p className="office3d-disclaimer">요청을 보내면 직원들이 작업을 시작해요.</p>}
        <div className="office3d-live-summary"><p data-office3d="live-notice" role="status" /><ul data-office3d="live-agents" aria-label="직원 업무 상태" /><p data-office3d="success-notice" role="status" />
          {selectedRunId && Object.values(runDisplayState?.errors || {}).some(Boolean) && <button type="button" className="button" onClick={onRefresh}>다시 확인</button>}
          {selectedRunId && <button type="button" className="button" onClick={onProgress}>진행 상황 보기<Icon name="arrow" /></button>}
        </div>
        <details className="office3d-advanced"><summary>자세히 보기</summary>
        {selectedRunId && <button type="button" className="button" onClick={onRefresh}>새로고침</button>}
        <nav className="office3d-workflow" aria-label="직원 자리 보기">{stages.map(({ name, index }, place) => <button type="button" className="office3d-step" key={name} data-agent-index={index}><span>{place + 1}</span>{roleLabel(['planner', 'developer', 'qa', 'security', 'orchestrator'][index], name)}</button>)}</nav>
        <p className="office3d-hint" data-office3d="handoff-note" />
        <div className="office3d-live-summary"><p data-office3d="trace-notice" role="status" /><ol data-office3d="trace-recent" aria-label="최근 업무 기록" /></div>
        <section className="office3d-actual-popups" data-office3d="actual-popups" aria-label="직원별 작업 내용" hidden>
          <p className="office3d-popup-notice" data-office3d="popup-result-notice" role="status" />
          <div className="office3d-popup-grid">{stages.map(({ name, index }) => <article className="office3d-work-popup" key={index} data-office3d={'actual-popup-' + index}>
            <div className="office3d-popup-heading"><strong>{name}</strong><span data-office3d={'actual-status-' + index} /></div>
            <small data-office3d={'actual-source-' + index} /><h4 data-office3d={'actual-title-' + index} /><p data-office3d={'actual-detail-' + index} />
          </article>)}</div>
        </section>
        <p className="office3d-hint" data-office3d="notice" role="status" />
        </details>
      </details>
    </aside>
  </section>
}
