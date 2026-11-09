// 전체 보기의 자리 배치는 유지하고, 모든 캐릭터의 근접 장면을 정면에서 촬영합니다.
export function createAgentCloseupCamera(office, engine) {
  const { P } = engine
  const target = new P(), eye = new P(), forward = new P(), cameraDirection = new P()
  let focusedAgent = -1
  const visibility = new Map()

  function restoreStations() {
    visibility.forEach((visible, object) => { object.visible = visible })
    visibility.clear()
    focusedAgent = -1
  }

  function isolate(agentIndex) {
    if (focusedAgent === agentIndex) return
    restoreStations()
    focusedAgent = agentIndex
    const conceal = object => {
      if (!object || visibility.has(object)) return
      visibility.set(object, object.visible)
      object.visible = false
    }
    office.agents.forEach((agent, index) => {
      if (index === agentIndex) return
      conceal(agent.g)
      conceal(office.workspace.getObjectByName(`desk_${index}`))
      conceal(office.props[index])
    })
    // 옆 수납장과 공용 화분도 얼굴·상체 앞에 걸리지 않게 근접 촬영 동안만 숨깁니다.
    office.workspace.children?.filter(object => object.name.startsWith('shared_')).forEach(conceal)
    if (agentIndex !== 2) conceal(office.cup)
  }

  // 원래 사진은 전체 보기 각도로 촬영되어 있어 카메라와 함께 돌리면 원근이 틀어집니다.
  // 근접 장면에서는 같은 사진의 상단을 배경으로 두고 얼굴과 책상을 촬영합니다.
  const backdrop = office.backdrop.clone()
  backdrop.name = 'agent_closeup_background'
  backdrop.geometry = office.backdrop.geometry.clone()
  const uv = backdrop.geometry.getAttribute('uv')
  for (let i = 0; i < uv.count; i++) uv.setY(i, .45 + uv.getY(i) * .55)
  uv.needsUpdate = true
  backdrop.visible = false
  office.root.add(backdrop)

  function hide() {
    restoreStations()
    backdrop.visible = false
    office.backdrop.visible = true
  }

  // 가시성만 조정하는 공통 도우미입니다. 정면 촬영은 모든 역할에 update()를 사용합니다.
  function focus(agentIndex) {
    isolate(agentIndex)
    backdrop.visible = false
    office.backdrop.visible = true
  }

  function update(camera, width, height, agentIndex) {
    // 카메라가 앞줄 책상과 사람을 통과하더라도 선택한 인물만 촬영합니다.
    // 실제 자리·상태는 옮기지 않으며 전체 보기에서는 원래 가시성을 복원합니다.
    isolate(agentIndex)
    const agent = office.agents[agentIndex]
    agent.head.getWorldPosition(target)
    // 머리의 작은 작업 모션은 살리고, 착석 방향을 기준으로 흔들림 없는 정면을 잡습니다.
    forward.set(Math.sin(agent.seatYaw), 0, Math.cos(agent.seatYaw))
    // 개발자는 노트북에 얼굴이 가리지 않도록 정면의 조금 높은 위치에서 촬영합니다.
    const developer = agentIndex === 1
    eye.copy(target).addScaledVector(forward, developer ? 10 : 12)
    eye.y += developer ? 2.1 : -1.44
    camera.position.copy(eye)
    camera.lookAt(target)
    camera.zoom = 1
    // 위쪽 대사 영역과 얼굴이 겹치지 않도록 얼굴을 아래쪽에 배치합니다.
    camera.setViewOffset(width, height, 0, -height * .13, width, height)
    camera.updateMatrixWorld()

    camera.getWorldDirection(cameraDirection)
    const distance = 35
    const viewHeight = 2 * distance * Math.tan(camera.fov * Math.PI / 360)
    const imageAspect = backdrop.geometry.parameters.width / backdrop.geometry.parameters.height / .55
    const photoHeight = Math.max(viewHeight * 1.5, viewHeight * camera.aspect * 1.05 / imageAspect)
    backdrop.position.copy(camera.position).addScaledVector(cameraDirection, distance)
    backdrop.quaternion.copy(camera.quaternion)
    backdrop.scale.set(photoHeight * imageAspect / backdrop.geometry.parameters.width, photoHeight / backdrop.geometry.parameters.height, 1)
    backdrop.visible = true
    office.backdrop.visible = false
    return target
  }

  return { update, hide, focus }
}
