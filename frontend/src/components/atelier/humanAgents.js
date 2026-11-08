// 직원 PNG와 3D 모델이 같은 역할·색상·외형을 사용하도록 관리합니다.
// PNG는 제공된 원본이며, 3D는 기존 관절 모델과 시연 애니메이션을 유지합니다.
export const humanAgents = [
  { id: 'planner', name: '기획 에이전트', initial: '기', portrait: '/characters/planning-agent.png', leadership: { title: '기획 총괄', badge: '팀 리더', summary: '개발 계획 · 역할 조율' }, appearance: { skin: '#e4b18b', hair: '#75422f', shirt: '#bda262', pants: '#655349', hairStyle: 'bun', glasses: '#c3a064', badge: '#b89b60' } },
  { id: 'developer', name: '개발 에이전트', initial: '개', portrait: '/characters/development-agent.png', appearance: { skin: '#b97b53', hair: '#342b29', shirt: '#a196b6', pants: '#41434b', hairStyle: 'side-part', glasses: '#74619c', badge: '#9d89b3' } },
  { id: 'qa', name: 'QA 에이전트', initial: 'Q', portrait: '/characters/qa-agent.png', appearance: { skin: '#e0b696', hair: '#42312a', shirt: '#819785', pants: '#aaa087', hairStyle: 'bob', glasses: null, badge: '#6f947c' } },
  { id: 'security', name: '보안 에이전트', initial: '보', portrait: '/characters/security-agent.png', appearance: { skin: '#e3ba9b', hair: '#262a31', shirt: '#384963', pants: '#74777c', hairStyle: 'short', glasses: null, badge: '#607fa3' } },
]
