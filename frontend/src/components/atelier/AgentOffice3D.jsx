import { useEffect, useRef, useState } from 'react'
import { mountAgentOffice } from './agentOfficeRuntime'
import { finaleOptions } from './office3dPerformance'
import './AgentOffice3D.css'

const stages = ['기획 총괄', '개발', 'QA', '보안']

// 입력은 표시용 요청과 모션 설정뿐입니다. Run·Step·Event API는 호출하지 않습니다.
export default function AgentOffice3D({ requestText, motionEnabled = true }) {
  const rootRef = useRef(null)
  const runtimeRef = useRef(null)
  const inputsRef = useRef({ requestText, motionEnabled })
  const [error, setError] = useState('')

  useEffect(() => {
    inputsRef.current = { requestText, motionEnabled }
    runtimeRef.current?.setRequest(requestText)
    runtimeRef.current?.setMotion(motionEnabled)
  }, [requestText, motionEnabled])

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

  return <section className="agent-office-3d" ref={rootRef} aria-label="멀티 에이전트 3D 오피스 시연" tabIndex={0}>
    <div className="office3d-heading"><div><h3>에이전트 오피스</h3><p>Planner · Developer · QA · Security</p></div><span className="office3d-mock">목업 시연</span></div>
    <p className="office3d-disclaimer">3D 움직임·오류·완료는 시각적 시연입니다. 실제 Agent 실행 결과나 프로젝트 진행률이 아닙니다.</p>
    <div className="office3d-lead-summary"><strong>기획 총괄 · Planner</strong><span>개발 계획과 협업 방향을 정리하는 팀 리더</span></div>
    <nav className="office3d-workflow" aria-label="3D 시연 단계">{stages.map((stage, index) => <button type="button" className="office3d-step" key={stage}><span>{index + 1}</span>{stage}</button>)}</nav>
    {error ? <p className="office3d-error" role="alert">{error}</p> : <>
      <div className="office3d-tools"><button type="button" data-office3d="home" aria-pressed="true">전체 보기</button><button type="button" data-office3d="fullscreen">전체 화면</button><button type="button" data-office3d="vectors" aria-pressed="false">에이전트 상태</button></div>
      <div className="office3d-stage" data-office3d="stage">
        <div className="office3d-loading" data-office3d="loading" role="status">3D 사무실을 준비하고 있어요…</div>
        <article className="office3d-bubble" data-office3d="bubble" aria-live="polite" hidden><div className="office3d-bubble-role" data-office3d="bubble-role" /><span className="office3d-emotion" data-office3d="bubble-emotion" /><h4 data-office3d="bubble-title" /><p data-office3d="bubble-detail" /><div className="office3d-bubble-track"><div data-office3d="bubble-progress" /></div></article>
        <aside className="office3d-celebration" data-office3d="celebration" aria-live="polite" hidden><span>목업 시연 · 4 / 4</span><h4>협업 완료 시연</h4><p data-office3d="finale-caption">기존 협업 포즈</p><p>실제 코드 생성·검증 결과는 아닙니다.</p></aside>
        <section className="office3d-status" data-office3d="vector-panel" hidden><h4 data-office3d="selected">에이전트 상태</h4><pre data-office3d="vector-data" /></section>
      </div>
      <div className="office3d-player">
        <div className="office3d-caption"><p data-office3d="caption">시연을 재생해 업무 흐름을 살펴보세요.</p><small data-office3d="stepcount">완료 0 / 4</small></div>
        <div className="office3d-controls"><button type="button" className="office3d-play" data-office3d="play" aria-pressed="false" disabled={!motionEnabled}>▶ 재생</button><label className="office3d-track"><span className="sr-only">3D 시연 재생 위치</span><input type="range" data-office3d="timeline" min="0" max="105" step="0.01" defaultValue="0" /><span data-office3d="clock">00 / 105초</span></label><label className="office3d-speed"><span className="sr-only">3D 시연 재생 속도</span><select data-office3d="speed" defaultValue="1.5"><option value="0.5">0.5배속</option><option value="1">1배속</option><option value="1.5">1.5배속</option></select></label></div>
        <div className="office3d-demo-controls"><label>시연 오류 연출<select data-office3d="error-mode" defaultValue="qa"><option value="qa">QA 오류</option><option value="security">보안 오류</option><option value="none">정상 진행</option></select></label><button type="button" data-office3d="restart" disabled={!motionEnabled}>시연 다시 시작</button></div>
        <div className="office3d-demo-controls"><label>마무리 연출<select data-office3d="finale-mode" defaultValue="team">{finaleOptions.map(({ value, label }) => <option value={value} key={value}>{label}</option>)}</select></label><button type="button" data-office3d="preview-finale" disabled={!motionEnabled}>마무리 미리보기</button></div>
        <p className="office3d-hint">{motionEnabled ? '전체 보기에서 드래그로 회전 · 스크롤 또는 두 손가락으로 확대' : '모션이 꺼져 있습니다. 타임라인을 직접 조절해 상태를 확인할 수 있습니다.'}</p>
        <p className="office3d-hint" data-office3d="notice" role="status" />
      </div>
    </>}
  </section>
}
