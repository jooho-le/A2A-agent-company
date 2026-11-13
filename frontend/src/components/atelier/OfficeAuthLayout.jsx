import '../../pages/LoginPage.css'
import { humanAgents } from './humanAgents'
import AuthRoleAccent from './AuthRoleAccent'

const staff = [
  { index: 0, side: 'left' },
  { index: 1, side: 'left' },
  { index: 4, side: 'left' },
  { index: 2, side: 'right' },
  { index: 3, side: 'right' },
]

function WorkAccent({ role }) {
  if (role === 'orchestrator' || role === 'security') return null
  return <svg className="office-auth-work-accent" viewBox="0 0 120 120" fill="none" aria-hidden="true">
      <rect className="auth-glass" x="6" y="16" width="108" height="88" rx="12" />
      <path className="auth-muted-line" d="M7 35h106" />
      <circle className="auth-lime-fill" cx="18" cy="26" r="3" />
      <circle className="auth-lime-fill" cx="29" cy="26" r="3" opacity=".5" />
      {role === 'planner' && <>
        <path className="auth-lime-line" d="m20 52 5 5 9-11m-14 30 5 5 9-11" />
        <path className="auth-muted-line" d="M46 53h47M46 77h35" />
      </>}
      {role === 'developer' && <>
        <path className="auth-lime-line" d="m38 51-15 15 15 15m44-30 15 15-15 15M67 49 53 83" />
      </>}
      {role === 'qa' && <>
        <circle className="auth-muted-line" cx="49" cy="64" r="18" />
        <path className="auth-muted-line" d="m62 78 17 17" />
        <path className="auth-lime-line" d="m79 57 7 7 13-16" />
      </>}
  </svg>
}

function StaffSurround({ side, reviewAccents = false }) {
  return <div className={`office-auth-cast office-auth-cast--${side}`} aria-hidden="true">
    {staff.filter(person => person.side === side).map(person => {
      const agent = humanAgents[person.index]
      return <figure className={`office-auth-character office-auth-character--${agent.id}`} key={agent.id}>
        {reviewAccents ? <AuthRoleAccent role={agent.id} /> : <WorkAccent role={agent.id} />}
        <img src={`/characters/login-${agent.id}.png`} alt="" width="768" height="960" draggable="false" />
        {!reviewAccents && <span className="office-auth-sparks" />}
      </figure>
    })}
  </div>
}

// 인증 기능은 각 페이지에 두고 오피스의 시각적 스타일만 공유합니다.
export default function OfficeAuthLayout({ children, reviewAccents = false }) {
  return <main className={`auth-page office-auth${reviewAccents ? ' office-auth--review-accents' : ''}`}>
    <div className="office-auth-shell">
      <header className="office-auth-brand">
        <span className="office-auth-monogram" aria-hidden="true">A</span>
        <span>AI 에이전트 회사<small>MY AI COMPANY</small></span>
      </header>
      <div className="office-auth-mobile-team" aria-hidden="true">{[4, 0, 1, 2, 3].map(index =>
        <img key={humanAgents[index].id} src={`/characters/login-${humanAgents[index].id}.png`} alt="" width="768" height="960" draggable="false" />)}</div>
      <div className="office-auth-layout">
        <StaffSurround side="left" reviewAccents={reviewAccents} />
        <div className="office-auth-form">{children}</div>
        <StaffSurround side="right" reviewAccents={reviewAccents} />
      </div>
      <footer className="office-auth-footer">AI 에이전트 회사<span>기획 · 개발 · 품질 · 보안</span></footer>
    </div>
  </main>
}
