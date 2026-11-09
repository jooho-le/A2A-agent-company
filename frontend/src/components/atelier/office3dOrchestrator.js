import { humanAgents } from './humanAgents'
import { addOrchestratorFringe } from './office3dAppearance'

export const orchestratorIndex = 4

// 기존 인물·책상 생성기를 재사용합니다. 서버나 실제 Agent 역할을 추가하지 않습니다.
export function addOfficeOrchestrator(office, engine) {
  const { createOfficeCharacter, createOfficeDesk, createOfficeWorkProp } = engine
  const index = orchestratorIndex
  const desk = createOfficeDesk(office.workspace, 0, 0, index)
  const agent = createOfficeCharacter(office.workspace, index, [0, 0, -.9])
  agent.chair = desk.getObjectByName(`task_chair_${index}`)
  agent.g.userData.managementServer = true
  // 앞머리·정수리·긴 뒷머리를 한 곡면으로 이어 관자놀이의 빈틈을 없앱니다.
  const oldHair = agent.head.children.find(part => part.name.startsWith('styled_hair_'))
  if (oldHair) oldHair.visible = false
  addOrchestratorFringe(agent, engine)
  office.agents.push(agent)
  office.props.push(createOfficeWorkProp(office.workspace, index, 0, 0))
  office.positions.push([0, 0, 0])
  office.animated.push(agent.g, agent.upper, agent.head, agent.eyes, agent.headphones, agent.lower, agent.chair,
    ...agent.legs.flatMap(leg => [leg.thigh, leg.shin]), ...agent.arms.flatMap(arm => [arm.shoulder, arm.elbow]))
}

export function managementDisplay(snapshot) {
  const refreshing = snapshot?.queryStatus?.run === 'loading'
  const valid = snapshot?.runId && snapshot?.queryStatus?.run === 'success' && snapshot.run?.status
  const status = snapshot?.run?.status
  const state = !valid ? 'unknown' : ['FINISHED', 'ABORTED'].includes(status) ? 'idle'
    : status === 'RECEIVED' ? 'pending' : ['WAITING_INPUT', 'HUMAN_REVIEW'].includes(status) ? 'waiting_input' : 'working'
  return { agentRole: 'ORCHESTRATOR', displayState: state, label: !valid ? refreshing ? '조회 중' : '상태 확인 불가'
    : ['FINISHED', 'ABORTED'].includes(status) ? '실행 관리 종료' : state === 'pending' ? '요청 접수' : state === 'waiting_input' ? '입력 대기' : '실행 관리 중',
  expression: 'normal', refreshing, step: null, poseState: state === 'working' ? 'working' : 'waiting',
  hasConfirmedState: Boolean(valid), managementServer: true }
}

// 임베드 글꼴의 한글 누락과 글자 넘침을 피하도록 모든 명찰에 같은 텍스처 방식을 사용합니다.
export function officeLabelTexture(text, engine, color = '#17212a') {
  const canvas = document.createElement('canvas')
  canvas.width = 1024; canvas.height = 256
  const context = canvas.getContext('2d')
  context.font = '600 132px system-ui, "Apple SD Gothic Neo", "Malgun Gothic", sans-serif'
  context.textAlign = 'center'; context.textBaseline = 'middle'; context.fillStyle = color
  context.fillText(text, 512, 128, 950)
  const texture = new engine.OfficeCanvasTexture(canvas)
  texture.colorSpace = engine.ct
  return texture
}

export function updateOfficeBadges(office, engine) {
  office.agents.forEach((agent, index) => {
    const badge = agent.upper.getObjectByName(`role_badge_${index}`)
    const old = badge.getObjectByName(`role_badge_text_${index}`)
    if (old) { badge.remove(old); old.geometry.dispose() }
    // 기존 판보다 여유 있게 배치해 긴 '오케스트레이터'도 잘리지 않게 합니다.
    badge.scale.set(1.18, 1.05, 1)
    const label = new engine.OfficeMesh(new engine.OfficePlaneGeometry(.56, .16), new engine.OfficeBasicMaterial({
      map: officeLabelTexture(humanAgents[index].name, engine), transparent: true, toneMapped: false,
    }))
    label.position.set(.01, 0, .019)
    label.name = `clean_role_badge_${index}`
    label.userData.text = humanAgents[index].name
    badge.userData.role = humanAgents[index].name
    agent.g.userData.role = humanAgents[index].name
    badge.add(label)
  })
}
