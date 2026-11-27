import { humanAgents } from './humanAgents'
import './AgentLeadership.css'

// 실제 dispatch나 진행 상태가 아닌, 데모 캐릭터의 역할 관계를 안내합니다.
export default function AgentLeadership() {
  const leader = humanAgents[4]
  return <section className="agent-leadership" aria-label="오케스트레이터 리더와 네 작업 에이전트">
    <div className="leadership-heading"><strong>오케스트레이터가 협업 흐름을 관리해요</strong><span>역할 안내</span></div>
    <div className="leadership-leader"><div><strong>{leader.name}<small>Orchestrator · {leader.leadership.badge}</small></strong><p>{leader.leadership.summary}</p></div></div>
    <div className="leadership-branches" aria-label="함께 협업하는 전문 역할">{[
      ['계획에이전트', 'Planner', '요청 분석 · 작업 순서와 기능 설정'], ['개발에이전트', 'Developer', '개발'], ['QA에이전트', 'QA', '요구사항 테스트 · 보고'], ['보안에이전트', 'Security', '보안 검토'],
    ].map(([label, role, task]) => <div className="leadership-member" key={role}><strong>{label}<small>{role}</small></strong><span>{task}</span></div>)}</div>
    <p className="leadership-note">오케스트레이터는 별도의 작업 Step 없이 전체 흐름을 조율하는 리더입니다. QA와 보안은 병렬로 검토할 수 있고, 확인된 결과에 따라 개발 재작업과 재검증으로 이어집니다.</p>
  </section>
}
