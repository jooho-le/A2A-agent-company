import { useEffect, useRef, useState } from 'react'
import { mountAgentOffice } from './agentOfficeRuntime'
import { createRunSuccessCeremony } from './runSuccessCeremony'
import { Icon } from './AtelierArtwork'
import './AgentOffice3D.css'

const stages = [
  { name: '오케스트레이터', index: 4 }, { name: '계획에이전트', index: 0 },
  { name: '개발에이전트', index: 1 }, { name: 'QA에이전트', index: 2 }, { name: '보안에이전트', index: 3 },
]

// 시간표가 아닌 Dashboard에서 확인한 Run·Step·Trace만 관측합니다.
export default function AgentOffice3D({ motionEnabled = true, runDisplayState = null, requestPanel, panelOpen = true, onPanelChange, onRefresh }) {
  const [successCeremony] = useState(() => createRunSuccessCeremony())
  const rootRef = useRef(null)
  const runtimeRef = useRef(null)
  const inputsRef = useRef({ motionEnabled, runDisplayState, successCeremony })
  const [error, setError] = useState('')
  const selectedRunId = runDisplayState?.runId ?? null

  useEffect(() => {
    successCeremony.observe(runDisplayState, 'actual')
    inputsRef.current = { motionEnabled, runDisplayState, successCeremony }
    runtimeRef.current?.setMotion(motionEnabled)
    runtimeRef.current?.setRunDisplay('actual', runDisplayState)
  }, [motionEnabled, runDisplayState, successCeremony])
  useEffect(() => {
    let cancelled = false
    import('./office3dScene').then(engine => {
      if (!cancelled) runtimeRef.current = mountAgentOffice(rootRef.current, inputsRef.current, engine)
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
    <div className="office3d-tools"><button type="button" data-office3d="home" aria-pressed="true">전체 보기</button><button type="button" data-office3d="fullscreen">전체 화면</button><button type="button" data-office3d="vectors" aria-pressed="false">직원 상태</button></div>
    <button type="button" className="office3d-panel-toggle" aria-controls="office-workspace-panel" aria-expanded={panelOpen} onClick={() => onPanelChange?.(!panelOpen)}><Icon name="plan" /><span>{panelOpen ? '패널 접기' : '업무 패널'}</span><Icon name="chevron" /></button>
    <aside className="office3d-console" id="office-workspace-panel" aria-label="업무 요청과 진행 상황" hidden={!panelOpen}>
      <div className="office3d-console-heading"><span className="eyebrow">YOUR COMPANY, IN MOTION.</span><h2>아이디어를, 현실로.</h2><p>업무를 맡기고 팀이 만드는 과정을 지켜보세요.</p></div>
      {requestPanel}
      <details className="office3d-inspector" open><summary><span>우리 팀의 업무</span><Icon name="chevron" /></summary>
        {!selectedRunId && <p className="office3d-disclaimer">아직 진행 중인 업무가 없어요. 업무를 맡기거나 프로젝트에서 기존 업무를 열어주세요.</p>}
        <div className="office3d-live-summary"><p data-office3d="live-notice" role="status" /><ul data-office3d="live-agents" aria-label="직원 업무 상태" /><p data-office3d="success-notice" role="status" />
          {selectedRunId && <button type="button" className="button" onClick={onRefresh}>상태 새로고침</button>}
        </div>
        <nav className="office3d-workflow" aria-label="직원 자리 보기">{stages.map(({ name, index }, place) => <button type="button" className="office3d-step" key={name} data-agent-index={index}><span>{place + 1}</span>{name}</button>)}</nav>
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
    </aside>
  </section>
}
