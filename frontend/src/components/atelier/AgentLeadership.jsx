import { humanAgents } from './humanAgents'
import './AgentLeadership.css'

// 실제 dispatch나 진행 상태가 아닌, 데모 캐릭터의 역할 관계를 안내합니다.
export default function AgentLeadership() {
  const leader = humanAgents[0]
  return <section className="agent-leadership" aria-label="기획 총괄 중심의 팀 역할 구조">
    <div className="leadership-heading"><strong>기획 총괄을 중심으로 협업해요</strong><span>역할 안내 · 목업</span></div>
    <div className="leadership-leader"><span className="leadership-mark">P</span><div><strong>{leader.leadership.title}<small>Planner · {leader.leadership.badge}</small></strong><p>{leader.leadership.summary}</p></div></div>
    <div className="leadership-branches" aria-label="함께 협업하는 전문 역할">{[
      ['개발', 'Developer', '기능 구현'], ['품질', 'QA', '품질 검증'], ['보안', 'Security', '보안 검토'],
    ].map(([label, role, task]) => <div className="leadership-member" key={role}><strong>{label}<small>{role}</small></strong><span>{task}</span></div>)}</div>
    <p className="leadership-note">개발 계획과 역할을 조율하는 기획 총괄, 각 분야를 맡는 전문 에이전트의 관계입니다.</p>
  </section>
}
