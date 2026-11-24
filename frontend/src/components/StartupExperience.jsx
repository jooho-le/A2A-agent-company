import { useEffect, useState } from 'react'
import { humanAgents } from './atelier/humanAgents'
import './StartupExperience.css'

const team = [[4, '총괄'], [0, '기획'], [1, '개발'], [2, '품질'], [3, '보안']]

// 같은 오피스 위에서 오프닝을 연출합니다. 앱과 실제 업무 상태는 다시 만들지 않습니다.
export default function StartupExperience({ children }) {
  const [phase, setPhase] = useState('opening')
  const visible = phase !== 'finished'

  useEffect(() => {
    if (!visible) return
    const leave = window.setTimeout(() => setPhase(current => current === 'finished' ? current : 'leaving'), 3600)
    const finish = window.setTimeout(() => setPhase('finished'), 4000)
    return () => { window.clearTimeout(leave); window.clearTimeout(finish) }
  }, [visible])

  useEffect(() => {
    if (!visible) return
    const escape = event => {
      if (event.key === 'Escape') setPhase('finished')
    }
    window.addEventListener('keydown', escape)
    return () => window.removeEventListener('keydown', escape)
  }, [visible])

  return <>
    <div className="startup-content" data-startup-active={visible ? '' : undefined}
      inert={visible ? true : undefined} aria-hidden={visible ? true : undefined}>
      {children}
    </div>
    {visible && <section className="startup-splash" data-phase={phase} aria-label="AIO 시작 화면">
      <div className="startup-backdrop" aria-hidden="true" />
      <div className="startup-light" aria-hidden="true" />
      <div className="startup-door startup-door-left" aria-hidden="true"><span>YOUR</span></div>
      <div className="startup-door startup-door-right" aria-hidden="true"><span>COMPANY.</span></div>
      <div className="startup-seam" aria-hidden="true" />
      <header className="startup-header">
        <span className="startup-wordmark"><span>A</span>AIO</span>
        <span className="startup-header-note">A COMPANY OF YOUR OWN.</span>
        <button type="button" className="startup-skip" onClick={() => setPhase('finished')}>
          건너뛰기 <span aria-hidden="true">↗</span>
        </button>
      </header>
      <div className="startup-story">
        <p className="startup-eyebrow"><span />YOUR IDEA. YOUR COMPANY.</p>
        <h1>
          <span className="startup-line"><span>아이디어 하나로,</span></span>
          <span className="startup-line startup-line-accent"><span>당신만의 회사.</span></span>
        </h1>
        <p className="startup-description">ALL IN ONE — 기획부터 개발·검증까지, 하나의 팀으로.</p>
        <div className="startup-signature"><span className="startup-signature-rule" /><span>당신의 다음 시작을 위해.</span></div>
      </div>
      <div className="startup-roster" aria-hidden="true">
        <span className="startup-roster-heading">FIVE ROLES. ONE TEAM.</span>
        <div className="startup-people">{team.map(([index, role], order) => <div className="startup-person" key={role} style={{ '--person-delay': `${1.4 + order * .11}s` }}>
          <span className="startup-avatar"><img src={humanAgents[index].portrait} alt="" decoding="async" /></span>
          <span>{role}</span>
        </div>)}</div>
      </div>
      <div className="startup-welcome" aria-hidden="true"><span>THE OFFICE IS YOURS.</span><strong>어서 오세요, 대표님.</strong></div>
      <footer className="startup-footer">
        <p role="status"><span className="startup-status-dot" />당신의 오피스로 들어가는 중</p>
        <div className="startup-progress" aria-hidden="true"><span /></div>
        <span>IDEA → TEAM → REALITY</span>
      </footer>
    </section>}
  </>
}
