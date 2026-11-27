// 직원 원본 이미지 참조와 역할별 3D 외형을 관리합니다.
// 원본 PNG는 보존합니다. 직원 카드에는 현재 3D 모델을 정면 렌더링한 office-*.png를 사용합니다.
export const humanAgents = [
  { id: 'planner', name: '계획에이전트', initial: '계', portrait: '/characters/office-planner.png', appearance: { skin: '#e4b18b', hair: '#75422f', shirt: '#8798aa', pants: '#56616d', hairStyle: 'bun', glasses: '#596b7b', badge: '#8194a8' } },
  { id: 'developer', name: '개발에이전트', initial: '개', portrait: '/characters/office-developer.png', appearance: { skin: '#b97b53', hair: '#342b29', shirt: '#a196b6', pants: '#41434b', hairStyle: 'side-part', glasses: '#74619c', badge: '#9d89b3' } },
  { id: 'qa', name: 'QA에이전트', initial: 'Q', portrait: '/characters/office-qa.png', appearance: { skin: '#e0b696', hair: '#42312a', shirt: '#819785', pants: '#aaa087', hairStyle: 'bob', glasses: null, badge: '#6f947c' } },
  { id: 'security', name: '보안에이전트', initial: '보', portrait: '/characters/office-security.png', appearance: { skin: '#e3ba9b', hair: '#262a31', shirt: '#384963', pants: '#74777c', hairStyle: 'short', glasses: null, badge: '#607fa3' } },
  // 네 작업 Agent의 기존 인덱스를 유지합니다. 리더는 다섯 번째 Step 역할이 아닙니다.
  { id: 'orchestrator', name: '오케스트레이터', initial: 'O', portrait: '/characters/office-orchestrator.png', leadership: { title: '오케스트레이터', badge: '리더', summary: '에이전트 호출 순서 관리 · 다음 작업 결정 및 진행' }, appearance: { skin: '#e5b79a', hair: '#282027', shirt: '#43546d', pants: '#514954', hairStyle: 'short', glasses: null, badge: '#c7aa68' } },
]
