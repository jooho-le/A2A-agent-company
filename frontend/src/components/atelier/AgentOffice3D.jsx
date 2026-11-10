import { useEffect, useRef, useState } from 'react'
import { mountAgentOffice } from './agentOfficeRuntime'
import { createRunSuccessCeremony } from './runSuccessCeremony'
import './AgentOffice3D.css'

const stages = [
  { name: '오케스트레이터', index: 4 }, { name: '계획에이전트', index: 0 },
  { name: '개발에이전트', index: 1 }, { name: 'QA에이전트', index: 2 }, { name: '보안에이전트', index: 3 },
]

// API 조회는 Dashboard의 Hook 한 곳에서만 수행합니다.
export default function AgentOffice3D({ requestText, motionEnabled = true, runDisplayState = null, displayMode: requestedMode, onDisplayModeChange }) {
  const selectedRunId = runDisplayState?.runId ?? null
  const [modeChoice, setModeChoice] = useState({ runId: null, mode: 'mock' })
  // 새 Run을 선택하면 실제 모드로 진입합니다. 같은 Run에서의 명시적인 목업 선택은 유지합니다.
  const displayMode = requestedMode ?? (modeChoice.runId === selectedRunId ? modeChoice.mode : selectedRunId ? 'actual' : 'mock')
  const chooseMode = mode => onDisplayModeChange ? onDisplayModeChange(mode) : setModeChoice({ runId: selectedRunId, mode })
  const actual = displayMode === 'actual'
  const [successCeremony] = useState(() => createRunSuccessCeremony())
  const rootRef = useRef(null)
  const runtimeRef = useRef(null)
  const inputsRef = useRef({ requestText, motionEnabled, displayMode, runDisplayState, successCeremony })
  const [error, setError] = useState('')

  useEffect(() => {
    successCeremony.observe(runDisplayState, displayMode)
    inputsRef.current = { requestText, motionEnabled, displayMode, runDisplayState, successCeremony }
    runtimeRef.current?.setRequest(requestText)
    runtimeRef.current?.setMotion(motionEnabled)
    runtimeRef.current?.setRunDisplay(displayMode, runDisplayState)
  }, [requestText, motionEnabled, displayMode, runDisplayState, successCeremony])

  useEffect(() => {
    let cancelled = false
    const root = rootRef.current
    // 원본 HTML에 내장된 렌더러를 지연 로드하며 전역 script나 iframe을 만들지 않습니다.
    import('./office3dScene').then((engine) => {
      if (cancelled) return
      runtimeRef.current = mountAgentOffice(root, inputsRef.current, engine)
    }).catch(() => {
      if (!cancelled) setError('3D 사무실을 표시할 수 없습니다. WebGL 지원과 브라우저 설정을 확인해주세요.')
    })
    return () => {
      cancelled = true
      runtimeRef.current?.dispose()
      runtimeRef.current = null
    }
  }, [])

  return <section className="agent-office-3d" ref={rootRef} aria-label={actual ? '멀티 에이전트 3D 오피스 실제 Run 상태' : '멀티 에이전트 3D 오피스 시연'} tabIndex={0}
    data-run-id={selectedRunId ?? undefined} data-run-data-availability={runDisplayState?.availability ?? 'idle'} data-display-mode={displayMode}>
    <div className="office3d-heading"><div><h3>에이전트 오피스</h3><p>오케스트레이터 · 계획에이전트 · 개발에이전트 · QA에이전트 · 보안에이전트</p></div><span className="office3d-mock">{actual ? '실제 업무' : '목업 시연'}</span></div>
    <div className="office3d-mode" role="group" aria-label="3D 표시 모드"><button type="button" aria-pressed={!actual} onClick={() => chooseMode('mock')}>목업 시연</button><button type="button" aria-pressed={actual} disabled={!selectedRunId} onClick={() => chooseMode('actual')}>실제 업무</button></div>
    <p className="office3d-disclaimer">{actual ? '실제 업무 기록에 따라 직원들의 상태를 자동으로 표시합니다. 캐릭터 움직임은 진행률을 뜻하지 않습니다.' : '3D 움직임·오류·완료는 시각적 시연입니다. 실제 Agent 실행 결과나 프로젝트 진행률이 아닙니다.'}</p>
    <div className="office3d-live-summary" hidden={!actual}><p data-office3d="live-notice" role="status" /><ul data-office3d="live-agents" aria-label="실제 Agent 상태" /><p data-office3d="success-notice" role="status" /><div className="office3d-demo-controls"><button type="button" data-office3d="success-play" disabled>성공 세리머니 재생</button><button type="button" data-office3d="success-reset">실제 성공 기록 초기화</button></div><p className="office3d-hint">재생 시작 전에 Run·포즈·다음 순서를 저장합니다. 재생 중 화면 이탈·새로고침 시 자동 재실행하지 않습니다. 발표는 한 탭에서 진행해주세요.</p></div>
    <div className="office3d-lead-summary"><strong>오케스트레이터 · 리더</strong><span>에이전트 호출 순서 관리 · 다음 작업 결정 및 진행</span></div>
    <nav className="office3d-workflow" aria-label="리더와 네 작업 에이전트 근접 보기">{stages.map(({ name, index }, place) => <button type="button" className="office3d-step" key={name} data-agent-index={index} title="캐릭터 근접 보기 · 서버 실행 상태는 변경하지 않습니다."><span>{place + 1}</span>{name}</button>)}</nav>
    <p className="office3d-hint office3d-handoff-note" data-office3d="handoff-note">{actual ? '문서 이동은 새로 확인된 A2A 호출·응답 이벤트를 나타냅니다. 실제 파일 전송이나 고정 순서를 의미하지 않습니다.' : '105초 목업의 문서 이동은 시연용이며 실제 Run 로그와 별개입니다.'}</p>
    <div className="office3d-live-summary" hidden><p data-office3d="trace-notice" role="status" /><ol data-office3d="trace-recent" aria-label="3D에서 처리한 실제 실행 이벤트" /><p className="office3d-hint">현재 Step 자세와 이벤트 연출은 구분됩니다. 새 이벤트는 저장 순서를 유지해 조회 이후 재생하며, 대상이 확인되지 않으면 기록만 표시합니다.</p></div>
    {error ? <p className="office3d-error" role="alert">{error}</p> : <>
      <div className="office3d-tools"><button type="button" data-office3d="home" aria-pressed="true">전체 보기</button><button type="button" data-office3d="fullscreen">전체 화면</button><button type="button" data-office3d="vectors" aria-pressed="false">에이전트 상태</button></div>
      <section className="office3d-actual-popups" data-office3d="actual-popups" aria-label="에이전트별 실제 업무 팝업" hidden>
        <p className="office3d-popup-notice" data-office3d="popup-result-notice" role="status" />
        <div className="office3d-popup-grid">{stages.map(({ name, index }) => <article className="office3d-work-popup" key={index} data-office3d={`actual-popup-${index}`} role="status" aria-atomic="true">
          <div className="office3d-popup-heading"><strong>{name}</strong><span data-office3d={`actual-status-${index}`} /></div>
          <small data-office3d={`actual-source-${index}`} /><h4 data-office3d={`actual-title-${index}`} /><p data-office3d={`actual-detail-${index}`} />
        </article>)}</div>
      </section>
      <div className="office3d-stage" data-office3d="stage">
        <div className="office3d-loading" data-office3d="loading" role="status">3D 사무실을 준비하고 있어요…</div>
        <article className="office3d-bubble" data-office3d="bubble" aria-live="polite" hidden><div className="office3d-bubble-role" data-office3d="bubble-role" /><span className="office3d-emotion" data-office3d="bubble-emotion" /><h4 data-office3d="bubble-title" /><p data-office3d="bubble-detail" /><div className="office3d-bubble-track"><div data-office3d="bubble-progress" /></div></article>
        <aside className="office3d-celebration" data-office3d="celebration" aria-live="polite" hidden><span data-office3d="finale-source">목업 시연 · 리더 + 4개 역할</span><h4 data-office3d="finale-title">협업 완료 시연</h4><p data-office3d="finale-caption">기존 협업 포즈</p><p data-office3d="finale-note">실제 코드 생성·검증 결과는 아닙니다.</p></aside>
        <section className="office3d-status" data-office3d="vector-panel" hidden><h4 data-office3d="selected">에이전트 상태</h4><pre data-office3d="vector-data" /></section>
      </div>
      <div className="office3d-player" hidden={actual}>
        <div className="office3d-caption"><p data-office3d="caption">시연을 재생해 업무 흐름을 살펴보세요.</p><small data-office3d="stepcount">작업 완료 0 / 4 · 리더 1</small></div>
        <div className="office3d-controls"><button type="button" className="office3d-play" data-office3d="play" aria-pressed="false" disabled={actual || !motionEnabled}>▶ 재생</button><label className="office3d-track"><span className="sr-only">3D 시연 재생 위치</span><input type="range" data-office3d="timeline" min="0" max="105" step="0.01" defaultValue="0" disabled={actual} /><span data-office3d="clock">00 / 105초</span></label><label className="office3d-speed"><span className="sr-only">3D 시연 재생 속도</span><select data-office3d="speed" defaultValue="1.5" disabled={actual}><option value="0.5">0.5배속</option><option value="1">1배속</option><option value="1.5">1.5배속</option></select></label></div>
        <div className="office3d-demo-controls"><label>시연 오류 연출<select data-office3d="error-mode" defaultValue="qa" disabled={actual}><option value="qa">QA 오류</option><option value="security">보안 오류</option><option value="none">정상 진행</option></select></label><button type="button" data-office3d="restart" disabled={actual || !motionEnabled}>시연 다시 시작</button></div>
        <div className="office3d-demo-controls"><span className="office3d-hint" data-office3d="finale-order">다음 세리머니를 확인하고 있어요.</span><button type="button" data-office3d="preview-finale" disabled={actual || !motionEnabled}>마무리 미리보기</button><button type="button" data-office3d="reset-finale" disabled={actual}>리허설 순서 초기화</button></div>
        <p className="office3d-hint">발표 시연 완료마다 1 → 2 → 3 순서로 한 포즈만 표시합니다. 미리보기는 순서를 바꾸지 않습니다.</p>
        <p className="office3d-hint">{motionEnabled ? '전체 보기에서 드래그로 회전 · 스크롤 또는 두 손가락으로 확대' : '모션이 꺼져 있습니다. 타임라인을 직접 조절해 상태를 확인할 수 있습니다.'}</p>
        <p className="office3d-hint" data-office3d="notice" role="status" />
      </div>
    </>}
  </section>
}
